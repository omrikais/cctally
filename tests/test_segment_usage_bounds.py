"""Half-open segment bounds on the weekly usage lookup (#750 S4 spec §1.2).

`_aggregate_weekly` buckets an entry into `[start_ts, end_ts)` — an exclusive
upper bound — while `_get_latest_row_for_week` selects
`captured_at_utc <= as_of_utc`, an inclusive one. A snapshot captured exactly
at a credit instant therefore went to the PRE-credit segment while an entry
stamped at the same instant went to the POST-credit one, which is #735. The
percent lookup adopts the cost side's convention.
"""
import datetime as dt
import sqlite3

import pytest

from conftest import load_script


@pytest.fixture()
def cctally():
    ns = load_script()

    class _NS:
        def __getattr__(self, name):
            try:
                return ns[name]
            except KeyError as exc:  # pragma: no cover - attribute protocol
                raise AttributeError(name) from exc

    return _NS()


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
            account_key TEXT,
            -- #769 S11 (#824): `get_latest_usage_for_week` excludes held rows,
            -- so this hand-built table has to carry the column the real
            -- schema declares or every read here raises.
            weekly_observation_held INTEGER NOT NULL DEFAULT 0
                CHECK (weekly_observation_held IN (0, 1))
        )
        """
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def week_ref(cctally):
    return cctally.make_week_ref(
        week_start_date="2026-06-25",
        week_end_date="2026-07-01",
        week_start_at="2026-06-25T10:00:00+00:00",
        week_end_at="2026-07-02T10:00:00+00:00",
    )


def _seed_snapshot(conn, *, captured, pct, account_key="unattributed"):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, account_key) VALUES (?,?,?,?,?,?,?)",
        (captured, "2026-06-25", "2026-07-01", "2026-06-25T10:00:00+00:00",
         "2026-07-02T10:00:00+00:00", pct, account_key),
    )
    conn.commit()


def test_a_capture_exactly_at_the_cut_belongs_to_the_successor(
        cctally, stats_conn, week_ref):
    """Spec §1.2. Cost buckets [start, end) at bin/_lib_aggregators.py;
    the percent lookup adopts the same convention, so the cut instant is the
    successor's."""
    _seed_snapshot(stats_conn, captured="2026-06-27T10:00:00Z", pct=41.0)
    pre = cctally.get_latest_usage_for_week(
        stats_conn, week_ref,
        since_utc="2026-06-25T10:00:00Z", before_utc="2026-06-27T10:00:00Z")
    assert pre is None, "the cut instant is EXCLUSIVE for the segment ending there"
    post = cctally.get_latest_usage_for_week(
        stats_conn, week_ref, since_utc="2026-06-27T10:00:00Z")
    assert post["weekly_percent"] == 41.0, "and INCLUSIVE for the one starting there"


def test_a_credited_tail_with_no_post_cut_capture_reports_missing(
        cctally, stats_conn, week_ref):
    """Spec §1.2. Without an inclusive lower bound the tail would silently
    return the latest PRE-cut reading, which is a percent from the wrong
    cycle."""
    _seed_snapshot(stats_conn, captured="2026-06-26T09:00:00Z", pct=41.0)
    row = cctally.get_latest_usage_for_week(
        stats_conn, week_ref,
        since_utc="2026-06-27T10:00:00Z", as_of_utc="2026-06-29T12:00:00Z")
    assert row is None


def test_neither_bound_leaves_the_query_unchanged(
        cctally, stats_conn, week_ref):
    _seed_snapshot(stats_conn, captured="2026-06-26T09:00:00Z", pct=41.0)
    assert cctally.get_latest_usage_for_week(
        stats_conn, week_ref)["weekly_percent"] == 41.0


def test_the_head_segment_takes_the_latest_capture_strictly_before_its_cut(
        cctally, stats_conn, week_ref):
    _seed_snapshot(stats_conn, captured="2026-06-26T09:00:00Z", pct=12.0)
    _seed_snapshot(stats_conn, captured="2026-06-27T09:59:59Z", pct=71.0)
    _seed_snapshot(stats_conn, captured="2026-06-27T10:00:00Z", pct=3.0)
    row = cctally.get_latest_usage_for_week(
        stats_conn, week_ref,
        since_utc="2026-06-25T10:00:00Z", before_utc="2026-06-27T10:00:00Z")
    assert row["weekly_percent"] == 71.0


def test_both_bounds_compose_with_as_of_and_the_account_scope(
        cctally, stats_conn, week_ref):
    _seed_snapshot(stats_conn, captured="2026-06-27T11:00:00Z", pct=5.0,
                   account_key="acct-a")
    _seed_snapshot(stats_conn, captured="2026-06-27T12:00:00Z", pct=9.0,
                   account_key="acct-b")
    row = cctally.get_latest_usage_for_week(
        stats_conn, week_ref, as_of_utc="2026-06-29T00:00:00Z",
        since_utc="2026-06-27T10:00:00Z", account_key="acct-a")
    assert row["weekly_percent"] == 5.0


def test_the_bounds_reach_the_generic_row_primitive_too(
        cctally, stats_conn, week_ref):
    """`_get_latest_row_for_week` is shared with the cost table, so the two
    new keyword-only parameters live there and `get_latest_usage_for_week`
    forwards them."""
    _seed_snapshot(stats_conn, captured="2026-06-26T09:00:00Z", pct=41.0)
    row = cctally._get_latest_row_for_week(
        stats_conn, "weekly_usage_snapshots", week_ref,
        before_utc="2026-06-26T09:00:00Z")
    assert row is None
    assert isinstance(dt.datetime.now(dt.timezone.utc), dt.datetime)


# --- one instant, two spellings: both consumers must agree ------------------
#
# `latest_usage_by_segment` ranks candidates by the PARSED capture instant
# while this primitive ordered and filtered on the stored TEXT, so on rows
# mixing offsets the two could pick different rows for one segment. Both
# cases below are constructed so a textual answer and an instant answer
# differ outright.

def _seed_spelled(conn, *, captured_text, pct):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, account_key) VALUES (?,?,?,?,?,?,?)",
        (captured_text, "2026-06-25", "2026-07-01",
         "2026-06-25T10:00:00+00:00", "2026-07-02T10:00:00+00:00", pct,
         "unattributed"),
    )
    conn.commit()


def test_the_latest_row_is_the_latest_instant_not_the_greatest_text(
        cctally, stats_conn, week_ref):
    """The 22.0 row is captured an hour EARLIER but spelled `+03:00`, so its
    text sorts after the 55.0 row's. A lexical `ORDER BY captured_at_utc DESC`
    returns 22.0."""
    _seed_spelled(stats_conn, captured_text="2026-06-26T12:00:00+03:00",
                  pct=22.0)   # 09:00Z
    _seed_spelled(stats_conn, captured_text="2026-06-26T10:00:00Z", pct=55.0)
    row = cctally.get_latest_usage_for_week(stats_conn, week_ref)
    assert row["weekly_percent"] == 55.0


def test_the_as_of_bound_admits_a_row_a_textual_compare_would_drop(
        cctally, stats_conn, week_ref):
    """The only row is captured at 09:00Z, an hour inside the as-of, but its
    `+03:00` text compares greater than the as-of's. A lexical `<=` drops it
    and the week reads as having no observation at all."""
    _seed_spelled(stats_conn, captured_text="2026-06-26T12:00:00+03:00",
                  pct=22.0)   # 09:00Z
    row = cctally.get_latest_usage_for_week(
        stats_conn, week_ref, as_of_utc="2026-06-26T09:30:00Z")
    assert row is not None and row["weekly_percent"] == 22.0
