"""Regression: ``004_heal_forked_week_start_date_buckets`` migration.

Self-heal companion to commit ``6def75f8`` (UTC-anchor ``week_start_date``
bucket key in ``_derive_week_from_payload`` / ``pick_week_selection``).
The writer fix prevents NEW ghost rows on the FIXED binary, but a
still-deployed older binary can keep writing ghosts every time the
host process inherits a non-UTC TZ. This migration auto-merges any
such ghost rows on the next ``open_db()`` so the in-place corruption
heals regardless of which binary wrote it.

Invariant under test: for every row with ``week_start_at IS NOT NULL``,
``week_start_date == substr(week_start_at, 1, 10)``.
"""
from __future__ import annotations

import sqlite3

import pytest

from conftest import load_script


@pytest.fixture
def ns():
    return load_script()


def _seed_schema(conn: sqlite3.Connection) -> None:
    """Create the three relevant tables + ``schema_migrations``. Schema
    mirrors what ``open_db()`` would set up (only the columns this
    migration touches; the rest are not relevant)."""
    conn.execute("""
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'statusline',
            payload_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE weekly_cost_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            cost_usd REAL NOT NULL,
            mode TEXT NOT NULL DEFAULT 'auto'
        )
    """)
    conn.execute("""
        CREATE TABLE percent_milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            percent_threshold INTEGER NOT NULL,
            cumulative_cost_usd REAL NOT NULL,
            usage_snapshot_id INTEGER NOT NULL,
            cost_snapshot_id INTEGER NOT NULL,
            UNIQUE(week_start_date, percent_threshold)
        )
    """)
    conn.execute("""
        CREATE TABLE schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        )
    """)


def _migration_handler(ns):
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "004_heal_forked_week_start_date_buckets":
            return m.handler
    raise AssertionError("migration not registered")


# Two canonical-vs-forked dates that pair with the ghost-bucket scenario
# from the May 2026 production data (UTC moment 2026-05-09T05:00:00Z
# lands on 2026-05-08 in Pacific).
CANON_DATE = "2026-05-09"
GHOST_DATE = "2026-05-08"
CANON_END = "2026-05-16"
GHOST_END = "2026-05-15"
START_AT = "2026-05-09T05:00:00+00:00"
END_AT = "2026-05-16T05:00:00+00:00"


def test_usage_snapshot_forked_row_is_rekeyed(ns):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    # One canonical row, one forked row — same physical week, different
    # week_start_date due to host-TZ contamination at insert time.
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-15T16:09:35Z", CANON_DATE, CANON_END, START_AT, END_AT, 66.0),
    )
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-15T17:01:32Z", GHOST_DATE, GHOST_END, START_AT, END_AT, 66.0),
    )
    conn.commit()

    _migration_handler(ns)(conn)

    dates = [r["week_start_date"] for r in conn.execute(
        "SELECT week_start_date FROM weekly_usage_snapshots ORDER BY id"
    )]
    end_dates = [r["week_end_date"] for r in conn.execute(
        "SELECT week_end_date FROM weekly_usage_snapshots ORDER BY id"
    )]
    assert dates == [CANON_DATE, CANON_DATE], (
        f"forked row not re-keyed; got week_start_date={dates}"
    )
    assert end_dates == [CANON_END, CANON_END]
    # Marker stamped so the dispatcher closes the gate.
    assert conn.execute(
        "SELECT 1 FROM schema_migrations "
        "WHERE name = '004_heal_forked_week_start_date_buckets'"
    ).fetchone() is not None
    conn.close()


def test_cost_snapshot_forked_row_is_rekeyed(ns):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-15T17:01:32Z", GHOST_DATE, GHOST_END, START_AT, END_AT, 1472.33),
    )
    conn.commit()

    _migration_handler(ns)(conn)

    row = conn.execute(
        "SELECT week_start_date, week_end_date FROM weekly_cost_snapshots"
    ).fetchone()
    assert row["week_start_date"] == CANON_DATE
    assert row["week_end_date"] == CANON_END
    conn.close()


