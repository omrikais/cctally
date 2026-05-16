"""Regression for migration 003's defensive widening of the milestone dedup key.

Spec §3.4 / Codex r2 finding 1: migration 003 dedups ``five_hour_milestones``
rows by ``percent_threshold`` alone. After migration 006 adds
``reset_event_id``, an operator-triggered re-run of 003 (``db unskip``,
fresh-DB-from-corrupted-backup, future tooling) would silently collapse
legitimately distinct pre/post-credit rows at the same physical threshold.

The defensive widening: when ``reset_event_id`` is present in PRAGMA
``table_info``, 003's dedup loop keys on ``(threshold, reset_event_id)``.
When absent, the key collapses to ``(threshold, 0)`` — byte-identical to
the original threshold-only behavior on the legacy upgrade path. PRAGMA
probe rather than version-detect so the path covers both the natural
migration order (003 runs before 006; column absent) AND operator-
triggered re-runs of 003 post-006 (column present).

The migration 003 handler is invoked via its registered ``handler``
attribute looked up through ``conftest.load_script()``; this keeps the
test stable across any future renames of the underlying Python symbol.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


@pytest.fixture
def ns():
    return load_script()


def _migration_003_handler(ns):
    """Return the migration 003 handler regardless of its Python name."""
    for m in ns["_STATS_MIGRATIONS"]:
        if m.name == "003_merge_5h_block_duplicates_v1":
            return m.handler
    raise AssertionError("migration 003 not registered")


def _build_with_dup_blocks(db_path: Path, *, with_seg_column: bool) -> None:
    """Seed two duplicate ``five_hour_blocks`` (same physical window jittered
    by 10 seconds — same canonical 10-min slot) and milestone rows at
    ``percent_threshold=25``. When ``with_seg_column`` is True, seed both a
    pre-credit row (``reset_event_id=0``) and a post-credit row
    (``reset_event_id=42``) plus a dropped-block sibling at seg=0 with the
    earliest captured_at_utc; on the legacy path (column absent), seed two
    threshold=25 rows spread across the duplicate blocks so the dedup
    collapses them to one.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    base_schema = """
        PRAGMA journal_mode = WAL;
        CREATE TABLE five_hour_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            five_hour_window_key INTEGER NOT NULL,
            five_hour_resets_at TEXT NOT NULL,
            block_start_at TEXT NOT NULL,
            first_observed_at_utc TEXT NOT NULL,
            last_observed_at_utc TEXT NOT NULL,
            final_five_hour_percent REAL NOT NULL,
            seven_day_pct_at_block_start REAL,
            seven_day_pct_at_block_end REAL,
            crossed_seven_day_reset INTEGER NOT NULL DEFAULT 0,
            total_input_tokens INTEGER NOT NULL DEFAULT 0,
            total_output_tokens INTEGER NOT NULL DEFAULT 0,
            total_cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            total_cost_usd REAL NOT NULL DEFAULT 0,
            is_closed INTEGER NOT NULL DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            last_updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        CREATE TABLE five_hour_block_models (
            id INTEGER PRIMARY KEY,
            block_id INTEGER,
            five_hour_window_key INTEGER NOT NULL,
            model TEXT NOT NULL,
            UNIQUE(five_hour_window_key, model)
        );
        CREATE TABLE five_hour_block_projects (
            id INTEGER PRIMARY KEY,
            block_id INTEGER,
            five_hour_window_key INTEGER NOT NULL,
            project_path TEXT NOT NULL,
            UNIQUE(five_hour_window_key, project_path)
        );
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY,
            five_hour_window_key INTEGER,
            captured_at_utc TEXT
        );
    """
    if with_seg_column:
        base_schema += """
        CREATE TABLE five_hour_milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id INTEGER NOT NULL,
            five_hour_window_key INTEGER NOT NULL,
            percent_threshold INTEGER NOT NULL,
            captured_at_utc TEXT NOT NULL,
            usage_snapshot_id INTEGER NOT NULL,
            reset_event_id INTEGER NOT NULL DEFAULT 0,
            UNIQUE(five_hour_window_key, percent_threshold, reset_event_id)
        );
        """
    else:
        base_schema += """
        CREATE TABLE five_hour_milestones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id INTEGER NOT NULL,
            five_hour_window_key INTEGER NOT NULL,
            percent_threshold INTEGER NOT NULL,
            captured_at_utc TEXT NOT NULL,
            usage_snapshot_id INTEGER NOT NULL,
            UNIQUE(five_hour_window_key, percent_threshold)
        );
        """
    conn.executescript(base_schema)

    # Two duplicate blocks (same canonical 10-min slot). The window keys
    # differ by jitter only; migration 003's job is to merge them.
    conn.execute(
        "INSERT INTO five_hour_blocks "
        "(id, five_hour_window_key, five_hour_resets_at, "
        " block_start_at, first_observed_at_utc, last_observed_at_utc, "
        " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
        "VALUES (1, 1746550800, '2026-05-16T19:30:00Z', "
        "'2026-05-16T14:30:00Z', '2026-05-16T14:35:00Z', "
        "'2026-05-16T18:00:00Z', 28.0, '2026-05-16T14:35:00Z', "
        "'2026-05-16T18:00:00Z')"
    )
    conn.execute(
        "INSERT INTO five_hour_blocks "
        "(id, five_hour_window_key, five_hour_resets_at, "
        " block_start_at, first_observed_at_utc, last_observed_at_utc, "
        " final_five_hour_percent, created_at_utc, last_updated_at_utc) "
        "VALUES (2, 1746550810, '2026-05-16T19:30:10Z', "
        "'2026-05-16T14:30:00Z', '2026-05-16T14:40:00Z', "
        "'2026-05-16T18:05:00Z', 28.0, '2026-05-16T14:40:00Z', "
        "'2026-05-16T18:05:00Z')"
    )

    if with_seg_column:
        # Pre-credit row (seg=0) on canonical (id=1) block at threshold=25:
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, reset_event_id) "
            "VALUES (1, 1746550800, 25, '2026-05-16T17:00:00Z', 100, 0)"
        )
        # Post-credit row (seg=42) on canonical (id=1) block at threshold=25
        # — distinct from the pre-credit row and MUST NOT be collapsed by
        # the dedup loop (Codex r2 finding 1 / spec §3.4).
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, reset_event_id) "
            "VALUES (1, 1746550800, 25, '2026-05-16T17:30:00Z', 101, 42)"
        )
        # Dropped-block sibling row at the same physical threshold AND
        # seg=0 — collides with canonical's pre-credit row on the dedup
        # key, so the merge migration keeps whichever has the earliest
        # captured_at_utc (here: this row, 16:55 < canonical's 17:00).
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id, reset_event_id) "
            "VALUES (2, 1746550810, 25, '2026-05-16T16:55:00Z', 99, 0)"
        )
    else:
        # Legacy shape — no reset_event_id column. Two threshold=25 rows
        # spread across the duplicate blocks; merge collapses to one.
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id) "
            "VALUES (1, 1746550800, 25, '2026-05-16T17:00:00Z', 100)"
        )
        conn.execute(
            "INSERT INTO five_hour_milestones "
            "(block_id, five_hour_window_key, percent_threshold, "
            " captured_at_utc, usage_snapshot_id) "
            "VALUES (2, 1746550810, 25, '2026-05-16T16:55:00Z', 99)"
        )
    conn.commit()
    conn.close()


