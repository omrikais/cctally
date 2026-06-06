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


# ═══════════════════════════════════════════════════════════════════════════
# #134 — dashboard Codex budget toggles (nested partial-merge writer + SSE
# fields + the broken codex_budget test-alert fix). Server-side only (Task 2).
# ═══════════════════════════════════════════════════════════════════════════


def _write_codex_budget_config(
    ns, *, codex, alerts_enabled=True, alert_thresholds=(90, 100),
):
    """Persist a ``budget`` block carrying a nested ``codex`` sub-block.

    ``codex`` is the literal nested dict to persist (or ``None`` to seed the
    no-Codex-budget sentinel — the null-codex 400 case). The parent block keeps
    a valid Claude side so ``_get_budget_config`` validates cleanly.
    """
    import _cctally_core
    block = {
        "alerts_enabled": alerts_enabled,
        "alert_thresholds": list(alert_thresholds),
        "projects": {},
        "project_alerts_enabled": False,
    }
    if codex is not None:
        block["codex"] = codex
    _cctally_core.CONFIG_PATH.write_text(json.dumps({"budget": block}) + "\n")


# ── 2A: nested partial-merge for budget.codex ────────────────────────────


def test_post_settings_codex_nested_merge_no_clobber(ns, monkeypatch):
    """Toggling budget.codex.alerts_enabled must NOT clobber the sibling
    amount_usd/period/alert_thresholds (the nested partial-merge contract)."""
    _write_codex_budget_config(
        ns,
        codex={
            "amount_usd": 200,
            "period": "calendar-month",
            "alerts_enabled": False,
            "alert_thresholds": [90, 100],
            "projected_enabled": False,
        },
    )
    # The reconcile helper (fired by flipping alerts_enabled) resolves the
    # Codex cost SUM at call time; stub it so the server thread never scans
    # the filesystem and no crossing is fabricated.
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range",
        lambda start, now, **kw: 0.0,
    )
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"codex": {"alerts_enabled": True}}},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    import _cctally_core
    saved = json.loads(_cctally_core.CONFIG_PATH.read_text())
    codex = saved["budget"]["codex"]
    # Sibling fields preserved — the regression this guards against.
    assert codex["amount_usd"] == 200
    assert codex["period"] == "calendar-month"
    assert codex["alert_thresholds"] == [90, 100]
    # The toggled leaf flipped on.
    assert codex["alerts_enabled"] is True
    # The untouched projected_enabled stayed put.
    assert codex["projected_enabled"] is False


def test_post_settings_codex_null_budget_400(ns):
    """POST budget.codex.* when no Codex budget is configured → 400 (the
    server fails closed; the frontend disables the toggle but the writer
    must not invent a Codex budget)."""
    _write_codex_budget_config(ns, codex=None)
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"codex": {"alerts_enabled": True}}},
        )
    finally:
        srv.shutdown()

    assert status == 400, body
    assert "Codex budget" in body["error"]
    # Nothing persisted — the null sentinel survives.
    import _cctally_core
    saved = json.loads(_cctally_core.CONFIG_PATH.read_text())
    assert "codex" not in saved["budget"]


def test_post_settings_codex_not_object_400(ns):
    """A non-dict budget.codex inbound block → 400 (hand-edited-junk guard)."""
    _write_codex_budget_config(
        ns,
        codex={
            "amount_usd": 200,
            "period": "calendar-month",
            "alerts_enabled": False,
            "alert_thresholds": [90, 100],
            "projected_enabled": False,
        },
    )
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"codex": ["not", "a", "dict"]}},
        )
    finally:
        srv.shutdown()

    assert status == 400, body
    assert "must be an object" in body["error"]


# ── 2B: reconcile dispatch keyed on the alerts_enabled sub-leaf ───────────


def _count_codex_milestones(ns):
    conn = ns["open_db"]()
    try:
        # Unified vendor-tagged table (#143): count only the Codex rows.
        return conn.execute(
            "SELECT COUNT(*) FROM budget_milestones WHERE vendor = 'codex'"
        ).fetchone()[0]
    finally:
        conn.close()


