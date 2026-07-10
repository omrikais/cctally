"""Per-migration goldens for cache migration ``001_dedup_highest_wins``.

Loads ``tests/fixtures/migrations/per-migration/001_dedup_highest_wins/pre.sqlite``,
runs the migration handler against a copy, and asserts the result matches
``post.sqlite`` (modulo the marker's ``applied_at_utc`` timestamp which is
``now_utc_iso()`` at run time).

Verifies:

  * Both seeded ``session_entries`` rows are deleted (3 -> 0).
  * Both seeded ``session_files`` rows are deleted (2 -> 0).
  * The ``001_dedup_highest_wins`` marker is stamped into
    ``schema_migrations``.
  * The schema (CREATE TABLE statements) for ``session_entries`` and
    ``session_files`` is preserved byte-identically across the migration.

Per-migration goldens are lazy-adopted (CLAUDE.md gotcha "lazy-adopted;
not retroactively backfilled"); 001 is the second to ship them
(005 / 006 shipped first).
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): declares this module exercises
# the handler's second-invocation idempotency (test names vary across modules).
IDEMPOTENCY_COVERED = True


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "001_dedup_highest_wins"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture(scope="module")
def db_module():
    """Load bin/_cctally_db.py once per module.

    Pre-Task 2 the migration didn't exist; with this fixture in place the
    test imports the production handler directly and exercises it against
    a copy of the pre.sqlite golden.
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _migration_handler(db_module):
    for m in db_module._CACHE_MIGRATIONS:
        if m.name == "001_dedup_highest_wins":
            return m.handler
    raise AssertionError("cache migration 001_dedup_highest_wins not registered")


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row[0] if row else ""


def test_pre_fixture_has_loser_rows(db_module):
    """Sanity: pre.sqlite has 3 session_entries + 2 session_files + empty
    markers + a STALE cache_meta walk-complete marker (cctally-dev#93)."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM session_files"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0] == 0
        # cctally-dev#93: a stale walk-complete marker that 001 must clear.
        marker = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
        ).fetchone()
        assert marker is not None, (
            "pre.sqlite must seed a stale cache_meta walk-complete marker "
            "so post.sqlite can demonstrate 001's atomic marker-clear"
        )
    finally:
        conn.close()


def test_post_fixture_has_empty_tables_and_marker(db_module):
    """Sanity: post.sqlite has empty entries/files tables, the 001 marker
    row, and the cache_meta walk-complete marker CLEARED (cctally-dev#93
    D5: 001 DELETEs it atomically with the wipe)."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_files"
        ).fetchone()[0] == 0
        marker = conn.execute(
            "SELECT name FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()
        assert marker is not None, "post.sqlite missing 001 marker"
        # cctally-dev#93: the walk-complete sentinel must be cleared.
        walk = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
        ).fetchone()
        assert walk is None, (
            "post.sqlite must show the walk-complete marker CLEARED — a "
            "wiped session_entries must never coexist with a complete-walk "
            "marker (D5/D2)"
        )
    finally:
        conn.close()


def test_migration_handler_wipes_tables_and_stamps_marker(db_module, tmp_path):
    """Run handler on a fresh copy of pre.sqlite; verify it matches post.sqlite."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        # Sanity: pre-handler state.
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM session_files"
        ).fetchone()[0] == 2

        _migration_handler(db_module)(conn)

        # Both seeded tables wiped.
        assert conn.execute(
            "SELECT COUNT(*) FROM session_entries"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_files"
        ).fetchone()[0] == 0

        # Marker stamped exactly once.
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='001_dedup_highest_wins'"
        ).fetchone()[0]
        assert cnt == 1

        # cctally-dev#93 D5: the walk-complete sentinel is cleared
        # atomically with the wipe (pre.sqlite seeds a stale one).
        walk = conn.execute(
            "SELECT 1 FROM cache_meta WHERE key='claude_ingest_walk_complete'"
        ).fetchone()
        assert walk is None, (
            "001 must clear the cache_meta walk-complete marker in the "
            "same transaction as the wipe"
        )

        # applied_at_utc is non-empty (now_utc_iso()); we don't assert its
        # exact value (it's wall-clock at handler time).
        applied_at = conn.execute(
            "SELECT applied_at_utc FROM schema_migrations "
            "WHERE name='001_dedup_highest_wins'"
        ).fetchone()[0]
        assert applied_at, "applied_at_utc must be non-empty"

        # Schema for session_entries / session_files is preserved.
        post_conn = sqlite3.connect(POST_DB)
        try:
            assert _table_sql(conn, "session_entries") == _table_sql(
                post_conn, "session_entries"
            )
            assert _table_sql(conn, "session_files") == _table_sql(
                post_conn, "session_files"
            )
        finally:
            post_conn.close()
    finally:
        conn.close()


def test_migration_handler_idempotent_against_marker(db_module, tmp_path):
    """A second invocation re-runs the body but the marker INSERT now
    uses ``INSERT OR IGNORE`` (D3 fix) so it silently no-ops on the
    second run instead of raising ``IntegrityError``.

    Under concurrent dispatcher invocations (dashboard + CLI on the
    same cache.db), the prior plain ``INSERT`` would cause the loser to
    surface as a migration-error banner. The dispatcher's ``applied``
    set still provides per-process idempotency; the OR IGNORE is
    cross-process race safety.
    """
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _migration_handler(db_module)(conn)
        # Pre-fix this raised sqlite3.IntegrityError; post-fix it returns clean.
        _migration_handler(db_module)(conn)
        # Marker still present exactly once.
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name = '001_dedup_highest_wins'"
        ).fetchone()[0]
        assert cnt == 1, "marker must remain exactly one row after re-run"
    finally:
        conn.close()
