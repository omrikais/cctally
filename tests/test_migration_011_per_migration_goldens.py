"""Per-migration golden for ``011_budget_milestone_period_keys`` (#137).

Loads ``tests/fixtures/migrations/per-migration/011_.../pre.sqlite`` (old-shape
budget/codex/projected milestone tables, no ``period`` column, narrow UNIQUE +
seeded crossings), runs the real 011 handler, and asserts:

  * each of the three tables gains a nullable ``period`` column;
  * every historical row backfills to ``period IS NULL`` (write-once sentinel,
    NOT a fabricated value);
  * the new period-inclusive UNIQUE is in place;
  * the ``011_budget_milestone_period_keys`` marker is stamped;
  * a second run hits the fast-path (period present) and is a no-op.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script

# W1 registry-completeness guard (#279 S7): declares this module exercises
# the handler's second-invocation idempotency (test names vary across modules).
IDEMPOTENCY_COVERED = True


FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "011_budget_milestone_period_keys"
)
PRE_DB = FIXTURE / "pre.sqlite"


@pytest.fixture
def ns():
    return load_script()


def _migration_handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "011_budget_milestone_period_keys":
            return m.handler
    raise AssertionError("migration 011 not registered")


def test_pre_fixture_has_legacy_shape_and_exact_pre_011_topology(ns):
    """Sanity: pre.sqlite is the fully migrated pre-011 database shape."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        for table in ("budget_milestones", "codex_budget_milestones",
                      "projected_milestones"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "period" not in cols, f"{table} cols={cols}"

        markers = [
            row[0]
            for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")
        ]
        expected = [
            migration.name
            for migration in ns["_STATS_MIGRATIONS"]
            if migration.seq <= 10
        ]
        assert markers == expected
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        conn.close()


def test_011_backfills_null_and_new_unique(ns, tmp_path):
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    conn.row_factory = sqlite3.Row
    try:
        _migration_handler(ns)(conn)
        ns["_stamp_applied"](conn, "011_budget_milestone_period_keys")  # dispatcher now owns the stamp (#140)

        for table in ("budget_milestones", "codex_budget_milestones",
                      "projected_milestones"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert "period" in cols, table
            null_count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE period IS NULL"
            ).fetchone()[0]
            total = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            assert total > 0 and null_count == total, table

        budget_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='budget_milestones'"
        ).fetchone()[0]
        assert "UNIQUE(week_start_at, period, threshold)" in budget_sql
        codex_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='codex_budget_milestones'"
        ).fetchone()[0]
        assert "UNIQUE(period_start_at, period, threshold)" in codex_sql
        proj_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='projected_milestones'"
        ).fetchone()[0]
        assert "UNIQUE(week_start_at, period, metric, threshold)" in proj_sql

        applied = {
            r[0] for r in conn.execute("SELECT name FROM schema_migrations")
        }
        assert "011_budget_milestone_period_keys" in applied

        # The new period-inclusive UNIQUE lets calendar-week and calendar-month
        # crossings coexist at the same instant (Symptom 2): the seeded NULL row
        # plus a fresh 'calendar-week' row at the same (week_start_at, threshold)
        # must NOT collide.
        seeded = conn.execute(
            "SELECT week_start_at, threshold FROM budget_milestones LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO budget_milestones (week_start_at, period, threshold, "
            "budget_usd, spent_usd, consumption_pct, crossed_at_utc) "
            "VALUES (?, 'calendar-week', ?, 100.0, 92.0, 92.0, ?)",
            (seeded["week_start_at"], seeded["threshold"], "2026-06-05T00:00:00Z"),
        )
        conn.commit()
        # Both rows now coexist at the same (week_start_at, threshold): the
        # backfilled NULL-period historical row + the new 'calendar-week' row.
        coexisting = conn.execute(
            "SELECT COUNT(*) FROM budget_milestones "
            "WHERE week_start_at = ? AND threshold = ?",
            (seeded["week_start_at"], seeded["threshold"]),
        ).fetchone()[0]
        assert coexisting == 2
    finally:
        conn.close()


def test_011_idempotent_second_run_is_noop(ns, tmp_path):
    work = tmp_path / "stats.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        _migration_handler(ns)(conn)
        ns["_stamp_applied"](conn, "011_budget_milestone_period_keys")  # dispatcher now owns the stamp (#140)
        _migration_handler(ns)(conn)  # second run hits fast-path — no raise
        ns["_stamp_applied"](conn, "011_budget_milestone_period_keys")
        cnt = conn.execute(
            "SELECT COUNT(*) FROM budget_milestones"
        ).fetchone()[0]
        assert cnt > 0
        marker_cnt = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='011_budget_milestone_period_keys'"
        ).fetchone()[0]
        assert marker_cnt == 1
    finally:
        conn.close()
