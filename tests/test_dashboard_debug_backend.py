"""GET /api/debug/backend — loopback-only diagnostic endpoint (issue #276).

Structural (never golden) shape assertions + the two Codex-P1 gate cases:
a hostname Host (DNS-rebinding vector) 403s, and expose_transcripts=True does
NOT open this surface (it never consults expose and still requires a loopback
Host). The non-loopback-PEER 403 is covered exhaustively by the pure-gate
matrix in tests/test_transcript_access.py (peer 192.168.0.9 cases) — a real
socket from this test always has a loopback peer.
"""
import json
import socketserver
import threading
from http.client import HTTPConnection

from conftest import load_script, redirect_paths  # type: ignore


def _boot(ns, tmp_path, monkeypatch, *, bind="127.0.0.1", expose=False):
    redirect_paths(ns, monkeypatch, tmp_path)
    H = ns["DashboardHTTPHandler"]
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.hub = ns["SSEHub"]()
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.static_dir = ns["STATIC_DIR"]
    H.cctally_host = bind
    H.cctally_expose_transcripts = expose
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _get(port, path, *, host=None):
    c = HTTPConnection("127.0.0.1", port, timeout=5)
    if host is None:
        c.request("GET", path)
    else:
        c.putrequest("GET", path, skip_host=True)
        c.putheader("Host", host)
        c.endheaders()
    r = c.getresponse()
    body = r.read()
    status = r.status
    c.close()
    return status, body


def test_debug_backend_shape_over_loopback(monkeypatch, tmp_path):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/debug/backend")
        assert status == 200
        payload = json.loads(body)
        assert payload["schemaVersion"] == 1
        assert set(payload) >= {"version", "dataset", "cache_state", "phases"}
        # tracing off in tests -> phases null + note
        assert payload["phases"] is None
        assert payload["note"] == "tracing_disabled"
        assert isinstance(payload["dataset"], dict)
        assert isinstance(payload["cache_state"], dict)
        # dataset row counts are safe cache-table names against a known-empty DB
        assert payload["dataset"].get("session_entries") == 0
    finally:
        srv.shutdown()


def test_debug_backend_403_on_hostname_host(monkeypatch, tmp_path):
    # A hostname Host from a loopback peer is a DNS-rebinding vector -> 403.
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, _ = _get(port, "/api/debug/backend", host="evil.example.com")
        assert status == 403
    finally:
        srv.shutdown()


def test_debug_backend_403_even_with_expose_transcripts(monkeypatch, tmp_path):
    # expose_transcripts=True + an IP-literal LAN Host (which the TRANSCRIPT
    # gate WOULD allow under expose) must STILL 403 here: this surface never
    # consults expose and requires a loopback Host as defense-in-depth.
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="0.0.0.0", expose=True)
    try:
        port = srv.server_address[1]
        status, _ = _get(
            port, "/api/debug/backend", host="192.168.0.9:%d" % port
        )
        assert status == 403
    finally:
        srv.shutdown()
