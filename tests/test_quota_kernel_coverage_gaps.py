"""#661 S1 — three kernel behaviours the mutation probe found unpinned.

`tests/quota-mutations.json` declares each kernel mutation with the test node
ids that must fail when it is applied, and running that list found three
mutations of `bin/_lib_quota_model.py` that the whole suite survived. Spec
section 21 says a mutation no test catches is a coverage gap, reported as
such; this module closes the three.

They live here rather than in `tests/test_quota_model_kernel.py` because that
file and the kernel it covers are owned by the kernel rounds and this session
does not modify either. Each test below names the mutation it detects, the
same way that file does, so the two can be merged later without losing the
record of which round found what.
"""
from __future__ import annotations

import datetime as dt

import pytest

from tests._script_loader import load_script_module

UTC = dt.timezone.utc
DAY = dt.datetime(2026, 8, 10, tzinfo=UTC)
WEEK = dt.datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def qm():
    return load_script_module()._lib_quota_model


def _snap(qm, hour, percent, *, day=None, rowid=0):
    return qm.SnapshotRecord(
        at=(day or DAY).replace(hour=hour), week_start=WEEK,
        percent=float(percent), source="statusline", rowid=rowid)


def _observation(qm, meter_delta, units, index, *, cause=None):
    return qm.DailyObservation(
        date=dt.date(2026, 6, 1) + dt.timedelta(days=index), segment_id=0,
        meter_delta=float(meter_delta), units=float(units),
        class_shares={"fresh": 1.0}, family_shares={"claude-opus-5": 1.0},
        cause=cause)


# --------------------------------------------------------------------------
# Gap 1 — the interval midpoints applied inside the daily series.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("readings,expected_midpoint,expected_raw", [
    # A reading of 0 denotes [0, 0.5) and 1 denotes [0.5, 1), so those two
    # carry half-width intervals and their midpoints are 0.25 and 0.75. Every
    # reading at or above 2 sits at `shown - 0.5`, which cancels in a
    # difference — which is why a day opening at 10 cannot distinguish the
    # corrected delta from the raw one.
    ((0, 5), 4.25, 5.0),
    ((1, 5), 3.75, 4.0),
    ((0, 1), 0.5, 1.0),
])
def test_the_daily_series_applies_the_low_reading_midpoints(
        qm, readings, expected_midpoint, expected_raw):
    """Mutation: the meter delta taken from raw readings rather than the
    interval midpoints, with the capped-reading censor left in place.

    `test_an_entry_inside_the_interval_is_counted_with_its_shares` states
    this as its mutation and cannot detect it: its readings are 10 and 20, so
    the raw delta and the corrected delta are both exactly 10. Only a day
    whose readings include 0 or 1 separates the two rules.
    """
    assert expected_midpoint != expected_raw, (
        "a case where both rules agree pins neither")
    segments = qm.build_segments(
        [_snap(qm, 1, readings[0], rowid=1),
         _snap(qm, 5, readings[1], rowid=2)], [])
    series = qm.build_daily_series(
        segments, [], now=DAY + dt.timedelta(days=2),
        newest_entry_at=DAY + dt.timedelta(days=2))
    if expected_midpoint < qm.DETECTOR["min_daily_gain"]:
        # Below the minimum daily gain the day is absent from the series
        # entirely, and the raw rule would have kept it. That difference is
        # the assertion.
        assert series == []
        return
    assert [o.meter_delta for o in series] == [pytest.approx(
        expected_midpoint)]


# --------------------------------------------------------------------------
# Gap 2 — the detector reads its horizon from the fingerprinted constant.
# --------------------------------------------------------------------------
def test_the_detector_reads_the_scan_horizon_from_the_constant(qm,
                                                               monkeypatch):
    """Mutation: inlining the horizon as a literal instead of reading
    `DETECTOR["max_auto_scan_days"]`.

    `test_max_auto_scan_days_is_a_fingerprinted_public_constant` asserts the
    constant's value and its presence in the payload, which is what makes a
    change invalidate persisted calibrations — but not that the scan actually
    obeys it. A literal would keep the fingerprint honest and the behaviour
    fixed, which is the worse of the two failures.
    """
    series = [_observation(qm, 5.0, 10e6 * (1 + 0.001 * (i % 7)), i)
              for i in range(40)]
    monkeypatch.setitem(qm.DETECTOR, "max_auto_scan_days", 20)
    result = qm.detect_change(series)
    assert result.max_auto_scan_days == 20
    assert result.input_eligible_days == 40
    assert result.scanned_eligible_days == 20
    assert result.truncated_eligible_days == 20
    assert result.history_truncated is True


# --------------------------------------------------------------------------
# Gap 3 — a withheld day with POSITIVE units never enters the detector.
# --------------------------------------------------------------------------
def test_a_withheld_day_with_positive_units_never_enters_the_detector(qm):
    """Mutation: dropping the `o.cause is None` filter from `detect_change`.

    `test_withheld_days_never_enter_the_detector` states this mutation and
    cannot detect it: the transition day it inserts carries zero units and
    zero meter movement, so the `units > 0` and `meter_delta > 0` filters
    already remove it and the cause filter is unobserved. A day marked
    `sparse-local-history` by the second pass carries POSITIVE units and
    positive meter movement by construction, so only it separates the rules —
    and it is exactly the day spec section 5 forbids from relocating the
    change point.
    """
    sparse = _observation(qm, 5.0, 4.0e6, 20,
                          cause=qm.WithholdingCause.SPARSE_LOCAL_HISTORY)
    assert sparse.units > 0.0 and sparse.meter_delta > 0.0, (
        "a zero-unit day is removed by a different filter and pins nothing")
    series = (
        [_observation(qm, 5.0, 10e6 * (1 + 0.001 * (i % 7)), i)
         for i in range(20)]
        + [sparse]
        + [_observation(qm, 5.0, 7e6 * (1 + 0.001 * (i % 3)), 21 + i)
           for i in range(3)]
    )
    result = qm.detect_change(series)
    assert result.scanned_eligible_days == 23
    assert result.input_eligible_days == 23
