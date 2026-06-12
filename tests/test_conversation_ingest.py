import json
import sqlite3, sys, pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _cctally_db as db

from conftest import load_script, redirect_paths

def _fresh():
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    return conn

def test_schema_creates_conversation_messages():
    conn = _fresh()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")}
    assert {"id", "session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
            "timestamp_utc", "entry_type", "text", "blocks_json", "model", "msg_id",
            "req_id", "cwd", "git_branch", "is_sidechain"} <= cols

def test_storage_dedup_is_source_path_byte_offset_not_uuid():
    conn = _fresh()
    # same uuid replayed across two files must BOTH persist (resume replay)
    for path in ("a.jsonl", "b.jsonl"):
        conn.execute(
            "INSERT OR IGNORE INTO conversation_messages"
            "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
            " VALUES('s','dup',?,0,'t','human','x','[]',0)", (path,))
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 2
    # re-inserting the same (source_path,byte_offset) is idempotent
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
        " VALUES('s','dup','a.jsonl',0,'t','human','x','[]',0)")
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 2

def test_fts_present_when_available_and_indexes_text():
    conn = _fresh()
    if not db._fts5_available(conn):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','token limit window','[]',0)")
    hits = conn.execute("SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH 'token'").fetchall()
    assert len(hits) == 1
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is None  # not set when FTS works

def test_delete_propagates_to_fts():
    conn = _fresh()
    if not db._fts5_available(conn):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','findme','[]',0)")
    conn.execute("DELETE FROM conversation_messages")
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'findme'").fetchone()[0] == 0

def test_fts_unavailable_sets_flag_and_skips_fts_table(monkeypatch):
    # The documented test seam: when the sqlite build lacks FTS5, _apply_cache_schema
    # must create NEITHER the virtual table NOR the triggers, and persist the flag so
    # search falls back to LIKE.
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is not None and flag[0] == "1"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_fts" not in names
    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not any(t.startswith("conv_fts_") for t in trigs)

def test_update_text_reindexes_fts():
    conn = _fresh()
    if not db._fts5_available(conn):
        import pytest; pytest.skip("sqlite build lacks FTS5")
    conn.execute("INSERT INTO conversation_messages"
                 "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
                 " VALUES('s','u','f',0,'t','assistant','alpha','[]',0)")
    conn.execute("UPDATE conversation_messages SET text='beta' WHERE uuid='u'")
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'").fetchone()[0] == 0


# ──────────────────────────────────────────────────────────────────────────
# #177 S6: the split multi-column conversation_fts(text, search_tool,
# search_thinking) replaces the old prose + aux pair — present / delete-
# propagates / per-column-update-reindexes — plus the FTS-unavailable drop and
# the create-failure all-or-nothing envelope under the single fts5_unavailable
# flag.
# ──────────────────────────────────────────────────────────────────────────

def _insert_split(conn, uuid, off, *, text="", search_tool="", search_thinking="",
                  entry_type="assistant"):
    conn.execute(
        "INSERT INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool,search_thinking)"
        " VALUES('s',?,?,?, 't',?,?,'[]',0,?,?)",
        (uuid, f"f{off}", off, entry_type, text, search_tool, search_thinking),
    )


def test_split_fts_present_when_available_and_indexes_columns():
    conn = _fresh()
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    _insert_split(conn, "u", 0, text="PROSETOKEN", search_tool="TOOLTOKEN command",
                  search_thinking="THINKTOKEN")
    # each column matchable via the column-filter syntax; prose-only and
    # tool-only stay disjoint.
    assert conn.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{text}: PROSETOKEN'"
    ).fetchall() == [(1,)]
    assert conn.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_tool}: TOOLTOKEN'"
    ).fetchall() == [(1,)]
    assert conn.execute(
        "SELECT rowid FROM conversation_fts WHERE conversation_fts MATCH '{search_thinking}: THINKTOKEN'"
    ).fetchall() == [(1,)]
    # a tool-only token does NOT match the {text} column filter.
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH '{text}: TOOLTOKEN'"
    ).fetchone()[0] == 0


def test_split_delete_propagates_to_fts():
    conn = _fresh()
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    _insert_split(conn, "u", 0, search_tool="findtool")
    conn.execute("DELETE FROM conversation_messages")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'findtool'"
    ).fetchone()[0] == 0


def test_split_update_reindexes_each_column():
    conn = _fresh()
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    _insert_split(conn, "u", 0, search_tool="alphatool", search_thinking="alphathink")
    conn.execute("UPDATE conversation_messages SET search_tool='betatool' WHERE uuid='u'")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'betatool'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alphatool'"
    ).fetchone()[0] == 0
    # the AU trigger fires on any of text/search_tool/search_thinking — a
    # search_thinking update reindexes too.
    conn.execute("UPDATE conversation_messages SET search_thinking='betathink' WHERE uuid='u'")
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'betathink'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_fts_unavailable_drops_table_under_one_flag(monkeypatch):
    """The single fts5_unavailable flag covers the consolidated index — when
    FTS5 is unavailable conversation_fts does not exist and no conv_fts_* trigger
    survives. (The old conversation_fts_aux is gone under the split shape.)"""
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is not None and flag[0] == "1"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_fts" not in names
    assert "conversation_fts_aux" not in names
    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not any(t.startswith("conv_fts_") for t in trigs)


