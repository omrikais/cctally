"""Per-migration goldens for ``005_percent_milestones_reset_event_id``.

Loads ``tests/fixtures/migrations/per-migration/005_.../pre.sqlite``,
runs the migration handler against it, and diffs the result against
``post.sqlite``. Verifies:

  * The ``reset_event_id`` column is added (NOT NULL DEFAULT 0).
  * All existing rows backfill to ``reset_event_id = 0``.
  * The new UNIQUE constraint
    ``UNIQUE(week_start_date, percent_threshold, reset_event_id)``
    allows post-credit threshold crossings (same week+threshold,
    different reset_event_id) to coexist.
  * The ``005_percent_milestones_reset_event_id`` marker is stamped
    into ``schema_migrations``.

Per-migration goldens are lazy-adopted (CLAUDE.md gotcha "lazy-adopted;
not retroactively backfilled"); 005 is the first to ship them.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "005_percent_milestones_reset_event_id"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"


@pytest.fixture
def ns():
    return load_script()


def _migration_handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "005_percent_milestones_reset_event_id":
            return m.handler
    raise AssertionError("migration 005 not registered")


def _table_schema(conn, table):
    return [
        (r[1], r[2], r[3], r[4])  # name, type, notnull, default
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]


def _milestone_rows(conn):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, week_start_date, percent_threshold, reset_event_id "
            "FROM percent_milestones ORDER BY id"
        ).fetchall()
    ]


def test_pre_fixture_has_legacy_shape(ns):
    """Sanity: pre.sqlite is at the pre-005 schema."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[0] for r in _table_schema(conn, "percent_milestones")]
        assert "reset_event_id" not in cols, (
            f"pre.sqlite should not have reset_event_id; cols={cols}"
        )
    finally:
        conn.close()


def test_migration_handler_adds_column_and_constraint(ns, tmp_path):
    """Run handler on a fresh copy of pre.sqlite; verify post shape."""
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        _migration_handler(ns)(conn)

        # Column present.
        cols = {r[0] for r in _table_schema(conn, "percent_milestones")}
        assert "reset_event_id" in cols, cols

        # All existing rows have reset_event_id = 0.
        rows = _milestone_rows(conn)
        assert len(rows) == 3
        for r in rows:
            assert r["reset_event_id"] == 0, r

        # Marker stamped.
        assert conn.execute(
            "SELECT 1 FROM schema_migrations "
            "WHERE name='005_percent_milestones_reset_event_id'"
        ).fetchone() is not None

        # New UNIQUE allows (week, threshold, distinct event_id) without
        # collision against the pre-existing (week, threshold, 0) row.
        # threshold=1 already exists with reset_event_id=0; inserting
        # threshold=1 with reset_event_id=42 must succeed under the new
        # UNIQUE shape.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " percent_threshold, cumulative_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-01-06T00:00:00Z", "2026-01-05", "2026-01-12",
             1, 12.0, 4, 4, 42),
        )
        conn.commit()
        post_rows = _milestone_rows(conn)
        assert len(post_rows) == 4

        # Verify the OLD 2-col UNIQUE is gone — duplicate
        # (same week, same threshold, same reset_event_id=0) should
        # still collide under the new UNIQUE.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " percent_threshold, cumulative_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-01-06T00:00:00Z", "2026-01-05", "2026-01-12",
                 1, 13.0, 5, 5, 0),
            )
    finally:
        conn.close()


def test_migration_handler_idempotent_on_rerun(ns, tmp_path):
    """Second invocation finds the column already present and no-ops."""
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        _migration_handler(ns)(conn)
        # Second call: should be a no-op (fast-path probe stamps marker).
        _migration_handler(ns)(conn)
        # Marker still exists exactly once.
        cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='005_percent_milestones_reset_event_id'"
        ).fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()
