"""Per-migration goldens for the Codex quota physical-group seek index.

Public issue omrikais/cctally#5, Task 5. The incremental projector loads one
dirty physical group at a time, and its reset member is
``unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))``. A b-tree over
the two raw reset columns cannot seek an equality on that expression, so 036's
``idx_qws_window_ident`` left the group load walking every reset under its limit
key. Measured on a 211K-row / 608-group store: 19.7ms with the identity index,
0.60ms with the expression index below.

The same measurement disqualified the identity index outright — it is not
covering for the full-sweep load either (which reads nine columns it does not
carry) and changed no query plan at all, while charging ten columns of write
cost on every ingested observation — so this migration drops it.

``EXPECTED_EXPRESSION`` is asserted from ``sqlite_master`` rather than from
``PRAGMA index_info``, which reports an expression column as ``None``: the
whole point is that a specific expression is indexed, and SQLite only uses an
expression index when the query's expression matches the indexed one verbatim.
Indexing the bare ``COALESCE`` while the reader wraps it in ``unixepoch`` would
pass a shape check and buy nothing.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "040_codex_quota_physical_group_index"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "040_codex_quota_physical_group_index"
INDEX = "idx_qws_physical_group"
RETIRED_INDEX = "idx_qws_window_ident"

EXPECTED_EXPRESSION = (
    "unixepoch(COALESCE(canonical_resets_at_utc, resets_at_utc))")
EXPECTED_PREFIX = (
    "source_root_key, logical_limit_key, observed_slot, window_minutes")
EXPECTED_PARTIAL = "WHERE source='codex'"


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _index_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return None if row is None else str(row[0])


def _snapshot_rows(conn):
    return list(conn.execute(
        "SELECT source, source_root_key, source_path, line_offset, "
        "       captured_at_utc, used_percent, resets_at_utc, "
        "       canonical_resets_at_utc "
        "  FROM quota_window_snapshots ORDER BY source_path, line_offset"))


def test_pre_fixture_is_a_039_head_install_with_the_retired_index(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 39
        assert _index_sql(conn, RETIRED_INDEX) is not None
        assert _index_sql(conn, INDEX) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        # Non-vacuity: an index over an empty table proves nothing.
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 2
    finally:
        conn.close()


def test_post_fixture_indexes_the_group_expression_and_retires_the_old_index(
    cctally_module
):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 40
        sql = _index_sql(conn, INDEX)
        assert sql is not None
        assert EXPECTED_PREFIX in sql
        assert EXPECTED_EXPRESSION in sql, (
            "the indexed expression must match the reader's verbatim, or "
            "SQLite will not use it")
        assert EXPECTED_PARTIAL in sql
        assert _index_sql(conn, RETIRED_INDEX) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_index_is_actually_used_by_the_group_predicate(cctally_module):
    """A shape assertion is not enough — the planner has to pick it.

    This is the failure the measurement caught: an index whose columns look
    right but which the planner never uses is pure write cost.
    """
    conn = sqlite3.connect(POST_DB)
    try:
        row = conn.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, "
            "       window_minutes, "
            "       COALESCE(canonical_resets_at_utc, resets_at_utc) "
            "  FROM quota_window_snapshots WHERE source='codex' LIMIT 1"
        ).fetchone()
        assert row is not None
        plan = [
            str(step[3]) for step in conn.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT id FROM quota_window_snapshots "
                " WHERE source='codex' AND source_root_key=? "
                "   AND logical_limit_key=? AND observed_slot=? "
                "   AND window_minutes=? "
                "   AND unixepoch(COALESCE(canonical_resets_at_utc, "
                "                          resets_at_utc))=unixepoch(?)",
                tuple(row),
            )
        ]
    finally:
        conn.close()

    assert any(INDEX in step for step in plan), (
        f"the group predicate did not use {INDEX}: {plan!r}")


def test_no_row_is_touched(cctally_module):
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert _snapshot_rows(post) == _snapshot_rows(pre)
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = (_index_sql(conn, INDEX), _snapshot_rows(conn))
        assert first[0] is not None
        handler(conn)
        assert (_index_sql(conn, INDEX), _snapshot_rows(conn)) == first
        assert _index_sql(conn, RETIRED_INDEX) is None
    finally:
        conn.close()


def test_handler_degrades_on_a_cache_without_the_anchor_column(
    cctally_module, tmp_path
):
    """A legacy-shape cache whose schema apply never reached the anchor column
    must not fail the migration — the expression would raise ``no such
    column``."""
    work = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(work)
    try:
        conn.execute(
            "CREATE TABLE quota_window_snapshots ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
            " source_root_key TEXT, source_path TEXT NOT NULL,"
            " line_offset INTEGER NOT NULL, captured_at_utc TEXT NOT NULL,"
            " observed_slot TEXT, logical_limit_key TEXT NOT NULL,"
            " limit_id TEXT, limit_name TEXT, window_minutes INTEGER NOT NULL,"
            " used_percent REAL NOT NULL, resets_at_utc TEXT NOT NULL)"
        )
        conn.commit()
        _handler(cctally_module)(conn)
        assert _index_sql(conn, INDEX) is None
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
