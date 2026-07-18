"""#294 S7 B4 — qualified live-tail SSE for both providers (spec §5.2 / §5.4).

Sibling of ``tests/test_dashboard_conversation_events.py`` (which pins the BARE
legacy Claude stream, byte-identical). Here: the qualified (``v1.``) streams —
the neutral preflight answered as plain JSON before any SSE bytes, the
``conversationKey``-framed vocabulary for both providers, Codex tail-on-growth,
end-to-end child discovery through the directory frontier, passivity under
``--no-sync``, and the per-path-cursor emission contract (an unrelated
conversation's growth never emits).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import socket as _socket
import sys
import threading
import time
import urllib.parse as _u
from http.client import HTTPConnection

from conftest import load_script, redirect_paths

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_conversation_dispatch as disp  # noqa: E402
import _lib_codex_conversation_query as cq  # noqa: E402

CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
_MODEL = "claude-opus-4-8"

_APPEND_LINE = json.dumps({
    "payload": {"content": [{"text": "appended turn", "type": "output_text"}],
                "phase": "output", "role": "assistant", "type": "message"},
    "timestamp": "2026-07-15T13:00:00Z", "type": "response_item"}) + "\n"


def _asst_line(uuid, msg_id, req_id, text, *, sid="s1",
               ts="2026-06-01T00:00:00Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": sid,
        "requestId": req_id, "timestamp": ts,
        "message": {"role": "assistant", "id": msg_id, "model": _MODEL,
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


def _make_snapshot(ns):
    DataSnapshot = ns["DataSnapshot"]
    return DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[])


def _wire_handler(ns, *, no_sync=False, expose=False, bind="127.0.0.1"):
    HandlerCls = ns["DashboardHTTPHandler"]
    HandlerCls.snapshot_ref = ns["_SnapshotRef"](_make_snapshot(ns))
    HandlerCls.hub = ns["SSEHub"]()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = bind
    HandlerCls.cctally_expose_transcripts = expose
    HandlerCls.no_sync = no_sync
    import socketserver
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    srv.handle_error = lambda request, client_address: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _boot_codex(ns, tmp_path, monkeypatch, *, scenario="modern-full",
                no_sync=False, fast=False):
    """Seed a Codex conversation and start a dashboard. Returns
    ``(srv, provider_root, rollout, conversation_key)``."""
    redirect_paths(ns, monkeypatch, tmp_path)
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / f"{scenario}.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        key = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    srv = _wire_handler(ns, no_sync=no_sync)   # loads the dashboard + conv sibling
    if fast:
        _speed_up(monkeypatch)
    return srv, provider_root, rollout, key


def _speed_up(monkeypatch):
    """Shrink the live-tail cadence so the ~10-cycle child-discovery boundary
    fires quickly (real timing, just faster). Patches the shared module constants
    the handler reads at runtime — after the dashboard sibling is loaded."""
    conv = sys.modules["_cctally_dashboard_conversation"]
    monkeypatch.setattr(conv, "_LIVE_TAIL_POLL_INTERVAL", 0.15)
    monkeypatch.setattr(conv, "_LIVE_TAIL_DEBOUNCE", 0.05)
    monkeypatch.setattr(conv, "_LIVE_TAIL_FILE_RESET_EVERY", 2)


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
    ctype = r.getheader("Content-Type")
    c.close()
    return status, body, ctype


def _open_sse(port, path, *, timeout=15.0):
    s = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    s.sendall((
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n").encode("utf-8"))
    return s


def _read_event(s, *, marker="event: tail", deadline=10.0, allow_timeout=False):
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
    raise AssertionError(f"no SSE frame {marker!r} within {deadline}s; buf={buf!r}")


def _events_path(key):
    return f"/api/conversation/{_u.quote(key, safe='')}/events"


# ── preflight: JSON before any SSE bytes ────────────────────────────────────


def test_qualified_not_found_is_json_404_before_sse(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _rollout, _key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        # A well-formed-looking but unresolvable v1 key → neutral not_found.
        status, body, ctype = _get(port, "/api/conversation/v1.deadbeef/events")
        assert status == 404
        assert ctype and "application/json" in ctype     # JSON, not SSE
        assert json.loads(body)["status"] == "not_found"
    finally:
        srv.shutdown()


def test_qualified_normalization_pending_is_json_200(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    # Force the Codex normalization authority to read pending (migration 025
    # not yet stamped) — the shared module object the dispatch preflight calls.
    monkeypatch.setattr(cq, "codex_normalization_authoritative", lambda conn: False)
    try:
        port = srv.server_address[1]
        status, body, ctype = _get(port, _events_path(key))
        assert status == 200
        assert ctype and "application/json" in ctype     # JSON, NOT event-stream
        assert json.loads(body)["status"] == "normalization_pending"
    finally:
        srv.shutdown()


def test_privacy_gate_403_before_any_preflight(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, _body, _c = _get(port, _events_path(key), host="evil.example.com")
        assert status == 403
    finally:
        srv.shutdown()


# ── qualified frames carry conversationKey (both providers) ─────────────────


def test_codex_ready_and_tail_use_conversation_key(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            assert _read_event(s, marker="event: ready", deadline=8.0
                               ).startswith("event: ready")
            time.sleep(1.0)                     # let connect-ingest + baseline settle
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(_APPEND_LINE)
            frame = _read_event(s, marker="event: tail", deadline=10.0)
            data = json.loads([ln for ln in frame.splitlines()
                               if ln.startswith("data: ")][0][len("data: "):])
            assert data.get("conversationKey") == key   # qualified frame
            assert "sessionId" not in data               # never the bare field
        finally:
            s.close()
    finally:
        srv.shutdown()


def test_qualified_claude_key_speaks_conversation_key(tmp_path, monkeypatch):
    """A v1.claude key reuses the Claude mechanics internally but speaks the
    qualified conversationKey vocabulary (never sessionId)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True)
    jsonl = projects / "s1.jsonl"
    jsonl.write_text(_asst_line("a1", "m1", "r1", "hello", sid="s1"))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_cache"](conn, rebuild=True)
    finally:
        conn.close()
    srv = _wire_handler(ns)
    key = disp._mint_claude_conversation_key("s1")
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            _read_event(s, marker="event: ready", deadline=8.0)
            time.sleep(1.0)
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(_asst_line("a2", "m2", "r2", "world", sid="s1"))
            frame = _read_event(s, marker="event: tail", deadline=10.0)
            data = json.loads([ln for ln in frame.splitlines()
                               if ln.startswith("data: ")][0][len("data: "):])
            assert data.get("conversationKey") == key
            assert "sessionId" not in data
        finally:
            s.close()
    finally:
        srv.shutdown()


