"""Per-migration goldens for cache migration ``018_create_conversation_title_fts``
(#217 S2 / E7 — title FTS over conversation_ai_titles).

Loads ``tests/fixtures/migrations/per-migration/018_create_conversation_title_fts/pre.sqlite``
(an existing install at the 017 head: cache migrations 001-017 applied, no
``conversation_title_fts_backfill_pending`` flag, no 018 marker), runs the
production 018 handler against a copy, and asserts it arms the DISTINCT flag.

018 is FLAG-ONLY: it sets the DISTINCT
``cache_meta['conversation_title_fts_backfill_pending'] = '1'`` flag so the
flock-held full sync runs ``_consume_title_fts`` (an FTS5 ``'rebuild'``, P1-7) to
populate the external-content title index from existing history. The handler does
NOT touch any data table and NEVER arms any reingest flag (P1-2: the title flag
joins ``_TARGETED_DECLINE_FLAGS`` only, never ``_REINGEST_FLAG_KEYS``). The
dispatcher central-stamps the migration marker (#140).
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
    / "018_create_conversation_title_fts"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "018_create_conversation_title_fts"
_FLAG = "conversation_title_fts_backfill_pending"
_SHARED_FLAG = "conversation_reingest_pending"


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


def _migration_handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _flag(conn, key=_FLAG) -> "str | None":
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else None


def test_pre_fixture_at_017_head_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 017 applied, has NOT the 018 marker, and does NOT
    carry the title-FTS backfill flag — the existing-install shape before the
    title FTS is armed. The title FTS table itself is present (created by
    _apply_cache_schema on every open)."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='017_arm_nested_agent_reingest'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the backfill flag"
        # The external-content title FTS table is part of the baseline schema.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='conversation_title_fts'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the title-FTS backfill flag set to '1' and the
    018 marker stamped (central dispatcher stamp, #140); no reingest flag set."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _flag(conn) == "1", "handler must set the backfill flag"
        assert _flag(conn, _SHARED_FLAG) is None, \
            "the shared conversation_reingest_pending flag must stay absent"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_distinct_flag_not_shared(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the DISTINCT
    title-FTS backfill flag (and only the flag — no data table touched) and must
    NOT arm the shared conversation_reingest_pending flag, then stamp the 018
    marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _flag(conn) is None
        assert _flag(conn, _SHARED_FLAG) is None

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _flag(conn) == "1", "handler must arm the distinct backfill flag"
        assert _flag(conn, _SHARED_FLAG) is None, \
            "handler must NOT re-arm the shared conversation_reingest_pending flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and must leave the flag set to '1'."""
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
