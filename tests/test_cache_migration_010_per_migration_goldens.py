"""Per-migration goldens for cache migration
``010_conversation_search_split`` (#177 S6).

Loads ``tests/fixtures/migrations/per-migration/010_conversation_search_split/pre.sqlite``
(an existing install at the 009 head: cache migrations 001-009 applied, the
LEGACY single-column ``conversation_fts(text)`` + ``conversation_fts_aux`` FTS
shape, no ``conversation_search_split_pending`` flag, no 010 marker), runs the
production 010 handler against a copy, and asserts the result matches
``post.sqlite``.

010 is FLAG-ONLY: it sets the DISTINCT
``cache_meta['conversation_search_split_pending'] = '1'`` flag so the flock-held
``_cctally_cache._consume_search_split`` backfills search_tool/search_thinking
from each row's blocks_json and swaps the legacy two-table FTS to the
consolidated split shape. It does NOT clear+rebuild and does NOT touch any data
table or the FTS shape itself (the swap is sync-side). The dispatcher
central-stamps the migration marker (#140).
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
    / "010_conversation_search_split"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "010_conversation_search_split"
_FLAG = "conversation_search_split_pending"


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


def test_pre_fixture_at_009_head_legacy_shape_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 009 applied, has NOT the 010 marker, does NOT carry
    the split flag, and carries the LEGACY two-table FTS shape (single-column
    conversation_fts + conversation_fts_aux) — the existing-install shape before
    the #177 S6 search split is armed."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='009_conversation_media_reingest'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the split flag"
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversation_fts)")]
        assert cols == ["text"], "pre fixture must carry the legacy single-column shape"
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='conversation_fts_aux'"
        ).fetchone() is not None, "pre fixture must carry the legacy aux table"
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the split flag set to '1' and the 010 marker
    stamped (central dispatcher stamp, #140). The FTS shape is still legacy — the
    swap is sync-side, not in the flag-only handler."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _flag(conn) == "1", "handler must set the split flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversation_fts)")]
        assert cols == ["text"], "flag-only handler must NOT swap the FTS shape"
    finally:
        conn.close()


def test_handler_sets_flag(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the split
    flag (and only the flag — no data table or FTS shape touched), then stamp the
    010 marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _flag(conn) is None

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _flag(conn) == "1", "handler must arm the split flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversation_fts)")]
        assert cols == ["text"], "handler must NOT swap the FTS shape"
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
