"""A credited week's `$/1%` takes BOTH halves from the epoch (#703 + #707 §6.3).

The ratio needs both halves changed, not just the numerator. Pairing post-credit
spend with the absolute stored percent is wrong: if a credit lands at 2% and the
counter climbs to 3%, that spend bought ONE percentage point, not three, and
dividing by three understates the rate threefold. Immediately after a credit the
absolute divisor is the landing level itself against approximately zero spend.

When the counter has not climbed past the level it was credited to, the ratio is
withheld with a typed cause rather than rendered — the `explain` command's rule
that a withheld quantity prints its cause instead of a misleading `$0.00`.

An uncredited week keeps the full-week numerator over the absolute divisor,
unchanged, so no golden without a credit moves.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

WEEK_START_DATE = "2026-08-29"
WEEK_END_DATE = "2026-09-05"
WEEK_START_AT = "2026-08-29T05:00:00+00:00"
WEEK_END_AT = "2026-09-05T05:00:00+00:00"
CREDIT_AT = "2026-09-01T17:00:00+00:00"
NOW_UTC = dt.datetime(2026, 9, 2, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _snapshot(conn, captured_at, percent):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (captured_at, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
         WEEK_END_AT, percent, "statusline", "{}", "unattributed"))


def _cost_snapshot(conn, cost_usd):
    conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, cost_usd, mode) VALUES (?,?,?,?,?,?,?)",
        ("2026-09-02T11:00:00Z", WEEK_START_DATE, WEEK_END_DATE,
         WEEK_START_AT, WEEK_END_AT, cost_usd, "auto"))


def _credit(conn, *, post_pct, observed_at=CREDIT_AT):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, confirming_capture_at_utc, "
        " observed_post_credit_pct, credit_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (observed_at, observed_at, WEEK_END_AT, observed_at, 67.0,
         "unattributed", WEEK_START_DATE, observed_at, observed_at,
         post_pct, "o:credit"))


def _row(ns, monkeypatch, *, post_pct, current_pct, post_credit_cost,
         full_week_cost=500.0, with_credit=True):
    """Build the trend row for the week, with `_compute_cost_for_weekref`
    pinned so the epoch numerator is an input rather than a JSONL fixture."""
    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-08-31T12:00:00Z", 67.0)
        _snapshot(conn, "2026-09-02T11:00:00Z", current_pct)
        _cost_snapshot(conn, full_week_cost)
        if with_credit:
            _credit(conn, post_pct=post_pct)
        conn.commit()
    finally:
        conn.close()

    seen: list = []

    def _fake_cost(ref, *, skip_sync=False, account_key=None, as_of=None):
        seen.append(ref.week_start_at)
        if ref.week_start_at == CREDIT_AT:
            return post_credit_cost
        return full_week_cost

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", _fake_cost)

    conn = ns["open_db"]()
    try:
        view = ns["build_trend_view"](
            conn, now_utc=NOW_UTC, n=4, display_tz=None, skip_sync=True)
    finally:
        conn.close()
    row = next(r for r in view.rows
               if r.week_start_date == dt.date.fromisoformat(WEEK_START_DATE))
    return row, seen


def test_the_divisor_is_the_climb_not_the_level(ns, monkeypatch):
    """Credit lands at 2.0, the counter is now 3.0, post-credit spend is $50.

    The honest rate is $50 per percentage point. The absolute divisor would give
    $16.67, understating it threefold.
    """
    row, _seen = _row(ns, monkeypatch, post_pct=2.0, current_pct=3.0,
                      post_credit_cost=50.0)
    assert row.dollars_per_percent == pytest.approx(50.0), (
        row.dollars_per_percent)
    assert row.dpp_withheld_cause is None
    assert row.credited is True


def test_the_numerator_is_the_epoch_not_the_week(ns, monkeypatch):
    """The displayed total cost stays the FULL week's spend, so the numerator is
    a separate computation over the credit's own range."""
    row, seen = _row(ns, monkeypatch, post_pct=2.0, current_pct=3.0,
                     post_credit_cost=50.0, full_week_cost=500.0)
    assert CREDIT_AT in seen, seen
    assert row.weekly_cost_usd == pytest.approx(500.0), row.weekly_cost_usd
    assert row.dollars_per_percent == pytest.approx(50.0)


