"""Unit tests for `_share_resolve_period` + `_share_custom_window_n`.

Spec §6.3 advertises a "Custom (start–end pickers)" period control;
prior to this fix, `_share_resolve_period` validated the `start` field
but silently dropped it, returning only `end_dt` as `now_override`.
Builders walk fixed-length trailing windows from `now_utc`, so the
right edge of the rendered period moved with End while Start did
nothing — directly contradicting the spec promise.

These tests pin:
- `_share_resolve_period` 3-tuple shape across all kinds.
- `_share_custom_window_n` per-panel unit semantics (weeks / days /
  months) and the `max(1, …)` floor.
- The `start_dt → window length` flow honored end-to-end (both bounds
  contribute to what gets rendered).
"""
from __future__ import annotations

import datetime as dt

from conftest import load_script, redirect_paths


# ----------------------------------------------------------------------
# _share_resolve_period — 3-tuple shape across all kinds.
# ----------------------------------------------------------------------

def test_resolve_period_returns_triple_for_absent_period():
    ns = load_script()
    out = ns["_share_resolve_period"]("weekly", {})
    assert out == (None, None, None)


def test_resolve_period_returns_triple_for_current():
    ns = load_script()
    out = ns["_share_resolve_period"]("weekly", {"period": {"kind": "current"}})
    assert out == (None, None, None)


def test_resolve_period_returns_now_only_for_previous():
    ns = load_script()
    now_override, start_override, err = ns["_share_resolve_period"](
        "weekly", {"period": {"kind": "previous"}},
    )
    assert err is None
    assert start_override is None  # previous keeps default window length
    assert isinstance(now_override, dt.datetime)


def test_resolve_period_returns_both_bounds_for_custom():
    ns = load_script()
    # Use full ISO with explicit Z — naive date-only inputs round-trip
    # through `parse_iso_datetime`'s host-local fallback (CLAUDE.md
    # `parse_iso_datetime` gotcha at bin/cctally:9433), which would
    # surface as Feb 28 23:00 UTC on a non-UTC test host.
    now_override, start_override, err = ns["_share_resolve_period"](
        "weekly",
        {"period": {"kind": "custom",
                    "start": "2026-03-01T00:00:00Z",
                    "end":   "2026-05-01T00:00:00Z"}},
    )
    assert err is None
    # End and start both surface as UTC datetimes for downstream use.
    assert isinstance(now_override, dt.datetime)
    assert isinstance(start_override, dt.datetime)
    assert now_override.year == 2026 and now_override.month == 5
    assert start_override.year == 2026 and start_override.month == 3


def test_resolve_period_rejects_inverted_custom_range():
    ns = load_script()
    out, start, err = ns["_share_resolve_period"](
        "weekly",
        {"period": {"kind": "custom",
                    "start": "2026-05-10",
                    "end":   "2026-05-04"}},
    )
    assert (out, start) == (None, None)
    assert err is not None
    assert err["field"] == "options.period"


def test_resolve_period_rejects_period_override_for_fixed_panels():
    """forecast / current-week / sessions accept kind=current only."""
    ns = load_script()
    for panel in ("forecast", "current-week", "sessions"):
        for kind in ("previous", "custom"):
            opts = {"period": {"kind": kind,
                                "start": "2026-04-01",
                                "end":   "2026-05-01"}}
            out, start, err = ns["_share_resolve_period"](panel, opts)
            assert (out, start) == (None, None)
            assert err is not None
            assert err["field"] == "options.period.kind", f"{panel}/{kind}"


# ----------------------------------------------------------------------
# _share_custom_window_n — per-panel unit semantics.
# ----------------------------------------------------------------------

def _utc(year, month, day):
    return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)


def test_custom_window_weekly_spans_full_weeks():
    ns = load_script()
    # 28 days = 4 weeks. Both `weekly` and `trend` measure in weeks.
    n = ns["_share_custom_window_n"]("weekly", _utc(2026, 4, 1), _utc(2026, 4, 29))
    assert n == 4

    n = ns["_share_custom_window_n"]("trend", _utc(2026, 4, 1), _utc(2026, 4, 29))
    assert n == 4


def test_custom_window_weekly_partial_week_rounds_up():
    """3 days is still 1 week — the window can't be empty."""
    ns = load_script()
    n = ns["_share_custom_window_n"]("weekly", _utc(2026, 5, 1), _utc(2026, 5, 4))
    assert n == 1


def test_custom_window_daily_inclusive_days():
    ns = load_script()
    # May 1 → May 8 = 7 inclusive days.
    n = ns["_share_custom_window_n"]("daily", _utc(2026, 5, 1), _utc(2026, 5, 8))
    assert n == 7


