"""#901 Q11 (spec §5.3a, 901-PA-001 b; G2): latest session metadata through
two partial indexes.

``_session_latest_meta_map`` resolves, per session, the latest non-NULL
``cwd`` and the latest non-NULL ``git_branch`` independently (they may come
from different rows), ordered ``timestamp_utc DESC, id DESC``. It used one
window query that sorted every touched session's whole history; it now runs
two ``LIMIT 1`` lookups per session, each served by a partial index on
``conversation_messages(session_id, timestamp_utc, id)``. Conversations
migration 011 delivers the indexes to existing stores.

Every result is held to two references over the same rows: a plain Python
scan, and the frozen window query it replaced.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script, redirect_paths

MIGRATION = "011_conversation_latest_meta_indexes"
INDEXES = {
    "idx_conv_session_latest_cwd": "cwd",
    "idx_conv_session_latest_git_branch": "git_branch",
}
#: The pre-Q11 implementation, verbatim.
LEGACY_WINDOW_SQL = (
    "SELECT DISTINCT session_id, "
    "  FIRST_VALUE(cwd) OVER ("
    "    PARTITION BY session_id "
    "    ORDER BY (cwd IS NULL), timestamp_utc DESC, id DESC), "
    "  FIRST_VALUE(git_branch) OVER ("
    "    PARTITION BY session_id "
    "    ORDER BY (git_branch IS NULL), timestamp_utc DESC, id DESC) "
    "FROM conversation_messages WHERE session_id IN (%s)"
)
LONG = 3000


def _db():
    load_script()
    import _cctally_db

    return _cctally_db


def _query():
    import _lib_conversation_query

    return _lib_conversation_query


def _insert(conn, rows):
    conn.executemany(
        "INSERT INTO conversation_messages (id, session_id, uuid, source_path,"
        " byte_offset, timestamp_utc, entry_type, cwd, git_branch)"
        " VALUES (?,?,?,?,?,?,'human',?,?)", rows)
    conn.commit()


def _seed(conn):
    """Long histories with sparse metadata, empty strings, NULL columns and
    timestamps, and ties broken by id."""
    rows = []
    next_id = 1

    def add(session, ts, cwd=None, branch=None, row_id=None):
        nonlocal next_id
        row_id = next_id if row_id is None else row_id
        next_id = max(next_id, row_id) + 1
        rows.append((row_id, session, f"{session}-{row_id}", f"{session}.jsonl",
                     row_id, ts, cwd, branch))

    base = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    def stamp(i):
        return (base + dt.timedelta(seconds=i)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")

    # sparse: cwd only on the first ten rows, a branch only at row 1500.
    for i in range(LONG):
        add("sparse", stamp(i), cwd=f"/early/{i}" if i < 10 else None,
            branch="mid" if i == 1500 else None)
    # dense-then-empty: the newest row's cwd is an empty string (a value).
    for i in range(LONG):
        add("emptied", stamp(i), cwd="" if i == LONG - 1 else f"/d/{i % 7}",
            branch=f"b{i % 5}")
    # ties: the latest second carries two rows; the larger id wins each column.
    add("ties", stamp(10), cwd="/tie/old", branch="old")
    add("ties", stamp(20), cwd="/tie/low-id", branch="low", row_id=next_id + 5)
    add("ties", stamp(20), cwd="/tie/high-id", branch=None,
        row_id=next_id + 50)
    add("ties", stamp(20), cwd=None, branch="tie-branch", row_id=next_id + 30)
    # NULL timestamps sort below every real one; alone they still count.
    add("null-ts", None, cwd="/null-ts/only", branch="null-ts-branch")
    add("null-ts", None, cwd="/null-ts/higher-id")
    add("mixed-ts", None, cwd="/mixed/null-ts", branch="null-branch")
    add("mixed-ts", stamp(5), cwd="/mixed/dated")
    # all-NULL metadata.
    for i in range(50):
        add("all-null", stamp(i))
    _insert(conn, rows)
    return ["sparse", "emptied", "ties", "null-ts", "mixed-ts", "all-null",
            "missing", "sparse"]


def _python_reference(conn, session_ids):
    rows = conn.execute(
        "SELECT session_id, timestamp_utc, id, cwd, git_branch"
        " FROM conversation_messages").fetchall()
    out = {sid: (None, None) for sid in session_ids}

    def key(row):
        # SQLite orders NULL below every TEXT value.
        return (row[1] is not None, row[1] or "", row[2])

    for sid in out:
        mine = [row for row in rows if row[0] == sid]
        cwd = [row for row in mine if row[3] is not None]
        branch = [row for row in mine if row[4] is not None]
        out[sid] = (max(cwd, key=key)[3] if cwd else None,
                    max(branch, key=key)[4] if branch else None)
    return out


def _legacy(conn, session_ids):
    out = {sid: (None, None) for sid in session_ids}
    sql = LEGACY_WINDOW_SQL % ",".join("?" for _ in session_ids)
    for sid, cwd, branch in conn.execute(sql, list(session_ids)):
        out[sid] = (cwd, branch)
    return out


def _plans(conn, query):
    """The statements one call runs, with their plans."""
    seen = []
    conn.set_trace_callback(seen.append)
    try:
        query._session_latest_meta_map(conn, ["sparse"])
    finally:
        conn.set_trace_callback(None)
    lookups = [sql for sql in seen if "FROM conversation_messages" in sql]
    # The trace may or may not expand bound parameters; bind when it did not.
    return [
        (sql, [str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, ("sparse",) if "?" in sql else ())])
        for sql in lookups
    ]


def _assert_parity_and_plans(conn):
    query = _query()
    ids = _seed(conn)
    result = query._session_latest_meta_map(conn, ids)
    assert result == _python_reference(conn, ids) == _legacy(conn, ids)
    assert result["sparse"] == ("/early/9", "mid")
    assert result["emptied"][0] == "", "an empty string is a value, not NULL"
    assert result["ties"] == ("/tie/high-id", "tie-branch")
    assert result["null-ts"] == ("/null-ts/higher-id", "null-ts-branch")
    assert result["mixed-ts"] == ("/mixed/dated", "null-branch")
    assert result["all-null"] == result["missing"] == (None, None)
    plans = _plans(conn, query)
    assert len(plans) == 2, plans
    for (sql, plan), (index, column) in zip(plans, INDEXES.items()):
        assert sql.endswith("LIMIT 1") and f"{column} IS NOT NULL" in sql, sql
        assert plan[0].startswith(
            f"SEARCH conversation_messages USING INDEX {index} (session_id=?)"
        ), plan
        assert not any("TEMP B-TREE" in step for step in plan), plan


def test_latest_meta_matches_both_references_on_the_conversations_schema():
    db = _db()
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        _assert_parity_and_plans(conn)
    finally:
        conn.close()


def test_the_legacy_window_query_sorted_the_whole_session():
    """Non-vacuity: the statement the lookups replace built temp b-trees."""
    db = _db()
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        plan = [str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + LEGACY_WINDOW_SQL % "?", ("sparse",))]
    finally:
        conn.close()
    assert any("USE TEMP B-TREE" in step for step in plan), plan


def test_an_empty_request_reads_nothing():
    db = _db()
    conn = sqlite3.connect(":memory:")
    seen = []
    try:
        db._apply_conversations_schema(conn)
        conn.set_trace_callback(seen.append)
        assert _query()._session_latest_meta_map(conn, []) == {}
    finally:
        conn.close()
    assert seen == []


# ── index delivery: fresh install, upgrade, rebuild and re-upgrade ──────────

def _index_sql(conn):
    return {
        name: sql for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
            " AND name IN (?, ?)", tuple(INDEXES))
    }


def _assert_indexes(conn):
    found = _index_sql(conn)
    assert set(found) == set(INDEXES), found
    for name, column in INDEXES.items():
        normalized = " ".join(found[name].split())
        assert normalized == (
            f"CREATE INDEX {name} ON conversation_messages(session_id,"
            f" timestamp_utc, id) WHERE {column} IS NOT NULL"), normalized


def _drop_indexes_and_trim(path, *, rows=()):
    """What a 010-head store, or an older binary's trim of 011, looks like."""
    raw = sqlite3.connect(path)
    try:
        for name in INDEXES:
            raw.execute(f"DROP INDEX IF EXISTS {name}")
        raw.execute("DELETE FROM schema_migrations WHERE name=?", (MIGRATION,))
        raw.execute("PRAGMA user_version=10")
        if rows:
            _insert(raw, rows)
        raw.commit()
    finally:
        raw.close()


