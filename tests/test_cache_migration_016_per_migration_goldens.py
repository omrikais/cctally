"""Per-migration goldens for cache migration ``016_drop_search_aux``
(#217 S1 / U7a).

Loads ``tests/fixtures/migrations/per-migration/016_drop_search_aux/pre.sqlite``
(an existing install at the 015 head: cache migrations 001-015 applied, the
post-#217 ``conversation_messages`` schema that NO LONGER carries ``search_aux``,
no 016 marker), runs the production 016 handler against a copy, and asserts it is
a CLEAN NO-OP — the column-presence guard skips-as-applied because ``search_aux``
is already gone from ``_apply_cache_schema``.

The builder golden is therefore a no-op by construction (spec #197 note): the
ACTUAL drop on a column-carrying DB is proven by
``tests/test_migration_016_drop_search_aux.py`` (which manually ``ADD COLUMN
search_aux`` rather than relying on ``_apply_cache_schema``), plus a
defer-on-pending-split regression there. This file pins the no-op + marker-stamp
post-state and guards the per-migration golden against drift.
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
    / "016_drop_search_aux"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "016_drop_search_aux"


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
    return {r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")}


def test_pre_fixture_at_015_head_without_search_aux_or_marker(cctally_module):
    """Sanity: pre.sqlite has 015 applied, NOT the 016 marker, and the
    post-#217 conversation_messages schema does NOT carry search_aux."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='015_conversation_sessions_filter_columns'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 0
        assert "search_aux" not in _cols(conn), (
            "post-#217 _apply_cache_schema must NOT emit search_aux"
        )
    finally:
        conn.close()


def test_post_fixture_has_marker_and_no_search_aux(cctally_module):
    """Sanity: post.sqlite carries the 016 marker (central dispatcher stamp,
    #140) and still has no search_aux column (the handler was a no-op)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert "search_aux" not in _cols(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_clean_noop_on_post_217_schema(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite (no search_aux column):
    it must be a clean no-op (column-presence guard) — no raise, schema
    unchanged — then the marker stamps."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        before = _cols(conn)
        assert "search_aux" not in before

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _cols(conn) == before, "handler must not change the schema"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_drops_then_is_idempotent_on_rerun(cctally_module, tmp_path):
    """Run-twice coverage on a COLUMN-CARRYING DB (the golden pre lacks
    ``search_aux``, so this is the only place the DROP path meets the rerun
    guard). Add the dead column back onto a copy of pre.sqlite (which has the
    current FTS shape — no ``conversation_fts_aux`` / no split-pending flag —
    so guard 3 passes), then invoke the handler TWICE: the first run DROPs the
    column, the second finds it gone and is a clean no-op (guard 1). This is the
    #279 S7 W1 run-twice closure — cache 016 was the one golden of 25 without a
    second-invocation test."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        conn.execute(
            "ALTER TABLE conversation_messages ADD COLUMN search_aux TEXT DEFAULT ''"
        )
        conn.commit()
        assert "search_aux" in _cols(conn)
        handler = _migration_handler(cctally_module)

        # First invocation: drops the column.
        handler(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)
        assert "search_aux" not in _cols(conn), "first run must DROP search_aux"

        # Second invocation: column already gone → guard 1 no-op, no raise.
        after_first = _cols(conn)
        handler(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)
        assert _cols(conn) == after_first, "rerun must be a clean no-op"
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()
