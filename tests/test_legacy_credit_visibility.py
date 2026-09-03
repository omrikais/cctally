"""A credit written before the credit columns existed is still a credit.

#703 + #707 §5.3. ``resolve_weekly_credit_epoch`` matches a keyless legacy row
by its recorded boundaries, and the boundary it matched — ``new_week_end_at`` —
used to be guaranteed to equal the reference's own end, because
``_apply_reset_events_to_weekrefs`` rewrote the post-credit reference's end to
exactly that value. §6.1 removed the rewrite and nothing replaced the guarantee,
so a legacy boundary-CHANGE row became invisible to every display and ``$/1%``
consumer: no marker, a ratio silently taken over the whole week, and cost read
from the ``weekly_cost_snapshots`` cache instead of the epoch's own range.

The identity a legacy row really has is the week that CONTAINS the instant it
records, so that is what the resolver matches on. Both legacy shapes are pinned
here, because they place the recorded boundaries differently: an in-place credit
writes ``old == effective`` and leaves the week's end in ``new_week_end_at``,
while a boundary change writes the prior API end in ``old_week_end_at`` and the
current one in ``new_week_end_at``.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

ACCOUNT = "unattributed"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _snap(conn, *, captured, start_date, start_at, end_at, end_date, pct):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (captured, start_date, end_date, start_at, end_at, pct,
         "statusline", "{}", ACCOUNT))


def _legacy_event(conn, *, detected, old_end, new_end, effective, pre_pct):
    """A `week_reset_events` row in the shape written before the credit columns.

    `week_start_date`, `observed_at_utc`, `credit_key` and `credit_order` are
    all absent, which is exactly what a store upgraded from an older release
    holds and what replay must never synthesize.
    """
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key) "
        "VALUES (?,?,?,?,?,?)",
        (detected, old_end, new_end, effective, pre_pct, ACCOUNT))


def _seed_boundary_change_one_key(conn):
    """The API moved the week's end; every snapshot keeps one week key.

    The reference's end is then the boundary the API stated FIRST — the value
    in `old_week_end_at` — so a resolver keyed on `new_week_end_at` sees
    nothing at all.
    """
    _snap(conn, captured="2026-04-14T12:00:00Z", start_date="2026-04-13",
          start_at="2026-04-13T14:00:00+00:00",
          end_at="2026-04-17T14:00:00+00:00", end_date="2026-04-17", pct=40.0)
    _snap(conn, captured="2026-04-17T13:00:00Z", start_date="2026-04-13",
          start_at="2026-04-13T14:00:00+00:00",
          end_at="2026-04-20T14:00:00+00:00", end_date="2026-04-20", pct=0.0)
    _snap(conn, captured="2026-04-18T13:00:00Z", start_date="2026-04-13",
          start_at="2026-04-13T14:00:00+00:00",
          end_at="2026-04-20T14:00:00+00:00", end_date="2026-04-20", pct=5.0)
    _legacy_event(conn, detected="2026-04-17T13:00:00+00:00",
                  old_end="2026-04-17T14:00:00+00:00",
                  new_end="2026-04-20T14:00:00+00:00",
                  effective="2026-04-17T13:00:00+00:00", pre_pct=40.0)
    conn.commit()


def _seed_boundary_change_two_keys(conn):
    """The operator's own shape: the moved boundary also moved the week key.

    `week_start_date` is derived from `resets_at - 7d`, so a genuine boundary
    change files the post-credit snapshots under a new key and the week renders
    as two references.
    """
    for captured, pct in (("2026-04-11T12:00:00Z", 40.0),
                          ("2026-04-16T12:00:00Z", 46.0)):
        _snap(conn, captured=captured, start_date="2026-04-10",
              start_at="2026-04-10T07:00:00+00:00",
              end_at="2026-04-17T07:00:00+00:00", end_date="2026-04-17",
              pct=pct)
    for captured, pct in (("2026-04-16T19:05:00Z", 1.0),
                          ("2026-04-18T12:00:00Z", 9.0)):
        _snap(conn, captured=captured, start_date="2026-04-16",
              start_at="2026-04-16T19:00:00+00:00",
              end_at="2026-04-23T19:00:00+00:00", end_date="2026-04-23",
              pct=pct)
    _legacy_event(conn, detected="2026-04-16T19:05:00+00:00",
                  old_end="2026-04-17T07:00:00+00:00",
                  new_end="2026-04-23T19:00:00+00:00",
                  effective="2026-04-16T19:00:00+00:00", pre_pct=46.0)
    conn.commit()


def _seed_in_place(conn):
    """The 2026-09-01 incident's own shape: `old == effective`, week unchanged."""
    for captured, pct in (("2026-08-31T12:00:00Z", 67.0),
                          ("2026-09-01T18:05:00Z", 0.0),
                          ("2026-09-02T11:00:00Z", 3.0)):
        _snap(conn, captured=captured, start_date="2026-08-29",
              start_at="2026-08-29T05:00:00+00:00",
              end_at="2026-09-05T05:00:00+00:00", end_date="2026-09-05",
              pct=pct)
    _legacy_event(conn, detected="2026-09-01T17:59:41+00:00",
                  old_end="2026-09-01T17:00:00+00:00",
                  new_end="2026-09-05T05:00:00+00:00",
                  effective="2026-09-01T17:00:00+00:00", pre_pct=67.0)
    conn.commit()


