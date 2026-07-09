"""#279 S5 F6.1/F6.2: /api/data 500-wrap + SSE mid-stream error logging (spec §8).

Behavior-annotated hardening — new behavior ONLY on previously-crash paths:
  * an exception in the /api/data body build (snapshot -> envelope -> dumps)
    used to escape to _QuietThreadingHTTPServer.handle_error, printing a
    stdlib traceback and dropping the socket with NO 500. It now returns a
    JSON 500 {"error": "internal error"}.
  * an exception mid-SSE (after headers are committed, so no 500 is possible)
    used to kill the stream silently via handle_error. It now routes through
    self.log_error (the operator signal) and closes cleanly.

These boot a real _QuietThreadingHTTPServer on an ephemeral loopback port and
drive it over http.client, mirroring test_dashboard_api_data /
test_dashboard_api_events. The SSE read uses the r.fp.read1() deadline loop
(Python 3.14 blocks on read(n) for a Content-Length-less stream).
"""
import http.client
import sys
import threading
import time

import pytest

from conftest import load_script


@pytest.fixture(autouse=True)
def _isolate_prod_dbs(monkeypatch, tmp_path):
    """Redirect $HOME so the envelope build's cache.db/stats.db opens land in
    tmp, not the real ~/.local/share/cctally (mirrors test_dashboard_api_data).
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _boot(ns):
    dash = sys.modules["_cctally_dashboard"]
    dash.DashboardHTTPHandler.hub = ns["SSEHub"]()
    dash.DashboardHTTPHandler.snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv = dash._QuietThreadingHTTPServer(
        ("127.0.0.1", 0), dash.DashboardHTTPHandler
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return dash, srv, t


def test_api_data_500_wrap(monkeypatch):
    ns = load_script()
    dash, srv, t = _boot(ns)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(dash, "snapshot_to_envelope", _boom)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        c.request("GET", "/api/data")
        r = c.getresponse()
        body = r.read().decode()
        # A JSON 500 — NOT a dropped connection / stdlib traceback.
        assert r.status == 500, body
        import json
        assert json.loads(body) == {"error": "internal error"}
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_api_events_stream_error_logs_and_closes(monkeypatch):
    ns = load_script()
    dash, srv, t = _boot(ns)

    # (a) detect a leaked non-disconnect traceback via handle_error.
    leaked = []
    orig_handle_error = srv.handle_error
    srv.handle_error = lambda req, addr: leaked.append(sys.exc_info()[0])

    # (b) capture the operator signal: the except-clause calls self.log_error.
    logged = []
    logged_evt = threading.Event()
    orig_log_error = dash.DashboardHTTPHandler.log_error

    def _rec_log_error(self, fmt, *args):
        if "stream failed" in fmt:
            logged.append(fmt % args if args else fmt)
            logged_evt.set()
        return orig_log_error(self, fmt, *args)

    monkeypatch.setattr(dash.DashboardHTTPHandler, "log_error", _rec_log_error)

    try:
        hub = dash.DashboardHTTPHandler.hub
        snap = ns["_empty_dashboard_snapshot"]()
        hub.publish(snap)  # seed so the first loop frame builds immediately

        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type").startswith("text/event-stream")

        # Read the first (GOOD) frame — synchronizes past the seeded envelope
        # build, so the following patch takes effect on the NEXT iteration.
        buf = b""
        deadline = time.monotonic() + 3.0
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = r.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        assert b"event: update" in buf, f"no first frame: {buf!r}"

        # Now break the NEXT envelope build mid-stream (headers already sent).
        monkeypatch.setattr(dash, "snapshot_to_envelope",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("boom mid-stream")))
        hub.publish(snap)

        # The except-clause must log + close (not crash / leak a traceback).
        assert logged_evt.wait(3.0), "stream-failed log_error never fired"
        assert any("stream failed" in m for m in logged)

        # The stream closes cleanly: read drains to EOF, no more frames.
        tail = b""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                chunk = r.fp.read1(4096)
            except (TimeoutError, OSError):
                break
            if not chunk:  # EOF — server closed the connection
                break
            tail += chunk
        # No non-disconnect traceback leaked to handle_error.
        assert leaked == [], f"traceback leaked to handle_error: {leaked}"
    finally:
        srv.handle_error = orig_handle_error
        srv.shutdown()
        t.join(timeout=2)