def test_post_settings_codex_projected_toggle_does_not_latch(ns, monkeypatch):
    """Toggling ONLY projected_enabled must NOT run the actual-spend reconcile
    — keying the dispatch on the alerts_enabled sub-leaf (NOT "codex" in
    touched) keeps projected live-pace and avoids silently latching a pending
    actual-spend crossing (spec §4 critical note)."""
    # Seed alerts already ON + a stubbed spend WAY over budget, so the
    # actual-spend reconcile WOULD latch both thresholds if it ran. This makes
    # the "no-latch" assertion non-vacuous.
    _write_codex_budget_config(
        ns,
        codex={
            "amount_usd": 200,
            "period": "calendar-month",
            "alerts_enabled": True,
            "alert_thresholds": [90, 100],
            "projected_enabled": False,
        },
    )
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range",
        lambda start, now, **kw: 500.0,  # 250% — crosses 90 + 100
    )
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"codex": {"projected_enabled": True}}},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    # Projected toggle alone reconciles NOTHING — no actual-spend rows latched.
    assert _count_codex_milestones(ns) == 0


def test_post_settings_codex_alerts_toggle_does_latch(ns, monkeypatch):
    """Companion non-vacuity: flipping alerts_enabled ON DOES run the
    actual-spend reconcile (latching already-crossed thresholds silently) —
    proving the stub + seed actually produce crossings, so the projected-only
    no-latch test above is meaningful."""
    _write_codex_budget_config(
        ns,
        codex={
            "amount_usd": 200,
            "period": "calendar-month",
            "alerts_enabled": False,
            "alert_thresholds": [90, 100],
            "projected_enabled": False,
        },
    )
    monkeypatch.setitem(
        ns, "_sum_codex_cost_for_range",
        lambda start, now, **kw: 500.0,  # 250% — crosses 90 + 100
    )
    _wire_dashboard_handlers(ns)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"codex": {"alerts_enabled": True}}},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    # Both thresholds latched (alerted_at set, no dispatch) — forward-only.
    assert _count_codex_milestones(ns) == 2


# ── 2C: SSE alerts_settings exposes the three Codex fields ────────────────


def test_alerts_settings_exposes_codex_fields_when_configured(ns):
    _write_codex_budget_config(
        ns,
        codex={
            "amount_usd": 200,
            "period": "calendar-month",
            "alerts_enabled": True,
            "alert_thresholds": [90, 100],
            "projected_enabled": True,
        },
    )
    now = dt.datetime(2026, 5, 26, 18, 0, 0, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](_empty_snap(ns, now), now_utc=now)
    settings = env["alerts_settings"]
    assert settings["codex_budget_configured"] is True
    assert settings["codex_budget_alerts_enabled"] is True
    assert settings["codex_projected_enabled"] is True


def test_alerts_settings_codex_fields_default_off_when_absent(ns):
    _write_budget_config(ns, projects={}, project_alerts_enabled=False)
    now = dt.datetime(2026, 5, 26, 18, 0, 0, tzinfo=dt.timezone.utc)
    env = ns["snapshot_to_envelope"](_empty_snap(ns, now), now_utc=now)
    settings = env["alerts_settings"]
    assert settings["codex_budget_configured"] is False
    assert settings["codex_budget_alerts_enabled"] is False
    assert settings["codex_projected_enabled"] is False


# ── 2D: the broken codex_budget test-alert (R4) + projected metric (R2) ───


def test_alerts_test_endpoint_accepts_codex_budget(ns, monkeypatch):
    """POST {"axis": "codex_budget"} to /api/alerts/test must return 2xx +
    dispatch a synthetic payload (currently 400 — R4)."""
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
            {"axis": "codex_budget", "threshold": 100},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    assert body["alert"]["axis"] == "codex_budget"
    assert abs(body["alert"]["context"]["budget_usd"] - 200.0) < 1e-9
    assert abs(body["alert"]["context"]["spent_usd"] - 200.0) < 1e-9
    assert body["alert"]["context"]["period"] == "calendar-month"
    assert body["dispatch"] == "queued"