def test_milestone_with_canonical_counterpart_is_deleted(ns):
    """When a forked milestone duplicates a canonical-keyed row at the
    same percent_threshold, the ghost is deleted (canonical preserves
    the original alerted_at and the genuine crossing's cumulative cost
    — re-keying the ghost would collide with UNIQUE)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    # Canonical milestone — the genuine crossing.
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " usage_snapshot_id, cost_snapshot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-15T16:09:35Z", CANON_DATE, CANON_END, START_AT, END_AT,
         66, 1453.62, 1, 1),
    )
    # Forked ghost milestone at the same threshold — buggy older
    # binary fired all 7 thresholds (60-66) in one tick.
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " usage_snapshot_id, cost_snapshot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-15T17:01:32Z", GHOST_DATE, GHOST_END, START_AT, END_AT,
         66, 1472.33, 2, 2),
    )
    conn.commit()

    _migration_handler(ns)(conn)

    rows = conn.execute(
        "SELECT week_start_date, percent_threshold, cumulative_cost_usd "
        "  FROM percent_milestones ORDER BY id"
    ).fetchall()
    # Only the canonical row survives, with its original cumulative cost.
    assert len(rows) == 1
    assert rows[0]["week_start_date"] == CANON_DATE
    assert rows[0]["percent_threshold"] == 66
    assert rows[0]["cumulative_cost_usd"] == 1453.62
    conn.close()


def test_milestone_without_canonical_counterpart_is_rekeyed(ns):
    """When a forked milestone has no canonical-keyed counterpart at
    the same threshold, it gets re-keyed in place (not deleted)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    # Only the forked row exists at threshold=67. No canonical counterpart.
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " usage_snapshot_id, cost_snapshot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-15T17:01:32Z", GHOST_DATE, GHOST_END, START_AT, END_AT,
         67, 1484.44, 3, 3),
    )
    conn.commit()

    _migration_handler(ns)(conn)

    row = conn.execute(
        "SELECT week_start_date, week_end_date, percent_threshold "
        "  FROM percent_milestones"
    ).fetchone()
    assert row["week_start_date"] == CANON_DATE
    assert row["week_end_date"] == CANON_END
    assert row["percent_threshold"] == 67
    conn.close()


def test_rerun_is_noop(ns):
    """Second invocation finds zero forked rows; idempotent."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-15T17:01:32Z", GHOST_DATE, GHOST_END, START_AT, END_AT, 66.0),
    )
    conn.commit()

    handler = _migration_handler(ns)
    handler(conn)
    # Second invocation must be a no-op (the dispatcher won't actually
    # call us twice because the marker row blocks re-invocation, but
    # the handler itself MUST be idempotent for crash-recovery cases).
    handler(conn)

    row = conn.execute(
        "SELECT week_start_date FROM weekly_usage_snapshots"
    ).fetchone()
    assert row["week_start_date"] == CANON_DATE
    conn.close()


def test_empty_fork_fast_path_just_stamps_marker(ns):
    """When no forked rows exist, the migration stamps the marker
    without touching any data rows."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    # One purely canonical row (no fork).
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-15T16:09:35Z", CANON_DATE, CANON_END, START_AT, END_AT, 66.0),
    )
    conn.commit()

    _migration_handler(ns)(conn)

    # Marker present.
    assert conn.execute(
        "SELECT 1 FROM schema_migrations "
        "WHERE name = '004_heal_forked_week_start_date_buckets'"
    ).fetchone() is not None
    # Canonical row untouched.
    row = conn.execute(
        "SELECT week_start_date FROM weekly_usage_snapshots"
    ).fetchone()
    assert row["week_start_date"] == CANON_DATE
    conn.close()


def test_row_with_null_week_start_at_is_left_alone(ns):
    """Legacy rows with ``week_start_at IS NULL`` cannot have their
    invariant validated — leave them as-is. The migration must not
    error on or rewrite them."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _seed_schema(conn)
    # Legacy row: NULL week_start_at; week_start_date is the source of truth.
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent) VALUES (?, ?, ?, NULL, NULL, ?)",
        ("2026-04-01T10:00:00Z", "2026-04-01", "2026-04-08", 12.0),
    )
    conn.commit()

    _migration_handler(ns)(conn)

    row = conn.execute(
        "SELECT week_start_date, week_start_at FROM weekly_usage_snapshots"
    ).fetchone()
    assert row["week_start_date"] == "2026-04-01"
    assert row["week_start_at"] is None
    conn.close()
