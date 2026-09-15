"""The in-place cut helper (#750 S4 spec §1.1).

The helper must reproduce BOTH appliers' decisions exactly. Every case
here corresponds to one of the five details the spec names, because a
helper that gets any of them wrong reintroduces the applier/consumer
drift invariant 1 exists to prevent.
"""
import datetime as dt
import sqlite3

import pytest

from conftest import load_script


def _events_conn():
    """An in-memory `week_reset_events` table shaped like the real one."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
    conn.commit()
    return conn


def _seed_event(conn, *, old, new, eff, account_key="unattributed"):
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key) "
        "VALUES (?,?,?,?,?,?)",
        ("2026-06-27T10:00:05+00:00", old, new, eff, 46.0, account_key),
    )
    conn.commit()


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
    conn = _events_conn()
    try:
        yield conn
    finally:
        conn.close()


def test_raw_string_equality_decides_in_place_not_a_canonical_compare(
        cctally, stats_conn):
    """A row whose two columns denote ONE instant in DIFFERENT spellings is
    NOT in-place, because both appliers compare the raw column values
    (bin/_lib_subscription_weeks.py:327, bin/_cctally_weekrefs.py:239).
    Canonicalizing here would bound a segment neither applier split."""
    _seed_event(stats_conn,
                old="2026-06-27T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00",
                eff="2026-06-27T10:00:00Z")          # same instant, other spelling
    cuts = cctally.in_place_cut_instants(stats_conn, account_key=None)
    assert cuts == frozenset(), (
        "a differently-spelled pair must not be treated as in-place, "
        "because the appliers' raw comparison does not treat it as one"
    )


def test_a_boundary_shift_is_not_a_cut(cctally, stats_conn):
    """Invariant 2: old != effective is a boundary shift and must not split."""
    _seed_event(stats_conn,
                old="2026-06-30T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00",
                eff="2026-06-27T10:00:00+00:00")
    assert cctally.in_place_cut_instants(
        stats_conn, account_key=None) == frozenset()


def test_two_cuts_in_one_week_are_both_returned(cctally, stats_conn):
    for eff in ("2026-06-29T10:00:00+00:00", "2026-06-27T10:00:00+00:00"):
        _seed_event(stats_conn, old=eff, new="2026-07-02T10:00:00+00:00",
                    eff=eff)
    cuts = cctally.in_place_cut_instants(stats_conn, account_key=None)
    assert sorted(c.isoformat() for c in cuts) == [
        "2026-06-27T10:00:00+00:00", "2026-06-29T10:00:00+00:00",
    ]


def test_the_week_is_matched_by_parsed_instant_not_by_raw_new_week_end(
        cctally, stats_conn):
    """new_week_end_at carries mixed spellings too, so the per-week grouping
    that bounds each cut is a datetime comparison even though the in-place
    TEST is raw. The two rows below spell ONE week end two ways: grouped by
    the raw text the boundary shift would land in a different bucket from the
    cut it must reject, and the cut would be admitted."""
    _seed_event(stats_conn, old="2026-06-25T10:00:00+00:00",
                new="2026-07-02T10:00:00Z", eff="2026-06-28T10:00:00+00:00")
    _seed_event(stats_conn, old="2026-06-27T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00", eff="2026-06-27T10:00:00+00:00")
    assert cctally.in_place_cut_instants(
        stats_conn, account_key=None) == frozenset(), (
        "the cut predates the boundary shift recorded against the same week "
        "end in another spelling, so it is outside this week"
    )


def test_the_read_is_scoped_to_one_account(cctally, stats_conn):
    """Both appliers scope with WHERE account_key = ?; unscoped, one
    account's credit would bound another account's segment."""
    _seed_event(stats_conn, old="2026-06-27T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00", eff="2026-06-27T10:00:00+00:00",
                account_key="acct-a")
    assert cctally.in_place_cut_instants(
        stats_conn, account_key="acct-b") == frozenset()
    assert cctally.in_place_cut_instants(
        stats_conn, account_key="acct-a") != frozenset()


def test_duplicate_rows_for_one_physical_reset_yield_one_cut(
        cctally, stats_conn):
    for _ in range(2):
        _seed_event(stats_conn, old="2026-06-27T10:00:00+00:00",
                    new="2026-07-02T10:00:00+00:00",
                    eff="2026-06-27T10:00:00+00:00")
    cuts = cctally.in_place_cut_instants(stats_conn, account_key=None)
    assert len(cuts) == 1


def test_a_cut_at_or_after_the_week_end_is_rejected(cctally, stats_conn):
    """Spec §1.1: cuts outside the week's effective bounds are rejected,
    the same bound `_ordered_in_place_cuts` (bin/_cctally_core.py:3303)
    already applies inside both appliers."""
    _seed_event(stats_conn, old="2026-07-02T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00", eff="2026-07-02T10:00:00+00:00")
    assert cctally.in_place_cut_instants(
        stats_conn, account_key=None) == frozenset()


def test_a_cut_before_a_boundary_shifted_start_is_rejected(
        cctally, stats_conn):
    """The appliers bound each cut by the week's EFFECTIVE start, which a
    boundary-shift row moves forward. A cut that predates that shift is not
    inside this week and must not be returned."""
    # Boundary shift: this week's start moves forward to 06-28T10:00.
    _seed_event(stats_conn, old="2026-06-25T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00", eff="2026-06-28T10:00:00+00:00")
    # An in-place credit stamped BEFORE that shifted start.
    _seed_event(stats_conn, old="2026-06-27T10:00:00+00:00",
                new="2026-07-02T10:00:00+00:00", eff="2026-06-27T10:00:00+00:00")
    assert cctally.in_place_cut_instants(
        stats_conn, account_key=None) == frozenset()


def test_a_missing_events_table_yields_no_cuts(cctally):
    """`build_weekly_view`'s unit fixtures hold only `weekly_usage_snapshots`.
    A consumer-side read of an absent table degrades to "no credit", which is
    the pre-S4 behaviour, rather than raising inside a render path."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        assert cctally.in_place_cut_instants(
            conn, account_key=None) == frozenset()
    finally:
        conn.close()


def test_the_returned_instants_are_timezone_aware_utc(cctally, stats_conn):
    _seed_event(stats_conn, old="2026-06-29T10:00:00+03:00",
                new="2026-07-02T10:00:00+00:00", eff="2026-06-29T10:00:00+03:00")
    cuts = cctally.in_place_cut_instants(stats_conn, account_key=None)
    (instant,) = cuts
    assert instant == dt.datetime(2026, 6, 29, 7, 0, tzinfo=dt.timezone.utc)
