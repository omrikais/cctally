"""Per-migration goldens for cache migration
``011_conversation_promote_command_args`` (#188 bug 4).

Loads ``tests/fixtures/migrations/per-migration/011_conversation_promote_command_args/pre.sqlite``
(an existing install at the 010 head: cache migrations 001-010 applied, no
``conversation_promote_command_args_pending`` flag, no 011 marker, plus one
legacy ``entry_type='meta'`` command-marker row whose ``<command-args>`` carry a
real user prompt), runs the production 011 handler against a copy, and asserts
the result matches ``post.sqlite``.

011 is FLAG-ONLY: it sets the DISTINCT
``cache_meta['conversation_promote_command_args_pending'] = '1'`` flag so the
flock-held ``_cctally_cache._consume_promote_command_args`` flips legacy meta
command rows to ``entry_type='human'`` (text=args) and recomputes the split
search columns. The handler does NOT touch any data table (the flip is
sync-side). The dispatcher central-stamps the migration marker (#140); a fresh
install stamps it WITHOUT running (fresh rows are already promoted at ingest).
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
    / "011_conversation_promote_command_args"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "011_conversation_promote_command_args"
_FLAG = "conversation_promote_command_args_pending"


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


def test_pre_fixture_at_010_head_without_flag_or_marker(cctally_module):
    """Sanity: pre.sqlite has 010 applied, has NOT the 011 marker, does NOT carry
    the promote flag, and seeds one legacy entry_type='meta' command row — the
    existing-install shape before the #188 bug-4 promotion is armed."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='010_conversation_search_split'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert _flag(conn) is None, "pre fixture must NOT carry the promote flag"
        # The seeded legacy meta command row is present and still META (untouched).
        row = conn.execute(
            "SELECT entry_type, text FROM conversation_messages WHERE uuid='u1'"
        ).fetchone()
        assert row is not None, "pre fixture must seed the legacy command row"
        assert row[0] == "meta" and row[1] == ""
    finally:
        conn.close()


def test_post_fixture_has_flag_set_and_marker_stamped(cctally_module):
    """Sanity: post.sqlite carries the promote flag set to '1' and the 011 marker
    stamped (central dispatcher stamp, #140). The seeded data row is STILL META
    — the flip is sync-side, not in the flag-only handler."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _flag(conn) == "1", "handler must set the promote flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT entry_type, text FROM conversation_messages WHERE uuid='u1'"
        ).fetchone()
        assert row[0] == "meta" and row[1] == "", (
            "flag-only handler must NOT flip the data row (sync-side)"
        )
    finally:
        conn.close()


def test_handler_sets_flag(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the promote
    flag (and only the flag — no data row touched), then stamp the 011 marker."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _flag(conn) is None

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _flag(conn) == "1", "handler must arm the promote flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT entry_type, text FROM conversation_messages WHERE uuid='u1'"
        ).fetchone()
        assert row[0] == "meta" and row[1] == "", "handler must NOT flip the row"
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


def test_consumer_promotes_seeded_legacy_command_row(cctally_module, tmp_path):
    """End-to-end of the migration's PURPOSE: after the handler arms the flag, the
    flock-held consumer (run directly here) flips the seeded legacy meta command
    row to entry_type='human' with text=args, and clears the flag. Proves the
    flag the handler set is actionable by the production consumer (not just a
    dangling key)."""
    import _cctally_cache  # the consumer lives here

    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)
        assert _flag(conn) == "1"

        # Run the production consumer (the sync-side flock-held worker).
        _cctally_cache._consume_promote_command_args(conn)

        row = conn.execute(
            "SELECT entry_type, text FROM conversation_messages WHERE uuid='u1'"
        ).fetchone()
        assert row[0] == "human", "consumer must promote the legacy command row"
        assert "Review feat/x" in row[1], "promoted text must be the <command-args>"
        assert _flag(conn) is None, "consumer must clear the flag when exhausted"
    finally:
        conn.close()
