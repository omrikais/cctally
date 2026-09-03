"""Per-migration goldens for #682's render_revision column delivery."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "045_conversation_render_revision_columns"
COLUMN = "render_revision"
TABLES = ("codex_conversation_rollups", "conversation_sessions")
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


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def _has_column(conn: sqlite3.Connection, table: str) -> bool:
    return any(row[1] == COLUMN
               for row in conn.execute(f"PRAGMA table_info({table})"))


def test_pre_fixture_is_a_044_head_without_the_columns(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 44
        for table in TABLES:
            assert not _has_column(conn, table)
            assert conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_carries_both_columns_over_existing_rows(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 45
        for table in TABLES:
            assert _has_column(conn, table)
            # The pre-existing row keeps its identity and takes the DEFAULT,
            # which is already the value that marks it as needing re-assembly.
            assert conn.execute(
                f"SELECT {COLUMN} FROM {table}").fetchall() == [(0,)]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_takes_pre_fixture_to_post_shape_and_is_idempotent(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        handler = _handler(db)
        handler(conn)
        first = {
            table: conn.execute(
                f"SELECT {COLUMN} FROM {table}").fetchall()
            for table in TABLES
        }
        handler(conn)
        for table in TABLES:
            assert _has_column(conn, table)
            assert conn.execute(
                f"SELECT {COLUMN} FROM {table}").fetchall() == first[table]
    finally:
        conn.close()


def test_handler_does_not_reset_a_revision_that_already_advanced(db, tmp_path):
    """Re-running must not undo assembly-frontier progress.

    ``add_column_if_missing`` is a no-op once the column exists, so a store
    whose rollup recompute has already advanced the frontier keeps its value.
    A handler written as an unconditional ALTER-or-UPDATE would fail here.
    """
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        for table in TABLES:
            conn.execute(f"UPDATE {table} SET {COLUMN}=7")
        conn.commit()
        _handler(db)(conn)
        for table in TABLES:
            assert conn.execute(
                f"SELECT {COLUMN} FROM {table}").fetchall() == [(7,)]
    finally:
        conn.close()


def test_handler_skips_a_store_whose_transcript_tables_are_absent(db, tmp_path):
    """A missing table is an ordinary state, not a migration failure.

    Cache migration 028 removes the legacy transcript objects and
    ``open_cache_db`` recreates them only afterwards, so the dispatcher can
    reach this handler while neither table exists. ``add_column_if_missing``
    raises on a missing table, and an unguarded ALTER here put
    ``[migration cache.db:045_...] failed: no such table: conversation_sessions``
    into the migrations harness's unrelated failure-banner scenario.
    """
    target = tmp_path / "no-transcript-tables.sqlite"
    conn = sqlite3.connect(target)
    try:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        _handler(db)(conn)
        remaining = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert remaining.isdisjoint(TABLES), (
            "the handler must skip the tables, not create them — the schema "
            "apply owns their CREATE TABLE and it already carries the column"
        )
    finally:
        conn.close()


def test_handler_still_delivers_when_only_one_table_is_present(db, tmp_path):
    """The two guards are independent, so a half-present store is not skipped."""
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        conn.execute("DROP TABLE codex_conversation_rollups")
        conn.commit()
        _handler(db)(conn)
        assert _has_column(conn, "conversation_sessions")
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
