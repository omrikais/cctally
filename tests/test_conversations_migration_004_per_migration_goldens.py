"""Per-migration goldens for conversations migration 004 Codex find projection."""
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
    / "conversations_004_codex_find_projection"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "004_codex_find_projection"


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


def _handler(module):
    return next(m.handler for m in module._CONVERSATIONS_MIGRATIONS if m.name == MIGRATION)


def _meta(conn, key):
    return conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()


def _messages(conn):
    return list(conn.execute(
        "SELECT conversation_key,source_path,line_offset,kind,text "
        "FROM codex_conversation_messages ORDER BY id"
    ))


def test_pre_fixture_is_003_head_without_projection_schema():
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='codex_find_projection'"
        ).fetchone() is None
        assert len(_messages(conn)) == 1
    finally:
        conn.close()


def test_post_fixture_arms_bounded_backfill_without_touching_transcripts():
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert post.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _meta(post, "codex_find_projection_backfill_pending") == ("1",)
        assert _meta(post, "codex_find_projection_backfill_cursor") == ("0",)
        assert _meta(post, "codex_find_projection_generation") == ("0",)
        assert _meta(post, "codex_find_projection_complete_version") is None
        assert post.execute("SELECT COUNT(*) FROM codex_find_projection").fetchone()[0] == 0
        assert _messages(post) == _messages(pre)
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent_and_preserves_progress(cctally_module, tmp_path):
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        conn.execute(
            "UPDATE cache_meta SET value='17' "
            "WHERE key='codex_find_projection_backfill_cursor'"
        )
        conn.commit()
        handler(conn)
        assert _meta(conn, "codex_find_projection_backfill_cursor") == ("17",)
        assert _meta(conn, "codex_find_projection_backfill_pending") == ("1",)
        pre = sqlite3.connect(PRE_DB)
        try:
            assert _messages(conn) == _messages(pre)
        finally:
            pre.close()
    finally:
        conn.close()
