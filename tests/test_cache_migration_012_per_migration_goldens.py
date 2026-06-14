"""Per-migration goldens for cache migration
``012_create_conversation_ai_titles`` (#193).

Loads ``tests/fixtures/migrations/per-migration/012_create_conversation_ai_titles/pre.sqlite``
(an existing install at the 011 head: cache migrations 001-011 applied, no
``ai_titles_backfill_pending`` flag, no 012 marker), runs the production 012
handler against a copy, and asserts the result matches ``post.sqlite``.

012 is FLAG-ONLY: it sets the DISTINCT
``cache_meta['ai_titles_backfill_pending'] = '1'`` flag so the flock-held
``_cctally_cache.backfill_ai_titles`` walks all history once (mtime-ascending,
last-write-wins) and upserts ``conversation_ai_titles``. The handler does NOT
touch any data table (the backfill is sync-side). The conversation_ai_titles
table itself is created by ``_apply_cache_schema`` (runs on every open), so it is
present in BOTH pre and post. The dispatcher central-stamps the migration marker
(#140); a fresh install stamps it WITHOUT running (its incremental fused walk
fills the table at ingest).
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
    / "012_create_conversation_ai_titles"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "012_create_conversation_ai_titles"
_FLAG = "ai_titles_backfill_pending"


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


def test_pre_fixture_at_011_head_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 011 applied, has NOT the 012 marker, does NOT carry
    the backfill flag, and already has the conversation_ai_titles table (created
    by _apply_cache_schema) — the existing-install shape before the #193 backfill
    is armed."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='011_conversation_promote_command_args'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the backfill flag"
        # The table exists (schema is applied on every open) but is empty — the
        # flock-held backfill is what populates it.
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_ai_titles"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the backfill flag set to '1' and the 012 marker
    stamped (central dispatcher stamp, #140). The table is STILL empty — the
    backfill is sync-side, not in the flag-only handler."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _flag(conn) == "1", "handler must set the backfill flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_ai_titles"
        ).fetchone()[0] == 0, (
            "flag-only handler must NOT populate the table (sync-side backfill)"
        )
    finally:
        conn.close()


def test_handler_sets_flag(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the backfill
    flag (and only the flag — no data table touched), then stamp the 012 marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _flag(conn) is None

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _flag(conn) == "1", "handler must arm the backfill flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_ai_titles"
        ).fetchone()[0] == 0, "handler must NOT populate the table"
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
