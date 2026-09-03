"""A percent milestone must never persist a cost the pre-record sync failed to
refresh.

``maybe_record_milestone`` syncs the week's cost snapshot before writing a
crossing so the row carries the cumulative cost *at* the crossing. When that
sync fails the old code logged to stderr and fell through to
``get_latest_cost_for_week``, stamping the previous snapshot's cumulative onto
the new threshold — a permanent, write-once row whose marginal is $0.00 and
whose ``$/1%`` is a lie.

Observed in production 2026-07-29: the 33% crossing at 16:48:55Z reused the
12:36:28Z snapshot ($1560.2353) because a concurrent cache ingest made the
account-scoped cost read fail closed
(``account attribution unavailable (cache required): concurrent ingest``).

The budget ladder already treats that exception as "skip this tick, fire on the
next healthy one" (#341 Task 4). These tests hold the percent ladder to the same
contract, on both cost paths:

  * the snapshot path — a failed ``cmd_sync_week`` must skip, not stamp stale;
  * the reset-week live-compute path — the same fail-closed exception must not
    escape and abort the ingest cycle.
"""
from __future__ import annotations

import pytest

from conftest import load_script, redirect_paths

_AS_OF = "2026-01-04T09:00:00Z"
_WEEK_START = "2026-01-01"
_WEEK_END = "2026-01-07"
_WEEK_START_AT = "2026-01-01T00:00:00+00:00"
_WEEK_END_AT = "2026-01-07T23:59:59+00:00"
_STALE_COST = 100.0


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda *a, **k: {})
    return ns


def _cache_mod():
    import _cctally_cache
    return _cctally_cache


def _seed(conn):
    """One week at 5%, one cost snapshot, one recorded milestone at 5."""
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-01-04T09:00:00Z", _WEEK_START, _WEEK_END, _WEEK_START_AT,
         _WEEK_END_AT, 6.0, "test", "{}"),
    )
    snap_id = int(cur.lastrowid)
    cost_cur = conn.execute(
        "INSERT INTO weekly_cost_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, range_start_iso, range_end_iso, cost_usd, source, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-01-04T05:00:00Z", _WEEK_START, _WEEK_END, _WEEK_START_AT,
         _WEEK_END_AT, _WEEK_START_AT, "2026-01-04T05:00:00+00:00",
         _STALE_COST, "test", "auto"),
    )
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-01-04T05:00:00Z", _WEEK_START, _WEEK_END, _WEEK_START_AT,
         _WEEK_END_AT, 5, _STALE_COST, None, snap_id, int(cost_cur.lastrowid)),
    )
    conn.commit()
    return snap_id


def _saved(snap_id):
    return {
        "id": snap_id,
        "weeklyPercent": 6.0,
        "weekStartDate": _WEEK_START,
        "weekEndDate": _WEEK_END,
        "weekStartAt": _WEEK_START_AT,
        "weekEndAt": _WEEK_END_AT,
        "capturedAt": _AS_OF,
    }


def _milestone(conn, threshold):
    return conn.execute(
        "SELECT cumulative_cost_usd, marginal_cost_usd FROM percent_milestones "
        "WHERE week_start_date = ? AND percent_threshold = ?",
        (_WEEK_START, threshold),
    ).fetchone()


def test_failed_cost_sync_does_not_stamp_the_stale_snapshot(ns, monkeypatch):
    """A crossing whose pre-record sync failed is skipped, not written stale."""
    def _boom(*a, **k):
        raise _cache_mod().AccountAttributionUnavailable(
            "account attribution unavailable (cache required): concurrent ingest")

    monkeypatch.setitem(ns, "cmd_sync_week", _boom)

    conn = ns["open_db"]()
    try:
        snap_id = _seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
        row = _milestone(conn, 6)
    finally:
        conn.close()

    assert row is None, (
        "the 6% crossing was persisted from the stale snapshot "
        f"(cumulative={row and row['cumulative_cost_usd']}, "
        f"marginal={row and row['marginal_cost_usd']}); a failed cost sync "
        "must skip the crossing so the next observation records a real cost"
    )


def test_skipped_crossing_is_recorded_on_the_next_healthy_tick(ns, monkeypatch):
    """Skipping must self-heal: the very next observation records the crossing
    with the freshly-synced cost, so no milestone is lost."""
    state = {"fail": True}
    fresh_cost = 175.0

    def _sync(args, *, conn=None, as_of=None, journal=None,
              account_key="unattributed", retained_selection=None):
        if state["fail"]:
            raise _cache_mod().AccountAttributionUnavailable(
                "account attribution unavailable (cache required): concurrent ingest")
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, range_start_iso, range_end_iso, cost_usd, source, "
            " mode, account_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (as_of, _WEEK_START, _WEEK_END, _WEEK_START_AT, _WEEK_END_AT,
             _WEEK_START_AT, "2026-01-04T09:00:00+00:00", fresh_cost, "test",
             "auto", account_key),
        )
        return 0

    monkeypatch.setitem(ns, "cmd_sync_week", _sync)

    conn = ns["open_db"]()
    try:
        snap_id = _seed(conn)
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
        assert _milestone(conn, 6) is None

        state["fail"] = False
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
        row = _milestone(conn, 6)
    finally:
        conn.close()

    assert row is not None, "the skipped crossing never healed"
    assert row["cumulative_cost_usd"] == pytest.approx(fresh_cost)
    assert row["marginal_cost_usd"] == pytest.approx(fresh_cost - _STALE_COST)


def test_reset_week_attribution_failure_does_not_abort_the_ingest_cycle(
    ns, monkeypatch
):
    """On a CREDITED week the cost is live-computed instead of read from a
    snapshot. That read is account-scoped too, so it can raise the same
    fail-closed exception — which on the passed-conn (ingest) path used to
    re-raise and roll back the whole cycle.

    The live-compute branch is reached by an actual credit row rather than by
    stubbing `_week_ref_has_reset_event`, because #703 + #707 removed that gate
    from the decision: the authoritative condition is the epoch the crossing is
    filed under. The credit records a landing level at or below the crossing's
    own reading, so the seeding guard admits and the skip under test is the
    fail-closed one rather than a refusal for another reason.
    """
    monkeypatch.setitem(ns, "cmd_sync_week", lambda *a, **k: 0)
    calls = []

    def _boom(*a, **k):
        calls.append(a)
        raise _cache_mod().AccountAttributionUnavailable(
            "account attribution unavailable (cache required): concurrent ingest")

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", _boom)

    conn = ns["open_db"]()
    try:
        snap_id = _seed(conn)
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, observed_post_credit_pct, "
            " credit_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("2026-01-04T08:30:00Z", None, None, "2026-01-04T08:00:00+00:00",
             46.0, "unattributed", _WEEK_START, "2026-01-04T08:30:00Z", 6.0,
             "o:creditop"))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ns["maybe_record_milestone"](_saved(snap_id), conn=conn, as_of=_AS_OF)
        conn.commit()
        row = _milestone(conn, 6)
    finally:
        conn.close()

    assert calls, (
        "the live-compute branch was never reached, so this test would pass "
        "for a reason other than the fail-closed skip")
    assert row is None
