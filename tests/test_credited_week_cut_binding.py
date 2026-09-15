"""A credited week's cut binding survives a moved tail end (#750 S4 §1.1).

The first shape of the consumer-side predicate keyed the cut mapping on
`week_reset_events.new_week_end_at` and then looked it up with a segment's
POST-applier `end_ts`. Two ordinary mechanisms move a tail's end away from
that column, and both are exercised here:

* `_apply_overlap_clamp_to_subweeks` runs AFTER the reset applier
  (bin/_lib_subscription_weeks.py) and clamps a tail's end to the next week's
  start when the successor's anchor drifted earlier;
* a boundary-shift event whose `old_week_end_at` equals this week's end
  rewrites `end_ts` to the shift instant.

When the lookup missed, no segment's end matched, the week read as
uncredited, every bound degraded to ``(None, None)`` and BOTH cycles resolved
to the latest observation in the week — the post-credit reading assigned to
the pre-credit cycle, which is #731 / #736 verbatim. It failed silently, so
these cases assert the resolved percentages rather than the mapping's shape.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from conftest import load_script


WEEK_START = dt.datetime(2026, 4, 6, 15, tzinfo=dt.timezone.utc)
CUT = dt.datetime(2026, 4, 9, 15, tzinfo=dt.timezone.utc)
WEEK_END = dt.datetime(2026, 4, 13, 15, tzinfo=dt.timezone.utc)
#: The tail end after `_apply_overlap_clamp_to_subweeks` pulled it back to the
#: successor's drifted anchor. It is no longer `new_week_end_at`.
CLAMPED_END = dt.datetime(2026, 4, 13, 9, tzinfo=dt.timezone.utc)
#: The tail end after a boundary-shift event whose `old_week_end_at` is this
#: week's end rewrote it to the shift instant.
SHIFTED_END = dt.datetime(2026, 4, 12, 8, tzinfo=dt.timezone.utc)


def _z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


@pytest.fixture()
def ns():
    return load_script()


@pytest.fixture()
def stats_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL,
            account_key TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            observed_pre_credit_pct REAL,
            account_key TEXT,
            origin_observation_id TEXT
        )
        """
    )
    # `_reset_aware_floor` UNIONs both recorded tables, so an absent
    # `weekly_credit_floors` makes the whole query raise and
    # `segment_capture_bounds` degrade to "no floor". Both tables are present
    # on any real store, so both are present here.
    conn.execute(
        """
        CREATE TABLE weekly_credit_floors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start_date TEXT NOT NULL,
            effective_at_utc TEXT NOT NULL,
            observed_pre_credit_pct REAL NOT NULL,
            applied_at_utc TEXT NOT NULL,
            account_key TEXT NOT NULL DEFAULT 'unattributed'
        )
        """
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _seed_event(conn, *, old, new, eff):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, account_key) VALUES (?,?,?,?,?)",
        (_z(WEEK_END), old, new, eff, "unattributed"),
    )
    conn.commit()


def _seed_snapshot(conn, *, captured, pct):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, account_key) VALUES (?,?,?,?,?,?,?)",
        (_z(captured), WEEK_START.date().isoformat(),
         WEEK_END.date().isoformat(), _z(WEEK_START), _z(WEEK_END), pct,
         "unattributed"),
    )
    conn.commit()


def _segments(ns, *, tail_end: dt.datetime):
    """The two cycles the appliers emit, with the tail's end already moved.

    Both keep `start_date`, which is the join key the credit does not move;
    `segment_key` is what tells them apart (epic invariant 3).
    """
    sub_week = ns["SubWeek"]
    return [
        sub_week(
            start_ts=_z(WEEK_START), end_ts=_z(CUT),
            start_date=WEEK_START.date(),
            end_date=(CUT - dt.timedelta(seconds=1)).date(),
            source="snapshot", display_start_date=WEEK_START.date(),
        ),
        sub_week(
            start_ts=_z(CUT), end_ts=_z(tail_end),
            start_date=WEEK_START.date(),
            end_date=(tail_end - dt.timedelta(seconds=1)).date(),
            source="snapshot", display_start_date=CUT.date(),
        ),
    ]


def _resolve(ns, conn, segments):
    return ns["latest_usage_by_segment"](conn, segments, account_key=None)


