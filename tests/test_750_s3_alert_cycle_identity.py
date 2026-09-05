"""A weekly threshold alert names the billing cycle that crossed (#750 S3).

Before S3 a subscription week held at most one in-place credit, so the week
and the cycle that crossed were the same thing and naming the week identified
the crossing. S3 makes a week N-ary: a week credited twice holds three cycles
under one ``week_start_date``, and "Week starting Jun 05" then names all three
at once. The alert already carries ``reset_event_id``, which says WHICH cycle
crossed, but nothing on the wire says when that cycle began, so no reader can
turn the segment number into a sentence.

Both publish paths therefore carry ``context.cycle_start_at`` — the start
instant of the cycle the crossing belongs to. It is the governing reset
event's ``effective_reset_at_utc`` for a post-credit segment and the week's
own start instant for segment 0, so an uncredited week publishes the value it
always did and renders byte-identically.

``context.week_start_at`` is deliberately NOT reused for this. Both scope
kernels — ``_lib_alert_scope._scope_weekly`` and its client twin in
``dashboard/web/src/lib/alertScope.ts`` — derive the window end by adding
seven days to it, and a credit does not move the week's end. Overloading the
field would push the derived end past the real one by the whole credit
offset, so the last test here pins that the field still holds the stored week
boundary.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from conftest import load_isolated_cctally_module

UTC = ZoneInfo("UTC")

WEEK_START_DATE = "2026-06-05"
WEEK_START_AT = "2026-06-05T15:00:00Z"
WEEK_END_AT = "2026-06-12T15:00:00Z"
# The instant the second in-place credit took effect: three days into a week
# whose boundaries the credit does not move.
SECOND_CREDIT_AT = "2026-06-08T20:00:00Z"
ACCOUNT = "deadbeefdeadbeef"


@pytest.fixture
def cc(tmp_path, monkeypatch):
    return load_isolated_cctally_module(tmp_path, monkeypatch)


def _seed_credit(conn, *, effective_at, origin):
    """One in-place credit that leaves both week boundaries where they were.

    ``origin_observation_id`` is what makes a SECOND credit of one week
    insertable at all: identity is dual-shaped (§1.1), and a row naming its
    originating observation is unique on ``(account_key,
    origin_observation_id)`` rather than on the boundary pair two credits of
    the same week necessarily share.
    """
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, account_key, origin_observation_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (effective_at, WEEK_END_AT, WEEK_END_AT, effective_at, ACCOUNT,
         origin),
    )
    return int(cur.lastrowid)


def _seed_milestone(conn, *, threshold, reset_event_id, alerted_at):
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " usage_snapshot_id, cost_snapshot_id, reset_event_id, account_key, "
        " alerted_at) "
        "VALUES (?, ?, '2026-06-12', ?, ?, ?, 47.32, 0, 0, ?, ?, ?)",
        (alerted_at, WEEK_START_DATE, WEEK_START_AT, WEEK_END_AT,
         threshold, reset_event_id, ACCOUNT, alerted_at),
    )


def _weekly_rows(conn):
    import _cctally_dashboard_envelope as envelope

    return [
        row for row in envelope._build_alerts_envelope_array(conn)
        if row["axis"] == "weekly"
    ]


def test_envelope_names_the_cycle_each_crossing_belongs_to(cc):
    """Three cohorts share one week, so the week alone identifies none of them.

    The published row for cohort 2 must say the cycle began on Jun 08, which
    is the only value that distinguishes it from the two crossings of the same
    week at the same threshold ladder.
    """
    conn = cc.open_db()
    try:
        first = _seed_credit(
            conn, effective_at="2026-06-06T18:00:00Z", origin="obs-1")
        second = _seed_credit(
            conn, effective_at=SECOND_CREDIT_AT, origin="obs-2")
        _seed_milestone(conn, threshold=95, reset_event_id=0,
                        alerted_at="2026-06-06T09:00:00Z")
        _seed_milestone(conn, threshold=95, reset_event_id=first,
                        alerted_at="2026-06-07T09:00:00Z")
        _seed_milestone(conn, threshold=95, reset_event_id=second,
                        alerted_at="2026-06-09T09:00:00Z")
        conn.commit()
        rows = _weekly_rows(conn)
    finally:
        conn.close()

    by_segment = {
        row["context"]["reset_event_id"]: row["context"]["cycle_start_at"]
        for row in rows
    }
    assert by_segment == {
        0: WEEK_START_AT,
        first: "2026-06-06T18:00:00Z",
        second: SECOND_CREDIT_AT,
    }


def test_envelope_cycle_start_is_the_week_start_on_an_uncredited_week(cc):
    """Segment 0 has no reset event, so the cycle IS the week."""
    conn = cc.open_db()
    try:
        _seed_milestone(conn, threshold=90, reset_event_id=0,
                        alerted_at="2026-06-06T09:00:00Z")
        conn.commit()
        [row] = _weekly_rows(conn)
    finally:
        conn.close()

    assert row["context"]["cycle_start_at"] == WEEK_START_AT


def test_envelope_withholds_the_cycle_start_on_a_pre_column_row(cc):
    """A row predating ``week_start_at`` retains no instant to publish.

    The empty string is the established treatment for exactly this case on
    this axis (``week_start_at`` itself, and ``block_start_at`` on the
    five-hour axis): the key stays on the wire and the reader degrades rather
    than a clock reading being invented for it.
    """
    conn = cc.open_db()
    try:
        conn.execute(
            "INSERT INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, "
            " percent_threshold, cumulative_cost_usd, usage_snapshot_id, "
            " cost_snapshot_id, reset_event_id, account_key, alerted_at) "
            "VALUES (?, ?, '2026-06-12', 100, 12.0, 0, 0, 0, ?, ?)",
            ("2026-06-06T09:00:00Z", WEEK_START_DATE, ACCOUNT,
             "2026-06-06T09:00:00Z"),
        )
        conn.commit()
        [row] = _weekly_rows(conn)
    finally:
        conn.close()

    assert row["context"]["cycle_start_at"] == ""


def _dispatch(cc, payload):
    import _cctally_alerts

    sink: list[list[str]] = []
    _cctally_alerts._dispatch_alert_notification(
        payload,
        popen_factory=(lambda args, **kwargs: sink.append(list(args))),
        mode="real",
        platform="linux",
        which_on_path=lambda name: name == "notify-send",
        tz=UTC,
    )
    assert sink, "the notifier was never spawned"
    return "\n".join(sink[0])


def test_notification_names_the_cycle_when_a_credit_opened_one(cc):
    """The rendered sentence, not the builder's return value.

    A subtitle that never reaches a notifier proves nothing about what a
    person reads, so this asserts on the bytes the spawn was handed.
    """
    payload = cc._build_alert_payload_weekly(
        threshold=95,
        crossed_at_utc="2026-06-09T09:00:00Z",
        week_start_date=WEEK_START_DATE,
        week_start_at=WEEK_START_AT,
        cycle_start_at=SECOND_CREDIT_AT,
        cumulative_cost_usd=47.32,
        dollars_per_percent=0.5,
        account_key=ACCOUNT,
    )
    text = _dispatch(cc, payload)
    assert "Week starting Fri, Jun 05 · cycle from Mon, Jun 08 20:00 UTC" in text


def test_notification_is_unchanged_when_the_cycle_is_the_week(cc):
    """An uncredited week names one thing, because there is one thing."""
    payload = cc._build_alert_payload_weekly(
        threshold=95,
        crossed_at_utc="2026-06-06T09:00:00Z",
        week_start_date=WEEK_START_DATE,
        week_start_at=WEEK_START_AT,
        cumulative_cost_usd=47.32,
        dollars_per_percent=0.5,
        account_key=ACCOUNT,
    )
    text = _dispatch(cc, payload)
    assert "Week starting Fri, Jun 05" in text
    assert "cycle from" not in text


def test_week_start_at_still_holds_the_stored_week_boundary(cc):
    """The rejected fix, pinned so it cannot be reintroduced.

    Both scope kernels derive the window end as ``week_start_at + 7 days``,
    and an in-place credit moves neither week boundary. Putting the cycle
    instant in this field would state a window ending three days after the
    week the alert actually fired against.
    """
    import _lib_alert_scope

    payload = cc._build_alert_payload_weekly(
        threshold=95,
        crossed_at_utc="2026-06-09T09:00:00Z",
        week_start_date=WEEK_START_DATE,
        week_start_at=WEEK_START_AT,
        cycle_start_at=SECOND_CREDIT_AT,
        cumulative_cost_usd=47.32,
        dollars_per_percent=0.5,
        account_key=ACCOUNT,
    )
    context = payload["context"]
    assert context["week_start_at"] == WEEK_START_AT
    scope = _lib_alert_scope.derive_alert_scope(
        "weekly", context, payload["account_key"]
    )
    assert scope.window_end == dt.datetime(2026, 6, 12, 15, tzinfo=dt.timezone.utc)
