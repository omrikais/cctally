"""``POST /api/settings`` round-trip tests for the ``budget`` config block.

Budget is its OWN config block (issue #19), NOT folded into ``alerts``.
The dashboard handler accepts an inbound ``budget`` block
(``weekly_usd`` / ``alerts_enabled`` / ``alert_thresholds``), merges it
partial-PUT onto the persisted block, validates the merged result via
``_get_budget_config`` (``_BudgetConfigError`` → HTTP 400, NO partial
write), persists, and echoes the full defaults-filled block.

Mirrors ``tests/test_config_cache_report.py``'s HTTP integration: 400
(NOT 422) on validation failure; 200 + the echoed block on success. The
pure validator (``_get_budget_config``) is covered by
``tests/test_budget.py`` + ``bin/cctally-config-test``; this file closes
the handler-path coverage gap (Task 4 code review, [Important]).
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


def _serve(ns, host="127.0.0.1", port=0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _wire_handlers(ns):
    """Minimal wiring so the POST settings handler can run (mirrors
    tests/test_config_cache_report.py)."""
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


def _post_json(host, port, path, body, *, origin_host=None):
    """POST a JSON body with matched Host + Origin (loopback CSRF contract)."""
    c = http.client.HTTPConnection(host, port, timeout=2)
    raw = json.dumps(body).encode()
    host_header = f"{host}:{port}"
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(raw)))
    c.putheader("Host", host_header)
    c.putheader("Origin", f"http://{origin_host or host_header}")
    c.endheaders()
    c.send(raw)
    r = c.getresponse()
    payload = r.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(payload) if payload else None
    except json.JSONDecodeError:
        parsed = payload
    return r.status, parsed


def test_http_budget_valid_round_trip(monkeypatch, tmp_path):
    """Valid block → 200 + the echoed defaults-filled budget block, persisted."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"weekly_usd": 300, "alert_thresholds": [90, 100]}},
        )
        assert status == 200, body
        assert body is not None
        # Echo is the full validated block (defaults filled: alerts_enabled True).
        assert body["budget"] == {
            "weekly_usd": 300.0,
            "alerts_enabled": True,
            "alert_thresholds": [90, 100],
        }
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg.get("budget", {}).get("weekly_usd") == 300.0
        assert cfg["budget"]["alert_thresholds"] == [90, 100]
    finally:
        srv.shutdown()


def test_http_budget_unset_via_null_weekly_usd(monkeypatch, tmp_path):
    """weekly_usd: null clears the target (preserving sibling keys)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    ns["CONFIG_PATH"].write_text(
        json.dumps({"budget": {"weekly_usd": 200.0,
                               "alert_thresholds": [80, 95]}})
    )
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"weekly_usd": None}},
        )
        assert status == 200, body
        assert body["budget"]["weekly_usd"] is None
        # Sibling thresholds preserved (partial-PUT merge).
        assert body["budget"]["alert_thresholds"] == [80, 95]
    finally:
        srv.shutdown()


def test_http_budget_invalid_weekly_usd_returns_400(monkeypatch, tmp_path):
    """weekly_usd <= 0 → 400 (no partial write)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"weekly_usd": -5}},
        )
        assert status == 400, body
        assert body is not None
        assert "weekly_usd" in body.get("error", "")
        # No config written (400 short-circuits before save_config).
        assert not ns["CONFIG_PATH"].exists() or "budget" not in json.loads(
            ns["CONFIG_PATH"].read_text()
        )
    finally:
        srv.shutdown()


def test_http_budget_threshold_over_100_returns_400(monkeypatch, tmp_path):
    """A threshold > 100 (F5 cap) → 400 with the [1, 100] message."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"weekly_usd": 300, "alert_thresholds": [90, 150]}},
        )
        assert status == 400, body
        assert "[1, 100]" in body.get("error", "")
    finally:
        srv.shutdown()


def test_http_budget_non_dict_block_returns_400(monkeypatch, tmp_path):
    """budget: "abc" → 400."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": "not-a-dict"},
        )
        assert status == 400, body
        assert body.get("error") == "budget must be an object"
    finally:
        srv.shutdown()


def test_http_budget_partial_save_preserves_untouched_leaves(monkeypatch, tmp_path):
    """A partial save (only alerts_enabled) must NOT clobber the persisted
    weekly_usd / alert_thresholds — partial-PUT merge (mirrors cache_report H3)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    ns["CONFIG_PATH"].write_text(
        json.dumps({"budget": {"weekly_usd": 250.0,
                               "alert_thresholds": [85, 95]}})
    )
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"budget": {"alerts_enabled": False}},
        )
        assert status == 200, body
        assert body["budget"]["alerts_enabled"] is False
        assert body["budget"]["weekly_usd"] == 250.0          # preserved
        assert body["budget"]["alert_thresholds"] == [85, 95]  # preserved
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg["budget"]["weekly_usd"] == 250.0
    finally:
        srv.shutdown()


def test_http_budget_combined_save_with_display(monkeypatch, tmp_path):
    """Combined save: budget + display both validate and persist."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {
                "budget": {"weekly_usd": 400},
                "display": {"tz": "Etc/UTC"},
            },
        )
        assert status == 200, body
        assert body["budget"]["weekly_usd"] == 400.0
        assert body["display"]["resolved_tz"] == "Etc/UTC"
    finally:
        srv.shutdown()


# ── Fix #3: POST /api/alerts/test accepts the `budget` axis (mirrors CLI) ──
#
# The endpoint previously rejected any axis not in ("weekly", "five_hour")
# with a 400, even though the CLI `cctally alerts test` handles budget and the
# React client is budget-aware. It now mirrors the CLI's budget branch,
# building the payload via `_build_alert_payload_budget` with the same
# synthetic values, and returns 200 with a budget payload + dispatch status.


def test_http_alerts_test_budget_axis_returns_200_payload(monkeypatch, tmp_path):
    """{"axis":"budget","threshold":100} → 200 with a budget payload + a
    dispatch status (osascript stubbed so the test is host-independent)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    # Stub dispatch so no osascript is spawned; the dashboard shim resolves
    # _dispatch_alert_notification via sys.modules["cctally"] at call time.
    monkeypatch.setitem(
        ns, "_dispatch_alert_notification",
        lambda payload, *, mode="real", **kw: "queued",
    )
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/alerts/test",
            {"axis": "budget", "threshold": 100},
        )
        assert status == 200, body
        assert body is not None
        assert "alert" in body and "dispatch" in body
        payload = body["alert"]
        assert payload["axis"] == "budget"
        assert payload["threshold"] == 100
        # Synthetic values mirror the CLI budget branch: $300 budget, spent
        # scaled to the threshold (100% → $300 of $300). Budget context is
        # nested under "context" (see _build_alert_payload_budget).
        ctx = payload["context"]
        assert abs(ctx["budget_usd"] - 300.0) < 1e-9
        assert abs(ctx["spent_usd"] - 300.0) < 1e-9
        assert abs(ctx["consumption_pct"] - 100.0) < 1e-9
        assert body["dispatch"] == "queued"
    finally:
        srv.shutdown()


def test_http_alerts_test_invalid_axis_400_message_lists_budget(monkeypatch, tmp_path):
    """An unknown axis still 400s, and the message now enumerates `budget`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/alerts/test",
            {"axis": "bogus", "threshold": 90},
        )
        assert status == 400, body
        assert "budget" in body.get("error", "")
    finally:
        srv.shutdown()
