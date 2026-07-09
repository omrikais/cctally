"""#279 S1 F3 — dashboard handler socket timeout + SSE timeout-as-disconnect.

`BaseHTTPRequestHandler.timeout` was never set, so a half-open request could
hold a handler thread forever (slow-loris). And the SSE loops caught only
BrokenPipeError/ConnectionResetError, NOT socket.timeout — with a handler
timeout now set, a stalled SSE send raises socket.timeout, which must be
treated as a client disconnect (loop exits cleanly), not an error/traceback.
"""
import socket
import sys

from conftest import load_script


def test_handler_timeout_set():
    load_script()                       # _cctally_dashboard imports need cctally
    import _cctally_dashboard as d
    assert d.DashboardHTTPHandler.timeout == 60


def test_sse_update_stream_timeout_is_disconnect(monkeypatch):
    """socket.timeout raised mid-SSE-write is swallowed as a disconnect: the
    handler method RETURNS instead of propagating the timeout."""
    load_script()                       # (re)bind sys.modules["cctally"]
    import _cctally_dashboard as d
    mod = sys.modules["cctally"]

    class _Worker:
        def stream(self, run_id):
            yield {"type": "message", "x": 1}

    monkeypatch.setattr(mod, "_UPDATE_WORKER", _Worker(), raising=False)

    class _W:
        def write(self, b):
            raise socket.timeout("timed out")

        def flush(self):
            pass

    h = object.__new__(d.DashboardHTTPHandler)
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.wfile = _W()

    # Must RETURN (timeout == disconnect), not raise socket.timeout.
    h._handle_get_update_stream("/api/update/stream/abc")
