"""Per-migration goldens for #971's delete-trigger plans (cache 052).

``pre.sqlite`` is a 051-head store carrying the shipped (head-51) bodies of
the nine Codex accounting-ledger triggers and the three partition-latest
triggers, without ``idx_codex_files_root_thread``, and one accounting row, one
thread with an alias file and one quota partition of three rows;
``post.sqlite`` is the handler's result. Detects: an upgrade that leaves a
head-51 store on the history-proportional bodies, an upgrade that re-derives
the partition-latest summary or otherwise scans history while opening, and an
upgrade that changes a ledger, summary or data row.
"""
from __future__ import annotations

import inspect
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


IDEMPOTENCY_COVERED = True
MIGRATION = "052_codex_delete_trigger_plans"
INDEX = "idx_codex_files_root_thread"
TRIGGERS = (
    "trg_codex_accounting_del", "trg_codex_accounting_file_del",
    "trg_codex_accounting_file_ins", "trg_codex_accounting_file_upd",
    "trg_codex_accounting_ins", "trg_codex_accounting_thread_del",
    "trg_codex_accounting_thread_ins", "trg_codex_accounting_thread_upd",
    "trg_codex_accounting_upd", "trg_qws_latest_del", "trg_qws_latest_ins",
    "trg_qws_latest_upd",
)
THREAD_TRIGGERS = (
    "trg_codex_accounting_thread_del", "trg_codex_accounting_thread_ins",
    "trg_codex_accounting_thread_upd",
)
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
ROOT = "rk-fixture"


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
    names = (INDEX, *TRIGGERS)
    return {
        name: sql for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            f"WHERE name IN ({','.join('?' * len(names))})", names)
    }


