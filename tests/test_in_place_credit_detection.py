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


def _past_week_end_iso() -> tuple[str, int]:
    """Build an ISO + epoch tuple in the PAST. Used for Task 2 clamp
    tests where we want to exercise the clamp alone WITHOUT tripping the
    in-place credit detection branch (which requires
    ``prior_end_dt > now_utc``).
    """
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )
    return past.isoformat(timespec="seconds"), int(past.timestamp())


def test_reset_aware_clamp_without_event_preserves_legacy_behavior(ns):
    """No week_reset_events row → clamp behaves like before (MAX over
    the whole week, post-credit reading is rejected as a regression).
    Uses a past end_at so the in-place credit detection branch is
    skipped (predicate ``prior_end_dt > now_utc`` is false).
    """
    end_at_iso, end_at_epoch = _past_week_end_iso()
    week_start_date, _ = _week_start_for(end_at_iso)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_at_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive cmd_record_usage with percent=2.0 (post-credit shape) and the
    # same end_at — without an event row AND the window is in the past
    # so detection is skipped, the legacy clamp must reject.
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
    Uses a past end_at so this test exercises the CLAMP alone, not the
    in-place credit detection branch (which would also fire on a future
    end_at and double-write the event row).
    """
    end_at_iso, end_at_epoch = _past_week_end_iso()
    week_start_date, _ = _week_start_for(end_at_iso)
    effective_iso = "2026-05-15T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit 67% sample.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
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
            week_start_date=week_start_date,
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


# ── Task 3: in-place credit detection branch ─────────────────────────


def _future_week_end_iso() -> tuple[str, int]:
    """Build an ISO + epoch tuple a few days in the future. The
    detection branch requires ``prior_end_dt > now_utc`` and we'd
    rather not freeze ``dt.datetime.now`` — the test owns its own
    "future" by stamping at "now + 3 days, rounded to next hour".
    """
    now = dt.datetime.now(dt.timezone.utc)
    future = (now + dt.timedelta(days=3)).replace(
        minute=0, second=0, microsecond=0
    )
    return future.isoformat(timespec="seconds"), int(future.timestamp())


def _week_start_for(end_iso: str) -> tuple[str, str]:
    """Given a week_end_at ISO, return (week_start_date, week_end_date)."""
    end = dt.datetime.fromisoformat(end_iso)
    start = end - dt.timedelta(days=7)
    return start.date().isoformat(), end.date().isoformat()


def test_detection_fires_on_threshold(ns, tmp_path):
    """prior=67, cur=2 (drop 65pp ≥ 25pp threshold) with the SAME
    week_end_at as the new fetch: writes event row, seed snapshot
    lands via the reset-aware clamp, and hwm-7d gets force-written.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    # Seed prior 67% snapshot with the SAME end_at as the new fetch.
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Pre-seed hwm-7d so we can verify the force-write decreased it.
    hwm_path = ns["APP_DIR"] / "hwm-7d"
    hwm_path.write_text(f"{week_start_date} 67.0\n")

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # 1 event row written with new == cur_end_canon.
    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        # In-place credit means old == new == cur_end_canon (no boundary shift).
        assert events[0]["new_week_end_at"] == end_iso
        assert events[0]["old_week_end_at"] == end_iso

        # 1 new snapshot at 2%.
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 2.0"
        ).fetchone()[0]
        assert cnt == 1, "post-credit 2% reading should have landed"
    finally:
        conn.close()

    # hwm-7d force-written to the new (lower) value.
    parts = hwm_path.read_text().strip().split()
    assert parts == [week_start_date, "2.0"], parts


