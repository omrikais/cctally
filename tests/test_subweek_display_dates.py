"""Tests for SubWeek.display_start_date and the reset-event post-processor
that overrides it for post-reset weeks. Pre-reset weeks pass through
unchanged (only end_ts / end_date shift, both pre-existing semantics)."""
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from conftest import load_script


_OPEN_CONNS: "list[sqlite3.Connection]" = []


def _track(conn):
    """Register a test-owned connection for teardown, and return it."""
    _OPEN_CONNS.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_tracked_conns():
    """Close every connection this module opened, after each test.

    Most call sites pass the connection straight into an applier, so there is
    no name to close it by. Left to the garbage collector they emitted 25
    `ResourceWarning: unclosed database` on the remote run, and an on-disk one
    is a file still open when `tmp_path` is removed. One teardown covers every
    call site, including the ones inside the seeding helpers.
    """
    yield
    while _OPEN_CONNS:
        _OPEN_CONNS.pop().close()


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

    conn = _track(sqlite3.connect(":memory:"))
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

    conn = _track(sqlite3.connect(":memory:"))
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
    conn = _track(sqlite3.connect(":memory:"))
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

# The two-credit case cannot be expressed as a single event row, so it is
# parametrized separately: a week credited twice must come back as THREE
# segments from both twins.
_TWO_CREDIT_WEEK_START = "2026-05-09T15:00:00+00:00"
_TWO_CREDIT_WEEK_END = "2026-05-16T15:00:00+00:00"
_TWO_CREDIT_CUTS = ("2026-05-11T10:00:00+00:00", "2026-05-14T16:00:00+00:00")


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


def _events_conn(tmp_path, rows, *, name="stats.db"):
    """A real stats.db carrying the given `week_reset_events` rows.

    Built by `_fixture_builders.create_stats_db` rather than by a hand-written
    `CREATE TABLE`, because the table's identity shape changed in epoch 1013
    (two partial unique indexes replaced one table-level UNIQUE) and a second
    copy of the DDL in this file would keep asserting against the retired
    shape. Each row is `(account_key, effective, old_end, new_end, origin)`.

    Rows are inserted in the order given, so the caller controls whether row
    id order and instant order agree — which is the only thing that makes an
    `ORDER BY` on the instant testable.
    """
    import _fixture_builders as fixtures

    path = tmp_path / name
    fixtures.create_stats_db(path)
    conn = _track(sqlite3.connect(path))
    conn.row_factory = sqlite3.Row
    for account, effective, old_end, new_end, origin in rows:
        fixtures.seed_week_reset_event(
            conn,
            detected_at_utc="2026-05-15T17:01:00Z",
            old_week_end_at=old_end,
            new_week_end_at=new_end,
            effective_reset_at_utc=effective,
            account_key=account,
            origin_observation_id=origin,
        )
    conn.commit()
    return conn


def _multi_account_credit_conn(tmp_path, *, name="stats.db"):
    """Two accounts, each with one in-place credit, on weeks that share an end.

    The end instant is the only thing either applier matches an event on, so
    this is the shape where an account-blind read misattributes: account A's
    week is split at account B's cut and vice versa.
    """
    return _events_conn(
        tmp_path,
        [
            ("acct-a", "2026-05-12T11:00:00+00:00",
             "2026-05-12T11:00:00+00:00", "2026-05-16T15:00:00+00:00", None),
            ("acct-b", "2026-05-14T19:00:00+00:00",
             "2026-05-14T19:00:00+00:00", "2026-05-16T15:00:00+00:00", None),
        ],
        name=name,
    )


_MULTI_ACCOUNT_WEEK_START = "2026-05-09T15:00:00+00:00"
_MULTI_ACCOUNT_WEEK_END = "2026-05-16T15:00:00+00:00"


