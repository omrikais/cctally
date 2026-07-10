"""Per-migration goldens for cache migration
``002_conversation_messages_backfill`` (Plan 1 Task 5; deferral is issue #139).

Loads ``tests/fixtures/migrations/per-migration/002_conversation_messages_backfill/pre.sqlite``
(an existing install's pre-feature shape: cost already cached, an EMPTY
``conversation_messages``), runs the production 002 handler against a copy, and
asserts the result matches ``post.sqlite``.

Issue #139 changed what the handler does: rather than walk the whole JSONL
history INLINE (which stalled the triggering command, even a stats-only
``cctally report``), the handler now sets the ``conversation_backfill_pending``
cache_meta flag and returns; the dispatcher central-stamps the migration marker
on the handler's clean return (#140). The actual offset-0 backfill is deferred
to the next ``sync_cache`` — that flag-consume behavior is covered by
``tests/test_conversation_ingest.py``; THIS file pins the handler's flag
contract plus the dispatcher's marker stamp (applied here via ``_stamp_applied``).

Verifies:

  * pre.sqlite has populated cost tables but EMPTY conversation_messages and no
    002 marker (the upgrade shape).
  * post.sqlite has the 002 marker stamped, the pending flag SET, and
    conversation_messages STILL EMPTY (the handler does not walk).
  * Running the handler against a copy of pre.sqlite sets the flag (the test
    then applies the dispatcher's central stamp via ``_stamp_applied``), leaves
    the cost delta cursor (session_files.last_byte_offset) and
    conversation_messages untouched, and is idempotent on a second run.

Because the handler no longer touches ``conversation_messages``, it never fires
the FTS5 sync triggers — so unlike the sync/backfill tests these handler tests
need no FTS5 skip-guard and run on a minimal (no-FTS5) sqlite build too.

Per-migration goldens are lazy-adopted (CLAUDE.md gotcha "lazy-adopted; not
retroactively backfilled"); 002 is the second cache migration to ship them
(001 was the first).
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
    / "002_conversation_messages_backfill"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture(scope="module")
def cctally_module():
    """Load bin/cctally once per module.

    The 002 handler delegates to ``_cctally_cache.backfill_conversation_messages``,
    whose ``_get_claude_data_dirs`` reads ``sys.modules['cctally']``. Loading the
    full script populates that AND registers the cache migrations. bin/cctally
    has no ``.py`` suffix, so an explicit ``SourceFileLoader`` is required."""
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
        if m.name == "002_conversation_messages_backfill":
            return m.handler
    raise AssertionError(
        "cache migration 002_conversation_messages_backfill not registered"
    )


def test_pre_fixture_is_existing_install_with_empty_messages(cctally_module):
    """Sanity: pre.sqlite carries cost state but an EMPTY message index and no
    002 marker — the pre-feature shape of an existing install."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM session_files"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='002_conversation_messages_backfill'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_pending_flag_and_marker(cctally_module):
    """Sanity: post.sqlite has the 002 marker stamped and the
    ``conversation_backfill_pending`` flag SET, with conversation_messages STILL
    EMPTY — the handler defers the walk to sync_cache (issue #139)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0, "handler must not backfill inline"
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_backfill_pending'"
        ).fetchone() == ("1",), "handler must set the pending flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='002_conversation_messages_backfill'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_pending_flag_and_marker(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the
    pending flag + stamp the marker while leaving conversation_messages AND the
    cost delta cursor untouched (the offset-0 walk is deferred to sync_cache)."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        # Pre-state.
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0
        pre_offsets = dict(conn.execute(
            "SELECT path, last_byte_offset FROM session_files"
        ).fetchall())
        assert all(v > 0 for v in pre_offsets.values()), pre_offsets

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, "002_conversation_messages_backfill")  # dispatcher now owns the stamp (#140)

        # Flag set + marker stamped, index STILL empty.
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_backfill_pending'"
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='002_conversation_messages_backfill'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0, "handler must NOT walk JSONL inline"

        # The cost delta cursor + rows must be UNTOUCHED — the handler only
        # writes cache_meta + schema_migrations.
        post_offsets = dict(conn.execute(
            "SELECT path, last_byte_offset FROM session_files"
        ).fetchall())
        assert post_offsets == pre_offsets
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and must leave the flag set (the
    marker INSERT is INSERT OR IGNORE, the flag is an upsert), with
    conversation_messages still empty."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, "002_conversation_messages_backfill")  # dispatcher now owns the stamp (#140)
        handler(conn)  # must not raise, must not duplicate
        cctally_module._stamp_applied(conn, "002_conversation_messages_backfill")
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_backfill_pending'"
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages"
        ).fetchone()[0] == 0
    finally:
        conn.close()
