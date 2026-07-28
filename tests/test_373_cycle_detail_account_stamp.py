"""#373 root cause 3 — the #341 account stamp must reach cycle detail.

`_union_cluster_milestones` reconstructs a `QuotaWindowIdentity` from
`_CodexCycle` fields WITHOUT `account_key`, so it takes the dataclass default,
the `unattributed` sentinel. `_load_active_milestones` filters
`AND account_key=?` and the retained rows carry the real key, so the lookup
returns nothing, `codex_quota_breakdown` takes its `if not milestones: return
()` early exit, and every Codex cycle renders 0 milestones and 0 blocks while
the index correctly reports its counts.

WHY THE EXISTING SUITE IS BLIND TO THIS: the seed helpers in
`tests/test_milestone_history.py` never write `account_key`, so both the block
and the milestone take the column default and MATCH. The fixtures below seed a
REAL account key on both sides, or the test would pass for the wrong reason.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

ROOT = "0123456789abcdef0123456789abcdef"
ACCOUNT = "fedcba9876543210fedcba9876543210"
UNATTRIBUTED = "unattributed"
LIMIT_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
             f'"sourceRootKey":"{ROOT}","windowMinutes":10080}}')
FIVE_HOUR_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
                 f'"sourceRootKey":"{ROOT}","windowMinutes":300}}')

CYCLE_START = "2026-07-21T17:02:32+00:00"
CYCLE_RESET = "2026-07-28T17:02:32+00:00"
BLOCK_START = "2026-07-22T10:00:00+00:00"
BLOCK_RESET = "2026-07-22T15:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_block(conn, *, key, window, start, reset, account_key, pct=28.0):
    conn.execute(
        "INSERT INTO quota_window_blocks "
        "(source, source_root_key, logical_limit_key, observed_slot, window_minutes,"
        " limit_id, limit_name, resets_at_utc, nominal_start_at_utc,"
        " first_observed_at_utc, last_observed_at_utc, first_percent, current_percent,"
        " last_source_path, last_line_offset, generation, orphaned_at, account_key) "
        "VALUES ('codex',?,?,'primary',?,'codex',NULL,?,?,?,?,0.0,?,"
        "'/tmp/r.jsonl',0,'gen-1',NULL,?)",
        (ROOT, key, window, reset, start, start, reset, pct, account_key),
    )


def _seed_milestones(conn, *, key, window, reset, thresholds, account_key,
                     captured_base):
    for threshold in thresholds:
        captured = captured_base + dt.timedelta(minutes=int(threshold))
        conn.execute(
            "INSERT INTO quota_percent_milestones "
            "(source, source_root_key, logical_limit_key, observed_slot,"
            " window_minutes, resets_at_utc, percent_threshold, captured_at_utc,"
            " source_path, line_offset, high_water_percent, generation,"
            " orphaned_at, account_key) "
            "VALUES ('codex',?,?,'primary',?,?,?,?,'/tmp/r.jsonl',?,?,'gen-1',NULL,?)",
            (
                ROOT, key, window, reset, int(threshold),
                captured.isoformat().replace("+00:00", "Z"),
                int(threshold), int(threshold), account_key,
            ),
        )


def _seed_snapshot(cache_conn, *, key, window, start, reset):
    """The physical evidence `_first_block_physical_tuple` resolves the cycle's
    accounting start from. Account-blind by design — it is physical evidence,
    not a projection."""
    cache_conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc,"
        " observed_slot, logical_limit_key, limit_id, limit_name, window_minutes,"
        " used_percent, resets_at_utc) "
        "VALUES ('codex',?,'/tmp/r.jsonl',0,?,'primary',?,'codex',NULL,?,0.0,?)",
        (ROOT, start.replace("+00:00", "Z"), key, window, reset),
    )


def _seed_account_stamped_cycle(conn, cache_conn, *, account_key, thresholds):
    """One weekly cycle and one 5h block inside it, BOTH account-stamped, with
    their milestones and the physical snapshots the breakdown needs."""
    base = dt.datetime(2026, 7, 21, 18, 0, tzinfo=dt.timezone.utc)
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=CYCLE_RESET, account_key=account_key)
    _seed_milestones(conn, key=LIMIT_KEY, window=10080, reset=CYCLE_RESET,
                     thresholds=thresholds, account_key=account_key,
                     captured_base=base)
    _seed_snapshot(cache_conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                   reset=CYCLE_RESET)

    _seed_block(conn, key=FIVE_HOUR_KEY, window=300, start=BLOCK_START,
                reset=BLOCK_RESET, account_key=account_key, pct=40.0)
    _seed_milestones(conn, key=FIVE_HOUR_KEY, window=300, reset=BLOCK_RESET,
                     thresholds=range(1, 4), account_key=account_key,
                     captured_base=dt.datetime(2026, 7, 22, 11, 0,
                                               tzinfo=dt.timezone.utc))
    _seed_snapshot(cache_conn, key=FIVE_HOUR_KEY, window=300, start=BLOCK_START,
                   reset=BLOCK_RESET)


class _Boundary:
    source_root_keys = (ROOT,)
    quota_identity = None
    resets_at = dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc)


NOW = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)


def _index_entry(ns, conn):
    import _cctally_milestone_history as mh
    index = mh.build_codex_cycle_index(conn, identity=_Boundary(), now_utc=NOW)
    return next(e for e in index if e["start_at_utc"] == "2026-07-21T17:02:32Z")


def _build_detail(ns, conn, cache_conn):
    import _cctally_milestone_history as mh
    entry = _index_entry(ns, conn)
    detail = mh.build_codex_cycle_detail(
        conn, cache_conn, identity=_Boundary(), key=entry["key"],
        speed="standard", now_utc=NOW,
    )
    assert not isinstance(detail, tuple), detail
    return detail


def test_cycle_detail_returns_milestones_for_an_account_stamped_cycle(ns):
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    _seed_account_stamped_cycle(conn, cache_conn, account_key=ACCOUNT,
                                thresholds=range(1, 29))
    conn.commit()
    cache_conn.commit()
    # Positive preconditions: rows exist AND carry a non-default account key.
    stored = conn.execute(
        "SELECT COUNT(DISTINCT percent_threshold) FROM quota_percent_milestones "
        "WHERE account_key=? AND window_minutes=10080", (ACCOUNT,)).fetchone()[0]
    assert stored == 28
    assert ACCOUNT != UNATTRIBUTED
    assert conn.execute(
        "SELECT COUNT(*) FROM quota_percent_milestones WHERE account_key=?",
        (UNATTRIBUTED,)).fetchone()[0] == 0

    detail = _build_detail(ns, conn, cache_conn)
    rendered = sum(len(s["milestones"]) for s in detail["segments"])
    assert rendered == 28
    # Blocks go through the same reconstructed identity, so they were empty too.
    assert len(detail["blocks"]) == 1
    assert len(detail["blocks"][0]["milestones"]) == 3


def test_index_count_and_detail_count_agree(ns):
    """The invariant whose violation IS the bug: the index reads
    quota_percent_milestones directly while the detail goes through
    codex_quota_breakdown, and the two disagreed silently."""
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    _seed_account_stamped_cycle(conn, cache_conn, account_key=ACCOUNT,
                                thresholds=range(1, 29))
    conn.commit()
    cache_conn.commit()
    entry = _index_entry(ns, conn)
    detail = _build_detail(ns, conn, cache_conn)
    assert entry["milestone_count"] == sum(
        len(s["milestones"]) for s in detail["segments"]
    )


def test_index_counts_stay_account_scoped_and_do_not_collapse_to_zero(ns):
    """Step 5's other direction. `_codex_milestone_count` and
    `_codex_five_hour_rows` query the same account-stamped tables, so adding
    `account_key` to their WHERE clauses must not silently zero them out."""
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    _seed_account_stamped_cycle(conn, cache_conn, account_key=ACCOUNT,
                                thresholds=range(1, 29))
    conn.commit()
    cache_conn.commit()
    entry = _index_entry(ns, conn)
    assert entry["milestone_count"] == 28
    assert entry["block_count"] == 1


def test_five_hour_blocks_survive_when_only_the_weekly_window_is_attributed(ns):
    """`_codex_five_hour_rows` spans identities, so it cannot filter on strict
    account equality.

    `_codex_milestone_count` is safe because its block and milestone parameters
    both come from ONE `block.identity` and therefore cannot disagree. This
    query is different: it selects 5h blocks by ROOT + TIME RANGE across
    identities. The 300-minute windows are a separate physical-window group
    from the weekly one and `adopt_unidentified_observations` resolves
    attribution PER GROUP, so a decorated install can legitimately carry the
    weekly window under a real account while its 5h windows are still
    `unattributed`. Strict equality then matches nothing and the cycle renders
    0 blocks — the exact failure class root cause 3 was.

    `test_index_counts_stay_account_scoped_and_do_not_collapse_to_zero` cannot
    catch this: it seeds both sides with the SAME key.
    """
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    base = dt.datetime(2026, 7, 21, 18, 0, tzinfo=dt.timezone.utc)
    # Weekly window: adopted by a real account.
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=CYCLE_RESET, account_key=ACCOUNT)
    _seed_milestones(conn, key=LIMIT_KEY, window=10080, reset=CYCLE_RESET,
                     thresholds=range(1, 29), account_key=ACCOUNT,
                     captured_base=base)
    _seed_snapshot(cache_conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                   reset=CYCLE_RESET)
    # 5h window: its own physical-window group, never adopted.
    _seed_block(conn, key=FIVE_HOUR_KEY, window=300, start=BLOCK_START,
                reset=BLOCK_RESET, account_key=UNATTRIBUTED, pct=40.0)
    _seed_milestones(conn, key=FIVE_HOUR_KEY, window=300, reset=BLOCK_RESET,
                     thresholds=range(1, 4), account_key=UNATTRIBUTED,
                     captured_base=dt.datetime(2026, 7, 22, 11, 0,
                                               tzinfo=dt.timezone.utc))
    _seed_snapshot(cache_conn, key=FIVE_HOUR_KEY, window=300, start=BLOCK_START,
                   reset=BLOCK_RESET)
    conn.commit()
    cache_conn.commit()

    # Positive preconditions: the two sides really are attributed differently.
    assert conn.execute(
        "SELECT account_key FROM quota_window_blocks WHERE window_minutes=10080"
    ).fetchone()[0] == ACCOUNT
    assert conn.execute(
        "SELECT account_key FROM quota_window_blocks WHERE window_minutes=300"
    ).fetchone()[0] == UNATTRIBUTED

    entry = _index_entry(ns, conn)
    assert entry["milestone_count"] == 28
    assert entry["block_count"] == 1
    detail = _build_detail(ns, conn, cache_conn)
    assert len(detail["blocks"]) == 1
    assert len(detail["blocks"][0]["milestones"]) == 3


def test_a_second_accounts_rows_are_never_borrowed(ns):
    """`account_key` participates in QuotaWindowIdentity equality by #341
    design: two accounts sharing one physical window are deliberately distinct
    identities. Dropping the filter instead of carrying the key would merge
    them."""
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    _seed_account_stamped_cycle(conn, cache_conn, account_key=ACCOUNT,
                                thresholds=range(1, 29))
    # A SECOND account on the very same physical window, with extra milestones.
    other = "0000000011112222333344445555aaaa"
    _seed_milestones(conn, key=LIMIT_KEY, window=10080, reset=CYCLE_RESET,
                     thresholds=range(29, 41), account_key=other,
                     captured_base=dt.datetime(2026, 7, 23, 1, 0,
                                               tzinfo=dt.timezone.utc))
    conn.commit()
    cache_conn.commit()
    # Positive precondition: the other account's rows really are present.
    assert conn.execute(
        "SELECT COUNT(*) FROM quota_percent_milestones WHERE account_key=?",
        (other,)).fetchone()[0] == 12

    detail = _build_detail(ns, conn, cache_conn)
    percents = {m["percent"] for s in detail["segments"] for m in s["milestones"]}
    assert percents == set(range(1, 29))
    assert _index_entry(ns, conn)["milestone_count"] == 28
