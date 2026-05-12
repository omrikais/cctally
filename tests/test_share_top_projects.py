"""Unit tests for top_projects aggregation across share-v2 builders.

Codex P2 caught five live builders returning `top_projects: []` while
the templates promised a Top-N project table. The fix is a per-builder
`session_entries` aggregation routed through
`_share_top_projects_for_range`.

These tests spy on the helper to verify (a) each builder calls it at
all, and (b) calls with the right time-window per panel. Helper unit
tests confirm the bucketing + sort + cap.
"""
from __future__ import annotations

import datetime as dt

from conftest import load_script


def _now():
    return dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc)


def _empty_snap(ns, **overrides):
    """DataSnapshot stub — only the fields each builder reads need to be set."""
    DataSnapshot = ns["DataSnapshot"]
    defaults = dict(
        current_week=None, forecast=None,
        trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=_now(),
    )
    defaults.update(overrides)
    return DataSnapshot(**defaults)


# ----------------------------------------------------------------------
# _share_top_projects_for_range — sorts, bucket-merges, caps.
# ----------------------------------------------------------------------

def _make_entry(ns, *, project, cost):
    """Fake _JoinedClaudeEntry — only the fields the helper reads."""
    Entry = ns["_JoinedClaudeEntry"]
    return Entry(
        timestamp=_now(),
        model="claude-sonnet-4-5",
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        source_path="",
        session_id=None,
        project_path=project,
        cost_usd=cost,  # _calculate_entry_cost accepts cost_usd as-is
    )


def test_top_projects_helper_sorts_buckets_and_caps(monkeypatch):
    ns = load_script()
    # Build 25 entries across 25 projects — should cap at 20 (builder cap).
    entries = [
        _make_entry(ns, project=f"/p/{i}", cost=float(25 - i))
        for i in range(25)
    ]
    monkeypatch.setitem(ns, "get_claude_session_entries",
                        lambda *a, **kw: entries)

    out = ns["_share_top_projects_for_range"](_now(), _now() + dt.timedelta(days=1))
    # Cap at 20, descending by cost.
    assert len(out) == 20
    # Most expensive first.
    assert out[0] == ("/p/0", 25.0)
    # Strictly descending costs.
    costs = [c for _, c in out]
    assert costs == sorted(costs, reverse=True)


def test_top_projects_helper_merges_same_project(monkeypatch):
    ns = load_script()
    entries = [
        _make_entry(ns, project="/p/a", cost=1.0),
        _make_entry(ns, project="/p/b", cost=2.5),
        _make_entry(ns, project="/p/a", cost=3.0),  # same project, accumulates
    ]
    monkeypatch.setitem(ns, "get_claude_session_entries",
                        lambda *a, **kw: entries)
    out = ns["_share_top_projects_for_range"](_now(), _now())
    # /p/a: 4.0 (1 + 3); /p/b: 2.5
    assert out == [("/p/a", 4.0), ("/p/b", 2.5)]


def test_top_projects_helper_null_project_collapses_to_unknown(monkeypatch):
    ns = load_script()
    entries = [
        _make_entry(ns, project=None, cost=1.5),
        _make_entry(ns, project="", cost=2.5),  # empty also falsy → '(unknown)'
        _make_entry(ns, project="/p/real", cost=0.5),
    ]
    monkeypatch.setitem(ns, "get_claude_session_entries",
                        lambda *a, **kw: entries)
    out = ns["_share_top_projects_for_range"](_now(), _now())
    # '(unknown)' collects 4.0, /p/real has 0.5.
    assert out == [("(unknown)", 4.0), ("/p/real", 0.5)]


def test_top_projects_helper_empty_entries_returns_empty(monkeypatch):
    ns = load_script()
    monkeypatch.setitem(ns, "get_claude_session_entries",
                        lambda *a, **kw: [])
    out = ns["_share_top_projects_for_range"](_now(), _now())
    assert out == []


def test_top_projects_helper_swallows_query_errors(monkeypatch):
    """If `get_claude_session_entries` raises (e.g., HOME unset), the
    helper returns []. Share render must not blow up because the rollup
    can't be computed."""
    ns = load_script()
    def boom(*a, **kw):
        raise RuntimeError("test: cache db not available")
    monkeypatch.setitem(ns, "get_claude_session_entries", boom)
    out = ns["_share_top_projects_for_range"](_now(), _now())
    assert out == []


