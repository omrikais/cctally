"""Per-migration goldens for cache migration
``004_conversation_reingest_subagent_kind`` (#166).

Loads ``tests/fixtures/migrations/per-migration/004_conversation_reingest_subagent_kind/pre.sqlite``
(an existing install's post-#164/pre-#166 shape: 003 already applied, a single
id-aware-but-kind-less conversation_messages row, no 004 marker), runs the
production 004 handler against a copy, and asserts the result matches
``post.sqlite``.

004 is FLAG-ONLY and REUSES 003's ``conversation_reingest_pending`` flag: rather
than clear+re-ingest inline (which would run without the ``cache.db.lock`` flock,
racing a concurrent sync, and empty the reader on ``--no-sync`` / eager opens),
the handler sets the flag and returns; the dispatcher central-stamps the
migration marker (#140). The actual clear + offset-0 re-ingest (which lands the
spawn ``subagent_type`` + record-level ``toolUseResult`` agentId/meta on existing
history) is deferred to the next ``sync_cache`` — the same consumption path 003
already uses, with zero new consumption code. THIS file pins the handler's flag
contract + the row-unchanged + marker-stamp post-state.

Because the handler never touches ``conversation_messages``, it never fires the
FTS5 sync triggers — so unlike the sync/backfill tests these handler tests need
no FTS5 skip-guard and run on a minimal (no-FTS5) sqlite build too.
"""
from __future__ import annotations

import importlib.util as ilu
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "004_conversation_reingest_subagent_kind"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


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
        if m.name == "004_conversation_reingest_subagent_kind":
            return m.handler
    raise AssertionError(
        "cache migration 004_conversation_reingest_subagent_kind not registered"
    )


def test_pre_fixture_is_existing_install_with_kindless_row(cctally_module):
    """Sanity: pre.sqlite carries one id-aware-but-kind-less conversation row,
    has 003 applied, and no 004 marker — the post-#164/pre-#166 shape of an
    existing install."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        rows = conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchall()
        assert len(rows) == 1
        block = json.loads(rows[0][0])[0]
        assert block["kind"] == "tool_use"
        assert "id" in block, "pre fixture is the post-#164 id-aware shape"
        assert "subagent_type" not in block, "pre fixture must be pre-#166 (kind-less)"
        # 003 already applied, 004 not yet.
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='003_conversation_reingest_tool_ids'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='004_conversation_reingest_subagent_kind'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='conversation_reingest_pending'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_pending_flag_marker_and_row_unchanged(cctally_module):
    """Sanity: post.sqlite has the 004 marker stamped and the (reused)
    ``conversation_reingest_pending`` flag SET, with the conversation_messages
    row UNCHANGED — the flag-only handler defers the clear+re-ingest to
    sync_cache (#166)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        # Row UNCHANGED (still kind-less — the handler does not clear/re-ingest).
        rows = conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchall()
        assert len(rows) == 1
        block = json.loads(rows[0][0])[0]
        assert "subagent_type" not in block, "handler must not re-ingest inline"
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_reingest_pending'"
        ).fetchone() == ("1",), "handler must set the pending flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='004_conversation_reingest_subagent_kind'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_flag_and_leaves_row_untouched(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the
    pending flag while leaving conversation_messages UNTOUCHED (the
    clear+re-ingest is deferred to sync_cache)."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        pre_blocks = conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchone()[0]

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, "004_conversation_reingest_subagent_kind")

        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_reingest_pending'"
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='004_conversation_reingest_subagent_kind'"
        ).fetchone()[0] == 1
        # The row must be byte-identical — the handler only writes cache_meta +
        # schema_migrations.
        assert conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchone()[0] == pre_blocks
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and must leave the flag set (the
    flag is an upsert), with the row still unchanged."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, "004_conversation_reingest_subagent_kind")
        handler(conn)  # must not raise, must not duplicate
        cctally_module._stamp_applied(conn, "004_conversation_reingest_subagent_kind")
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='conversation_reingest_pending'"
        ).fetchone() == ("1",)
        block = json.loads(conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchone()[0])[0]
        assert "subagent_type" not in block
    finally:
        conn.close()
