"""A milestone's cost is measured from the credit, not from a display rewrite.

A crossing recorded after a credit must carry the cost spent SINCE the credit.
The old code got there indirectly: it ran the displayed week reference through
`_apply_reset_events_to_weekrefs`, which rewrote `week_start_at` to the credit's
effective instant, and then asked `_week_ref_has_reset_event` whether the
rewrite had happened. Two things are wrong with that.

It could not see a MANUAL credit at all. `_apply_reset_events_to_weekrefs`
matches on the boundary columns, and a manual credit leaves both NULL because it
moved no boundary; so a week credited by `record-credit` fell through to the
cached FULL-WEEK cost, and every milestone after it carried the whole week's
spend as if the credit had not happened.

And it is about to stop seeing an automatic credit too. #703 + #707 stops the
display splitting a credited week, so the reference keeps its original
`week_start_at`, nothing equals any `effective_reset_at_utc`, and the same
full-week fallback would take over for every credit.

The authoritative condition is `reset_event_id != 0` — the epoch this crossing
was actually filed under — and the range is built directly from that epoch's
accounting instant to the unchanged week end. Segment 0 keeps the cached
full-week path.
"""
from __future__ import annotations

import pytest

from conftest import load_script, redirect_paths


_WEEK_START = "2026-01-01"
_WEEK_END = "2026-01-07"
_WEEK_START_AT = "2026-01-01T00:00:00+00:00"
_WEEK_END_AT = "2026-01-07T23:59:59+00:00"
_CREDIT_EFFECTIVE = "2026-01-04T09:00:00+00:00"
_CREDIT_OBSERVED = "2026-01-04T09:41:00Z"
_AS_OF = "2026-01-04T12:00:00Z"

FULL_WEEK_COST = 900.0
POST_CREDIT_COST = 12.5


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})
    # `_compute_cost_for_weekref` reaches `_sum_cost_for_range` through the
    # `cctally` namespace, so stubbing it here is what makes this test about the
    # RANGE rather than about cost arithmetic. The stub answers by start
    # instant: the week's own start is the full-week cost, the credit's
    # accounting instant is the post-credit cost, and anything else is a range
    # nobody should be asking for.
    def _sum(start, end, *, mode="auto", skip_sync=False, account_key=None):
        import datetime as dt
        if start == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc):
            return FULL_WEEK_COST
        if start == dt.datetime(2026, 1, 4, 9, 41, tzinfo=dt.timezone.utc):
            return POST_CREDIT_COST
        raise AssertionError(f"unexpected accounting range start {start!r}")
    monkeypatch.setitem(ns, "_sum_cost_for_range", _sum)
    # The pre-record cost sync writes the cached FULL-WEEK snapshot, which is
    # what the old path stamped onto a post-credit crossing.
    def _sync(args, *, conn=None, as_of=None, journal=None,
              account_key="unattributed", retained_selection=None):
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, range_start_iso, range_end_iso, cost_usd, source, "
            " mode, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (as_of or _AS_OF, _WEEK_START, _WEEK_END, _WEEK_START_AT,
             _WEEK_END_AT, _WEEK_START_AT, _AS_OF, FULL_WEEK_COST, "test",
             "auto", account_key))
        return 0
    monkeypatch.setitem(ns, "cmd_sync_week", _sync)
    return ns


def _seed_snapshot(conn, percent=1.0):
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_AS_OF, _WEEK_START, _WEEK_END, _WEEK_START_AT, _WEEK_END_AT,
         percent, "test", "{}"))
    return int(cur.lastrowid)


def _seed_manual_credit(conn):
    """A `record-credit` row: BOTH boundary columns NULL, because a manual
    credit moved no boundary and has none to record.

    The synthetic post-credit snapshot comes with it, because that is what
    `_apply_credit` writes and what the epoch's seeding guard reads: an epoch
    holding no observation at or below the credited level refuses to seed, and
    without the synthetic there would be no crossing to measure the cost of.
    """
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_CREDIT_OBSERVED, _WEEK_START, _WEEK_END, _WEEK_START_AT,
         _WEEK_END_AT, 0.0, "record-credit", "{}"))
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, observed_post_credit_pct, "
        " credit_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_CREDIT_OBSERVED, None, None, _CREDIT_EFFECTIVE, 46.0,
         "unattributed", _WEEK_START, _CREDIT_OBSERVED, 0.0, "o:manualop"))


def _saved(snap_id, percent=1.0):
    return {
        "id": snap_id,
        "weeklyPercent": percent,
        "weekStartDate": _WEEK_START,
        "weekEndDate": _WEEK_END,
        "weekStartAt": _WEEK_START_AT,
        "weekEndAt": _WEEK_END_AT,
        "capturedAt": _AS_OF,
    }


def _milestone(conn, threshold):
    return conn.execute(
        "SELECT cumulative_cost_usd, reset_event_id FROM percent_milestones "
        "WHERE week_start_date = ? AND percent_threshold = ?",
        (_WEEK_START, threshold)).fetchone()


def test_milestone_cost_measures_from_the_credit_without_a_display_rewrite(ns):
    conn = ns["open_db"]()
    try:
        _seed_manual_credit(conn)
        snap_id = _seed_snapshot(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
        row = _milestone(conn, 1)
    finally:
        conn.close()
    assert row is not None, "the crossing was not recorded at all"
    assert row["reset_event_id"] != 0, (
        "the manual credit opened no epoch, so the crossing was filed "
        "pre-credit")
    assert row["cumulative_cost_usd"] == pytest.approx(POST_CREDIT_COST)
    assert row["cumulative_cost_usd"] != pytest.approx(FULL_WEEK_COST)


def test_an_uncredited_week_keeps_the_cached_full_week_path(ns):
    """Segment 0 is unchanged: no live recompute, the cached snapshot's cost,
    and a `cost_snapshot_id` anchoring it."""
    conn = ns["open_db"]()
    try:
        snap_id = _seed_snapshot(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
        row = conn.execute(
            "SELECT cumulative_cost_usd, reset_event_id, cost_snapshot_id "
            "FROM percent_milestones WHERE percent_threshold = 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["reset_event_id"] == 0
    assert row["cumulative_cost_usd"] == pytest.approx(FULL_WEEK_COST)
    assert row["cost_snapshot_id"] != 0


def test_the_range_starts_at_the_accounting_instant_not_the_display_one(ns):
    """The hour-floored effective instant is display-only. Measuring from it
    would attribute up to an hour of pre-credit spend to the new epoch, which is
    the same back-dating §5.3 removes from the accounting floor."""
    seen = {}

    def _sum(start, end, *, mode="auto", skip_sync=False, account_key=None):
        seen["start"] = start
        seen["end"] = end
        return POST_CREDIT_COST

    conn = ns["open_db"]()
    try:
        import _cctally_core
        ns["_sum_cost_for_range"] = _sum
        _seed_manual_credit(conn)
        snap_id = _seed_snapshot(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
    finally:
        conn.close()
    assert seen, "no live cost range was computed at all"
    assert seen["start"] == _cctally_core.parse_iso_datetime(
        _CREDIT_OBSERVED, "observed")
    assert seen["start"] != _cctally_core.parse_iso_datetime(
        _CREDIT_EFFECTIVE, "effective")
    # The week END is unchanged: a credit is a counter discontinuity inside an
    # unchanged window, so the range runs to the week's own end (clamped by the
    # triggering observation clock the caller supplies).
    assert seen["end"] == _cctally_core.parse_iso_datetime(_AS_OF, "asOf")
