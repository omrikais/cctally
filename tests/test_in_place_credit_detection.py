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
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        # In-place credit row shape (post-Bug-1 fix): old == effective,
        # new == cur_end_canon (DISTINCT values). The previous
        # old==new==cur_end shape collapsed the credited week to a
        # zero-width window in _apply_reset_events_to_weekrefs because
        # both pre_map[old] and post_map[new] fired on the same WeekRef.
        # See bin/_cctally_record.py:cmd_record_usage for the rationale.
        assert events[0]["new_week_end_at"] == end_iso
        assert events[0]["old_week_end_at"] == events[0]["effective_reset_at_utc"]
        assert events[0]["old_week_end_at"] != events[0]["new_week_end_at"]

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
        # In-place credit row shape (post-Bug-1 fix): old == effective,
        # new == cur_end (DISTINCT). See live-detection test
        # ``test_detection_fires_on_threshold`` for the same shape and
        # bin/_cctally_record.py:cmd_record_usage for rationale.
        assert events[0]["new_week_end_at"] == end_iso
        assert events[0]["old_week_end_at"] == events[0]["effective_reset_at_utc"]
        assert events[0]["old_week_end_at"] != events[0]["new_week_end_at"]
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


# ── Round-2 review regressions (v1.7.2) ──────────────────────────────


def test_event_row_old_is_effective_not_cur_end(ns, tmp_path):
    """Regression for round-2 review Bug 1: the in-place credit event
    row's ``old_week_end_at`` MUST be the effective reset moment
    (floor-to-hour of now), NOT ``cur_end_canon``. Old shape stored
    ``old==new==cur_end``, which collapsed the credited week to a
    zero-width window in ``_apply_reset_events_to_weekrefs`` because
    both ``pre_map[old]`` and ``post_map[new]`` fired on the same
    WeekRef. New shape is ``(effective, cur_end)`` — distinct values.

    Covers the live detection path; a sibling test
    ``test_backfill_event_row_old_is_effective_not_cur_end`` covers
    the backfill path.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

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

    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchone()
        assert row is not None, "live detection must have written an event row"
        assert row["old_week_end_at"] != row["new_week_end_at"], (
            "old==new collapses the credited week to a zero-width window"
            f" (got old={row['old_week_end_at']!r}, new={row['new_week_end_at']!r})"
        )
        assert row["old_week_end_at"] == row["effective_reset_at_utc"]
        assert row["new_week_end_at"] == end_iso
    finally:
        conn.close()


def test_backfill_event_row_old_is_effective_not_cur_end(ns):
    """Bug-1 regression in the backfill path. Seed a historical
    snapshot pattern (67% then 2%, same end) and call
    ``_backfill_week_reset_events`` directly. The synthesized event
    row must carry ``old_week_end_at == effective_reset_at_utc``,
    NOT ``old == new``.
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # Wipe any backfill-synthesized rows from the open_db()
        # invocation above; we want a clean slate. The backfill is
        # idempotent (UNIQUE + INSERT OR IGNORE + pre-check) so
        # re-running is safe.
        conn.execute("DELETE FROM week_reset_events")
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        row = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchone()
        assert row is not None, "backfill must have synthesized an event row"
        assert row["old_week_end_at"] != row["new_week_end_at"], (
            f"backfill wrote zero-width row: old={row['old_week_end_at']!r},"
            f" new={row['new_week_end_at']!r}"
        )
        assert row["old_week_end_at"] == row["effective_reset_at_utc"]
        assert row["new_week_end_at"] == end_iso
    finally:
        conn.close()


