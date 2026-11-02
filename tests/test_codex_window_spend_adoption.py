"""Codex window-scoped spend adoption.

Spec: ``docs/superpowers/specs/2026-07-30-codex-window-scoped-spend-adoption.md``.

The observation axis already adopts unidentified quota observations into a
physical window's single identified account (``adopt_unidentified_observations``,
#341 §2). The SPEND axis did not, so a Codex cycle whose ladder crossing carries
a real account could render ``$0.00`` for the segment behind it: the crossing
came from the folded observation, the dollars came from per-file attribution,
and per-file attribution had no decision covering those bytes.

This module pins the same continuity inference applied to
``codex_session_entries.account_key``: same grouping key
(``_physical_window_key``), same single-identified-account guard, account-level
weekly windows only, nominal ``[reset - window, reset)`` range, and a
cross-window agreement guard for the overlap that reset drift creates.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

import _lib_codex_account_adoption as adoption
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc

# Synthetic throughout, in the style bin/build-migrations-fixtures.py already
# uses for this migration's goldens. This module is in the PUBLIC mirror subset,
# and a real source_root_key or Codex account_key (a salted hash of account id +
# email) published here would be confirmable against the operator's own store.
ROOT = "rk"
OTHER_ROOT = "rk2"
ACCT_A = "a" * 32
ACCT_B = "b" * 32
UNATTRIBUTED = "unattributed"

WEEK = 10_080
FIVE_HOUR = 300

# The live-store cycle the defect was reproduced against, transposed onto the
# synthetic root above. Only the instants are carried over; they are not
# identifying.
RESET = dt.datetime(2026, 8, 5, 4, 35, 6, tzinfo=UTC)
NOMINAL_START = RESET - dt.timedelta(minutes=WEEK)          # 2026-07-29T04:35:06Z
BLOCK_START = dt.datetime(2026, 7, 29, 4, 35, 29, 396_000, tzinfo=UTC)
CROSSING = dt.datetime(2026, 7, 29, 5, 29, 52, 95_000, tzinfo=UTC)

WEEK_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
            f'"sourceRootKey":"{ROOT}","windowMinutes":10080}}')
FIVE_HOUR_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
                 f'"sourceRootKey":"{ROOT}","windowMinutes":300}}')
SPARK_KEY = ('{"limitId":"codex","modelPool":"gpt-5.3-codex-spark",'
             '"observedSlot":"primary","source":"codex",'
             f'"sourceRootKey":"{ROOT}","windowMinutes":10080}}')

MODEL = "gpt-5"


# --------------------------------------------------------------------------
# Pure kernel
# --------------------------------------------------------------------------

def _window(*, root=ROOT, minutes=WEEK, reset=RESET, accounts=(),
            model_scoped=False):
    return adoption.SpendAdoptionWindow(
        source_root_key=root,
        window_minutes=minutes,
        canonical_resets_at=reset,
        identified_accounts=frozenset(accounts),
        model_scoped=model_scoped,
    )


def _candidate(entry_id, timestamp, *, root=ROOT, account_key=None):
    return adoption.SpendAdoptionCandidate(
        entry_id=entry_id, source_root_key=root, timestamp=timestamp,
        account_key=account_key,
    )


def _plan(windows, candidates):
    return {
        stamp.entry_id: stamp.account_key
        for stamp in adoption.build_spend_adoption_plan(windows, candidates)
    }


def test_a_single_identified_weekly_window_adopts_its_unattributed_spend():
    """The reported defect, at kernel level."""
    windows = (_window(accounts=(ACCT_A,)),)
    candidates = (
        _candidate(1, CROSSING - dt.timedelta(minutes=30)),
        _candidate(2, CROSSING),
    )
    assert _plan(windows, candidates) == {1: ACCT_A, 2: ACCT_A}


def test_the_nominal_range_is_half_open_on_the_reset():
    windows = (_window(accounts=(ACCT_A,)),)
    candidates = (
        _candidate(1, NOMINAL_START - dt.timedelta(seconds=1)),
        _candidate(2, NOMINAL_START),
        _candidate(3, RESET - dt.timedelta(seconds=1)),
        _candidate(4, RESET),
    )
    assert _plan(windows, candidates) == {2: ACCT_A, 3: ACCT_A}


def test_a_window_with_no_identified_account_stamps_nothing():
    windows = (_window(accounts=()),)
    assert _plan(windows, (_candidate(1, CROSSING),)) == {}


def test_a_window_with_two_identified_accounts_stamps_nothing():
    """Never guess between accounts (#341 never-combine)."""
    windows = (_window(accounts=(ACCT_A, ACCT_B)),)
    assert _plan(windows, (_candidate(1, CROSSING),)) == {}


def test_overlapping_windows_that_disagree_stamp_nothing():
    """Reset drift makes consecutive weekly windows overlap. An entry claimed by
    two windows resolving to different accounts stays unattributed; the
    non-overlapping remainder of each window is still adopted."""
    later = _window(accounts=(ACCT_A,))
    # Drifted predecessor whose reset lands six hours INTO the successor.
    earlier = _window(
        accounts=(ACCT_B,), reset=NOMINAL_START + dt.timedelta(hours=6))
    overlap = NOMINAL_START + dt.timedelta(hours=3)
    after_overlap = NOMINAL_START + dt.timedelta(hours=9)
    before_overlap = NOMINAL_START - dt.timedelta(hours=3)
    plan = _plan(
        (later, earlier),
        (
            _candidate(1, overlap),
            _candidate(2, after_overlap),
            _candidate(3, before_overlap),
        ),
    )
    assert plan == {2: ACCT_A, 3: ACCT_B}


def test_overlapping_windows_that_agree_still_stamp():
    later = _window(accounts=(ACCT_A,))
    earlier = _window(
        accounts=(ACCT_A,), reset=NOMINAL_START + dt.timedelta(hours=6))
    overlap = NOMINAL_START + dt.timedelta(hours=3)
    assert _plan((later, earlier), (_candidate(1, overlap),)) == {1: ACCT_A}


def test_an_overlapping_unidentified_window_does_not_block_its_neighbour():
    """Absence of evidence is not evidence of ambiguity.

    A claiming window that identifies nobody contributes nothing to the union,
    so it cannot veto the neighbour that does name an account. Blocking on it
    was the first implementation's rule and it stamped ZERO rows against a real
    store.
    """
    later = _window(accounts=(ACCT_A,))
    earlier = _window(
        accounts=(), reset=NOMINAL_START + dt.timedelta(hours=6))
    plan = _plan(
        (later, earlier),
        (
            _candidate(1, NOMINAL_START + dt.timedelta(hours=3)),
            _candidate(2, NOMINAL_START + dt.timedelta(hours=9)),
        ),
    )
    assert plan == {1: ACCT_A, 2: ACCT_A}


def test_one_identified_window_survives_a_crowd_of_unidentified_neighbours():
    """Realistic window density, which one or two seeded windows cannot show.

    A real root carries dozens of distinct canonical weekly windows whose resets
    move by DAYS, and only the most recent ones are identified at all — the rest
    predate the durable attribution map. Ten-minute canonical anchoring cannot
    collapse day-scale movement, so the identified window is always overlapped by
    several unidentified ones.
    """
    identified = _window(accounts=(ACCT_A,))
    neighbours = tuple(
        _window(accounts=(), reset=RESET - delta)
        for delta in (dt.timedelta(hours=25), dt.timedelta(days=2),
                      dt.timedelta(days=3))
    )
    # Later drifted resets, one of them naming the SAME account (the shape the
    # live store is in: two identified windows minutes apart, agreeing).
    trailing = (
        _window(accounts=(), reset=RESET + dt.timedelta(minutes=84)),
        _window(accounts=(ACCT_A,), reset=RESET + dt.timedelta(minutes=115)),
    )
    candidates = (
        _candidate(1, CROSSING),
        _candidate(2, NOMINAL_START + dt.timedelta(days=4)),
    )
    plan = _plan(neighbours + (identified,) + trailing, candidates)
    assert plan == {1: ACCT_A, 2: ACCT_A}
    # Non-vacuity: drop the one identified window and nothing is left to adopt
    # from, so the crowd on its own must stamp nothing.
    assert _plan(neighbours + (trailing[0],), candidates) == {}


def test_the_literal_unattributed_sentinel_is_adoptable():
    """``_codex_cache_account_predicate`` counts the literal in the unattributed
    bucket, so treating it as identified here would strand the row forever."""
    windows = (_window(accounts=(ACCT_A,)),)
    candidates = (_candidate(1, CROSSING, account_key=UNATTRIBUTED),)
    assert _plan(windows, candidates) == {1: ACCT_A}


def test_a_five_hour_window_is_out_of_scope():
    """5h windows nest inside the weekly one and add no evidence — they neither
    stamp nor block."""
    five_hour = _window(
        minutes=FIVE_HOUR, accounts=(ACCT_B,),
        reset=CROSSING + dt.timedelta(hours=1))
    assert _plan((five_hour,), (_candidate(1, CROSSING),)) == {}
    assert _plan(
        (five_hour, _window(accounts=(ACCT_A,))),
        (_candidate(1, CROSSING),),
    ) == {1: ACCT_A}


def test_a_model_scoped_weekly_pool_is_out_of_scope():
    """A separate model pool (Spark) is never account weekly quota (#373)."""
    spark = _window(accounts=(ACCT_B,), model_scoped=True)
    assert _plan((spark,), (_candidate(1, CROSSING),)) == {}
    assert _plan(
        (spark, _window(accounts=(ACCT_A,))),
        (_candidate(1, CROSSING),),
    ) == {1: ACCT_A}


def test_an_already_identified_row_is_never_restamped():
    windows = (_window(accounts=(ACCT_A,)),)
    candidates = (_candidate(1, CROSSING, account_key=ACCT_B),)
    assert _plan(windows, candidates) == {}


def test_another_roots_window_never_claims_this_roots_entry():
    windows = (_window(root=OTHER_ROOT, accounts=(ACCT_A,)),)
    assert _plan(windows, (_candidate(1, CROSSING),)) == {}


def test_the_plan_is_idempotent_over_its_own_output():
    windows = (_window(accounts=(ACCT_A,)),)
    first = adoption.build_spend_adoption_plan(
        windows, (_candidate(1, CROSSING),))
    assert first
    stamped = (_candidate(1, CROSSING, account_key=first[0].account_key),)
    assert adoption.build_spend_adoption_plan(windows, stamped) == ()


def test_the_plan_is_deterministically_ordered():
    windows = (_window(accounts=(ACCT_A,)),)
    candidates = tuple(
        _candidate(entry_id, CROSSING) for entry_id in (7, 2, 5, 1))
    plan = adoption.build_spend_adoption_plan(windows, candidates)
    assert [stamp.entry_id for stamp in plan] == [1, 2, 5, 7]


# --------------------------------------------------------------------------
# Glue over cache.db
# --------------------------------------------------------------------------

@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seed_snapshot(cache, *, root=ROOT, key=WEEK_KEY, window=WEEK,
                   captured, reset=RESET, account_key, source_path,
                   line_offset=1, limit_name=None, observed_model=None,
                   used_percent=1.0):
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc,"
        " observed_slot, logical_limit_key, limit_id, limit_name,"
        " window_minutes, used_percent, resets_at_utc, observed_model,"
        " account_key, canonical_resets_at_utc) "
        "VALUES ('codex',?,?,?,?,'primary',?,'codex',?,?,?,?,?,?,?)",
        (root, source_path, int(line_offset), _z(captured), key, limit_name,
         window, used_percent, _z(reset), observed_model, account_key,
         _z(reset)),
    )


