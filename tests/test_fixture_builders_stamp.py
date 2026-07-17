"""Unit tests for the shared stamp_all_stats_migrations_applied fixture helper
(cctally-dev#94). The render-only fixture builders (share, dashboard) use it to
ship a fully-migrated stats.db so a read command's sync_cache walk can't flip
the #93 upgrade-gate to PROCEED and recompute seeded display tables."""
import sqlite3
import tempfile
from pathlib import Path

import _fixture_builders as fb
from _cctally_db import _CACHE_MIGRATIONS, _STATS_MIGRATIONS


def test_stamp_marks_every_registered_migration_and_advances_user_version():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "stats.db"
        fb.create_stats_db(db)
        with sqlite3.connect(db) as conn:
            fb.stamp_all_stats_migrations_applied(conn)
            conn.commit()
            applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
    expected = {m.name for m in _STATS_MIGRATIONS}
    assert applied == expected, f"missing={expected - applied} extra={applied - expected}"
    assert uv == len(_STATS_MIGRATIONS), f"user_version={uv} expected={len(_STATS_MIGRATIONS)}"


def test_stamp_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "stats.db"
        fb.create_stats_db(db)
        with sqlite3.connect(db) as conn:
            fb.stamp_all_stats_migrations_applied(conn)
            fb.stamp_all_stats_migrations_applied(conn)  # second call must not raise/duplicate
            conn.commit()
            n = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
    assert n == len(_STATS_MIGRATIONS)
    assert uv == len(_STATS_MIGRATIONS)


def test_cache_stamp_marks_every_registered_migration_and_advances_user_version():
    """Render fixtures are fresh schema, never legacy-migration input."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "cache.db"
        fb.create_cache_db(db)
        with sqlite3.connect(db) as conn:
            fb.stamp_all_cache_migrations_applied(conn)
            conn.commit()
            applied = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            uv = conn.execute("PRAGMA user_version").fetchone()[0]
    expected = {m.name for m in _CACHE_MIGRATIONS}
    assert applied == expected, f"missing={expected - applied} extra={applied - expected}"
    assert uv == len(_CACHE_MIGRATIONS), (
        f"user_version={uv} expected={len(_CACHE_MIGRATIONS)}"
    )
