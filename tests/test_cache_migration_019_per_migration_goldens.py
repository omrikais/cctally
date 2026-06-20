"""Per-migration goldens for cache migration ``019_create_conversation_file_touches``
(#217 S2 / I-3 — file-path search axis over conversation_file_touches).

Loads ``tests/fixtures/migrations/per-migration/019_create_conversation_file_touches/pre.sqlite``
(an existing install at the 018 head: cache migrations 001-018 applied, no
``conversation_reingest_file_touches_pending`` flag, no 019 marker), runs the
production 019 handler against a copy, and asserts it arms the DISTINCT flag.

019 is FLAG-ONLY: it sets the DISTINCT
``cache_meta['conversation_reingest_file_touches_pending'] = '1'`` flag so the
flock-held full sync runs ``_consume_file_touches`` to derive
conversation_file_touches from existing blocks_json history. The handler does NOT
touch any data table and NEVER arms any reingest flag (P1-2: the file-touch flag
joins ``_TARGETED_DECLINE_FLAGS`` only, never ``_REINGEST_FLAG_KEYS``). The
dispatcher central-stamps the migration marker (#140). The
``conversation_file_touches`` table itself is part of the baseline schema (created
by ``_apply_cache_schema`` on every open, before the FTS branch).
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
    / "019_create_conversation_file_touches"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "019_create_conversation_file_touches"
_FLAG = "conversation_reingest_file_touches_pending"
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


def test_pre_fixture_at_018_head_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 018 applied, has NOT the 019 marker, and does NOT
    carry the file-touch backfill flag — the existing-install shape before the
    file-path axis is armed. The conversation_file_touches table itself is present
    (created by _apply_cache_schema on every open)."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='018_create_conversation_title_fts'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the backfill flag"
        # The file-touch table is part of the baseline schema.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='conversation_file_touches'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the file-touch backfill flag set to '1' and the
    019 marker stamped (central dispatcher stamp, #140); no reingest flag set."""
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
    file-touch backfill flag (and only the flag — no data table touched) and must
    NOT arm the shared conversation_reingest_pending flag, then stamp the 019
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
