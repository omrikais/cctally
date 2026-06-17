"""Per-migration goldens for cache migration
``015_conversation_sessions_filter_columns`` (browse-rail filter columns).

Loads ``tests/fixtures/migrations/per-migration/015_conversation_sessions_filter_columns/pre.sqlite``
(an existing install at the 014 head: cache migrations 001-014 applied, the
``conversation_sessions`` rollup carrying ONLY the four structural columns, no
015 marker), runs the production 015 handler against a copy, and asserts it adds
the three filter columns (``project_label``/``cost_usd``/``cache_rebuild_count``)
and arms the SHARED ``conversation_sessions_backfill_pending`` flag so the next
sync_cache full recompute fills them.

015 ALTER-adds the three columns (idempotent: duplicate-column tolerated) and
sets the backfill flag — it does NO per-session assemble work (the heavy
cache_rebuild_count derive rides the sync-side recompute, mirroring 013). The
dispatcher central-stamps the migration marker (#140); a fresh install gets the
columns from _apply_cache_schema's CREATE TABLE and stamps 015 WITHOUT running.
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
    / "015_conversation_sessions_filter_columns"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "015_conversation_sessions_filter_columns"
_FLAG = "conversation_sessions_backfill_pending"
_NEW_COLS = {"project_label", "cost_usd", "cache_rebuild_count"}


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


def _cols(conn) -> "set[str]":
    return {r[1] for r in conn.execute("PRAGMA table_info(conversation_sessions)")}


def _flag(conn) -> "str | None":
    row = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?", (_FLAG,)
    ).fetchone()
    return row[0] if row else None


def test_pre_fixture_at_014_head_without_columns_or_marker(cctally_module):
    """Sanity: pre.sqlite has 014 applied, NOT the 015 marker, and the rollup
    has ONLY the four structural columns — the existing-install shape before the
    filter columns are added."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='014_conversation_queued_prompt_reingest'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        cols = _cols(conn)
        assert _NEW_COLS.isdisjoint(cols), (
            f"pre fixture must NOT carry the filter columns: {cols & _NEW_COLS}")
        assert cols == {"session_id", "msg_count", "started_utc",
                        "last_activity_utc"}
    finally:
        conn.close()


def test_post_fixture_has_columns_and_marker(cctally_module):
    """Sanity: post.sqlite carries the three filter columns, the backfill flag
    set to '1', and the 015 marker stamped (central dispatcher stamp, #140)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _NEW_COLS <= _cols(conn)
        assert _flag(conn) == "1"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_adds_columns_and_arms_flag(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must add the three
    filter columns and arm the shared backfill flag, then stamp the 015 marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _NEW_COLS.isdisjoint(_cols(conn))
        assert _flag(conn) is None

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _NEW_COLS <= _cols(conn)
        assert _flag(conn) == "1", "handler must arm the backfill flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise (the ALTERs swallow the
    duplicate-column OperationalError) and must leave the columns + flag in
    place."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)
        handler(conn)  # must not raise (columns already present)
        cctally_module._stamp_applied(conn, _MIGRATION)
        assert _NEW_COLS <= _cols(conn)
        assert _flag(conn) == "1"
    finally:
        conn.close()
