"""Per-migration goldens for #1068's rollup input-fingerprint column.

Stored-data failure detected: an existing conversations store that never
receives ``input_fingerprint`` (its schema apply returns early on the current
marker), or a delivery that invents a fingerprint for a row derived before the
column existed. Either would let the scoped Viewer reuse an unverified row.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


IDEMPOTENCY_COVERED = True
MIGRATION = "013_conversation_sessions_input_fingerprint"
TABLES = ("conversation_sessions", "conversation_sessions_staging")
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / ("conversations_" + MIGRATION)
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


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


def _has_column(conn, table):
    return "input_fingerprint" in {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_pre_fixture_is_a_012_head_without_the_column(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        for table in TABLES:
            assert not _has_column(conn, table)
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_has_the_column_on_both_tables_with_null_rows(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        for table in TABLES:
            assert _has_column(conn, table)
            assert conn.execute(
                f"SELECT session_id, input_fingerprint FROM {table}"
            ).fetchall() == [(f"golden-{table}", None)]
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
            for table in TABLES:
                assert _has_column(conn, table)
                assert conn.execute(
                    f"SELECT msg_count, cost_usd, title, input_fingerprint "
                    f"FROM {table}"
                ).fetchall() == [(3, 1.25, "Golden session", None)]
    finally:
        conn.close()


def test_a_fresh_conversations_store_creates_the_column(db):
    """A fresh install stamps 013 without running it, so the schema apply
    itself must create the column on both tables."""
    conn = sqlite3.connect(":memory:")
    try:
        db._apply_conversations_schema(conn)
        for table in TABLES:
            assert _has_column(conn, table)
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
