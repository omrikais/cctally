"""Integration tests for the dashboard's doctor surface (Task 16):

  * SSE snapshot envelope carries a `doctor` aggregate block per tick
    (spec §5.5 — severity + counts + Z-suffix generated_at + sha1: fp).
  * `GET /api/doctor` returns the full kernel-serialized JSON report
    on demand (spec §5.6).
  * Runtime-bind override propagates from cmd_dashboard (`args.host`)
    into `doctor_gather_state` so `safety.dashboard_bind` reflects
    what the process is ACTUALLY bound to, not the config-only view
    the CLI sees (Codex H4).
  * The fingerprint is stable across ticks when the severity tree
    hasn't shifted — even though `generated_at` drifts each tick.

Mirrors the in-process pattern used by tests/test_dashboard_api_data.py
and tests/test_dashboard_api_block.py (boot a TCPServer thread, fire
http.client requests, parse JSON), rather than the slower subprocess
pattern. `redirect_paths` pins HOME so doctor_gather_state never
touches the developer's real ~/.local/share/cctally.
"""
import datetime as dt
import http.client
import json
import pathlib
import socket
import sys
import threading

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, shorten_sse_keepalive, start, stop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_handler(ns, tmp_path, monkeypatch, *, runtime_bind="127.0.0.1"):
    """Boot a real DashboardHTTPHandler against a clean fixture HOME.

    Wires the class attrs `cmd_dashboard` would normally set, plus the
    new `cctally_host` attribute that `_handle_get_doctor` reads.
    """
    redirect_paths(ns, monkeypatch, tmp_path)
    # #630 S2: the /api/events handler blocks in `q.get(timeout=...)` and only
    # learns its client has gone on the keep-alive write after that timeout —
    # and on a socket the peer has closed the FIRST write still succeeds. At
    # the shipped period that is two full timeouts, which is the whole
    # presence backstop, so stop() could not reap the handler at all.
    shorten_sse_keepalive(ns, monkeypatch)
    # Allow `import _lib_doctor` to resolve from bin/ (matches what
    # cmd_dashboard's import path does in the real process).
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    snap = ns["_empty_dashboard_snapshot"]()
    HandlerCls.snapshot_ref = SnapshotRef(snap)
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = runtime_bind

    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), HandlerCls)
    srv._test_thread = start(srv)
    return srv, snap


def _first_data_line(buf):
    """The first `data:` line inside a COMPLETE frame of `buf`, or `None`.

    Only complete frames are scanned. Splitting on the frame terminator and
    dropping the last part is what excludes a partial trailing frame, and it is
    also what skips a `:`-prefixed keep-alive comment frame without mistaking
    its terminator for the data frame's.
    """
    text = buf.decode("utf-8", errors="ignore")
    for frame in text.split("\n\n")[:-1]:
        for line in frame.splitlines():
            if line.startswith("data: "):
                return line[len("data: "):]
    return None