def test_detection_does_not_fire_below_threshold(ns, tmp_path):
    """prior=26, cur=2 (drop 24pp < 25pp): no event row, no seed insert
    (legacy monotonic clamp blocks the lower percent), hwm unchanged.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=26.0,
        )
        conn.commit()
    finally:
        conn.close()

    hwm_path = ns["APP_DIR"] / "hwm-7d"
    hwm_path.write_text(f"{week_start_date} 26.0\n")

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 0, "drop below threshold must not fire detection"
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 2.0"
        ).fetchone()[0]
        assert cnt == 0, "legacy clamp should still block the lower reading"
    finally:
        conn.close()

    parts = hwm_path.read_text().strip().split()
    assert parts == [week_start_date, "26.0"], parts


def test_detection_skipped_when_window_expired(ns, tmp_path):
    """prior=67, cur=2 BUT prior_end_dt <= now_utc: no event row.
    This is the natural-rollover case (the old week's end has actually
    passed), not a goodwill credit. Use a past end_at.
    """
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).replace(
        minute=0, second=0, microsecond=0
    )
    end_iso = past.isoformat(timespec="seconds")
    end_epoch = int(past.timestamp())
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 0, "expired-window case must not fire detection"
    finally:
        conn.close()


def test_dedup_via_pre_check(ns, tmp_path):
    """Pre-seed a week_reset_events row for the current new_week_end_at.
    Drive a 67→2 record-usage call. The pre-check fires before the
    INSERT, so no second event row is written.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective="2026-05-15T17:00:00+00:00",
            old_week_end_at=end_iso,
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 1, "pre-check should have prevented a duplicate event row"
    finally:
        conn.close()


def test_dedup_via_seed_snapshot(ns, tmp_path):
    """First call writes event + 2% seed. Second call (next OAuth fetch
    at 3%) sees prior=2 (post-credit) → branch not entered (drop is 1pp
    not >= 25pp) → no second event row.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # First call: detection fires.
    rc = ns["cmd_record_usage"](_record_usage_args(percent=2.0, resets_at=end_epoch))
    assert rc == 0
    # Second call: prior is now 2%, drop 2→3 is +1, branch not entered.
    rc = ns["cmd_record_usage"](_record_usage_args(percent=3.0, resets_at=end_epoch))
    assert rc == 0

    conn = ns["open_db"]()
    try:
        events = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert events == 1, "second call must not re-fire the detection branch"
    finally:
        conn.close()


# ── Task 4: backfill extension for historical in-place credits ───────


def test_backfill_detects_historical_in_place_credit(ns):
    """Seed snapshots showing 67→2 with the SAME week_end on consecutive
    captures (captured BEFORE the end_at — i.e., we were "in the window"
    when the credit landed). Run ``_backfill_week_reset_events``.
    Assert: one event row with old == new == cur_end (in-place credit
    shape), effective == floor_to_hour(captured_at_of_2pct_row).
    """
    # Use a future end_at so captured_dt < prior_end_dt is true.
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        # No event row pre-seeded; backfill should synthesize one.
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        # In-place credit shape: old == new == cur_end.
        assert events[0]["old_week_end_at"] == end_iso
        assert events[0]["new_week_end_at"] == end_iso
        # Effective is floor-to-hour of the captured_at when the drop
        # was first observed (Anthropic's reset times are always
        # hour-aligned). Compare as UTC moment to absorb the host-tz
        # rendering quirk in ``parse_iso_datetime`` — see project
        # gotcha ``unixepoch_for_cross_offset_compare``.
        eff_dt = dt.datetime.fromisoformat(events[0]["effective_reset_at_utc"])
        assert eff_dt.astimezone(dt.timezone.utc) == dt.datetime(
            2026, 5, 14, 17, 0, 0, tzinfo=dt.timezone.utc
        )
    finally:
        conn.close()


def test_backfill_idempotent_on_rerun(ns):
    """Run ``_backfill_week_reset_events`` twice. Assert: only one event
    row exists (UNIQUE(old_week_end_at, new_week_end_at) + INSERT OR
    IGNORE).
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, _ = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)
        ns["_backfill_week_reset_events"](conn)
        ns["_backfill_week_reset_events"](conn)

        cnt = conn.execute(
            "SELECT COUNT(*) FROM week_reset_events"
        ).fetchone()[0]
        assert cnt == 1, "backfill should be idempotent (UNIQUE + IGNORE)"
    finally:
        conn.close()