def test_migration_003_legacy_path_byte_identical(ns, tmp_path):
    """When ``reset_event_id`` column is absent, dedup behaves byte-
    identically to the pre-defensive version: collapses duplicates by
    threshold alone, keeps the earliest captured_at_utc.
    """
    db = tmp_path / "legacy.sqlite"
    _build_with_dup_blocks(db, with_seg_column=False)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        _migration_003_handler(ns)(conn)
    finally:
        try:
            conn.commit()
        except sqlite3.ProgrammingError:
            pass
        conn.close()

    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    try:
        rows = conn2.execute(
            "SELECT id, percent_threshold, captured_at_utc, block_id "
            "FROM five_hour_milestones ORDER BY percent_threshold"
        ).fetchall()
        # Single milestone row survives at threshold=25 (the earlier of the
        # two — 16:55 from the dropped block beats 17:00 from canonical).
        assert len(rows) == 1, (
            f"expected 1 row, got {[dict(r) for r in rows]}"
        )
        assert rows[0]["captured_at_utc"] == "2026-05-16T16:55:00Z", (
            dict(rows[0])
        )
    finally:
        conn2.close()


def test_migration_003_post_006_widens_dedup_to_segment(ns, tmp_path):
    """When ``reset_event_id`` column is present, dedup keys on
    ``(threshold, segment)``. Pre-credit row (seg=0) and post-credit row
    (seg=42) both survive even though they share the same physical
    threshold. This is the load-bearing defensive widening from spec
    §3.4 — without it, an operator-triggered re-run of 003 after 006 has
    landed would silently collapse legitimately distinct pre/post-credit
    rows into one.
    """
    db = tmp_path / "post_006.sqlite"
    _build_with_dup_blocks(db, with_seg_column=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        _migration_003_handler(ns)(conn)
    finally:
        try:
            conn.commit()
        except sqlite3.ProgrammingError:
            pass
        conn.close()

    conn2 = sqlite3.connect(db)
    conn2.row_factory = sqlite3.Row
    try:
        rows = conn2.execute(
            "SELECT id, percent_threshold, reset_event_id, "
            "       captured_at_utc, block_id "
            "FROM five_hour_milestones ORDER BY reset_event_id"
        ).fetchall()
        seg_to_row = {r["reset_event_id"]: dict(r) for r in rows}
        assert set(seg_to_row.keys()) == {0, 42}, (
            f"both segments must survive; got {seg_to_row}"
        )
        # Pre-credit row picks the earliest captured_at_utc among segment-0
        # rows (16:55 from the dropped block beats 17:00 from canonical).
        assert seg_to_row[0]["captured_at_utc"] == "2026-05-16T16:55:00Z"
        # Post-credit row is the only seg=42 row, unchanged.
        assert seg_to_row[42]["captured_at_utc"] == "2026-05-16T17:30:00Z"
    finally:
        conn2.close()