def test_reset_aware_clamp_handles_non_utc_event_offset(ns):
    """Regression for round-2 review Bug 2: the reset-aware DB clamp
    must use ``unixepoch()`` on both sides, not lex string compare.

    Setup: insert a ``week_reset_events`` row with
    ``effective_reset_at_utc='2026-03-01T14:00:00-03:00'`` (BACKFILL-
    shaped event written from a NEGATIVE-offset host before Bug 3 was
    fixed). The real UTC moment is ``2026-03-01T17:00:00Z`` (subtract a
    negative offset = add hours).

    Seed a pre-credit 67% snapshot at ``captured_at_utc=2026-03-01T15:00:00Z``:
      * Real time: 15:00 UTC — BEFORE the credit (17:00 UTC),
        i.e., legitimately pre-credit and MUST be filtered out of the
        post-credit segment's MAX.
      * Lex string compare: ``'2026-03-01T15:00:00Z'`` vs
        ``'2026-03-01T14:00:00-03:00'`` differs at char 12 (`5` > `4`)
        → ``'15:00:00Z'`` is lex-GREATER than ``'14:00:00-03:00'``,
        so a lex ``>=`` filter would WRONGLY INCLUDE the pre-credit
        67% row in the post-credit MAX.

    Drive ``cmd_record_usage(percent=4)``. Under the lex bug, the MAX
    includes 67 → 4 < 67 → ``should_insert = False`` → the new
    post-credit reading is silently dropped. With ``unixepoch()``
    wrapping both sides, 15:00Z = 15 UTC, 14:00-03:00 = 17 UTC; 15 < 17
    so the 67% row is correctly EXCLUDED, MAX = None (no post-credit
    rows yet), and the 4% reading lands.

    Negative-offset hosts are the failure mode where the bug bites the
    clamp; positive-offset hosts trip a different (also-bug) corner
    where real-time-post-credit rows get lex-EXCLUDED from MAX,
    which would also break the post-credit clamp but in a way that
    happens to allow this specific test percent to pass. Negative
    offsets surface the rejection path cleanly.
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # Pre-credit 67% snapshot — captured BEFORE the credit moment
        # in real time but LEX-greater than the effective string due
        # to the legacy `-03:00` offset on the event row.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-03-01T15:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # Event row with NEGATIVE-offset effective_reset_at_utc
        # (legacy shape that Bug 3 would have written from a host
        # like America/Buenos_Aires before the .astimezone(UTC) fix).
        # Real UTC equivalent: 2026-03-01T17:00:00Z.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
            ("2026-03-01T14:35:00-03:00",
             "2026-03-01T14:00:00-03:00",
             end_iso,
             "2026-03-01T14:00:00-03:00"),
        )
        conn.commit()
    finally:
        conn.close()

    args = _record_usage_args(percent=4.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE weekly_percent = 4.0"
        ).fetchone()[0]
        assert cnt == 1, (
            "post-credit 4% reading must land — the lex bug would have"
            " wrongly INCLUDED the pre-credit 67% row in the segment"
            " MAX (lex '15:00:00Z' > '14:00:00-03:00') and rejected 4%"
            " as a regression"
        )
    finally:
        conn.close()


def test_backfill_writes_effective_with_utc_offset(ns):
    """Regression for round-2 review Bug 3: the backfill's
    ``effective_reset_at_utc`` must be stored with ``+00:00`` offset,
    NOT host-local. ``parse_iso_datetime`` returns ``.astimezone()``
    (host-local fallback), so without the explicit ``.astimezone(UTC)``
    canonicalization before ``isoformat``, the column would be e.g.
    ``+03:00`` on a non-UTC host — breaking lex comparisons in any
    downstream consumer that hasn't yet been upgraded to ``unixepoch()``
    (Bug 2's defense applies to the clamp, but defense-in-depth on
    write keeps future readers safe).
    """
    end_iso, _ = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        conn.execute("DELETE FROM week_reset_events")
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-13T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T17:30:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=2.0,
        )
        conn.commit()
        ns["_backfill_week_reset_events"](conn)

        row = conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events"
        ).fetchone()
        assert row is not None
        eff = row["effective_reset_at_utc"]
        # Either trailing `+00:00` or `Z` (both denote UTC) is acceptable;
        # a host-local offset like `+03:00` is the bug.
        assert eff.endswith("+00:00") or eff.endswith("Z"), (
            f"effective_reset_at_utc must be UTC; got {eff!r}"
        )
    finally:
        conn.close()


def test_alerts_envelope_id_unique_across_segments(ns):
    """Regression for round-2 review Bug 4: the dashboard's alerts
    envelope id MUST include ``reset_event_id`` so pre-credit (segment
    0) and post-credit (segment N) alerts at the same
    (week_start_date, threshold) don't collide on the React key.

    Without the segment in the id, both rows render with the same
    ``id``, causing duplicate-key warnings and non-deterministic
    render order in ``<li key={a.id}>`` / ``<tr key={a.id}>``.
    """
    end_iso = "2026-05-16T05:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = week_start_date + "T05:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit alerted milestone (reset_event_id = 0).
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
             "2026-05-12T11:00:00Z", 0),
        )
        evt_id = _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective="2026-05-14T17:00:00+00:00",
            old_week_end_at="2026-05-14T17:00:00+00:00",
        )
        # Post-credit alerted milestone at the SAME (week, threshold)
        # but reset_event_id = evt_id. Under the old envelope-id
        # format these two rows would collide on
        # `weekly:<week>:<threshold>`.
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, percent_threshold, "
            " cumulative_cost_usd, marginal_cost_usd, "
            " usage_snapshot_id, cost_snapshot_id, "
            " alerted_at, reset_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-14T20:00:00Z", week_start_date, week_end_date,
             week_start_at, end_iso, 3, 5.0, None, 2, 2,
             "2026-05-14T20:00:00Z", evt_id),
        )
        conn.commit()

        dashboard_mod = ns["_cctally_dashboard"]
        envelope = dashboard_mod._build_alerts_envelope_array(conn)
        weekly = [a for a in envelope if a.get("axis") == "weekly"
                  and a.get("threshold") == 3
                  and a.get("context", {}).get("week_start_date") == week_start_date]
        assert len(weekly) == 2, (
            "both pre-credit and post-credit alerted rows must surface"
            f" (got {len(weekly)}: {weekly})"
        )
        ids = [a["id"] for a in weekly]
        assert len(set(ids)) == 2, (
            f"alerts envelope ids must be unique across segments; got {ids}"
        )
        assert all(s.startswith(f"weekly:{week_start_date}:3:") for s in ids), ids
    finally:
        conn.close()


# ── Round-3 user-test regressions (v1.7.2) ───────────────────────────


def test_credit_branch_defensive_cleanup_removes_stale_replays(ns, tmp_path):
    """Bug A: race-defensive cleanup in the credit-detection branch.

    Failure mode the user hit: between the moment Anthropic credited the
    user (effective_reset_at_utc) and the next cctally record-usage
    invocation, the EXTERNAL claude-statusline tool replayed stale
    pre-credit ``--percent 67`` values (its in-memory HWM cache hadn't
    caught up). Those replays landed at ``captured_at_utc >= effective``
    with ``weekly_percent == 67`` (the pre-credit MAX), then dominated
    the reset-aware clamp's MAX over the post-credit segment so
    legitimate fresh OAuth values were rejected.

    Fix: after the credit branch writes the event row + force-writes
    hwm-7d, run a defensive DELETE pass scoped to the same week, rows
    captured at-or-after ``effective``, with ``weekly_percent`` exactly
    matching the pre-credit value (round-to-1dp equality).
    """
    end_iso, end_epoch = _future_week_end_iso()
    week_start_date, week_end_date = _week_start_for(end_iso)

    conn = ns["open_db"]()
    try:
        # 1. Pre-credit baseline snapshot at 67%.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-14T10:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # 2. Race condition: the EXTERNAL statusline tool already wrote
        # POST-credit-time rows still carrying the stale 67% value
        # (these are the rows the defensive DELETE must clean up). Use
        # captured_at_utc values that we KNOW will be >= effective_iso
        # — the credit branch computes effective_iso as floor_to_hour
        # of `now`, so anything >= "now floored to hour" works. We use
        # the very-recent-past minute so the timestamps stamp AFTER
        # floor_to_hour(now). Critically, these rows are the MOST
        # RECENT prior snapshots, so the in-place credit detection
        # branch reads weekly_percent=67 as `prior_pct` (latest row by
        # captured_at_utc DESC) — that's exactly what fires the
        # detection (prior=67 vs new=2 = 65pp drop) and what the
        # cleanup uses for its strict-equality predicate.
        now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        floor_hour = now_utc.replace(minute=0, second=0)
        # Two stale-replay rows captured AT and just-after the floor.
        _seed_usage_snapshot(
            conn,
            captured_at_utc=floor_hour.isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        _seed_usage_snapshot(
            conn,
            captured_at_utc=(floor_hour + dt.timedelta(minutes=5))
            .isoformat().replace("+00:00", "Z"),
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        conn.commit()
    finally:
        conn.close()

    # Drive the credit branch. percent=2 < clamp MAX over the stale 67%
    # replays if the cleanup didn't run; with cleanup, the post-credit
    # segment's MAX is 5% (the legitimate survivor), and 2% is still
    # below that — but the seed snapshot path runs in this same
    # invocation. We mainly assert the DELETE happened.
    args = _record_usage_args(percent=2.0, resets_at=end_epoch)
    rc = ns["cmd_record_usage"](args)
    assert rc == 0

    conn = ns["open_db"]()
    try:
        # Event row written (proves we entered the credit branch).
        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, "
            "       effective_reset_at_utc FROM week_reset_events"
        ).fetchall()
        assert len(events) == 1, events
        effective_iso = events[0]["effective_reset_at_utc"]

        # The two 67% post-credit-time replay rows MUST be gone.
        stale_post_credit = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(?) "
            "  AND round(weekly_percent, 1) = 67.0",
            (week_start_date, effective_iso),
        ).fetchone()[0]
        assert stale_post_credit == 0, (
            "defensive cleanup should have deleted post-credit-time"
            " stale 67% replays"
        )

        # Pre-credit 67% row (captured BEFORE the credit moment) MUST
        # survive — its captured_at_utc is "2026-05-14T10:00:00Z" which
        # is well before any plausible floor_to_hour(now). The
        # equality predicate is fine; the filter that protects this
        # row is the timestamp half (>= effective_iso).
        pre_credit_67 = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? "
            "  AND captured_at_utc < ? "
            "  AND round(weekly_percent, 1) = 67.0",
            (week_start_date, effective_iso),
        ).fetchone()[0]
        assert pre_credit_67 == 1, (
            "pre-credit 67% rows must survive (clamp's reset-aware"
            " filter handles them)"
        )

        # The post-credit 2% seed snapshot MUST have landed — proves
        # the cleanup unblocked the seed. Without cleanup, the
        # reset-aware clamp's MAX would still see the post-credit-time
        # 67% rows (they're at-or-after effective_reset_at_utc and
        # part of the segment's MAX window), and the 2% reading would
        # be rejected as a regression.
        seed_landed = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE week_start_date = ? AND round(weekly_percent, 1) = 2.0",
            (week_start_date,),
        ).fetchone()[0]
        assert seed_landed == 1, (
            "post-credit seed snapshot must land after cleanup"
        )
    finally:
        conn.close()


# ── Round-3 Bug B: pre-credit ref synthesis ──────────────────────────


def test_apply_reset_events_synthesizes_pre_credit_ref(ns):
    """Bug B: a credited week must render as TWO refs after
    ``_apply_reset_events_to_weekrefs`` — a pre-credit segment closed
    at ``effective_reset_at_utc`` AND the existing post-credit segment.
    Detected via the in-place credit row shape
    ``old_week_end_at == effective_reset_at_utc``.
    """
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Seed an in-place credit event row (old==effective shape).
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        # Build ONE WeekRef matching the credited week.
        ref = ns["make_week_ref"](
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
        )
        out = ns["_apply_reset_events_to_weekrefs"](conn, [ref])
    finally:
        conn.close()

    # Two refs returned for the credited week.
    assert len(out) == 2, f"expected 2 refs (pre + post), got {len(out)}: {out}"

    # First ref returned is the POST-credit segment (preserves
    # ref-slot ordering in the DESC-sorted output of get_recent_weeks).
    post = out[0]
    pre = out[1]

    assert post.week_start_at == effective_iso
    assert post.week_end_at == end_iso

    assert pre.week_start_at == week_start_at
    assert pre.week_end_at == effective_iso

    # Both refs share the same lookup keys (week_start date + the
    # `key` field) so per-segment milestone readers can still join on
    # ``reset_event_id``.
    assert pre.week_start == post.week_start
    assert pre.key == post.key


def test_apply_reset_events_does_not_split_for_boundary_shift(ns):
    """Regression guard: an event with ``old != effective`` (the
    classic boundary-shift case, where Anthropic moved ``resets_at``
    forward before the natural end) must NOT trigger the new split
    behavior. The pre_map / post_map logic for boundary shifts is
    unchanged.
    """
    # Two distinct weeks for the boundary-shift event.
    pre_end_iso = "2026-05-10T17:00:00+00:00"
    new_end_iso = "2026-05-12T19:00:00+00:00"
    effective_iso = "2026-05-12T19:00:00+00:00"  # different from old
    # The old end is OLDER than effective (classic shift); critically
    # `old != effective` (the marker that distinguishes shifts from
    # in-place credits).
    old_end_iso = pre_end_iso

    pre_week_start_at = "2026-05-03T17:00:00+00:00"
    post_week_start_at = "2026-05-05T19:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_reset_event(
            conn,
            new_week_end_at=new_end_iso,
            effective=effective_iso,
            old_week_end_at=old_end_iso,
        )
        pre_ref = ns["make_week_ref"](
            week_start_date="2026-05-03",
            week_end_date="2026-05-10",
            week_start_at=pre_week_start_at,
            week_end_at=old_end_iso,
        )
        post_ref = ns["make_week_ref"](
            week_start_date="2026-05-05",
            week_end_date="2026-05-12",
            week_start_at=post_week_start_at,
            week_end_at=new_end_iso,
        )
        out = ns["_apply_reset_events_to_weekrefs"](
            conn, [post_ref, pre_ref],
        )
    finally:
        conn.close()

    # Exactly 2 refs (one per input). No synthesized split.
    assert len(out) == 2, out

    # Post-reset ref: week_start_at rewritten to effective.
    post_out = next(r for r in out if r.week_end_at == new_end_iso)
    assert post_out.week_start_at == effective_iso

    # Pre-reset ref: week_end_at rewritten to effective.
    pre_out = next(r for r in out if r.week_start_at == pre_week_start_at)
    assert pre_out.week_end_at == effective_iso


def test_trend_table_shows_pre_credit_row(ns, capsys):
    """End-to-end: with an in-place credit event row + seeded
    weekly_usage_snapshots, ``cmd_report`` (JSON mode) must emit TWO
    trend rows for the credited week — pre-credit (closed at
    ``effective``) and post-credit (opened at ``effective``).

    Verifies the per-segment cost paths in ``cmd_report`` don't crash
    on a duplicated lookup key (both refs share ``week_start_date``).
    """
    import json
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit snapshot at 67%.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T16:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # Post-credit snapshot at 4%.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=4.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](argparse.Namespace(
        weeks=1,
        sync_current=False,
        week_start_name=None,
        mode="auto",
        offline=True,
        project=None,
        json=True,
        detail=False,
        format=None,
        theme=None,
        reveal_projects=False,
        no_branding=False,
        output=None,
        copy=False,
        open=False,
        tz=None,
    ))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    trend = payload["trend"]
    # 2 refs for the credited week (pre + post segment).
    credited = [
        r for r in trend if r["weekStartDate"] == week_start_date
    ]
    assert len(credited) == 2, f"expected 2 trend rows, got: {credited}"

    # Identify pre vs post by week_end_at.
    pre = next(r for r in credited if r["weekEndAt"] == effective_iso)
    post = next(r for r in credited if r["weekEndAt"] == end_iso)

    # Pre-credit row carries the pre-credit usage value (67%).
    assert pre["weeklyPercent"] == 67.0, pre
    # Post-credit row carries the post-credit usage value (4%).
    assert post["weeklyPercent"] == 4.0, post


# ── Round-3 Bug C: cctally blocks uses API-anchored data ─────────────


def _seed_five_hour_block_row(
    conn,
    *,
    five_hour_resets_at: str,
    block_start_at: str,
    five_hour_window_key: int,
    final_pct: float = 50.0,
) -> int:
    """Insert a minimal ``five_hour_blocks`` row and return its id."""
    cur = conn.execute(
        "INSERT INTO five_hour_blocks "
        "(five_hour_window_key, five_hour_resets_at, block_start_at, "
        " first_observed_at_utc, last_observed_at_utc, "
        " final_five_hour_percent, is_closed, "
        " created_at_utc, last_updated_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (five_hour_window_key, five_hour_resets_at, block_start_at,
         block_start_at, block_start_at, final_pct,
         block_start_at, block_start_at),
    )
    return int(cur.lastrowid)


def test_blocks_anchor_picks_five_hour_blocks_when_available(ns):
    """Bug C: ``_load_recorded_five_hour_windows`` must also pull
    ``five_hour_resets_at`` from the canonical ``five_hour_blocks``
    rollup table — and the canonical entry must dominate over any
    jittered raw value sharing the same 10-minute floor.
    """
    # Use a moment in the recent past so it falls inside the default
    # range (2020-01-01 → now widened by 5h).
    canonical_resets = "2026-05-15T22:50:00+00:00"  # 17:50Z block start
    block_start = "2026-05-15T17:50:00+00:00"

    conn = ns["open_db"]()
    try:
        # Seed a weekly_usage_snapshots row whose
        # `five_hour_resets_at` is JITTERED away from the canonical
        # value by less than 10 minutes — these should collapse to the
        # same floored bucket and the canonical (heavy-weight) entry
        # should dominate.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-15T20:00:00Z", "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             "2026-05-15T22:48:00+00:00"),  # 2min jitter from canonical
        )
        _seed_five_hour_block_row(
            conn,
            five_hour_resets_at=canonical_resets,
            block_start_at=block_start,
            five_hour_window_key=int(
                dt.datetime.fromisoformat(canonical_resets).timestamp()
            ),
        )
        conn.commit()
    finally:
        conn.close()

    range_start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    range_end = dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc)
    windows = ns["_load_recorded_five_hour_windows"](range_start, range_end)

    assert len(windows) >= 1, f"expected at least one window, got {windows}"

    # The returned anchor must be the floored canonical value (22:50Z).
    expected_floor = dt.datetime(
        2026, 5, 15, 22, 50, 0, tzinfo=dt.timezone.utc,
    )
    assert expected_floor in windows, (
        f"canonical 22:50Z anchor missing from windows: {windows}"
    )


def test_blocks_anchor_falls_back_when_no_five_hour_blocks_row(ns):
    """Bug C regression guard: when the ``five_hour_blocks`` table is
    empty (no canonical anchors available), ``_load_recorded_five_hour_windows``
    must still return raw-snapshot anchors. Heuristic behavior unchanged
    in this case.
    """
    raw_resets = "2026-05-15T22:48:00+00:00"

    conn = ns["open_db"]()
    try:
        # Seed several weekly_usage_snapshots rows with the same raw
        # reset (count >= 1 to survive the
        # _select_non_overlapping_recorded_windows filter).
        for i in range(3):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, "
                " source, payload_json, "
                " five_hour_percent, five_hour_resets_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"2026-05-15T20:0{i}:00Z", "2026-05-09", "2026-05-16",
                 "2026-05-09T17:00:00+00:00",
                 "2026-05-16T17:00:00+00:00",
                 5.0 + i, "test", "{}", 25.0 + i, raw_resets),
            )
        conn.execute("DELETE FROM five_hour_blocks")
        conn.commit()
    finally:
        conn.close()

    range_start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    range_end = dt.datetime(2026, 5, 16, tzinfo=dt.timezone.utc)
    windows = ns["_load_recorded_five_hour_windows"](range_start, range_end)

    # Raw anchor (floored to 22:40Z because :48 floors to :40 in
    # 10-minute buckets).
    expected_floor = dt.datetime(
        2026, 5, 15, 22, 40, 0, tzinfo=dt.timezone.utc,
    )
    assert expected_floor in windows, (
        f"raw-snapshot anchor missing without five_hour_blocks: {windows}"
    )


# ── Round-4 Bug D: cmd_report current-row picks post-credit ref ───────


def test_cmd_report_current_row_picks_post_credit_for_credited_week(ns, capsys):
    """Bug D (v1.7.2 round-4): on the user's live DB, ``cmd_report``'s
    "current week" summary box rendered the PRE-credit row (67%, the
    closed segment) instead of the POST-credit row (4%, the live
    segment). Root cause: both refs share ``WeekRef.key``, the match
    predicate ``week_ref.key == current_ref.key`` matched both, and
    last-write-wins picked the wrong row.

    Fix: route ``current_ref`` through ``_apply_reset_events_to_weekrefs``
    so its ``week_start_at`` reflects the post-credit segment, then
    match on BOTH ``key`` AND ``week_start_at``.
    """
    import json
    end_iso = "2026-05-16T17:00:00+00:00"
    effective_iso = "2026-05-15T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        # Pre-credit snapshot at 67%, captured BEFORE the effective
        # reset moment.
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T16:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=67.0,
        )
        # Post-credit snapshot at 4%, captured AFTER the effective
        # reset moment (so it sorts as `latest_usage`).
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=4.0,
        )
        _seed_reset_event(
            conn,
            new_week_end_at=end_iso,
            effective=effective_iso,
            old_week_end_at=effective_iso,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](argparse.Namespace(
        weeks=1,
        sync_current=False,
        week_start_name=None,
        mode="auto",
        offline=True,
        project=None,
        json=True,
        detail=False,
        format=None,
        theme=None,
        reveal_projects=False,
        no_branding=False,
        output=None,
        copy=False,
        open=False,
        tz=None,
    ))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    current = payload["current"]
    assert current is not None, payload
    # The "current" row must be the POST-credit segment:
    # week_start_at == effective_iso, weeklyPercent == 4.0.
    assert current["weekStartAt"] == effective_iso, current
    assert current["weekEndAt"] == end_iso, current
    assert current["weeklyPercent"] == 4.0, current
    # The currentWeek envelope mirrors the same post-credit anchor.
    assert payload["currentWeek"]["weekStartAt"] == effective_iso


def test_cmd_report_current_row_legacy_uncredited_week(ns, capsys):
    """Bug D regression guard: an uncredited week (single ref, no
    reset event row) must still pick its sole ref as the current row.
    The round-4 fix re-routes ``current_ref`` through
    ``_apply_reset_events_to_weekrefs`` but with no events the function
    is a no-op (returns ``refs`` unchanged), so non-credited weeks are
    unaffected.
    """
    import json
    end_iso = "2026-05-16T17:00:00+00:00"
    week_start_date, week_end_date = _week_start_for(end_iso)
    week_start_at = "2026-05-09T17:00:00+00:00"

    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-05-15T20:00:00Z",
            week_start_date=week_start_date,
            week_end_date=week_end_date,
            week_start_at=week_start_at,
            week_end_at=end_iso,
            weekly_percent=42.0,
        )
        conn.commit()
    finally:
        conn.close()

    rc = ns["cmd_report"](argparse.Namespace(
        weeks=1,
        sync_current=False,
        week_start_name=None,
        mode="auto",
        offline=True,
        project=None,
        json=True,
        detail=False,
        format=None,
        theme=None,
        reveal_projects=False,
        no_branding=False,
        output=None,
        copy=False,
        open=False,
        tz=None,
    ))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    current = payload["current"]
    assert current is not None
    assert current["weekStartAt"] == week_start_at
    assert current["weekEndAt"] == end_iso
    assert current["weeklyPercent"] == 42.0


# ── Round-4 Bug E: cmd_blocks active row swaps to canonical ──────────


def test_blocks_active_uses_five_hour_blocks_when_anchor_differs(
    ns, monkeypatch
):
    """Bug E (v1.7.2 round-4): when the ACTIVE 5h block is heuristic-
    anchored (e.g. activity restarted at 23:00 IDT after a gap) but a
    canonical ``five_hour_blocks`` row pins the current API window
    elsewhere (e.g. 20:50 IDT), ``cmd_blocks`` must surface the
    API-anchored window for the ACTIVE row — heuristic and canonical
    can sit in different 10-minute floor buckets, so the round-3
    anchor-overlay in ``_load_recorded_five_hour_windows`` doesn't
    catch this case.
    """
    # API-anchored window: 20:50 IDT (17:50Z) start, 01:50 IDT (22:50Z) end.
    # Heuristic anchor would be at 23:00 IDT (20:00Z) — 130 min later.
    canonical_block_start = "2026-05-15T17:50:00+00:00"
    canonical_resets_at = "2026-05-15T22:50:00+00:00"
    canonical_key = int(dt.datetime.fromisoformat(canonical_resets_at).timestamp())

    # Pin "now" between 23:00 IDT (heuristic anchor) and the canonical
    # window end (22:50Z), so the canonical window is still ACTIVE.
    now_utc = dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    conn = ns["open_db"]()
    try:
        # Seed a weekly_usage_snapshots row that pins the live
        # five_hour_window_key. _maybe_swap_active_block_to_canonical
        # picks the latest snapshot's key.
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at, "
            " five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_utc.isoformat(timespec="seconds"),
             "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             canonical_resets_at, canonical_key),
        )
        # Seed the canonical five_hour_blocks row.
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, "
            " created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (canonical_key, canonical_resets_at, canonical_block_start,
             canonical_block_start, canonical_block_start,
             25.0, canonical_block_start, canonical_block_start),
        )
        conn.commit()
    finally:
        conn.close()

    # Build a heuristic ACTIVE block at 23:00 IDT — emulates the post-
    # gap reanchoring `_group_entries_into_blocks` produces from real
    # JSONL activity.
    Block = ns["Block"]
    heuristic_start = dt.datetime(
        2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc,
    )  # 23:00 IDT
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    blocks = [
        Block(
            start_time=heuristic_start,
            end_time=heuristic_end,
            actual_end_time=now_utc,
            is_active=True,
            is_gap=False,
            entries_count=3,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_tokens=300,
            cost_usd=1.23,
            models=["claude-opus-4-7"],
            burn_rate=None,
            projection=None,
            anchor="heuristic",
        ),
    ]

    ns["_maybe_swap_active_block_to_canonical"](blocks, now=now_utc)

    # The active block's times must be rewritten to the canonical
    # window and its anchor flipped to "recorded".
    active = blocks[0]
    expected_start = dt.datetime.fromisoformat(
        canonical_block_start
    ).astimezone(dt.timezone.utc)
    expected_end = dt.datetime.fromisoformat(
        canonical_resets_at
    ).astimezone(dt.timezone.utc)
    assert active.start_time == expected_start, active.start_time
    assert active.end_time == expected_end, active.end_time
    assert active.anchor == "recorded", active.anchor


def test_blocks_active_falls_back_when_no_canonical_row(ns, monkeypatch):
    """Bug E regression guard: when no ``five_hour_blocks`` row matches
    the live key (or the table is empty), the heuristic ACTIVE block
    is preserved verbatim — same times, ``anchor="heuristic"``, so the
    renderer keeps the ``~`` prefix.
    """
    now_utc = dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    # No five_hour_blocks row at all. (Setup doesn't need to seed
    # weekly_usage_snapshots either — the function returns early when
    # no snapshot has a five_hour_window_key.)
    Block = ns["Block"]
    heuristic_start = dt.datetime(
        2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc,
    )
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    blocks = [
        Block(
            start_time=heuristic_start,
            end_time=heuristic_end,
            actual_end_time=now_utc,
            is_active=True,
            is_gap=False,
            entries_count=3,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_tokens=300,
            cost_usd=1.23,
            models=["claude-opus-4-7"],
            burn_rate=None,
            projection=None,
            anchor="heuristic",
        ),
    ]

    ns["_maybe_swap_active_block_to_canonical"](blocks, now=now_utc)

    # Active block unchanged.
    active = blocks[0]
    assert active.start_time == heuristic_start
    assert active.end_time == heuristic_end
    assert active.anchor == "heuristic"


def test_blocks_active_skips_when_canonical_window_already_closed(
    ns, monkeypatch
):
    """Bug E corner case: if the canonical ``five_hour_blocks`` row's
    ``five_hour_resets_at`` is already in the past relative to ``now``,
    the canonical block is closed — the heuristic active block reflects
    a NEW window's worth of real activity and must NOT be overwritten.
    """
    # Canonical window: 12:50Z → 17:50Z (already closed at 20:30Z).
    canonical_block_start = "2026-05-15T12:50:00+00:00"
    canonical_resets_at = "2026-05-15T17:50:00+00:00"
    canonical_key = int(dt.datetime.fromisoformat(canonical_resets_at).timestamp())
    now_utc = dt.datetime(2026, 5, 15, 20, 30, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setenv("CCTALLY_AS_OF", now_utc.isoformat(timespec="seconds"))

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, "
            " week_start_at, week_end_at, weekly_percent, "
            " source, payload_json, "
            " five_hour_percent, five_hour_resets_at, "
            " five_hour_window_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_utc.isoformat(timespec="seconds"),
             "2026-05-09", "2026-05-16",
             "2026-05-09T17:00:00+00:00", "2026-05-16T17:00:00+00:00",
             5.0, "test", "{}", 25.0,
             canonical_resets_at, canonical_key),
        )
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, is_closed, "
            " created_at_utc, last_updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (canonical_key, canonical_resets_at, canonical_block_start,
             canonical_block_start, canonical_block_start,
             80.0, canonical_block_start, canonical_block_start),
        )
        conn.commit()
    finally:
        conn.close()

    Block = ns["Block"]
    heuristic_start = dt.datetime(
        2026, 5, 15, 20, 0, 0, tzinfo=dt.timezone.utc,
    )
    heuristic_end = heuristic_start + dt.timedelta(hours=5)
    blocks = [
        Block(
            start_time=heuristic_start,
            end_time=heuristic_end,
            actual_end_time=now_utc,
            is_active=True,
            is_gap=False,
            entries_count=3,
            input_tokens=100,
            output_tokens=200,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            total_tokens=300,
            cost_usd=1.23,
            models=["claude-opus-4-7"],
            burn_rate=None,
            projection=None,
            anchor="heuristic",
        ),
    ]

    ns["_maybe_swap_active_block_to_canonical"](blocks, now=now_utc)

    active = blocks[0]
    # Heuristic times preserved; anchor unchanged.
    assert active.start_time == heuristic_start
    assert active.end_time == heuristic_end
    assert active.anchor == "heuristic"
