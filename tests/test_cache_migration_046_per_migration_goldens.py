"""Per-migration goldens for #769 S6's Codex accounting file identity."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "046_codex_source_file_identity"
TABLE = "codex_session_files"
COLUMNS = ("device_id", "inode")
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


def _columns(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def test_pre_fixture_is_a_045_head_without_the_identity_columns(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 45
        assert not _columns(conn) & set(COLUMNS)
        assert conn.execute(
            f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_carries_both_columns_at_null_over_the_existing_row(
    tmp_path,
):
    """NULL is the point, not an oversight.

    The identity means "the pass that wrote this offset read this inode". The
    migration cannot know that, so it must not invent it; the value arrives on
    the next ingest of the path, where the two are observed together.
    """
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 46
        assert set(COLUMNS) <= _columns(conn)
        assert conn.execute(
            f"SELECT device_id, inode FROM {TABLE}").fetchall() == [(None, None)]
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
        assert set(COLUMNS) <= _columns(conn)
        assert conn.execute(
            f"SELECT device_id, inode FROM {TABLE}").fetchall() == [(None, None)]
        handler(conn)
        assert set(COLUMNS) <= _columns(conn)
        assert conn.execute(
            f"SELECT device_id, inode FROM {TABLE}").fetchall() == [(None, None)]
    finally:
        conn.close()


def test_handler_skips_a_store_without_the_table(db, tmp_path):
    """An absent table is a reachable state, not a corrupt one."""
    path = tmp_path / "no-table.sqlite"
    conn = sqlite3.connect(path)
    try:
        _handler(db)(conn)
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
