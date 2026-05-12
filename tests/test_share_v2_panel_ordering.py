"""Regression tests for share-v2 panel_data builders against the
newest-first orientation of `DataSnapshot.weekly_periods`,
`DataSnapshot.monthly_periods`, `DataSnapshot.daily_panel`, and
`DataSnapshot.blocks_panel`.

Codex review on PR #35 caught five identical mis-slices: the builders
treated those arrays as oldest-first (using `[-N:]` / no reverse) but
the sync-thread producers return them newest-first. Templates downstream
(`bin/_lib_share_templates.py`) expect oldest→newest in panel_data, with
`weeks[-1]`/`days[-1]`/`months[-1]` as the right-edge anchor,
`progression[-1]` as "today," and `recent_blocks` plotted left→right via
`enumerate()`. These tests pin that contract.
"""
from __future__ import annotations

import datetime as dt
import types

from conftest import load_script


def _ns():
    return types.SimpleNamespace(**load_script())


def _weekly_row(ns, *, weeks_ago: int, is_current: bool, cost: float):
    """One newest-first WeeklyPeriodRow at `today - 7*weeks_ago days`."""
    today = dt.date(2026, 5, 11)
    start_date = today - dt.timedelta(days=7 * weeks_ago)
    end_date = start_date + dt.timedelta(days=7)
    return ns.WeeklyPeriodRow(
        label=start_date.strftime("%m-%d"),
        cost_usd=cost,
        total_tokens=0, input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        used_pct=50.0,
        dollar_per_pct=cost / 50.0 if cost else None,
        delta_cost_pct=None,
        is_current=is_current,
        models=[],
        week_start_at=f"{start_date.isoformat()}T00:00:00+00:00",
        week_end_at=f"{end_date.isoformat()}T00:00:00+00:00",
    )


def _daily_row(ns, *, days_ago: int, cost: float):
    """One newest-first DailyPanelRow for `today - days_ago`."""
    today = dt.date(2026, 5, 11)
    d = today - dt.timedelta(days=days_ago)
    return ns.DailyPanelRow(
        date=d.isoformat(),
        label=d.strftime("%m-%d"),
        cost_usd=cost,
        is_today=(days_ago == 0),
        intensity_bucket=0,
        models=[],
    )


def _monthly_row(ns, *, months_ago: int, cost: float):
    """One newest-first MonthlyPeriodRow at `today.month - months_ago`."""
    base = dt.date(2026, 5, 1)
    year, month = base.year, base.month - months_ago
    while month <= 0:
        month += 12
        year -= 1
    label = f"{year:04d}-{month:02d}"
    return ns.MonthlyPeriodRow(
        label=label,
        cost_usd=cost,
        total_tokens=0, input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        delta_cost_pct=None,
        is_current=(months_ago == 0),
        models=[],
    )


def _block_row(ns, *, hours_ago: int, cost: float, is_active: bool = False):
    """One newest-first BlocksPanelRow `hours_ago` 5h-blocks back."""
    now = dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc)
    start = now - dt.timedelta(hours=5 * hours_ago)
    end = start + dt.timedelta(hours=5)
    return ns.BlocksPanelRow(
        start_at=start.isoformat(),
        end_at=end.isoformat(),
        anchor="recorded",
        is_active=is_active,
        cost_usd=cost,
        models=[],
        label=start.strftime("%H:%M %b %d"),
    )


def _snapshot(ns, *, weekly_periods=(), monthly_periods=(),
              daily_panel=(), blocks_panel=(), current_week=None):
    return ns.DataSnapshot(
        current_week=current_week,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc),
        weekly_periods=list(weekly_periods),
        monthly_periods=list(monthly_periods),
        daily_panel=list(daily_panel),
        blocks_panel=list(blocks_panel),
    )


# ----------------------------------------------------------------------
# Finding 1: weekly share — must keep the newest 8 weeks, oldest-first,
# with current_week_index pointing at the actual current week.
# ----------------------------------------------------------------------

def test_weekly_share_panel_keeps_newest_8_weeks_oldest_first():
    ns = _ns()
    # 12 weeks, newest-first (mirrors _dashboard_build_weekly_periods).
    # Mark weeks_ago=0 (the newest) as the current week.
    rows = [
        _weekly_row(ns, weeks_ago=i, is_current=(i == 0), cost=100.0 + i)
        for i in range(12)
    ]
    snap = _snapshot(ns, weekly_periods=rows)

    out = ns._build_weekly_share_panel_data({}, snap)

    # Eight weeks emitted (clipped from 12).
    assert len(out["weeks"]) == 8

    # Output is oldest → newest: start_date strictly ascends.
    starts = [w["start_date"] for w in out["weeks"]]
    assert starts == sorted(starts), f"weeks not chronological: {starts}"

    # The newest week (weeks_ago=0) is the right-edge anchor.
    newest_start = (dt.date(2026, 5, 11)).isoformat()
    assert out["weeks"][-1]["start_date"] == newest_start

    # current_week_index points at the rightmost cell (the current week).
    assert out["current_week_index"] == len(out["weeks"]) - 1


def test_weekly_share_panel_handles_fewer_than_8_weeks():
    """No clipping needed; still oldest→newest with correct current_idx."""
    ns = _ns()
    rows = [
        _weekly_row(ns, weeks_ago=i, is_current=(i == 0), cost=10.0 * (i + 1))
        for i in range(3)
    ]
    snap = _snapshot(ns, weekly_periods=rows)
    out = ns._build_weekly_share_panel_data({}, snap)
    assert len(out["weeks"]) == 3
    starts = [w["start_date"] for w in out["weeks"]]
    assert starts == sorted(starts)
    assert out["current_week_index"] == 2  # rightmost = current


