"""Per-migration goldens for #901 Q11's latest-metadata indexes
(conversations migration 011).

Spec ``docs/superpowers/specs/2026-10-03-901-dashboard-disk-writes.md`` §5.3a
(901-PA-001 b). Two partial indexes on ``conversation_messages(session_id,
timestamp_utc, id)``, one ``WHERE cwd IS NOT NULL`` and one ``WHERE git_branch
IS NOT NULL``, serve ``_session_latest_meta_map``'s two ``LIMIT 1`` lookups per
session with a single seek each.

``pre.sqlite`` is a genuine 010-head conversations store without them, holding
one session whose metadata is sparse; ``post.sqlite`` is that store after the
handler, every row untouched.
"""
from __future__ import annotations

import fcntl
import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script

IDEMPOTENCY_COVERED = True
MIGRATION = "011_conversation_latest_meta_indexes"
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations"
    / "per-migration" / f"conversations_{MIGRATION}"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
INDEXES = {
    "idx_conv_session_latest_cwd": "cwd",
    "idx_conv_session_latest_git_branch": "git_branch",
}


@pytest.fixture
def db():
    load_script()
    import _cctally_db
    return _cctally_db


def _handler(db):
    return next(item.handler for item in db._CONVERSATIONS_MIGRATIONS
                if item.name == MIGRATION)


def _copy(path: Path, tmp_path: Path) -> Path:
    target = tmp_path / path.name
    shutil.copyfile(path, target)
    return target


def _indexes(conn):
    return {
        name: " ".join(sql.split()) for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'"
            " AND name IN (?, ?)", tuple(INDEXES))
    }


def _expected():
    return {
        name: (f"CREATE INDEX {name} ON conversation_messages(session_id,"
               f" timestamp_utc, id) WHERE {column} IS NOT NULL")
        for name, column in INDEXES.items()
    }


def _rows(conn):
    return list(conn.execute(
        "SELECT id, session_id, timestamp_utc, cwd, git_branch"
        " FROM conversation_messages ORDER BY id"))


def test_pre_fixture_is_a_010_head_store_without_the_indexes(tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert _indexes(conn) == {}
        assert len(_rows(conn)) >= 6
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_carries_both_indexes_and_the_marker(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert _indexes(conn) == _expected()
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_each_lookup_seeks_its_index_and_reads_the_latest_value(tmp_path):
    conn = sqlite3.connect(_copy(POST_DB, tmp_path))
    try:
        session = conn.execute(
            "SELECT session_id FROM conversation_messages LIMIT 1").fetchone()[0]
        for name, column in INDEXES.items():
            sql = (f"SELECT {column} FROM conversation_messages WHERE"
                   f" session_id = ? AND {column} IS NOT NULL ORDER BY"
                   " timestamp_utc DESC, id DESC LIMIT 1")
            plan = [str(r[3]) for r in conn.execute(
                "EXPLAIN QUERY PLAN " + sql, (session,))]
            assert plan == [
                f"SEARCH conversation_messages USING INDEX {name}"
                " (session_id=?)"], plan
        assert [conn.execute(
            f"SELECT {column} FROM conversation_messages WHERE session_id = ?"
            f" AND {column} IS NOT NULL ORDER BY timestamp_utc DESC, id DESC"
            " LIMIT 1", (session,)).fetchone()[0]
            for column in INDEXES.values()] == ["/early", "branch-mid"]
    finally:
        conn.close()


def test_no_row_is_touched(tmp_path):
    pre = sqlite3.connect(_copy(PRE_DB, tmp_path))
    post_path = tmp_path / "post-copy.sqlite"
    shutil.copyfile(POST_DB, post_path)
    post = sqlite3.connect(post_path)
    try:
        assert _rows(post) == _rows(pre)
    finally:
        pre.close()
        post.close()


def test_handler_is_idempotent(db, tmp_path):
    conn = sqlite3.connect(_copy(PRE_DB, tmp_path))
    try:
        _handler(db)(conn)
        first = (_indexes(conn), _rows(conn))
        assert first[0] == _expected()
        _handler(db)(conn)
        assert (_indexes(conn), _rows(conn)) == first
    finally:
        conn.close()


def test_handler_defers_before_any_ddl_when_claude_ingest_holds_its_flock(
    db, tmp_path,
):
    handler = _handler(db)
    gate_exc = handler.__globals__["MigrationGateNotMet"]
    target = _copy(PRE_DB, tmp_path)
    conn = sqlite3.connect(target)
    try:
        with open(Path(str(target) + ".lock"), "w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with pytest.raises(gate_exc, match="011"):
                handler(conn)
        assert _indexes(conn) == {}, "deferral precedes any DDL"
        handler(conn)
        assert _indexes(conn) == _expected()
    finally:
        conn.close()


def test_handler_skips_a_store_without_the_table(db, tmp_path):
    """An absent table is a reachable state, not a corrupt one."""
    conn = sqlite3.connect(tmp_path / "no-table.sqlite")
    try:
        _handler(db)(conn)
        assert _indexes(conn) == {}
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
