"""Per-migration goldens for cache migration
``006_conversation_reingest_source_tool_use_id`` (#279 S7 W3 backfill).

Flag-only handler: it sets the DISTINCT
``conversation_source_tool_use_reingest_pending`` flag (NOT the shared
``conversation_reingest_pending`` — re-arming that would re-enable migration
005's read-time human-fallback) so sync_cache later backfills the message-level
``source_tool_use_id``. The marker is what triggers the re-ingest on an install
already at 005.

pre.sqlite = existing install with 005 applied + one conversation_messages row;
post.sqlite = the row UNCHANGED + the distinct flag set + the 006 marker.
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
    / "006_conversation_reingest_source_tool_use_id"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

MIGRATION = "006_conversation_reingest_source_tool_use_id"
FLAG_KEY = "conversation_source_tool_use_reingest_pending"
# The shared flag migration 005 uses — 006 must NOT re-arm it.
SHARED_FLAG_KEY = "conversation_reingest_pending"


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
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {MIGRATION} not registered")


def _row_blocks(conn):
    return conn.execute("SELECT blocks_json FROM conversation_messages").fetchall()


def test_pre_fixture_lacks_flag_and_marker(cctally_module):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert len(_row_blocks(conn)) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key=?", (FLAG_KEY,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_distinct_flag_marker_row_unchanged(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    pre_blocks = _row_blocks(sqlite3.connect(PRE_DB))
    conn = sqlite3.connect(POST_DB)
    try:
        assert _row_blocks(conn) == pre_blocks
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (FLAG_KEY,)
        ).fetchone() == ("1",)
        # The distinct flag — NOT the shared one — is set.
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key=?", (SHARED_FLAG_KEY,)
        ).fetchone()[0] == 0, "006 must NOT re-arm 005's shared flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_distinct_flag_and_is_idempotent(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        pre_blocks = _row_blocks(conn)
        handler = _handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, MIGRATION)
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (FLAG_KEY,)
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key=?", (SHARED_FLAG_KEY,)
        ).fetchone()[0] == 0
        assert _row_blocks(conn) == pre_blocks
        # Second invocation: upsert → still "1", no raise.
        handler(conn)
        cctally_module._stamp_applied(conn, MIGRATION)
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (FLAG_KEY,)
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()
