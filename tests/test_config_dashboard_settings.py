"""Settings validation tests for the ``dashboard`` config block.

Covers the ``POST /api/settings`` integration for the new top-level
``dashboard`` block (cache-failure-markers opt-out, spec §5):
  * valid ``{"dashboard": {"cache_failure_markers": false}}`` → 200 +
    echo + persisted config;
  * non-bool ``cache_failure_markers`` → 400 ``{error, field}``;
  * ``dashboard.bind`` / ``dashboard.expose_transcripts`` are NOT
    dashboard-writable → 400 (not live-mutable);
  * unknown inner key → 400; non-dict block → 400;
  * combined save with ``display`` validates + persists;
  * the SSE envelope mirrors the value as
    ``dashboard_prefs.cache_failure_markers`` (defaulting to ``true``
    when absent — opt-out, not opt-in).

Mirrors the cache_report settings test harness
(``tests/test_config_cache_report.py``): handler boot via
``load_script`` + ``redirect_paths`` + a booted ThreadingHTTPServer,
loopback Host/Origin parity on the POST.
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
from pathlib import Path

import datetime as dt

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


# ---------------------------------------------------------------------------
# POST /api/settings — dashboard.cache_failure_markers round-trip
# ---------------------------------------------------------------------------
def test_http_dashboard_cache_failure_markers_round_trip(monkeypatch, tmp_path):
    """Valid bool returns 200 + the echoed dashboard block + persists."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": {"cache_failure_markers": False}},
        )
        assert status == 200, body
        assert body is not None
        assert body["dashboard"] == {"cache_failure_markers": False}
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg.get("dashboard", {}).get("cache_failure_markers") is False
    finally:
        srv.shutdown()


def test_http_dashboard_cache_failure_markers_true_round_trip(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": {"cache_failure_markers": True}},
        )
        assert status == 200, body
        assert body["dashboard"] == {"cache_failure_markers": True}
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg.get("dashboard", {}).get("cache_failure_markers") is True
    finally:
        srv.shutdown()


def test_http_dashboard_non_bool_marker_returns_400(monkeypatch, tmp_path):
    """A string/int for cache_failure_markers → 400 with the field set."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        for bad in ("yes", 1, 0, "true"):
            status, body = _post_json(
                "127.0.0.1", port, "/api/settings",
                {"dashboard": {"cache_failure_markers": bad}},
            )
            assert status == 400, (bad, body)
            assert body is not None
            assert body.get("field") == "dashboard.cache_failure_markers", (bad, body)
        # Persisted config must be untouched (no partial write).
        assert not ns["CONFIG_PATH"].exists() or "dashboard" not in json.loads(
            ns["CONFIG_PATH"].read_text()
        )
    finally:
        srv.shutdown()


def test_http_dashboard_bind_rejected(monkeypatch, tmp_path):
    """dashboard.bind is NOT live-mutable via the dashboard → 400."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": {"bind": "lan"}},
        )
        assert status == 400, body
        assert body is not None
        assert body.get("field") == "dashboard.bind"
    finally:
        srv.shutdown()


def test_http_dashboard_expose_transcripts_rejected(monkeypatch, tmp_path):
    """dashboard.expose_transcripts is NOT live-mutable → 400."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": {"expose_transcripts": True}},
        )
        assert status == 400, body
        assert body is not None
        assert body.get("field") == "dashboard.expose_transcripts"
    finally:
        srv.shutdown()


def test_http_dashboard_unknown_inner_key_returns_400(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": {"frobnicate": True}},
        )
        assert status == 400, body
    finally:
        srv.shutdown()


def test_http_dashboard_non_dict_block_returns_400(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": "nope"},
        )
        assert status == 400, body
        assert body is not None
        assert body.get("field") == "dashboard"
    finally:
        srv.shutdown()


def test_http_dashboard_combined_save_with_display(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {
                "dashboard": {"cache_failure_markers": False},
                "display": {"tz": "Etc/UTC"},
            },
        )
        assert status == 200, body
        assert body["dashboard"] == {"cache_failure_markers": False}
        assert body["display"]["resolved_tz"] == "Etc/UTC"
    finally:
        srv.shutdown()


def test_http_dashboard_preserves_sibling_keys(monkeypatch, tmp_path):
    """Writing cache_failure_markers must NOT clobber a persisted
    dashboard.bind / dashboard.expose_transcripts sibling."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    ns["CONFIG_PATH"].write_text(json.dumps(
        {"dashboard": {"bind": "lan", "expose_transcripts": True}}
    ))
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"dashboard": {"cache_failure_markers": False}},
        )
        assert status == 200, body
        cfg = json.loads(ns["CONFIG_PATH"].read_text())["dashboard"]
        assert cfg["cache_failure_markers"] is False
        assert cfg["bind"] == "lan"                 # sibling preserved
        assert cfg["expose_transcripts"] is True    # sibling preserved
    finally:
        srv.shutdown()


def test_http_top_level_unknown_key_still_rejected(monkeypatch, tmp_path):
    """Adding the dashboard block does NOT widen the allowlist."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"frobnicate": {"x": 1}},
        )
        assert status == 400, body
    finally:
        srv.shutdown()


# ---------------------------------------------------------------------------
# SSE envelope mirror — dashboard_prefs.cache_failure_markers
# ---------------------------------------------------------------------------
def test_envelope_dashboard_prefs_default_true_when_absent(monkeypatch, tmp_path):
    """No config.json → the envelope mirror defaults to true (opt-out)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    now = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=now)
    assert env["dashboard_prefs"]["cache_failure_markers"] is True


def test_envelope_dashboard_prefs_reflects_persisted_false(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["CONFIG_PATH"].write_text(json.dumps(
        {"dashboard": {"cache_failure_markers": False}}
    ))
    now = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    snap = ns["_empty_dashboard_snapshot"]()
    env = ns["snapshot_to_envelope"](snap, now_utc=now)
    assert env["dashboard_prefs"]["cache_failure_markers"] is False
