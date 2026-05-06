"""Minimal SSE protocol test — we just need to prove one event makes it
through with the right headers and framing. Longer-running behavior
(keep-alives, disconnects) is manually verified.

Python 3.14 note: http.client.HTTPResponse.read(amt) on a response
without Content-Length (as any SSE stream lacks) blocks until EOF
rather than returning partial data at timeout. We therefore read
directly from the response's underlying buffered file via read1()
(which returns whatever bytes are already buffered rather than
blocking until the full request size is satisfied) until we have the
first complete event frame (terminated by `\n\n`).
"""
import http.client
import json
import threading
import time

from conftest import load_script


def test_events_headers_and_first_frame():
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref

    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]

    try:
        # Publish one snapshot BEFORE the client connects so the seeded-
        # event path kicks in on subscribe and we don't wait 15s for a
        # keep-alive.
        hub.publish(snap)

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type").startswith("text/event-stream")
        assert r.getheader("Cache-Control") == "no-cache"

        # Read until we see a full SSE frame (terminated by blank line).
        # read1() returns already-buffered bytes up to n rather than
        # blocking until n bytes are available — essential here because
        # after the first frame the socket idles until the next publish
        # or 15s keep-alive.
        buf = b""
        deadline = time.monotonic() + 2.0
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = r.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        raw = buf.decode("utf-8", errors="replace")
        assert "event: update" in raw, f"no event frame in {raw!r}"

        # The data: line contains valid JSON with the envelope shape.
        data_line = [ln for ln in raw.splitlines() if ln.startswith("data: ")][0]
        payload = json.loads(data_line[len("data: "):])
        assert "header" in payload
    finally:
        srv.shutdown()
        t.join(timeout=2)
