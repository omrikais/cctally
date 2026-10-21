"""Per-migration goldens for #968's container evidence and maintenance state."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "049_codex_rollout_containers"
TABLE = "codex_session_files"
COLUMNS = (
    "container_kind", "container_path", "container_size_bytes",
    "decoded_size_bytes", "content_anchor_version", "content_anchor",
    "content_anchor_offset", "container_state", "container_state_reason",
    "pending_container_fingerprint", "container_resume_strikes",
)
TABLES = (
    "codex_prune_debt", "codex_barrier_membership", "codex_barrier_targets",
    "codex_barrier_population", "codex_barrier_certificates",
    "codex_barrier_repairs", "codex_barrier_entry_threads",
)
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
PARENT_INDEX = "idx_codex_barrier_entry_threads_parent"
PARENT_COLUMNS = ["source_root_key", "native_thread_id", "conversation_key"]


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


def _columns(conn) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def _tables(conn) -> set:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _legacy_row(conn):
    return conn.execute(
        "SELECT container_kind, container_state, container_path, "
        "container_size_bytes, decoded_size_bytes, content_anchor, "
        "content_anchor_offset, pending_container_fingerprint, "
        "container_resume_strikes FROM codex_session_files").fetchall()


def test_pre_fixture_is_a_048_head_without_container_state(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 48
        assert not _columns(conn) & set(COLUMNS)
        assert not _tables(conn) & set(TABLES)
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
        assert conn.execute(
            f"SELECT path, size_bytes, last_byte_offset, device_id, inode FROM {TABLE}"
        ).fetchall() == [(
            "/fixtures/codex/sessions/2026/10/14/rollout.jsonl",
            512, 512, None, None,
        )]
    finally:
        conn.close()


def test_post_fixture_reads_the_existing_cursor_as_plain_and_accepted(tmp_path):
    """An existing row describes a plain file whose content was accepted.

    No evidence column is invented: path, sizes and the anchor stay NULL until
    the next ingest observes them together with the offset.
    """
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
        assert set(COLUMNS) <= _columns(conn)
        assert set(TABLES) <= _tables(conn)
        assert [row[2] for row in conn.execute(
            f"PRAGMA index_info({PARENT_INDEX})")] == PARENT_COLUMNS
        assert conn.execute(
            f"SELECT path, size_bytes, last_byte_offset, device_id, inode FROM {TABLE}"
        ).fetchall() == [(
            "/fixtures/codex/sessions/2026/10/14/rollout.jsonl",
            512, 512, None, None,
        )]
        assert conn.execute(
            f"SELECT content_anchor_version, container_state_reason FROM {TABLE}"
        ).fetchall() == [(None, None)]
        assert _legacy_row(conn) == [
            ("plain", "accepted", None, None, None, None, None, None, 0)]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_takes_pre_to_post_shape_and_is_idempotent(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        handler = _handler(db)
        for _ in range(2):
            handler(conn)
            assert set(COLUMNS) <= _columns(conn)
            assert set(TABLES) <= _tables(conn)
            assert [row[2] for row in conn.execute(
                f"PRAGMA index_info({PARENT_INDEX})")] == PARENT_COLUMNS
            assert _legacy_row(conn) == [
                ("plain", "accepted", None, None, None, None, None, None, 0)]
    finally:
        conn.close()


def test_handler_on_a_store_without_the_cursor_table_creates_only_tables(
        db, tmp_path):
    conn = sqlite3.connect(tmp_path / "no-table.sqlite")
    try:
        _handler(db)(conn)
        assert set(TABLES) <= _tables(conn)
        assert [row[2] for row in conn.execute(
            f"PRAGMA index_info({PARENT_INDEX})")] == PARENT_COLUMNS
        assert TABLE not in _tables(conn)
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