# ----------------------------------------------------------------------
# Finding 2: daily share — must keep the most recent 7 days, oldest-first.
# ----------------------------------------------------------------------

def test_daily_share_panel_keeps_most_recent_7_days_oldest_first():
    ns = _ns()
    # 30 days newest-first; days_ago=0 .. 29.
    rows = [_daily_row(ns, days_ago=i, cost=10.0 + i) for i in range(30)]
    snap = _snapshot(ns, daily_panel=rows)

    out = ns._build_daily_share_panel_data({}, snap)

    assert len(out["days"]) == 7

    # Oldest → newest.
    dates = [d["date"] for d in out["days"]]
    assert dates == sorted(dates), f"days not chronological: {dates}"

    # Last entry is today (days_ago=0); first entry is 6 days ago.
    today = dt.date(2026, 5, 11).isoformat()
    six_ago = (dt.date(2026, 5, 11) - dt.timedelta(days=6)).isoformat()
    assert out["days"][-1]["date"] == today
    assert out["days"][0]["date"] == six_ago


# ----------------------------------------------------------------------
# Finding 3: current-week progression — must iterate chronologically so
# the template's `progression[-1]` is today, not the week's start day.
# ----------------------------------------------------------------------

def test_current_week_progression_is_chronological():
    ns = _ns()
    # Week began 4 days ago (covers 5 of the 30 daily rows: days_ago 0..4).
    week_start = dt.datetime(2026, 5, 7, 0, 0, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)
    cw = ns.TuiCurrentWeek(
        week_start_at=week_start,
        week_end_at=week_end,
        used_pct=42.0,
        five_hour_pct=None,
        five_hour_resets_at=None,
        spent_usd=123.45,
        dollars_per_percent=2.94,
        latest_snapshot_at=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc),
    )
    rows = [_daily_row(ns, days_ago=i, cost=10.0 + i) for i in range(30)]
    snap = _snapshot(ns, daily_panel=rows, current_week=cw)

    out = ns._build_current_week_share_panel_data({}, snap)

    progression = out["daily_progression"]
    # Five days inside the current week (today + 4 prior days).
    assert len(progression) == 5

    # Oldest → newest.
    dates = [p["date"] for p in progression]
    assert dates == sorted(dates), f"progression not chronological: {dates}"

    # Template uses `progression[-1].date` as the "through" label → today.
    today = dt.date(2026, 5, 11).isoformat()
    week_start_iso = dt.date(2026, 5, 7).isoformat()
    assert progression[-1]["date"] == today
    assert progression[0]["date"] == week_start_iso


# ----------------------------------------------------------------------
# Finding 4: monthly share — must reverse to oldest-first so the template's
# months[0] = period start and months[-1] = most recent month.
# ----------------------------------------------------------------------

def test_monthly_share_panel_is_oldest_first():
    ns = _ns()
    # 12 months newest-first (months_ago=0 .. 11).
    rows = [_monthly_row(ns, months_ago=i, cost=10.0 + i) for i in range(12)]
    snap = _snapshot(ns, monthly_periods=rows)

    out = ns._build_monthly_share_panel_data({}, snap)

    assert len(out["months"]) == 12

    # Oldest → newest: "YYYY-MM" labels strictly ascend.
    labels = [m["month"] for m in out["months"]]
    assert labels == sorted(labels), f"months not chronological: {labels}"

    # The newest month (months_ago=0) is the right-edge anchor.
    assert out["months"][-1]["month"] == "2026-05"
    # 12 months back from 2026-05 = 2025-06.
    assert out["months"][0]["month"] == "2025-06"


# ----------------------------------------------------------------------
# Finding 5: blocks share — must keep the most recent 8 blocks (not the
# oldest 8), reversed to oldest-first so the template's `enumerate(recent)`
# plots left→right time order.
# ----------------------------------------------------------------------

def test_blocks_share_panel_keeps_most_recent_8_blocks_oldest_first():
    ns = _ns()
    # 20 blocks newest-first; hours_ago = 0 .. 19 (5h apart). Mark the
    # newest as active.
    rows = [
        _block_row(ns, hours_ago=i, cost=1.0 + i, is_active=(i == 0))
        for i in range(20)
    ]
    snap = _snapshot(ns, blocks_panel=rows)

    out = ns._build_blocks_share_panel_data({}, snap)

    # Eight recent blocks, oldest → newest.
    recent = out["recent_blocks"]
    assert len(recent) == 8

    starts = [b["start_at"] for b in recent]
    assert starts == sorted(starts), f"blocks not chronological: {starts}"

    # Newest block is the right-edge anchor (matches the active block).
    cb_start = out["current_block"]["start_at"]
    assert recent[-1]["start_at"] == cb_start

    # Selecting MOST RECENT 8: the oldest in `recent` should be hours_ago=7,
    # not hours_ago=19 (which would be the bug — taking the oldest slice).
    expected_oldest = (dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc)
                       - dt.timedelta(hours=5 * 7)).isoformat()
    assert recent[0]["start_at"] == expected_oldest


def test_blocks_share_panel_handles_fewer_than_8_blocks():
    """No clipping needed; still oldest→newest."""
    ns = _ns()
    rows = [
        _block_row(ns, hours_ago=i, cost=1.0 + i, is_active=(i == 0))
        for i in range(3)
    ]
    snap = _snapshot(ns, blocks_panel=rows)
    out = ns._build_blocks_share_panel_data({}, snap)
    recent = out["recent_blocks"]
    assert len(recent) == 3
    starts = [b["start_at"] for b in recent]
    assert starts == sorted(starts)
