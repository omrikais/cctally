"""Task 5 — transaction-neutral, capture-time-pure derivation chokepoints.

DB journal redesign spec §5.2.3: every derivation chokepoint the single-flight
ingester drives must (a) operate on the *caller's* connection with **no internal
``commit()``/``open_db()``** when a ``conn`` is passed, and (b) inject the
record's capture time as ``as_of`` wherever the live code consults wall clock.
Legacy callers (``conn=None`` / ``commit`` default / ``as_of=None``) keep today's
behavior bit-identical — the seam is purely additive.

These tests exercise ONLY the new seam (the discriminating assertions); the
legacy-path behavior is covered by the existing golden/reconcile harnesses.
Each new-seam assertion is non-vacuous: stashing the impl change (removing the
``conn``/``commit``/``as_of`` kwarg) makes the call raise ``TypeError`` (RED).

Conventions mirror ``tests/test_in_place_credit_detection.py``: ``load_script``
+ ``redirect_paths`` for an isolated scratch HOME; chokepoints reached via
``ns[...]``; connections via ``ns["open_db"]()``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import types

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# ── helpers ────────────────────────────────────────────────────────────

_AS_OF = "2026-01-04T09:00:00Z"          # a fixed, deterministic capture time
_THREE_DAYS_AGO = "2026-01-01T09:00:00Z"  # diverges from "now" for the anchor test


def _seed_usage_snapshot(
    conn,
    *,
    captured_at_utc,
    week_start_date,
    week_end_at,
    weekly_percent,
    week_start_at=None,
    week_end_date=None,
    five_hour_percent=None,
    five_hour_resets_at=None,
    five_hour_window_key=None,
) -> int:
    if week_start_at is None:
        week_start_at = week_start_date + "T00:00:00+00:00"
    if week_end_date is None:
        week_end_date = week_end_at[:10]
    cur = conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, "
        " five_hour_percent, five_hour_resets_at, five_hour_window_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (captured_at_utc, week_start_date, week_end_date, week_start_at,
         week_end_at, weekly_percent, "test", "{}",
         five_hour_percent, five_hour_resets_at, five_hour_window_key),
    )
    conn.commit()
    return int(cur.lastrowid)


# ── insert_cost_snapshot: +commit +as_of ───────────────────────────────


def test_insert_cost_snapshot_commit_false_leaves_txn_open(ns):
    """`commit=False` does not commit — the txn stays open on the caller's
    connection (transaction-neutral). `as_of` sets `captured_at_utc`."""
    conn = ns["open_db"]()
    try:
        rowid = ns["insert_cost_snapshot"](
            conn,
            week_start=dt.date(2026, 1, 1),
            week_end=dt.date(2026, 1, 7),
            week_start_at="2026-01-01T00:00:00+00:00",
            week_end_at="2026-01-07T23:59:59+00:00",
            range_start_iso="2026-01-01T00:00:00+00:00",
            range_end_iso="2026-01-07T23:59:59+00:00",
            cost_usd=1.25,
            mode="auto",
            project=None,
            commit=False,
            as_of=_AS_OF,
        )
        assert conn.in_transaction, "commit=False must leave the txn uncommitted"
        row = conn.execute(
            "SELECT captured_at_utc FROM weekly_cost_snapshots WHERE id = ?",
            (rowid,),
        ).fetchone()
        assert row[0] == _AS_OF
    finally:
        conn.close()


def test_insert_cost_snapshot_default_commits(ns):
    """Default `commit=True` keeps legacy behavior: the row is committed."""
    conn = ns["open_db"]()
    try:
        ns["insert_cost_snapshot"](
            conn,
            week_start=dt.date(2026, 1, 1),
            week_end=dt.date(2026, 1, 7),
            week_start_at="2026-01-01T00:00:00+00:00",
            week_end_at="2026-01-07T23:59:59+00:00",
            range_start_iso="2026-01-01T00:00:00+00:00",
            range_end_iso="2026-01-07T23:59:59+00:00",
            cost_usd=1.25,
            mode="auto",
            project=None,
        )
        assert not conn.in_transaction, "default commit=True must commit"
    finally:
        conn.close()


# ── insert_percent_milestone: +as_of ───────────────────────────────────


def test_insert_percent_milestone_as_of(ns):
    conn = ns["open_db"]()
    try:
        ns["insert_percent_milestone"](
            conn,
            week_start_date="2026-01-01",
            week_end_date="2026-01-07",
            week_start_at="2026-01-01T00:00:00+00:00",
            week_end_at="2026-01-07T23:59:59+00:00",
            percent_threshold=5,
            cumulative_cost_usd=2.0,
            marginal_cost_usd=None,
            usage_snapshot_id=1,
            cost_snapshot_id=0,
            commit=False,
            as_of=_AS_OF,
        )
        assert conn.in_transaction
        row = conn.execute(
            "SELECT captured_at_utc FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 5",
            ("2026-01-01",),
        ).fetchone()
        assert row[0] == _AS_OF
    finally:
        conn.close()


# ── _fire_in_place_credit: +as_of anchors detected_at_utc, +commit gate ─


def test_fire_in_place_credit_as_of_and_txn_neutral(ns):
    """`as_of` anchors `week_reset_events.detected_at_utc` to the capture time
    (3 days back), NOT wall clock; `commit=False` leaves the txn open."""
    week_start_date = "2026-01-01"
    cur_end_canon = "2026-01-08T00:00:00+00:00"
    effective_dt = dt.datetime(2026, 1, 4, 9, 0, 0, tzinfo=dt.timezone.utc)
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-01-03T10:00:00Z",
            week_start_date=week_start_date,
            week_end_at=cur_end_canon,
            weekly_percent=60.0,
        )
        ns["_fire_in_place_credit"](
            conn, week_start_date, cur_end_canon, 20.0,
            observed_pre_credit_pct=60.0,
            effective_dt=effective_dt,
            as_of=_THREE_DAYS_AGO,
            commit=False,
        )
        assert conn.in_transaction, "commit=False must leave the txn uncommitted"
        row = conn.execute(
            "SELECT detected_at_utc FROM week_reset_events "
            "WHERE new_week_end_at = ?",
            (cur_end_canon,),
        ).fetchone()
        assert row is not None
        assert row[0] == _THREE_DAYS_AGO, (
            "detected_at_utc must anchor to as_of, not wall clock"
        )
    finally:
        conn.close()


# ── _apply_credit: +as_of anchors applied_at_utc, +commit gate ──────────


def test_apply_credit_as_of_and_txn_neutral(ns):
    plan = types.SimpleNamespace(
        week_start_date="2026-01-01",
        week_start_at="2026-01-01T00:00:00+00:00",
        week_end_at="2026-01-07T23:59:59+00:00",
        cur_end_canon="2026-01-07T23:59:59+00:00",
        from_pct=60.0,
        from_source="hwm",
        to_pct=40.0,
        effective_iso="2026-01-04T09:00:00+00:00",
        captured_iso="2026-01-04T09:00:05Z",
    )
    conn = ns["open_db"]()
    try:
        ns["_apply_credit"](conn, plan, as_of=_THREE_DAYS_AGO, commit=False)
        assert conn.in_transaction, "commit=False must leave the txn uncommitted"
        row = conn.execute(
            "SELECT applied_at_utc FROM weekly_credit_floors "
            "WHERE week_start_date = ?",
            ("2026-01-01",),
        ).fetchone()
        assert row is not None
        assert row[0] == _THREE_DAYS_AGO
    finally:
        conn.close()


# ── cmd_sync_week: +conn +as_of ─────────────────────────────────────────


def test_cmd_sync_week_conn_txn_neutral(ns):
    """`cmd_sync_week(conn=..., as_of=...)` inserts its cost snapshot on the
    caller's connection WITHOUT committing or closing it."""
    conn = ns["open_db"]()
    try:
        args = argparse.Namespace(
            week_start=None, week_end=None, week_start_name=None,
            mode="auto", offline=True, project=None, json=False, quiet=True,
        )
        ns["cmd_sync_week"](args, conn=conn, as_of=_AS_OF)
        assert conn.in_transaction, "cost-snapshot insert must not be committed"
        row = conn.execute(
            "SELECT captured_at_utc FROM weekly_cost_snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None and row[0] == _AS_OF
        # Connection is still usable (not closed by cmd_sync_week).
        conn.execute("SELECT 1")
    finally:
        conn.close()


# ── maybe_record_milestone: +conn +as_of ────────────────────────────────


def test_maybe_record_milestone_conn_txn_neutral(ns):
    """Driven on a passed connection, a crossing folds into the caller's txn
    (uncommitted, conn not closed) and the milestone's captured_at_utc == as_of.
    """
    week_start_date = "2026-01-01"
    week_end_at = "2026-01-07T23:59:59+00:00"
    conn = ns["open_db"]()
    try:
        snap_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date=week_start_date,
            week_end_at=week_end_at,
            weekly_percent=5.0,
        )
        saved = {
            "id": snap_id,
            "weeklyPercent": 5.0,
            "weekStartDate": week_start_date,
            "weekEndDate": "2026-01-07",
            "weekStartAt": "2026-01-01T00:00:00+00:00",
            "weekEndAt": week_end_at,
            "capturedAt": "2026-01-04T08:00:00Z",
        }
        ns["maybe_record_milestone"](saved, conn=conn, as_of=_AS_OF)
        # A crossing wrote milestone rows but did NOT commit or close the conn.
        assert conn.in_transaction, "milestone fold must not be committed"
        conn.execute("SELECT 1")  # conn still open
        rows = conn.execute(
            "SELECT percent_threshold, captured_at_utc FROM percent_milestones "
            "WHERE week_start_date = ? ORDER BY percent_threshold",
            (week_start_date,),
        ).fetchall()
        assert rows, "expected a crossing to record milestone rows"
        assert all(r[1] == _AS_OF for r in rows), (
            "milestone captured_at_utc must use the injected as_of"
        )
    finally:
        conn.close()


