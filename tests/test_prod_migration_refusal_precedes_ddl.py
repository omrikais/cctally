"""#566: the refusal must precede the DDL it exists to prevent.

`open_cache_db` acquires its flocks, then calls `apply_policy` and
`_apply_cache_schema`, and only afterwards reaches the dispatcher where
`ProdMigrationRefused` lives. A dev-checkout binary pointed at the real prod
directory therefore mutated the production schema and only then refused. The
ordering is pre-existing, but the #566 head bump makes it reachable for EVERY
existing store rather than only for stores that happened to be behind.

Making this test non-vacuous is the whole difficulty. The prod directory is
resolved through the password database rather than `$HOME`
(`_cctally_core._real_prod_data_dir`), precisely so that the suite's fake-HOME
harnesses do not trip the guard — see `tests/test_prod_migration_guard.py`'s
`test_fake_home_prod_shaped_not_blocked`. A fixture that only fakes `HOME`
would therefore never reach the guard at all, and the test would pass while
proving nothing. The fixture below patches `_real_prod_data_dir` itself, which
is what makes the tmp store genuinely "the real prod dir" to the predicate, and
`test_the_fixture_actually_reaches_the_guard` asserts that precondition
directly rather than trusting it.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from conftest import load_script, redirect_paths

INDEX = "idx_codex_entries_root_path"
PENDING = "042_codex_entries_root_path_index"


def _read_only(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _schema_fingerprint(path) -> str:
    conn = _read_only(path)
    try:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(("\x1f".join(str(c) for c in row) + "\x1e").encode())
    return digest.hexdigest()


def _user_version(path) -> int:
    conn = _read_only(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _index_present(path) -> bool:
    conn = _read_only(path)
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (INDEX,),
        ).fetchone() is not None
    finally:
        conn.close()


@pytest.fixture
def prod_shaped_store(monkeypatch, tmp_path):
    """A cache.db one head behind, sitting in what the guard calls real prod.

    Returns the path to the store. `load_script()` runs FIRST, because it
    re-derives every path constant from `HOME` and would otherwise clobber the
    patches (the ordering trap documented on `conftest.load_script`).
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    import _cctally_core
    import _cctally_db

    path = _cctally_core.CACHE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _cctally_db._apply_cache_schema(conn)
        conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
        )
        prior = [m.name for m in _cctally_db._CACHE_MIGRATIONS
                 if m.name != PENDING]
        # Stamped, so the dispatcher treats this as an existing install and
        # would genuinely RUN the pending handler rather than fast-stamping it
        # as a fresh database. Without this the override case below would pass
        # for the wrong reason.
        conn.executemany(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
            "VALUES (?, '2026-08-14T00:00:00Z')",
            [(name,) for name in prior],
        )
        conn.execute(f"PRAGMA user_version={len(prior)}")
        conn.commit()
    finally:
        conn.close()

    # The two conditions `_would_block_prod_migration` reads. Patching
    # `_real_prod_data_dir` is what makes the tmp store the real prod dir as far
    # as the predicate is concerned; faking HOME alone would not.
    monkeypatch.setattr(
        _cctally_core, "_real_prod_data_dir", lambda: path.parent)
    repo = tmp_path / "fake-checkout"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: repo)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)
    return path


def test_the_fixture_actually_reaches_the_guard(prod_shaped_store):
    """Precondition, asserted rather than assumed.

    If this fails, every other assertion in this module is vacuous: the store
    would open normally and no refusal would ever be due.
    """
    import _cctally_db

    conn = sqlite3.connect(prod_shaped_store)
    try:
        assert _cctally_db._would_block_prod_migration(conn) is True
    finally:
        conn.close()
    assert _user_version(prod_shaped_store) == len(
        _cctally_db._CACHE_MIGRATIONS) - 1
    assert not _index_present(prod_shaped_store)


def test_refused_prod_open_leaves_schema_and_version_untouched(
    prod_shaped_store,
):
    import _cctally_cache
    import _cctally_db

    before_schema = _schema_fingerprint(prod_shaped_store)
    before_version = _user_version(prod_shaped_store)

    with pytest.raises(_cctally_db.ProdMigrationRefused):
        _cctally_cache.open_cache_db()

    assert _schema_fingerprint(prod_shaped_store) == before_schema, (
        "the schema changed before the refusal — the DDL ran first"
    )
    assert _user_version(prod_shaped_store) == before_version
    assert not _index_present(prod_shaped_store)


def test_refused_eager_open_leaves_schema_and_version_untouched(
    prod_shaped_store,
):
    """The eager cache path carries the same ordering and the same fix."""
    import _cctally_db

    before_schema = _schema_fingerprint(prod_shaped_store)
    before_version = _user_version(prod_shaped_store)

    with pytest.raises(_cctally_db.ProdMigrationRefused):
        _cctally_db._eagerly_apply_cache_migrations()

    assert _schema_fingerprint(prod_shaped_store) == before_schema, (
        "the schema changed before the refusal — the DDL ran first"
    )
    assert _user_version(prod_shaped_store) == before_version
    assert not _index_present(prod_shaped_store)


def test_the_refusal_names_the_pending_migration(prod_shaped_store):
    import _cctally_cache
    import _cctally_db

    with pytest.raises(_cctally_db.ProdMigrationRefused) as exc:
        _cctally_cache.open_cache_db()
    assert PENDING in str(exc.value)
    assert "CCTALLY_ALLOW_PROD_MIGRATION" in str(exc.value)


def test_the_override_still_lets_the_open_proceed(
    prod_shaped_store, monkeypatch,
):
    """Non-vacuity for the guard itself: with the hatch set, the identical
    arrangement migrates and the index appears."""
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "1")
    import _cctally_cache
    import _cctally_db

    conn = _cctally_cache.open_cache_db()
    conn.close()

    assert _index_present(prod_shaped_store)
    assert _user_version(prod_shaped_store) == len(
        _cctally_db._CACHE_MIGRATIONS)


def test_a_current_store_in_prod_is_not_refused(prod_shaped_store):
    """The preflight must fire only when the open would ADVANCE the store.

    A dev binary reading an already-current prod cache.db is the everyday case
    and must keep working; refusing there would break every read command.

    The arrangement deliberately leaves the store non-current on the
    COMPATIBILITY leg while stamping it current on the version leg. Without
    that, `open_cache_db` returns at its fast path before the flock branch, so
    the preflight is never evaluated and the case passes whatever the preflight
    does — it would stay green even if the preflight raised unconditionally.
    Dropping `conversation_messages` forces the open down the flocked branch,
    where the preflight really runs, while `user_version` at head means it has
    nothing to advance and must therefore permit the open.
    """
    import _cctally_cache
    import _cctally_db

    conn = sqlite3.connect(prod_shaped_store)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(name, applied_at_utc) "
            "VALUES (?, '2026-08-14T00:00:00Z')",
            (PENDING,),
        )
        conn.execute("DROP TABLE IF EXISTS conversation_messages")
        conn.execute(
            f"PRAGMA user_version={len(_cctally_db._CACHE_MIGRATIONS)}")
        conn.commit()
    finally:
        conn.close()

    opened = _cctally_cache.open_cache_db()
    opened.close()
    assert _user_version(prod_shaped_store) == len(
        _cctally_db._CACHE_MIGRATIONS)


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