@pytest.mark.parametrize(
    "account_key,expected_cut",
    [
        ("acct-a", "2026-05-12T11:00:00+00:00"),
        ("acct-b", "2026-05-14T19:00:00+00:00"),
    ],
)
def test_reset_event_appliers_scope_their_events_to_one_account(
    account_key, expected_cut, tmp_path,
):
    """Asked for one account, both appliers must split that account's week at
    that account's cut and at no other.

    Before both appliers took `account_key` they read `week_reset_events`
    with no predicate at all, so the requesting account could not be
    expressed. Every consumer therefore saw the union of every account's
    cuts, and the applier had no way to answer the question `report
    --account` and `weekly --account` were already asking.
    """
    ns = load_script()
    apply_subweeks = ns["_apply_reset_events_to_subweeks"]
    apply_weekrefs = ns["_apply_reset_events_to_weekrefs"]
    make_ref = ns["make_week_ref"]
    parse = ns["parse_iso_datetime"]

    sw_out = apply_subweeks(
        _multi_account_credit_conn(tmp_path),
        [_make_subweek(ns, start_iso=_MULTI_ACCOUNT_WEEK_START,
                       end_iso=_MULTI_ACCOUNT_WEEK_END)],
        account_key=account_key,
    )
    start_date = parse(_MULTI_ACCOUNT_WEEK_START, "case.start").astimezone().date()
    end_date = parse(_MULTI_ACCOUNT_WEEK_END, "case.end").astimezone().date()
    ref_out = apply_weekrefs(
        _multi_account_credit_conn(tmp_path, name="stats-refs.db"),
        [make_ref(
            week_start_date=start_date.isoformat(),
            week_end_date=end_date.isoformat(),
            week_start_at=_MULTI_ACCOUNT_WEEK_START,
            week_end_at=_MULTI_ACCOUNT_WEEK_END,
        )],
        account_key=account_key,
    )

    cut = parse(expected_cut, "case.cut")
    assert [(parse(w.start_ts, "sw.start"), parse(w.end_ts, "sw.end"))
            for w in sw_out] == [
        (parse(_MULTI_ACCOUNT_WEEK_START, "sw.week_start"), cut),
        (cut, parse(_MULTI_ACCOUNT_WEEK_END, "sw.week_end")),
    ]
    assert sorted(
        (parse(r.week_start_at, "ref.start"), parse(r.week_end_at, "ref.end"))
        for r in ref_out
    ) == [
        (parse(_MULTI_ACCOUNT_WEEK_START, "ref.week_start"), cut),
        (cut, parse(_MULTI_ACCOUNT_WEEK_END, "ref.week_end")),
    ]


def test_reset_event_appliers_keep_the_merged_read_on_no_account(tmp_path):
    """`account_key=None` stays the account-blind merged read.

    It is what every analytics caller that asks no account question passes,
    and it is what keeps a single-account install byte-identical. The
    assertion is that the merged read reaches an event the account-scoped
    read excludes, which is what distinguishes "no predicate" from "the
    predicate happened to match".
    """
    ns = load_script()
    apply_subweeks = ns["_apply_reset_events_to_subweeks"]
    parse = ns["parse_iso_datetime"]

    def _cuts(account_key):
        out = apply_subweeks(
            _multi_account_credit_conn(
                tmp_path, name=f"stats-{account_key or 'merged'}.db"),
            [_make_subweek(ns, start_iso=_MULTI_ACCOUNT_WEEK_START,
                           end_iso=_MULTI_ACCOUNT_WEEK_END)],
            account_key=account_key,
        )
        return {parse(w.start_ts, "sw.start") for w in out}

    merged = _cuts(None)
    assert merged - _cuts("acct-a"), (merged, _cuts("acct-a"))


def _two_credit_conn(tmp_path, *, name="stats.db"):
    """One week, credited twice, as two in-place `week_reset_events` rows.

    Both rows carry the week's unchanged `new_week_end_at`, which is exactly
    the collision that used to lose the first credit: the subweeks twin held a
    single `pre_credit` slot and the weekrefs twin keyed `post_map` on the
    parsed end instant beside a bare `in_place_credit_new_ends` set.
    """
    # Inserted NEWEST FIRST so the ascending output cannot come from row order.
    return _events_conn(
        tmp_path,
        [("unattributed", effective, effective, _TWO_CREDIT_WEEK_END, None)
         for effective in reversed(_TWO_CREDIT_CUTS)],
        name=name,
    )


