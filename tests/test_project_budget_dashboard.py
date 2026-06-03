"""Dashboard surface for the per-project budget alert axis (issue #19/#121).

Task 4 wires the fifth alert axis (``project_budget``) into the existing
dashboard Recent-alerts envelope + the ``alerts_settings`` mirror + the
``/api/alerts/test`` picker. These tests exercise the SERVER side:

1. ``_build_alerts_envelope_array`` emits an ``axis == "project_budget"`` item
   from seeded ``project_budget_milestones`` rows, carrying the project
   basename + the snapshotted ``$spent of $budget`` context + the shared
   severity token.
2. The ``alerts_settings`` mirror carries ``project_alerts_enabled`` sourced
   from the validated budget config.
3. ``POST /api/alerts/test {"axis": "project_budget"}`` builds a synthetic
   example payload (no real ``budget.projects`` entry) and returns 200.
4. ``POST /api/settings {"budget": {"project_alerts_enabled": true}}`` persists
   the toggle (the 4th reconcile surface — the reconcile wiring itself is
   covered byte-for-byte in tests/test_project_budget_alerts.py).

All offline/deterministic; the dashboard handler runs in a server thread.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import threading

import pytest

from conftest import load_script, redirect_paths

WEEK_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _write_budget_config(ns, *, projects=None, project_alerts_enabled=False,
                         thresholds=(90, 100)):
    import _cctally_core
    block = {
        "alerts_enabled": True,
        "alert_thresholds": list(thresholds),
        "projects": dict(projects or {}),
        "project_alerts_enabled": project_alerts_enabled,
    }
    _cctally_core.CONFIG_PATH.write_text(
        json.dumps({"budget": block}) + "\n"
    )


def _seed_project_milestone(ns, *, project_key, threshold, budget_usd,
                            spent_usd, consumption_pct, alerted=True):
    conn = ns["open_db"]()
    try:
        ns["insert_project_budget_milestone"](
            conn,
            week_start_at="2026-05-26T14:00:00+00:00",
            project_key=project_key,
            threshold=threshold,
            budget_usd=budget_usd,
            spent_usd=spent_usd,
            consumption_pct=consumption_pct,
            commit=False,
        )
        if alerted:
            conn.execute(
                "UPDATE project_budget_milestones SET alerted_at = ? "
                "WHERE project_key = ? AND threshold = ?",
                ("2026-05-26T15:00:00Z", project_key, threshold),
            )
        conn.commit()
    finally:
        conn.close()


def _wire_dashboard_handlers(ns):
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
    ns["DashboardHTTPHandler"].sync_lock = threading.Lock()
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].no_sync = False
    ns["DashboardHTTPHandler"].display_tz_pref_override = None


def _post_json(host, port, path, body):
    c = http.client.HTTPConnection(host, port, timeout=2)
    raw = json.dumps(body).encode()
    host_header = f"{host}:{port}"
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(raw)))
    c.putheader("Host", host_header)
    c.putheader("Origin", f"http://{host_header}")
    c.endheaders()
    c.send(raw)
    r = c.getresponse()
    payload = r.read().decode("utf-8", errors="replace")
    parsed = json.loads(payload) if payload else None
    return r.status, parsed


# ── (1) envelope emits the project_budget axis ───────────────────────────


def test_envelope_emits_project_budget_axis(ns):
    # /repos/foo is over budget ($26 of $25 → 104%, crosses 90 + 100).
    _seed_project_milestone(
        ns, project_key="/repos/foo", threshold=90,
        budget_usd=25.0, spent_usd=26.0, consumption_pct=104.0,
    )
    _seed_project_milestone(
        ns, project_key="/repos/foo", threshold=100,
        budget_usd=25.0, spent_usd=26.0, consumption_pct=104.0,
    )
    conn = ns["open_db"]()
    try:
        envelope = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()

    items = [a for a in envelope if a.get("axis") == "project_budget"]
    assert len(items) == 2, envelope
    by_threshold = {a["threshold"]: a for a in items}
    assert set(by_threshold) == {90, 100}

    crit = by_threshold[100]
    # Project basename resolved from the snapshotted git-root path.
    assert crit["context"]["project"] == "foo"
    assert crit["context"]["project_key"] == "/repos/foo"
    assert abs(crit["context"]["budget_usd"] - 25.0) < 1e-9
    assert abs(crit["context"]["spent_usd"] - 26.0) < 1e-9
    assert abs(crit["context"]["consumption_pct"] - 104.0) < 1e-9
    # Shared severity authority: 100 → critical, 90 → warn.
    assert crit["severity"] == "critical"
    assert by_threshold[90]["severity"] == "warn"
    # Envelope id mirrors the dispatch payload shape.
    assert crit["id"] == "project_budget:2026-05-26T14:00:00+00:00:/repos/foo:100"


def test_envelope_disambiguates_same_basename_projects(ns):
    """Two configured roots sharing a basename (/fake/work/app +
    /fake/personal/app) that both cross must render DISTINCT context.project
    labels — `app (work)` / `app (personal)` — not both collapse to `app`.
    Mirrors the live notification + budget table via _project_disambiguate_labels.
    Regression for the [check-review P2] envelope identity-collapse."""
    _seed_project_milestone(
        ns, project_key="/fake/work/app", threshold=100,
        budget_usd=25.0, spent_usd=26.0, consumption_pct=104.0,
    )
    _seed_project_milestone(
        ns, project_key="/fake/personal/app", threshold=100,
        budget_usd=25.0, spent_usd=30.0, consumption_pct=120.0,
    )
    conn = ns["open_db"]()
    try:
        envelope = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()

    items = [a for a in envelope if a.get("axis") == "project_budget"]
    label_by_key = {
        a["context"]["project_key"]: a["context"]["project"] for a in items
    }
    assert label_by_key == {
        "/fake/work/app": "app (work)",
        "/fake/personal/app": "app (personal)",
    }


def test_envelope_omits_unalerted_project_rows(ns):
    # A row with alerted_at NULL must NOT surface (forward-only semantics).
    _seed_project_milestone(
        ns, project_key="/repos/bar", threshold=90,
        budget_usd=10.0, spent_usd=9.5, consumption_pct=95.0, alerted=False,
    )
    conn = ns["open_db"]()
    try:
        envelope = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()
    assert [a for a in envelope if a.get("axis") == "project_budget"] == []


# ── (2) alerts_settings mirror carries project_alerts_enabled ────────────


def _empty_snap(ns, now):
    return ns["DataSnapshot"](
        current_week=None,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=now,
    )


def test_alerts_settings_mirror_carries_project_alerts_enabled(ns):
    _write_budget_config(
        ns, projects={"/repos/foo": 25.0}, project_alerts_enabled=True,
    )
    now = dt.datetime(2026, 5, 26, 18, 0, 0, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](_empty_snap(ns, now), now_utc=now)
    assert env["alerts_settings"]["project_alerts_enabled"] is True


def test_alerts_settings_mirror_defaults_project_alerts_off(ns):
    _write_budget_config(ns, projects={}, project_alerts_enabled=False)
    now = dt.datetime(2026, 5, 26, 18, 0, 0, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](_empty_snap(ns, now), now_utc=now)
    assert env["alerts_settings"]["project_alerts_enabled"] is False


# ── (3) /api/alerts/test accepts project_budget (synthetic payload) ──────


def test_alerts_test_endpoint_accepts_project_budget(ns, monkeypatch):
    # Capture the dispatch so no real osascript/notify-send spawns.
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: "queued",
    )
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/alerts/test",
            {"axis": "project_budget", "threshold": 100},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    assert body["alert"]["axis"] == "project_budget"
    # Synthetic example project — no real budget.projects entry required.
    assert body["alert"]["context"]["project"] == "example-project"
    assert abs(body["alert"]["context"]["budget_usd"] - 25.0) < 1e-9
    assert abs(body["alert"]["context"]["spent_usd"] - 26.0) < 1e-9
    assert body["dispatch"] == "queued"


# ── (4) POST /api/settings persists project_alerts_enabled ───────────────


def test_post_settings_persists_project_alerts_enabled(ns, monkeypatch):
    _write_budget_config(
        ns, projects={"/repos/foo": 25.0}, project_alerts_enabled=False,
    )
    # The reconcile helper resolves _sum_cost_by_project at call time; stub it
    # so the server thread never scans the filesystem.
    monkeypatch.setitem(
        ns, "_sum_cost_by_project",
        lambda start, now, mode="auto", **kw: {},
    )
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"project_alerts_enabled": True}},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    assert body["budget"]["project_alerts_enabled"] is True
    # Persisted to config.json.
    import _cctally_core
    saved = json.loads(_cctally_core.CONFIG_PATH.read_text())
    assert saved["budget"]["project_alerts_enabled"] is True
