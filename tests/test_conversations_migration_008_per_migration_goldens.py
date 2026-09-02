"""Per-migration goldens for conversations migration 008 render revisions."""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from _script_loader import load_script_module  # the ONE cctally loader (#630 S6)


IDEMPOTENCY_COVERED = True
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations" / "per-migration"
    / "conversations_008_conversation_render_revision"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "008_conversation_render_revision"


@pytest.fixture(scope="module")
def cctally_module():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    return load_script_module()


def _handler(module):
    return next(
        migration.handler for migration in module._CONVERSATIONS_MIGRATIONS
        if migration.name == MIGRATION
    )


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_golden_adds_columns_with_zero_default_and_preserves_rows():
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute("PRAGMA user_version").fetchone()[0] == 7
        assert post.execute("PRAGMA user_version").fetchone()[0] == 8
        for table in ("conversation_sessions", "codex_conversation_rollups"):
            assert "render_revision" not in _columns(pre, table)
            assert "render_revision" in _columns(post, table)
            assert post.execute(
                f"SELECT render_revision FROM {table}"
            ).fetchone() == (0,)
        assert post.execute(
            "SELECT session_id,title FROM conversation_sessions"
        ).fetchone() == ("claude-key", "Claude")
        assert post.execute(
            "SELECT conversation_key,title FROM codex_conversation_rollups"
        ).fetchone() == ("codex-key", "Codex")
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent(cctally_module, tmp_path):
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        conn.execute(
            "UPDATE conversation_sessions SET render_revision=17"
        )
        conn.execute(
            "UPDATE codex_conversation_rollups SET render_revision=19"
        )
        conn.commit()
        handler(conn)
        assert conn.execute(
            "SELECT render_revision FROM conversation_sessions"
        ).fetchone() == (17,)
        assert conn.execute(
            "SELECT render_revision FROM codex_conversation_rollups"
        ).fetchone() == (19,)
    finally:
        conn.close()
