"""Per-migration goldens for cache migration
``023_conversation_sessions_enrichment_columns`` (#302 browse-rail enrichment).

Loads ``tests/fixtures/migrations/per-migration/023_conversation_sessions_enrichment_columns/pre.sqlite``
(an existing install at the 022 head: cache migrations 001-022 applied, no
``conversation_sessions_backfill_pending`` flag, no 023 marker), runs the
production 023 handler against a copy, and asserts it arms the flag.

023 is FLAG-ONLY: it sets the SHARED
``cache_meta['conversation_sessions_backfill_pending'] = '1'`` flag so the
flock-held post-walk recompute does the one-time full recompute+fill that
populates the new git_branch/models_json/title columns on
``conversation_sessions``. The handler does NOT touch any data table (the fill is
sync-side). The three columns themselves are added by ``_apply_cache_schema``
(runs on every open, via CREATE TABLE + add_column_if_missing), so they are
present in BOTH pre and post. The dispatcher central-stamps the migration marker
(#140); a fresh install stamps it WITHOUT running (its incremental DELETE+INSERT
re-derive fills the columns at ingest). Mirrors 013.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): declares this module exercises
# the handler's second-invocation idempotency (test names vary across modules).
IDEMPOTENCY_COVERED = True


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "023_conversation_sessions_enrichment_columns"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "023_conversation_sessions_enrichment_columns"
_FLAG = "conversation_sessions_backfill_pending"
_ENRICHMENT_COLS = {"git_branch", "models_json", "title"}


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


def _enrichment_cols(conn) -> set:
    return {r[1] for r in conn.execute(
        "PRAGMA table_info(conversation_sessions)")}


def test_pre_fixture_at_022_head_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 022 applied, has NOT the 023 marker, does NOT carry
    the backfill flag, and ALREADY has the three enrichment columns (added by
    _apply_cache_schema) — the existing-install shape before the enrichment
    backfill is armed."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='022_index_conversation_messages_model'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the backfill flag"
        assert _ENRICHMENT_COLS <= _enrichment_cols(conn), (
            "the three enrichment columns come from _apply_cache_schema, so they "
            "must be present in the pre fixture"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the backfill flag set to '1' and the 023 marker
    stamped (central dispatcher stamp, #140). The table is STILL empty — the
    recompute+fill is sync-side, not in the flag-only handler."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _flag(conn) == "1", "handler must set the backfill flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions"
        ).fetchone()[0] == 0, (
            "flag-only handler must NOT populate the table (sync-side recompute)"
        )
    finally:
        conn.close()


def test_handler_sets_flag(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the backfill
    flag (and only the flag — no data table touched), then stamp the 023 marker."""
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
            "SELECT COUNT(*) FROM conversation_sessions"
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
