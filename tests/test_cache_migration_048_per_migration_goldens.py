"""Per-migration goldens for #901 Q11's extended physical-group index (048).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` §5.3a
(901-PA-001 a). ``idx_qws_physical_group`` keeps its name, its partial
predicate and its five equality members, and gains the loader's order columns
``captured_at_utc, resets_at_utc, source_path, line_offset``. Each
physical-group shard then reads its rows in ``ORDER BY source_root_key,
captured_at_utc, resets_at_utc, source_path, line_offset`` order straight off
the seek, where it used to sort the group's whole history through a temp
b-tree ("USE TEMP B-TREE FOR LAST 4 TERMS OF ORDER BY").

``pre.sqlite`` is a genuine 047-head install carrying the five-member index
and quota rows of two groups. ``post.sqlite`` is that store after the handler:
the same name and predicate over nine key columns, every row untouched.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from _script_loader import load_script_module  # the ONE cctally loader


IDEMPOTENCY_COVERED = True

MIGRATION = "048_codex_quota_physical_group_order"
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
INDEX = "idx_qws_physical_group"
LEGACY_COLUMNS = (
    "source_root_key", "logical_limit_key", "observed_slot", "window_minutes",
    None)
EXTENDED_COLUMNS = LEGACY_COLUMNS + (
    "captured_at_utc", "resets_at_utc", "source_path", "line_offset")
EXPRESSION = "unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))"
SHARD = (
    "SELECT source_path, line_offset FROM quota_window_snapshots"
    " INDEXED BY idx_qws_physical_group"
    " WHERE source='codex' AND source_root_key IS NOT NULL"
    " AND (source_root_key=? AND logical_limit_key=? AND observed_slot=?"
    " AND window_minutes=?"
    " AND unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))"
    "=unixepoch(?))"
    " ORDER BY source_root_key, captured_at_utc, resets_at_utc, source_path,"
    " line_offset")


@pytest.fixture(scope="module")
def cctally_module():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    return load_script_module()


def _handler(cctally_module):
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def _columns(conn):
    return tuple(
        row[2] for row in conn.execute(f"PRAGMA index_xinfo({INDEX})")
        if row[5])


def _index_sql(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (INDEX,)).fetchone()
    return None if row is None else str(row[0])


def _rows(conn):
    return list(conn.execute("SELECT * FROM quota_window_snapshots ORDER BY id"))


def _groups(conn):
    """One entry per physical group, its reset in one spelling."""
    return list(conn.execute(
        "SELECT DISTINCT source_root_key, logical_limit_key, observed_slot,"
        " window_minutes, datetime(unixepoch(COALESCE(canonical_resets_at_utc,"
        " resets_at_utc)), 'unixepoch')"
        " FROM quota_window_snapshots WHERE source='codex' ORDER BY 1, 2, 3,"
        " 4, 5"))


def _shard_plan_and_rows(conn, group):
    plan = [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + SHARD,
                                                group)]
    return plan, list(conn.execute(SHARD, group))


def test_pre_fixture_is_a_047_head_install_with_the_five_member_index(
    tmp_path,
):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 47
        assert _columns(conn) == LEGACY_COLUMNS
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 0
        assert len(_groups(conn)) == 2 and len(_rows(conn)) >= 5
        plan, _ = _shard_plan_and_rows(conn, _groups(conn)[0])
        assert any("USE TEMP B-TREE" in step for step in plan), plan
    finally:
        conn.close()


def test_post_fixture_carries_the_extended_index_and_the_marker(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 48
        assert _columns(conn) == EXTENDED_COLUMNS
        sql = _index_sql(conn)
        assert EXPRESSION in sql, (
            "the indexed expression must match the reader's verbatim")
        assert sql.rstrip().endswith("WHERE source='codex'"), sql
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_each_shard_streams_in_the_loaders_order(tmp_path):
    pre = sqlite3.connect(_copy(PRE_DB, tmp_path))
    post_path = tmp_path / "post-copy.sqlite"
    shutil.copyfile(POST_DB, post_path)
    post = sqlite3.connect(post_path)
    try:
        for group in _groups(post):
            plan, rows = _shard_plan_and_rows(post, group)
            assert plan[0].startswith(
                f"SEARCH quota_window_snapshots USING INDEX {INDEX} "), plan
            assert not any("TEMP B-TREE" in step for step in plan), plan
            assert rows == _shard_plan_and_rows(pre, group)[1], (
                "the same rows in the same order as the sorted legacy shard")
            assert len(rows) >= 2
    finally:
        pre.close()
        post.close()


def test_no_row_is_touched(tmp_path):
    pre = sqlite3.connect(_copy(PRE_DB, tmp_path))
    post_path = tmp_path / "post-copy.sqlite"
    shutil.copyfile(POST_DB, post_path)
    post = sqlite3.connect(post_path)
    try:
        assert _rows(post) == _rows(pre)
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = (_index_sql(conn), _columns(conn), _rows(conn))
        assert first[1] == EXTENDED_COLUMNS
        handler(conn)
        assert (_index_sql(conn), _columns(conn), _rows(conn)) == first
    finally:
        conn.close()


def test_handler_replaces_the_index_atomically(cctally_module, tmp_path):
    """A reader that found the index by name and then pins it with INDEXED BY
    must never see it missing: the drop and the create commit together."""
    path = _copy(PRE_DB, tmp_path)
    conn = sqlite3.connect(path)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        _handler(cctally_module)(conn)
    finally:
        conn.close()
    upper = [" ".join(s.split()).upper() for s in statements]
    drop = next(i for i, s in enumerate(upper)
                if s.startswith("DROP INDEX") and INDEX.upper() in s)
    create = next(i for i, s in enumerate(upper)
                  if s.startswith("CREATE INDEX") and INDEX.upper() in s)
    opened = max(i for i, s in enumerate(upper[:drop])
                 if s.startswith(("BEGIN", "SAVEPOINT")))
    closed = next(i for i, s in enumerate(upper) if i > create
                  and s.startswith(("COMMIT", "RELEASE")))
    assert opened < drop < create < closed, statements


def test_handler_degrades_on_a_cache_without_the_anchor_column(
    cctally_module, tmp_path,
):
    """The expression reads ``canonical_resets_at_utc``; a legacy-shape cache
    without it must not fail the migration."""
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
        _handler(cctally_module)(conn)
        assert _index_sql(conn) is None
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
