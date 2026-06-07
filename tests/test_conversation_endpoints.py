"""Tests for the conversation-viewer GET routes + the transcript gate (Plan 2,
Task 7).

Boots a real ``DashboardHTTPHandler`` against a fixture cache.db (seeded with
Plan 1's ``conversation_messages`` / ``session_entries``) and drives the three
routes plus the per-request ``transcriptsEnabled`` injection. Mirrors the
handler-boot pattern in ``tests/test_dashboard_api_block.py`` — ``load_script``
+ ``redirect_paths`` + a booted ``socketserver.ThreadingTCPServer``.

The gate (anti-DNS-rebinding) is exercised by sending an explicit ``Host``
header via ``HTTPConnection`` with ``skip_host=True``.
"""
import datetime as dt
import json
import pathlib
import socketserver
import sys
import threading
from http.client import HTTPConnection

from conftest import load_script, redirect_paths

# A real model id from CLAUDE_MODEL_PRICING so token-derived cost is non-zero.
_MODEL = "claude-opus-4-8"


def _seed_cache(ns):
    """Seed conversation_messages + session_entries into the redirected
    cache.db. Two sessions; s1 has an assistant turn with cost."""
    cache = ns["open_cache_db"]()
    msg_cols = (
        "session_id", "uuid", "parent_uuid", "source_path", "byte_offset",
        "timestamp_utc", "entry_type", "text", "blocks_json", "model",
        "msg_id", "req_id", "cwd", "git_branch", "is_sidechain",
    )

    def _msg(**kw):
        row = {k: kw.get(k) for k in msg_cols}
        row["blocks_json"] = kw.get("blocks_json", "[]")
        row["text"] = kw.get("text", "")
        row["is_sidechain"] = kw.get("is_sidechain", 0)
        cache.execute(
            "INSERT OR IGNORE INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain) VALUES(:session_id,:uuid,:parent_uuid,:source_path,"
            ":byte_offset,:timestamp_utc,:entry_type,:text,:blocks_json,:model,"
            ":msg_id,:req_id,:cwd,:git_branch,:is_sidechain)",
            row,
        )

    def _entry(*, source_path, line_offset, model, msg_id, req_id,
               inp=0, out=0, cc=0, cr=0):
        cache.execute(
            "INSERT OR IGNORE INTO session_entries "
            "(source_path,line_offset,timestamp_utc,model,msg_id,req_id,"
            " input_tokens,output_tokens,cache_create_tokens,cache_read_tokens)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (source_path, line_offset, "t", model, msg_id, req_id,
             inp, out, cc, cr),
        )

    # s1 — a human + an assistant turn carrying the searchable token.
    _msg(session_id="s1", uuid="h1", source_path="a.jsonl", byte_offset=0,
         timestamp_utc="2026-06-01T00:00:00Z", entry_type="human",
         text="hi", cwd="/home/u/proj", git_branch="main")
    _msg(session_id="s1", uuid="a1", source_path="a.jsonl", byte_offset=1,
         timestamp_utc="2026-06-01T00:00:05Z", entry_type="assistant",
         text="the token limit window resets every five hours", model=_MODEL,
         msg_id="m1", req_id="r1", cwd="/home/u/proj", git_branch="main")
    _entry(source_path="a.jsonl", line_offset=1, model=_MODEL,
           msg_id="m1", req_id="r1", inp=1000, out=500)
    # s2 — separate session, no token match.
    _msg(session_id="s2", uuid="h2", source_path="b.jsonl", byte_offset=0,
         timestamp_utc="2026-06-02T00:00:00Z", entry_type="human",
         text="how do I budget my weekly usage", cwd="/home/u/other")
    cache.commit()
    cache.close()


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


def _boot(ns, tmp_path, monkeypatch, *, bind="127.0.0.1", expose=False):
    """Seed the cache and start a server with the given bind/expose posture.

    Returns the running ThreadingTCPServer; caller must ``srv.shutdown()``.
    """
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)

    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    HandlerCls.snapshot_ref = SnapshotRef(_make_snapshot(ns))
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = bind
    HandlerCls.cctally_expose_transcripts = expose

    # Threading server (mirrors production's ThreadingHTTPServer) so a
    # long-lived SSE connection (`/api/events` blocks in a keep-alive loop)
    # does not wedge the single accept thread and starve later requests.
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _get(port, path, *, host=None):
    """GET helper. When ``host`` is given, send it as the literal Host header
    (skip_host=True) so the gate sees a non-loopback authority."""
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


