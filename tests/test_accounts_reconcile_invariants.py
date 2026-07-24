"""Per-account reconcile invariants (#341 Task 1, Step 10, review finding P3-1).

The 8a review recommended extending the reconcile surface with per-account
runs: for each account bucket present in the derived tables, the account-scoped
HWM/floor + milestone math must be internally consistent — the SAME invariants
the global reconcile enforces, applied per account partition rather than
globally. Concretely: one account's mid-week credit/reset floor must not clamp
another account's week, and one account's milestone ledger (max threshold +
cumulative cost, 1e-9 USD tolerance) must not merge with another's.

This lives in the pytest reconcile surface (not the bin/cctally-reconcile-test
bash harness) because that harness reconciles two SUBCOMMANDS' JSON over shared
fixtures, and the per-account CLI (`--account`) does not land until Task 3 — the
kernels (`_reset_aware_floor`, `get_milestone_cost_for_week`,
`get_max_milestone_for_week`) are the reconcilable surface today. SQLite DB
fixtures are also not byte-portable across versions, so an in-process seed is
the byte-stable equivalent of a fixture builder (project gotcha).

Isolation mirrors tests/test_accounts_journal.py.
"""
from __future__ import annotations

import pytest

from conftest import load_script, redirect_paths

_TOL = 1e-9

_WSD = "2026-01-04"
_WSA = "2026-01-04T00:00:00+00:00"
_WEA = "2026-01-11T00:00:00+00:00"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _acc():
    import _lib_accounts
    return _lib_accounts


def _seed_credit_floor(conn, *, account_key, effective_at_utc,
                       observed_pre_credit_pct=46.0):
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
        " applied_at_utc, account_key) VALUES (?, ?, ?, ?, ?)",
        (_WSD, effective_at_utc, observed_pre_credit_pct,
         "2026-01-09T00:00:00Z", account_key),
    )


# --------------------------------------------------------------------------
# Floor chokepoint — per-account partition
# --------------------------------------------------------------------------

def test_reset_aware_floor_is_per_account(ns):
    """One account's mid-week credit floor must not leak into another's week.
    Account B's floor is strictly LATER than account A's; the account-scoped
    floor returns each account's own moment, and the merged (account_key=None)
    read returns the latest across both — proving the scoped exclusion is the
    account predicate, not an empty query (non-vacuity)."""
    acc = _acc()
    key_a = acc.account_key("claude", "uuid-A")
    key_b = acc.account_key("claude", "uuid-B")
    floor = ns["_reset_aware_floor"]
    t_a = "2026-01-06T00:00:00Z"
    t_b = "2026-01-08T00:00:00Z"  # strictly later than A's

    conn = ns["open_db"]()
    try:
        _seed_credit_floor(conn, account_key=key_a, effective_at_utc=t_a)
        _seed_credit_floor(conn, account_key=key_b, effective_at_utc=t_b)
        conn.commit()

        floor_a = floor(conn, _WSD, _WSA, _WEA, account_key=key_a)
        floor_b = floor(conn, _WSD, _WSA, _WEA, account_key=key_b)
        floor_merged = floor(conn, _WSD, _WSA, _WEA, account_key=None)
    finally:
        conn.close()

    assert floor_a == t_a, "account A's scoped floor is A's own credit moment"
    assert floor_b == t_b, "account B's scoped floor is B's own credit moment"
    assert floor_a != floor_b, "the two accounts' floors are distinct"
    assert floor_merged == t_b, (
        "the merged read returns the latest across BOTH accounts — proving A's "
        "scoped floor excluded B's later floor by the account predicate, not "
        "vacuously")


def test_reset_aware_floor_absent_account_is_none(ns):
    """An account with NO floor row in the week resolves to None (no floor),
    even while a sibling account has one — the scoping never borrows a
    neighbour's floor."""
    acc = _acc()
    key_a = acc.account_key("claude", "uuid-A")
    key_c = acc.account_key("claude", "uuid-C")  # no floor row
    floor = ns["_reset_aware_floor"]

    conn = ns["open_db"]()
    try:
        _seed_credit_floor(conn, account_key=key_a,
                           effective_at_utc="2026-01-06T00:00:00Z")
        conn.commit()
        floor_c = floor(conn, _WSD, _WSA, _WEA, account_key=key_c)
        floor_a = floor(conn, _WSD, _WSA, _WEA, account_key=key_a)
    finally:
        conn.close()

    assert floor_c is None, "an account with no floor row has no floor"
    assert floor_a == "2026-01-06T00:00:00Z", "the sibling's floor is intact"


# --------------------------------------------------------------------------
# Milestone ledger — per-account partition (max threshold + cumulative cost)
# --------------------------------------------------------------------------

def test_milestone_ledger_is_per_account(ns):
    """Two accounts crossing thresholds in the SAME week keep independent
    ledgers: `get_max_milestone_for_week` and `get_milestone_cost_for_week`
    (1e-9 USD tolerance) each resolve one account's rows only, never a merged
    max/cost."""
    acc = _acc()
    key_a = acc.account_key("claude", "uuid-A")
    key_b = acc.account_key("claude", "uuid-B")
    insert = ns["insert_percent_milestone"]
    get_max = ns["get_max_milestone_for_week"]
    get_cost = ns["get_milestone_cost_for_week"]

    conn = ns["open_db"]()
    try:
        # Account A crosses 60 at $12.50 cumulative.
        insert(conn, _WSD, "2026-01-11", _WSA, _WEA, 60, 12.5, 12.5, 0, 0,
               commit=False, account_key=key_a)
        # Account B crosses 60 at a DIFFERENT cost AND a higher max (70).
        insert(conn, _WSD, "2026-01-11", _WSA, _WEA, 60, 7.25, 7.25, 0, 0,
               commit=False, account_key=key_b)
        insert(conn, _WSD, "2026-01-11", _WSA, _WEA, 70, 9.0, 1.75, 0, 0,
               commit=False, account_key=key_b)
        conn.commit()

        max_a = get_max(conn, _WSD, account_key=key_a)
        max_b = get_max(conn, _WSD, account_key=key_b)
        cost_a = get_cost(conn, _WSD, 60, account_key=key_a)
        cost_b = get_cost(conn, _WSD, 60, account_key=key_b)
        # Non-vacuity: a global (account-blind) MAX would return 70 for BOTH.
        global_max = conn.execute(
            "SELECT MAX(percent_threshold) FROM percent_milestones "
            "WHERE week_start_date = ?", (_WSD,)).fetchone()[0]
    finally:
        conn.close()

    assert max_a == 60, "account A's own max is 60, not the global 70"
    assert max_b == 70, "account B's own max is 70"
    assert global_max == 70, (
        "the account-blind global max is 70 — proving max_a==60 is the account "
        "predicate at work, not an empty ledger")
    assert abs(cost_a - 12.5) < _TOL, "account A's 60% cumulative cost"
    assert abs(cost_b - 7.25) < _TOL, "account B's 60% cumulative cost"
    assert abs(cost_a - cost_b) > _TOL, (
        "the two accounts' 60% cumulative costs are distinct — the ledgers "
        "never merge")
