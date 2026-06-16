"""Per-migration goldens for cache migration
``014_conversation_queued_prompt_reingest`` (queued-while-busy user prompts).

Loads ``tests/fixtures/migrations/per-migration/014_conversation_queued_prompt_reingest/pre.sqlite``
(an existing install at the 013 head: cache migrations 001-013 applied, no
``conversation_queued_prompt_reingest_pending`` flag, no 014 marker), runs the
production 014 handler against a copy, and asserts it arms the flag.

014 is FLAG-ONLY: it sets the DISTINCT
``cache_meta['conversation_queued_prompt_reingest_pending'] = '1'`` flag so the
flock-held #179 resumable per-file reingest re-parses every JSONL through the
parser that now promotes ``queued_command`` prompts (messages typed while the
agent was busy) to HUMAN turns. The handler does NOT touch any data table. The
dispatcher central-stamps the migration marker (#140); a fresh install stamps it
WITHOUT running (empty table -> the flag, if ever set, is a harmless no-op).
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "014_conversation_queued_prompt_reingest"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "014_conversation_queued_prompt_reingest"
_FLAG = "conversation_queued_prompt_reingest_pending"


@pytest.fixture(scope="module")
def cctally_module():
    """Load bin/cctally once per module (registers the cache migrations).
    bin/cctally has no ``.py`` suffix, so an explicit ``SourceFileLoader`` is
    required."""
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _migration_handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _flag(conn) -> "str | None":
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (_FLAG,)
    ).fetchone()
    return row[0] if row else None


def test_pre_fixture_at_013_head_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 013 applied, has NOT the 014 marker, and does NOT
    carry the reingest flag — the existing-install shape before the queued-prompt
    reingest is armed."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='013_create_conversation_sessions'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the reingest flag"
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the reingest flag set to '1' and the 014 marker
    stamped (central dispatcher stamp, #140)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _flag(conn) == "1", "handler must set the reingest flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_flag(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the reingest
    flag (and only the flag — no data table touched), then stamp the 014 marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _flag(conn) is None

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _flag(conn) == "1", "handler must arm the reingest flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and must leave the flag set to '1'
    (an INSERT-OR-REPLACE flag write is naturally idempotent)."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)
        handler(conn)  # must not raise, must leave the flag set
        cctally_module._stamp_applied(conn, _MIGRATION)
        assert _flag(conn) == "1"
    finally:
        conn.close()