def _transcript(tmp_path, count):
    session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    lines = []
    for i in range(count):
        record = {
            "cwd": f"/synthetic/delivery/{i // 50}",
            "message": {"content": [{"text": f"turn {i}", "type": "text"}],
                        "role": "user"},
            "sessionId": session, "type": "user", "uuid": f"d-u{i}",
            "timestamp": (dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
                          + dt.timedelta(seconds=i)).strftime(
                              "%Y-%m-%dT%H:%M:%S.000Z")}
        if i % 40 == 0:
            record["gitBranch"] = f"g{i}"
        lines.append(json.dumps(record) + "\n")
    path = tmp_path / ".claude" / "projects" / "-delivery" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines))
    return session


def _deliver(ns, tmp_path, delivery):
    import _cctally_core

    path = _cctally_core.CONVERSATIONS_DB_PATH
    ns["open_conversations_db"](attach_cache=False).close()
    if delivery == "upgrade":
        _drop_indexes_and_trim(path)
    elif delivery == "rebuild":
        session = _transcript(tmp_path, 400)
        conn = ns["open_conversations_db"]()
        try:
            ns["sync_claude_conversations"](conn)
            ns["sync_claude_conversations"](conn, rebuild=True)
            assert conn.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE"
                " session_id=?", (session,)).fetchone()[0] == 400
        finally:
            conn.close()
    elif delivery == "re-upgrade":
        _drop_indexes_and_trim(path)
        ns["open_conversations_db"](attach_cache=False).close()
        # An older binary trims the marker and keeps writing without the
        # indexes; the current binary converges again.
        _drop_indexes_and_trim(path, rows=[
            (900_001, "late", "late-1", "late.jsonl", 1,
             "2026-06-09T00:00:00.000Z", "/late", "late-branch")])
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 1
        _assert_indexes(conn)
        if delivery == "rebuild":
            session = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            result = _query()._session_latest_meta_map(conn, [session])
            assert result == _python_reference(conn, [session]) == _legacy(
                conn, [session])
            assert result[session] == ("/synthetic/delivery/7", "g360")
            conn.execute("DELETE FROM conversation_messages")
            conn.commit()
        _assert_parity_and_plans(conn)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "delivery", ["fresh-install", "upgrade", "rebuild", "re-upgrade"])
def test_both_indexes_reach_every_store(tmp_path, monkeypatch, delivery):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["CONFIG_PATH"].write_text('{"conversation":{"retention_days":0}}\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    _deliver(ns, tmp_path, delivery)
