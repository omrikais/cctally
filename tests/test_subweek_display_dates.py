"""Tests for SubWeek.display_start_date and the reset-event post-processor
that overrides it for post-reset weeks. Pre-reset weeks pass through
unchanged (only end_ts / end_date shift, both pre-existing semantics)."""
import datetime as dt
import sqlite3

import pytest

from conftest import load_script


def _make_subweek(ns, *, start_iso, end_iso, source="snapshot"):
    """Build a SubWeek with display_start_date defaulting to start_date
    (mirroring _compute_subscription_weeks)."""
    SubWeek = ns["SubWeek"]
    parse = ns["parse_iso_datetime"]
    s_dt = parse(start_iso, "test.start")
    e_dt = parse(end_iso, "test.end")
    s_date = s_dt.astimezone().date()
    e_date = (e_dt - dt.timedelta(seconds=1)).astimezone().date()
    return SubWeek(
        start_ts=start_iso,
        end_ts=end_iso,
        start_date=s_date,
        end_date=e_date,
        source=source,
        display_start_date=s_date,
    )


def test_subweek_default_display_start_date_matches_start_date():
    ns = load_script()
    sw = _make_subweek(
        ns,
        start_iso="2026-04-09T15:00:00+00:00",
        end_iso="2026-04-16T15:00:00+00:00",
    )
    assert sw.display_start_date == sw.start_date


def test_apply_reset_events_overrides_post_reset_display_start_date():
    """When a SubWeek's end_ts equals a reset event's new_week_end_at, the
    POST-reset week's start_ts and display_start_date both move to
    effective_reset_at_utc. start_date (the bucket / lookup key) must NOT
    shift — it stays the API-derived backdated date."""
    ns = load_script()
    apply_events = ns["_apply_reset_events_to_subweeks"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            UNIQUE(old_week_end_at, new_week_end_at)
        )
    """)
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc) "
        "VALUES (?, ?, ?, ?)",
        ("2026-04-13T18:01:00Z",
         "2026-04-16T15:00:00+00:00",
         "2026-04-18T15:00:00+00:00",
         "2026-04-13T18:00:00+00:00"),
    )

    pre = _make_subweek(
        ns,
        start_iso="2026-04-09T15:00:00+00:00",
        end_iso="2026-04-16T15:00:00+00:00",
    )
    post = _make_subweek(
        ns,
        start_iso="2026-04-11T15:00:00+00:00",  # API-derived backdated start
        end_iso="2026-04-18T15:00:00+00:00",
    )

    out = apply_events(conn, [pre, post])
    assert len(out) == 2
    pre_out, post_out = out

    # Pre-reset: end_ts moved to reset moment (existing behavior). Both
    # end_date and display_start_date stay aligned with their source
    # (start_date unchanged; end_date shifted by existing code).
    assert pre_out.end_ts == "2026-04-13T18:00:00+00:00"
    assert pre_out.end_date == dt.date(2026, 4, 13)
    assert pre_out.start_date == dt.date(2026, 4, 9)
    assert pre_out.display_start_date == dt.date(2026, 4, 9)

    # Post-reset: start_ts moved to reset moment; display_start_date follows.
    assert post_out.start_ts == "2026-04-13T18:00:00+00:00"
    assert post_out.display_start_date == dt.date(2026, 4, 13)
    # Bucket / lookup key intact (still 2026-04-11, the API-derived date).
    assert post_out.start_date == dt.date(2026, 4, 11)


def test_apply_reset_events_no_event_passes_through():
    """When no reset events exist, display_start_date == start_date for
    every SubWeek."""
    ns = load_script()
    apply_events = ns["_apply_reset_events_to_subweeks"]

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            UNIQUE(old_week_end_at, new_week_end_at)
        )
    """)

    sw = _make_subweek(
        ns,
        start_iso="2026-04-09T15:00:00+00:00",
        end_iso="2026-04-16T15:00:00+00:00",
    )
    out = apply_events(conn, [sw])
    assert out[0].display_start_date == out[0].start_date == dt.date(2026, 4, 9)


def _credit_conn(*, old_end, new_end, effective):
    """An in-memory `week_reset_events` table holding one event row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE week_reset_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at_utc TEXT NOT NULL,
            old_week_end_at TEXT NOT NULL,
            new_week_end_at TEXT NOT NULL,
            effective_reset_at_utc TEXT NOT NULL,
            UNIQUE(old_week_end_at, new_week_end_at)
        )
    """)
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, effective_reset_at_utc) "
        "VALUES (?, ?, ?, ?)",
        ("2026-05-15T17:01:00Z", old_end, new_end, effective),
    )
    return conn


