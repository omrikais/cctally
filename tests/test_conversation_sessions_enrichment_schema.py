"""#302 Task 1 — schema for the browse-rail enrichment columns.

Two guards:
  1. ``_apply_cache_schema`` (runs on every open, before the dispatcher and any
     rail read) adds ``git_branch`` / ``models_json`` / ``title`` to an existing
     pre-023 ``conversation_sessions`` rollup via ``add_column_if_missing`` — so a
     rail read can never ``SELECT`` a missing column (Codex P1-2, the repo's
     column-addition rule, NOT a raw ALTER in a migration).
  2. The flag-only migration ``023_conversation_sessions_enrichment_columns`` arms
     the SHARED ``conversation_sessions_backfill_pending`` flag so the next
     sync_cache full recompute fills the new columns (mirrors 013). The handler
     does NO data work.

Uses the ``load_script()`` idiom (populates sys.modules for the bin/ siblings) so
the REAL ``_apply_cache_schema`` + registered ``_CACHE_MIGRATIONS`` are exercised.
"""
import pathlib
import sqlite3
import sys

from conftest import load_script  # type: ignore

_MIGRATION = "023_conversation_sessions_enrichment_columns"
_FLAG = "conversation_sessions_backfill_pending"
_NEW_COLS = {"git_branch", "models_json", "title"}


def _db_module(ns):
    bin_dir = str(pathlib.Path(ns["__file__"]).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import _cctally_db as db
    return db


def _handler(db):
    for m in db._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def test_apply_cache_schema_adds_enrichment_columns_to_old_rollup(tmp_path):
    ns = load_script()
    db = _db_module(ns)
    conn = sqlite3.connect(tmp_path / "cache.db")
    try:
        # Simulate a pre-023 rollup: the migration-015 shape WITHOUT the #302
        # enrichment columns.
        conn.execute(
            "CREATE TABLE conversation_sessions ("
            " session_id TEXT NOT NULL PRIMARY KEY,"
            " msg_count INTEGER NOT NULL DEFAULT 0,"
            " started_utc TEXT, last_activity_utc TEXT,"
            " project_label TEXT, cost_usd REAL NOT NULL DEFAULT 0,"
            " cache_rebuild_count INTEGER NOT NULL DEFAULT 0)")
        conn.commit()
        db._apply_cache_schema(conn)  # must ADD the missing columns idempotently
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(conversation_sessions)")}
        assert _NEW_COLS <= cols, f"missing enrichment columns; got {cols}"
        # Idempotent: a second apply must not raise (duplicate-column tolerated).
        db._apply_cache_schema(conn)
    finally:
        conn.close()


def test_fresh_schema_carries_enrichment_columns(tmp_path):
    """A FRESH cache.db (no pre-existing table) gets the three columns straight
    from the base CREATE TABLE."""
    ns = load_script()
    db = _db_module(ns)
    conn = sqlite3.connect(tmp_path / "cache.db")
    try:
        db._apply_cache_schema(conn)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(conversation_sessions)")}
        assert _NEW_COLS <= cols, f"fresh schema missing columns; got {cols}"
    finally:
        conn.close()


def test_migration_023_arms_backfill_flag(tmp_path):
    ns = load_script()
    db = _db_module(ns)
    conn = sqlite3.connect(tmp_path / "cache.db")
    try:
        db._apply_cache_schema(conn)
        conn.execute("DELETE FROM cache_meta WHERE key=?", (_FLAG,))
        conn.commit()
        _handler(db)(conn)  # the registered 023 handler
        row = conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (_FLAG,)).fetchone()
        assert row is not None and row[0] == "1"
    finally:
        conn.close()