def test_split_fts_create_failure_drops_and_insert_still_commits(monkeypatch):
    """If the split FTS create fails, the schema apply must drop the index +
    trigger set, set the fts5_unavailable flag, and a later
    conversation_messages INSERT must still commit (the shared write txn — which
    also carries session_entries cost ingest — is NOT rolled back)."""
    conn = sqlite3.connect(":memory:")
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")

    # Fail the split-table create by feeding a malformed DDL through the seam the
    # apply uses (the _CONV_FTS_SPLIT_DDL execute). Simulate by monkeypatching
    # _create_conversation_fts_triggers to raise AFTER the table create — that
    # exercises the same except-arm cleanup (drop table + trigger set + flag).
    def _boom(c):
        raise sqlite3.OperationalError("simulated split fts trigger create failure")

    monkeypatch.setattr(db, "_create_conversation_fts_triggers", _boom)
    db._apply_cache_schema(conn)

    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "conversation_fts" not in names, "split FTS must be dropped on create failure"
    assert "conversation_fts_aux" not in names
    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert not any(t.startswith("conv_fts_") for t in trigs), "trigger set dropped"
    flag = conn.execute("SELECT value FROM cache_meta WHERE key='fts5_unavailable'").fetchone()
    assert flag is not None and flag[0] == "1", "the shared flag is set"

    # The load-bearing assertion: a conversation_messages INSERT still commits —
    # no orphan trigger over a missing table rolls back the shared write txn.
    conn.execute(
        "INSERT INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,"
        " blocks_json,is_sidechain,search_tool)"
        " VALUES('s','u','f',0,'t','assistant','x','[]',0,'a')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1


# ──────────────────────────────────────────────────────────────────────────
# Task 4: sync_cache second seek-and-walk ingest + lifecycle (real path)
#
# Driven against the real ``sync_cache`` via the established
# ``load_script() + redirect_paths()`` harness (per the "HOME-only test
# loader reads prod DB" gotcha — bare ``setenv(HOME)`` would read the
# developer's real cache.db once ``_cctally_core`` is cached). The harness
# pins every path constant (including ``CACHE_DB_PATH`` and the Claude
# projects dir at ``<tmp>/.claude/projects``) to the per-test tmp tree.
# ──────────────────────────────────────────────────────────────────────────


def _asst_line(uuid, msg_id, req_id, text, *, ts="2026-06-01T00:00:00Z",
               model="claude-opus-4-7", out_tokens=5):
    """One assistant JSONL line carrying both a cost (msg_id/req_id/usage)
    AND a conversation surface (content text)."""
    return json.dumps({
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s1",
        "requestId": req_id,
        "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": out_tokens,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _user_line(uuid, text, *, ts="2026-06-01T00:01:00Z"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "message": {"role": "user", "content": text},
    }) + "\n"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Load bin/cctally under a redirected tmp data dir + Claude projects
    tree. Returns (ns, conn, projects_dir, sync)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_cache_db"]()
    sync_cache = ns["sync_cache"]

    def sync(rebuild=False):
        return sync_cache(conn, rebuild=rebuild)

    yield ns, conn, projects, sync
    try:
        conn.close()
    except Exception:
        pass


def _conv_rows(conn):
    return conn.execute(
        "SELECT uuid, source_path, byte_offset, entry_type, text, msg_id, model "
        "FROM conversation_messages ORDER BY source_path, byte_offset"
    ).fetchall()


def test_iter_sync_entries_parses_each_line_once(monkeypatch):
    """#138 acceptance: the fused walker parses each JSONL line exactly once
    (one json.loads per line), yielding a (offset, cost, msgrow) triple per
    productive line. The pre-#138 design walked the delta byte range TWICE
    (cost walk then conversation seek-and-walk), parsing every line twice."""
    import io
    import _cctally_cache as cache
    body = (_asst_line("a1", "m1", "r1", "one")
            + _user_line("u1", "two")
            + _asst_line("a2", "m2", "r2", "three"))
    fh = io.StringIO(body)

    calls = {"n": 0}
    real = cache.json.loads

    def spy(s, *a, **k):
        calls["n"] += 1
        return real(s, *a, **k)

    monkeypatch.setattr(cache.json, "loads", spy)
    out = list(cache._iter_sync_entries(fh, "/p/a.jsonl"))

    assert calls["n"] == 3, f"one json.loads per line expected, got {calls['n']}"
    assert len(out) == 3
    o0, c0, m0 = out[0]
    o1, c1, m1 = out[1]
    o2, c2, m2 = out[2]
    # assistant lines yield BOTH a cost tuple and a message row; user line is
    # a message row only.
    assert c0 is not None and m0 is not None and m0.entry_type == "assistant"
    assert c1 is None and m1 is not None and m1.entry_type == "human"
    assert c2 is not None and m2 is not None
    # cost tuple shape is (UsageEntry, msg_id, req_id)
    assert c0[1] == "m1" and c0[2] == "r1"
    # offsets are byte-accurate and strictly increasing; the triple's offset
    # equals the message row's own byte_offset.
    assert o0 == 0 and o1 == m1.byte_offset and o2 == m2.byte_offset
    assert o0 < o1 < o2


def test_iter_sync_entries_partial_tail_rewinds():
    """A mid-write tail line (no trailing newline) rewinds the handle and stops,
    so fh.tell() after the loop is the cost cursor's final_offset and the next
    sync re-reads the line once complete — same discipline as the leaf walkers."""
    import io
    import _cctally_cache as cache
    complete = _asst_line("a1", "m1", "r1", "ok")
    partial = '{"type":"assistant","uuid":"a2"'  # no newline
    fh = io.StringIO(complete + partial)
    out = list(cache._iter_sync_entries(fh, "/p/a.jsonl"))
    assert len(out) == 1
    assert fh.tell() == len(complete)  # rewound to start of the partial line
    assert fh.readline().startswith('{"type":"assistant","uuid":"a2"')


def test_sync_ingests_messages_and_dedups_replay(isolated):
    """Two files: b.jsonl resumes a.jsonl, replaying a1's uuid. Both physical
    rows are stored (UNIQUE(source_path, byte_offset)); the uuid is only
    indexed, never a dedup key. Reader-level dedup is Plan 2."""
    ns, conn, projects, sync = isolated
    a1 = _asst_line("a1", "m1", "r1", "answer one")
    (projects / "a.jsonl").write_text(a1)
    (projects / "b.jsonl").write_text(a1 + _user_line("u2", "next"))

    sync(rebuild=True)

    # 3 physical message rows: a1 in a.jsonl, a1 replayed in b.jsonl, u2.
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(DISTINCT uuid) FROM conversation_messages"
    ).fetchone()[0] == 2
    # Both a1 rows are assistants carrying msg_id 'm1' + model.
    a_rows = conn.execute(
        "SELECT entry_type, msg_id, model FROM conversation_messages "
        "WHERE uuid='a1'"
    ).fetchall()
    assert len(a_rows) == 2
    assert all(r == ("assistant", "m1", "claude-opus-4-7") for r in a_rows)
    # u2 is a human prompt with prose indexed.
    u_row = conn.execute(
        "SELECT entry_type, text FROM conversation_messages WHERE uuid='u2'"
    ).fetchone()
    assert u_row == ("human", "next")


def test_sync_delta_append_ingests_only_new_messages(isolated):
    """An append-only growth must ingest only the newly-appended message,
    not re-walk from offset 0 — the conversation walk shares the cost walk's
    [start_offset, final_offset] region."""
    ns, conn, projects, sync = isolated
    f = projects / "a.jsonl"
    f.write_text(_asst_line("a1", "m1", "r1", "first"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 1
    first_offset = conn.execute(
        "SELECT byte_offset FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()[0]
    assert first_offset == 0

    with f.open("a") as fh:
        fh.write(_user_line("u2", "second"))
    sync()
    rows = _conv_rows(conn)
    assert [r[0] for r in rows] == ["a1", "u2"]
    # The appended row starts AFTER the first line (delta resume, not a
    # re-walk from 0).
    assert rows[1][2] > 0


def test_sync_cursor_consistency_byte_offset_unchanged(isolated):
    """The conversation walk re-seeks the SAME handle the cost walk used; it
    must NOT corrupt session_files.last_byte_offset (written from the cost
    walk's final_offset). The recorded offset must equal the file size."""
    ns, conn, projects, sync = isolated
    f = projects / "a.jsonl"
    body = _asst_line("a1", "m1", "r1", "x") + _user_line("u2", "y")
    f.write_text(body)
    sync()
    last_off = conn.execute(
        "SELECT last_byte_offset FROM session_files WHERE path=?", (str(f),)
    ).fetchone()[0]
    assert last_off == f.stat().st_size, (
        "last_byte_offset must reflect the cost walk's final_offset (== EOF), "
        "never be clobbered by the conversation walk's fh movement"
    )


def test_sync_rebuild_clears_conversation_messages(isolated):
    """A --rebuild full-clear empties conversation_messages then re-ingests
    only what is currently on disk."""
    ns, conn, projects, sync = isolated
    f = projects / "a.jsonl"
    f.write_text(_asst_line("a1", "m1", "r1", "keep") + _user_line("u2", "drop"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 2

    # Shrink the on-disk content, then rebuild: the table must reflect only
    # the new content, never stale rows.
    f.write_text(_asst_line("a9", "m9", "r9", "fresh"))
    sync(rebuild=True)
    rows = _conv_rows(conn)
    assert [r[0] for r in rows] == ["a9"]


def test_sync_truncation_full_resets_then_reingests(isolated):
    """Truncating a tracked file (size shrinks) escalates to a full reset of
    conversation_messages (parallel to session_entries) then re-ingests every
    file from offset 0. No stale rows from the pre-truncation content survive."""
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(
        _asst_line("a1", "m1", "r1", "alpha")
        + _user_line("u1", "alpha2")
    )
    fb.write_text(_asst_line("b1", "mb", "rb", "bravo"))
    sync()
    assert sorted(r[0] for r in _conv_rows(conn)) == ["a1", "b1", "u1"]

    # Truncate A (shrinks below tracked size) -> escalation full reset.
    fa.write_text(_asst_line("a2", "m2", "r2", "rewritten"))
    stats = sync()
    assert stats.files_reset_truncated >= 1
    rows = sorted(r[0] for r in _conv_rows(conn))
    # B preserved (re-ingested), A's old rows gone, A's new row present. No
    # stale 'a1'/'u1' rows linger from the pre-truncation content.
    assert rows == ["a2", "b1"], rows


def test_sync_cost_cardinality_one_session_entry_per_turn(isolated):
    """Cost-cardinality scaffolding (Plan 2 verifies the SUM end-to-end). A
    single assistant turn produces exactly ONE session_entries row keyed on
    (msg_id, req_id), even though it also produces a conversation_messages
    row. The two indexes are independent: messages are keyed by physical line,
    cost by logical turn."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_asst_line("a1", "m1", "r1", "one turn"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM session_entries WHERE msg_id='m1' AND req_id='r1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE msg_id='m1'"
    ).fetchone()[0] == 1


def test_sync_cost_path_unchanged_session_entries_present(isolated):
    """Belt-and-suspenders: adding the second walk must NOT disturb the cost
    rows — session_entries is still populated with the right token totals."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "m1", "r1", "x", out_tokens=42)
    )
    sync()
    row = conn.execute(
        "SELECT input_tokens, output_tokens FROM session_entries "
        "WHERE msg_id='m1' AND req_id='r1'"
    ).fetchone()
    assert row == (10, 42)


# ──────────────────────────────────────────────────────────────────────────
# Task 5: backfill_conversation_messages walker (existing-install path)
# ──────────────────────────────────────────────────────────────────────────


def test_backfill_populates_existing_install_and_is_idempotent(isolated):
    """Simulate the pre-feature state: cost already ingested (session_files
    cursors at EOF, session_entries populated) but conversation_messages
    EMPTY. The backfill walker populates it from all JSONL at offset 0, and a
    second run is a no-op (INSERT OR IGNORE on (source_path, byte_offset))."""
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1"))
    fb.write_text(_asst_line("b1", "mb", "rb", "world"))

    # Cost-ingest only (this is what a pre-feature cache already had), then
    # wipe conversation_messages to model the upgrade state.
    sync()
    conn.execute("DELETE FROM conversation_messages")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 0
    # session cost cursors are at EOF (cost was ingested).
    offs = dict(conn.execute(
        "SELECT path, last_byte_offset FROM session_files"
    ).fetchall())
    assert all(v > 0 for v in offs.values()), offs

    backfill = ns["backfill_conversation_messages"]
    inserted = backfill(conn)
    n1 = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0]
    assert n1 == 3, n1
    assert inserted == 3
    # Cost cursors UNTOUCHED by the backfill (it must not move offsets).
    offs2 = dict(conn.execute(
        "SELECT path, last_byte_offset FROM session_files"
    ).fetchall())
    assert offs2 == offs

    # Idempotent re-run: no new rows.
    inserted2 = backfill(conn)
    assert inserted2 == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == n1


def test_backfill_migration_stamped_not_run_on_fresh_install(tmp_path, monkeypatch):
    """A fresh install (no session_entries) must STAMP the backfill migration
    without invoking its handler — there is no history to populate. Opening a
    brand-new cache.db must leave 002 marked applied with conversation_messages
    empty and no error."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Fresh open: no JSONL on disk, brand-new cache.db.
    conn = ns["open_cache_db"]()
    try:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name='002_conversation_messages_backfill'"
        ).fetchone()
        assert applied is not None, (
            "fresh install must STAMP the backfill migration (framework "
            "fresh-install fast-path)"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_existing_install_defers_backfill_to_sync_then_consumes_flag(isolated, monkeypatch):
    """REGRESSION (Plan 1 Task 5; deferral is issue #139): on an EXISTING
    install the 002 handler must NOT walk the JSONL history inline — that
    blocked the triggering command, even a stats-only ``cctally report`` that
    fires the cache dispatcher but never reads cache.db. Instead the handler
    sets the ``conversation_backfill_pending`` cache_meta flag and returns (the
    dispatcher central-stamps the migration marker on the handler's clean
    return, #140); the next ``sync_cache`` — which already holds the flock + owns the
    walker — consumes the flag and runs the offset-0 backfill once.

    Models the upgrade state precisely: cost already ingested (session_entries
    non-empty, session_files cursors at EOF) but conversation_messages empty AND
    no 002 marker. Spies on the real walker to prove WHO runs it and WHEN:
      1. After the dispatcher run the marker is persisted and the flag is SET,
         but the backfill has NOT run (spy 0) and conversation_messages is still
         empty — the command returns without the walk.
      2. The first ``sync_cache`` consumes the flag: backfill runs exactly once
         (spy 1), conversation_messages is populated, and the flag is cleared.
      3. A second ``sync_cache`` does NOT re-run the backfill (spy still 1, flag
         already gone) — stable row counts alone are insufficient since INSERT
         OR IGNORE keeps them stable even on a redundant re-walk.
      4. A second dispatcher run does NOT re-invoke the handler (the marker
         persists), so the flag is never re-set.
    """
    import _cctally_db as db
    import _cctally_cache as cache  # the module whose global sync_cache calls
    ns, conn, projects, sync = isolated
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1"))
    fb.write_text(_asst_line("b1", "mb", "rb", "world"))

    # Cost-ingest (populates session_entries + session_files cursors at EOF and,
    # on this fresh cache, stamps 002 via the fresh-install fast-path). Then
    # rewind to the pre-feature upgrade state: drop the 002 marker, empty
    # conversation_messages + any pending flag, and roll PRAGMA user_version
    # back to 1 (the value a cache.db carried when only cache 001 was registered
    # — before 002 shipped). That reproduces exactly what an upgrading install
    # looks like: schema_migrations exists with 001 applied, session_entries
    # non-empty (=> NOT fresh), 002 pending, user_version < len(registry) so the
    # dispatcher's user_version fast-path does NOT short-circuit the walk.
    sync()
    conn.execute(
        "DELETE FROM schema_migrations "
        "WHERE name='002_conversation_messages_backfill'"
    )
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("DELETE FROM cache_meta WHERE key='conversation_backfill_pending'")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM schema_migrations "
        "WHERE name='002_conversation_messages_backfill'"
    ).fetchone() is None
    assert conn.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0] > 0

    # Spy on the real walker. sync_cache calls the bare module global
    # ``backfill_conversation_messages``; rebinding it on the module patches the
    # exact name sync_cache resolves at call time.
    calls = {"n": 0}
    real_backfill = cache.backfill_conversation_messages

    def _spy(c):
        calls["n"] += 1
        return real_backfill(c)

    monkeypatch.setattr(cache, "backfill_conversation_messages", _spy)

    # (1) Dispatcher run: 002 pending on an existing install -> handler sets the
    # flag and the dispatcher central-stamps the marker (#140), but does NOT walk.
    db._run_pending_migrations(
        conn, registry=db._CACHE_MIGRATIONS, db_label="cache.db",
    )
    assert calls["n"] == 0, "handler must NOT walk inline — it only sets a flag"
    assert conn.execute(
        "SELECT 1 FROM schema_migrations "
        "WHERE name='002_conversation_messages_backfill'"
    ).fetchone() is not None, (
        "the dispatcher must central-stamp 002's marker on the handler's clean "
        "return (#140) — even on the existing-install path"
    )
    assert conn.execute(
        "SELECT value FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() == ("1",), "handler must set the pending flag"
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 0, "no inline backfill — the command returns un-stalled"

    # (2) First sync consumes the flag: backfill runs ONCE, index populated,
    # flag cleared.
    sync()
    assert calls["n"] == 1, "the first sync after the flag must run the backfill"
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, "sync must clear the pending flag after backfilling"

    # (3) Second sync does NOT re-run the backfill (flag gone).
    sync()
    assert calls["n"] == 1, (
        "a later sync must NOT re-walk — the flag was cleared, so the one-time "
        "backfill never repeats"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 3

    # (4) Second dispatcher run: the persisted marker keeps the handler from
    # re-running, so the flag is never re-set.
    db._run_pending_migrations(
        conn, registry=db._CACHE_MIGRATIONS, db_label="cache.db",
    )
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, (
        "a re-dispatch must not re-set the flag — the marker persists the "
        "migration so the handler never re-runs"
    )


def test_rebuild_clears_pending_flag_without_separate_backfill(isolated, monkeypatch):
    """A ``cache-sync --rebuild`` re-walks every file from offset 0, so its
    normal per-file second seek-and-walk repopulates conversation_messages
    fully. The deferred-backfill flag (issue #139) must therefore be cleared on
    the rebuild path WITHOUT a redundant offset-0 backfill_conversation_messages
    pass — the rebuild already covers it."""
    import _cctally_cache as cache
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "m1", "r1", "hello") + _user_line("u1", "q1")
    )

    # Stand up the deferred-upgrade state: a pending flag set, index empty. Also
    # arm the #177 enrichment reingest flag — the rebuild's normal offset-0 walk
    # re-derives the enriched fields, so it must be cleared on the rebuild path
    # too (else cache-sync --rebuild re-arms it every run).
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES('conversation_backfill_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES('conversation_reingest_enrichment_pending','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()

    calls = {"n": 0}
    real_backfill = cache.backfill_conversation_messages

    def _spy(c):
        calls["n"] += 1
        return real_backfill(c)

    monkeypatch.setattr(cache, "backfill_conversation_messages", _spy)

    sync(rebuild=True)

    # The normal rebuild walk populated the index; the dedicated backfill walker
    # was never invoked (the flag-consume branch is skipped under rebuild).
    assert calls["n"] == 0, (
        "rebuild must NOT run the separate backfill — its normal offset-0 walk "
        "already repopulates conversation_messages"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_backfill_pending'"
    ).fetchone() is None, "rebuild must clear the pending flag directly"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_enrichment_pending'"
    ).fetchone() is None, "rebuild must also clear the #177 enrichment reingest flag"


# ──────────────────────────────────────────────────────────────────────────
# #138 item 2: integrity-safe, storm-free FTS full-clear
#   (clear_conversation_messages: drop triggers → truncate base → 'delete-all'
#    on the FTS index → recreate triggers; NOT the per-row delete-trigger storm)
# ──────────────────────────────────────────────────────────────────────────


def _insert_msg(conn, uuid, off, text, *, entry_type="assistant"):
    conn.execute(
        "INSERT INTO conversation_messages"
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,text,blocks_json,is_sidechain)"
        " VALUES('s',?,?,?, '2026-01-01T00:00:00Z',?,?,'[]',0)",
        (uuid, f"f{off}", off, entry_type, text),
    )


def test_clear_conversation_messages_empties_both_and_keeps_integrity():
    """The full-clear empties BOTH conversation_messages and conversation_fts,
    leaves the triggers in place (a later insert is indexed again), and the FTS
    index passes integrity-check — the load-bearing acceptance criterion. The
    naive 'clear the index then let the per-row delete trigger fire' ordering
    corrupts the external-content index ('database disk image is malformed')."""
    conn = _fresh()
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    for i in range(6):
        _insert_msg(conn, f"u{i}", i, "alpha findme prose")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 6

    db.clear_conversation_messages(conn)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    # Triggers restored: a fresh insert is indexed; old prose stays gone.
    _insert_msg(conn, "fresh", 99, "beta brandnew")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    # Integrity-check must not raise (would raise on a corrupted index).
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_clear_conversation_messages_fts_unavailable_plain_delete(monkeypatch):
    """On an FTS5-unavailable build _apply_cache_schema sets the flag + creates
    no triggers / no vtable. The full-clear must still empty the base table
    without issuing a 'delete-all' against the absent conversation_fts."""
    monkeypatch.setattr(db, "_fts5_available", lambda conn: False)
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    for i in range(3):
        _insert_msg(conn, f"u{i}", i, "x")
    conn.commit()
    db.clear_conversation_messages(conn)  # must not raise
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0


def test_sync_rebuild_keeps_fts_integrity(isolated):
    """A `cache-sync --rebuild` full-clears via the storm-free path and the FTS
    index remains integrity-valid + correctly repopulated."""
    ns, conn, projects, sync = isolated
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    f = projects / "a.jsonl"
    f.write_text(_asst_line("a1", "m1", "r1", "alpha old prose") + _user_line("u1", "alpha more"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] >= 1

    f.write_text(_asst_line("a9", "m9", "r9", "beta fresh prose"))
    sync(rebuild=True)

    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'beta'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_sync_truncation_keeps_fts_integrity(isolated):
    """The truncation-escalation full-clear uses the same storm-free path; the
    FTS index stays integrity-valid after a real reset + re-ingest."""
    ns, conn, projects, sync = isolated
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    fa = projects / "a.jsonl"
    fb = projects / "b.jsonl"
    fa.write_text(_asst_line("a1", "m1", "r1", "alpha gone") + _user_line("u1", "alpha gone2"))
    fb.write_text(_asst_line("b1", "mb", "rb", "bravo kept"))
    sync()
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] >= 1

    fa.write_text(_asst_line("a2", "m2", "r2", "gamma rewritten"))  # shrink → escalation
    stats = sync()
    assert stats.files_reset_truncated >= 1

    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'alpha'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'gamma'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_fts WHERE conversation_fts MATCH 'bravo'"
    ).fetchone()[0] == 1
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


# ──────────────────────────────────────────────────────────────────────────
# Migration 005: isMeta reingest reclassifies stale 'human' rows to 'meta'
# ──────────────────────────────────────────────────────────────────────────

def _meta_skill_line(uuid, *, path="/x/skills/brainstorming", ts="2026-06-01T00:02:00Z"):
    body = f"Base directory for this skill: {path}\n\n# Skill body"
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "isMeta": True, "sourceToolUseID": "toolu_x",
        "message": {"role": "user", "content": [{"type": "text", "text": body}]},
    }) + "\n"


def test_migration_005_reingest_reclassifies_ismeta_rows_to_meta(isolated):
    """005 is flag-only (sets conversation_reingest_pending); the offset-0
    re-ingest then re-parses every JSONL through the meta-aware parser, so a
    stale 'human' skill-body row (a pre-upgrade ingest) is reclassified to
    entry_type='meta' with text='' and the flag is cleared (Codex P2.1)."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "m1", "r1", "hello") + _meta_skill_line("u1")
    )
    sync()
    # Simulate a PRE-upgrade ingest: the skill body stored as a 'human' prose row
    # (entry_type='human', body in the indexed text column).
    body = "Base directory for this skill: /x/skills/brainstorming\n\n# Skill body"
    conn.execute(
        "UPDATE conversation_messages SET entry_type='human', text=? WHERE uuid='u1'",
        (body,))
    conn.commit()
    assert conn.execute(
        "SELECT entry_type FROM conversation_messages WHERE uuid='u1'"
    ).fetchone()[0] == "human"

    # Run the real 005 handler (flag-only) then sync to consume it.
    db._005_conversation_reingest_meta(conn)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is not None
    sync()

    row = conn.execute(
        "SELECT entry_type, text, blocks_json FROM conversation_messages WHERE uuid='u1'"
    ).fetchone()
    assert row is not None, "the skill row survives the reingest"
    assert row[0] == "meta"             # reclassified by the meta-aware parser
    assert row[1] == ""                 # not FTS-indexed / not a title candidate
    assert "Skill body" in row[2]       # blocks still carry the body for rendering
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_pending'"
    ).fetchone() is None, "the reingest flag is dropped after consumption"


# ──────────────────────────────────────────────────────────────────────────
# #177 Session 1: the enriched data contract lands through the real sync_cache
# write path — the new columns are populated and the aux FTS indexes the tool
# content. Plus migration 007's flag-only reingest cycle.
# ──────────────────────────────────────────────────────────────────────────

def _asst_tooluse_line(uuid, msg_id, req_id, *, cmd="enrichcmd",
                       ts="2026-06-01T00:00:00Z"):
    """An assistant JSONL line carrying a Bash tool_use (so search_tool is
    non-empty) and a message-level stop_reason."""
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": "s1",
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": "claude-opus-4-7",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": "Bash", "id": "tu1",
                         "input": {"command": cmd}}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def test_sync_lands_enriched_columns_and_split_fts(isolated):
    """A real sync_cache ingests the enriched fields: stop_reason on the row,
    search_tool populated from the tool input (search_aux stays '' — documented-
    dead, #177 S6), blocks_json carrying structured input/input_truncated, and
    the split FTS indexing the tool content via the search_tool column."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync(rebuild=True)

    row = conn.execute(
        "SELECT stop_reason, search_tool, search_aux, blocks_json "
        "FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()
    assert row is not None
    assert row[0] == "tool_use"                 # stop_reason column populated
    assert "enrichcmd" in row[1]                # search_tool carries the tool input
    assert row[2] == ""                         # search_aux documented-dead
    blocks = json.loads(row[3])
    tu = [b for b in blocks if b["kind"] == "tool_use"][0]
    assert tu["input"] == {"command": "enrichcmd"}
    assert tu["input_truncated"] is False
    if db._fts5_available(conn):
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_fts "
            "WHERE conversation_fts MATCH '{search_tool}: enrichcmd'"
        ).fetchone()[0] == 1


def test_migration_007_reingest_lands_enrichment_on_stale_row(isolated):
    """007 is flag-only (sets conversation_reingest_enrichment_pending); the
    offset-0 reingest then re-parses every JSONL through the enriched parser, so
    a stale pre-upgrade row (enriched columns NULL/'') gets them backfilled, and
    the flag is cleared after consumption."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync()
    # Simulate a PRE-upgrade ingest: blank out the enriched columns.
    conn.execute(
        "UPDATE conversation_messages SET stop_reason=NULL, search_tool='' WHERE uuid='a1'")
    conn.commit()
    assert conn.execute(
        "SELECT stop_reason, search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone() == (None, "")

    # Run the real 007 handler (flag-only) then sync to consume it.
    db._007_conversation_reingest_enrichment(conn)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_enrichment_pending'"
    ).fetchone() is not None
    sync()

    row = conn.execute(
        "SELECT stop_reason, search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()
    assert row[0] == "tool_use", "stop_reason re-derived by the enriched parser"
    assert "enrichcmd" in row[1], "search_tool re-derived from the tool input"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_reingest_enrichment_pending'"
    ).fetchone() is None, "the enrichment reingest flag is dropped after consumption"


# ──────────────────────────────────────────────────────────────────────────
# #177 Session 4: migration 009 flag-only media reingest. The flag must be
# wired into EVERY reingest flag site (Codex F2 — a missed site either never
# triggers the reingest or re-arms it forever), and the resumable reingest must
# land the media placeholders + web captures on a stale pre-upgrade row.
# ──────────────────────────────────────────────────────────────────────────

def test_009_flag_in_reingest_flag_keys_and_sql_sites():
    import inspect, _cctally_cache as cc
    assert "conversation_media_reingest_pending" in cc._REINGEST_FLAG_KEYS
    # The two SELECT/DELETE literal lists + the tuple + completion cleanup: the
    # flag string must appear at least 4 times in the module source (Codex F2 —
    # a missed site either never triggers the reingest or re-arms it forever).
    src = inspect.getsource(cc)
    assert src.count("conversation_media_reingest_pending") >= 4


def _user_tool_result_image_line(uuid, tool_use_id, *, ts="2026-06-01T00:02:00Z"):
    """A user JSONL line carrying a tool_result whose content array holds an
    image item (the MCP-screenshot shape) plus a WebSearch toolUseResult so the
    reingest lands both media[] and web_search."""
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "toolUseResult": {"query": "q1", "results": [
            {"content": [{"title": "T1", "url": "https://e.example/x"}]}]},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": "image/png",
                                             "data": "AAAA"}},
                {"type": "text", "text": "screenshot"}]}]},
    }) + "\n"


