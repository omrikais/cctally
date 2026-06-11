"""Per-migration goldens for cache migration
``007_conversation_reingest_enrichment`` (#177).

Loads ``tests/fixtures/migrations/per-migration/007_conversation_reingest_enrichment/pre.sqlite``
(an existing install at the 006 head: migrations 001-006 applied, a single
pre-#177 conversation_messages row whose enrichment columns are at their
default state — ``search_aux=''``, ``stop_reason``/``attribution_*`` NULL — and
whose blocks_json lacks the #177 keys, no 007 marker, the enrichment flag
unset), runs the production 007 handler against a copy, and asserts the result
matches ``post.sqlite``.

007 is FLAG-ONLY and uses a DISTINCT flag
``conversation_reingest_enrichment_pending`` (NOT the shared
``conversation_reingest_pending`` that gates migration 005's read-time
human-fallback — re-arming it could misclassify a genuine human prompt during
the pre-reingest window). Rather than clear+re-ingest inline (which would run
without the ``cache.db.lock`` flock, racing a concurrent sync, and empty the
reader on ``--no-sync`` / eager opens), the handler sets the flag and returns;
the dispatcher central-stamps the migration marker (#140). The actual clear +
offset-0 re-ingest (which lands structured tool ``input`` + ``input_truncated``,
the raised result cap + ``full_length``, ``stop_reason``/``attribution_*``, and
the ``search_aux`` aux-FTS blob on existing history) is deferred to the next
``sync_cache`` — the same consumption path 003-006 already use, with zero new
consumption code. THIS file pins the handler's flag contract + the row-unchanged
+ marker-stamp post-state.

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
    / "007_conversation_reingest_enrichment"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_FLAG = "conversation_reingest_enrichment_pending"


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
        if m.name == "007_conversation_reingest_enrichment":
            return m.handler
    raise AssertionError(
        "cache migration 007_conversation_reingest_enrichment not registered"
    )


def test_pre_fixture_is_existing_install_at_006_without_enrichment(cctally_module):
    """Sanity: pre.sqlite carries one pre-#177 conversation row (enrichment
    columns at default, blocks_json without the #177 keys), has 006 applied, and
    has neither the 007 marker nor the enrichment flag — the existing-install
    shape before the enrichment re-ingest."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        rows = conn.execute(
            "SELECT blocks_json, search_aux, stop_reason, attribution_skill, "
            "       attribution_plugin "
            "FROM conversation_messages"
        ).fetchall()
        assert len(rows) == 1
        blocks_json, search_aux, stop_reason, attr_skill, attr_plugin = rows[0]
        block = json.loads(blocks_json)[0]
        assert block["kind"] == "tool_use"
        assert "input" not in block, "pre fixture must lack the #177 input key"
        assert "input_truncated" not in block
        # Enrichment columns at default (the deferred re-ingest populates them).
        assert search_aux == "", "search_aux defaults to '' pre-reingest"
        assert stop_reason is None
        assert attr_skill is None
        assert attr_plugin is None
        # 006 applied, 007 not yet.
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='006_conversation_reingest_source_tool_use_id'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_conversation_reingest_enrichment'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta WHERE key=?", (_FLAG,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_flag_marker_and_row_unchanged(cctally_module):
    """Sanity: post.sqlite has the 007 marker stamped and the DISTINCT
    ``conversation_reingest_enrichment_pending`` flag SET, with the
    conversation_messages row UNCHANGED — the flag-only handler defers the
    clear+re-ingest to sync_cache. The SHARED ``conversation_reingest_pending``
    must NOT be touched (distinct-flag design, #177)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        # Row UNCHANGED (still pre-#177 — the handler does not clear/re-ingest).
        rows = conn.execute(
            "SELECT blocks_json, search_aux FROM conversation_messages"
        ).fetchall()
        assert len(rows) == 1
        block = json.loads(rows[0][0])[0]
        assert "input" not in block, "handler must not re-ingest inline"
        assert rows[0][1] == "", "handler must not populate search_aux inline"
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (_FLAG,)
        ).fetchone() == ("1",), "handler must set the enrichment pending flag"
        # The DISTINCT-flag contract: the shared 005 flag must NOT be re-armed.
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='conversation_reingest_pending'"
        ).fetchone()[0] == 0, "handler must NOT touch the shared 005 flag"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_conversation_reingest_enrichment'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_sets_flag_and_leaves_row_untouched(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite; it must set the
    DISTINCT enrichment flag while leaving conversation_messages UNTOUCHED (the
    clear+re-ingest is deferred to sync_cache)."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        pre_blocks = conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchone()[0]

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, "007_conversation_reingest_enrichment")

        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (_FLAG,)
        ).fetchone() == ("1",)
        # The shared 005 flag must stay untouched (distinct-flag design).
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='conversation_reingest_pending'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='007_conversation_reingest_enrichment'"
        ).fetchone()[0] == 1
        # The row must be byte-identical — the handler only writes cache_meta +
        # schema_migrations.
        assert conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchone()[0] == pre_blocks
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run must not raise and must leave the flag set (the flag
    is an upsert), with the row still unchanged."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, "007_conversation_reingest_enrichment")
        handler(conn)  # must not raise, must not duplicate
        cctally_module._stamp_applied(conn, "007_conversation_reingest_enrichment")
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (_FLAG,)
        ).fetchone() == ("1",)
        block = json.loads(conn.execute(
            "SELECT blocks_json FROM conversation_messages"
        ).fetchone()[0])[0]
        assert "input" not in block
    finally:
        conn.close()