def _seed_entry(cache, *, root=ROOT, timestamp, account_key, source_path,
                line_offset, total_tokens=1_000, session_id="s"):
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model,"
        " input_tokens, cached_input_tokens, output_tokens,"
        " reasoning_output_tokens, total_tokens, source_root_key,"
        " conversation_key, account_key) "
        "VALUES (?,?,?,?,?,?,0,0,0,?,?,NULL,?)",
        (source_path, int(line_offset), _z(timestamp), session_id, MODEL,
         int(total_tokens), int(total_tokens), root, account_key),
    )


def _accounts(cache):
    return {
        int(row[0]): row[1] for row in cache.execute(
            "SELECT id, account_key FROM codex_session_entries ORDER BY id")
    }


def _apply(ns, cache, **kwargs):
    import _cctally_cache
    stamped = _cctally_cache.apply_codex_window_spend_adoption(cache, **kwargs)
    cache.commit()
    return stamped


def test_the_glue_adopts_unattributed_entries_inside_a_named_window(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/quota.jsonl")
        _seed_entry(cache, timestamp=CROSSING - dt.timedelta(minutes=10),
                    account_key=None, source_path="/s/a.jsonl", line_offset=10)
        _seed_entry(cache, timestamp=CROSSING, account_key="",
                    source_path="/s/a.jsonl", line_offset=20)
        # Outside the nominal window entirely.
        _seed_entry(cache, timestamp=NOMINAL_START - dt.timedelta(hours=1),
                    account_key=None, source_path="/s/a.jsonl", line_offset=5)
        cache.commit()

        assert _apply(ns, cache) == 2
        assert _accounts(cache) == {1: ACCT_A, 2: ACCT_A, 3: None}
    finally:
        cache.close()


def test_the_glue_is_idempotent(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/quota.jsonl")
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        cache.commit()

        assert _apply(ns, cache) == 1
        before = _accounts(cache)
        assert _apply(ns, cache) == 0
        assert _accounts(cache) == before
    finally:
        cache.close()


def test_the_glue_leaves_an_ambiguous_window_alone(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/a.jsonl")
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_B,
                       source_path="/s/b.jsonl", line_offset=2)
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        cache.commit()

        assert _apply(ns, cache) == 0
        assert _accounts(cache) == {1: None}
    finally:
        cache.close()


def test_the_glue_ignores_a_spark_pool_window(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, key=SPARK_KEY, captured=BLOCK_START,
                       account_key=ACCT_A, source_path="/s/spark.jsonl",
                       limit_name="gpt-5.3-codex-spark weekly",
                       observed_model="gpt-5.3-codex-spark")
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        cache.commit()

        assert _apply(ns, cache) == 0
        assert _accounts(cache) == {1: None}
    finally:
        cache.close()


def test_the_glue_ignores_a_five_hour_window(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, key=FIVE_HOUR_KEY, window=FIVE_HOUR,
                       captured=BLOCK_START, reset=CROSSING + dt.timedelta(hours=1),
                       account_key=ACCT_A, source_path="/s/5h.jsonl")
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        cache.commit()

        assert _apply(ns, cache) == 0
        assert _accounts(cache) == {1: None}
    finally:
        cache.close()


def test_a_bounded_pass_only_visits_windows_touching_the_synced_entries(ns):
    """An ordinary hook-tick sync must not scan all history. The bound is the
    span of the entries (and window resets) this sync wrote."""
    cache = ns["open_cache_db"]()
    try:
        old_reset = RESET - dt.timedelta(days=21)
        _seed_snapshot(cache, captured=old_reset - dt.timedelta(days=6),
                       reset=old_reset, account_key=ACCT_A,
                       source_path="/s/old.jsonl")
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/new.jsonl")
        _seed_entry(cache, timestamp=old_reset - dt.timedelta(days=1),
                    account_key=None, source_path="/s/old.jsonl", line_offset=10)
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/new.jsonl", line_offset=10)
        cache.commit()

        touched = {ROOT: (CROSSING, CROSSING)}
        assert _apply(ns, cache, touched=touched) == 1
        assert _accounts(cache) == {1: None, 2: ACCT_A}
        # The unbounded pass then repairs the rest.
        assert _apply(ns, cache) == 1
        assert _accounts(cache) == {1: ACCT_A, 2: ACCT_A}
    finally:
        cache.close()


def test_an_empty_touched_map_is_a_no_op(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/quota.jsonl")
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        cache.commit()

        assert _apply(ns, cache, touched={}) == 0
        assert _accounts(cache) == {1: None}
    finally:
        cache.close()


class _RefusesSQL:
    """Stands in for a connection that must never be touched."""

    def __getattr__(self, name):
        raise AssertionError(
            f"an empty touched map must issue NO SQL, but {name!r} was used")


def test_an_empty_touched_map_issues_no_sql_at_all(ns):
    """The invariant three docstrings and docs/codex-gotchas.md assert. A schema
    PRAGMA before the early return quietly made it false."""
    import _cctally_cache

    assert _cctally_cache.apply_codex_window_spend_adoption(
        _RefusesSQL(), touched={}) == 0


def test_the_glue_adopts_a_row_carrying_the_literal_unattributed_sentinel(ns):
    cache = ns["open_cache_db"]()
    try:
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/quota.jsonl")
        _seed_entry(cache, timestamp=CROSSING, account_key=UNATTRIBUTED,
                    source_path="/s/a.jsonl", line_offset=10)
        cache.commit()

        assert _apply(ns, cache) == 1
        assert _accounts(cache) == {1: ACCT_A}
    finally:
        cache.close()


def test_the_glue_adopts_under_realistic_window_density(ns):
    """One identified window overlapped by several unidentified ones — the shape
    every real root is in, and the one a one- or two-window fixture hides."""
    cache = ns["open_cache_db"]()
    try:
        for index, delta in enumerate((dt.timedelta(hours=25), dt.timedelta(days=2),
                                       dt.timedelta(days=3))):
            _seed_snapshot(cache, captured=BLOCK_START - delta, reset=RESET - delta,
                           account_key=None, source_path=f"/s/old{index}.jsonl")
        _seed_snapshot(cache, captured=BLOCK_START + dt.timedelta(minutes=84),
                       reset=RESET + dt.timedelta(minutes=84), account_key=None,
                       source_path="/s/drift.jsonl")
        _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                       source_path="/s/named.jsonl")
        _seed_entry(cache, timestamp=CROSSING, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        _seed_entry(cache, timestamp=NOMINAL_START + dt.timedelta(days=4),
                    account_key=None, source_path="/s/a.jsonl", line_offset=20)
        cache.commit()

        assert _apply(ns, cache) == 2
        assert _accounts(cache) == {1: ACCT_A, 2: ACCT_A}
    finally:
        cache.close()


def test_a_bounded_pass_never_stamps_what_the_full_pass_would_refuse(ns):
    """The stamp is one-way, so an incremental sync and a later rebuild must not
    land on different attribution.

    A window whose reset sits just BELOW the touched span still claims rows in
    the span the scan offers, so it has to be loaded even though the sync did not
    touch it — otherwise the bounded pass sees only one of the two accounts
    claiming the row and stamps a conflict away.
    """
    cache = ns["open_cache_db"]()
    try:
        touched_at = CROSSING
        near, far = (touched_at + dt.timedelta(hours=1),
                     touched_at - dt.timedelta(hours=1))
        _seed_snapshot(cache, captured=touched_at, reset=near, account_key=ACCT_A,
                       source_path="/s/near.jsonl")
        _seed_snapshot(cache, captured=touched_at, reset=far, account_key=ACCT_B,
                       source_path="/s/far.jsonl")
        # Claimed by `near` only: one identified account, so it is adopted.
        _seed_entry(cache, timestamp=touched_at, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        # Claimed by BOTH, which name different accounts: genuinely ambiguous.
        _seed_entry(cache, timestamp=touched_at - dt.timedelta(hours=2),
                    account_key=None, source_path="/s/a.jsonl", line_offset=20)
        cache.commit()

        assert _apply(ns, cache, touched={ROOT: (touched_at, touched_at)}) == 1
        bounded = _accounts(cache)
        assert bounded == {1: ACCT_A, 2: None}
        # The full pass reaches exactly the same verdict, and adds nothing.
        assert _apply(ns, cache) == 0
        assert _accounts(cache) == bounded
    finally:
        cache.close()


def test_a_bounded_pass_never_scans_past_the_windows_it_loaded(ns):
    """The mirror of the case above, at the top edge.

    A candidate beyond the touched span can be claimed by a window whose reset is
    beyond the window bound too, so the scan stops at the touched span's own high
    water mark rather than at the union of the loaded window ranges.
    """
    cache = ns["open_cache_db"]()
    try:
        touched_at = CROSSING
        _seed_snapshot(cache, captured=touched_at,
                       reset=touched_at + dt.timedelta(days=5), account_key=ACCT_A,
                       source_path="/s/top.jsonl")
        _seed_snapshot(cache, captured=touched_at,
                       reset=touched_at + dt.timedelta(days=8), account_key=ACCT_B,
                       source_path="/s/hidden.jsonl")
        _seed_entry(cache, timestamp=touched_at, account_key=None,
                    source_path="/s/a.jsonl", line_offset=10)
        _seed_entry(cache, timestamp=touched_at + dt.timedelta(days=4),
                    account_key=None, source_path="/s/a.jsonl", line_offset=20)
        cache.commit()

        assert _apply(ns, cache, touched={ROOT: (touched_at, touched_at)}) == 1
        assert _accounts(cache) == {1: ACCT_A, 2: None}
        # And the full pass agrees: the second row is claimed by two windows naming
        # different accounts, so it stays nobody's.
        assert _apply(ns, cache) == 0
        assert _accounts(cache) == {1: ACCT_A, 2: None}
    finally:
        cache.close()


# --------------------------------------------------------------------------
# The reported defect, end to end through codex_quota_breakdown
# --------------------------------------------------------------------------

def _seed_milestone(stats, *, percent, captured, account_key,
                    source_path, line_offset):
    stats.execute(
        "INSERT INTO quota_percent_milestones "
        "(source, source_root_key, logical_limit_key, observed_slot,"
        " window_minutes, resets_at_utc, percent_threshold, captured_at_utc,"
        " source_path, line_offset, high_water_percent, generation,"
        " orphaned_at, account_key) "
        "VALUES ('codex',?,?,'primary',?,?,?,?,?,?,?,'gen-1',NULL,?)",
        # `_load_active_milestones` compares `resets_at_utc` as TEXT against
        # `_utc_iso(...)`, which is the `+00:00` spelling — not `Z`.
        (ROOT, WEEK_KEY, WEEK, RESET.isoformat(), int(percent), _z(captured),
         source_path, int(line_offset), int(percent), account_key),
    )
    stats.commit()


def test_the_ladder_segment_reports_the_spend_behind_its_own_crossing(ns):
    """The defect: percent 1 is stamped with the Pro account by the observation
    fold while the 119 entries behind it carry no per-file decision at all, so
    the strict cost read selects zero rows and the segment renders $0.00."""
    from _lib_quota import QuotaWindowIdentity
    import _cctally_quota as quota_mod

    stats = ns["open_db"]()
    try:
        cache = ns["open_cache_db"]()
        try:
            _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                           source_path="/s/quota.jsonl")
            for index in range(3):
                _seed_entry(
                    cache,
                    timestamp=BLOCK_START + dt.timedelta(minutes=10 * (index + 1)),
                    account_key=None, source_path="/s/rollout.jsonl",
                    line_offset=100 + index, total_tokens=250_000)
            cache.commit()
            _seed_milestone(stats, percent=1, captured=CROSSING, account_key=ACCT_A,
                            source_path="/s/rollout.jsonl", line_offset=900)

            identity = QuotaWindowIdentity(
                source="codex", source_root_key=ROOT, logical_limit_key=WEEK_KEY,
                observed_slot="primary", window_minutes=WEEK, account_key=ACCT_A,
            )

            before = quota_mod.codex_quota_breakdown(
                identity, RESET, speed="standard", cache_conn=cache, stats_conn=stats,
                account_key=ACCT_A)
            assert len(before) == 1, "precondition: the durable ladder has one crossing"
            assert before[0].cost_usd == 0.0, (
                "precondition: the segment's spend is invisible to its own account")

            assert _apply(ns, cache) == 3

            after = quota_mod.codex_quota_breakdown(
                identity, RESET, speed="standard", cache_conn=cache, stats_conn=stats,
                account_key=ACCT_A)
            assert len(after) == 1
            assert after[0].total_tokens == 750_000
            assert after[0].cost_usd > 0.0
            assert after[0].marginal_cost_usd == pytest.approx(after[0].cost_usd)

            # The dollars left the unattributed bucket rather than being duplicated.
            unattributed = quota_mod.codex_quota_breakdown(
                identity, RESET, speed="standard", cache_conn=cache, stats_conn=stats,
                account_key=UNATTRIBUTED)
            assert unattributed == ()
        finally:
            cache.close()
    finally:
        stats.close()


# --------------------------------------------------------------------------
# Wiring: the pass runs at the end of an ordinary Codex sync
# --------------------------------------------------------------------------

def test_sync_codex_cache_runs_the_adoption_pass(ns, monkeypatch, tmp_path):
    """The pass is wired into ``sync_codex_cache`` under the cache writer lock,
    after the walk commits, and consumes the persisted debt (#980 D3): each
    unit reads the candidate range ``[low - 7d, high]`` of the root's item."""
    import _cctally_cache

    calls: list[dict] = []
    real = _cctally_cache.apply_codex_window_spend_adoption_unit

    def _spy(conn, **kwargs):
        calls.append(kwargs)
        return real(conn, **kwargs)

    monkeypatch.setattr(
        _cctally_cache, "apply_codex_window_spend_adoption_unit", _spy)
    conn = ns["open_cache_db"]()
    try:
        _cctally_cache.add_codex_finalization_debt(
            conn, spans={ROOT: (CROSSING, CROSSING + dt.timedelta(hours=1))})
        conn.commit()
        _cctally_cache.sync_codex_cache(conn)
        assert calls, "sync_codex_cache never invoked the adoption pass"
        assert calls[0]["root_key"] == ROOT
        assert calls[0]["candidate_range"] == (
            CROSSING - dt.timedelta(days=7), CROSSING + dt.timedelta(hours=1))
        assert _cctally_cache.load_codex_finalization_debt(conn).empty
    finally:
        conn.close()


def test_post_walk_maintenance_failure_withholds_the_full_walk_sentinel(
    ns, monkeypatch,
):
    """A best-effort rederivation failure is retryable, never certifiable,
    and its work stays owed (#980: a failed adoption is revisited)."""
    import _cctally_cache

    def _fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic adoption failure")

    monkeypatch.setattr(
        _cctally_cache, "apply_codex_window_spend_adoption_unit", _fail)
    conn = ns["open_cache_db"]()
    try:
        _cctally_cache.add_codex_finalization_debt(
            conn, spans={ROOT: (CROSSING, CROSSING)})
        conn.commit()
        stats = _cctally_cache.sync_codex_cache(conn)
        assert stats.maintenance_failed is True
        assert stats.full_walk_complete is False
        assert conn.execute(
            "SELECT COUNT(*) FROM cache_meta "
            "WHERE key='dashboard_codex_full_walk_complete'"
        ).fetchone() == (0,)
        debt = _cctally_cache.load_codex_finalization_debt(conn)
        assert [item.span for item in debt.items[ROOT]] == [(CROSSING, CROSSING)]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# #980 Part D: durable, cancellable finalization (spec §4.4 D3-D5, T-D1)
# --------------------------------------------------------------------------

#: The unit's candidate read; its start flips the trace-driven clock.
_UNIT_READ = "ORDER BY timestamp_utc, id LIMIT"


def _seed_window_with_entries(cache, count, *, start=CROSSING, offset=0):
    """One identified weekly window and ``count`` unattributed entries inside
    its nominal range, one second apart."""
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                   source_path="/s/quota.jsonl", line_offset=offset + 1)
    cache.executemany(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model,"
        " input_tokens, cached_input_tokens, output_tokens,"
        " reasoning_output_tokens, total_tokens, source_root_key,"
        " conversation_key, account_key) "
        "VALUES ('/s/a.jsonl',?,?,'s',?,1,0,0,0,1,?,NULL,NULL)",
        [(offset + 10 + index, _z(start + dt.timedelta(seconds=index)),
          MODEL, ROOT) for index in range(count)])


def _span(count, *, start=CROSSING):
    return (start, start + dt.timedelta(seconds=max(0, count - 1)))


class _TraceClock:
    """Reads 0.0 until a statement containing ``marker`` starts (or ``flip``
    is called), then 100.0: a deadline that passes while that read runs."""

    def __init__(self, cache, marker=None):
        self.late = False
        self.statements: list[str] = []

        def trace(sql):
            self.statements.append(sql)
            if marker is not None and marker in sql:
                self.late = True
        cache.set_trace_callback(trace)

    def flip(self):
        self.late = True

    def __call__(self):
        return 100.0 if self.late else 0.0


def _items(cc, cache):
    debt = cc.load_codex_finalization_debt(cache)
    return {root: [(item.span, item.cursor) for item in entries]
            for root, entries in debt.items.items()}


def _control_accounts(ns, cache):
    """The unbudgeted control on the same final data: every row unattributed
    again, then the whole-history pass."""
    import _cctally_cache as cc
    cache.execute("UPDATE codex_session_entries SET account_key = NULL")
    cc.apply_codex_window_spend_adoption(cache)
    cache.commit()
    return _accounts(cache)


def test_finalization_unit_read_interrupted_rolls_back(ns):
    """A real SQLite progress-handler interrupt of a unit's read is a
    cancellation: the unit's savepoint rolls back (debt and cursor unchanged),
    the enclosing transaction survives with the earlier steps' writes, and the
    outcome is never a success-shaped zero."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 2500)
        cc.add_codex_finalization_debt(cache, spans={ROOT: _span(2500)})
        cache.commit()
        before = _items(cc, cache)
        cache.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                      "VALUES ('t_earlier_step', '1')")
        clock = _TraceClock(cache, _UNIT_READ)
        outcome = cc.settle_codex_finalization(cache, deadline=10.0, clock=clock)
        cache.set_trace_callback(None)
        assert clock.late, "non-vacuity: the unit read started"
        assert outcome.cancelled and outcome.cancel_cause == "interrupt"
        assert outcome.units_done == 0 and outcome.adopted == 0
        assert cache.in_transaction, "an interrupted read ended the transaction"
        assert cache.execute("SELECT value FROM cache_meta "
                             "WHERE key='t_earlier_step'").fetchone() == ("1",)
        assert _items(cc, cache) == before
        assert cc.load_codex_finalization_debt(cache).strikes == 1
        assert set(_accounts(cache).values()) == {None}
        cache.commit()
    finally:
        cache.close()


def test_finalization_near_deadline_read_then_expensive_plan_stops_before_writes(
    ns, monkeypatch,
):
    """A read that finishes just before the threshold, followed by planning
    that crosses it, stops before the first write batch."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 3)
        cc.add_codex_finalization_debt(cache, spans={ROOT: _span(3)})
        cache.commit()
        before = _items(cc, cache)
        clock = _TraceClock(cache)
        real_plan = cc._lib_codex_account_adoption.build_spend_adoption_plan

        def expensive_plan(*args, **kwargs):
            plan = real_plan(*args, **kwargs)
            clock.flip()
            return plan

        monkeypatch.setattr(cc._lib_codex_account_adoption,
                            "build_spend_adoption_plan", expensive_plan)
        outcome = cc.settle_codex_finalization(cache, deadline=10.0, clock=clock)
        cache.set_trace_callback(None)
        assert clock.late, "non-vacuity: planning ran"
        assert outcome.cancelled and outcome.cancel_cause == "deadline"
        assert not [sql for sql in clock.statements
                    if sql.lstrip().upper().startswith(
                        "UPDATE CODEX_SESSION_ENTRIES")]
        assert _items(cc, cache) == before
        assert set(_accounts(cache).values()) == {None}
        cache.commit()
    finally:
        cache.close()


@pytest.mark.parametrize("helper", [
    "apply_codex_window_spend_adoption",
    "reconcile_codex_window_attribution_spend",
])
def test_adoption_cancellation_not_swallowed_as_zero(ns, helper):
    """A SQLite interrupt inside the non-strict best-effort helpers raises
    ``CodexFinalizationCancelled``; it used to be swallowed as a zero."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 3)
        cache.commit()
        cache.set_progress_handler(lambda: 1, 1)
        try:
            with pytest.raises(Exception) as info:
                getattr(cc, helper)(cache)
        finally:
            cache.set_progress_handler(None, 0)
        assert type(info.value).__name__ == "CodexFinalizationCancelled"
        cache.rollback()
        assert set(_accounts(cache).values()) == {None}
    finally:
        cache.close()


def test_restart_resumes_persisted_debt_and_cursor(ns, monkeypatch):
    """A committed unit persists its cursor; a new connection (a restarted
    process) resumes from it, budgeted or not, and the result equals the
    unbudgeted control."""
    import _cctally_cache as cc
    monkeypatch.setattr(cc, "_CODEX_ADOPTION_UNIT_CAP", 2)
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 5)
        cc.add_codex_finalization_debt(cache, spans={ROOT: _span(5)})
        cache.commit()
        cc._codex_settle_finalization(cache, deadline=None, max_units=1)
        cache.commit()
    finally:
        cache.close()
    cache = ns["open_cache_db"]()
    try:
        assert _items(cc, cache) == {ROOT: [(
            _span(5), (_z(CROSSING + dt.timedelta(seconds=1)), 2))]}
        assert list(_accounts(cache).values()).count(ACCT_A) == 2
        outcome = cc.settle_codex_finalization(
            cache, deadline=time_monotonic() + 5.0)
        cache.commit()
        assert outcome.units_done >= 1 and not outcome.cancelled
        outcome = cc.settle_codex_finalization(cache, deadline=None)
        cache.commit()
        assert cc.load_codex_finalization_debt(cache).empty
        settled = _accounts(cache)
        assert set(settled.values()) == {ACCT_A}
        assert _control_accounts(ns, cache) == settled
    finally:
        cache.close()


def time_monotonic() -> float:
    import time
    return time.monotonic()


def test_full_debt_expands_and_drains(ns):
    """An unexpanded ``full`` is model-pending; its expansion is one unit that
    replaces it by one item per root over the root's whole range (setting
    ``model`` and ``attribution``), and the items drain to the control."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 4)
        _seed_entry(cache, timestamp=NOMINAL_START - dt.timedelta(days=3),
                    account_key=None, source_path="/s/old.jsonl",
                    line_offset=1)
        cc.add_codex_finalization_debt(cache, full=True)
        cache.commit()
        assert cc.codex_model_resolution_pending(cache)
        outcome = cc._codex_settle_finalization(cache, deadline=None,
                                                max_units=1)
        cache.commit()
        assert outcome.units_done == 1
        debt = cc.load_codex_finalization_debt(cache)
        assert not debt.full and debt.model and debt.attribution
        assert list(debt.items) == [ROOT] and len(debt.items[ROOT]) == 1
        low, high = debt.items[ROOT][0].span
        assert low <= NOMINAL_START - dt.timedelta(days=3) and high >= RESET
        assert cc.codex_model_resolution_pending(cache)
        cc.settle_codex_finalization(cache, deadline=None)
        cache.commit()
        assert cc.load_codex_finalization_debt(cache).empty
        assert not cc.codex_model_resolution_pending(cache)
        settled = _accounts(cache)
        assert list(settled.values()).count(ACCT_A) == 4
        assert _control_accounts(ns, cache) == settled
    finally:
        cache.close()


def test_work_behind_committed_cursor_is_reprocessed(ns, monkeypatch):
    """Quota evidence and an earlier accounting row added behind a started
    item's committed cursor become a new item, so the final attribution
    equals the unbudgeted control."""
    import _cctally_cache as cc
    monkeypatch.setattr(cc, "_CODEX_ADOPTION_UNIT_CAP", 2)
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 4)
        cc.add_codex_finalization_debt(cache, spans={ROOT: _span(4)})
        cache.commit()
        cc._codex_settle_finalization(cache, deadline=None, max_units=1)
        cache.commit()
        assert _items(cc, cache)[ROOT][0][1] is not None, "a started item"
        # Behind the cursor: an earlier row in the identified window, and an
        # older window whose evidence (another account) arrives only now.
        earlier = CROSSING - dt.timedelta(hours=1)
        older_reset = RESET - dt.timedelta(days=7)
        older_row = NOMINAL_START - dt.timedelta(days=2)
        _seed_entry(cache, timestamp=earlier, account_key=None,
                    source_path="/s/b.jsonl", line_offset=1)
        _seed_entry(cache, timestamp=older_row, account_key=None,
                    source_path="/s/b.jsonl", line_offset=2)
        _seed_snapshot(cache, captured=older_row, reset=older_reset,
                       account_key=ACCT_B, source_path="/s/quota-old.jsonl")
        cc.add_codex_finalization_debt(
            cache, spans={ROOT: (older_row, earlier)})
        cc.add_codex_finalization_debt(
            cache, spans={ROOT: (older_reset, older_reset)})
        cache.commit()
        items = _items(cc, cache)[ROOT]
        assert len(items) == 2 and items[1][1] is None
        cc.settle_codex_finalization(cache, deadline=None)
        cache.commit()
        assert cc.load_codex_finalization_debt(cache).empty
        settled = _accounts(cache)
        assert ACCT_B in settled.values(), "non-vacuity: the older window"
        assert list(settled.values()).count(None) == 0
        assert _control_accounts(ns, cache) == settled
    finally:
        cache.close()