# ----------------------------------------------------------------------
# Per-builder wiring — spy on the helper to confirm correct range.
# ----------------------------------------------------------------------

class _CallSpy:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, range_start, range_end, *, skip_sync=True):
        self.calls.append((range_start, range_end))
        return [("/captured/project", 1.0)]


def test_weekly_builder_passes_each_week_range(monkeypatch):
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)

    # Two weeks of data, newest-first.
    WeeklyPeriodRow = ns["WeeklyPeriodRow"]
    rows = [
        WeeklyPeriodRow(
            label="05-04", cost_usd=10.0,
            total_tokens=0, input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            used_pct=50.0, dollar_per_pct=0.2,
            delta_cost_pct=None, is_current=True, models=[],
            week_start_at="2026-05-04T00:00:00+00:00",
            week_end_at="2026-05-11T00:00:00+00:00",
        ),
        WeeklyPeriodRow(
            label="04-27", cost_usd=8.0,
            total_tokens=0, input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            used_pct=40.0, dollar_per_pct=0.2,
            delta_cost_pct=None, is_current=False, models=[],
            week_start_at="2026-04-27T00:00:00+00:00",
            week_end_at="2026-05-04T00:00:00+00:00",
        ),
    ]
    snap = _empty_snap(ns, weekly_periods=rows)

    out = ns["_build_weekly_share_panel_data"]({}, snap)

    # Two queries, one per week, with each week's exact bounds.
    assert len(spy.calls) == 2
    # weekly_periods is newest-first; builder reverses to oldest→newest,
    # so the FIRST builder query is the OLDER week.
    older_start, older_end = spy.calls[0]
    newer_start, newer_end = spy.calls[1]
    assert older_start.day == 27 and older_end.day == 4
    assert newer_start.day == 4 and newer_end.day == 11
    # Both weeks carry top_projects in the output.
    for w in out["weeks"]:
        assert w["top_projects"] == [("/captured/project", 1.0)]


def test_current_week_builder_queries_week_to_date_range(monkeypatch):
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)
    # Freeze "now" so the range_end is deterministic.
    monkeypatch.setitem(ns, "_share_now_utc", _now)

    TuiCurrentWeek = ns["TuiCurrentWeek"]
    cw = TuiCurrentWeek(
        week_start_at=dt.datetime(2026, 5, 4, 0, 0, tzinfo=dt.timezone.utc),
        week_end_at=dt.datetime(2026, 5, 11, 0, 0, tzinfo=dt.timezone.utc),
        used_pct=50.0,
        five_hour_pct=None, five_hour_resets_at=None,
        spent_usd=12.0, dollars_per_percent=0.24,
        latest_snapshot_at=_now(),
    )
    snap = _empty_snap(ns, current_week=cw, daily_panel=[])

    out = ns["_build_current_week_share_panel_data"]({}, snap)

    assert len(spy.calls) == 1
    rng_start, rng_end = spy.calls[0]
    # range_start = week start; range_end = "now" (week-to-date, not week-end).
    assert rng_start.day == 4
    assert rng_end == _now()
    assert out["top_projects"] == [("/captured/project", 1.0)]


def test_daily_builder_queries_seven_day_range(monkeypatch):
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)

    DailyPanelRow = ns["DailyPanelRow"]
    # Build 7 days, newest-first (matches `_dashboard_build_daily_panel`
    # output orientation).
    today = dt.date(2026, 5, 11)
    rows = [
        DailyPanelRow(
            date=(today - dt.timedelta(days=i)).isoformat(),
            label=(today - dt.timedelta(days=i)).strftime("%m-%d"),
            cost_usd=1.0 + i, is_today=(i == 0),
            intensity_bucket=0, models=[],
        )
        for i in range(7)
    ]
    snap = _empty_snap(ns, daily_panel=rows)

    out = ns["_build_daily_share_panel_data"]({}, snap)

    assert len(spy.calls) == 1
    rng_start, rng_end = spy.calls[0]
    # 7-day window covering May 5 → May 12 (end-exclusive).
    assert rng_start.day == 5
    # End-exclusive midnight on May 12.
    assert rng_end.day == 12 and rng_end.hour == 0
    assert out["top_projects"] == [("/captured/project", 1.0)]


