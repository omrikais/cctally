"""Per-migration goldens for #968's conversation container evidence."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "012_codex_conversation_containers"
TABLE = "codex_conversation_source_files"
COLUMNS = (
    "container_kind", "container_path", "container_size_bytes",
    "decoded_size_bytes", "content_anchor_version", "content_anchor",
    "content_anchor_offset", "container_state", "container_state_reason",
    "pending_container_fingerprint",
)
TABLES = ("codex_conversation_prune_debt", "codex_conversation_publications",
          "codex_conversation_published_threads")
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / ("conversations_" + MIGRATION)
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
PARENT_INDEX = "idx_codex_published_threads_parent"
PARENT_COLUMNS = ["source_root_key", "native_thread_id", "conversation_key"]


@pytest.fixture
def db():
    load_script()
    import _cctally_db
    return _cctally_db


def _handler(db):
    return next(item.handler for item in db._CONVERSATIONS_MIGRATIONS
                if item.name == MIGRATION)


def _copy(path, tmp_path):
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def _columns(conn):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _legacy_row(conn):
    return conn.execute(
        "SELECT container_kind, container_state, container_path, "
        "container_size_bytes, decoded_size_bytes, content_anchor, "
        "content_anchor_offset, pending_container_fingerprint FROM " + TABLE).fetchall()


def test_pre_fixture_is_an_011_head_without_container_state(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
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
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
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
        assert _legacy_row(conn) == [("plain", "accepted", None, None, None, None, None, None)]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_takes_pre_to_post_shape_and_is_idempotent(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        for _ in range(2):
            _handler(db)(conn)
            assert set(COLUMNS) <= _columns(conn)
            assert set(TABLES) <= _tables(conn)
            assert [row[2] for row in conn.execute(
                f"PRAGMA index_info({PARENT_INDEX})")] == PARENT_COLUMNS
            assert _legacy_row(conn) == [("plain", "accepted", None, None, None, None, None, None)]
    finally:
        conn.close()


def test_a_fresh_conversations_store_holds_no_cache_only_968_table(db):
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        db._apply_conversations_schema(conn)  # Exercise its marker-current fast path.
        assert set(TABLES) <= _tables(conn)
        assert [row[2] for row in conn.execute(
            f"PRAGMA index_info({PARENT_INDEX})")] == PARENT_COLUMNS
        assert not _tables(conn) & set(db._CODEX_968_CACHE_TABLES)
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