def test_both_appliers_emit_three_segments_for_a_twice_credited_week(tmp_path):
    """A week credited twice is THREE billing cycles, from both twins.

    Each twin held one slot for one credit, so the second event overwrote the
    first and the interval between the two cuts was lost: it belonged to no
    segment, and `_aggregate_weekly` drops an interval no SubWeek covers.
    """
    ns = load_script()
    apply_subweeks = ns["_apply_reset_events_to_subweeks"]
    apply_weekrefs = ns["_apply_reset_events_to_weekrefs"]
    make_ref = ns["make_week_ref"]
    parse = ns["parse_iso_datetime"]

    sw_out = apply_subweeks(
        _two_credit_conn(tmp_path),
        [_make_subweek(ns, start_iso=_TWO_CREDIT_WEEK_START,
                       end_iso=_TWO_CREDIT_WEEK_END)],
    )
    start_date = parse(_TWO_CREDIT_WEEK_START, "case.start").astimezone().date()
    end_date = parse(_TWO_CREDIT_WEEK_END, "case.end").astimezone().date()
    ref_out = apply_weekrefs(
        _two_credit_conn(tmp_path, name="stats-refs.db"),
        [make_ref(
            week_start_date=start_date.isoformat(),
            week_end_date=end_date.isoformat(),
            week_start_at=_TWO_CREDIT_WEEK_START,
            week_end_at=_TWO_CREDIT_WEEK_END,
        )],
    )

    c1 = parse(_TWO_CREDIT_CUTS[0], "cut.1")
    c2 = parse(_TWO_CREDIT_CUTS[1], "cut.2")
    week_start = parse(_TWO_CREDIT_WEEK_START, "week.start")
    week_end = parse(_TWO_CREDIT_WEEK_END, "week.end")
    expected = [(week_start, c1), (c1, c2), (c2, week_end)]

    # Identical segment counts and identical bounds from both twins.
    assert len(sw_out) == 3, [(w.start_ts, w.end_ts) for w in sw_out]
    assert len(ref_out) == 3, [(r.week_start_at, r.week_end_at) for r in ref_out]

    sw_bounds = [(parse(w.start_ts, "sw.start"), parse(w.end_ts, "sw.end"))
                 for w in sw_out]
    ref_bounds = [(parse(r.week_start_at, "ref.start"),
                   parse(r.week_end_at, "ref.end")) for r in ref_out]
    assert sw_bounds == expected
    assert sorted(ref_bounds) == expected

    # Gap-free ascending coverage: each segment starts where the last ended.
    for earlier, later in zip(sw_bounds, sw_bounds[1:]):
        assert earlier[1] == later[0]

    # One shared week identity, three distinct segment identities.
    assert {w.start_date for w in sw_out} == {start_date}
    assert len({w.segment_key for w in sw_out}) == 3
    assert len({r.key for r in ref_out}) == 1

    # The weekrefs twin is newest-first, because the ref it replaces already
    # sat in a DESC-ordered list.
    assert ref_bounds == sorted(expected, reverse=True)


def test_subweeks_applier_ignores_a_cut_outside_the_week_it_names():
    """A cut on or outside the week's own bounds is not a billing cycle.

    Splitting there emits a zero-width segment, which `_aggregate_weekly`
    would key as a bucket that can never receive an entry.
    """
    ns = load_script()
    apply_subweeks = ns["_apply_reset_events_to_subweeks"]

    for effective in (_TWO_CREDIT_WEEK_START, _TWO_CREDIT_WEEK_END,
                      "2026-05-02T15:00:00+00:00"):
        out = apply_subweeks(
            _credit_conn(old_end=effective, new_end=_TWO_CREDIT_WEEK_END,
                         effective=effective),
            [_make_subweek(ns, start_iso=_TWO_CREDIT_WEEK_START,
                           end_iso=_TWO_CREDIT_WEEK_END)],
        )
        assert len(out) == 1, (effective, [(w.start_ts, w.end_ts) for w in out])
        assert out[0].start_ts == _TWO_CREDIT_WEEK_START
        assert out[0].end_ts == _TWO_CREDIT_WEEK_END


# --- #750 S3, Unit B review -------------------------------------------------
#
# Two shapes the original §2.1 enumeration missed. Both twins answered them by
# row order, which is a different question from the one
# `_latest_reset_event_for_end` answers, so two consumers of the same store
# disagreed about where a week starts.