def test_resumable_reingest_consumes_media_flag(isolated):
    """009 is flag-only (sets conversation_media_reingest_pending); the resumable
    reingest re-parses every JSONL through the current parser, so a stale
    pre-upgrade row (no media[] / web_search) gains them, and the flag + cursor +
    gen keys are cleared after consumption."""
    ns, conn, projects, sync = isolated
    (projects / "a.jsonl").write_text(_user_tool_result_image_line("u1", "tw1"))
    sync()
    # Simulate a PRE-S4 ingest: strip the media + web_search keys off blocks_json.
    blocks = json.loads(conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='u1'").fetchone()[0])
    for b in blocks:
        b.pop("media", None)
        b.pop("web_search", None)
    conn.execute("UPDATE conversation_messages SET blocks_json=? WHERE uuid='u1'",
                 (json.dumps(blocks, separators=(",", ":")),))
    conn.commit()
    stale = json.loads(conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='u1'").fetchone()[0])
    assert all("media" not in b for b in stale)

    # Run the real 009 handler (flag-only) then sync to consume it.
    db._009_conversation_media_reingest(conn)
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_media_reingest_pending'"
    ).fetchone() is not None
    sync()

    re_blocks = json.loads(conn.execute(
        "SELECT blocks_json FROM conversation_messages WHERE uuid='u1'").fetchone()[0])
    tr = [b for b in re_blocks if b["kind"] == "tool_result"][0]
    assert tr["media"] == [{"kind": "image", "media_type": "image/png",
                            "bytes": 4, "index": 0}], "media[] re-derived by the parser"
    assert tr["web_search"]["query"] == "q1", "web_search capture re-derived"
    for key in ("conversation_media_reingest_pending",
                "conversation_reingest_cursor", "conversation_reingest_cursor_gen"):
        assert conn.execute(
            "SELECT 1 FROM cache_meta WHERE key=?", (key,)
        ).fetchone() is None, f"{key} dropped after consumption"


# ──────────────────────────────────────────────────────────────────────────
# #177 S6: migration 010 search-column split consumed by the REAL sync_cache
# (flag-only handler -> flock-held backfill + legacy->split FTS swap).
# ──────────────────────────────────────────────────────────────────────────

def _to_legacy_shape(conn):
    """Tear the fresh split shape down to the legacy prose+aux two-table shape
    so a real sync can exercise the swap."""
    db._drop_conversation_fts_triggers(conn)
    conn.execute("DROP TABLE IF EXISTS conversation_fts")
    conn.execute("DROP TABLE IF EXISTS conversation_fts_aux")
    conn.execute("CREATE VIRTUAL TABLE conversation_fts "
                 "USING fts5(text, content='conversation_messages', content_rowid='id')")
    db._create_conversation_fts_aux_table(conn)
    db._create_conversation_fts_legacy_triggers(conn)
    conn.commit()


def test_sync_consumes_search_split_flag_backfills_and_swaps(isolated):
    """End-to-end: a legacy-shape cache with the migration-010 flag set is
    swapped to the split shape by the next real sync_cache, the tool content is
    backfilled into search_tool, and the flag + cursor clear."""
    ns, conn, projects, sync = isolated
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync()   # split-shape ingest of the tool row
    # Model a pre-S6 install: revert to legacy FTS shape, blank the split
    # columns, arm the migration-010 flag.
    _to_legacy_shape(conn)
    conn.execute("UPDATE conversation_messages SET search_tool='', search_thinking=''")
    db._set_cache_meta(conn, "conversation_search_split_pending", "1")
    conn.commit()
    assert db._conversation_fts_is_split(conn) is False

    sync()   # consumes the flag under the flock

    assert db._conversation_fts_is_split(conn), "sync swapped to the split shape"
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='conversation_fts_aux'"
    ).fetchone() is None
    assert "enrichcmd" in conn.execute(
        "SELECT search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()[0]
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT rowid FROM conversation_fts "
        "WHERE conversation_fts MATCH '{search_tool}: enrichcmd'"
    ).fetchall() != []
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")


