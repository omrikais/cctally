"""Per-migration goldens for conversations migration 007 find projection v2."""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations" / "per-migration"
    / "conversations_007_codex_find_projection_v2_meta"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "007_codex_find_projection_v2_meta"


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
    return next(
        migration.handler for migration in module._CONVERSATIONS_MIGRATIONS
        if migration.name == MIGRATION
    )


def _meta(conn, key):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)
    ).fetchone()


def _messages(conn):
    return conn.execute(
        "SELECT id,kind,text,detail_json FROM codex_conversation_messages ORDER BY id"
    ).fetchall()


def _projection(conn):
    return conn.execute(
        "SELECT message_id,projected_text,leaves_json,projection_version "
        "FROM codex_find_projection ORDER BY message_id,surface"
    ).fetchall()


def test_golden_arms_v2_rebuild_without_mutating_retained_truth():
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute("PRAGMA user_version").fetchone()[0] == 6
        assert post.execute("PRAGMA user_version").fetchone()[0] == 7
        assert _meta(pre, "codex_find_projection_complete_version") == ("1",)
        assert _meta(post, "codex_find_projection_complete_version") is None
        assert _meta(post, "codex_find_projection_backfill_pending") == ("1",)
        assert _meta(post, "codex_find_projection_backfill_cursor") == ("0",)
        assert _meta(post, "codex_find_projection_backfill_version") == ("2",)
        assert _meta(post, "codex_find_projection_generation") == ("9",)
        assert _messages(post) == _messages(pre)
        assert _projection(post) == _projection(pre)
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent_and_preserves_v2_progress(cctally_module, tmp_path):
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
        assert _meta(conn, "codex_find_projection_backfill_version") == ("2",)
        assert _meta(conn, "codex_find_projection_backfill_pending") == ("1",)
    finally:
        conn.close()