def test_backfill_preserves_boundary_shift_branch(ns):
    """Boundary-shift legacy: classic mid-week reset where the API
    advances ``week_end_at`` to a new value AND ``weekly_percent``
    drops. Backfill should still emit an event row in the legacy shape
    (``old == prior_end``, ``new == cur_end``, distinct values). This
    is the regression guard for the v1.7.1 path that the in-place
    credit branch must NOT clobber.
    """
    # Future ends so captured < prior_end is satisfied.
    end_1 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3, hours=0)).replace(
        minute=0, second=0, microsecond=0
    )
    end_2 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2, hours=0)).replace(
        minute=0, second=0, microsecond=0
    )
    end_1_iso = end_1.isoformat(timespec="seconds")
    end_2_iso = end_2.isoformat(timespec="seconds")
    week_start = (end_1 - dt.timedelta(days=7)).date().isoformat()
    week_start_2 = (end_2 - dt.timedelta(days=7)).date().isoformat()

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start,
            week_end_at=end_1_iso,
            weekly_percent=67.0,
        )
        # Same captured ordering, NEW end_at (Anthropic shifted the boundary),
        # weekly_percent dropped 25+pp.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_2,
            week_end_at=end_2_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        assert events[0]["old_week_end_at"] == end_1_iso
        assert events[0]["new_week_end_at"] == end_2_iso
    finally:
        conn.close()


# ── Task 5: milestone writer stamps reset_event_id ────────────────────


def _seed_cost_snapshot(
    conn,
    *,
    week_start_date: str,
    week_end_date: str,
    week_start_at: str,
    week_end_at: str,
    cost_usd: float,
    captured_at_utc: str = "2026-05-15T18:00:00Z",
) -> int:
    """Insert a weekly_cost_snapshots row (avoids the milestone writer
    bailing out on missing cost data).
    """
    cur = conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, cost_usd, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_end_date,
         week_start_at, week_end_at, cost_usd, "auto"),
    )
    return int(cur.lastrowid)


def test_milestone_segment_zero_when_no_event(ns):
    """No ``week_reset_events`` row for this week_end_at →
    ``maybe_record_milestone`` writes ``reset_event_id = 0`` (pre-credit
    sentinel).
    """
    end_iso = "2026-05-09T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Cost snapshot so the writer doesn't bail.
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.34,
        )
        # Usage snapshot at 3% (so floor(3) = 3 → threshold 3 crosses).
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-04T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            week_start_at=week_start_at,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        # When max_existing is None and current_floor is 3, the writer
        # records only the just-crossed threshold (3), not the prior
        # ones. The point of this test is to assert reset_event_id=0,
        # not the multi-threshold-catchup loop.
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id FROM percent_milestones "
            "WHERE week_start_date = ? ORDER BY percent_threshold",
            (week_start_date,),
        ).fetchall()
        assert len(rows) == 1, rows
        assert rows[0]["percent_threshold"] == 3
        assert rows[0]["reset_event_id"] == 0
    finally:
        conn.close()


def test_milestone_segment_assigned_when_event_active(ns):
    """``week_reset_events`` row exists with effective < captured_at:
    new milestone rows get ``reset_event_id = event.id``.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.34,
        )
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # Usage snapshot captured AFTER the credit moment.
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
        "capturedAt": "2026-05-14T18:00:00Z",
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id FROM percent_milestones "
            "WHERE week_start_date = ? ORDER BY percent_threshold",
            (week_start_date,),
        ).fetchall()
        # Only threshold 3 lands (no prior max_existing → start at
        # current_floor). The assertion of interest is reset_event_id.
        assert len(rows) == 1, rows
        assert rows[0]["percent_threshold"] == 3
        assert rows[0]["reset_event_id"] == evt_id
    finally:
        conn.close()


def test_milestone_post_credit_threshold_lands_as_new_row(ns):
    """Pre-credit milestone (week, threshold=3, reset_event_id=0) exists.
    Seed event row. Drive ``maybe_record_milestone`` for the same week
    + threshold=3 captured post-event.
    Assert: TWO rows exist for (week, threshold=3) — pre-credit
    reset_event_id=0 + post-credit reset_event_id=event.id.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.34,
        )
        # Pre-credit milestone (reset_event_id = 0).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1, 0),
        )
        # Event row for the credit boundary.
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # New usage snapshot at 3%, captured post-event.
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
        "capturedAt": "2026-05-14T18:00:00Z",
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id "
            "FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 3 "
            "ORDER BY reset_event_id ASC",
            (week_start_date,),
        ).fetchall()
        assert len(rows) == 2, rows
        assert rows[0]["reset_event_id"] == 0
        assert rows[1]["reset_event_id"] == evt_id
    finally:
        conn.close()