def test_alerts_test_endpoint_accepts_projected_codex_metric(ns, monkeypatch):
    """POST {"axis": "projected", "metric": "codex_budget_usd"} must return
    2xx (currently 400 — the metric was rejected; R2)."""
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
            {"axis": "projected", "metric": "codex_budget_usd",
             "threshold": 100},
        )
    finally:
        srv.shutdown()

    assert status == 200, body
    assert body["alert"]["axis"] == "projected"
    assert body["alert"]["context"]["metric"] == "codex_budget_usd"
    assert body["dispatch"] == "queued"


# ═══════════════════════════════════════════════════════════════════════════
# #137 Task 2 — envelope mappers read ``period`` FROM THE ROW (Symptom 1) and
# carry the period segment in the React-key ``id``. The historical pre-011
# NULL-period sentinel COALESCEs to the vendor-default noun.
# ═══════════════════════════════════════════════════════════════════════════


def _seed_budget_milestone(ns, *, week_start_at, period, threshold,
                           budget_usd=300.0, spent_usd=290.0,
                           consumption_pct=96.0, alerted=True):
    # Claude rows in the unified vendor-tagged table (#143): the seed param
    # keeps the legacy `week_start_at` NAME (the instant), routed to the
    # `period_start_at` column with `vendor='claude'`.
    conn = ns["open_db"]()
    try:
        ns["insert_budget_milestone"](
            conn,
            vendor="claude",
            period_start_at=week_start_at,
            period=period,
            threshold=threshold,
            budget_usd=budget_usd,
            spent_usd=spent_usd,
            consumption_pct=consumption_pct,
            commit=False,
        )
        if alerted:
            conn.execute(
                "UPDATE budget_milestones SET alerted_at = ? "
                "WHERE vendor = 'claude' AND period_start_at = ? "
                "  AND threshold = ?",
                ("2026-06-01T15:00:00Z", week_start_at, threshold),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_codex_budget_milestone(ns, *, period_start_at, period, threshold,
                                 budget_usd=200.0, spent_usd=195.0,
                                 consumption_pct=97.5, alerted=True):
    # Codex rows in the unified vendor-tagged table (#143): same helper with
    # `vendor='codex'`.
    conn = ns["open_db"]()
    try:
        ns["insert_budget_milestone"](
            conn,
            vendor="codex",
            period_start_at=period_start_at,
            period=period,
            threshold=threshold,
            budget_usd=budget_usd,
            spent_usd=spent_usd,
            consumption_pct=consumption_pct,
            commit=False,
        )
        if alerted:
            conn.execute(
                "UPDATE budget_milestones SET alerted_at = ? "
                "WHERE vendor = 'codex' AND period_start_at = ? "
                "  AND threshold = ?",
                ("2026-06-01T15:00:00Z", period_start_at, threshold),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_projected_milestone(ns, *, week_start_at, period, metric, threshold,
                              projected_value=110.0, denominator=100.0,
                              alerted=True):
    conn = ns["open_db"]()
    try:
        ns["insert_projected_milestone"](
            conn,
            week_start_at=week_start_at,
            period=period,
            metric=metric,
            threshold=threshold,
            projected_value=projected_value,
            denominator=denominator,
            commit=False,
        )
        if alerted:
            conn.execute(
                "UPDATE projected_milestones SET alerted_at = ? "
                "WHERE week_start_at = ? AND metric = ? AND threshold = ?",
                ("2026-06-01T15:00:00Z", week_start_at, metric, threshold),
            )
        conn.commit()
    finally:
        conn.close()


def _envelope_items(ns, axis):
    conn = ns["open_db"]()
    try:
        envelope = ns["_cctally_dashboard"]._build_alerts_envelope_array(conn)
    finally:
        conn.close()
    return [a for a in envelope if a.get("axis") == axis]


def _set_config(ns, block):
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(json.dumps(block) + "\n")


# ── budget: period from the ROW, not live config (Symptom 1) ──────────────


def test_budget_envelope_period_from_row_not_live_config(ns):
    """A subscription-week row keeps its noun after the user switches
    budget.period to calendar-month — the period comes FROM THE ROW."""
    _seed_budget_milestone(
        ns, week_start_at="2026-06-01T00:00:00+00:00",
        period="subscription-week", threshold=90,
    )
    # Live config now says calendar-month — must NOT leak onto the historical row.
    _set_config(ns, {"budget": {
        "weekly_usd": 300.0, "period": "calendar-month", "alerts_enabled": True,
    }})
    items = _envelope_items(ns, "budget")
    assert len(items) == 1, items
    assert items[0]["context"]["period"] == "subscription-week"
    assert items[0]["id"] == (
        "budget:2026-06-01T00:00:00+00:00:subscription-week:90"
    )


def test_budget_envelope_null_period_coalesces_to_default(ns):
    """A pre-011 NULL-period row renders the vendor-default noun via COALESCE
    and never lands a literal 'None' in the id."""
    _seed_budget_milestone(
        ns, week_start_at="2026-05-01T00:00:00+00:00",
        period=None, threshold=100,
    )
    items = _envelope_items(ns, "budget")
    assert len(items) == 1, items
    assert items[0]["context"]["period"] == "subscription-week"
    assert "None" not in items[0]["id"]
    assert items[0]["id"] == (
        "budget:2026-05-01T00:00:00+00:00:subscription-week:100"
    )


# ── codex_budget: vendor default calendar-month ───────────────────────────


def test_codex_budget_envelope_period_from_row_not_live_config(ns):
    """A calendar-week Codex row keeps its noun after the user switches
    budget.codex.period to calendar-month."""
    _seed_codex_budget_milestone(
        ns, period_start_at="2026-06-01T00:00:00+00:00",
        period="calendar-week", threshold=90,
    )
    _set_config(ns, {"budget": {
        "weekly_usd": 300.0, "alerts_enabled": True,
        "codex": {"amount_usd": 200.0, "period": "calendar-month",
                  "alerts_enabled": True},
    }})
    items = _envelope_items(ns, "codex_budget")
    assert len(items) == 1, items
    assert items[0]["context"]["period"] == "calendar-week"
    assert items[0]["id"] == (
        "codex_budget:2026-06-01T00:00:00+00:00:calendar-week:90"
    )


def test_codex_budget_envelope_null_period_coalesces_to_default(ns):
    """A pre-011 NULL-period Codex row renders the vendor-default
    (calendar-month) noun and a non-None id."""
    _seed_codex_budget_milestone(
        ns, period_start_at="2026-05-01T00:00:00+00:00",
        period=None, threshold=100,
    )
    items = _envelope_items(ns, "codex_budget")
    assert len(items) == 1, items
    assert items[0]["context"]["period"] == "calendar-month"
    assert "None" not in items[0]["id"]
    assert items[0]["id"] == (
        "codex_budget:2026-05-01T00:00:00+00:00:calendar-month:100"
    )


# ── projected: id gains the period segment (context stays metric-driven) ──


def test_projected_envelope_id_carries_period_segment(ns):
    _seed_projected_milestone(
        ns, week_start_at="2026-06-01T00:00:00+00:00",
        period="subscription-week", metric="weekly_pct", threshold=100,
    )
    items = _envelope_items(ns, "projected")
    assert len(items) == 1, items
    assert items[0]["id"] == (
        "projected:2026-06-01T00:00:00+00:00:subscription-week:weekly_pct:100"
    )
    # Context stays metric-driven — no live-config period noun.
    assert "period" not in items[0]["context"]


def test_projected_envelope_null_period_coalesces_in_id(ns):
    _seed_projected_milestone(
        ns, week_start_at="2026-05-01T00:00:00+00:00",
        period=None, metric="budget_usd", threshold=90,
    )
    items = _envelope_items(ns, "projected")
    assert len(items) == 1, items
    assert "None" not in items[0]["id"]
    assert items[0]["id"] == (
        "projected:2026-05-01T00:00:00+00:00:subscription-week:budget_usd:90"
    )
