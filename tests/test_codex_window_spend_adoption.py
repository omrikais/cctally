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


def test_the_glue_is_idempotent(ns):
    cache = ns["open_cache_db"]()
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                   source_path="/s/quota.jsonl")
    _seed_entry(cache, timestamp=CROSSING, account_key=None,
                source_path="/s/a.jsonl", line_offset=10)
    cache.commit()

    assert _apply(ns, cache) == 1
    before = _accounts(cache)
    assert _apply(ns, cache) == 0
    assert _accounts(cache) == before


def test_the_glue_leaves_an_ambiguous_window_alone(ns):
    cache = ns["open_cache_db"]()
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                   source_path="/s/a.jsonl")
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_B,
                   source_path="/s/b.jsonl", line_offset=2)
    _seed_entry(cache, timestamp=CROSSING, account_key=None,
                source_path="/s/a.jsonl", line_offset=10)
    cache.commit()

    assert _apply(ns, cache) == 0
    assert _accounts(cache) == {1: None}


def test_the_glue_ignores_a_spark_pool_window(ns):
    cache = ns["open_cache_db"]()
    _seed_snapshot(cache, key=SPARK_KEY, captured=BLOCK_START,
                   account_key=ACCT_A, source_path="/s/spark.jsonl",
                   limit_name="gpt-5.3-codex-spark weekly",
                   observed_model="gpt-5.3-codex-spark")
    _seed_entry(cache, timestamp=CROSSING, account_key=None,
                source_path="/s/a.jsonl", line_offset=10)
    cache.commit()

    assert _apply(ns, cache) == 0
    assert _accounts(cache) == {1: None}


def test_the_glue_ignores_a_five_hour_window(ns):
    cache = ns["open_cache_db"]()
    _seed_snapshot(cache, key=FIVE_HOUR_KEY, window=FIVE_HOUR,
                   captured=BLOCK_START, reset=CROSSING + dt.timedelta(hours=1),
                   account_key=ACCT_A, source_path="/s/5h.jsonl")
    _seed_entry(cache, timestamp=CROSSING, account_key=None,
                source_path="/s/a.jsonl", line_offset=10)
    cache.commit()

    assert _apply(ns, cache) == 0
    assert _accounts(cache) == {1: None}


def test_a_bounded_pass_only_visits_windows_touching_the_synced_entries(ns):
    """An ordinary hook-tick sync must not scan all history. The bound is the
    span of the entries (and window resets) this sync wrote."""
    cache = ns["open_cache_db"]()
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


def test_an_empty_touched_map_is_a_no_op(ns):
    cache = ns["open_cache_db"]()
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                   source_path="/s/quota.jsonl")
    _seed_entry(cache, timestamp=CROSSING, account_key=None,
                source_path="/s/a.jsonl", line_offset=10)
    cache.commit()

    assert _apply(ns, cache, touched={}) == 0
    assert _accounts(cache) == {1: None}


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
    _seed_snapshot(cache, captured=BLOCK_START, account_key=ACCT_A,
                   source_path="/s/quota.jsonl")
    _seed_entry(cache, timestamp=CROSSING, account_key=UNATTRIBUTED,
                source_path="/s/a.jsonl", line_offset=10)
    cache.commit()

    assert _apply(ns, cache) == 1
    assert _accounts(cache) == {1: ACCT_A}


def test_the_glue_adopts_under_realistic_window_density(ns):
    """One identified window overlapped by several unidentified ones — the shape
    every real root is in, and the one a one- or two-window fixture hides."""
    cache = ns["open_cache_db"]()
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


def test_a_bounded_pass_never_stamps_what_the_full_pass_would_refuse(ns):
    """The stamp is one-way, so an incremental sync and a later rebuild must not
    land on different attribution.

    A window whose reset sits just BELOW the touched span still claims rows in
    the span the scan offers, so it has to be loaded even though the sync did not
    touch it — otherwise the bounded pass sees only one of the two accounts
    claiming the row and stamps a conflict away.
    """
    cache = ns["open_cache_db"]()
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


def test_a_bounded_pass_never_scans_past_the_windows_it_loaded(ns):
    """The mirror of the case above, at the top edge.

    A candidate beyond the touched span can be claimed by a window whose reset is
    beyond the window bound too, so the scan stops at the touched span's own high
    water mark rather than at the union of the loaded window ranges.
    """
    cache = ns["open_cache_db"]()
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

    stats, cache = ns["open_db"](), ns["open_cache_db"]()
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


# --------------------------------------------------------------------------
# Wiring: the pass runs at the end of an ordinary Codex sync
# --------------------------------------------------------------------------

def test_sync_codex_cache_runs_the_adoption_pass(ns, monkeypatch, tmp_path):
    """The pass is wired into ``sync_codex_cache`` under the cache writer lock,
    after the walk commits."""
    import _cctally_cache

    calls: list[dict] = []
    real = _cctally_cache.apply_codex_window_spend_adoption

    def _spy(conn, **kwargs):
        calls.append(kwargs)
        return real(conn, **kwargs)

    monkeypatch.setattr(
        _cctally_cache, "apply_codex_window_spend_adoption", _spy)
    conn = ns["open_cache_db"]()
    _cctally_cache.sync_codex_cache(conn)
    assert calls, "sync_codex_cache never invoked the adoption pass"
    assert "touched" in calls[0]
