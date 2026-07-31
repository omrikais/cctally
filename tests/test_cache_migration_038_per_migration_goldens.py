"""Per-migration goldens for ``codex_session_files.ingest_complete``.

Public issue omrikais/cctally#5. The column exists because
``_write_codex_file_batch`` persists the file's full observed ``st_size``
alongside whatever offset ingestion actually reached, and the delta detector
skips on ``size == prev_size`` without consulting that offset. Committing a
budgeted mid-file stop under today's representation would make the unread
suffix permanently invisible on any rollout that never grows again.

``DEFAULT 1`` is the load-bearing part. Every row that existed before the
budgeted ingest WAS scanned to its stored target, so reading them as complete
preserves today's behaviour exactly; a row that read 0 would be a file re-scanned
from its stored offset for no reason.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "038_codex_session_files_ingest_complete"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "038_codex_session_files_ingest_complete"
COLUMN = "ingest_complete"


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
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _columns(conn) -> dict:
    return {
        str(row[1]): row for row in conn.execute(
            "PRAGMA table_info(codex_session_files)")
    }


def _cursors(conn):
    return list(conn.execute(
        "SELECT path, size_bytes, mtime_ns, last_byte_offset, "
        "       last_ingested_at, source_root_key "
        "  FROM codex_session_files ORDER BY path"))


def test_pre_fixture_is_a_037_head_install_without_the_column(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 37
        assert COLUMN not in _columns(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        # Non-vacuity: a default over zero rows would prove nothing.
        assert len(_cursors(conn)) == 2
    finally:
        conn.close()


def test_every_pre_existing_row_reads_complete(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
        column = _columns(conn)[COLUMN]
        assert column[2] == "INTEGER"
        assert column[3] == 1, "the column must be NOT NULL"
        assert str(column[4]) == "1", "the default must be 1, not 0 or NULL"
        assert [
            row[0] for row in conn.execute(
                f"SELECT {COLUMN} FROM codex_session_files ORDER BY path")
        ] == [1, 1]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_no_cursor_is_touched(cctally_module):
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert _cursors(post) == _cursors(pre)
    finally:
        pre.close()
        post.close()


def test_a_fresh_schema_carries_the_same_default(cctally_module, tmp_path):
    """A fresh install fast-stamps every migration handler without running it,
    so ``_apply_cache_schema`` has to produce the identical column."""
    import _cctally_db

    work = tmp_path / "fresh.sqlite"
    conn = sqlite3.connect(work)
    try:
        _cctally_db._apply_cache_schema(conn)
        conn.execute(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
            "VALUES ('/roots/rk/sessions/c.jsonl', 1, 1, 1, '2026-07-31T12:00:00Z')"
        )
        conn.commit()
        assert conn.execute(
            f"SELECT {COLUMN} FROM codex_session_files").fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        # A partial stop the resumable ingest would write; a second handler run
        # must not "heal" it back to 1.
        conn.execute(
            f"UPDATE codex_session_files SET {COLUMN}=0 "
            "WHERE path='/roots/rk/sessions/a.jsonl'")
        conn.commit()
        first = (sorted(_columns(conn)), _cursors(conn), list(conn.execute(
            f"SELECT path, {COLUMN} FROM codex_session_files ORDER BY path")))
        handler(conn)
        assert (sorted(_columns(conn)), _cursors(conn), list(conn.execute(
            f"SELECT path, {COLUMN} FROM codex_session_files ORDER BY path"
        ))) == first
    finally:
        conn.close()
