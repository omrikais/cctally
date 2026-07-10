"""Per-migration goldens for cache migration ``005_conversation_reingest_meta``
(#279 S7 W3 backfill).

Flag-only handler (same class as cache 003/004): it sets the SHARED
``conversation_reingest_pending`` cache_meta flag (reused from 003/004) so
sync_cache later reclassifies injected ``isMeta`` user lines; the marker is
what triggers this re-ingest on an install already at 004. The actual clear +
offset-0 re-ingest is deferred to sync_cache under the ``cache.db.lock`` flock.

pre.sqlite = existing install with 004 applied + one conversation_messages row;
post.sqlite = the row UNCHANGED + the flag set + the 005 marker.
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
    / "005_conversation_reingest_meta"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

MIGRATION = "005_conversation_reingest_meta"
FLAG_KEY = "conversation_reingest_pending"


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


def test_post_fixture_has_flag_marker_row_unchanged(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    pre_blocks = _row_blocks(sqlite3.connect(PRE_DB))
    conn = sqlite3.connect(POST_DB)
    try:
        assert _row_blocks(conn) == pre_blocks, "flag-only handler must not touch the row"
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (FLAG_KEY,)
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_flag_and_is_idempotent(cctally_module, tmp_path):
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
        assert _row_blocks(conn) == pre_blocks
        # Second invocation: flag is an upsert → still "1", no raise.
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
