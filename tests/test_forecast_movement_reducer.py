"""#661 S2 Task B1 — the realized-week-movement reducer (spec §4.1).

`_select_dollars_per_percent`'s trailing-median path divided each prior week's
cost by `_floored_week_max(week)`, #290's reset-aware, display-oriented
high-water mark. On a credited week that is the POST-credit reading — a
fragment of the week's cost-bearing consumption — so the week of 2026-05-09
priced at $355.15 per point against a true $25.37, and that value then sat
inside a later four-week median.

The defect is in the consumer, not in the reducer: `_floored_week_max` decided
the floor-versus-pre-credit-peak question correctly for its current-high-water
and MAX-clamp consumers and is unchanged. What this module covers is the
replacement denominator — realized meter movement, segmented at the recorded
reset and credit boundaries.

Two things the reducer must NOT do, both of which an earlier draft of the spec
got wrong. A naive sum of positive adjacent movement over the #290 fixture's
readings of 46 then 31 yields 46, because the drop contributes zero. Treating
31 as fresh consumption yields 77 by ASSUMING the credit reset the meter to
zero, which the fixture does not establish. Neither is knowable, so the week is
withheld.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from conftest import load_script

UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 6, 1, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)
WEEK_START_DATE = "2026-06-01"


def _mod():
    """The forecast module, reached through the one `bin/cctally` loader."""
    return load_script()["_cctally_forecast"]


def _conn(*, with_source_columns=True):
    conn = sqlite3.connect(":memory:")
    columns = ("id INTEGER PRIMARY KEY, week_start_date TEXT, "
               "week_start_at TEXT, week_end_at TEXT, captured_at_utc TEXT, "
               "weekly_percent REAL, account_key TEXT")
    if with_source_columns:
        columns += ", source TEXT, payload_json TEXT"
    conn.execute(f"CREATE TABLE weekly_usage_snapshots ({columns})")
    conn.execute(
        "CREATE TABLE weekly_credit_floors (week_start_date TEXT, "
        "effective_at_utc TEXT, account_key TEXT)")
    conn.execute(
        "CREATE TABLE week_reset_events (effective_reset_at_utc TEXT, "
        "account_key TEXT)")
    return conn


def _credit(conn, *, hours):
    conn.execute(
        "INSERT INTO weekly_credit_floors VALUES (?, ?, 'unattributed')",
        (WEEK_START_DATE,
         (WEEK_START + dt.timedelta(hours=hours)).isoformat()))


def _reset(conn, *, hours):
    conn.execute(
        "INSERT INTO week_reset_events VALUES (?, 'unattributed')",
        ((WEEK_START + dt.timedelta(hours=hours)).isoformat(),))


def _readings(*rows):
    """`[(captured, percent, source, payload_json)]` from `(hours, pct[, at])`.

    A row of three carries the effective instant of the credit the synthetic
    post-credit snapshot is the baseline for, spelled as `_apply_credit`
    spells it.
    """
    out = []
    for row in rows:
        hours, percent = row[0], row[1]
        at = WEEK_START + dt.timedelta(hours=hours)
        if len(row) == 2:
            out.append((at.isoformat(), percent, "userscript", "{}"))
            continue
        effective = WEEK_START + dt.timedelta(hours=row[2])
        out.append((at.isoformat(), percent, "record-credit", json.dumps(
            {"kind": "record-credit", "from": 46.0, "to": percent,
             "effective": effective.isoformat(timespec="seconds")})))
    return out


def _movement(conn, readings, *, account_key=None):
    return _mod()._realized_week_movement(
        conn, WEEK_START, WEEK_END, WEEK_START_DATE, readings,
        account_key=account_key)


def test_b1_a_plain_week_measures_its_whole_climb():
    conn = _conn()
    out = _movement(conn, _readings((2, 12.0), (50, 30.0), (100, 44.0)))
    assert out.withheld_cause is None
    assert out.points == pytest.approx(44.0)
    conn.close()


def test_b1_the_first_segment_measures_from_zero_not_from_its_first_reading():
    """A week starts at an empty meter, so the first reading IS consumption.

    Measuring the first segment from its own first reading would lose
    everything consumed before the first status-line capture of the week.
    """
    conn = _conn()
    out = _movement(conn, _readings((2, 12.0)))
    assert out.points == pytest.approx(12.0)
    conn.close()


def test_b1_segments_at_a_credit_and_uses_the_synthetic_baseline():
    """Readings 46, a synthetic post-credit baseline of 0, then 31 → 77."""
    conn = _conn()
    _credit(conn, hours=72)
    out = _movement(conn, _readings((24, 46.0), (73, 0.0, 72), (120, 31.0)))
    assert out.withheld_cause is None
    assert out.points == pytest.approx(77.0)
    assert out.segments == 2
    conn.close()


def test_b1_a_nonzero_synthetic_baseline_is_subtracted_not_counted():
    """The post-credit reading is a new baseline, not fresh consumption."""
    conn = _conn()
    _credit(conn, hours=72)
    out = _movement(conn, _readings((24, 46.0), (73, 10.0, 72), (120, 31.0)))
    assert out.points == pytest.approx(46.0 + (31.0 - 10.0))
    conn.close()


def test_b1_withholds_when_the_synthetic_baseline_is_absent():
    """The #290 fixture shape: 46 then 31, with no synthetic snapshot.

    A naive positive-adjacent sum gives 46 (the drop contributes zero) and
    treating 31 as fresh consumption gives 77 by ASSUMING the credit zeroed
    the meter. Neither is knowable, so the week is withheld.
    """
    conn = _conn()
    _credit(conn, hours=72)
    out = _movement(conn, _readings((24, 46.0), (120, 31.0)))
    assert out.points is None
    assert out.withheld_cause == "credit-baseline-absent"
    conn.close()


def test_b1_a_synthetic_for_a_different_credit_is_not_this_ones_baseline():
    """The payload names the credit the snapshot is the baseline for.

    Accepting any `record-credit` row as any segment's baseline would let one
    credit's synthetic rescue another credit's segment, which is the same
    unknowable assumption the withholding exists to refuse.
    """
    conn = _conn()
    _credit(conn, hours=72)
    out = _movement(conn, _readings((24, 46.0), (73, 0.0, 30), (120, 31.0)))
    assert out.points is None
    assert out.withheld_cause == "credit-baseline-absent"
    conn.close()


def test_b1_a_store_without_the_source_column_withholds_a_credited_week():
    """No `source` column means no synthetic can be identified at all.

    The fail-closed direction is the only defensible one: the reducer cannot
    prove a baseline exists, so it must not price the week.
    """
    conn = _conn(with_source_columns=False)
    _credit(conn, hours=72)
    out = _movement(conn, [
        ((WEEK_START + dt.timedelta(hours=24)).isoformat(), 46.0, None, None),
        ((WEEK_START + dt.timedelta(hours=120)).isoformat(), 31.0, None, None),
    ])
    assert out.points is None
    assert out.withheld_cause == "credit-baseline-absent"
    conn.close()


def test_b1_withholds_a_right_censored_week():
    conn = _conn()
    out = _movement(conn, _readings((24, 40.0), (120, 100.0)))
    assert out.points is None
    assert out.withheld_cause == "right-censored"
    conn.close()


def test_b1_right_censoring_outranks_a_missing_credit_baseline():
    """A capped week has no point estimate at all, so the cap is the cause."""
    conn = _conn()
    _credit(conn, hours=72)
    out = _movement(conn, _readings((24, 100.0), (120, 31.0)))
    assert out.withheld_cause == "right-censored"
    conn.close()


def test_b1_never_infers_a_credit_from_a_decrease():
    """A decrease with NO recorded credit is not a credit. It is one segment.

    Spec §4.1: `week_reset_events` and `weekly_credit_floors` are the
    authoritative boundary records.
    """
    conn = _conn()
    out = _movement(conn, _readings((24, 40.0), (72, 38.0), (120, 45.0)))
    assert out.withheld_cause is None
    assert out.points == pytest.approx(47.0)
    assert out.segments == 1
    conn.close()


def test_b1_a_boundary_with_no_readings_after_it_needs_no_baseline():
    """A reset ends a week; the readings after it carry the NEXT window's
    bounds and are not in this week's set at all. An empty segment cannot
    withhold a week whose measurable part is complete."""
    conn = _conn()
    _reset(conn, hours=160)
    out = _movement(conn, _readings((24, 40.0), (120, 45.0)))
    assert out.withheld_cause is None
    assert out.points == pytest.approx(45.0)
    assert out.segments == 1
    conn.close()


def test_b1_segments_at_a_reset_boundary_too():
    """Spec §4.1 names reset AND credit boundaries, so a reading after a
    recorded reset needs its own baseline exactly as a credit's does.

    The cause names the RESET, because that is the record that created the
    segment. It read `credit-baseline-absent` until the Stage C review, which
    pointed a reader at a table with nothing in it; the withholding itself
    was right either way.
    """
    conn = _conn()
    _reset(conn, hours=72)
    out = _movement(conn, _readings((24, 46.0), (120, 31.0)))
    assert out.points is None
    assert out.withheld_cause == "reset-baseline-absent"
    conn.close()


def test_b1_a_boundary_outside_the_week_does_not_segment_it():
    conn = _conn()
    conn.execute(
        "INSERT INTO weekly_credit_floors VALUES (?, ?, 'unattributed')",
        (WEEK_START_DATE, (WEEK_END + dt.timedelta(hours=5)).isoformat()))
    out = _movement(conn, _readings((24, 46.0), (120, 31.0)))
    assert out.withheld_cause is None
    assert out.points == pytest.approx(46.0)
    conn.close()


def test_b1_boundaries_are_scoped_to_the_account_when_one_is_given():
    """One account's credit must not segment another account's week."""
    conn = _conn()
    conn.execute(
        "INSERT INTO weekly_credit_floors VALUES (?, ?, 'other')",
        (WEEK_START_DATE,
         (WEEK_START + dt.timedelta(hours=72)).isoformat()))
    out = _movement(conn, _readings((24, 46.0), (120, 31.0)),
                    account_key="mine")
    assert out.withheld_cause is None
    assert out.points == pytest.approx(46.0)
    # The same week read WITHOUT an account key is the merged view, which sees
    # every account's boundary records.
    merged = _movement(conn, _readings((24, 46.0), (120, 31.0)))
    assert merged.withheld_cause == "credit-baseline-absent"
    conn.close()


def test_b1_withholds_a_week_with_no_usable_reading():
    conn = _conn()
    assert _movement(conn, []).withheld_cause == "no-readings"
    assert _movement(conn, [("not-a-time", 5.0, "userscript", "{}")]
                     ).withheld_cause == "no-readings"
    conn.close()


def test_b1_the_withholding_causes_are_a_closed_set():
    module = _mod()
    assert set(module.MOVEMENT_WITHHELD_CAUSES) == {
        "credit-baseline-absent", "reset-baseline-absent",
        "boundary-records-unreadable", "right-censored", "no-readings"}
    conn = _conn()
    _credit(conn, hours=72)
    for readings in (_readings((24, 46.0), (120, 31.0)),
                     _readings((24, 100.0)),
                     []):
        out = _movement(conn, readings)
        assert out.withheld_cause in module.MOVEMENT_WITHHELD_CAUSES
    conn.close()


def test_b1_readings_are_ordered_by_instant_not_by_row_order():
    """The store holds mixed UTC offsets, so a lexical order is wrong."""
    conn = _conn()
    rows = _readings((100, 44.0), (2, 12.0), (50, 30.0))
    out = _movement(conn, rows)
    assert out.points == pytest.approx(44.0)
    conn.close()
