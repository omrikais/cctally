"""#682 follow-up: an existing cache.db must receive ``render_revision``.

``_apply_cache_schema`` declares both ``render_revision`` columns with
``add_column_if_missing``, but ``open_cache_db`` runs that whole schema pass
only when the store's ``PRAGMA user_version`` differs from
``len(_CACHE_MIGRATIONS)``.  #682 added the two column declarations without a
companion cache migration, so the head never moved and an already-current
store never re-ran the schema apply.  Those installs kept the pre-#682 table
shape while the shipped code assumed the new one, and every
``sync_cache`` died in ``_recompute_conversation_sessions`` at
``UPDATE conversation_sessions SET render_revision=?`` — a statement that
cannot even be prepared against the old shape.  The dashboard rendered that
as a permanent ``server sync error`` banner.

Fresh stores were unaffected, which is why no existing harness saw it: every
fixture store is created from scratch, so ``schema_current`` is false on the
first open and the CREATE TABLE statements already carry the column.  These
tests exercise the upgrade path instead.
"""
from __future__ import annotations

import sqlite3

import pytest

from conftest import load_script, redirect_paths


#: The registry head that shipped in v1.105.0, pinned as a literal.  Deriving
#: it from ``len(_CACHE_MIGRATIONS)`` would track the code under test and make
#: the fixture agree with any future head by construction.
HEAD_BEFORE_DELIVERY = 44

RENDER_REVISION_TABLES = ("conversation_sessions", "codex_conversation_rollups")


def _columns(conn: sqlite3.Connection, table: str) -> "set[str]":
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _seed_pre_delivery_store(ns, path) -> None:
    """Write a store shaped exactly like a real v1.105.0 install.

    Applying the current schema and then dropping the two columns reproduces
    the upgraded-but-unmigrated shape without needing a checked-in binary
    fixture, and it stays correct as the surrounding schema evolves.
    """
    import _cctally_db

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _cctally_db._apply_cache_schema(conn)
        for table in RENDER_REVISION_TABLES:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN render_revision")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        for item in _cctally_db._CACHE_MIGRATIONS[:HEAD_BEFORE_DELIVERY]:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
                "VALUES (?, '2026-09-02T00:00:00Z')",
                (item.name,),
            )
        conn.execute(f"PRAGMA user_version={HEAD_BEFORE_DELIVERY}")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def pre_delivery_store(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core

    _seed_pre_delivery_store(ns, _cctally_core.CACHE_DB_PATH)
    return ns


def test_seeded_store_really_lacks_the_columns(pre_delivery_store, tmp_path):
    """Guard the fixture itself, so the tests below cannot pass vacuously."""
    import _cctally_core

    conn = sqlite3.connect(_cctally_core.CACHE_DB_PATH)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == (
            HEAD_BEFORE_DELIVERY
        )
        for table in RENDER_REVISION_TABLES:
            assert "render_revision" not in _columns(conn, table)
    finally:
        conn.close()


def test_opening_an_existing_store_delivers_render_revision(pre_delivery_store):
    ns = pre_delivery_store
    conn = ns["open_cache_db"]()
    try:
        for table in RENDER_REVISION_TABLES:
            assert "render_revision" in _columns(conn, table), (
                f"{table}.render_revision was never delivered to a store that "
                "was already at the previous registry head"
            )
    finally:
        conn.close()


def test_the_statement_that_broke_every_sync_now_prepares(pre_delivery_store):
    """Pin the exact production symptom, not just the column's presence.

    ``_recompute_conversation_sessions`` issues this unconditionally on every
    sync, so its failure to prepare is what took the whole store offline.
    """
    ns = pre_delivery_store
    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE conversation_sessions SET render_revision=?", (1,))
        conn.execute(
            "UPDATE codex_conversation_rollups SET render_revision=?", (1,)
        )
    finally:
        conn.close()