def test_gate_blocks_lan_hostname(tmp_path, monkeypatch):
    """expose=False, loopback bind: a request arriving with a LAN *hostname*
    Host header is rejected with 403 (anti-DNS-rebinding)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/conversations",
                             host="machine.local:8789")
        assert status == 403, (status, body)
        payload = json.loads(body)
        assert "error" in payload
    finally:
        srv.shutdown()


def test_gate_blocks_lan_bind_without_expose(tmp_path, monkeypatch):
    """LAN bind (0.0.0.0) without the expose opt-in: even an IP-literal Host
    is rejected because the bind itself is not allowed to serve transcripts."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="0.0.0.0", expose=False)
    try:
        port = srv.server_address[1]
        status, _ = _get(port, "/api/conversations", host="192.168.0.9:8789")
        assert status == 403
    finally:
        srv.shutdown()


def test_conversations_route_returns_rail(tmp_path, monkeypatch):
    """Loopback Host → 200; body is the browse rail shape."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/conversations")
        assert status == 200, (status, body)
        payload = json.loads(body)
        assert "conversations" in payload and "page" in payload
        sids = [r["session_id"] for r in payload["conversations"]]
        assert set(sids) == {"s1", "s2"}
        s1 = next(r for r in payload["conversations"]
                  if r["session_id"] == "s1")
        assert s1["project_label"] == "proj"
        assert s1["cost_usd"] > 0
    finally:
        srv.shutdown()


def test_conversation_detail_and_search_routing(tmp_path, monkeypatch):
    """``/api/conversation/search?q=token`` routes to SEARCH (not the <id>
    reader); ``/api/conversation/s1`` routes to the reader."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # search route — must return the search payload shape (has "hits"),
        # NOT a reader payload, and NOT 404 (which the <id> reader would give
        # for a session literally named "search").
        status, body = _get(port, "/api/conversation/search?q=token")
        assert status == 200, (status, body)
        payload = json.loads(body)
        assert "hits" in payload and "mode" in payload
        assert len(payload["hits"]) == 1
        assert payload["hits"][0]["session_id"] == "s1"

        # reader route — known session id → 200 with the reader payload.
        status, body = _get(port, "/api/conversation/s1")
        assert status == 200, (status, body)
        reader = json.loads(body)
        assert "items" in reader and "page" in reader
        assert reader["session_id"] == "s1"

        # reader route — unknown session → 404.
        status, _ = _get(port, "/api/conversation/does-not-exist")
        assert status == 404
    finally:
        srv.shutdown()


def test_conversation_detail_pagination_threads_query(tmp_path, monkeypatch):
    """The reader's ``?after=``/``?limit=`` cursor must thread through the HTTP
    route. Regression: ``do_GET`` strips the query before dispatch, so the
    detail handler MUST read the raw ``self.path`` — else ``limit`` defaults to
    500 and every request re-serves the head (pagination dead). s1 has 2 items
    (human + assistant); ``limit=1`` proves the param was honored."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Page 1: limit=1 → exactly ONE item + a live cursor. (Old bug: limit
        # ignored → both items in one page, next_after None.)
        status, body = _get(port, "/api/conversation/s1?limit=1")
        assert status == 200, (status, body)
        page1 = json.loads(body)
        assert len(page1["items"]) == 1, page1
        assert page1["page"]["has_more"] is True
        cursor = page1["page"]["next_after"]
        assert cursor is not None
        first_id = page1["items"][0]["anchor"]["id"]

        # Page 2: after=<cursor> → the NEXT item, not the head again.
        status, body = _get(
            port, f"/api/conversation/s1?after={cursor}&limit=1")
        assert status == 200, (status, body)
        page2 = json.loads(body)
        assert len(page2["items"]) == 1, page2
        assert page2["items"][0]["anchor"]["id"] != first_id
        assert page2["page"]["has_more"] is False
    finally:
        srv.shutdown()


class _ExplodingQuery:
    """Stand-in conversation query kernel whose every method raises mid-query,
    modeling a `sqlite3.OperationalError` (lock past busy_timeout) /
    `DatabaseError` that fires AFTER `open_cache_db()` succeeds."""

    def _boom(self, *_a, **_k):
        raise __import__("sqlite3").OperationalError("database is locked")

    list_conversations = _boom
    get_conversation = _boom
    search_conversations = _boom


def test_kernel_exception_returns_clean_500(tmp_path, monkeypatch):
    """A kernel exception DURING the query (not at open_cache_db) must surface
    as a clean HTTP 500 with a JSON ``{"error": ...}`` body — NOT a hung/reset
    socket (no status line), NOT a 200. Without the per-handler
    ``except Exception`` the exception propagates out of ``do_GET`` and the
    client sees a connection reset; this proves the wrap is non-vacuous.

    Covers all three handlers (list / reader / search), each of which has its
    own kernel call site.
    """
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        # Force every conversation handler down its kernel-exception path.
        monkeypatch.setattr(
            ns["DashboardHTTPHandler"], "_conversation_query",
            staticmethod(lambda: _ExplodingQuery()),
        )
        for route in ("/api/conversations",
                      "/api/conversation/s1",
                      "/api/conversation/search?q=token"):
            status, body = _get(port, route)
            assert status == 500, (route, status, body)
            payload = json.loads(body)
            assert "error" in payload, (route, payload)
    finally:
        srv.shutdown()


def test_cache_open_failure_returns_500(tmp_path, monkeypatch):
    """A failure at ``open_cache_db()`` itself (BEFORE the query) must surface
    as a clean HTTP 500 with ``{"error": "cache unavailable: ..."}`` on all
    three conversation routes. Distinct from ``test_kernel_exception_…``: this
    fires at connection time, not mid-query, exercising the FIRST try/except of
    the shared scaffold. Characterizes the open-failure branch the #151
    scaffold-collapse must preserve byte-for-byte (without the
    ``except (DatabaseError, OSError)`` the OSError propagates out of
    ``do_GET`` and the client sees a reset, not a 500 — so this is
    non-vacuous)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        import _cctally_dashboard as _dash

        def _boom():
            raise OSError("disk gone")

        # Patch the binding the handler resolves at request time
        # (LOAD_GLOBAL in _cctally_dashboard's namespace). Seeding already
        # happened in _boot via ns["open_cache_db"], so this only affects
        # the live request path.
        monkeypatch.setattr(_dash, "open_cache_db", _boom)
        for route in ("/api/conversations",
                      "/api/conversation/s1",
                      "/api/conversation/search?q=token"):
            status, body = _get(port, route)
            assert status == 500, (route, status, body)
            payload = json.loads(body)
            assert payload.get("error", "").startswith("cache unavailable:"), \
                (route, payload)
    finally:
        srv.shutdown()


