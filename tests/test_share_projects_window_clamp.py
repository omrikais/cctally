"""Regression — Codex round 1 finding #1.

`_build_projects_share_panel_data` clamps the multi-week aggregation
via ``take = min(weeks_back, n_weeks)`` but historically returned the
unclamped ``weeks_back`` for ``window_weeks`` AND used the unclamped
value to compute the ``period_start`` bound. On thin-history dashboards
(fresh installs, post-rebuild) that mismatch produced share artifacts
labeled ``Last 12 weeks`` while only (say) 3 weeks of data were
aggregated, with a 12-week date range — materially misleading.

The fix returns the *effective* week count downstream so the share
template's ``_projects_period`` builder + label both agree with the
actual span the rows cover.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from conftest import load_script


# `_build_projects_share_panel_data` now clips `period_end` to
# min(cw_start + 7d, _share_now_utc()) — week-to-date semantics, per
# Codex review 2026-05-20 finding #3. Pin `CCTALLY_AS_OF` past the
# fixture's `cw_start + 7d` (2026-05-25) so each test sees the
# "post-reset" branch and period_end stays at `cw_start + 7d` — that
# is the pre-clip behavior these clamp tests assert against.
@pytest.fixture(autouse=True)
def _pin_share_now_after_week_end(monkeypatch):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-01T00:00:00Z")


def _make_snap(ns, envelope: dict) -> object:
    """Return a `_empty_dashboard_snapshot` mutated to carry the given
    projects envelope. Uses dataclasses.replace because DataSnapshot is
    declared frozen-ish."""
    import dataclasses
    snap = ns["_empty_dashboard_snapshot"]()
    return dataclasses.replace(snap, projects_envelope=envelope)


def _envelope_with_n_trend_weeks(n: int) -> dict:
    """Synthesize a projects_envelope whose `trend.weeks` carries `n`
    entries. The exact bucket_path / dates don't matter for the clamp
    test — only the trend `weeks` count and per-project list lengths."""
    return {
        "current_week": {
            "week_label":      "wk May 18",
            "week_start_date": "2026-05-18",
            "week_start_at":   "2026-05-18T00:00:00Z",
            "total_cost_usd":  5.0,
            "rows": [
                {"key": "alpha", "bucket_path": "/repos/alpha",
                 "cost_usd": 5.0, "attributed_pct": 2.5,
                 "sessions_count": 1},
            ],
        },
        "trend": {
            "window_weeks": n,
            "weeks": [
                {"week_start_date": f"2026-05-{18 - (n - 1 - i) * 7:02d}",
                 "week_label":      "wk",
                 "total_cost_usd":  3.0,
                 "total_pct":       1.5}
                for i in range(n)
            ],
            "projects": [
                {"key": "alpha", "bucket_path": "/repos/alpha",
                 "weekly_cost": [3.0] * n,
                 "weekly_pct":  [1.5] * n,
                 "sessions_per_week":   [1] * n,
                 "first_seen_per_week": ["2026-05-01T12:00:00Z"] * n,
                 "last_seen_per_week":  ["2026-05-01T18:00:00Z"] * n},
            ],
        },
    }


def test_window_clamped_to_available_history(monkeypatch, tmp_path):
    """When weeks_back=12 but the trend envelope only carries 3 weeks,
    the panel_data MUST return window_weeks=3 (not 12), and period_start
    MUST be 2 weeks before cw_start (not 11) — otherwise the share
    template renders "Last 12 weeks" and a 12-week date range over 3
    weeks of data."""
    ns = load_script()
    snap = _make_snap(ns, _envelope_with_n_trend_weeks(3))
    build = ns["_build_projects_share_panel_data"]
    out = build({"windowWeeks": 12}, snap)
    assert out["window_weeks"] == 3, (
        f"expected window_weeks clamped to 3 (history available), "
        f"got {out['window_weeks']}"
    )
    # cw_start = 2026-05-18T00:00:00Z; period_start should be 2 weeks
    # earlier (effective_weeks=3 → cw_start - 7*(3-1) days), NOT 11
    # weeks earlier (the old, unclamped behavior).
    cw_start = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
    assert out["period_start"] == cw_start - dt.timedelta(days=14), (
        f"period_start should reflect effective weeks, "
        f"got {out['period_start']!r}"
    )
    assert out["period_end"] == cw_start + dt.timedelta(days=7)


def test_window_not_clamped_when_history_sufficient(monkeypatch, tmp_path):
    """When the trend envelope carries >= weeks_back weeks, no clamp
    happens — windowWeeks rides through and period_start is the full
    requested span."""
    ns = load_script()
    snap = _make_snap(ns, _envelope_with_n_trend_weeks(12))
    build = ns["_build_projects_share_panel_data"]
    out = build({"windowWeeks": 8}, snap)
    assert out["window_weeks"] == 8
    cw_start = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
    assert out["period_start"] == cw_start - dt.timedelta(days=49)
    assert out["period_end"] == cw_start + dt.timedelta(days=7)


def test_window_clamps_to_one_when_no_trend(monkeypatch, tmp_path):
    """Edge: empty trend envelope. The 1-week branch is taken when
    weeks_back==1, but the multi-week branch with n_weeks==0 falls back
    to a single-week period rather than emitting "Last 0 weeks"."""
    ns = load_script()
    envelope = _envelope_with_n_trend_weeks(0)
    # _envelope_with_n_trend_weeks(0) yields an empty weeks list; reuse
    # it but force the multi-week code path via weeks_back=4.
    snap = _make_snap(ns, envelope)
    build = ns["_build_projects_share_panel_data"]
    out = build({"windowWeeks": 4}, snap)
    assert out["window_weeks"] == 1, (
        f"expected window_weeks=1 on empty trend, "
        f"got {out['window_weeks']}"
    )
    cw_start = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
    assert out["period_start"] == cw_start
    assert out["period_end"] == cw_start + dt.timedelta(days=7)


def test_panel_window_weeks_1_unaffected(monkeypatch, tmp_path):
    """windowWeeks=1 always returns 1 — independent of trend depth."""
    ns = load_script()
    snap = _make_snap(ns, _envelope_with_n_trend_weeks(5))
    build = ns["_build_projects_share_panel_data"]
    out = build({"windowWeeks": 1}, snap)
    assert out["window_weeks"] == 1
    cw_start = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
    assert out["period_start"] == cw_start
    assert out["period_end"] == cw_start + dt.timedelta(days=7)


# --- mid-week period_end clip (Codex review 2026-05-20 finding #3) ---


def test_period_end_clipped_to_now_when_mid_week(monkeypatch, tmp_path):
    """Mid-week exports must NOT advertise the next reset as `period_end`.

    The rows in projects panel_data are week-to-date — `current_week.rows`
    are aggregated through "now", and the multi-week branch's trailing
    slice is also week-to-date. So a period_end of `cw_start + 7d` on a
    mid-week export would advertise data through a future reset that the
    rows don't actually include. Clip to `min(cw_start + 7d, now)` so the
    frontmatter agrees with the live dashboard's "spent this week" KPI.
    """
    # Pin `now` to 3 days into the fixture week (cw_start = 2026-05-18) so
    # the reset (cw_start + 7d = 2026-05-25) is strictly in the future. The
    # autouse fixture above pins to 2026-06-01 (post-reset); override here
    # for this single test.
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-21T00:00:00Z")
    ns = load_script()
    snap = _make_snap(ns, _envelope_with_n_trend_weeks(5))
    build = ns["_build_projects_share_panel_data"]
    out = build({"windowWeeks": 1}, snap)
    expected = dt.datetime(2026, 5, 21, tzinfo=dt.timezone.utc)
    assert out["period_end"] == expected, (
        f"period_end should clip to now (mid-week), got {out['period_end']!r}"
    )


def test_period_end_uses_week_end_when_now_past_reset(monkeypatch, tmp_path):
    """After the reset has passed, period_end stays at cw_start + 7d.

    A "current week" cw_start whose reset is already behind `now` is
    typical of test fixtures and CCTALLY_AS_OF rewinds — the clip then
    becomes a no-op, preserving the prior behavior.
    """
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-01T00:00:00Z")
    ns = load_script()
    snap = _make_snap(ns, _envelope_with_n_trend_weeks(5))
    build = ns["_build_projects_share_panel_data"]
    out = build({"windowWeeks": 1}, snap)
    cw_start = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
    assert out["period_end"] == cw_start + dt.timedelta(days=7)
