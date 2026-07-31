"""Per-migration goldens for the Codex quota window-identity covering index.

Public issue omrikais/cctally#5. The migration exists only because the cache
schema apply is version-gated: an install already at the registry head never
re-runs ``_apply_cache_schema``, so an index dropped into that function alone
would never appear on an upgraded machine.

The column list is asserted explicitly. The identity prefix is what the exact
physical-group filter matches on, and the trailing ``captured_at_utc`` /
``used_percent`` / ``id`` are the remaining observation-load inputs — an index
trimmed back to the identity columns still leaves the full-sweep path
table-scanning, which is the cost the index exists to remove.
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
    / "036_codex_quota_window_identity_index"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "036_codex_quota_window_identity_index"
INDEX = "idx_qws_window_ident"

EXPECTED_COLUMNS = [
    "source", "source_root_key", "logical_limit_key", "observed_slot",
    "window_minutes", "resets_at_utc", "canonical_resets_at_utc",
    "captured_at_utc", "used_percent", "id",
]


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


def _has_index(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (INDEX,),
    ).fetchone() is not None


def _index_columns(conn) -> list[str]:
    return [
        str(row[2]) for row in conn.execute(f"PRAGMA index_info({INDEX})")
    ]


def _snapshot_rows(conn):
    return list(conn.execute(
        "SELECT source, source_root_key, source_path, line_offset, "
        "       captured_at_utc, used_percent, resets_at_utc, "
        "       canonical_resets_at_utc "
        "  FROM quota_window_snapshots ORDER BY source_path, line_offset"))


def test_pre_fixture_is_a_035_head_install_without_the_index(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 35
        assert not _has_index(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        # Non-vacuity: an index over an empty table proves nothing.
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots").fetchone()[0] == 2
    finally:
        conn.close()


def test_post_fixture_carries_the_index_with_the_measured_columns(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 36
        assert _has_index(conn)
        assert _index_columns(conn) == EXPECTED_COLUMNS
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


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
        first = (_index_columns(conn), _snapshot_rows(conn))
        assert first[0] == EXPECTED_COLUMNS
        handler(conn)
        assert (_index_columns(conn), _snapshot_rows(conn)) == first
    finally:
        conn.close()


def test_handler_degrades_on_a_cache_without_the_anchor_column(
    cctally_module, tmp_path
):
    """A legacy-shape cache whose schema apply never reached the anchor column
    must not fail the migration — the index would raise ``no such column``."""
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
        assert not _has_index(conn)
    finally:
        conn.close()
