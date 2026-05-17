"""Tests for /api/data JSON envelope."""
import datetime as dt
import http.client
import json
import threading

import pytest

from conftest import load_script


def test_envelope_has_all_top_level_keys():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert set(env.keys()) >= {
        "generated_at", "last_sync_at", "sync_age_s", "last_sync_error",
        "header", "current_week", "forecast", "trend", "sessions",
    }


def test_envelope_null_panels_on_empty_snapshot():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    # Panels that have no data serialize as None so the JS can render "—".
    assert env["current_week"] is None
    assert env["forecast"] is None
    assert env["trend"] is None
    assert env["sessions"]["total"] == 0
    assert env["sessions"]["rows"] == []


def test_envelope_generated_at_is_iso_z():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["generated_at"].endswith("Z")


def test_envelope_is_json_serializable():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    json.dumps(env)  # must not raise


def test_api_data_returns_json_200():
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=2)
        c.request("GET", "/api/data")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200
        assert r.getheader("Content-Type").startswith("application/json")
        env = json.loads(body)
        assert "header" in env
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_envelope_has_weekly_and_monthly_keys():
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert "weekly" in env
    assert "monthly" in env


def test_envelope_weekly_monthly_empty_rows_on_empty_snapshot():
    """Empty snapshot -> `{rows: []}` panel keys, NOT null. Spec §2.7
    says the empty state is `weekly.rows === []`, not `weekly === null`,
    so the panel can distinguish "synced + no data" from "loading".

    View-model unification (Bundle 1, spec §6.6) added optional
    `total_cost_usd` / `total_tokens` scalars on the monthly block;
    empty snapshots emit them as 0.0 / 0 (additive identity, not None
    — see ``DataSnapshot.monthly_total_*`` defaults). Weekly's totals
    land in Task 9.
    """
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["weekly"] == {
        "rows": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    assert env["monthly"] == {
        "rows": [],
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }


def test_envelope_weekly_emits_rows_when_snapshot_populated():
    """Hand-build a snapshot with one WeeklyPeriodRow; envelope emits it."""
    ns = load_script()
    row = ns["WeeklyPeriodRow"](
        label="04-23",
        cost_usd=48.21,
        total_tokens=346_000_000,
        input_tokens=414_000,
        output_tokens=240_000,
        cache_creation_tokens=21_300_000,
        cache_read_tokens=324_000_000,
        used_pct=41.0,
        dollar_per_pct=1.18,
        delta_cost_pct=0.09,
        is_current=True,
        models=[{"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 26.51, "cost_pct": 55.0}],
        week_start_at="2026-04-23T09:59:00+02:00",
        week_end_at="2026-04-30T09:59:00+02:00",
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.weekly_periods = [row]
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 25,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["weekly"] is not None
    assert len(env["weekly"]["rows"]) == 1
    r = env["weekly"]["rows"][0]
    assert r["label"] == "04-23"
    assert r["cost_usd"] == 48.21
    assert r["used_pct"] == 41.0
    assert r["dollar_per_pct"] == 1.18
    assert r["is_current"] is True
    assert r["models"][0]["chip"] == "opus"
    assert r["week_start_at"].startswith("2026-04-23")


def _make_weekly_row(ns, *, label, cost_usd, total_tokens,
                     week_start_at, week_end_at, is_current=False):
    """Helper for the structural-invariant tests below.

    Keeps the cross-test row construction in one place so adding a
    field to ``WeeklyPeriodRow`` only requires editing one spot.
    """
    return ns["WeeklyPeriodRow"](
        label=label,
        cost_usd=cost_usd,
        total_tokens=total_tokens,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        used_pct=None,
        dollar_per_pct=None,
        delta_cost_pct=None,
        is_current=is_current,
        models=[],
        week_start_at=week_start_at,
        week_end_at=week_end_at,
    )


def _make_monthly_row(ns, *, label, cost_usd, total_tokens):
    return ns["MonthlyPeriodRow"](
        label=label,
        cost_usd=cost_usd,
        total_tokens=total_tokens,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        delta_cost_pct=None,
        is_current=False,
        models=[],
    )


def _make_daily_row(ns, *, date, cost_usd, total_tokens, is_today=False):
    return ns["DailyPanelRow"](
        date=date,
        label=date[5:],
        cost_usd=cost_usd,
        is_today=is_today,
        intensity_bucket=0,
        models=[],
        total_tokens=total_tokens,
    )


def test_weekly_envelope_total_matches_sum_of_visible_rows():
    """Structural invariant (spec §6.6, Critical #1 regression):
    the weekly envelope's ``total_cost_usd`` / ``total_tokens`` MUST
    equal the sum over the rendered ``rows[]`` — never undercount
    synthesized rows.

    Pre-fix, ``snap.weekly_total_cost_usd`` was sourced from a
    parallel ``build_weekly_view`` call that iterated only over
    ``_aggregate_weekly`` buckets — but the dashboard's
    ``_dashboard_build_weekly_periods`` synthesizes Bug-K pre-credit
    segment rows on top of those buckets. On credit weeks the
    builder-sourced total undercounted the rendered footer by
    hundreds of dollars (~$372 in the v1.7.2 round-5 case). Coupling
    the sync-thread totals to ``sum(r.cost_usd for r in rows)`` makes
    the invariant structural.
    """
    ns = load_script()
    # Hand-build a snapshot that mimics the Bug-K synthesized layout:
    # a single subscription week containing a pre-credit synthesized
    # row PLUS the post-credit row that ``_aggregate_weekly`` produced.
    pre_row = _make_weekly_row(
        ns, label="04-18", cost_usd=372.50, total_tokens=900_000_000,
        week_start_at="2026-04-18T00:00:00Z",
        week_end_at="2026-04-21T12:30:00Z",
    )
    post_row = _make_weekly_row(
        ns, label="04-21", cost_usd=134.00, total_tokens=300_000_000,
        week_start_at="2026-04-21T12:30:00Z",
        week_end_at="2026-04-25T00:00:00Z",
        is_current=True,
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.weekly_periods = [post_row, pre_row]  # newest-first
    # Mimic what the sync thread now does (Critical #1 fix):
    # sum-over-visible-rows.
    snap.weekly_total_cost_usd = sum(r.cost_usd for r in snap.weekly_periods)
    snap.weekly_total_tokens = sum(r.total_tokens for r in snap.weekly_periods)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert env["weekly"]["rows"], "test setup must produce visible rows"
    expected_cost = sum(r["cost_usd"] for r in env["weekly"]["rows"])
    expected_tokens = sum(r["total_tokens"] for r in env["weekly"]["rows"])
    assert env["weekly"]["total_cost_usd"] == pytest.approx(
        expected_cost, abs=1e-9,
    )
    assert env["weekly"]["total_tokens"] == expected_tokens
    # And materially: the pre-credit row's $372 IS in the total.
    assert env["weekly"]["total_cost_usd"] == pytest.approx(506.50, abs=1e-9)


def test_monthly_envelope_total_matches_sum_of_visible_rows():
    """Same structural invariant for the monthly envelope (spec §6.6).
    Mirrors the weekly assertion so the symmetric fix-shape stays
    pinned (no parallel ``build_monthly_view`` totals drift).
    """
    ns = load_script()
    rows = [
        _make_monthly_row(ns, label="2026-04", cost_usd=182.50,
                          total_tokens=1_000_000_000),
        _make_monthly_row(ns, label="2026-03", cost_usd=140.25,
                          total_tokens=800_000_000),
    ]
    snap = ns["_empty_dashboard_snapshot"]()
    snap.monthly_periods = rows
    snap.monthly_total_cost_usd = sum(r.cost_usd for r in rows)
    snap.monthly_total_tokens = sum(r.total_tokens for r in rows)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert env["monthly"]["rows"], "test setup must produce visible rows"
    assert env["monthly"]["total_cost_usd"] == pytest.approx(
        sum(r["cost_usd"] for r in env["monthly"]["rows"]), abs=1e-9,
    )
    assert env["monthly"]["total_tokens"] == sum(
        r["total_tokens"] for r in env["monthly"]["rows"]
    )


def test_daily_envelope_total_matches_sum_of_visible_rows():
    """Same structural invariant for the daily envelope (spec §6.6).
    Daily's materialized panel includes zero-cost gap rows (the
    contiguous N-day calendar window); those contribute 0 to the sum
    so the invariant holds whether or not gap days are present.
    """
    ns = load_script()
    rows = [
        _make_daily_row(ns, date="2026-04-25", cost_usd=12.34,
                        total_tokens=10_000_000, is_today=True),
        # zero-cost gap day — must NOT shift the invariant.
        _make_daily_row(ns, date="2026-04-24", cost_usd=0.0, total_tokens=0),
        _make_daily_row(ns, date="2026-04-23", cost_usd=8.50,
                        total_tokens=7_000_000),
    ]
    snap = ns["_empty_dashboard_snapshot"]()
    snap.daily_panel = rows
    snap.daily_total_cost_usd = sum(r.cost_usd for r in rows)
    snap.daily_total_tokens = sum(r.total_tokens for r in rows)
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=dt.datetime(2026, 4, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert env["daily"]["rows"], "test setup must produce visible rows"
    assert env["daily"]["total_cost_usd"] == pytest.approx(
        sum(r["cost_usd"] for r in env["daily"]["rows"]), abs=1e-9,
    )
    assert env["daily"]["total_tokens"] == sum(
        r["total_tokens"] for r in env["daily"]["rows"]
    )


def test_envelope_monthly_emits_rows_when_snapshot_populated():
    ns = load_script()
    row = ns["MonthlyPeriodRow"](
        label="2026-04",
        cost_usd=182.50,
        total_tokens=1_000_000_000,
        input_tokens=2_000_000,
        output_tokens=500_000,
        cache_creation_tokens=92_000_000,
        cache_read_tokens=900_000_000,
        delta_cost_pct=0.02,
        is_current=True,
        models=[{"model": "claude-opus-4-5-20251101", "display": "opus-4-5",
                 "chip": "opus", "cost_usd": 110.0, "cost_pct": 60.0}],
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.monthly_periods = [row]
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 25,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["monthly"] is not None
    assert env["monthly"]["rows"][0]["label"] == "2026-04"
    # Monthly rows have no used_pct or week boundaries.
    assert "used_pct" not in env["monthly"]["rows"][0]
    assert "week_start_at" not in env["monthly"]["rows"][0]