def test_apply_reset_events_splits_in_place_credit_into_two_subweeks():
    """An in-place credit (`old_week_end_at == effective_reset_at_utc`) ends
    one billing cycle and begins another INSIDE the same week, so the
    credited SubWeek must come back as TWO contiguous segments.

    Before this behavior existed, `_apply_reset_events_to_subweeks` only
    shifted the surviving SubWeek's `start_ts` forward to `effective`, and
    every entry in `[original_start, effective)` fell into a gap that
    `_aggregate_weekly` drops — silent data loss, not just a missing row.
    """
    ns = load_script()
    apply_events = ns["_apply_reset_events_to_subweeks"]

    week_start = "2026-05-09T15:00:00+00:00"
    week_end = "2026-05-16T15:00:00+00:00"
    effective = "2026-05-15T17:00:00+00:00"
    conn = _credit_conn(old_end=effective, new_end=week_end,
                        effective=effective)

    sw = _make_subweek(ns, start_iso=week_start, end_iso=week_end)
    out = apply_events(conn, [sw])

    assert len(out) == 2, [(w.start_ts, w.end_ts) for w in out]
    pre, post = out

    # Ascending by start instant — `_aggregate_weekly`'s bisect and
    # `_apply_overlap_clamp_to_subweeks` both declare that a precondition.
    assert pre.start_ts == week_start
    assert pre.end_ts == effective
    assert post.start_ts == effective
    assert post.end_ts == week_end

    # Both keep the same `start_date`: it is the join key into
    # `weekly_usage_snapshots.week_start_date`, which the credit never moves.
    assert pre.start_date == post.start_date == dt.date(2026, 5, 9)

    # ...which is exactly why the two need a DISTINCT bucket identity.
    assert pre.segment_key != post.segment_key

    # Display dates track each segment's own start.
    assert pre.display_start_date == dt.date(2026, 5, 9)
    assert post.display_start_date == dt.date(2026, 5, 15)


def test_subweek_segment_key_is_the_utc_canonicalized_start_instant():
    """`segment_key` must be the UTC-canonicalized instant, not the raw
    string: `_aggregate_buckets` returns `sorted(by_bucket.keys())` and every
    consumer reads that order as chronological, while SubWeek timestamps may
    carry non-UTC offsets."""
    ns = load_script()
    same_instant_utc = _make_subweek(
        ns, start_iso="2026-05-09T15:00:00+00:00",
        end_iso="2026-05-16T15:00:00+00:00",
    )
    same_instant_offset = _make_subweek(
        ns, start_iso="2026-05-09T18:00:00+03:00",
        end_iso="2026-05-16T18:00:00+03:00",
    )
    assert (same_instant_utc.segment_key
            == same_instant_offset.segment_key)

    earlier = _make_subweek(
        ns, start_iso="2026-05-09T23:00:00+00:00",
        end_iso="2026-05-16T23:00:00+00:00",
    )
    later = _make_subweek(
        ns, start_iso="2026-05-10T01:00:00+03:00",  # 2026-05-09T22:00Z
        end_iso="2026-05-17T01:00:00+03:00",
    )
    # `later` is chronologically EARLIER; a raw-string key would sort it after.
    assert later.segment_key < earlier.segment_key


