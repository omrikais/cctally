"""Per-migration goldens for #582's Codex accounting dirty-path ledger."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "044_codex_accounting_change_ledger"
TABLE = "codex_accounting_change_log"
INDEX = "idx_codex_accounting_change_mutation"
TRIGGERS = {
    "trg_codex_accounting_ins",
    "trg_codex_accounting_del",
    "trg_codex_accounting_upd",
    "trg_codex_accounting_thread_ins",
    "trg_codex_accounting_thread_del",
    "trg_codex_accounting_thread_upd",
}
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


@pytest.fixture
def db():
    load_script()
    import _cctally_db
    return _cctally_db


def _handler(db):
    return next(item.handler for item in db._CACHE_MIGRATIONS
                if item.name == MIGRATION)


def _objects(conn):
    return tuple(conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name=? OR name=? OR name IN (?,?,?,?,?,?) ORDER BY type, name",
        (TABLE, INDEX, *sorted(TRIGGERS)),
    ))


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def test_pre_fixture_is_a_043_head_without_the_ledger(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 43
        assert _objects(conn) == ()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_carries_empty_ledger_and_triggers(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 44
        objects = _objects(conn)
        assert {row[1] for row in objects} == {TABLE, INDEX, *TRIGGERS}
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'"
        ).fetchone() == ("0",)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_takes_pre_fixture_to_post_shape_and_is_idempotent(db, tmp_path):
    target = _copy(PRE_DB, tmp_path)
    conn = sqlite3.connect(target)
    try:
        handler = _handler(db)
        handler(conn)
        first = _objects(conn)
        handler(conn)
        assert _objects(conn) == first
        assert {row[1] for row in first} == {TABLE, INDEX, *TRIGGERS}
    finally:
        conn.close()