@pytest.mark.parametrize(
    "tail_end, why",
    [
        (CLAMPED_END, "_apply_overlap_clamp_to_subweeks pulled the tail's "
                      "end back to the successor's drifted anchor"),
        (SHIFTED_END, "a boundary-shift event rewrote the tail's end to the "
                      "shift instant"),
    ],
    ids=["overlap-clamp", "boundary-shift"],
)
def test_each_cycle_keeps_its_own_percent_when_the_tail_end_moved(
        ns, stats_conn, tail_end, why):
    """The pre-credit cycle must keep its own reading, not the post-credit
    one. This is the silent failure: nothing raises, and both cycles simply
    report the week's latest percentage."""
    _seed_event(stats_conn, old=_z(CUT), new=_z(WEEK_END), eff=_z(CUT))
    _seed_snapshot(stats_conn, captured=dt.datetime(
        2026, 4, 8, 12, tzinfo=dt.timezone.utc), pct=40.0)
    _seed_snapshot(stats_conn, captured=dt.datetime(
        2026, 4, 10, 12, tzinfo=dt.timezone.utc), pct=12.0)

    head, tail = _segments(ns, tail_end=tail_end)
    resolved = _resolve(ns, stats_conn, [head, tail])
    assert resolved[head.segment_key] == 40.0, (
        f"the pre-credit cycle received the post-credit reading because {why}"
    )
    assert resolved[tail.segment_key] == 12.0


@pytest.mark.parametrize(
    "tail_end", [CLAMPED_END, SHIFTED_END],
    ids=["overlap-clamp", "boundary-shift"],
)
def test_the_head_is_bounded_above_by_the_cut_when_the_tail_end_moved(
        ns, stats_conn, tail_end):
    """The bound itself, so a later regression is diagnosable rather than
    only visible as a wrong percentage."""
    _seed_event(stats_conn, old=_z(CUT), new=_z(WEEK_END), eff=_z(CUT))
    head, tail = _segments(ns, tail_end=tail_end)
    bounds = ns["segment_capture_bounds"](
        stats_conn, [head, tail], account_key=None)
    assert bounds[head.segment_key] == (_z(WEEK_START), _z(CUT))
    assert bounds[tail.segment_key] == (_z(CUT), None)


def test_an_uncredited_weeks_segments_are_still_unbounded(ns, stats_conn):
    """The control. A week whose only row is a boundary shift, with nothing
    else falling inside either segment, keeps both bounds ``None`` — the
    capture-jitter tolerance every uncredited week relies on."""
    _seed_event(stats_conn, old=_z(WEEK_END), new=_z(WEEK_END + dt.timedelta(
        days=7)), eff="2026-04-05T15:00:00Z")
    head, tail = _segments(ns, tail_end=WEEK_END)
    bounds = ns["segment_capture_bounds"](
        stats_conn, [head, tail], account_key=None)
    assert bounds[head.segment_key] == (None, None)
    assert bounds[tail.segment_key] == (None, None)


def test_a_cut_at_an_unshifted_weeks_own_start_is_rejected(ns, stats_conn):
    """Spec §1.1 bullet 5. Both appliers bound a cut below by the shift when
    the week carries one and by the week's OWN start when it does not
    (bin/_lib_subscription_weeks.py, bin/_cctally_weekrefs.py). The week here
    carries no shift, so a "cut" stamped at its start is outside it and must
    bound nothing.

    The UPPER bound is what a cut decides, so that is what is asserted. The
    segment's lower bound is a separate quantity here: `_reset_aware_floor`
    independently admits the same row as an in-week clamp floor, which it did
    before this session and still does."""
    _seed_event(stats_conn, old=_z(WEEK_START), new=_z(WEEK_END),
                eff=_z(WEEK_START))
    head = ns["SubWeek"](
        start_ts=_z(WEEK_START), end_ts=_z(WEEK_END),
        start_date=WEEK_START.date(),
        end_date=(WEEK_END - dt.timedelta(seconds=1)).date(),
        source="snapshot", display_start_date=WEEK_START.date(),
    )
    cuts = ns["credited_segment_cuts"](
        [WEEK_START], ns["in_place_cut_instants"](stats_conn, account_key=None))
    assert cuts == frozenset(), (
        "a cut at the week's own start is outside it, and both appliers "
        "reject it whether or not the week carries a boundary shift"
    )
    _since, before = ns["segment_capture_bounds"](
        stats_conn, [head], account_key=None)[head.segment_key]
    assert before is None, "an admitted cut would have bounded this segment"