def _epochs_by_week(ns, conn):
    refs = ns["get_recent_weeks"](conn, 8, account_key=ACCOUNT)
    return {
        r.week_start.isoformat(): ns["_week_ref_credit_epoch"](
            conn, r, account_key=ACCOUNT)
        for r in refs
    }


def test_a_legacy_boundary_change_credit_is_visible_on_its_own_week(ns):
    conn = ns["open_db"]()
    try:
        _seed_boundary_change_one_key(conn)
        epochs = _epochs_by_week(ns, conn)
        assert epochs["2026-04-13"] is not None, epochs
    finally:
        conn.close()


def test_a_legacy_boundary_change_credit_marks_that_weeks_bucket_key(ns):
    import types

    conn = ns["open_db"]()
    try:
        _seed_boundary_change_one_key(conn)
        week = types.SimpleNamespace(
            start_date=dt.date(2026, 4, 13),
            start_ts="2026-04-13T14:00:00+00:00",
            end_ts="2026-04-17T14:00:00+00:00")
        assert ns["_credited_week_keys"](
            conn, [week], account_key=ACCOUNT) == {"2026-04-13"}
    finally:
        conn.close()


def test_a_split_legacy_boundary_change_marks_only_the_credited_reference(ns):
    """The credit lands in the window the counter continued in, and only there.

    Under the moved key the week renders as two references. The credit instant
    is the later reference's own start, so that is the reference whose low
    `Used %` the marker explains; the earlier one holds no credit at all.
    """
    conn = ns["open_db"]()
    try:
        _seed_boundary_change_two_keys(conn)
        epochs = _epochs_by_week(ns, conn)
        assert epochs["2026-04-16"] is not None, epochs
        assert epochs["2026-04-10"] is None, epochs
    finally:
        conn.close()


def test_a_legacy_in_place_credit_stays_visible(ns):
    conn = ns["open_db"]()
    try:
        _seed_in_place(conn)
        epochs = _epochs_by_week(ns, conn)
        assert epochs["2026-08-29"] is not None, epochs
    finally:
        conn.close()


def test_the_two_display_layers_agree_about_a_legacy_credited_week(ns):
    """The WeekRef layer and the SubWeek layer must not disagree (#703 §6.3).

    `build_trend_view` and `report` resolve the epoch through a `WeekRef`; the
    `weekly` table and the dashboard's weekly panel resolve it through a
    `SubWeek`. The two carry different ends for a boundary-change week — the
    reference keeps the boundary the API stated first, while the sub-week is
    carried forward to the one it stated second — so while the legacy row was
    matched on `new_week_end_at` alone, one layer saw the credit and the other
    did not. The dashboard then reported the same week as credited with a
    live-computed cost in its weekly panel and as uncredited with no cost at all
    in its trend.
    """
    conn = ns["open_db"]()
    try:
        _seed_boundary_change_one_key(conn)
        now = dt.datetime(2026, 4, 19, 12, 0, tzinfo=dt.timezone.utc)
        trend = ns["build_trend_view"](conn, now_utc=now, n=4, skip_sync=True)
        sub_weeks = ns["_compute_subscription_weeks"](
            conn, dt.datetime(2026, 4, 6, tzinfo=dt.timezone.utc), now,
            account_key=ACCOUNT)
        marked = ns["_credited_week_keys"](
            conn, sub_weeks, account_key=ACCOUNT)
    finally:
        conn.close()
    trend_row = trend.rows[-1]
    assert trend_row.week_start_at.date() == dt.date(2026, 4, 13), trend_row
    assert trend_row.credited is True, trend_row
    assert marked == {"2026-04-13"}, marked
    # A credited week takes the live epoch-range cost path, never the
    # `weekly_cost_snapshots` cache. The cache holds nothing for this week, so
    # a `None` here is the invisible-credit failure re-appearing.
    assert trend_row.weekly_cost_usd is not None, trend_row
