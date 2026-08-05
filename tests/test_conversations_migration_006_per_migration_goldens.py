"""Per-migration goldens for conversations migration 006 file touches."""
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
    / "conversations_006_backfill_codex_file_touches"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "006_backfill_codex_file_touches"


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


def _touches(conn):
    return conn.execute(
        "SELECT t.conversation_key,t.source_path,t.file_path,t.tool,m.line_offset "
        "FROM codex_conversation_file_touches t "
        "JOIN codex_conversation_messages m ON m.id=t.message_id "
        "ORDER BY t.file_path"
    ).fetchall()


def test_golden_backfills_retained_dict_patch_rows():
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute("PRAGMA user_version").fetchone()[0] == 5
        assert post.execute("PRAGMA user_version").fetchone()[0] == 6
        assert _touches(pre) == []
        assert _touches(post) == [
            ("codex-key", "/codex.jsonl", "src/legacy-alpha.py", "apply_patch", 42),
            ("codex-key", "/codex.jsonl", "src/legacy-beta.py", "apply_patch", 42),
        ]
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
        first = _touches(conn)
        handler(conn)
        assert _touches(conn) == first
    finally:
        conn.close()
