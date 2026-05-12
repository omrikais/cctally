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
import sys
import threading

from conftest import load_script, redirect_paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_handler(ns, tmp_path, monkeypatch, *, runtime_bind="127.0.0.1"):
    """Boot a real DashboardHTTPHandler against a clean fixture HOME.

    Wires the class attrs `cmd_dashboard` would normally set, plus the
    new `cctally_host` attribute that `_handle_get_doctor` reads.
    """
    redirect_paths(ns, monkeypatch, tmp_path)
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
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, snap


def _read_first_sse_data_frame(response, *, deadline_s=2.0):
    """Pull bytes off the SSE socket until we see a complete event frame
    (terminated by `\\n\\n`), then return the parsed `data:` payload."""
    import time
    buf = b""
    deadline = time.monotonic() + deadline_s
    while b"\n\n" not in buf and time.monotonic() < deadline:
        try:
            chunk = response.fp.read1(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    text = buf.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no data frame in SSE buffer: {text!r}")


# ---------------------------------------------------------------------------
# GET /api/doctor
# ---------------------------------------------------------------------------

def test_api_doctor_get_returns_full_payload(tmp_path, monkeypatch):
    """`GET /api/doctor` returns the kernel-serialized JSON with the
    six spec'd category ids and schema_version=1."""
    ns = load_script()
    srv, _snap = _start_handler(ns, tmp_path, monkeypatch)
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/doctor")
        r = c.getresponse()
        assert r.status == 200, r.status
        assert r.getheader("Content-Type", "").startswith("application/json")
        payload = json.loads(r.read())
    finally:
        srv.shutdown()

    assert payload["schema_version"] == 1
    cats = {c["id"] for c in payload["categories"]}
    assert cats == {"install", "hooks", "auth", "db", "data", "safety"}
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
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        # No Origin header — same shape as `curl` from a terminal.
        c.request("GET", "/api/doctor")
        r = c.getresponse()
        assert r.status == 200, r.status
    finally:
        srv.shutdown()


def test_api_doctor_safety_dashboard_bind_runtime_override(tmp_path, monkeypatch):
    """Boot with `cctally_host = "0.0.0.0"` (LAN exposure) while
    `config.json` is absent (default = loopback). `safety.dashboard_bind`
    must WARN and surface `runtime_bind = "0.0.0.0"` in details."""
    ns = load_script()
    srv, _ = _start_handler(ns, tmp_path, monkeypatch, runtime_bind="0.0.0.0")
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/doctor")
        r = c.getresponse()
        payload = json.loads(r.read())
    finally:
        srv.shutdown()

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
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/api/doctor")
        payload = json.loads(c.getresponse().read())
    finally:
        srv.shutdown()

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
    try:
        # Seed one snapshot BEFORE the SSE client subscribes so the
        # initial frame path emits immediately (no 15s keep-alive wait).
        hub.publish(snap)

        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        payload = _read_first_sse_data_frame(r)
    finally:
        srv.shutdown()

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
    try:
        hub.publish(snap)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        payload_a = _read_first_sse_data_frame(r)
        hub.publish(snap)
        payload_b = _read_first_sse_data_frame(r)
    finally:
        srv.shutdown()

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
    try:
        hub.publish(snap)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        payload = _read_first_sse_data_frame(r)
    finally:
        srv.shutdown()

    # If the frame parsed via json.loads above, the dict is by definition
    # JSON-serializable. Re-stringify to confirm round-trip stability.
    json.dumps(payload["doctor"])
