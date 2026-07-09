"""Per-migration goldens for cache migration ``020_session_entries_physical_unique``
(#279 S3 F3 — the physical-key UNIQUE backstop on session_entries).

Loads ``tests/fixtures/migrations/per-migration/020_session_entries_physical_unique/pre.sqlite``
(an existing install at the 019 head with the ``idx_entries_physical`` index
absent and physical-key duplicates seeded), runs the production 020 handler
against a copy, and asserts keep-first-id dedup + index creation. The committed
``post.sqlite`` is the golden.

020 does DATA work (unlike the flag-only 018/019): it collapses each
``(source_path, line_offset)`` duplicate group down to its ``MIN(id)`` keeper and
(re)creates the UNIQUE ``idx_entries_physical`` index. The dispatcher
central-stamps the marker (#140); the handler does NOT self-stamp.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "020_session_entries_physical_unique"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "020_session_entries_physical_unique"
_INDEX = "idx_entries_physical"


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


def _migration_handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _physical_dupe_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM (SELECT source_path, line_offset "
        "FROM session_entries GROUP BY source_path, line_offset "
        "HAVING COUNT(*) > 1)"
    ).fetchone()[0]


def _marker_count(conn, name):
    return conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (name,)
    ).fetchone()[0]


def test_pre_fixture_at_019_head_with_dupes_and_no_index(cctally_module):
    """Sanity: pre.sqlite has 019 applied, NOT the 020 marker, carries
    physical-key duplicates, and LACKS the idx_entries_physical index."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert _marker_count(conn, "019_create_conversation_file_touches") == 1
        assert _marker_count(conn, _MIGRATION) == 0
        assert _INDEX not in _indexes(conn), "pre must lack the physical index"
        assert _physical_dupe_count(conn) == 2, (
            "pre must carry the seeded duplicate groups"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries").fetchone()[0] == 5
    finally:
        conn.close()


def test_post_fixture_deduped_indexed_and_stamped(cctally_module):
    """Sanity: post.sqlite has no physical dupes, the UNIQUE index present, the
    020 marker stamped, and exactly the MIN(id) keepers survived."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _marker_count(conn, _MIGRATION) == 1
        assert _INDEX in _indexes(conn)
        assert _physical_dupe_count(conn) == 0
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM session_entries ORDER BY id")]
        # keepers are the MIN(id) of each physical group: 1, 3, 5.
        assert ids == [1, 3, 5]
    finally:
        conn.close()


def test_handler_dedups_keep_first_id_and_creates_index(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite: it must collapse each
    physical-key group to MIN(id), create the UNIQUE index, and (with the
    dispatcher's central stamp reproduced) mark 020 applied."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _INDEX not in _indexes(conn)
        assert _physical_dupe_count(conn) == 2

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _INDEX in _indexes(conn)
        assert _physical_dupe_count(conn) == 0
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM session_entries ORDER BY id")]
        assert ids == [1, 3, 5], "keep-first-id must retain the MIN(id) rows"
        assert _marker_count(conn, _MIGRATION) == 1
    finally:
        conn.close()


def test_handler_is_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run on the deduped post state must be a no-op (no rows
    removed, index still present) and must not raise."""
    work = tmp_path / "cache.db"
    shutil.copy(POST_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)  # must not raise, must leave the deduped state intact
        assert _physical_dupe_count(conn) == 0
        assert _INDEX in _indexes(conn)
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM session_entries ORDER BY id")]
        assert ids == [1, 3, 5]
    finally:
        conn.close()