# ── Task 6: percent-breakdown filters by active segment ──────────────


def test_percent_breakdown_filters_by_active_segment(ns, capsys):
    """Seed pre-credit + post-credit milestones for the same week +
    threshold. Run ``cmd_percent_breakdown --json``. The active segment
    is the post-credit one; only its rows appear in the milestone list.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit row (segment 0).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1, 0),
        )
        # Event row.
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # Post-credit row (same threshold, different segment).
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 12.0, None, 2, 2, evt_id),
        )
        # Seed a usage snapshot so cmd_percent_breakdown resolves this week.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=True, tz=None,
    )
    rc = ns["cmd_percent_breakdown"](args)
    assert rc == 0
    captured = capsys.readouterr()
    import json as _json
    out = _json.loads(captured.out)
    assert len(out["milestones"]) == 1, out["milestones"]
    # Post-credit row has cumulative_cost_usd = 12.0; pre-credit had 100.0.
    assert out["milestones"][0]["cumulativeCostUSD"] == 12.0


def test_percent_breakdown_empty_post_credit_hint(ns, capsys):
    """Seed event row + pre-credit milestones but no post-credit ones.
    Run cmd_percent_breakdown. The active segment has no rows, so the
    output should include a clear "post-credit segment, no milestones
    crossed yet" hint instead of the generic "No percent milestones
    recorded for this week" line.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit rows only.
        for pct in (1, 2, 3):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
                 week_start_at, end_iso, pct, 10.0 * pct, None, pct, pct, 0),
            )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()
    finally:
        conn.close()

    args = argparse.Namespace(
        week_start=None, week_start_name=None, json=False, tz=None,
    )
    rc = ns["cmd_percent_breakdown"](args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "post-credit" in captured.out.lower(), captured.out


# ── Task 7: dashboard milestone panel filter (shared with TUI) ───────


def test_tui_percent_milestones_filters_to_active_segment(ns):
    """``_tui_build_percent_milestones`` (shared builder for the TUI
    panel AND the dashboard's ``snap.percent_milestones`` envelope
    array) filters to the active segment when a credit event exists for
    the week.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit milestones (segment 0).
        for pct in (1, 2, 3):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
                 week_start_at, end_iso, pct, 100.0 * pct, None, pct, pct, 0),
            )
        # Event row.
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        # One post-credit milestone.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 1, 5.0, None, 4, 4, evt_id),
        )
        # Latest snapshot points the builder at this week.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T10:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()

        out = ns["_tui_build_percent_milestones"](conn)
    finally:
        conn.close()

    # Active segment is evt_id; only the post-credit row (1%, $5.00) shows.
    assert len(out) == 1, out
    assert out[0].percent == 1
    assert out[0].cumulative_cost_usd == 5.0


# ── Task 8: alerts dedup (independent post-credit fire) ──────────────


def test_post_credit_alert_fires_independently(ns):
    """Pre-credit milestone (threshold=3, ``alerted_at=NOW``,
    ``reset_event_id=0``) exists. Drive a post-credit milestone for
    threshold=3 captured after the event. The new row INSERTs at
    ``reset_event_id = event.id`` (cohabits with the pre-credit row
    via the new UNIQUE), ``alerted_at`` gets stamped on the new row
    via the segment-filtered UPDATE, the pre-credit row's
    ``alerted_at`` is untouched.

    This is the alert dedup verification: the alert pipeline reads
    rows via ``maybe_record_milestone``'s INSERT OR IGNORE + UPDATE
    set-then-dispatch flow. Without the segment-filtered UPDATE
    (Task 5), the post-credit row would land but the UPDATE would
    target a row keyed on (week, threshold) ignoring segment — the
    pre-credit row matches WHERE first, gets re-stamped, and the
    post-credit row stays NULL.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"
    pre_alerted_at = "2026-05-12T11:00:00Z"

    conn = ns["open_db"]()
    try:
        # Pre-credit milestone, already alerted.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 100.0, None, 1, 1,
             pre_alerted_at, 0),
        )
        # Cost snapshot (so the writer doesn't bail).
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.0,
        )
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        usage_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=3.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Configure alerts: weekly_thresholds = [3] so the threshold-3 alert
    # would fire. ``five_hour_thresholds`` must be non-empty per config
    # validation (paired-axis invariant). Set in config.json.
    ns["save_config"]({"alerts": {"enabled": True,
                                   "weekly_thresholds": [3],
                                   "five_hour_thresholds": [95]}})

    saved = {
        "id": usage_id,
        "weeklyPercent": 3.0,
        "weekStartDate": week_start_date,
        "weekEndDate": week_end_date,
        "weekStartAt": week_start_at,
        "weekEndAt": end_iso,
        "fiveHourPercent": None,
        "capturedAt": "2026-05-14T18:00:00Z",
    }
    ns["maybe_record_milestone"](saved)

    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id, alerted_at "
            "FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 3 "
            "ORDER BY reset_event_id ASC",
            (week_start_date,),
        ).fetchall()
        assert len(rows) == 2, rows
        # Pre-credit row: alerted_at preserved.
        assert rows[0]["reset_event_id"] == 0
        assert rows[0]["alerted_at"] == pre_alerted_at
        # Post-credit row: alerted_at stamped fresh (some recent ISO).
        assert rows[1]["reset_event_id"] == evt_id
        assert rows[1]["alerted_at"] is not None
        assert rows[1]["alerted_at"] != pre_alerted_at
    finally:
        conn.close()