def test_oversized_unit_strikes_hand_off_to_worker(ns):
    """A unit cancelled on two consecutive budgeted calls is not attempted
    again by a budgeted call; the maintenance executor settles it."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_window_with_entries(cache, 2500)
        cc.add_codex_finalization_debt(cache, spans={ROOT: _span(2500)})
        cache.commit()
        for strike in (1, 2):
            clock = _TraceClock(cache, _UNIT_READ)
            outcome = cc.settle_codex_finalization(
                cache, deadline=10.0, clock=clock)
            cache.commit()
            assert outcome.cancelled, strike
            assert cc.load_codex_finalization_debt(cache).strikes == strike
        clock = _TraceClock(cache, _UNIT_READ)
        outcome = cc.settle_codex_finalization(cache, deadline=10.0, clock=clock)
        cache.commit()
        cache.set_trace_callback(None)
        assert outcome.deferred_to_worker and outcome.units_done == 0
        assert not clock.late, "a struck unit was attempted again"
    finally:
        cache.close()
    result = cc.run_codex_prune_drain(wait_for_requests=False)
    assert result.finalization_units >= 1
    cache = ns["open_cache_db"]()
    try:
        assert cc.load_codex_finalization_debt(cache).empty
        assert set(_accounts(cache).values()) == {ACCT_A}
    finally:
        cache.close()


def test_applier_produces_debt(ns):
    """The journal ``_cache_applier`` records finalization work in its own
    transaction: a span per root for changed quota rows (plus ``model`` while
    a row is unresolved), and ``attribution`` ONLY for applied file-account
    and window-attribution records.

    Detects: an attribution-only apply arming ``model`` while a permanently
    unresolvable NULL ``observed_model`` row exists (the settled production
    copy has six), which fenced the quota projection after every
    ``cctally account attribute`` -- resolution never reads the account."""
    import _cctally_cache as cc
    import _cctally_journal as jr
    import _lib_journal as jl
    ns["open_cache_db"]().close()
    observation = jl.make_obs(
        at="2026-07-28T10:00:00Z", src="codex-quota", provider="codex",
        account=None,
        payload={
            "kind": "quota_window_snapshot", "source": "codex",
            "source_root_key": "root-a", "source_path": "/codex/root-a/r.jsonl",
            "line_offset": 10, "captured_at_utc": "2026-07-28T10:00:00Z",
            "observed_slot": "primary",
            "logical_limit_key": '{"limitId":"native-primary"}',
            "limit_id": "native-primary", "limit_name": "Primary",
            "window_minutes": 300, "used_percent": 42.0,
            "resets_at_utc": "2026-07-28T15:00:00+00:00", "plan_type": "pro",
            "individual_limit_json": None, "reached_type": None,
            "observed_model": None,
        })
    assert jr._cache_applier([(observation, "seg", 0)]) is None
    cache = ns["open_cache_db"]()
    try:
        debt = cc.load_codex_finalization_debt(cache)
        assert debt.model and not debt.attribution
        (low, high), = [item.span for item in debt.items["root-a"]]
        assert low <= dt.datetime(2026, 7, 28, 15, tzinfo=UTC) <= high
        assert cc.codex_model_resolution_pending(cache)
        cache.execute("DELETE FROM cache_meta WHERE key=?",
                      (cc.CODEX_FINALIZATION_DEBT_KEY,))
        cache.commit()
    finally:
        cache.close()
    account_op = jl.make_codex_file_account(
        at="2026-07-28T10:00:01Z", root_scope="root-a", file_identity="fid-1",
        incarnation=1, from_offset=0, account_key=ACCT_A)
    assert jr._cache_applier([(account_op, "seg", 100)]) is None
    cache = ns["open_cache_db"]()
    try:
        assert cc._codex_unresolved_quota_model_exists(cache), (
            "non-vacuity: a NULL observed_model row is present")
        debt = cc.load_codex_finalization_debt(cache)
        assert debt.attribution and not debt.model
        assert not cc.codex_model_resolution_pending(cache)
        cache.execute("DELETE FROM cache_meta WHERE key=?",
                      (cc.CODEX_FINALIZATION_DEBT_KEY,))
        cache.commit()
        asserted, _retracted, _skipped = jr._apply_window_attribution_records(
            cache, [jl.make_codex_window_attribution(
                at="2026-07-28T10:00:02Z", account_key=ACCT_A,
                source_root_key="root-a",
                logical_limit_key="limit-weekly", observed_slot="primary",
                window_minutes=10080,
                raw_resets_at_utc=["2026-07-20T09:45:35Z"],
                canonical_resets_at_utc="2026-07-20T09:40:00Z")])
        cache.commit()
        assert asserted == 1
        debt = cc.load_codex_finalization_debt(cache)
        assert debt.attribution and not debt.model
        assert not cc.codex_model_resolution_pending(cache)
    finally:
        cache.close()
    import _cctally_quota as quota
    result = quota.reconcile_codex_quota_projection()
    assert result.status != quota.PROJECTION_DEFERRED_MODEL, (
        "an attribution-only apply fenced the quota projection")


# --------------------------------------------------------------------------
# #980 F1: model resolution is a prerequisite of every dependent unit
# --------------------------------------------------------------------------

SPARK_MODEL = "gpt-5.3-codex-spark"
_SPARK_PATH = "/s/spark.jsonl"


def _seed_resolvable_spark_window(cache):
    """A weekly window that IS the Spark pool but was materialized before its
    model was stamped: a NULL ``observed_model``, an unlabelled raw key, and
    the rollout's own accounting row that model resolution reads (same
    ``source_path``, earlier ``line_offset``). Until resolution runs, the
    window reads as account-level weekly quota naming ``ACCT_A``."""
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model,"
        " input_tokens, cached_input_tokens, output_tokens,"
        " reasoning_output_tokens, total_tokens, source_root_key,"
        " conversation_key, account_key) "
        "VALUES (?,1,?,'s',?,1,0,0,0,1,?,NULL,NULL)",
        (_SPARK_PATH, _z(BLOCK_START), SPARK_MODEL, ROOT))
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                   source_path=_SPARK_PATH, line_offset=5,
                   observed_model=None)
    _seed_entry(cache, timestamp=CROSSING, account_key=None,
                source_path="/s/a.jsonl", line_offset=10)


def _spark_model(cache):
    return cache.execute(
        "SELECT observed_model FROM quota_window_snapshots "
        "WHERE source_path=?", (_SPARK_PATH,)).fetchone()[0]


def _spy_dependent_units(cc, monkeypatch):
    """Record the Spark row's ``observed_model`` whenever an adoption unit or
    a reconciliation pass starts."""
    seen: list = []
    for name in ("apply_codex_window_spend_adoption_unit",
                 "reconcile_codex_window_attribution_spend"):
        real = getattr(cc, name)

        def spy(conn, *args, _real=real, **kwargs):
            seen.append(_spark_model(conn))
            return _real(conn, *args, **kwargs)

        monkeypatch.setattr(cc, name, spy)
    return seen


def _debt_text(cc, cache):
    return cc._encode_codex_finalization_debt(
        cc.load_codex_finalization_debt(cache))


def test_prewalk_settlement_never_adopts_before_model_resolution(
    ns, monkeypatch,
):
    """Detects: a budgeted sync's pre-walk settlement (stages expand + adopt)
    ran adoption over inherited debt while model resolution was still owed, so
    a resolvable NULL Spark window was filed as account-level quota and
    stamped unrelated unattributed spend -- a stamp later resolution never
    undoes (adoption skips attributed rows)."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_resolvable_spark_window(cache)
        cache.commit()
        assert cc.apply_codex_window_spend_adoption(cache) >= 1, (
            "non-vacuity: the unresolved Spark window adopts as account quota")
        cache.rollback()
        cc.add_codex_finalization_debt(
            cache, spans={ROOT: (CROSSING, CROSSING)}, model=True)
        cache.commit()
        seen = _spy_dependent_units(cc, monkeypatch)
        cc.sync_codex_cache(cache, budget_seconds=5.0)
        assert None not in seen, "a dependent unit ran over an unresolved model"
        cc.sync_codex_cache(cache)
        assert seen and None not in seen
        assert _spark_model(cache) == SPARK_MODEL
        assert cc.load_codex_finalization_debt(cache).empty
        settled = _accounts(cache)
        assert set(settled.values()) == {None}, "the Spark window adopted"
        assert _control_accounts(ns, cache) == settled
    finally:
        cache.close()


