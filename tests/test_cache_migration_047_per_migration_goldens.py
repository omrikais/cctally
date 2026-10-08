"""Per-migration goldens for #901's spill-free read paths (cache migration 047).

``pre.sqlite`` is a genuine 046-head install with every 047 object removed and
quota rows seeded underneath: two raw partitions, a whole-second tie spelled two
ways, and one row the loader refuses (blank slot). ``post.sqlite`` is that store
after the handler: one summary row per valid raw partition at its maximum
second, no row for the refused one, and no physical row touched.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script

IDEMPOTENCY_COVERED = True
MIGRATION = "047_spill_free_read_paths"
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
OBJECTS = {
    ("table", "codex_quota_partition_latest"),
    ("index", "idx_qws_partition_capture"),
    ("trigger", "trg_qws_latest_ins"),
    ("trigger", "trg_qws_latest_del"),
    ("trigger", "trg_qws_latest_upd"),
    ("index", "idx_qws_codex_load_order"),
    ("index", "idx_codex_entries_root_path_time"),
    ("index", "idx_codex_files_alias_recent"),
    ("index", "idx_codex_threads_recent"),
    ("index", "idx_entries_model_time"),
    ("index", "idx_codex_entries_model_time"),
    ("index", "idx_codex_window_attributions_read_order"),
}


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
        (str(row[0]), str(row[1])) for row in conn.execute(
            "SELECT type, name FROM sqlite_master")
    }


def _physical(conn):
    return list(conn.execute("SELECT * FROM quota_window_snapshots ORDER BY id"))


def _summary(conn):
    return sorted(conn.execute("SELECT * FROM codex_quota_partition_latest"))


def _epoch(text: str) -> int:
    return int(dt.datetime.fromisoformat(
        text.replace("Z", "+00:00")).timestamp())


def test_pre_fixture_is_a_046_head_install_without_any_047_object(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 46
        assert not (_objects(conn) & OBJECTS)
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_carries_every_object_and_the_marker(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 47
        assert OBJECTS <= _objects(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_post_summary_holds_each_partition_maximum(db, tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        rows = _summary(conn)
        assert sorted(row[-1] for row in rows) == [
            _epoch("2026-07-31T09:00:00Z"), _epoch("2026-07-31T11:00:00Z")]
        assert rows == sorted(
            conn.execute(db._codex_quota_latest_bootstrap_select()))
    finally:
        conn.close()


def test_no_physical_row_is_touched(tmp_path):
    pre = sqlite3.connect(_copy(PRE_DB, tmp_path))
    post_path = tmp_path / "post-copy.sqlite"
    shutil.copyfile(POST_DB, post_path)
    post = sqlite3.connect(post_path)
    try:
        assert _physical(post) == _physical(pre)
    finally:
        pre.close()
        post.close()


def test_the_tie_fetch_seeks_the_partition_index(db, tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    on = " AND ".join(
        f"{expr} = s.{key}" for key, expr in zip(
            db._CODEX_QUOTA_LATEST_KEY_COLUMNS,
            db._codex_quota_latest_key_exprs("q.")))
    sql = (
        "SELECT q.id FROM codex_quota_partition_latest AS s"
        " CROSS JOIN quota_window_snapshots AS q"
        " INDEXED BY idx_qws_partition_capture"
        f" ON {on} AND unixepoch(q.captured_at_utc) = s.latest_capture_epoch"
        f" WHERE {db._codex_quota_latest_validity_sql('q.')}")
    try:
        plan = [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        winners = [row[0] for row in conn.execute(sql)]
    finally:
        conn.close()
    assert plan[0] == "SCAN s", plan
    assert plan[1].startswith(
        "SEARCH q USING INDEX idx_qws_partition_capture (source=? AND <expr>=?"
    ), plan
    assert len(winners) == 3, "two tied captures plus the weekly maximum"


def test_handler_is_idempotent(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        _handler(db)(conn)
        first = (_objects(conn), _summary(conn), _physical(conn))
        _handler(db)(conn)
        assert (_objects(conn), _summary(conn), _physical(conn)) == first
    finally:
        conn.close()


def test_handler_rebootstraps_a_summary_it_did_not_write(db, tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        conn.execute(
            "UPDATE codex_quota_partition_latest SET latest_capture_epoch = 0")
        conn.commit()
        assert _summary(conn) != sorted(
            conn.execute(db._codex_quota_latest_bootstrap_select()))
        _handler(db)(conn)
        assert _summary(conn) == sorted(
            conn.execute(db._codex_quota_latest_bootstrap_select()))
    finally:
        conn.close()


def test_handler_defers_before_any_ddl_when_the_codex_flock_is_held(
    db, tmp_path,
):
    handler = _handler(db)
    gate_exc = handler.__globals__["MigrationGateNotMet"]
    target = _copy(PRE_DB, tmp_path)
    conn = sqlite3.connect(target)
    try:
        lock_path = Path(str(target) + ".codex.lock")
        with open(lock_path, "w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with pytest.raises(gate_exc, match="047"):
                handler(conn)
        assert not (_objects(conn) & OBJECTS), (
            "a deferral must happen before any object is created")
        handler(conn)
        assert OBJECTS <= _objects(conn)
    finally:
        conn.close()


def test_handler_degrades_on_a_cache_without_the_anchor_column(db, tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.sqlite")
    try:
        conn.execute(
            "CREATE TABLE quota_window_snapshots ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
            " source_root_key TEXT, source_path TEXT NOT NULL,"
            " line_offset INTEGER NOT NULL, captured_at_utc TEXT NOT NULL,"
            " observed_slot TEXT, logical_limit_key TEXT NOT NULL,"
            " limit_id TEXT, limit_name TEXT, window_minutes INTEGER NOT NULL,"
            " used_percent REAL NOT NULL, resets_at_utc TEXT NOT NULL)")
        conn.commit()
        _handler(db)(conn)
        assert not (_objects(conn) & OBJECTS)
    finally:
        conn.close()