def test_api_data_transcripts_enabled_is_host_aware(tmp_path, monkeypatch):
    """``/api/data.transcriptsEnabled`` is computed per-request from the Host
    header: loopback → True; LAN hostname + expose=False → False."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Loopback request → enabled.
        status, body = _get(port, "/api/data")
        assert status == 200, (status, body)
        assert json.loads(body)["transcriptsEnabled"] is True

        # LAN hostname Host, expose off → disabled (never enabled-then-403).
        status, body = _get(port, "/api/data", host="machine.local:8789")
        assert status == 200, (status, body)
        assert json.loads(body)["transcriptsEnabled"] is False
    finally:
        srv.shutdown()


def _first_sse_update_envelope(port, *, host=None, timeout=5.0):
    """Open ``GET /api/events``, publish a snapshot, and return the parsed
    JSON envelope from the first ``event: update`` block on the stream.

    The SSE stream is a long-lived ``text/event-stream`` response, so we
    drive it over a raw socket and parse the first ``event: update``/``data:``
    pair. The caller is expected to have published a snapshot via
    ``HandlerCls.hub.publish(...)`` (so the subscriber's queue has a frame).
    """
    import socket as _socket
    s = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.settimeout(timeout)
        authority = host if host is not None else f"127.0.0.1:{port}"
        req = (
            f"GET /api/events HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"Connection: keep-alive\r\n\r\n"
        ).encode("utf-8")
        s.sendall(req)

        # Read until we see a full `event: update\ndata: {...}\n\n` block.
        buf = b""
        deadline = dt.datetime.now() + dt.timedelta(seconds=timeout)
        while dt.datetime.now() < deadline:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            text = buf.decode("utf-8", "replace")
            marker = "event: update\n"
            idx = text.find(marker)
            if idx == -1:
                continue
            rest = text[idx + len(marker):]
            # The data line follows immediately; block ends at the blank line.
            end = rest.find("\n\n")
            if end == -1:
                continue
            block = rest[:end]
            for line in block.split("\n"):
                if line.startswith("data: "):
                    return json.loads(line[len("data: "):])
        raise AssertionError(
            "no `event: update` SSE block arrived within the timeout; "
            f"buffer={buf!r}"
        )
    finally:
        s.close()


def test_sse_update_envelope_carries_transcripts_enabled(tmp_path, monkeypatch):
    """The SSE ``update`` envelope (``/api/events``) MUST carry
    ``transcriptsEnabled`` equal to the per-connection gate value — the same
    contract as ``/api/data``.

    The client replaces the whole snapshot on every SSE tick, so if the
    envelope omits this field the steady-state UI loses the gate (the
    ViewSwitcher disappears ~15s after bootstrap). Loopback → True; LAN
    hostname + expose=False → False (never enabled-then-403)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        HandlerCls = ns["DashboardHTTPHandler"]

        # Publish a frame so each fresh SSE subscriber gets an immediate tick.
        HandlerCls.hub.publish(_make_snapshot(ns))

        # Loopback connection → gate True.
        env = _first_sse_update_envelope(port)
        assert "transcriptsEnabled" in env, env
        assert env["transcriptsEnabled"] is True

        # LAN hostname Host, expose off → gate False (mirrors /api/data).
        env = _first_sse_update_envelope(port, host="machine.local:8789")
        assert "transcriptsEnabled" in env, env
        assert env["transcriptsEnabled"] is False
    finally:
        srv.shutdown()