def test_monthly_builder_queries_12_month_range(monkeypatch):
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)

    MonthlyPeriodRow = ns["MonthlyPeriodRow"]
    # 12 months newest-first, May 2026 back to June 2025.
    rows = []
    for i in range(12):
        year, month = 2026, 5 - i
        while month <= 0:
            month += 12
            year -= 1
        rows.append(MonthlyPeriodRow(
            label=f"{year:04d}-{month:02d}",
            cost_usd=10.0 + i,
            total_tokens=0, input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0,
            delta_cost_pct=None, is_current=(i == 0), models=[],
        ))
    snap = _empty_snap(ns, monthly_periods=rows)

    out = ns["_build_monthly_share_panel_data"]({}, snap)

    assert len(spy.calls) == 1
    rng_start, rng_end = spy.calls[0]
    # range_start = first day of June 2025 (oldest month, post-reverse).
    assert rng_start.year == 2025 and rng_start.month == 6 and rng_start.day == 1
    # range_end = first day of June 2026 (month AFTER May 2026, end-exclusive).
    assert rng_end.year == 2026 and rng_end.month == 6 and rng_end.day == 1
    assert out["top_projects"] == [("/captured/project", 1.0)]


def test_monthly_builder_year_boundary_carry(monkeypatch):
    """December → January rollover: end-exclusive boundary needs +1 year."""
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)

    MonthlyPeriodRow = ns["MonthlyPeriodRow"]
    # Single row: December 2026.
    rows = [MonthlyPeriodRow(
        label="2026-12", cost_usd=5.0,
        total_tokens=0, input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        delta_cost_pct=None, is_current=True, models=[],
    )]
    snap = _empty_snap(ns, monthly_periods=rows)
    ns["_build_monthly_share_panel_data"]({}, snap)
    _, rng_end = spy.calls[0]
    # Newest month is December → end-exclusive = Jan 2027.
    assert rng_end.year == 2027 and rng_end.month == 1


def test_blocks_builder_queries_recent_blocks_range(monkeypatch):
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)

    BlocksPanelRow = ns["BlocksPanelRow"]
    # 3 blocks, newest-first, 5h apart, with active = newest.
    now = dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc)
    rows = []
    for i in range(3):
        start = now - dt.timedelta(hours=5 * i)
        end = start + dt.timedelta(hours=5)
        rows.append(BlocksPanelRow(
            start_at=start.isoformat(),
            end_at=end.isoformat(),
            anchor="recorded", is_active=(i == 0),
            cost_usd=1.0 + i, models=[],
            label=start.strftime("%H:%M %b %d"),
        ))
    snap = _empty_snap(ns, blocks_panel=rows)

    out = ns["_build_blocks_share_panel_data"]({}, snap)

    assert len(spy.calls) == 1
    rng_start, rng_end = spy.calls[0]
    # range covers oldest start → newest end.
    assert rng_start == now - dt.timedelta(hours=10)
    # current_block.end_at is the newest's end (5h after now).
    assert rng_end == now + dt.timedelta(hours=5)
    assert out["top_projects"] == [("/captured/project", 1.0)]


def test_no_call_when_panel_empty(monkeypatch):
    """Empty snapshots → no aggregation queries (keep the render fast on
    cold starts)."""
    ns = load_script()
    spy = _CallSpy()
    monkeypatch.setitem(ns, "_share_top_projects_for_range", spy)
    snap = _empty_snap(ns)  # everything empty
    ns["_build_weekly_share_panel_data"]({}, snap)
    ns["_build_daily_share_panel_data"]({}, snap)
    ns["_build_monthly_share_panel_data"]({}, snap)
    ns["_build_blocks_share_panel_data"]({}, snap)
    # current-week early-returns when cw is None — also no call.
    ns["_build_current_week_share_panel_data"]({}, snap)
    assert spy.calls == []