def _read_first_sse_data_frame(response, *, deadline_s=2.0):
    """Pull bytes off the SSE socket until a complete frame carrying a `data:`
    line arrives, then return that payload parsed.

    #630 S2 shortens `_SSE_KEEPALIVE_SECONDS` to 0.5 s in this file, which turns
    an interleaved `b": keep-alive\\n\\n"` comment frame from a theoretical
    possibility into a real one. That comment frame is a complete
    `\\n\\n`-terminated frame with no `data:` line, so stopping at the first
    `\\n\\n` raised "no data frame in SSE buffer" on a perfectly healthy stream.
    tests/test_dashboard_api_events.py carries the same guard for the same
    reason.
    """
    import time
    buf = b""
    deadline = time.monotonic() + deadline_s
    while True:
        line = _first_data_line(buf)
        if line is not None:
            return json.loads(line)
        if time.monotonic() >= deadline:
            break
        try:
            chunk = response.fp.read1(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    line = _first_data_line(buf)
    if line is not None:
        return json.loads(line)
    raise AssertionError(
        f"no data frame in SSE buffer: "
        f"{buf.decode('utf-8', errors='ignore')!r}")


class _ScriptedSSEStream:
    """A stand-in response whose `fp.read1` hands back pre-baked SSE chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.fp = self

    def read1(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


def test_read_first_sse_data_frame_reads_past_an_interleaved_keep_alive():
    """A `: keep-alive` comment frame must not be mistaken for the data frame.

    #630 S2 shortens `_SSE_KEEPALIVE_SECONDS` to 0.5 s in this file so an
    abandoned `/api/events` handler notices its closed client inside `stop()`'s
    backstop. The handler writes `b": keep-alive\\n\\n"`, which is a complete
    `\\n\\n`-terminated frame carrying no `data:` line, so stopping at the first
    `\\n\\n` reports "no data frame in SSE buffer" for a healthy stream.
    tests/test_dashboard_api_events.py carries the same guard for the same
    reason.
    """
    stream = _ScriptedSSEStream([b": keep-alive\n\n", b'data: {"ok": true}\n\n'])
    assert _read_first_sse_data_frame(stream) == {"ok": True}


def _raw_get(port, path):
    """Return every byte written for one close-delimited HTTP request."""
    chunks = []
    sock = socket.create_connection(("127.0.0.1", port), timeout=PRESENCE_BACKSTOP_SECONDS)
    try:
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        while True:
            block = sock.recv(65536)
            if not block:
                break
            chunks.append(block)
    except OSError:
        pass
    finally:
        sock.close()
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# GET /api/doctor
# ---------------------------------------------------------------------------

def test_api_doctor_get_returns_full_payload(tmp_path, monkeypatch):
    """`GET /api/doctor` returns the kernel-serialized JSON with the
    ten spec'd category ids and schema_version=1."""
    ns = load_script()
    srv, _snap = _start_handler(ns, tmp_path, monkeypatch)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/api/doctor")
        r = c.getresponse()
        assert r.status == 200, r.status
        assert r.getheader("Content-Type", "").startswith("application/json")
        payload = json.loads(r.read())
    finally:
        stop(srv, srv._test_thread)

    assert payload["schema_version"] == 1
    cats = {c["id"] for c in payload["categories"]}
    assert cats == {
        "install", "hooks", "auth", "db", "journal", "data", "accounts",
        "pricing", "quota", "safety", "telemetry",
    }
    assert set(payload["overall"]["counts"].keys()) == {"ok", "warn", "fail"}
    assert payload["overall"]["severity"] in {"ok", "warn", "fail"}
    assert payload["generated_at"].endswith("Z")


def test_api_doctor_no_csrf_required(tmp_path, monkeypatch):
    """GETs are read-only; loopback bind is the protection. `_handle_get_doctor`
    must NOT route through `_check_origin_csrf` (which would 403 a vanilla
    HTTP/1.1 client that doesn't send an Origin header)."""
    ns = load_script()
    srv, _ = _start_handler(ns, tmp_path, monkeypatch)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        # No Origin header — same shape as `curl` from a terminal.
        c.request("GET", "/api/doctor")
        r = c.getresponse()
        assert r.status == 200, r.status
    finally:
        stop(srv, srv._test_thread)


def test_api_doctor_preparation_failure_returns_one_json_500(
        tmp_path, monkeypatch):
    """A failure before headers are sent still has a usable status channel."""
    ns = load_script()
    monkeypatch.setitem(
        ns, "doctor_gather_state",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("prepare boom")),
    )
    srv, _ = _start_handler(ns, tmp_path, monkeypatch)
    try:
        raw = _raw_get(srv.server_address[1], "/api/doctor")
    finally:
        stop(srv, srv._test_thread)

    assert raw.count(b"HTTP/1.") == 1, raw[:400]
    assert raw.split(b"\r\n", 1)[0].split()[1:2] == [b"500"], raw[:200]
    assert b'"error"' in raw


def test_api_doctor_committed_write_failure_never_appends_a_second_response(
        tmp_path, monkeypatch):
    """A partial doctor body cannot be followed by a second HTTP response."""
    ns = load_script()
    handler = ns["DashboardHTTPHandler"]
    real_doctor = handler._handle_get_doctor
    fired = []

    def fail_first_body_write(self):
        real_write = self.wfile.write

        def write(data):
            if data[:1] == b"{" and not fired:
                fired.append(bytes(data))
                raise BrokenPipeError("simulated peer gone mid-body")
            return real_write(data)

        self.wfile.write = write
        try:
            return real_doctor(self)
        finally:
            self.wfile.write = real_write

    monkeypatch.setattr(handler, "_handle_get_doctor", fail_first_body_write)
    srv, _ = _start_handler(ns, tmp_path, monkeypatch)
    try:
        raw = _raw_get(srv.server_address[1], "/api/doctor")
    finally:
        stop(srv, srv._test_thread)

    assert fired, "the injected doctor body failure never fired"
    assert raw.split(b"\r\n", 1)[0].split()[1:2] == [b"200"], raw[:200]
    assert raw.count(b"HTTP/1.") == 1, (
        "a second HTTP response followed the committed doctor response: %r"
        % (raw[:400],)
    )


