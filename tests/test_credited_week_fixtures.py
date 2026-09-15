"""The credited-week fixtures, asserted directly (#750 S4 §6.2 / #734).

The byte-compared goldens beside these fixtures cover the assembled wire and
render contract, which #734 says the hand-seeded unit cases cannot reach.
These cases pin what a golden asserts only by accident: cardinality as
cuts-plus-one, half-open ownership of the capture stamped exactly at a cut,
distinct segment identities, and the exact per-cycle values.

`build_reset_week` in `bin/build-dashboard-fixtures.py` stays the
BOUNDARY-SHIFT control — `old_week_end_at` 14:00Z against a reset at 13:00Z,
so `old != effective` — and epic invariant 2 says it must not split. That is
asserted here too, because a credited-week fixture that also split the control
would prove nothing about the predicate.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

import pytest

from conftest import load_script

_NS = load_script()

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def _stats(kind: str, scenario: str) -> sqlite3.Connection:
    path = (FIXTURES / kind / scenario / ".local" / "share" / "cctally"
            / "stats.db")
    # A hard failure, never a skip: these fixture DBs are COMMITTED, so an
    # absent one is a broken checkout rather than an environment this module
    # does not apply to. (`tests/fixtures/project/*/.local/.../*.db` are
    # gitignored and rebuilt into a scratch dir by their harness, which is why
    # the project-side credited scenarios are covered by their byte-compared
    # goldens rather than from here.)
    assert path.exists(), f"committed fixture DB missing: {path}"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _segments(conn, since: dt.datetime, until: dt.datetime):
    return _NS["_compute_subscription_weeks"](
        conn, since, until, account_key=None)


WEEK_START = dt.datetime(2026, 4, 13, 14, 0, tzinfo=dt.timezone.utc)
WEEK_END = dt.datetime(2026, 4, 20, 14, 0, tzinfo=dt.timezone.utc)
CUT = dt.datetime(2026, 4, 16, 9, 0, tzinfo=dt.timezone.utc)
SAME_DAY_CUTS = (
    dt.datetime(2026, 4, 17, 3, 0, tzinfo=dt.timezone.utc),
    dt.datetime(2026, 4, 17, 15, 0, tzinfo=dt.timezone.utc),
)


@pytest.mark.parametrize("scenario,cuts", [
    ("credited-week", (CUT,)),
    ("credited-week-same-day", SAME_DAY_CUTS),
])
def test_a_dashboard_credited_fixture_emits_cuts_plus_one_cycles(
        scenario, cuts):
    """N cuts make N+1 billing cycles, and each abuts the next. Written as
    cuts-plus-one rather than as a literal, because nothing in this contract
    may assume two segments (spec §1.3)."""
    conn = _stats("dashboard", scenario)
    try:
        segments = [
            w for w in _segments(conn, WEEK_START - dt.timedelta(days=1),
                                 WEEK_END)
            if w.start_date == WEEK_START.date()
        ]
    finally:
        conn.close()
    assert len(segments) == len(cuts) + 1, [
        (w.start_ts, w.end_ts) for w in segments]
    for earlier, later in zip(segments, segments[1:]):
        assert earlier.end_ts == later.start_ts, "cycles must abut"
    identities = [w.segment_key for w in segments]
    assert len(set(identities)) == len(identities), identities
    shared = {w.start_date for w in segments}
    assert len(shared) == 1, (
        "every cycle of one week shares `start_date`; it is the join key, "
        "never the identity"
    )


def test_the_capture_at_the_cut_belongs_to_the_post_credit_cycle():
    """Spec §1.2. `credited-week` stamps one observation EXACTLY at the cut.
    It belongs to the cycle that STARTS there, so the pre-credit cycle keeps
    its own earlier peak instead of taking the credited reading."""
    conn = _stats("dashboard", "credited-week")
    try:
        segments = _segments(conn, WEEK_START - dt.timedelta(days=1), WEEK_END)
        by_segment = _NS["latest_usage_by_segment"](
            conn, segments, account_key=None)
        credited = [w for w in segments if w.start_date == WEEK_START.date()]
    finally:
        conn.close()
    assert len(credited) == 2, [(w.start_ts, w.end_ts) for w in credited]
    assert by_segment[credited[0].segment_key] == 41.0, (
        "the pre-credit cycle reads its own peak, not the cut-time capture"
    )
    assert by_segment[credited[1].segment_key] == 30.0


def test_the_same_day_fixture_gives_every_cycle_its_own_percent():
    conn = _stats("dashboard", "credited-week-same-day")
    try:
        segments = _segments(conn, WEEK_START - dt.timedelta(days=1), WEEK_END)
        by_segment = _NS["latest_usage_by_segment"](
            conn, segments, account_key=None)
        credited = [w for w in segments if w.start_date == WEEK_START.date()]
    finally:
        conn.close()
    values = [by_segment[w.segment_key] for w in credited]
    assert values == [44.0, 22.0, 13.0], values


def test_build_reset_week_is_a_boundary_shift_and_does_not_split():
    """Invariant 2. `old_week_end_at` (14:00Z) differs from the effective
    reset (13:00Z), so the row is a boundary SHIFT and no cut is derived from
    it. Without this the credited fixtures above would not prove that the
    predicate distinguishes the two shapes."""
    conn = _stats("dashboard", "reset-week")
    try:
        rows = conn.execute(
            "SELECT old_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events"
        ).fetchall()
        assert rows, "the control fixture must carry a reset event"
        assert all(r["old_week_end_at"] != r["effective_reset_at_utc"]
                   for r in rows), [dict(r) for r in rows]
        assert _NS["in_place_cut_instants"](
            conn, account_key=None) == frozenset()
    finally:
        conn.close()
