"""#566: the index must reach a store already stamped at the old head.

`d1f14fad3` added `idx_codex_entries_root_path` to `_apply_cache_schema`,
which `open_cache_db` runs only when `user_version != len(_CACHE_MIGRATIONS)`.
Every existing install was already current, so the index never arrived. The
maintainer's live install, running the release that contained the commit,
measured `user_version = 41`, `_expected_head("cache") = 41` and the index
absent from `~/.local/share/cctally/cache.db`.

Registering the migration is what makes an already-current store pick it up,
and the shared ensure-helper is what stops the two delivery paths drifting.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script

IDEMPOTENCY_COVERED = True

INDEX = "idx_codex_entries_root_path"
MIGRATION = "042_codex_entries_root_path_index"
HELPER = "_apply_codex_entries_root_path_index"

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration" / MIGRATION
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


@pytest.fixture
def db():
    """The `_cctally_db` instance the freshly-exec'd `bin/cctally` bound.

    `load_script()` drops and re-imports every `_cctally_*` sibling, so the
    import has to happen AFTER it or the test would hold a stale module (the
    identity trap documented in `tests/test_prod_migration_guard.py`).
    """
    load_script()
    import _cctally_db

    return _cctally_db


def _index_sql(conn: sqlite3.Connection) -> "str | None":
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
    ).fetchone()
    return None if row is None else str(row[0])


def _index_present(conn: sqlite3.Connection) -> bool:
    return _index_sql(conn) is not None


def _registered_handler(db):
    for migration in db._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} is not registered in _CACHE_MIGRATIONS")


def _store_at_old_head(db, path) -> sqlite3.Connection:
    """A cache-shaped store with the schema applied and the index removed."""
    conn = sqlite3.connect(path)
    db._apply_cache_schema(conn)
    conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
    conn.commit()
    assert not _index_present(conn)
    return conn


def _scratch_copy(golden: Path, tmp_path) -> Path:
    """Copy a committed golden into the scratch dir before opening it.

    `sqlite3.connect` opens read-write. On a WAL fixture that lets SQLite
    checkpoint the committed bytes and leave `-wal`/`-shm` sidecars beside the
    tracked file, so a read-only assertion could dirty the working tree.
    `mode=ro` is NOT the fix: a read-only open of a WAL database still needs the
    `-shm` file and fails outright when it is absent.
    """
    target = tmp_path / golden.name
    shutil.copyfile(golden, target)
    return target


def test_migration_creates_the_index_on_a_store_stamped_at_the_old_head(db, tmp_path):
    conn = _store_at_old_head(db, tmp_path / "cache.db")
    try:
        _registered_handler(db)(conn)
        assert _index_present(conn)
    finally:
        conn.close()


def test_migration_handler_idempotent_against_marker(db, tmp_path):
    conn = _store_at_old_head(db, tmp_path / "cache.db")
    try:
        handler = _registered_handler(db)
        handler(conn)
        first = _index_sql(conn)
        handler(conn)
        assert _index_sql(conn) == first
    finally:
        conn.close()


def test_schema_apply_and_migration_share_one_helper(db, tmp_path):
    """The two delivery paths must not drift (the 040/041 precedent)."""
    conn = _store_at_old_head(db, tmp_path / "cache.db")
    try:
        getattr(db, HELPER)(conn)
        assert _index_present(conn)
    finally:
        conn.close()


def test_the_schema_apply_delegates_to_the_helper(db):
    """`_apply_cache_schema` must call the helper rather than inline the DDL.

    An inline copy is the drift the helper exists to prevent: the schema body
    and the migration would then carry two independent statements.
    """
    import inspect

    source = inspect.getsource(db._apply_cache_schema)
    assert HELPER in source
    assert f"CREATE INDEX IF NOT EXISTS {INDEX}" not in source


def test_pre_fixture_is_a_041_head_install_without_the_index(db, tmp_path):
    conn = sqlite3.connect(_scratch_copy(PRE_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 41
        assert not _index_present(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 0
        # Non-vacuity: an index over an empty table proves nothing, and the
        # join this index serves is per (root, file), so more than one file
        # under one root is what makes the population representative.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 4
        assert conn.execute(
            "SELECT COUNT(DISTINCT source_path) FROM codex_session_entries"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(DISTINCT source_root_key) FROM codex_session_entries"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_carries_the_index_over_both_members(db, tmp_path):
    conn = sqlite3.connect(_scratch_copy(POST_DB, tmp_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
        sql = _index_sql(conn)
        assert sql is not None
        assert "source_root_key" in sql and "source_path" in sql, (
            "a root-only index is the one that already existed and cannot "
            "serve the per-file alias join"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 4
    finally:
        conn.close()


def test_the_planner_picks_it_for_the_per_file_alias_lookup(db, tmp_path):
    """A shape assertion is not enough — SQLite has to choose it."""
    conn = sqlite3.connect(_scratch_copy(POST_DB, tmp_path))
    try:
        plan = " ".join(str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id FROM codex_session_entries "
            " WHERE source_root_key = ? AND source_path = ?",
            ("r" * 32, "/roots/rk/sessions/a.jsonl"),
        ))
    finally:
        conn.close()
    assert INDEX in plan, plan


def test_handler_takes_the_committed_pre_fixture_to_the_post_shape(db, tmp_path):
    target = tmp_path / "cache.db"
    shutil.copyfile(PRE_DB, target)
    conn = sqlite3.connect(target)
    try:
        handler = _registered_handler(db)
        handler(conn)
        assert _index_present(conn)
        first = _index_sql(conn)
        handler(conn)
        assert _index_sql(conn) == first
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
