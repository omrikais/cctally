"""Tests for the per-conversation live-tail SSE endpoint
``GET /api/conversation/<id>/events`` (conversation live-tail, spec §2).

Boots a real ``DashboardHTTPHandler`` against a fixture cache.db seeded from
REAL JSONL files on disk (the watch loop fast-polls + targeted-syncs the
session's actual files), then drives the SSE stream over a raw socket with the
``read1``/deadline pattern from ``tests/test_dashboard_api_events.py`` (a
Content-Length-less ``text/event-stream`` blocks on ``read(n)`` under Python
3.14, so we read whatever is buffered and stop at a deadline).

Covered: the fail-closed privacy gate (non-IP-literal Host → 403), tail-on-
growth (append a JSONL line → ``event: tail`` arrives + the new turn is
queryable), and ``--no-sync`` passivity (no ingest, no emit; data is frozen).
"""
import datetime as dt
import json
import pathlib
import socket as _socket
import socketserver
import sys
import threading
import time
from http.client import HTTPConnection

from conftest import load_script, redirect_paths

# A real model id from CLAUDE_MODEL_PRICING so token-derived cost is non-zero.
_MODEL = "claude-opus-4-8"


def _asst_line(uuid, msg_id, req_id, text, *, sid="s1",
               ts="2026-06-01T00:00:00Z", model=_MODEL, out_tokens=5):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": sid,
        "requestId": req_id, "timestamp": ts,
        "message": {"role": "assistant", "id": msg_id, "model": model,
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": out_tokens,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


def _make_snapshot(ns):
    DataSnapshot = ns["DataSnapshot"]
    return DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )


def _boot(ns, tmp_path, monkeypatch, *, bind="127.0.0.1", expose=False,
          no_sync=False):
    """Seed cache.db from a REAL JSONL file (session ``s1``) and start a
    server. Returns ``(srv, projects_dir, session_jsonl)``; caller must
    ``srv.shutdown()``."""
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    jsonl = projects / "s1.jsonl"
    jsonl.write_text(_asst_line("a1", "m1", "r1", "answer A", sid="s1"))

    # Full sync so conversation_messages + session_files are populated for s1
    # (the watch loop resolves the file set from conversation_messages, and
    # baselines `seen` from session_files).
    conn = ns["open_cache_db"]()
    try:
        ns["sync_cache"](conn, rebuild=True)
    finally:
        conn.close()

    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    HandlerCls.snapshot_ref = SnapshotRef(_make_snapshot(ns))
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = bind
    HandlerCls.cctally_expose_transcripts = expose
    HandlerCls.no_sync = no_sync

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, projects, jsonl


def _get(port, path, *, host=None):
    """Plain GET helper. With ``host`` set, send it as the literal Host header
    (skip_host=True) so the privacy gate sees a non-loopback authority."""
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


def _open_sse(port, path, *, timeout=10.0):
    """Open an SSE stream over a raw socket and return the connected socket.

    The stream is a long-lived ``text/event-stream`` with no Content-Length,
    so the caller reads frames via ``_read_event``.
    """
    s = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n"
    ).encode("utf-8")
    s.sendall(req)
    return s


def _read_event(s, *, marker="event: tail", deadline=8.0, allow_timeout=False):
    """Read from the SSE socket until a frame containing ``marker`` (terminated
    by a blank line) arrives, or until the deadline. Returns the matching frame
    text, or ``None`` when ``allow_timeout`` and nothing matched in time."""
    buf = b""
    end = dt.datetime.now() + dt.timedelta(seconds=deadline)
    while dt.datetime.now() < end:
        s.settimeout(0.5)
        try:
            chunk = s.recv(4096)
        except (_socket.timeout, TimeoutError):
            continue
        if not chunk:
            break
        buf += chunk
        text = buf.decode("utf-8", "replace")
        idx = text.find(marker)
        if idx == -1:
            continue
        rest = text[idx:]
        blank = rest.find("\n\n")
        if blank == -1:
            continue
        return rest[:blank]
    if allow_timeout:
        return None
    raise AssertionError(
        f"no SSE frame matching {marker!r} arrived within {deadline}s; "
        f"buffer={buf!r}")


def test_events_403_for_non_loopback_host(tmp_path, monkeypatch):
    """The events route is behind the same fail-closed privacy gate: a
    non-IP-literal Host header (DNS-rebinding shape) → 403."""
    ns = load_script()
    srv, _projects, _jsonl = _boot(ns, tmp_path, monkeypatch,
                                   bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        status, _ = _get(port, "/api/conversation/s1/events",
                         host="evil.example.com")
        assert status == 403
    finally:
        srv.shutdown()


def test_events_emits_tail_on_file_growth(tmp_path, monkeypatch):
    """Appending a new turn to the open session's JSONL pushes ``event: tail``,
    and the appended turn becomes queryable through the reader route."""
    ns = load_script()
    srv, _projects, jsonl = _boot(ns, tmp_path, monkeypatch,
                                  bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, "/api/conversation/s1/events")
        try:
            # Let the connect-ingest + the cache-cursor `seen` baseline settle
            # BEFORE the append, so the growth is detected by the steady-state
            # poll loop (a write landing mid-connect-ingest would be consumed by
            # it and baselined away — that catch-up path is the client's `open`
            # pollTail, not the `tail` ping this test asserts).
            time.sleep(1.0)
            # Append a new assistant turn carrying a distinctive token.
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(_asst_line("a2", "m2", "r2", "answer AA", sid="s1"))
            frame = _read_event(s, marker="event: tail", deadline=10.0)
            assert frame.startswith("event: tail")
            data_line = [ln for ln in frame.splitlines()
                         if ln.startswith("data: ")][0]
            assert json.loads(data_line[len("data: "):])["sessionId"] == "s1"
        finally:
            s.close()
        # The appended turn is now queryable (the targeted ingest applied it).
        status, body = _get(port, "/api/conversation/s1")
        assert status == 200, (status, body)
        items = json.loads(body)["items"]
        assert any("AA" in (it.get("text") or "") for it in items)
    finally:
        srv.shutdown()


def test_events_passive_under_no_sync(tmp_path, monkeypatch):
    """Under ``--no-sync`` the stream is frozen: appending to the JSONL must NOT
    produce an ``event: tail`` (no ingest, no emit — keep-alive only)."""
    ns = load_script()
    srv, _projects, jsonl = _boot(ns, tmp_path, monkeypatch,
                                  bind="127.0.0.1", expose=False, no_sync=True)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, "/api/conversation/s1/events")
        try:
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(_asst_line("a2", "m2", "r2", "answer AA", sid="s1"))
            frame = _read_event(s, marker="event: tail", deadline=3.0,
                                allow_timeout=True)
            assert frame is None
        finally:
            s.close()
    finally:
        srv.shutdown()
