"""Neither window resolver multiplies a cycle count by seven days (#750 S4).

Acceptance 12. A credited week adds a billing CYCLE without adding seven
calendar days, so `cw_start - 7 * (weeks - 1)` names a span wider than the
cycles it claims to cover the moment one credited week is inside the window.
Both sites now derive their bounds from the selected interval identities —
`projects.trend.weeks[].week_start_at` (§2.2) — and both keep the seven-day
walk as the fallback for an envelope that predates that field.

Each case pairs the interval-identity call with the same call WITHOUT the
identities, because the two answers must differ: an assertion that only pins
the new answer would still pass against the arithmetic it replaced.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import sys

import pytest

from conftest import load_script


CW_START = "2026-06-29T00:00:00Z"
#: Four cycles ending in the current week, of which the third and fourth are
#: the two halves of ONE credited week. Oldest first, which is the order
#: `projects.trend.weeks[]` is published in.
CREDITED_INTERVAL_STARTS = [
    "2026-06-15T00:00:00Z",
    "2026-06-22T00:00:00Z",
    "2026-06-24T12:00:00Z",   # the credit cut inside the 06-22 week
    "2026-06-29T00:00:00Z",
]


@pytest.fixture()
def ns():
    return load_script()


# --- the All-origin drill-width resolver ------------------------------------

def _resolve(ns, *, shared_start, interval_starts):
    # The resolver lives on the dashboard module rather than on the `cctally`
    # re-export surface, like every other dashboard-only pure helper.
    dash = sys.modules["_cctally_dashboard"]
    return dash.resolve_aggregate_project_window_weeks(
        1,
        shared_start_at=shared_start,
        current_week_start_at=CW_START,
        interval_starts=interval_starts,
    )


def test_the_drill_widens_when_the_cycles_do_not_reach_the_shared_start(ns):
    """Four cycles here span 06-15 to now, but `cw_start - 21d` claims 06-08.
    A shared start between those two dates is reached by the arithmetic and
    NOT by the data, so the resolver must widen to eight rather than publish a
    row whose drill covers less than the ranking that produced it."""
    shared = "2026-06-10T00:00:00Z"
    assert _resolve(ns, shared_start=shared, interval_starts=None) == 4, (
        "the seven-day walk believes four cycles reach 2026-06-08"
    )
    assert _resolve(
        ns, shared_start=shared, interval_starts=CREDITED_INTERVAL_STARTS,
    ) == 8, (
        "the four emitted cycles only reach 2026-06-15, so four is short"
    )


def test_a_shared_start_inside_the_cycles_still_resolves_to_four(ns):
    """The control: when the identities DO reach the shared start, the
    identity path and the arithmetic agree, and neither widens."""
    shared = "2026-06-20T00:00:00Z"
    assert _resolve(
        ns, shared_start=shared, interval_starts=CREDITED_INTERVAL_STARTS,
    ) == 4


def test_the_widest_choice_is_taken_when_no_cycle_set_reaches(ns):
    """Twelve is the widest accepted choice. With only four identities the
    eight- and twelve-cycle spans fall back to the seven-day walk, which is
    what an envelope carrying a short trend supports."""
    assert _resolve(
        ns, shared_start="2025-01-01T00:00:00Z",
        interval_starts=CREDITED_INTERVAL_STARTS,
    ) == 12


def test_an_envelope_without_the_field_keeps_the_seven_day_walk(ns):
    """The documented fallback. Every entry is unparseable, so nothing is
    derived and the answer is the pre-#750 one rather than a refusal."""
    shared = "2026-06-10T00:00:00Z"
    assert _resolve(
        ns, shared_start=shared, interval_starts=[None, "not-a-timestamp"],
    ) == 4


# --- the Projects share period ----------------------------------------------

def _share_envelope(*, with_instants: bool) -> dict:
    """A projects envelope whose four trend cycles include a credited week."""
    starts = [
        "2026-05-04T00:00:00Z",
        "2026-05-11T00:00:00Z",
        "2026-05-13T09:00:00Z",   # the credit cut inside the 05-11 week
        "2026-05-18T00:00:00Z",
    ]
    weeks = []
    for start in starts:
        week: dict = {
            "week_start_date": start[:10],
            "week_label": "wk",
            "total_cost_usd": 3.0,
            "total_pct": 1.5,
        }
        if with_instants:
            week["week_start_at"] = start
        weeks.append(week)
    return {
        "current_week": {
            "week_label": "wk May 18",
            "week_start_date": "2026-05-18",
            "week_start_at": "2026-05-18T00:00:00Z",
            "total_cost_usd": 5.0,
            "rows": [
                {"key": "alpha", "bucket_path": "/repos/alpha",
                 "cost_usd": 5.0, "attributed_pct": 2.5,
                 "sessions_count": 1},
            ],
        },
        "trend": {
            "window_weeks": 4,
            "weeks": weeks,
            "projects": [
                {"key": "alpha", "bucket_path": "/repos/alpha",
                 "weekly_cost": [3.0] * 4,
                 "weekly_pct": [1.5] * 4,
                 "sessions_per_week": [1] * 4,
                 "first_seen_per_week": ["2026-05-01T12:00:00Z"] * 4,
                 "last_seen_per_week": ["2026-05-01T18:00:00Z"] * 4},
            ],
        },
    }


@pytest.fixture(autouse=True)
def _pin_share_now(monkeypatch):
    # Past the fixture's `cw_start + 7d`, so `period_end` is the week end and
    # only `period_start` is under test.
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-06-01T00:00:00Z")


def _share_period_start(ns, *, with_instants: bool):
    snap = dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        projects_envelope=_share_envelope(with_instants=with_instants),
    )
    return ns["_build_projects_share_panel_data"](
        {"windowWeeks": 4}, snap)["period_start"]


def test_the_share_period_starts_at_the_oldest_selected_cycle(ns):
    """The artifact states the period its data actually covers: the oldest of
    the four aggregated cycles, 2026-05-04. The seven-day walk would advertise
    2026-04-27 — a week of history the artifact does not contain."""
    assert _share_period_start(ns, with_instants=True) == dt.datetime(
        2026, 5, 4, tzinfo=dt.timezone.utc)


def test_the_share_period_falls_back_to_the_seven_day_walk(ns):
    """An envelope predating `week_start_at` keeps the old answer, which is
    still the best available one there."""
    assert _share_period_start(ns, with_instants=False) == dt.datetime(
        2026, 4, 27, tzinfo=dt.timezone.utc)
