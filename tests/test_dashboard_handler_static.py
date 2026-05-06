"""HTTP smoke tests for the static-serving path of DashboardHTTPHandler."""
import http.client
import threading
import time

from conftest import load_script


def _serve_once(ns, host="127.0.0.1", port=0):
    """Start the server on an ephemeral port, return (srv, thread, port)."""
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def test_static_placeholder_served_with_200():
    ns = load_script()
    # Seed the handler's hub so /api/events wouldn't fail (not tested here).
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = _serve_once(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/static/placeholder.txt")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200, f"status={r.status}"
        assert "placeholder" in body
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_static_404_on_missing_file():
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = _serve_once(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/static/does-not-exist.css")
        r = c.getresponse()
        r.read()
        assert r.status == 404
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_static_denies_path_traversal():
    """Must not serve files outside STATIC_DIR."""
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = _serve_once(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/static/../../bin/cctally")
        r = c.getresponse()
        r.read()
        assert r.status == 400
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_static_denies_percent_encoded_traversal():
    """Percent-encoded `..` bypasses lexical check but must be caught by containment."""
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = _serve_once(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/static/%2e%2e/%2e%2e/bin/cctally")
        r = c.getresponse()
        r.read()
        # 403 is the deterministic outcome: lexical check doesn't fire
        # (rel contains no literal ".."), but relative_to() rejects the
        # resolved path as outside STATIC_DIR.
        assert r.status in (403, 404)
    finally:
        srv.shutdown()
        t.join(timeout=2)
