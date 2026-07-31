"""Per-migration goldens for conversations migration
``003_background_mcp_result_replay``.

Spec:
``docs/superpowers/specs/2026-07-31-background-mcp-result-recovery-design.md`` §4.

The handler arms the byte-zero replay that recovers backgrounded-MCP results by
writing ONE marker. It touches no transcript row, and the key is DISTINCT from
every existing reingest flag — reusing one would conflate two enrichments and
make a partially-completed replay unrecoverable.

pre.sqlite  = conversations schema at 002-head (the Codex replay marker armed)
              + one source-file row, no background marker.
post.sqlite = the row UNCHANGED, the background replay marker armed, the Codex
              marker untouched, + the 003 stamp.
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
    / "conversations_003_background_mcp_result_replay"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

MIGRATION = "003_background_mcp_result_replay"
MARKER_KEY = "conversation_background_mcp_reingest_pending"
CODEX_MARKER_KEY = "codex_conversation_replay_from_zero_pending"


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
    for m in cctally_module._CONVERSATIONS_MIGRATIONS:
        if m.name == MIGRATION:
            return m.handler
    raise AssertionError(f"conversations migration {MIGRATION} not registered")


def _meta(conn, key):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()


def _source_files(conn):
    return list(conn.execute(
        "SELECT path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at "
        "FROM conversation_source_files ORDER BY path"))


def test_pre_fixture_is_a_002_head_store_with_no_background_marker(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert _meta(conn, MARKER_KEY) is None
        assert _meta(conn, CODEX_MARKER_KEY) == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        assert len(_source_files(conn)) == 1
    finally:
        conn.close()


def test_post_fixture_arms_the_background_replay_marker(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert _meta(conn, MARKER_KEY) == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_codex_replay_marker_is_left_exactly_as_002_set_it(cctally_module):
    """The two replay markers are DISTINCT: consuming or clearing the Codex one
    here would silently discard that repair."""
    conn = sqlite3.connect(POST_DB)
    try:
        assert _meta(conn, CODEX_MARKER_KEY) == ("1",)
    finally:
        conn.close()


def test_the_marker_name_matches_the_constant_the_sync_consumes(cctally_module):
    import _cctally_cache

    assert _cctally_cache.CONVERSATION_BACKGROUND_MCP_REINGEST_KEY == MARKER_KEY
    assert MARKER_KEY in _cctally_cache._REINGEST_FLAG_KEYS
    assert MARKER_KEY in _cctally_cache._TARGETED_DECLINE_FLAGS


def test_no_transcript_row_is_touched(cctally_module):
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert _source_files(post) == _source_files(pre)
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = (_meta(conn, MARKER_KEY), _source_files(conn))
        assert first[0] == ("1",)
        handler(conn)
        assert (_meta(conn, MARKER_KEY), _source_files(conn)) == first
    finally:
        conn.close()
