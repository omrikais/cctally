"""Per-migration goldens for cache migration 035's replay arming.

Spec:
``docs/superpowers/specs/2026-07-30-codex-thread-source-inference-design.md``

The handler arms a byte-zero Codex replay; it must never PERFORM one. Clearing
`codex_session_files` here would leave the next ordinary `sync_codex_cache` with
an empty `rebuild_known_identities` snapshot, sending every re-read rollout to
the live-`auth.json` branch and re-attributing pre-mechanism spend.
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
    / "035_codex_thread_source_inference_replay"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "035_codex_thread_source_inference_replay"
MARKER = "codex_replay_from_zero_pending"

# Every re-derivable Codex family the handler must leave alone.
PRESERVED = (
    "codex_session_entries",
    "codex_session_files",
    "codex_conversation_threads",
    "codex_conversation_events",
    "codex_source_roots",
    "quota_window_snapshots",
    "codex_file_accounts",
)


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


def _marker(conn):
    return conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (MARKER,)).fetchone()


def _counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in PRESERVED
    }


def test_pre_fixture_is_a_034_head_install_with_no_marker(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 34
        assert _marker(conn) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        assert _counts(conn)["codex_session_entries"] == 1
        assert _counts(conn)["codex_session_files"] == 1
    finally:
        conn.close()


def test_post_fixture_arms_the_marker(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 35
        assert _marker(conn) == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_marker_name_matches_the_constant_the_sync_consumes(cctally_module):
    """A drifted literal would arm a marker nothing reads."""
    import _cctally_cache

    assert _cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY == MARKER


def test_no_codex_row_family_is_cleared(cctally_module):
    """The handler arms a replay; performing one here is the hazard."""
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert _counts(post) == _counts(pre)
        query = (
            "SELECT source_path, line_offset, source_root_key, account_key "
            "FROM codex_session_entries ORDER BY source_path, line_offset")
        assert list(post.execute(query)) == list(pre.execute(query))
        cursors = (
            "SELECT path, size_bytes, last_byte_offset, source_root_key "
            "FROM codex_session_files ORDER BY path")
        assert list(post.execute(cursors)) == list(pre.execute(cursors))
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
        first = (_marker(conn), _counts(conn))
        assert first[0] == ("1",)
        handler(conn)
        assert (_marker(conn), _counts(conn)) == first
    finally:
        conn.close()