# ── maybe_update_five_hour_block: +conn +as_of ──────────────────────────


def test_maybe_update_five_hour_block_conn_txn_neutral(ns):
    week_key = int(dt.datetime(2026, 1, 4, 5, 0, tzinfo=dt.timezone.utc).timestamp())
    saved = {
        "id": 1,
        "capturedAt": "2026-01-04T06:00:00Z",
        "weeklyPercent": 12.0,
        "fiveHourPercent": 30.0,
        "fiveHourResetsAt": "2026-01-04T10:00:00+00:00",
        "fiveHourWindowKey": week_key,
    }
    conn = ns["open_db"]()
    try:
        ns["maybe_update_five_hour_block"](saved, conn=conn, as_of=_AS_OF)
        assert conn.in_transaction, "5h-block upsert must not be committed"
        conn.execute("SELECT 1")  # conn still open
        row = conn.execute(
            "SELECT last_updated_at_utc FROM five_hour_blocks "
            "WHERE five_hour_window_key = ?",
            (week_key,),
        ).fetchone()
        assert row is not None and row[0] == _AS_OF
    finally:
        conn.close()


# ── _budget_crossings: +as_of (crossed_at_utc / alerted_at), txn-neutral ─


def test_budget_crossings_as_of_and_txn_neutral(ns):
    now_dt = dt.datetime(2026, 1, 4, 9, 0, 0, tzinfo=dt.timezone.utc)
    conn = ns["open_db"]()
    try:
        fired = ns["_budget_crossings"](
            conn,
            vendor="claude",
            period_key="2026-01-01T00:00:00+00:00",
            period="subscription-week",
            thresholds=[50],
            target=100.0,
            spent=80.0,
            now_utc=now_dt,
            as_of=_AS_OF,
        )
        assert conn.in_transaction, "budget crossings must not be committed"
        assert fired and fired[0][0] == 50
        row = conn.execute(
            "SELECT crossed_at_utc, alerted_at FROM budget_milestones "
            "WHERE vendor = 'claude' AND threshold = 50"
        ).fetchone()
        assert row is not None
        assert row[0] == _AS_OF, "crossed_at_utc must use as_of"
        assert row[1] == _AS_OF, "alerted_at must use as_of"
    finally:
        conn.close()