# The three `week_reset_events` row shapes both appliers handle, plus one
# spelling variant. Each entry is
# (case id, week_start_at, week_end_at, old_end, new_end, effective,
#  expected segment count).
#
#   in_place_credit        — `old_week_end_at == effective_reset_at_utc`; the
#                            week's own end matches `new_week_end_at`. Two
#                            billing cycles come back.
#   pre_reset_truncation   — the week's end matches `old_week_end_at` and the
#                            credit is NOT in place, so the week is cut short
#                            at `effective` and stays one segment.
#   post_reset_shift       — the week's end matches `new_week_end_at` for a
#                            non-in-place event, so the week starts at
#                            `effective` and stays one segment.
#   in_place_credit_offset — the in-place case with the week's bounds written
#                            in +03:00 while the event row is written in UTC.
#                            `make_week_ref` canonicalizes a WeekRef's
#                            timestamps to UTC, so this spelling difference
#                            is absorbed before either applier sees it.
#   event_row_offset       — the in-place case with the EVENT ROW written in
#                            +03:00 while the week is written in UTC. Neither
#                            applier canonicalizes `week_reset_events` text,
#                            so this is the spelling difference that actually
#                            reaches the matching step, and it is where the
#                            twins' strategies can disagree:
#                            `_apply_reset_events_to_subweeks` compares parsed
#                            instants and `_apply_reset_events_to_weekrefs`
#                            compared raw strings through `pre_map` /
#                            `post_map`, so the weekrefs twin silently
#                            recognized nothing and rendered one row where the
#                            subweeks twin rendered two.
_APPLIER_PARITY_CASES = [
    (
        "in_place_credit",
        "2026-05-09T15:00:00+00:00", "2026-05-16T15:00:00+00:00",
        "2026-05-15T17:00:00+00:00", "2026-05-16T15:00:00+00:00",
        "2026-05-15T17:00:00+00:00",
        2,
    ),
    (
        "pre_reset_truncation",
        "2026-04-09T15:00:00+00:00", "2026-04-16T15:00:00+00:00",
        "2026-04-16T15:00:00+00:00", "2026-04-20T15:00:00+00:00",
        "2026-04-13T18:00:00+00:00",
        1,
    ),
    (
        "post_reset_shift",
        "2026-04-13T15:00:00+00:00", "2026-04-20T15:00:00+00:00",
        "2026-04-16T15:00:00+00:00", "2026-04-20T15:00:00+00:00",
        "2026-04-13T18:00:00+00:00",
        1,
    ),
    (
        "in_place_credit_offset",
        "2026-05-09T18:00:00+03:00", "2026-05-16T18:00:00+03:00",
        "2026-05-15T17:00:00+00:00", "2026-05-16T15:00:00+00:00",
        "2026-05-15T17:00:00+00:00",
        2,
    ),
    (
        "event_row_offset",
        "2026-05-09T15:00:00+00:00", "2026-05-16T15:00:00+00:00",
        "2026-05-15T20:00:00+03:00", "2026-05-16T18:00:00+03:00",
        "2026-05-15T20:00:00+03:00",
        2,
    ),
]


@pytest.mark.parametrize(
    "case_id,week_start,week_end,old_end,new_end,effective,expected_segments",
    _APPLIER_PARITY_CASES,
    ids=[c[0] for c in _APPLIER_PARITY_CASES],
)
def test_subweek_and_weekref_appliers_agree_on_every_event_shape(
    case_id, week_start, week_end, old_end, new_end, effective,
    expected_segments,
):
    """Cross-applier parity: `_apply_reset_events_to_subweeks` and
    `_apply_reset_events_to_weekrefs` must produce the same segment count
    and the same interval bounds for the same `week_reset_events` row, on
    EVERY row shape the two handle.

    Nothing structurally ties the twins together — the weekrefs applier
    grew the in-place-credit case in v1.7.2 and the subweeks applier did
    not, which is how `weekly` came to silently drop a billing cycle that
    `report` renders. This test is the tie, so it must cover all three
    shapes rather than only the credit one. The `in_place_credit_offset`
    case additionally pins the two matching strategies together: a week
    whose bounds are written in a non-UTC offset must be recognized by both
    appliers, not only by the one that parses before comparing.
    """
    ns = load_script()
    apply_subweeks = ns["_apply_reset_events_to_subweeks"]
    apply_weekrefs = ns["_apply_reset_events_to_weekrefs"]
    make_ref = ns["make_week_ref"]
    parse = ns["parse_iso_datetime"]

    sw_conn = _credit_conn(old_end=old_end, new_end=new_end,
                           effective=effective)
    ref_conn = _credit_conn(old_end=old_end, new_end=new_end,
                            effective=effective)

    sw_out = apply_subweeks(
        sw_conn, [_make_subweek(ns, start_iso=week_start, end_iso=week_end)]
    )
    start_date = parse(week_start, "case.start").astimezone().date()
    end_date = parse(week_end, "case.end").astimezone().date()
    ref_out = apply_weekrefs(ref_conn, [make_ref(
        week_start_date=start_date.isoformat(),
        week_end_date=end_date.isoformat(),
        week_start_at=week_start, week_end_at=week_end,
    )])

    assert len(sw_out) == expected_segments, [
        (w.start_ts, w.end_ts) for w in sw_out
    ]
    assert len(ref_out) == expected_segments, [
        (r.week_start_at, r.week_end_at) for r in ref_out
    ]

    def _bounds_sw(w):
        return (parse(w.start_ts, "sw.start"), parse(w.end_ts, "sw.end"))

    def _bounds_ref(r):
        return (parse(r.week_start_at, "ref.start"),
                parse(r.week_end_at, "ref.end"))

    assert (sorted(_bounds_sw(w) for w in sw_out)
            == sorted(_bounds_ref(r) for r in ref_out))
