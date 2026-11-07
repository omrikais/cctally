"""Per-migration goldens for #1033's reset-anchor evidence summary (cache 053).

``pre.sqlite`` is a 052-head store without the summary table, its stale index,
its three triggers or its two ``cache_meta`` keys, holding retained Codex
quota rows (one with a NULL slot); ``post.sqlite`` is the handler's result.
Detects: an upgrade that marks a populated, never-maintained summary ``ready``
(the seed would then fold an empty summary and mint fresh anchors), an upgrade
that scans history while opening, a retry that discards a recorded state, an
apply that keeps a summary whose maintenance was interrupted, and an older
binary's writes that silently drift a ``ready`` summary.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


IDEMPOTENCY_COVERED = True
MIGRATION = "053_codex_reset_anchor_evidence"
STATE_KEY = "codex_reset_anchor_evidence"
GENERATION_KEY = "codex_reset_anchor_evidence_generation"
TABLE = "codex_reset_anchor_evidence"
OBJECTS = (
    TABLE, "idx_codex_reset_anchor_evidence_stale",
    "trg_qws_anchor_evidence_ins", "trg_qws_anchor_evidence_del",
    "trg_qws_anchor_evidence_upd",
)
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
ROOT = "rk-fixture"
QUOTA_COLUMNS = (
    "source", "source_root_key", "source_path", "line_offset",
    "captured_at_utc", "observed_slot", "logical_limit_key", "limit_id",
    "window_minutes", "used_percent", "resets_at_utc")


@pytest.fixture
def db():
    load_script()
    import _cctally_db
    return _cctally_db


def _handler(db):
    return next(item.handler for item in db._CACHE_MIGRATIONS
                if item.name == MIGRATION)


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def _objects(conn):
    return {
        name: sql for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            f"WHERE name IN ({','.join('?' * len(OBJECTS))})", OBJECTS)
    }


def _fresh_objects(db, tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")
    try:
        db._apply_cache_schema(conn)
        conn.commit()
        return _objects(conn)
    finally:
        conn.close()


def _meta(conn, key):
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def _summary(conn):
    return sorted(conn.execute(f"SELECT * FROM {TABLE}"))


def _oracle(conn):
    """Count and lexicographic minimum per in-scope bucket, in Python."""
    out = {}
    for row in conn.execute(
            "SELECT source_root_key, observed_slot, logical_limit_key,"
            " resets_at_utc, source_path, line_offset, id"
            " FROM quota_window_snapshots"
            " WHERE source='codex' AND observed_slot IS NOT NULL"):
        bucket, order = tuple(row[:4]), tuple(row[4:])
        count, minimum = out.get(bucket, (0, order))
        out[bucket] = (count + 1, min(minimum, order))
    return sorted((*bucket, count, *minimum, 0)
                  for bucket, (count, minimum) in out.items())


def _rows(conn):
    """Every ordinary table's rows, as sorted multisets."""
    tables = [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name")]
    return {
        table: sorted(conn.execute(f'SELECT * FROM "{table}"'), key=repr)
        for table in tables
    }


def _quota_statements(conn, action):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        action()
    finally:
        conn.set_trace_callback(None)
    return [" ".join(s.split()) for s in statements
            if "quota_window_snapshots" in s
            and "pragma" not in s.lower()
            and not s.lstrip().upper().startswith(("CREATE", "DROP"))]


#: The one O(1) population probe the state decision may issue.
_POPULATION_PROBE = (
    "SELECT 1 FROM quota_window_snapshots WHERE source = 'codex' "
    "AND observed_slot IS NOT NULL LIMIT 1")


def test_pre_fixture_is_a_052_head_without_the_summary(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 52
        assert _objects(conn) == {}
        assert _meta(conn, STATE_KEY) is None
        assert _meta(conn, GENERATION_KEY) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'"
            " AND observed_slot IS NOT NULL").fetchone()[0] >= 2
    finally:
        conn.close()


def test_post_fixture_is_bootstrap_with_the_fresh_schema(db, tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 53
        assert _objects(conn) == _fresh_objects(db, tmp_path)
        assert _meta(conn, STATE_KEY) == "bootstrap"
        assert int(_meta(conn, GENERATION_KEY)) >= 1
        assert _summary(conn) == []
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_on_a_populated_store_records_bootstrap_without_a_scan(
        db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        before = _rows(conn)
        statements = _quota_statements(conn, lambda: _handler(db)(conn))
        assert set(statements) <= {_POPULATION_PROBE}, statements
        assert _objects(conn) == _fresh_objects(db, tmp_path)
        assert _meta(conn, STATE_KEY) == "bootstrap"
        after = _rows(conn)
        assert after.pop(TABLE) == []
        for table in before:
            if table != "cache_meta":
                assert after[table] == before[table], table
        assert {key for key, _v in after["cache_meta"]} - {
            key for key, _v in before["cache_meta"]} == {
            STATE_KEY, GENERATION_KEY}
    finally:
        conn.close()


def test_handler_replay_is_a_noop(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        handler = _handler(db)
        handler(conn)
        first = (_objects(conn), _rows(conn))
        handler(conn)
        assert (_objects(conn), _rows(conn)) == first
    finally:
        conn.close()


def _open(ns, monkeypatch, tmp_path):
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns["open_cache_db"]()


def test_a_fresh_install_starts_ready(monkeypatch, tmp_path):
    ns = load_script()
    conn = _open(ns, monkeypatch, tmp_path)
    try:
        assert _meta(conn, STATE_KEY) == "ready"
        assert int(_meta(conn, GENERATION_KEY)) >= 1
    finally:
        conn.close()


def _insert_raw(conn, rows):
    """A head-52 writer: plain SQL through whatever triggers the store has."""
    conn.executemany(
        f"INSERT OR IGNORE INTO quota_window_snapshots ({', '.join(QUOTA_COLUMNS)})"
        f" VALUES ({', '.join('?' for _ in QUOTA_COLUMNS)})", rows)


def _quota_row(path, offset, *, reset="2026-11-07T00:00:00Z", slot="primary"):
    return ("codex", ROOT, path, offset, "2026-11-06T10:00:00Z", slot,
            "codex:primary", "codex", 10080, 10.0, reset)


def test_a_markerless_retry_keeps_the_recorded_state(db, tmp_path):
    """A head-52 trim then re-upgrade, or a retried 053, must not reset a
    maintained summary (a ready one would lose readiness for no reason; a
    bootstrap one would wrongly turn ready)."""
    for state in ("ready", "bootstrap"):
        conn = sqlite3.connect(tmp_path / f"retry-{state}.db")
        try:
            db._apply_cache_schema(conn)
            conn.commit()
            _insert_raw(conn, [_quota_row("/r/a.jsonl", 1),
                               _quota_row("/r/b.jsonl", 2)])
            conn.execute("UPDATE cache_meta SET value=? WHERE key=?",
                         (state, STATE_KEY))
            conn.commit()
            before = (_meta(conn, STATE_KEY), _meta(conn, GENERATION_KEY),
                      _summary(conn))
            assert before[2], "non-vacuity: the summary holds rows"
            _handler(db)(conn)
            db._apply_cache_schema(conn)
            conn.commit()
            assert (_meta(conn, STATE_KEY), _meta(conn, GENERATION_KEY),
                    _summary(conn)) == before
        finally:
            conn.close()


@pytest.mark.parametrize("damage", [
    "drop-table", "drop-stale-index", "drop-ins-trigger", "drop-del-trigger",
    "drop-upd-trigger", "malformed-state", "missing-state"])
def test_missing_objects_or_a_malformed_state_invalidate_the_summary(
        db, tmp_path, damage):
    """A summary whose maintenance may have been interrupted must never stay
    ``ready``: the apply empties it, advances the generation and records
    ``bootstrap`` for a populated store."""
    conn = sqlite3.connect(tmp_path / f"{damage}.db")
    try:
        db._apply_cache_schema(conn)
        conn.commit()
        _insert_raw(conn, [_quota_row("/r/a.jsonl", 1)])
        conn.commit()
        generation = int(_meta(conn, GENERATION_KEY))
        assert _meta(conn, STATE_KEY) == "ready" and _summary(conn)
        statement = {
            "drop-table": f"DROP TABLE {TABLE}",
            "drop-stale-index":
                "DROP INDEX idx_codex_reset_anchor_evidence_stale",
            "drop-ins-trigger": "DROP TRIGGER trg_qws_anchor_evidence_ins",
            "drop-del-trigger": "DROP TRIGGER trg_qws_anchor_evidence_del",
            "drop-upd-trigger": "DROP TRIGGER trg_qws_anchor_evidence_upd",
            "malformed-state":
                f"UPDATE cache_meta SET value='readyish' WHERE key='{STATE_KEY}'",
            "missing-state": f"DELETE FROM cache_meta WHERE key='{STATE_KEY}'",
        }[damage]
        conn.execute(statement)
        conn.commit()
        db._apply_codex_reset_anchor_evidence(conn)
        assert set(_objects(conn)) == set(OBJECTS)
        assert _meta(conn, STATE_KEY) == "bootstrap"
        assert int(_meta(conn, GENERATION_KEY)) > generation
        assert _summary(conn) == []
    finally:
        conn.close()


def test_a_damaged_empty_store_is_invalidated_back_to_ready(db, tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    try:
        db._apply_cache_schema(conn)
        conn.execute("DROP TRIGGER trg_qws_anchor_evidence_del")
        conn.execute(
            f"UPDATE cache_meta SET value='bootstrap' WHERE key='{STATE_KEY}'")
        conn.commit()
        db._apply_codex_reset_anchor_evidence(conn)
        assert _meta(conn, STATE_KEY) == "ready"
    finally:
        conn.close()


def test_the_conversations_schema_carries_none_of_the_objects(db):
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        assert _objects(conn) == {}
    finally:
        conn.close()


# ── downgrade: an older (head-52) binary writing a head-53 store ────────────


def test_head_52_writes_keep_a_ready_summary_exact(monkeypatch, tmp_path):
    ns = load_script()
    conn = _open(ns, monkeypatch, tmp_path)
    conn.close()
    import _cctally_core
    raw = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        _insert_raw(raw, [
            _quota_row("/r/b.jsonl", 1), _quota_row("/r/a.jsonl", 2),
            _quota_row("/r/c.jsonl", 3, reset="2026-11-07T00:00:00+00:00"),
            _quota_row("/r/c.jsonl", 4, slot=None)])
        raw.execute("DELETE FROM quota_window_snapshots WHERE source_path=?",
                    ("/r/b.jsonl",))
        raw.commit()
        assert _meta(raw, STATE_KEY) == "ready"
        assert _summary(raw) == _oracle(raw) and _summary(raw)
    finally:
        raw.close()


@pytest.mark.parametrize("state", ["ready", "bootstrap"])
def test_a_head_52_per_row_clear_empties_or_leaves_inert(
        monkeypatch, tmp_path, state):
    """A head-52 ``_clear_codex_derived_rows`` deletes the Codex quota rows
    row by row: a ``ready`` summary must end exactly empty, and a
    ``bootstrap`` one must stay untouched and not ready."""
    ns = load_script()
    conn = _open(ns, monkeypatch, tmp_path)
    conn.close()
    import _cctally_core
    raw = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        _insert_raw(raw, [_quota_row("/r/a.jsonl", 1),
                          _quota_row("/r/b.jsonl", 2)])
        raw.execute("UPDATE cache_meta SET value=? WHERE key=?",
                    (state, STATE_KEY))
        raw.commit()
        before = _summary(raw)
        assert before
        raw.execute("DELETE FROM quota_window_snapshots WHERE source='codex'")
        raw.commit()
        assert _meta(raw, STATE_KEY) == state
        assert _summary(raw) == ([] if state == "ready" else before)
    finally:
        raw.close()


def test_a_trimmed_marker_then_reupgrade_changes_nothing(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_db as db
    conn = _open(ns, monkeypatch, tmp_path)
    try:
        _insert_raw(conn, [_quota_row("/r/a.jsonl", 1),
                           _quota_row("/r/b.jsonl", 2)])
        conn.commit()
        before = (_meta(conn, STATE_KEY), _meta(conn, GENERATION_KEY),
                  _summary(conn), _objects(conn))
        assert before[0] == "ready" and before[2]
        # What a head-52 opener's version-ahead recovery leaves behind.
        conn.execute("DELETE FROM schema_migrations WHERE name=?", (MIGRATION,))
        conn.execute("PRAGMA user_version=52")
        conn.commit()
    finally:
        conn.close()
    conn = ns["open_cache_db"]()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            db._CACHE_MIGRATIONS)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
        assert (_meta(conn, STATE_KEY), _meta(conn, GENERATION_KEY),
                _summary(conn), _objects(conn)) == before
    finally:
        conn.close()


# ── bounded upgrade work ─────────────────────────────────────────────────────


def _head_52_store(db, path: Path, *, history: int) -> None:
    pre = sqlite3.connect(f"file:{PRE_DB}?mode=ro", uri=True)
    try:
        markers = list(pre.execute(
            "SELECT name, applied_at_utc FROM schema_migrations"))
    finally:
        pre.close()
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)
        # A store open_cache_db created carries this column; without it the
        # open takes its one-time legacy Codex purge, unrelated to 053.
        conn.execute(
            "ALTER TABLE codex_session_files ADD COLUMN last_total_tokens INTEGER")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
        conn.executemany(
            "INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", markers)
        for name in reversed(OBJECTS[2:]):
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute("DELETE FROM cache_meta WHERE key IN (?, ?)",
                     (STATE_KEY, GENERATION_KEY))
        _insert_raw(conn, [
            _quota_row(f"/h/{i % 5}.jsonl", i,
                       reset=f"2026-11-{7 + i % 3:02d}T00:00:00Z")
            for i in range(history)])
        conn.execute("PRAGMA user_version=52")
        conn.commit()
    finally:
        conn.close()


def _upgrade_steps(db, ns, monkeypatch, tmp_path, history):
    import _cctally_cache
    import _cctally_core

    redirect_paths(ns, monkeypatch, tmp_path / f"data-{history}")
    _cctally_core.CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _head_52_store(db, _cctally_core.CACHE_DB_PATH, history=history)
    counter = [0]
    real_open = _cctally_cache._cache_open_guarded

    def tick():
        counter[0] += 1
        return 0

    def counted_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.set_progress_handler(tick, 1)
        return conn

    monkeypatch.setattr(_cctally_cache, "_cache_open_guarded", counted_open)
    conn = ns["open_cache_db"]()
    try:
        conn.set_progress_handler(None, 0)
        monkeypatch.setattr(_cctally_cache, "_cache_open_guarded", real_open)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
        assert _meta(conn, STATE_KEY) == "bootstrap"
        assert _summary(conn) == []
    finally:
        conn.close()
    return counter[0]


def test_upgrade_work_is_bounded_by_history(monkeypatch, tmp_path):
    """An upgrade that aggregates the summary while opening: the complete
    open of a head-52 store takes VM steps independent of 20 versus 2,000
    retained quota rows."""
    ns = load_script()
    import _cctally_db as db
    small = _upgrade_steps(db, ns, monkeypatch, tmp_path, 20)
    big = _upgrade_steps(db, ns, monkeypatch, tmp_path, 2000)
    assert big <= small + 256, (small, big)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