# ── _reconcile_budget_on_config_write: +conn (no close) +as_of ──────────


def test_reconcile_budget_on_config_write_conn_not_closed(ns):
    """When a `conn` is passed the reconcile operates on it and does NOT close
    it (Task 6 drives this from the ingest cycle)."""
    week_start_date = "2026-01-01"
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date=week_start_date,
            week_end_at="2026-01-07T23:59:59+00:00",
            weekly_percent=50.0,
        )
        validated = {
            "weekly_usd": 100.0,
            "alerts_enabled": True,
            "alert_thresholds": [50, 90],
            "period": "subscription-week",
        }
        ns["_reconcile_budget_on_config_write"](validated, conn=conn, as_of=_AS_OF)
        # Passed conn must survive the call (not closed).
        conn.execute("SELECT 1")
    finally:
        conn.close()


# ── maybe_record_budget_milestone: +conn (no close) +as_of ──────────────


def test_maybe_record_budget_milestone_conn_not_closed(ns):
    week_start_date = "2026-01-01"
    conn = ns["open_db"]()
    try:
        _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date=week_start_date,
            week_end_at="2026-01-07T23:59:59+00:00",
            weekly_percent=50.0,
        )
        saved = {"weeklyPercent": 50.0, "weekStartDate": week_start_date}
        ns["maybe_record_budget_milestone"](saved, conn=conn, as_of=_AS_OF)
        conn.execute("SELECT 1")  # conn still open (not closed by the helper)
    finally:
        conn.close()