def test_api_doctor_safety_dashboard_bind_runtime_override(tmp_path, monkeypatch):
    """Boot with `cctally_host = "0.0.0.0"` (LAN exposure) while
    `config.json` is absent (default = loopback). `safety.dashboard_bind`
    must WARN and surface `runtime_bind = "0.0.0.0"` in details."""
    ns = load_script()
    srv, _ = _start_handler(ns, tmp_path, monkeypatch, runtime_bind="0.0.0.0")
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/api/doctor")
        r = c.getresponse()
        payload = json.loads(r.read())
    finally:
        stop(srv, srv._test_thread)

    safety = next(c for c in payload["categories"] if c["id"] == "safety")
    bind_chk = next(c for c in safety["checks"] if c["id"] == "safety.dashboard_bind")
    assert bind_chk["severity"] == "warn"
    assert bind_chk["details"]["runtime_bind"] == "0.0.0.0"


def test_api_doctor_runtime_bind_loopback_stays_ok(tmp_path, monkeypatch):
    """Mirror of the WARN test: when `cctally_host` is loopback (the
    default), safety.dashboard_bind stays OK and reports the loopback
    runtime override."""
    ns = load_script()
    srv, _ = _start_handler(ns, tmp_path, monkeypatch, runtime_bind="127.0.0.1")
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/api/doctor")
        payload = json.loads(c.getresponse().read())
    finally:
        stop(srv, srv._test_thread)

    safety = next(c for c in payload["categories"] if c["id"] == "safety")
    bind_chk = next(c for c in safety["checks"] if c["id"] == "safety.dashboard_bind")
    assert bind_chk["severity"] == "ok"
    assert bind_chk["details"]["runtime_bind"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# SSE envelope
# ---------------------------------------------------------------------------

def test_sse_envelope_includes_doctor_block(tmp_path, monkeypatch):
    """The SSE snapshot envelope grows a `doctor` aggregate block
    (severity + counts + generated_at + fingerprint) so the dashboard
    can render a status chip without a separate /api/doctor fetch."""
    ns = load_script()
    srv, snap = _start_handler(ns, tmp_path, monkeypatch)
    hub = ns["DashboardHTTPHandler"].hub
    c = r = None
    try:
        # Seed one snapshot BEFORE the SSE client subscribes so the
        # initial frame path emits immediately (no keep-alive wait).
        hub.publish(snap)

        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        payload = _read_first_sse_data_frame(r)
    finally:
        stop(srv, srv._test_thread,
             connections=[x for x in (c, r) if x is not None])

    assert "doctor" in payload, "envelope missing `doctor` block"
    doc = payload["doctor"]
    assert {"severity", "counts", "generated_at", "fingerprint"} <= set(doc.keys())
    assert doc["severity"] in {"ok", "warn", "fail"}
    assert set(doc["counts"].keys()) == {"ok", "warn", "fail"}
    assert doc["generated_at"].endswith("Z")
    assert doc["fingerprint"].startswith("sha1:")
    # SHA1 hex digest is 40 chars after the prefix.
    assert len(doc["fingerprint"]) == len("sha1:") + 40


def test_sse_doctor_fingerprint_stable_across_age_drift(tmp_path, monkeypatch):
    """Identity slice (severity tree shape) is stable across two ticks
    on the same fixture HOME — even though `generated_at` advances.
    Re-publishing the same snapshot triggers a second SSE frame; both
    should carry the SAME fingerprint."""
    ns = load_script()
    srv, snap = _start_handler(ns, tmp_path, monkeypatch)
    hub = ns["DashboardHTTPHandler"].hub
    c = r = None
    try:
        hub.publish(snap)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/api/events")
        r = c.getresponse()
        payload_a = _read_first_sse_data_frame(r)
        hub.publish(snap)
        payload_b = _read_first_sse_data_frame(r)
    finally:
        stop(srv, srv._test_thread,
             connections=[x for x in (c, r) if x is not None])

    fp_a = payload_a["doctor"]["fingerprint"]
    fp_b = payload_b["doctor"]["fingerprint"]
    assert fp_a == fp_b, (
        f"fingerprint drifted across ticks despite no state change: "
        f"{fp_a!r} → {fp_b!r}"
    )


def test_sse_envelope_doctor_block_serializes_as_json(tmp_path, monkeypatch):
    """Hard guard: the doctor block must json-serialize without raising
    so the SSE pipeline never crashes mid-stream on a bad value type
    (e.g., a datetime that slipped through the kernel's `_iso_z`)."""
    ns = load_script()
    srv, snap = _start_handler(ns, tmp_path, monkeypatch)
    hub = ns["DashboardHTTPHandler"].hub
    c = r = None
    try:
        hub.publish(snap)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/api/events")
        r = c.getresponse()
        payload = _read_first_sse_data_frame(r)
    finally:
        stop(srv, srv._test_thread,
             connections=[x for x in (c, r) if x is not None])

    # If the frame parsed via json.loads above, the dict is by definition
    # JSON-serializable. Re-stringify to confirm round-trip stability.
    json.dumps(payload["doctor"])