def test_a_week_with_no_climb_yet_withholds_the_ratio(ns, monkeypatch):
    """The counter has not climbed past the level it was credited to, so there
    is no divisor the epoch supports."""
    row, _seen = _row(ns, monkeypatch, post_pct=2.0, current_pct=2.0,
                      post_credit_cost=0.5)
    assert row.dollars_per_percent is None
    assert row.dpp_withheld_cause == "no-climb-since-credit"
    assert row.credited is True


def test_an_uncredited_week_is_unchanged(ns, monkeypatch):
    """The full-week numerator over the absolute divisor, exactly as before."""
    row, _seen = _row(ns, monkeypatch, post_pct=2.0, current_pct=40.0,
                      post_credit_cost=50.0, full_week_cost=500.0,
                      with_credit=False)
    assert row.credited is False
    assert row.dpp_withheld_cause is None
    assert row.dollars_per_percent == pytest.approx(500.0 / 40.0)


def test_a_credit_with_no_landing_level_keeps_the_full_week_ratio(
        ns, monkeypatch):
    """A row written before #707 records no landing level, so there is no honest
    divisor to build from it. It renders what it rendered before the change
    rather than withholding a value the store can still support."""
    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-08-31T12:00:00Z", 67.0)
        _snapshot(conn, "2026-09-02T11:00:00Z", 40.0)
        _cost_snapshot(conn, 500.0)
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date) VALUES (?,?,?,?,?,?,?)",
            (CREDIT_AT, CREDIT_AT, WEEK_END_AT, CREDIT_AT, 67.0,
             "unattributed", WEEK_START_DATE))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setitem(
        ns, "_compute_cost_for_weekref",
        lambda ref, **kw: 500.0)
    conn = ns["open_db"]()
    try:
        view = ns["build_trend_view"](
            conn, now_utc=NOW_UTC, n=4, display_tz=None, skip_sync=True)
    finally:
        conn.close()
    row = next(r for r in view.rows
               if r.week_start_date == dt.date.fromisoformat(WEEK_START_DATE))
    assert row.credited is True
    assert row.dpp_withheld_cause is None
    assert row.dollars_per_percent == pytest.approx(500.0 / 40.0)


def test_two_credits_in_one_week_use_the_LATEST_epoch(ns, monkeypatch):
    """The governing epoch is the latest one at or before the capture, through
    the same shared resolver every accounting reader uses."""
    conn = ns["open_db"]()
    try:
        _snapshot(conn, "2026-08-31T12:00:00Z", 67.0)
        _snapshot(conn, "2026-09-02T11:00:00Z", 12.0)
        _cost_snapshot(conn, 500.0)
        _credit(conn, post_pct=2.0, observed_at="2026-08-31T13:00:00+00:00")
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, confirming_capture_at_utc, "
            " observed_post_credit_pct, credit_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (CREDIT_AT, CREDIT_AT, WEEK_END_AT, CREDIT_AT, 30.0,
             "unattributed", WEEK_START_DATE, CREDIT_AT, CREDIT_AT, 10.0,
             "o:second"))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setitem(
        ns, "_compute_cost_for_weekref",
        lambda ref, **kw: 20.0 if ref.week_start_at == CREDIT_AT else 500.0)
    conn = ns["open_db"]()
    try:
        view = ns["build_trend_view"](
            conn, now_utc=NOW_UTC, n=4, display_tz=None, skip_sync=True)
    finally:
        conn.close()
    row = next(r for r in view.rows
               if r.week_start_date == dt.date.fromisoformat(WEEK_START_DATE))
    # The SECOND credit governs: 12.0 - 10.0 = 2.0 points bought $20.
    assert row.dollars_per_percent == pytest.approx(10.0), (
        row.dollars_per_percent)