@pytest.mark.parametrize("path", ["settle_unbudgeted", "budgeted_sync",
                                  "executor_unit"])
def test_failed_model_resolution_holds_dependent_units(ns, monkeypatch, path):
    """Detects: model resolution failing inside a settlement while the same
    settlement still ran adoption and reconciliation, over a window whose
    pool classification depends on the unresolved model. The dependent units
    must not run; their items, cursors and ``attribution`` stay exactly as
    they were, and a later successful settlement reaches the unbudgeted
    control with the Spark window demoted."""
    import _cctally_cache as cc
    cache = ns["open_cache_db"]()
    try:
        _seed_resolvable_spark_window(cache)
        cc.add_codex_finalization_debt(
            cache, spans={ROOT: (CROSSING, CROSSING)}, model=True,
            attribution=True)
        cache.commit()
        before = _debt_text(cc, cache)
        seen = _spy_dependent_units(cc, monkeypatch)

        def failing_resolution(_conn):
            raise sqlite3.OperationalError("injected model resolution failure")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(cc._cctally_db_sib,
                          "backfill_codex_quota_observed_model",
                          failing_resolution)
            if path == "settle_unbudgeted":
                outcome = cc.settle_codex_finalization(cache, deadline=None)
                cache.commit()
                assert outcome.model_failed and not outcome.completed
            elif path == "budgeted_sync":
                stats = cc.sync_codex_cache(cache, budget_seconds=5.0)
                assert stats.maintenance_failed
                assert not stats.full_walk_complete
            else:
                succeeded, cleared = cc._run_codex_executor_unit(
                    cache, "finalization")
                assert not succeeded and not cleared
        assert seen == [], "a dependent unit ran after resolution failed"
        assert set(_accounts(cache).values()) == {None}
        assert _debt_text(cc, cache) == before, "the dependent debt changed"
        assert _spark_model(cache) is None
        cc.settle_codex_finalization(cache, deadline=None)
        cache.commit()
        assert seen and None not in seen
        assert cc.load_codex_finalization_debt(cache).empty
        settled = _accounts(cache)
        assert set(settled.values()) == {None}, "the Spark window adopted"
        assert _control_accounts(ns, cache) == settled
    finally:
        cache.close()