_SHIFT_WEEK_START = "2026-06-05T15:00:00+00:00"
_SHIFT_WEEK_END = "2026-06-12T15:00:00+00:00"
_SHIFT_PREV_START = "2026-06-01T15:00:00+00:00"
_SHIFT_PREV_END = "2026-06-08T15:00:00+00:00"
_SHIFT_EARLY = "2026-06-06T10:00:00+00:00"
_SHIFT_MID = "2026-06-06T22:00:00+00:00"
_SHIFT_LATE = "2026-06-07T10:00:00+00:00"
_SHIFT_CUT = "2026-06-09T09:00:00+00:00"


def _weekref_for(ns, start_iso, end_iso):
    parse = ns["parse_iso_datetime"]
    return ns["make_week_ref"](
        week_start_date=parse(
            start_iso, "case.start").astimezone().date().isoformat(),
        week_end_date=parse(
            end_iso, "case.end").astimezone().date().isoformat(),
        week_start_at=start_iso,
        week_end_at=end_iso,
    )


def test_both_appliers_take_the_latest_boundary_shift_not_the_last_written(
    tmp_path,
):
    """Two boundary-shift rows sharing one `(old_end, new_end)` pair.

    Epoch 1013 admits that pair twice whenever the rows carry distinct
    origins, and §1.5 records the immediate-fire residual that produces it.
    The appliers held ONE slot per week for the shift and filled it from
    whichever row the unordered scan reached last, while
    `_latest_reset_event_for_end` answers with the latest INSTANT. So
    `weekly` / `report` / `project` / the dashboard placed the week's start at
    one reset and `diff` / `percent-breakdown` placed it at another.

    The winning row is written NEITHER first NOR last, so neither an
    unordered scan (which SQLite answers in rowid order) nor an `ORDER BY id
    DESC` reaches it. Two rows could only separate the clause from one of
    those two mutations; three separate it from both. The chokepoint is
    asserted alongside the two appliers so the three cannot drift apart.
    """
    ns = load_script()
    parse = ns["parse_iso_datetime"]
    rows = [
        ("unattributed", _SHIFT_EARLY, _SHIFT_PREV_END, _SHIFT_WEEK_END,
         "obs-early"),
        ("unattributed", _SHIFT_LATE, _SHIFT_PREV_END, _SHIFT_WEEK_END,
         "obs-late"),
        ("unattributed", _SHIFT_MID, _SHIFT_PREV_END, _SHIFT_WEEK_END,
         "obs-mid"),
    ]

    sw_conn = _events_conn(tmp_path, rows)
    ref_conn = _events_conn(tmp_path, rows, name="stats-refs.db")
    chokepoint_conn = _events_conn(tmp_path, rows, name="stats-choke.db")
    sw_out = ns["_apply_reset_events_to_subweeks"](
        sw_conn,
        [_make_subweek(ns, start_iso=_SHIFT_WEEK_START,
                       end_iso=_SHIFT_WEEK_END)],
    )
    ref_out = ns["_apply_reset_events_to_weekrefs"](
        ref_conn, [_weekref_for(ns, _SHIFT_WEEK_START, _SHIFT_WEEK_END)])
    chokepoint = ns["_latest_reset_event_for_end"](
        chokepoint_conn, _SHIFT_WEEK_END, account_key=None)

    late = parse(_SHIFT_LATE, "shift.late")
    assert len(sw_out) == 1 and len(ref_out) == 1
    assert parse(sw_out[0].start_ts, "sw.start") == late
    assert parse(ref_out[0].week_start_at, "ref.start") == late
    assert parse(
        chokepoint["effective_reset_at_utc"], "choke.eff") == late


