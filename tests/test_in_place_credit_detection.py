"""In-place credit detection (v1.7.2) — record-usage tests.

Covers:

* **Reset-aware DB clamp** (Task 2): the monotonic 7d clamp now joins
  against ``week_reset_events`` so the ``MAX(weekly_percent)`` query
  filters to samples captured at-or-after the segment's
  ``effective_reset_at_utc``. Legacy behavior preserved when no event
  row exists.

* **In-place credit detection branch** (Task 3): when ``resets_at``
  stays unchanged but ``weekly_percent`` drops by ≥25pp, emit a
  ``week_reset_events`` row, force-write ``hwm-7d``, and let the seed
  snapshot land via the now-reset-aware clamp.

* **Backfill extension** (Task 4): historical in-place credits get a
  parallel-branch detection in ``_backfill_week_reset_events``.

* **Milestone segment stamping** (Task 5).

* **percent-breakdown filter** (Task 6).

* **Alerts dedup** (Task 8).

Conventions:
* Each test uses ``tmp_path`` via ``redirect_paths`` so ``hwm-7d`` and
  the SQLite DBs land in an isolated scratch HOME.
* ``argparse.Namespace`` is constructed directly to drive
  ``cmd_record_usage`` without a shell.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ── helpers ────────────────────────────────────────────────────────────


def _record_usage_args(
    *,
    percent: float,
    resets_at: int,
    five_hour_percent: float | None = None,
    five_hour_resets_at: int | None = None,
    week_start_name: str | None = None,
) -> argparse.Namespace:
    """Build a minimal Namespace matching cmd_record_usage's signature."""
    return argparse.Namespace(
        percent=percent,
        resets_at=resets_at,
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=five_hour_resets_at,
        week_start_name=week_start_name,
    )


def _seed_usage_snapshot(
    conn,
    *,
    captured_at_utc: str,
    week_start_date: str,
    week_end_at: str,
    weekly_percent: float,
    week_start_at: str | None = None,
    week_end_date: str | None = None,
) -> int:
    """Insert a weekly_usage_snapshots row and return its id."""
    if week_start_at is None:
        week_start_at = week_start_date + "T00:00:00+00:00"
    if week_end_date is None:
        week_end_date = week_end_at[:10]
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_end_date,
         week_start_at, week_end_at, weekly_percent, "test", "{}"),
    )
    return int(cur.lastrowid)


def _seed_reset_event(
    conn,
    *,
    new_week_end_at: str,
    effective: str,
    old_week_end_at: str | None = None,
    detected_at_utc: str = "2026-05-15T19:35:00Z",
) -> int:
    """Insert a week_reset_events row and return its id."""
    if old_week_end_at is None:
        old_week_end_at = effective
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
        (detected_at_utc, old_week_end_at, new_week_end_at, effective),
    )
    conn.commit()
    return int(cur.lastrowid)


def _epoch(iso: str) -> int:
    """Parse an ISO-8601 UTC timestamp into a unix epoch int."""
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# ── Task 2: reset-aware DB clamp ──────────────────────────────────────


def test_reset_aware_clamp_without_event_preserves_legacy_behavior(ns):
    """No week_reset_events row → clamp behaves like before (MAX over
    the whole week, post-credit reading is rejected as a regression).
    """
    # Resets_at points to the same week_end_at as the seeded prior row.
    end_at_epoch = _epoch("2026-05-16T05:00:00+00:00")
    end_at_iso = "2026-05-16T05:00:00+00:00"
    # Seed a 67% sample on the same week (no event row).
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date="2026-05-09",
            week_end_at=end_at_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive cmd_record_usage with percent=2.0 (post-credit shape) and the
    # same end_at — without an event row, the legacy clamp must reject.
    args = _record_usage_args(percent=2.0, resets_at=end_at_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 2.0"
        ).fetchone()[0]
        assert cnt == 0, "clamp should have rejected the 2% reading"
    finally:
        conn.close()


def test_reset_aware_clamp_with_event_filters_to_post_credit(ns):
    """With a week_reset_events row, the MAX query filters to samples
    captured at-or-after effective_reset_at_utc. Pre-credit 67% no
    longer dominates; a fresh post-credit 4% lands.
    """
    end_at_iso = "2026-05-16T05:00:00+00:00"
    end_at_epoch = _epoch(end_at_iso)
    effective_iso = "2026-05-15T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit 67% sample.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date="2026-05-09",
            week_end_at=end_at_iso,
            weekly_percent=67.0,
        )
        # Event row marking the segment boundary.
        _seed_reset_event(
            conn,
            new_week_end_at=end_at_iso,
            effective=effective_iso,
        )
        # Post-credit 2% sample (already past the boundary; the new
        # clamp's MAX over the post-segment window starts at 2%).
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T18:00:00Z",
            week_start_date="2026-05-09",
            week_end_at=end_at_iso,
            weekly_percent=2.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Now drive cmd_record_usage with percent=4.0 — must pass the
    # reset-aware clamp (4 > 2 over the post-credit segment).
    args = _record_usage_args(percent=4.0, resets_at=end_at_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 4.0"
        ).fetchone()[0]
        assert cnt == 1, "clamp should have passed the 4% reading post-credit"
    finally:
        conn.close()