# ── I3 gate pickup: _as_of_or_command rejects a naive capture time ──────
# `.astimezone()` on a naive datetime silently assumes host-local, which would
# make capture-time-pure derivation non-deterministic across hosts. A journal
# `at` is always UTC ISO-Z / explicit-offset, so a naive value is a caller bug
# that must fail loud rather than guess the offset.

def test_as_of_or_command_rejects_naive():
    import _cctally_core  # preserved across load_script()
    fn = _cctally_core._as_of_or_command
    # tz-aware inputs parse fine (Z and explicit offset).
    assert fn("2026-01-04T09:00:00Z").tzinfo is not None
    assert fn("2026-01-04T09:00:00+02:00").utcoffset() == dt.timedelta(0)
    # naive input (no Z, no offset) raises rather than assuming host-local.
    with pytest.raises(ValueError):
        fn("2026-01-04T09:00:00")


# ── I3 gate pickup (a): passed-conn chokepoint dispatches ZERO alerts ────
# On the ingest (passed-conn) path the chokepoint stamps `alerted_at` in-txn
# but must NOT itself fire the notification — dispatch is the ingester's
# post-commit ALERT_DISPATCHER job (spec §5.2.7). Non-vacuous: `alerted_at` is
# stamped (proving the crossing hit an enabled threshold that WOULD dispatch on
# the own-conn path), yet the dispatch spy is never called.

def test_passed_conn_milestone_stamps_but_does_not_dispatch(ns, monkeypatch):
    calls = []
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda *a, **k: calls.append((a, k)),
    )
    monkeypatch.setitem(
        ns, "load_config",
        lambda *a, **k: {"alerts": {"enabled": True, "weekly_thresholds": [5]}},
    )
    week_start_date = "2026-01-01"
    week_end_at = "2026-01-07T23:59:59+00:00"
    conn = ns["open_db"]()
    try:
        snap_id = _seed_usage_snapshot(
            conn,
            captured_at_utc="2026-01-04T08:00:00Z",
            week_start_date=week_start_date,
            week_end_at=week_end_at,
            weekly_percent=5.0,
        )
        saved = {
            "id": snap_id,
            "weeklyPercent": 5.0,
            "weekStartDate": week_start_date,
            "weekEndDate": "2026-01-07",
            "weekStartAt": "2026-01-01T00:00:00+00:00",
            "weekEndAt": week_end_at,
            "capturedAt": "2026-01-04T08:00:00Z",
        }
        ns["maybe_record_milestone"](saved, conn=conn, as_of=_AS_OF)
        # The threshold-5 crossing stamped alerted_at inside the caller's txn …
        row = conn.execute(
            "SELECT alerted_at FROM percent_milestones "
            "WHERE week_start_date = ? AND percent_threshold = 5",
            (week_start_date,),
        ).fetchone()
        assert row is not None and row[0] == _AS_OF, (
            "an enabled threshold crossing must stamp alerted_at in-txn"
        )
        # … but the chokepoint itself dispatched NOTHING (ingester's job).
        assert calls == [], "passed-conn chokepoint must not dispatch alerts"
    finally:
        conn.close()