# --- the automatic-reset floor on an UNCREDITED, boundary-shifted week ------
#
# `_reset_aware_floor` is now called with the SEGMENT's bounds rather than the
# snapshot row's (#750 S4 §2.1), and `_apply_reset_events_to_subweeks` always
# sets a pre-shift week's end to the reset instant. The floor's reset leg
# admits an event whose effective instant lies in `[start, end)`, so on a
# pre-shift segment that leg is now structurally inert: the instant IS the
# segment's exclusive end. Week-scoped it was live there, because it read the
# snapshot row's stale unclamped `week_end_at`, and it discarded that week's
# own pre-shift captures.
#
# That is a behaviour change on an UNCREDITED week, which §2.1 authorizes by
# re-deriving the automatic leg. It is pinned here so it is a decision rather
# than a side effect, and so the two share `project-md-{anon,reveal}` goldens
# have something that argues for them. Those two goldens moved for a DIFFERENT
# reason — the fixture store carries no `week_reset_events` and no
# `weekly_credit_floors` row at all, so no floor leg runs on it; there the old
# map keyed on `weekly_usage_snapshots.week_start_at` while `cmd_project`
# looked up a `SubWeek.start_ts` that anchor discovery had moved to the capture
# instant, and one week resolved to nothing.

SHIFT = dt.datetime(2026, 4, 12, 8, tzinfo=dt.timezone.utc)
PRE_SHIFT_START = dt.datetime(2026, 4, 6, 15, tzinfo=dt.timezone.utc)
POST_SHIFT_END = dt.datetime(2026, 4, 19, 15, tzinfo=dt.timezone.utc)


def _shifted_pair(ns):
    """The two UNCREDITED weeks a boundary shift leaves behind.

    The applier clamps the first week's end to the shift instant and moves the
    second week's start there, while the second week's `start_date` stays the
    unshifted join key.
    """
    sub_week = ns["SubWeek"]
    return [
        sub_week(
            start_ts=_z(PRE_SHIFT_START), end_ts=_z(SHIFT),
            start_date=PRE_SHIFT_START.date(),
            end_date=(SHIFT - dt.timedelta(seconds=1)).date(),
            source="snapshot", display_start_date=PRE_SHIFT_START.date(),
        ),
        sub_week(
            start_ts=_z(SHIFT), end_ts=_z(POST_SHIFT_END),
            start_date=dt.date(2026, 4, 13),
            end_date=(POST_SHIFT_END - dt.timedelta(seconds=1)).date(),
            source="snapshot", display_start_date=SHIFT.date(),
        ),
    ]


def _seed_shift_event(conn):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, account_key) VALUES (?,?,?,?,?)",
        (_z(SHIFT), "2026-04-13T15:00:00Z", "2026-04-20T15:00:00Z",
         _z(SHIFT), "unattributed"),
    )
    conn.commit()


def _seed_dated_snapshot(conn, *, week_start_date, captured, pct):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, weekly_percent, account_key) "
        "VALUES (?,?,?,?)",
        (_z(captured), week_start_date.isoformat(), pct, "unattributed"),
    )
    conn.commit()


def test_a_pre_shift_segment_keeps_its_own_captures(ns, stats_conn):
    _seed_shift_event(stats_conn)
    pre, post = _shifted_pair(ns)
    bounds = ns["segment_capture_bounds"](
        stats_conn, [pre, post], account_key=None)
    assert bounds[pre.segment_key] == (None, None), (
        "the reset leg cannot fire on a segment whose exclusive end IS the "
        "reset instant"
    )
    _seed_dated_snapshot(
        stats_conn, week_start_date=PRE_SHIFT_START.date(),
        captured=dt.datetime(2026, 4, 10, 9, tzinfo=dt.timezone.utc), pct=62.0)
    resolved = ns["latest_usage_by_segment"](
        stats_conn, [pre, post], account_key=None)
    assert resolved[pre.segment_key] == 62.0


def test_the_post_shift_segment_is_still_floored_at_the_shift(ns, stats_conn):
    """The other half of the same change. The leg stays LIVE where the shift
    falls inside the segment, so a capture predating the shift is still
    discarded from the week that starts at it."""
    _seed_shift_event(stats_conn)
    pre, post = _shifted_pair(ns)
    bounds = ns["segment_capture_bounds"](
        stats_conn, [pre, post], account_key=None)
    assert bounds[post.segment_key] == (_z(SHIFT), None)
    _seed_dated_snapshot(
        stats_conn, week_start_date=dt.date(2026, 4, 13),
        captured=dt.datetime(2026, 4, 11, 9, tzinfo=dt.timezone.utc), pct=90.0)
    _seed_dated_snapshot(
        stats_conn, week_start_date=dt.date(2026, 4, 13),
        captured=dt.datetime(2026, 4, 14, 9, tzinfo=dt.timezone.utc), pct=20.0)
    resolved = ns["latest_usage_by_segment"](
        stats_conn, [pre, post], account_key=None)
    assert resolved[post.segment_key] == 20.0