def test_rebuild_swaps_legacy_shape_and_clears_split_flag(isolated):
    """A `cache-sync --rebuild` on a legacy-shape DB with the migration-010 flag
    swaps to the split shape (the walk repopulates the columns through the new
    triggers) and clears the flag without a redundant backfill pass."""
    ns, conn, projects, sync = isolated
    if not db._fts5_available(conn):
        pytest.skip("sqlite build lacks FTS5")
    (projects / "a.jsonl").write_text(_asst_tooluse_line("a1", "m1", "r1"))
    sync()
    _to_legacy_shape(conn)
    db._set_cache_meta(conn, "conversation_search_split_pending", "1")
    conn.commit()
    assert db._conversation_fts_is_split(conn) is False

    sync(rebuild=True)

    assert db._conversation_fts_is_split(conn), "rebuild swapped to the split shape"
    assert conn.execute(
        "SELECT 1 FROM cache_meta WHERE key='conversation_search_split_pending'"
    ).fetchone() is None
    assert "enrichcmd" in conn.execute(
        "SELECT search_tool FROM conversation_messages WHERE uuid='a1'"
    ).fetchone()[0]
    assert conn.execute(
        "SELECT rowid FROM conversation_fts "
        "WHERE conversation_fts MATCH '{search_tool}: enrichcmd'"
    ).fetchall() != []
    conn.execute("INSERT INTO conversation_fts(conversation_fts) VALUES('integrity-check')")
