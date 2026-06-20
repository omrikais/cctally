"""Unit + regression coverage for cache migration ``016_drop_search_aux``
(#217 S1 / U7a).

016 drops the documented-dead ``conversation_messages.search_aux`` column under
THREE guards:

  1. column-presence — idempotent skip-as-applied when the column is already
     gone (the post-#217 ``_apply_cache_schema`` no longer emits it, so a fresh
     install never carries it);
  2. ``sqlite_version() >= 3.35`` — older SQLite has no ``ALTER TABLE … DROP
     COLUMN``, so the handler skips-as-applied leaving the harmless dead column;
  3. search-split-consumed gate (Codex P1) — ``DROP COLUMN`` FAILS while the
     legacy ``conversation_fts_aux`` table / its triggers reference ``search_aux``
     (and while migration 010's ``conversation_search_split_pending`` flag is
     set), so the handler DEFERS via ``MigrationGateNotMet`` until the
     migration-010 state machine (``_consume_search_split`` in sync_cache) has
     consumed the split. 016 does NOT tear the aux table down itself.

The per-migration golden builder sources ``pre.sqlite`` from the CURRENT
``_apply_cache_schema``, which no longer carries ``search_aux`` — so the builder
golden for 016 is a clean no-op (pre already lacks the column; the idempotent
handler skips). The actual drop is therefore proven HERE: this test manually
``ADD COLUMN search_aux`` (not via ``_apply_cache_schema``), runs the handler,
and asserts the column is gone — plus a regression for the 010-pending/legacy-aux
topology asserting 016 defers rather than failing.
"""
from __future__ import annotations

import importlib.util as ilu
import sqlite3
import sys
from pathlib import Path

import pytest


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


@pytest.fixture
def db_module(cctally_module):
    return sys.modules["_cctally_db"]


def _handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _cols(conn) -> "set[str]":
    return {r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")}


def _sqlite_supports_drop_column() -> bool:
    return sqlite3.sqlite_version_info >= (3, 35, 0)


def _apply_schema(conn, db_module) -> None:
    db_module._apply_cache_schema(conn)


def _add_search_aux(conn) -> None:
    """Manually add back the legacy (now-dropped-from-the-live-schema) column,
    so the drop has something to remove — mirrors a real existing install that
    carried ``search_aux`` before this migration."""
    conn.execute(
        "ALTER TABLE conversation_messages "
        "ADD COLUMN search_aux TEXT NOT NULL DEFAULT ''"
    )
    conn.commit()


def test_016_registered_at_contiguous_head(cctally_module):
    """016 must be the 16th cache migration (head was 015; numbering is
    import-time-contiguous, so a gap or duplicate would have raised at load)."""
    names = [m.name for m in cctally_module._CACHE_MIGRATIONS]
    assert names[15] == _MIGRATION, names
    assert names.index(_MIGRATION) == 15


@pytest.mark.skipif(
    not _sqlite_supports_drop_column(),
    reason="this build of SQLite lacks ALTER TABLE … DROP COLUMN (<3.35)",
)
def test_016_drops_search_aux_when_present(cctally_module, db_module, tmp_path):
    """The handler drops a present ``search_aux`` column (no legacy aux FTS, no
    pending split flag → all three guards clear)."""
    work = tmp_path / "cache.db"
    conn = sqlite3.connect(work)
    try:
        _apply_schema(conn, db_module)
        _add_search_aux(conn)
        assert "search_aux" in _cols(conn)

        _handler(cctally_module)(conn)

        assert "search_aux" not in _cols(conn), (
            "016 must drop the dead search_aux column when all guards clear"
        )
    finally:
        conn.close()


def test_016_idempotent_skip_when_already_absent(
    cctally_module, db_module, tmp_path
):
    """Column-presence guard: a fresh install (post-#217 ``_apply_cache_schema``
    never emits ``search_aux``) runs the handler as a no-op — no raise, column
    stays absent."""
    work = tmp_path / "cache.db"
    conn = sqlite3.connect(work)
    try:
        _apply_schema(conn, db_module)
        assert "search_aux" not in _cols(conn), (
            "post-#217 _apply_cache_schema must NOT emit search_aux"
        )
        # Idempotent: the handler returns cleanly and the column stays gone.
        _handler(cctally_module)(conn)
        assert "search_aux" not in _cols(conn)
        # Second run is also a no-op.
        _handler(cctally_module)(conn)
        assert "search_aux" not in _cols(conn)
    finally:
        conn.close()


def test_016_defers_on_pending_split_flag(cctally_module, db_module, tmp_path):
    """Search-split-consumed gate (Codex P1): when migration 010's
    ``conversation_search_split_pending`` flag is set, 016 DEFERS via
    ``MigrationGateNotMet`` — it does NOT drop the column and does NOT raise a
    hard failure (the flag means the legacy aux FTS may still be live)."""
    MigrationGateNotMet = db_module.MigrationGateNotMet
    work = tmp_path / "cache.db"
    conn = sqlite3.connect(work)
    try:
        _apply_schema(conn, db_module)
        _add_search_aux(conn)
        db_module._set_cache_meta(
            conn, "conversation_search_split_pending", "1")
        conn.commit()

        with pytest.raises(MigrationGateNotMet):
            _handler(cctally_module)(conn)

        assert "search_aux" in _cols(conn), (
            "016 must NOT drop search_aux while the split is pending"
        )
    finally:
        conn.close()


def test_016_defers_on_legacy_aux_fts_topology(
    cctally_module, db_module, tmp_path
):
    """Search-split-consumed gate (Codex P1): the load-bearing regression. A DB
    standing up the LEGACY ``conversation_fts_aux`` external-content table + its
    triggers over ``search_aux`` (the pre-S6 shape, no pending flag) — running
    016 must DEFER via ``MigrationGateNotMet`` rather than attempting the
    ``DROP COLUMN`` (which FAILS while a trigger references ``search_aux``)."""
    MigrationGateNotMet = db_module.MigrationGateNotMet
    work = tmp_path / "cache.db"
    conn = sqlite3.connect(work)
    try:
        _apply_schema(conn, db_module)
        _add_search_aux(conn)
        if not db_module._fts5_available(conn):
            pytest.skip("FTS5 unavailable on this SQLite build")
        # Tear the fresh split FTS down to the legacy prose+aux pair so the aux
        # index + triggers reference search_aux (the topology DROP COLUMN can't
        # survive). Mirrors the migration-010 fixture builder's _to_legacy_shape.
        db_module._drop_conversation_fts_triggers(conn)
        conn.execute("DROP TABLE IF EXISTS conversation_fts")
        conn.execute("DROP TABLE IF EXISTS conversation_fts_aux")
        conn.execute(
            "CREATE VIRTUAL TABLE conversation_fts "
            "USING fts5(text, content='conversation_messages', "
            "content_rowid='id')")
        db_module._create_conversation_fts_aux_table(conn)
        db_module._create_conversation_fts_legacy_triggers(conn)
        conn.commit()
        # No pending flag set — the gate must still catch the live aux table.
        assert conn.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='conversation_search_split_pending'"
        ).fetchone() is None

        with pytest.raises(MigrationGateNotMet):
            _handler(cctally_module)(conn)

        assert "search_aux" in _cols(conn), (
            "016 must NOT drop search_aux while the legacy aux FTS is live"
        )
        # The aux table must be UNTOUCHED — 010's state machine owns its teardown.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='conversation_fts_aux'"
        ).fetchone() is not None, (
            "016 must NOT tear down the aux table (010's state machine owns it)"
        )
    finally:
        conn.close()