def test_a_week_with_a_shift_and_a_credit_starts_its_head_at_the_shift(
    tmp_path,
):
    """A week carrying BOTH a boundary shift and an in-place credit.

    The head segment used to derive its start from the API-derived week start,
    which discards the shift: the week then overlaps the previous one over
    `[api_start, shift)`, and `_apply_overlap_clamp_to_subweeks` resolves that
    overlap by moving the previous week's spend into this week's head.
    """
    ns = load_script()
    parse = ns["parse_iso_datetime"]
    rows = [
        # The boundary shift: this week's API-derived start backdates into the
        # previous week, so the week really begins at `_SHIFT_EARLY`.
        ("unattributed", _SHIFT_EARLY, _SHIFT_PREV_END, _SHIFT_WEEK_END,
         "obs-shift"),
        # The in-place credit, strictly inside the shifted week.
        ("unattributed", _SHIFT_CUT, _SHIFT_CUT, _SHIFT_WEEK_END, "obs-cut"),
    ]

    sw_conn = _events_conn(tmp_path, rows)
    ref_conn = _events_conn(tmp_path, rows, name="stats-refs.db")
    sw_out = ns["_apply_reset_events_to_subweeks"](
        sw_conn,
        [_make_subweek(ns, start_iso=_SHIFT_WEEK_START,
                       end_iso=_SHIFT_WEEK_END)],
    )
    ref_out = ns["_apply_reset_events_to_weekrefs"](
        ref_conn, [_weekref_for(ns, _SHIFT_WEEK_START, _SHIFT_WEEK_END)])

    shift = parse(_SHIFT_EARLY, "shift")
    cut = parse(_SHIFT_CUT, "cut")
    week_end = parse(_SHIFT_WEEK_END, "week.end")
    expected = [(shift, cut), (cut, week_end)]

    sw_bounds = [(parse(w.start_ts, "sw.start"), parse(w.end_ts, "sw.end"))
                 for w in sw_out]
    ref_bounds = [(parse(r.week_start_at, "ref.start"),
                   parse(r.week_end_at, "ref.end")) for r in ref_out]
    assert sw_bounds == expected, sw_bounds
    assert sorted(ref_bounds) == expected, ref_bounds

    # The head no longer reaches back before the shift, so it cannot overlap
    # the week that ends there.
    assert parse(
        _SHIFT_WEEK_START, "week.api_start") < shift == sw_bounds[0][0]
    # The display date follows the segment's real start, not the API one.
    # Derived through `astimezone()`, the way `SubWeek` derives it, rather
    # than written as a literal: a literal is only correct at the host offsets
    # where these instants happen not to cross midnight, so it fails on a
    # developer machine at +09:00 or later while the `TZ=Etc/UTC` authoritative
    # run stays green.
    assert sw_out[0].display_start_date == shift.astimezone().date()
    # Every segment keeps the week's own join key.
    assert {w.start_date for w in sw_out} == {
        parse(_SHIFT_WEEK_START, "week.api_start").astimezone().date()}

    # End to end through the clamp, which is where the harm the docstring
    # names actually lands. A single SubWeek never reaches
    # `_apply_overlap_clamp_to_subweeks` at all — it returns its input
    # unchanged below two weeks — so the previous week has to be in the input
    # for the assertion to mean anything. With the head derived from the
    # API-derived start the previous week's end is clamped back to it, and
    # `[api_start, shift)` — real spend of the previous cycle — is bucketed
    # into the credited week's head instead.
    two_week_conn = _events_conn(tmp_path, rows, name="stats-two-week.db")
    clamped = ns["_apply_overlap_clamp_to_subweeks"](
        ns["_apply_reset_events_to_subweeks"](
            two_week_conn,
            [_make_subweek(ns, start_iso=_SHIFT_PREV_START,
                           end_iso=_SHIFT_PREV_END),
             _make_subweek(ns, start_iso=_SHIFT_WEEK_START,
                           end_iso=_SHIFT_WEEK_END)],
        )
    )
    clamped_bounds = [(parse(w.start_ts, "sw.start"), parse(w.end_ts, "sw.end"))
                      for w in clamped]
    assert clamped_bounds == [
        (parse(_SHIFT_PREV_START, "prev.start"), shift),
        (shift, cut),
        (cut, week_end),
    ], clamped_bounds