def test_self_heal_probe_scoped_to_active_segment(ns):
    """When the live record-usage path bails on dedup-no-insert, the
    self-heal probe re-checks whether a milestone is owed. With a
    credited week + pre-credit MAX=67 in segment 0, the post-credit
    segment N has zero rows — a probe that didn't scope to the segment
    would silently no-op even though the post-credit threshold-1 row
    is owed.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"
    effective = "2026-05-14T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit milestones up to threshold 67.
        for pct in (1, 2, 67):
            conn.execute(
                "INSERT INTO percent_milestones "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, percent_threshold, "
                " cumulative_cost_usd, marginal_cost_usd, "
                " usage_snapshot_id, cost_snapshot_id, reset_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("2026-05-12T10:00:00Z", week_start_date, week_end_date,
                 week_start_at, end_iso, pct, 10.0 * pct, None, pct, pct, 0),
            )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective,
            old_week_end_at=end_iso,
        )
        _seed_cost_snapshot(
            conn,
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            cost_usd=12.0,
        )
        # Latest snapshot at 1% (post-credit) — but NO milestone row yet
        # in the post-credit segment. The live record-usage path will
        # bail on the dedup since this matches an existing snapshot
        # shape (one-row test setup); the self-heal probe must spot
        # the missing milestone.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T18:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=1.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive record-usage with the SAME percent as the latest snapshot
    # to trip the dedup path (forces self-heal to run).
    end_at_epoch = _epoch(end_iso)
    args = _record_usage_args(percent=1.0, resets_at=end_at_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    # The post-credit segment should now have a threshold-1 row.
    conn = ns["open_db"]()
    try:
        rows = conn.execute(
            "SELECT percent_threshold, reset_event_id "
            "FROM percent_milestones "
            "WHERE week_start_date = ? "
            "ORDER BY reset_event_id, percent_threshold",
            (week_start_date,),
        ).fetchall()
        # Pre-credit 1, 2, 67 + post-credit 1 = 4 rows total.
        assert len(rows) == 4, rows
        post = [r for r in rows if r["reset_event_id"] != 0]
        assert len(post) == 1
        assert post[0]["percent_threshold"] == 1
    finally:
        conn.close()