def test_custom_window_daily_min_one():
    """Same-day pick still renders something."""
    ns = load_script()
    # Half-day window — ceiling math keeps it at 1.
    n = ns["_share_custom_window_n"](
        "daily",
        _utc(2026, 5, 1),
        dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert n >= 1


def test_custom_window_monthly_calendar_inclusive():
    ns = load_script()
    # Jan 2026 → May 2026 → 5 calendar months inclusive.
    n = ns["_share_custom_window_n"]("monthly", _utc(2026, 1, 15), _utc(2026, 5, 10))
    assert n == 5


def test_custom_window_monthly_year_boundary():
    ns = load_script()
    # Nov 2025 → Feb 2026 = Nov + Dec + Jan + Feb = 4.
    n = ns["_share_custom_window_n"]("monthly", _utc(2025, 11, 1), _utc(2026, 2, 1))
    assert n == 4


# ----------------------------------------------------------------------
# End-to-end: `_share_apply_period_override` threads start_dt → window
# length so the rendered weekly_periods array has the right number of
# rows for the custom range. (Uses an empty test DB; what we're pinning
# is the wiring, not the data — the builder gets called with the right
# `n`, which is the bit that previously regressed.)
# ----------------------------------------------------------------------

def test_apply_period_override_passes_custom_window_to_weekly(
    monkeypatch, tmp_path,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    captured: dict = {}
    orig = ns["_dashboard_build_weekly_periods"]

    def spy(conn, now_utc, **kwargs):
        captured["now_utc"] = now_utc
        captured["kwargs"] = kwargs
        return orig(conn, now_utc, **kwargs)

    monkeypatch.setitem(ns, "_dashboard_build_weekly_periods", spy)

    # Empty DataSnapshot stub — the override path only cares that
    # `dataclasses.replace` works on it.
    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None,
        trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc),
        weekly_periods=[],
    )

    out_snap, err = ns["_share_apply_period_override"](
        "weekly",
        {"period": {"kind": "custom",
                     "start": "2026-03-30",
                     "end":   "2026-05-11"}},
        snap,
    )

    assert err is None
    assert out_snap is not None
    # 6 weeks (Mar 30 → May 11 = 42 days = 6 weeks).
    assert captured["kwargs"]["n"] == 6
    # `now_utc` is the END of the range.
    assert captured["now_utc"].year == 2026
    assert captured["now_utc"].month == 5
    assert captured["now_utc"].day == 11


def test_apply_period_override_blocks_uses_start_dt_as_window_anchor(
    monkeypatch, tmp_path,
):
    """Blocks doesn't use `n=` — its builder takes `week_start_at` and
    `week_end_at`. Custom-period must thread start_dt → week_start_at."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    captured: dict = {}
    orig = ns["_dashboard_build_blocks_panel"]

    def spy(conn, now_utc, *, week_start_at, week_end_at, **kwargs):
        captured["week_start_at"] = week_start_at
        captured["week_end_at"] = week_end_at
        return orig(conn, now_utc, week_start_at=week_start_at,
                    week_end_at=week_end_at, **kwargs)

    monkeypatch.setitem(ns, "_dashboard_build_blocks_panel", spy)

    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None,
        trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc),
        blocks_panel=[],
    )

    out_snap, err = ns["_share_apply_period_override"](
        "blocks",
        {"period": {"kind": "custom",
                     "start": "2026-05-09T00:00:00Z",
                     "end":   "2026-05-11T00:00:00Z"}},
        snap,
    )

    assert err is None
    assert out_snap is not None
    # week_start_at = start_dt (not now - 7 days).
    assert captured["week_start_at"].day == 9
    assert captured["week_end_at"].day == 11


def test_apply_period_override_blocks_previous_falls_back_to_7day(
    monkeypatch, tmp_path,
):
    """kind=previous keeps the legacy 7-day-ending-at-now_utc window."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    captured: dict = {}
    orig = ns["_dashboard_build_blocks_panel"]

    def spy(conn, now_utc, *, week_start_at, week_end_at, **kwargs):
        captured["week_start_at"] = week_start_at
        captured["week_end_at"] = week_end_at
        captured["now_utc"] = now_utc
        return orig(conn, now_utc, week_start_at=week_start_at,
                    week_end_at=week_end_at, **kwargs)

    monkeypatch.setitem(ns, "_dashboard_build_blocks_panel", spy)

    DataSnapshot = ns["DataSnapshot"]
    snap = DataSnapshot(
        current_week=None, forecast=None,
        trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc),
        blocks_panel=[],
    )

    out_snap, err = ns["_share_apply_period_override"](
        "blocks",
        {"period": {"kind": "previous"}},
        snap,
    )

    assert err is None
    # week_end_at = now_utc; week_start_at = now_utc - 7d.
    assert captured["week_end_at"] == captured["now_utc"]
    assert (captured["week_end_at"] - captured["week_start_at"]) == dt.timedelta(days=7)
