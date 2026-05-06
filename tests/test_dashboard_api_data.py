"""Tests for /api/data JSON envelope."""
import datetime as dt
import http.client
import json
import threading

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
    so the panel can distinguish "synced + no data" from "loading"."""
    ns = load_script()
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=dt.datetime(2026, 4, 20,
                                                               12, 0, tzinfo=dt.timezone.utc))
    assert env["weekly"] == {"rows": []}
    assert env["monthly"] == {"rows": []}


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
