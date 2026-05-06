"""Origin/Host parity matrix for /api/sync, /api/settings, /api/alerts/test."""
import http.client
import threading
from conftest import load_script, redirect_paths


def _serve(ns, host="127.0.0.1", port=0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _wire_handlers(ns):
    """Minimal wiring so all three POST handlers can run."""
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


def _post(host, port, path, *, origin_host=None, host_header=None):
    """POST {} with explicit Origin and Host headers.

    Uses skip_host so http.client doesn't override our explicit Host header
    on Python versions that auto-set it from the connection target.
    """
    c = http.client.HTTPConnection(host, port, timeout=2)
    body = "{}"
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(body)))
    if origin_host:
        c.putheader("Origin", f"http://{origin_host}")
    if host_header:
        c.putheader("Host", host_header)
    c.endheaders()
    c.send(body.encode())
    r = c.getresponse()
    r.read()
    return r.status


def test_loopback_origin_localhost_host_localhost(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status = _post("127.0.0.1", port, "/api/sync",
                       origin_host=f"localhost:{port}",
                       host_header=f"localhost:{port}")
        assert status in (200, 204), f"expected 200/204, got {status}"
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_loopback_origin_127_host_127(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status = _post("127.0.0.1", port, "/api/sync",
                       origin_host=f"127.0.0.1:{port}",
                       host_header=f"127.0.0.1:{port}")
        assert status in (200, 204)
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_lan_bind_lan_ip_origin_match(monkeypatch, tmp_path):
    """Bound to 0.0.0.0; browser uses LAN IP. Origin and Host both = LAN IP."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns, host="0.0.0.0")
    try:
        # We connect via 127.0.0.1 (loopback to the same listening socket)
        # but use Origin/Host as if a real LAN device were calling.
        status = _post("127.0.0.1", port, "/api/sync",
                       origin_host=f"192.0.2.42:{port}",
                       host_header=f"192.0.2.42:{port}")
        assert status in (200, 204)
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_origin_missing_403(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        # No Origin header at all (curl / non-browser caller).
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("POST", "/api/sync", body="{}",
                  headers={"Content-Type": "application/json"})
        r = c.getresponse(); r.read()
        assert r.status == 403
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_cross_origin_attack_rejected(monkeypatch, tmp_path):
    """evil.com page POSTing: Host = us, Origin = evil.com -> mismatch -> 403."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns, host="0.0.0.0")
    try:
        status = _post("127.0.0.1", port, "/api/sync",
                       origin_host="evil.com",
                       host_header=f"192.0.2.42:{port}")
        assert status == 403
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_malformed_origin_rejected(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("POST", "/api/sync", body="{}",
                  headers={"Content-Type": "application/json",
                           "Origin": "not a url",
                           "Host": f"127.0.0.1:{port}"})
        r = c.getresponse(); r.read()
        assert r.status == 403
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_settings_endpoint_csrf_same_rule(monkeypatch, tmp_path):
    """/api/settings shares the same Origin/Host parity check."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status_ok = _post("127.0.0.1", port, "/api/settings",
                          origin_host=f"127.0.0.1:{port}",
                          host_header=f"127.0.0.1:{port}")
        # Empty body is rejected with 400 by handler logic AFTER CSRF passes —
        # success here is "anything other than 403".
        assert status_ok != 403
        status_403 = _post("127.0.0.1", port, "/api/settings",
                            origin_host="evil.com",
                            host_header=f"127.0.0.1:{port}")
        assert status_403 == 403
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_alerts_test_endpoint_csrf_same_rule(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status_403 = _post("127.0.0.1", port, "/api/alerts/test",
                            origin_host="evil.com",
                            host_header=f"127.0.0.1:{port}")
        assert status_403 == 403
    finally:
        srv.shutdown(); t.join(timeout=2)


def test_dev_proxy_misalignment_rejected(monkeypatch, tmp_path):
    """Pins the dev-proxy footgun: rewriting only one of Origin/Host -> 403.

    Vite's changeOrigin only rewrites Host. Without the manual proxyReq
    callback that rewrites Origin to match, dev mode would silently 403
    every POST /api/sync. This test fails fast if a future config drift
    re-introduces that mismatch upstream.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        # Exact dev-mode shape: browser's Origin still localhost:5173,
        # Host got rewritten to upstream by changeOrigin.
        status = _post("127.0.0.1", port, "/api/sync",
                       origin_host="localhost:5173",
                       host_header=f"127.0.0.1:{port}")
        assert status == 403
    finally:
        srv.shutdown(); t.join(timeout=2)
