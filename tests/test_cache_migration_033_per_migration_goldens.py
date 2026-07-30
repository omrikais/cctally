"""Per-migration goldens for cache migration 033's #425 chain closure."""
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
    / "033_codex_reset_anchor_component_closure"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "033_codex_reset_anchor_component_closure"
ANCHOR = "2026-08-01T19:00:00Z"
RAWS = [
    "2026-08-01T19:00:00Z",
    "2026-08-01T19:20:00Z",
    "2026-08-01T19:10:00Z",
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


def _anchors(conn):
    return {
        row[0] for row in conn.execute(
            "SELECT DISTINCT canonical_resets_at_utc "
            "FROM quota_window_snapshots WHERE source='codex'")
    }


def test_pre_fixture_reproduces_the_migration_032_split(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 32
        assert _anchors(conn) == {
            "2026-08-01T19:00:00Z",
            "2026-08-01T19:20:00Z",
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_closes_the_component_and_preserves_raw_evidence(
        cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 33
        assert _anchors(conn) == {ANCHOR}
        assert [row[0] for row in conn.execute(
            "SELECT resets_at_utc FROM quota_window_snapshots "
            "ORDER BY line_offset"
        )] == RAWS
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = list(conn.execute(
            "SELECT id, resets_at_utc, canonical_resets_at_utc "
            "FROM quota_window_snapshots ORDER BY id"))
        handler(conn)
        assert list(conn.execute(
            "SELECT id, resets_at_utc, canonical_resets_at_utc "
            "FROM quota_window_snapshots ORDER BY id")) == first
    finally:
        conn.close()