def test_a_cut_before_the_boundary_shift_is_outside_the_week(tmp_path):
    """The shift is the lower bound on the cuts, not just the head's start.

    A cut between the API-derived start and the shift belongs to the previous
    billing cycle. Admitting it would emit a head running from the shift back
    to an earlier instant — an inverted interval no consumer can render.
    """
    ns = load_script()
    parse = ns["parse_iso_datetime"]
    early_cut = "2026-06-05T20:00:00+00:00"
    rows = [
        ("unattributed", _SHIFT_EARLY, _SHIFT_PREV_END, _SHIFT_WEEK_END,
         "obs-shift"),
        ("unattributed", early_cut, early_cut, _SHIFT_WEEK_END, "obs-cut"),
    ]

    sw_conn = _events_conn(tmp_path, rows)
    ref_conn = _events_conn(tmp_path, rows, name="stats-refs.db")
    sw_out = ns["_apply_reset_events_to_subweeks"](
        sw_conn,
        [_make_subweek(ns, start_iso=_SHIFT_WEEK_START,
                       end_iso=_SHIFT_WEEK_END)],
    )
    ref_out = ns["_apply_reset_events_to_weekrefs"](
        ref_conn, [_weekref_for(ns, _SHIFT_WEEK_START, _SHIFT_WEEK_END)])

    shift = parse(_SHIFT_EARLY, "shift")
    week_end = parse(_SHIFT_WEEK_END, "week.end")
    assert [(parse(w.start_ts, "sw.start"), parse(w.end_ts, "sw.end"))
            for w in sw_out] == [(shift, week_end)]
    assert [(parse(r.week_start_at, "ref.start"),
             parse(r.week_end_at, "ref.end"))
            for r in ref_out] == [(shift, week_end)]


def test_ordered_in_place_cuts_is_the_one_home_of_the_segment_filter():
    """`_ordered_in_place_cuts` is now shared by both twins (#750 S3, Unit B).

    Each twin carried its own copy of this filter and neither copy had a test,
    so the two could drift while the cross-applier parity cases stayed green —
    none of them exercises a duplicate spelling, a boundary cut, or an absent
    start. (The two copies fell through identically; an earlier commit body
    claimed otherwise and was wrong. Consolidating them was still right,
    because one home is what keeps them identical.)

    The name promises one home, so the test asserts one home: exactly one
    definition in `bin/`, and a call to it from each twin.
    """
    import ast

    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    defs = [
        path.name
        for path in sorted(bin_dir.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and node.name == "_ordered_in_place_cuts"
    ]
    assert defs == ["_cctally_core.py"], defs
    for twin in ("_lib_subscription_weeks.py", "_cctally_weekrefs.py"):
        source = (bin_dir / twin).read_text(encoding="utf-8")
        assert "_ordered_in_place_cuts(" in source, twin

    ns = load_script()
    order = ns["_ordered_in_place_cuts"]
    parse = ns["parse_iso_datetime"]
    start, end = "2026-06-05T15:00:00+00:00", "2026-06-12T15:00:00+00:00"
    end_dt = parse(end, "week.end")

    def _raw(cuts):
        return [raw for _dt, raw in order(
            [(parse(c, "cut"), c) for c in cuts], start, end_dt)]

    # Ascending, whatever order the rows arrived in.
    assert _raw(["2026-06-09T09:00:00+00:00", "2026-06-07T09:00:00+00:00"]) == [
        "2026-06-07T09:00:00+00:00", "2026-06-09T09:00:00+00:00"]
    # Deduplicated on the INSTANT, so two spellings of one instant are one cut.
    assert _raw(["2026-06-09T09:00:00+00:00", "2026-06-09T12:00:00+03:00"]) == [
        "2026-06-09T09:00:00+00:00"]
    # Strictly inside: a cut on either boundary would be a zero-width segment.
    assert _raw([start]) == [] and _raw([end]) == []
    assert _raw(["2026-06-01T09:00:00+00:00"]) == []
    assert _raw(["2026-06-20T09:00:00+00:00"]) == []
    # No cuts at all, and the empty input, both answer with no segments.
    assert order(None, start, end_dt) == []
    assert order([], start, end_dt) == []
    # An absent or unparseable start cannot support the LOWER bound, so a cut
    # before the week is admitted rather than silently dropped. The UPPER
    # bound is a different quantity and is always available, so it still
    # applies: a cut at or after the week's end makes the tail `[cn, end)`
    # inverted or zero-width, which is not a billing cycle. The two bounds
    # used to be skipped together, and this fall-through is the case the test
    # named in its docstring and never exercised.
    def _raw_from(cuts, start_value):
        return [raw for _dt, raw in order(
            [(parse(c, "cut"), c) for c in cuts], start_value, end_dt)]

    for bad_start in (None, "", "not-a-timestamp"):
        assert _raw_from(["2026-06-01T09:00:00+00:00"], bad_start) == [
            "2026-06-01T09:00:00+00:00"]
        assert _raw_from([end], bad_start) == []
        assert _raw_from(["2026-06-20T09:00:00+00:00"], bad_start) == []
