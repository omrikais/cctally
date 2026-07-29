"""Per-migration goldens for cache migration ``031_codex_file_account_map``
(#416 spec sections 3.2/3.3).

The two attribution-map tables (``codex_file_incarnations``,
``codex_file_accounts``) are created by ``_apply_cache_schema`` in its
unconditional executescript — the repo's table-addition rule, and specifically
before the FTS5 ``legacy_present`` early-return so a legacy-shape cache still
receives them. Migration 031 exists because that schema apply is VERSION-GATED:
``_cctally_store.schema_current`` compares ``PRAGMA user_version`` against
``len(_CACHE_MIGRATIONS)`` and a steady-state open skips the entire DDL pass
when they match. Registering 031 bumps the head, so an install already at the
030 head re-runs the schema apply and gains the tables.

The ``pre.sqlite`` golden therefore reproduces the real pre-#416 on-disk shape:
full production schema, ``schema_migrations`` at cache 001-030, and the two
tables absent. There is deliberately NO backfill (spec D1: history that was
never durably stamped becomes ``unattributed``; nothing is inferred).
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): this module exercises the handler's
# second-invocation idempotency.
IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "031_codex_file_account_map"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "031_codex_file_account_map"
_TABLES = ("codex_file_incarnations", "codex_file_accounts")


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _tables(conn) -> set:
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_pre_fixture_at_030_head_without_the_map(cctally_module):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='030_session_entries_cache_creation_split'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 0
        present = _tables(conn)
        for table in _TABLES:
            assert table not in present, (
                f"{table} must be ABSENT in pre.sqlite — otherwise the golden "
                "does not exercise the handler at all")
    finally:
        conn.close()


def test_post_fixture_has_the_empty_map(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        present = _tables(conn)
        for table in _TABLES:
            assert table in present
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, (
                "spec D1: nothing is inferred, so the map starts EMPTY")
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (_MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_creates_the_map(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        present = _tables(conn)
        for table in _TABLES:
            assert table in present
    finally:
        conn.close()


def test_map_shape_matches_the_attribution_contract(cctally_module, tmp_path):
    """``account_key`` must be NULLABLE: NULL is the stably-absent SENTINEL
    decision, and the string ``"unattributed"`` is never stored (the two-shaped
    stamp rule). A NOT NULL column here would force the sentinel to be written
    as a literal and break the sentinel/undecided distinction."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _handler(cctally_module)(conn)
        cols = {
            row[1]: row for row in
            conn.execute("PRAGMA table_info(codex_file_accounts)")
        }
        assert set(cols) == {
            "file_identity", "incarnation", "from_offset", "root_scope",
            "account_key", "decided_at_utc"}
        assert cols["account_key"][3] == 0, "account_key must be nullable"
        # The primary key is the incarnation-qualified interval start.
        pk = {name for name, row in cols.items() if row[5]}
        assert pk == {"file_identity", "incarnation", "from_offset"}
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        conn.execute(
            "INSERT INTO codex_file_accounts (file_identity, incarnation, "
            "from_offset, root_scope, account_key, decided_at_utc) "
            "VALUES ('fid', 1, 0, 'rk', NULL, '2026-07-28T00:00:00Z')")
        conn.commit()
        handler(conn)  # second run must not raise and must not wipe the map
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_file_accounts").fetchone()[0] == 1
    finally:
        conn.close()
