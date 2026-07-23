"""Per-migration goldens for conversations migration
``001_adopt_schema_version_marker`` (DB journal redesign spec §7.2).

Adoption, not migration: the handler brings an existing populated
conversations.db under the framework WITHOUT touching its transcript data — it
re-asserts the ``conversation_schema_version='1'`` marker (an idempotent
upsert), and the dispatcher stamps the ``schema_migrations`` row. The physical
schema is owned by ``_apply_conversations_schema`` (run before the dispatcher in
``open_conversations_db``), so this handler never re-creates it.

pre.sqlite  = conversations schema + one ``conversation_source_files`` row +
              the marker='1', with an EMPTY ``schema_migrations`` ledger.
post.sqlite = the row UNCHANGED, marker still '1', + the 001 marker stamped.
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
    / "conversations_001_adopt_schema_version_marker"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

MIGRATION = "001_adopt_schema_version_marker"
MARKER_KEY = "conversation_schema_version"


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
    for m in cctally_module._CONVERSATIONS_MIGRATIONS:
        if m.name == MIGRATION:
            return m.handler
    raise AssertionError(f"conversations migration {MIGRATION} not registered")


def _source_files(conn):
    return conn.execute(
        "SELECT path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at "
        "FROM conversation_source_files ORDER BY path"
    ).fetchall()


def test_pre_fixture_lacks_marker(cctally_module):
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert len(_source_files(conn)) == 1
        # The conversation schema marker is already present (pre-framework DB).
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (MARKER_KEY,)
        ).fetchone() == ("1",)
        # But the framework ledger row is NOT yet stamped.
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_post_fixture_has_marker_row_unchanged(cctally_module):
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    pre_conn = sqlite3.connect(PRE_DB)
    try:
        pre_rows = _source_files(pre_conn)
    finally:
        pre_conn.close()
    conn = sqlite3.connect(POST_DB)
    try:
        assert _source_files(conn) == pre_rows, "adopt handler must not touch data"
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (MARKER_KEY,)
        ).fetchone() == ("1",)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_handler_adopts_and_is_idempotent(cctally_module, tmp_path):
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        pre_rows = _source_files(conn)
        handler = _handler(cctally_module)
        handler(conn)
        cctally_module._stamp_applied(conn, MIGRATION)
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?", (MARKER_KEY,)
        ).fetchone() == ("1",)
        assert _source_files(conn) == pre_rows
        # Second invocation on its own output — idempotent upsert, no raise.
        handler(conn)
        cctally_module._stamp_applied(conn, MIGRATION)
        assert _source_files(conn) == pre_rows
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        conn.close()