def test_codex_subsequent_growth_re_detected(tmp_path, monkeypatch):
    """Two appends → two tails: the watch advances its cursor only to the
    committed offset, so growth after an ingest is re-detected next cycle."""
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            _read_event(s, marker="event: ready", deadline=8.0)
            time.sleep(1.0)
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(_APPEND_LINE)
            _read_event(s, marker="event: tail", deadline=10.0)
            line2 = _APPEND_LINE.replace("appended turn", "second turn").replace(
                "13:00:00", "13:05:00")
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(line2)
            frame2 = _read_event(s, marker="event: tail", deadline=10.0)
            assert frame2.startswith("event: tail")
        finally:
            s.close()
    finally:
        srv.shutdown()


# ── passivity + unrelated-mutation isolation ────────────────────────────────


def test_codex_passive_under_no_sync(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch, no_sync=True)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            assert _read_event(s, marker="event: ready", deadline=3.0,
                               allow_timeout=True) is None   # passive: no ready
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(_APPEND_LINE)
            assert _read_event(s, marker="event: tail", deadline=3.0,
                               allow_timeout=True) is None   # frozen: no tail
        finally:
            s.close()
    finally:
        srv.shutdown()


def test_unrelated_conversation_growth_does_not_emit(tmp_path, monkeypatch):
    """Growing a DIFFERENT conversation's file must not emit on this stream —
    emission keys on the watched files' per-path cursors, never a global seq."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    provider_root = tmp_path / "provider"
    a = provider_root / "sessions" / "2026" / "07" / "15" / "a.jsonl"
    b = provider_root / "sessions" / "2026" / "07" / "16" / "b.jsonl"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "modern-full.jsonl", a)
    shutil.copyfile(CORPUS / "nested-parent.jsonl", b)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        key_a = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads "
            "WHERE source_path=?", (str(a),)).fetchone()[0]
    finally:
        conn.close()
    srv = _wire_handler(ns)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key_a))
        try:
            _read_event(s, marker="event: ready", deadline=8.0)
            time.sleep(1.0)
            with open(b, "a", encoding="utf-8") as fh:      # grow the UNRELATED file
                fh.write(_APPEND_LINE)
            assert _read_event(s, marker="event: tail", deadline=3.0,
                               allow_timeout=True) is None   # no emit for A
        finally:
            s.close()
    finally:
        srv.shutdown()


# ── child discovery end-to-end (directory frontier) ─────────────────────────


def test_codex_child_discovery_emits_tail(tmp_path, monkeypatch):
    """A brand-new child rollout dropped mid-watch is discovered by the frontier,
    ingested, joins the parent's file set, and pushes a tail."""
    ns = load_script()
    srv, provider_root, _rollout, parent_key = _boot_codex(
        ns, tmp_path, monkeypatch, scenario="nested-parent", fast=True)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(parent_key))
        try:
            _read_event(s, marker="event: ready", deadline=8.0)
            time.sleep(0.6)
            # Drop the child rollout under the same sessions tree (a new file no
            # table yet knows) — the frontier must find + ingest + widen + emit.
            child = provider_root / "sessions" / "2026" / "07" / "15" / "child.jsonl"
            shutil.copyfile(CORPUS / "nested-child.jsonl", child)
            frame = _read_event(s, marker="event: tail", deadline=12.0)
            assert frame.startswith("event: tail")
        finally:
            s.close()
        # The child was ingested and joined the parent's widened file set.
        conn = ns["open_cache_db"]()
        try:
            paths = set(cq.codex_conversation_source_paths(conn, parent_key))
        finally:
            conn.close()
        assert str(provider_root / "sessions" / "2026" / "07" / "15" /
                   "child.jsonl") in paths
    finally:
        srv.shutdown()


# ── dispatch-level preflight unit (all three statuses, no server) ────────────


def test_neutral_events_preflight_statuses(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    srv.shutdown()
    conn = ns["open_cache_db"]()
    try:
        ok = disp.neutral_events_preflight(conn, key)
        assert ok["status"] == "ok" and ok["source"] == "codex"
        nf = disp.neutral_events_preflight(conn, "v1.garbage")
        assert nf["status"] == "not_found"
        monkeypatch.setattr(cq, "codex_normalization_authoritative",
                            lambda c: False)
        pend = disp.neutral_events_preflight(conn, key)
        assert pend["status"] == "normalization_pending"
    finally:
        conn.close()