def _fresh_objects(db, tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")
    try:
        db._apply_cache_schema(conn)
        conn.commit()
        return _objects(conn)
    finally:
        conn.close()


def _rows(conn):
    """Every ordinary table's rows, as sorted multisets."""
    tables = [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name")]
    return {
        table: sorted(conn.execute(f'SELECT * FROM "{table}"'), key=repr)
        for table in tables
    }


def test_pre_fixture_is_a_051_head_with_the_shipped_bodies(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 51
        objects = _objects(conn)
        assert INDEX not in objects
        assert set(objects) == set(TRIGGERS)
        for name in THREAD_TRIGGERS:
            assert "LEFT JOIN codex_session_files" in objects[name], name
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 3
    finally:
        conn.close()


def test_post_fixture_carries_the_fresh_schema_bodies(db, tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 52
        assert _objects(conn) == _fresh_objects(db, tmp_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_installs_the_fresh_schema_bodies(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        _handler(db)(conn)
        objects = _objects(conn)
        assert set(objects) == {INDEX, *TRIGGERS}
        assert objects == _fresh_objects(db, tmp_path)
    finally:
        conn.close()


def test_handler_changes_no_rows(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        before = _rows(conn)
        assert before["codex_accounting_change_log"], "non-vacuity: a ledger row"
        assert before["codex_quota_partition_latest"], "non-vacuity: a summary"
        assert before["codex_conversation_threads"], "non-vacuity: a thread"
        _handler(db)(conn)
        assert _rows(conn) == before
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


# ── bounded upgrade work (spec T5) ───────────────────────────────────────────

FIXED_FILES = 5


def _head_51_store(db, path: Path, *, history: int) -> None:
    """A head-51 store like ``pre.sqlite``, with ``history`` quota rows in one
    partition and ``history`` entries, over a fixed file count."""
    pre = sqlite3.connect(f"file:{PRE_DB}?mode=ro", uri=True)
    try:
        shipped = dict(pre.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            f"AND name IN ({','.join('?' * len(TRIGGERS))})", TRIGGERS))
        markers = list(pre.execute(
            "SELECT name, applied_at_utc FROM schema_migrations"))
    finally:
        pre.close()
    conn = sqlite3.connect(path)
    try:
        db._apply_cache_schema(conn)
        # A store that ``open_cache_db`` created carries this column; without
        # it the open takes its one-time legacy Codex purge, a history-sized
        # DELETE that is not part of the 052 upgrade.
        conn.execute(
            "ALTER TABLE codex_session_files ADD COLUMN last_total_tokens INTEGER")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
        conn.executemany(
            "INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", markers)
        conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
        for name in TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(shipped[name])
        conn.executemany(
            "INSERT INTO codex_session_files (path, size_bytes, mtime_ns, "
            " last_byte_offset, last_ingested_at, last_native_thread_id, "
            " source_root_key) VALUES (?, 1, 1, 1, '2026-11-05T00:00:00Z', ?, ?)",
            [(f"/h/{i}.jsonl", f"native-{i}", ROOT) for i in range(FIXED_FILES)])
        conn.executemany(
            "INSERT INTO codex_session_entries (source_path, line_offset, "
            " timestamp_utc, session_id, model, source_root_key, "
            " conversation_key) VALUES (?, ?, '2026-11-05T00:00:00Z', 's', "
            " 'gpt-5', ?, ?)",
            [(f"/h/{i % FIXED_FILES}.jsonl", i, ROOT, f"conv-{i}")
             for i in range(history)])
        conn.executemany(
            "INSERT INTO quota_window_snapshots (source, source_root_key, "
            " source_path, line_offset, captured_at_utc, observed_slot, "
            " logical_limit_key, limit_id, window_minutes, used_percent, "
            " resets_at_utc) VALUES ('codex', ?, ?, ?, ?, 'primary', "
            " 'limit-primary', 'codex', 300, 10.0, '2026-11-06T00:00:00Z')",
            [(ROOT, f"/h/{i % FIXED_FILES}.jsonl", i,
              f"2026-11-05T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z")
             for i in range(history)])
        conn.execute("PRAGMA user_version=51")
        conn.commit()
    finally:
        conn.close()


def _counting(counter):
    def tick():
        counter[0] += 1
        return 0
    return tick


def _upgrade_steps(db, ns, monkeypatch, tmp_path, history):
    """VM steps of a complete ``open_cache_db`` upgrade of a head-51 store,
    then of a separate 052 handler replay on the upgraded store."""
    import _cctally_cache
    import _cctally_core

    redirect_paths(ns, monkeypatch, tmp_path / f"data-{history}")
    _cctally_core.CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _head_51_store(db, _cctally_core.CACHE_DB_PATH, history=history)
    open_counter = [0]
    real_open = _cctally_cache._cache_open_guarded

    def counted_open(*args, **kwargs):
        conn = real_open(*args, **kwargs)
        conn.set_progress_handler(_counting(open_counter), 1)
        return conn

    monkeypatch.setattr(_cctally_cache, "_cache_open_guarded", counted_open)
    conn = ns["open_cache_db"]()
    try:
        conn.set_progress_handler(None, 0)
        monkeypatch.setattr(_cctally_cache, "_cache_open_guarded", real_open)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(
            db._CACHE_MIGRATIONS)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == (
            history)
        replay_counter = [0]
        conn.set_progress_handler(_counting(replay_counter), 1)
        _handler(db)(conn)
        conn.set_progress_handler(None, 0)
    finally:
        conn.close()
    return open_counter[0], replay_counter[0]


@pytest.mark.parametrize("mutant", ["none", "schema-apply", "handler"])
def test_upgrade_work_is_bounded_by_history(monkeypatch, tmp_path, mutant):
    """Detects an upgrade that re-derives the partition-latest summary (or
    otherwise scans history) on a head-51 store: the complete open, which runs
    ``_apply_cache_schema`` before the dispatcher and 052, and a 052 replay
    each take VM steps independent of 20 versus 2,000 quota rows and entries.
    The two mutants force a re-bootstrap into exactly one call site and prove
    the bounds catch it there."""
    ns = load_script()
    # Bound AFTER the load: load_script re-executes the siblings, so a module
    # object taken earlier would carry the mutant where the opener never looks.
    import _cctally_db as db
    caller = {"schema-apply": "_apply_cache_schema",
              "handler": "_052_codex_delete_trigger_plans"}.get(mutant)
    if caller is not None:
        real_summary = db._apply_codex_quota_latest_summary

        def mutated(conn, *, rebootstrap=False):
            frame = inspect.currentframe().f_back
            if frame is not None and frame.f_code.co_name == caller:
                rebootstrap = True
            return real_summary(conn, rebootstrap=rebootstrap)

        monkeypatch.setattr(db, "_apply_codex_quota_latest_summary", mutated)
    small_open, small_replay = _upgrade_steps(db, ns, monkeypatch, tmp_path, 20)
    big_open, big_replay = _upgrade_steps(db, ns, monkeypatch, tmp_path, 2000)
    counts = (small_open, big_open, small_replay, big_replay)
    open_bounded = big_open <= small_open + 256
    replay_bounded = big_replay <= small_replay + 256
    expected = {
        "none": (True, True),
        "schema-apply": (False, True),
        "handler": (False, False),
    }[mutant]
    assert (open_bounded, replay_bounded) == expected, (mutant, counts)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
