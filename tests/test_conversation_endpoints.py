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

    # --- 1A: cache-rebuild fixtures (sess-clean, sess-rebuild) -------------
    # Two sessions whose assistant turns drive _stamp_cache_failures. The
    # kernel keys a running-max of cache_read on (subagent_key, model) and
    # flags a turn iff rm >= _CACHE_FAILURE_CACHE_FLOOR (20_000) AND
    # cc >= _CACHE_FAILURE_CREATE_FLOOR (20_000) AND cr <= 0.5*rm AND
    # cc/(cc+cr) >= 0.75 (the REAL thresholds — the plan's illustrative
    # 200/2000/4000 values are an order of magnitude too small to ever trip
    # the 20_000 floors). Both sessions are main-session (non-agent
    # source_path -> subagent_key None) and single-model so the key is
    # constant across turns.

    # sess-clean: no turn ever recreates -> ZERO flagged turns.
    #   turn1 cr=0     cc=2000   -> rm=0,     no flag; running_max -> 0
    #   turn2 cr=40000 cc=2000   -> rm=0,     no flag; running_max -> 40000
    #   turn3 cr=40000 cc=2000   -> rm=40000 but cc=2000 < CREATE_FLOOR -> no flag
    _clean_turns = [
        ("cu1", "cm1", "cr1", 0, 2000),
        ("cu2", "cm2", "cr2", 40000, 2000),
        ("cu3", "cm3", "cr3", 40000, 2000),
    ]
    for i, (uuid, mid, rid, cr, cc) in enumerate(_clean_turns):
        _msg(session_id="sess-clean", uuid=uuid, source_path="clean.jsonl",
             byte_offset=i, timestamp_utc=f"2026-06-03T00:00:0{i}Z",
             entry_type="assistant", text=f"clean turn {i}", model=_MODEL,
             msg_id=mid, req_id=rid, cwd="/home/u/clean", git_branch="main")
        _entry(source_path="clean.jsonl", line_offset=i, model=_MODEL,
               msg_id=mid, req_id=rid, inp=500, out=200, cc=cc, cr=cr)

    # sess-rebuild: turn2 recreates its prefix -> EXACTLY ONE flagged turn.
    #   turn1 cr=40000 cc=5000   -> rm=0,     no flag; running_max -> 40000
    #   turn2 cr=2000  cc=30000  -> rm=40000 (>=20000), cc=30000 (>=20000),
    #          cr=2000 <= 0.5*40000=20000, cc/(cc+cr)=0.9375 >= 0.75 -> FLAG
    _rebuild_turns = [
        ("ru1", "rm1", "rr1", 40000, 5000),
        ("ru2", "rm2", "rr2", 2000, 30000),
    ]
    for i, (uuid, mid, rid, cr, cc) in enumerate(_rebuild_turns):
        _msg(session_id="sess-rebuild", uuid=uuid, source_path="rebuild.jsonl",
             byte_offset=i, timestamp_utc=f"2026-06-04T00:00:0{i}Z",
             entry_type="assistant", text=f"rebuild turn {i}", model=_MODEL,
             msg_id=mid, req_id=rid, cwd="/home/u/rebuild", git_branch="main")
        _entry(source_path="rebuild.jsonl", line_offset=i, model=_MODEL,
               msg_id=mid, req_id=rid, inp=500, out=200, cc=cc, cr=cr)

    # Populate the browse-rail rollup from the seeded messages (full recompute;
    # no backfill flag armed) so the booted handler's /api/conversations read
    # exercises the FAST rollup path. sync_cache does this in production, but
    # this test direct-seeds and never runs it. The bin/ dir is on sys.path by
    # the time _boot calls _seed_cache, so the import resolves.
    import _cctally_cache as _cc
    _cc._recompute_conversation_sessions(cache)
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


def _get_ct(port, path, *, host=None):
    """GET helper returning ``(status, content_type, body)`` — for routes whose
    Content-Type matters (e.g. the export route's ``text/markdown``)."""
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
    ct = r.headers.get("Content-Type")
    c.close()
    return status, ct, body


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
        # The seeder also stages sess-clean / sess-rebuild (the cache-rebuild
        # fixtures, 1A); the rail returns every non-null session.
        assert set(sids) == {"s1", "s2", "sess-clean", "sess-rebuild"}
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


def test_conversation_outline_route(tmp_path, monkeypatch):
    """``/api/conversation/<sid>/outline`` (#177 S5): loopback 200 with the
    outline shape (``session_id``/``stats``/``turns``), 404 on an unknown id,
    403 under a LAN hostname Host (gate reused), and route-ordering proof that
    the ``/outline`` suffix dispatches to the outline handler — NOT the detail
    catch-all parsing ``s1/outline`` as a session id.
    """
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Happy path: 200 with the outline body shape.
        status, body = _get(port, "/api/conversation/s1/outline")
        assert status == 200, (status, body)
        outline = json.loads(body)
        assert outline["session_id"] == "s1"
        assert "stats" in outline and "turns" in outline
        # Precedence: this is the outline handler (has "turns"), not the detail
        # catch-all (which carries "items"/"page" and would 404 on "s1/outline").
        assert "turns" in outline and "items" not in outline
        assert isinstance(outline["turns"], list) and outline["turns"]

        # Unknown session → 404.
        status, _ = _get(port, "/api/conversation/does-not-exist/outline")
        assert status == 404

        # Privacy gate reused verbatim: LAN hostname + expose=False → 403.
        status, _ = _get(port, "/api/conversation/s1/outline",
                         host="machine.local:8789")
        assert status == 403
    finally:
        srv.shutdown()


def test_conversation_export_route(tmp_path, monkeypatch):
    """``/api/conversation/<sid>/export?scope=<all|prompts|chat|recipe>`` (#217 S5
    F1/F5): loopback 200 ``text/markdown`` with a non-empty body for a valid
    scope; unknown scope → 400 (validated in the handler, NOT a 500); unknown
    session → 404; LAN hostname + expose=False → 403 (the same fail-closed gate
    as the sibling reader routes — Codex P0-1, no ``_check_origin_csrf``).
    """
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Happy path: 200, text/markdown, non-empty body, for every scope.
        for scope in ("all", "prompts", "chat", "recipe"):
            status, ct, body = _get_ct(
                port, f"/api/conversation/s1/export?scope={scope}")
            assert status == 200, (scope, status, body)
            assert ct is not None and ct.startswith("text/markdown"), (scope, ct)
            assert body, (scope, "empty body")

        # Default scope (no query string) → 200 (defaults to `all`).
        status, ct, body = _get_ct(port, "/api/conversation/s1/export")
        assert status == 200 and ct.startswith("text/markdown"), (status, ct)

        # Unknown scope → 400 (handler-level validation, BEFORE the kernel).
        status, _, _ = _get_ct(port, "/api/conversation/s1/export?scope=bogus")
        assert status == 400, status

        # Unknown session → 404.
        status, _, _ = _get_ct(
            port, "/api/conversation/does-not-exist/export?scope=all")
        assert status == 404, status

        # Privacy gate reused verbatim: LAN hostname + expose=False → 403.
        status, _, _ = _get_ct(port, "/api/conversation/s1/export?scope=all",
                               host="machine.local:8789")
        assert status == 403, status
    finally:
        srv.shutdown()


def test_conversation_prompts_route(tmp_path, monkeypatch):
    """``/api/conversation/<sid>/prompts`` (#217 S7 F10): loopback 200 with the
    prompt-spine shape (``session_id``/``prompts`` of ``{uuid,text}``), 404 on an
    unknown id, 403 under a LAN hostname Host (gate reused), and route-ordering
    proof that the ``/prompts`` suffix dispatches to the prompts handler — NOT
    the detail catch-all parsing ``s1/prompts`` as a session id.
    """
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Happy path: 200 with the prompt-spine body shape (s1 has one main
        # human prompt "hi"; the assistant turn is not a human prompt).
        status, body = _get(port, "/api/conversation/s1/prompts")
        assert status == 200, (status, body)
        payload = json.loads(body)
        assert payload["session_id"] == "s1"
        assert "prompts" in payload and isinstance(payload["prompts"], list)
        # Precedence: this is the prompts handler (has "prompts"), not the detail
        # catch-all (which carries "items"/"page") and not the outline handler
        # (which carries "turns"/"stats"); both would mis-handle "s1/prompts".
        assert "items" not in payload and "turns" not in payload
        assert [p["text"] for p in payload["prompts"]] == ["hi"]
        assert all(p["uuid"] for p in payload["prompts"])

        # Unknown session → 404.
        status, _ = _get(port, "/api/conversation/does-not-exist/prompts")
        assert status == 404

        # Privacy gate reused verbatim: LAN hostname + expose=False → 403.
        status, _ = _get(port, "/api/conversation/s1/prompts",
                         host="machine.local:8789")
        assert status == 403
    finally:
        srv.shutdown()


def test_conversation_find_route(tmp_path, monkeypatch):
    """``/api/conversation/<sid>/find`` (#177 S6): loopback 200 with the anchor
    shape (``anchors``/``total``/``mode``/``search_depth``), 404 on an unknown
    id, 403 under a LAN hostname Host (gate reused), and route-ordering proof
    that ``/find`` dispatches to the find handler — NOT the detail catch-all
    parsing ``s1/find`` as a session id."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Happy path: s1's assistant turn matches the prose 'token'.
        status, body = _get(port, "/api/conversation/s1/find?q=token")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert "anchors" in out and "total" in out and "mode" in out
        assert out["search_depth"] == "full"
        assert isinstance(out["anchors"], list) and out["total"] >= 1
        # Precedence: find handler (has "anchors"), not the detail catch-all
        # (which carries "items"/"page" and would 404 on "s1/find").
        assert "items" not in out

        # Unknown session → 404.
        status, _ = _get(port, "/api/conversation/does-not-exist/find?q=token")
        assert status == 404

        # Invalid kind → 400.
        status, body = _get(port, "/api/conversation/s1/find?q=token&kind=bogus")
        assert status == 400, (status, body)
        assert "error" in json.loads(body)

        # Privacy gate reused verbatim: LAN hostname + expose=False → 403.
        status, _ = _get(port, "/api/conversation/s1/find?q=token",
                         host="machine.local:8789")
        assert status == 403
    finally:
        srv.shutdown()


def test_conversation_find_regex_case_params(tmp_path, monkeypatch):
    """``/api/conversation/<sid>/find`` (#217 S4 / I-1.2): ``regex``/``case``
    truthy params thread into the kernel and surface ``mode``; an invalid regex
    is PRE-VALIDATED in the handler → 400 (NOT a 500, which the generic
    ``_run_conversation_query`` envelope would otherwise produce);
    ``kind=title``/``kind=files`` still → 400."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # regex=1: a regex pattern matches s1's prose; mode == "regex".
        status, body = _get(port, "/api/conversation/s1/find?q=tok.n&regex=1")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["mode"] == "regex" and out["search_depth"] == "full"
        assert out["total"] >= 1

        # case=1 (no regex): case-sensitive substring; mode == "like".
        status, body = _get(port, "/api/conversation/s1/find?q=token&case=1")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["mode"] == "like" and out["search_depth"] == "full"

        # Invalid regex → 400 with an error body (pre-validated, NOT 500).
        status, body = _get(
            port, "/api/conversation/s1/find?q=%28&regex=1")  # q="(" unbalanced
        assert status == 400, (status, body)
        err = json.loads(body)
        assert "error" in err and "invalid regex" in err["error"]

        # An invalid regex WITHOUT the regex flag is a literal substring → 200.
        status, _ = _get(port, "/api/conversation/s1/find?q=%28")
        assert status == 200

        # kind=title / kind=files still → 400 (find excludes the search facets).
        for k in ("title", "files"):
            status, _ = _get(port, f"/api/conversation/s1/find?q=token&kind={k}")
            assert status == 400, (k, status)
    finally:
        srv.shutdown()


def test_conversation_search_kind_param(tmp_path, monkeypatch):
    """``/api/conversation/search?kind=...`` (#177 S6): a valid kind → 200 with
    the additive ``kind``/``search_depth`` fields; an invalid kind → 400."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/conversation/search?q=token&kind=tools")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert out["kind"] == "tools" and out["search_depth"] == "full"
        status, body = _get(port, "/api/conversation/search?q=token&kind=bogus")
        assert status == 400, (status, body)
        assert "error" in json.loads(body)
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


def test_conversations_display_is_live_recompute_filter_is_stored(tmp_path, monkeypatch):
    """Pin the shipped cost-source contract (Option B): the rail DISPLAYS a
    live read-time recompute (``_session_cost_map`` from ``session_entries`` /
    ``CLAUDE_MODEL_PRICING``, pricing-immediate), while the cost FILTER predicate
    (``_rollup_where``) compares the STORED ``conversation_sessions.cost_usd``
    rollup column. In steady state the two agree; they diverge only briefly after
    a pricing edit (before the next ``sync_cache`` re-derive). This test forces a
    divergence by skewing ONLY the stored column, then asserts display reads live
    and the filter reads stored — so a future change can't silently flip the rail
    back to reading the stored column (which would re-introduce the
    filter==display coupling and lose pricing-immediacy)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Baseline: s1's displayed cost is the live recompute (1000 input + 500
        # output @ claude-opus-4-8 → 0.0175), and the recompute also stamped the
        # stored column to the same value.
        status, body = _get(port, "/api/conversations")
        assert status == 200, (status, body)
        s1 = next(r for r in json.loads(body)["conversations"]
                  if r["session_id"] == "s1")
        live_cost = s1["cost_usd"]
        assert abs(live_cost - 0.0175) < 1e-9, s1

        # Deliberately skew ONLY the stored rollup column for s1 to a value far
        # from the live recompute (999.0). The display path never reads this
        # column; the filter path reads ONLY this column.
        skewed = 999.0
        cache = ns["open_cache_db"]()
        cache.execute(
            "UPDATE conversation_sessions SET cost_usd=? WHERE session_id=?",
            (skewed, "s1"))
        cache.commit()
        cache.close()

        # (a) DISPLAY still reads LIVE — the skewed stored 999.0 must NOT surface.
        status, body = _get(port, "/api/conversations")
        assert status == 200, (status, body)
        s1 = next(r for r in json.loads(body)["conversations"]
                  if r["session_id"] == "s1")
        assert abs(s1["cost_usd"] - live_cost) < 1e-9, s1
        assert s1["cost_usd"] != skewed, s1

        # (b) FILTER reads the STORED (skewed) column. cost_min=500 admits s1
        # (stored 999 >= 500) even though the LIVE 0.0175 would exclude it —
        # proof the predicate compares the stored column, not the live value.
        status, body = _get(port, "/api/conversations?cost_min=500")
        assert status == 200, (status, body)
        sids = {r["session_id"] for r in json.loads(body)["conversations"]}
        assert "s1" in sids, sids

        # And cost_max=1 EXCLUDES s1 (stored 999 > 1) though the LIVE 0.0175
        # would include it — the complementary direction of the same proof. The
        # displayed cost on any row that survives is still the live recompute.
        status, body = _get(port, "/api/conversations?cost_max=1")
        assert status == 200, (status, body)
        rows = json.loads(body)["conversations"]
        sids = {r["session_id"] for r in rows}
        assert "s1" not in sids, sids
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
    find_in_conversation = _boom


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
                      "/api/conversation/search?q=token",
                      "/api/conversation/s1/find?q=token"):
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


def _seed_payload_rows(ns, tmp_path):
    """Seed conversation_messages rows for the #178 payload route, pointing
    source_path/byte_offset at REAL JSONL files on disk so read_full_payload can
    re-read them. Returns (input_id, result_id). Two lines:

      line 0 — an Edit tool_use with an old_string longer than the 8000-char
               leaf cap, so the route proves it re-derives the FULL input.
      line 1 — a Bash tool_result carrying toolUseResult.stderr, so the route
               proves it serves the full result + stderr from disk.
    """
    p = tmp_path / "payload.jsonl"
    line0 = (json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_e", "name": "Edit",
         "input": {"file_path": "/f.py", "old_string": "X" * 9000,
                   "new_string": "Y"}}]}}) + "\n").encode()
    line1 = (json.dumps({
        "toolUseResult": {"stdout": "out\n", "stderr": "boom", "interrupted": False},
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_b",
             "content": [{"type": "text", "text": "out\nboom"}],
             "is_error": True}]}}) + "\n").encode()
    with open(p, "wb") as fh:
        fh.write(line0)
        fh.write(line1)

    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sp", "pe", None, str(p), 0, "2026-06-04T00:00:00Z", "assistant", "",
         json.dumps([{"kind": "tool_use", "name": "Edit", "input_summary": "{}",
                      "input": {"file_path": "/f.py"}, "input_truncated": True,
                      "id": "toolu_e", "preview": "/f.py"}]),
         _MODEL, "mp", "rp", "/home/u/proj", "main", 0))
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sp", "pr", None, str(p), len(line0), "2026-06-04T00:00:01Z",
         "tool_result", "",
         json.dumps([{"kind": "tool_result", "text": "out\nboom",
                      "truncated": False, "full_length": 8, "is_error": True,
                      "tool_use_id": "toolu_b"}]),
         None, None, None, None, None, 0))
    cache.commit()
    cache.close()
    return "toolu_e", "toolu_b"


def test_payload_route_input_result_and_gate(tmp_path, monkeypatch):
    """The #178 ``/api/conversation/<sid>/payload`` route: loopback 200 with the
    discriminated input/result shapes (full input beyond the leaf cap; full
    result + Bash stderr from disk), 403 on a LAN hostname (gate reused), 400 on
    a bad ``which``, and 404 on an unknown tool_use_id."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        _seed_payload_rows(ns, tmp_path)

        # which=input -> full structured input dict, beyond the 8000 leaf cap.
        status, body = _get(
            port, "/api/conversation/sp/payload?tool_use_id=toolu_e&which=input")
        assert status == 200, (status, body)
        payload = json.loads(body)
        assert payload["which"] == "input"
        assert payload["input"]["old_string"] == "X" * 9000
        assert payload["truncated"] is False

        # which=result -> full result text + Bash stderr.
        status, body = _get(
            port, "/api/conversation/sp/payload?tool_use_id=toolu_b&which=result")
        assert status == 200, (status, body)
        payload = json.loads(body)
        assert payload["which"] == "result"
        assert payload["text"] == "out\nboom"
        assert payload["is_error"] is True
        assert payload["stderr"] == "boom"

        # Privacy gate reused verbatim: LAN hostname + expose=False -> 403.
        status, _ = _get(
            port, "/api/conversation/sp/payload?tool_use_id=toolu_e&which=input",
            host="machine.local:8789")
        assert status == 403

        # Bad which -> 400.
        status, _ = _get(
            port, "/api/conversation/sp/payload?tool_use_id=toolu_e&which=bogus")
        assert status == 400

        # Unknown tool_use_id -> 404.
        status, _ = _get(
            port, "/api/conversation/sp/payload?tool_use_id=nope&which=result")
        assert status == 404
    finally:
        srv.shutdown()


def test_payload_route_source_gone_returns_410(tmp_path, monkeypatch):
    """A row whose source_path points at a missing/rotated JSONL -> 410 (the
    documented consequence of storing only capped text)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        cache = ns["open_cache_db"]()
        gone = str(tmp_path / "rotated-away.jsonl")          # never created
        cache.execute(
            "INSERT OR IGNORE INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sg", "g1", None, gone, 0, "2026-06-05T00:00:00Z", "assistant", "",
             json.dumps([{"kind": "tool_use", "name": "Bash", "input_summary": "{}",
                          "input": {"command": "ls"}, "input_truncated": False,
                          "id": "toolu_gone", "preview": "ls"}]),
             _MODEL, "mg", "rg", None, None, 0))
        cache.commit()
        cache.close()
        status, body = _get(
            port, "/api/conversation/sg/payload?tool_use_id=toolu_gone&which=input")
        assert status == 410, (status, body)
        assert "error" in json.loads(body)
    finally:
        srv.shutdown()


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


# ──────────────────────────────────────────────────────────────────────────
# #177 S4: the on-demand media route. Serves decoded image/PDF bytes by
# re-reading the source JSONL (the #178 mechanism), behind the privacy gate +
# a Fetch-Metadata cross-origin check. Re-uses the booted-handler harness.
# ──────────────────────────────────────────────────────────────────────────
import base64 as _b64

PNG_BYTES = b"\x89PNG_fake_pixels"
PNG_B64 = _b64.b64encode(PNG_BYTES).decode()
PDF_BYTES = b"%PDF-fake"
PDF_B64 = _b64.b64encode(PDF_BYTES).decode()


def _get_media(port, path, *, host=None, sec_fetch_site=None):
    """GET helper that returns ``(status, headers, body)`` so the media route's
    exact response headers can be asserted. Optional Host (gate spoof) +
    Sec-Fetch-Site (Fetch-Metadata oracle) headers."""
    c = HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", path, skip_host=(host is not None))
    if host is not None:
        c.putheader("Host", host)
    if sec_fetch_site is not None:
        c.putheader("Sec-Fetch-Site", sec_fetch_site)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    status = r.status
    headers = dict(r.getheaders())
    c.close()
    return status, headers, body


def _seed_media_rows(ns, tmp_path):
    """Seed conversation_messages rows for the media route, pointing
    source_path/byte_offset at a REAL JSONL on disk so read_media_bytes can
    re-read it. Two lines:

      line 0 — a user tool_result whose content array holds a PNG image item
               (the MCP-screenshot shape) addressed by tool_use_id=tu_img.
      line 1 — a user-content document (PDF) addressed by uuid=ud.
    Returns the JSONL path so a 410 test can delete it."""
    p = tmp_path / "media.jsonl"
    line0 = (json.dumps({"type": "user", "uuid": "ur", "sessionId": "sm",
                         "timestamp": "t", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_img", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": PNG_B64}}]}]}}) + "\n").encode()
    line1 = (json.dumps({"type": "user", "uuid": "ud", "sessionId": "sm",
                         "timestamp": "t", "message": {"role": "user", "content": [
        {"type": "document", "source": {"type": "base64",
                                        "media_type": "application/pdf",
                                        "data": PDF_B64}}]}}) + "\n").encode()
    with open(p, "wb") as fh:
        fh.write(line0)
        fh.write(line1)

    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sm", "ur", None, str(p), 0, "2026-06-06T00:00:00Z", "tool_result", "",
         json.dumps([{"kind": "tool_result", "text": "", "truncated": False,
                      "full_length": 0, "is_error": False, "tool_use_id": "tu_img",
                      "media": [{"kind": "image", "media_type": "image/png",
                                 "bytes": len(PNG_B64), "index": 0}]}]),
         None, None, None, None, None, 0))
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sm", "ud", None, str(p), len(line0), "2026-06-06T00:00:01Z", "human", "",
         json.dumps([{"kind": "document", "media_type": "application/pdf",
                      "bytes": len(PDF_B64), "index": 0}]),
         None, None, None, None, None, 0))
    cache.commit()
    cache.close()
    return p


def test_media_route_serves_png(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        _seed_media_rows(ns, tmp_path)
        status, headers, body = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0")
        assert status == 200, (status, body)
        assert body == PNG_BYTES
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Security-Policy"] == "default-src 'none'"
        assert headers["Cache-Control"] == "private, max-age=86400"
        assert "Content-Disposition" not in headers
    finally:
        srv.shutdown()


def test_media_route_pdf_disposition(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        _seed_media_rows(ns, tmp_path)
        status, headers, body = _get_media(
            port, "/api/conversation/sm/media?uuid=ud&index=0")
        assert status == 200, (status, body)
        assert body == PDF_BYTES
        assert headers["Content-Type"] == "application/pdf"
        assert headers["Content-Disposition"] == 'inline; filename="attachment-0.pdf"'
        assert "Content-Security-Policy" not in headers   # no CSP sandbox for PDFs
    finally:
        srv.shutdown()


def test_media_route_param_validation(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        _seed_media_rows(ns, tmp_path)
        for path in (
            "/api/conversation/sm/media?index=0",                       # no key
            "/api/conversation/sm/media?tool_use_id=tu_img&uuid=ud&index=0",  # both keys
            "/api/conversation/sm/media?tool_use_id=tu_img&index=-1",   # negative
            "/api/conversation/sm/media?tool_use_id=tu_img&index=abc",  # non-int
        ):
            status, _, _ = _get_media(port, path)
            assert status == 400, (path, status)
    finally:
        srv.shutdown()


def test_media_route_404_unknown_and_unsupported(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        p = _seed_media_rows(ns, tmp_path)
        # unknown tool_use_id -> 404
        status, _, _ = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=nope&index=0")
        assert status == 404
        # placeholder exists but the source's media_type is not allowlisted -> 404.
        bmp_line = (json.dumps({"type": "user", "uuid": "ub", "sessionId": "sm",
                                "timestamp": "t", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_bmp", "content": [
                {"type": "image", "source": {"media_type": "image/bmp",
                                             "data": PNG_B64}}]}]}}) + "\n").encode()
        with open(p, "ab") as fh:
            off = p.stat().st_size
            fh.write(bmp_line)
        cache = ns["open_cache_db"]()
        cache.execute(
            "INSERT OR IGNORE INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sm", "ub", None, str(p), off, "2026-06-06T00:00:02Z", "tool_result", "",
             json.dumps([{"kind": "tool_result", "text": "", "truncated": False,
                          "full_length": 0, "is_error": False, "tool_use_id": "tu_bmp",
                          "media": [{"kind": "image", "media_type": "image/bmp",
                                     "bytes": len(PNG_B64), "index": 0}]}]),
             None, None, None, None, None, 0))
        cache.commit()
        cache.close()
        status, _, _ = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_bmp&index=0")
        assert status == 404
    finally:
        srv.shutdown()


def test_media_route_410_gone(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        p = _seed_media_rows(ns, tmp_path)
        p.unlink()                                  # delete the source JSONL
        status, _, body = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0")
        assert status == 410, (status, body)
    finally:
        srv.shutdown()


def test_media_route_unexpected_read_error_500_envelope(tmp_path, monkeypatch):
    # #183 — defensive-envelope parity with the sibling byte handlers. The kernel
    # `read_media_bytes` is internally defensive (OSError/ValueError -> 410 gone),
    # but an UNEXPECTED exception type used to escape the handler unguarded —
    # killing the thread with no logged 500 because the response hadn't started.
    # The handler now wraps the read + emission: a pre-emission failure returns a
    # logged 500 envelope ({type}: {msg}), not a stack trace / dropped connection.
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        _seed_media_rows(ns, tmp_path)
        # Pin the SAME kernel-module instance the route resolves and force its
        # read to raise an unexpected error (not the caught OSError/ValueError).
        cq_mod = ns["_load_sibling"]("_lib_conversation_query")

        def _boom(*_a, **_k):
            raise RuntimeError("synthetic read failure")

        monkeypatch.setattr(cq_mod, "read_media_bytes", _boom)
        status, _, body = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0")
        assert status == 500, (status, body)
        # The logged-500 envelope carries the exception class + message, mirroring
        # `_run_conversation_query` / `_handle_get_doctor`.
        payload = json.loads(body)
        assert "RuntimeError: synthetic read failure" in payload["error"]
    finally:
        srv.shutdown()


def test_media_route_403_gate_and_cross_site(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        _seed_media_rows(ns, tmp_path)
        # spoofed LAN Host + expose off -> 403 (gate reused verbatim).
        status, _, _ = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0",
            host="evil.example:8789")
        assert status == 403
        # cross-site Sec-Fetch-Site -> 403 (Codex F1 embed defense).
        status, _, _ = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0",
            sec_fetch_site="cross-site")
        assert status == 403
        # same-origin -> 200; absent header -> 200 (defense-in-depth, not primary).
        status, _, body = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0",
            sec_fetch_site="same-origin")
        assert status == 200 and body == PNG_BYTES
        status, _, body = _get_media(
            port, "/api/conversation/sm/media?tool_use_id=tu_img&index=0")
        assert status == 200 and body == PNG_BYTES
    finally:
        srv.shutdown()


def test_media_route_413_too_large(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        # Force the loaded kernel module's payload ceiling tiny so a small
        # base64 string trips the encoded-length precheck -> 413. The handler
        # resolves the sibling via sys.modules["_lib_conversation_query"]; pin
        # the same instance the route uses.
        cq_mod = ns["_load_sibling"]("_lib_conversation_query")
        monkeypatch.setattr(cq_mod, "_MEDIA_PAYLOAD_CEILING", 8)
        p = tmp_path / "big.jsonl"
        big = "A" * 100   # > 8 * 4/3 encoded-precheck
        line = (json.dumps({"type": "user", "uuid": "ubig", "sessionId": "sm",
                            "timestamp": "t", "message": {"role": "user", "content": [
            {"type": "image", "source": {"media_type": "image/png",
                                         "data": big}}]}}) + "\n").encode()
        with open(p, "wb") as fh:
            fh.write(line)
        cache = ns["open_cache_db"]()
        cache.execute(
            "INSERT OR IGNORE INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sm", "ubig", None, str(p), 0, "2026-06-06T00:00:03Z", "human", "",
             json.dumps([{"kind": "image", "media_type": "image/png",
                          "bytes": len(big), "index": 0}]),
             None, None, None, None, None, 0))
        cache.commit()
        cache.close()
        status, _, body = _get_media(
            port, "/api/conversation/sm/media?uuid=ubig&index=0")
        assert status == 413, (status, body)
    finally:
        srv.shutdown()


# === 1A: per-session cache-rebuild count helper ============================

def test_session_cache_rebuild_count_matches_outline(tmp_path, monkeypatch):
    """session_cache_rebuild_count(conn, sid) is the single source of truth for
    the rollup's cache_rebuild_count column: it must equal the count /outline
    would report (which OMITS stats.cache_failures entirely when 0 — so absent
    reads as 0). Proven over a clean session (0) and a rebuild session (1)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    import importlib
    lq = importlib.import_module("_lib_conversation_query")
    with ns["open_cache_db"]() as conn:
        for sid in ("sess-clean", "sess-rebuild"):
            count = lq.session_cache_rebuild_count(conn, sid)
            outline = lq.get_conversation_outline(conn, sid)
            cf = outline["stats"].get("cache_failures")
            expected = cf["count"] if cf else 0
            assert count == expected, f"{sid}: helper {count} != outline {expected}"
        # Concrete expectations (guards against both reading 0 vacuously).
        assert lq.session_cache_rebuild_count(conn, "sess-clean") == 0
        assert lq.session_cache_rebuild_count(conn, "sess-rebuild") == 1


def test_session_cache_rebuild_count_unknown_session(tmp_path, monkeypatch):
    """An unknown session (no rows) assembles to None -> 0, never raises."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    import importlib
    lq = importlib.import_module("_lib_conversation_query")
    with ns["open_cache_db"]() as conn:
        assert lq.session_cache_rebuild_count(conn, "does-not-exist") == 0

# === 1B: last_anchor on the conversation detail head =======================

def test_get_conversation_exposes_last_anchor(tmp_path, monkeypatch):
    """get_conversation() head carries last_anchor = {session_id, uuid, id} of
    the final RENDERED item, with the REAL session_id (assembled anchors carry
    session_id None until the page patches them — Codex P2 #4)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    import importlib
    lq = importlib.import_module("_lib_conversation_query")
    with ns["open_cache_db"]() as conn:
        detail = lq.get_conversation(conn, "sess-clean", after=None, limit=1)
        la = detail["last_anchor"]
        assert la is not None
        assert la["session_id"] == "sess-clean"          # real id, not null
        assert isinstance(la["uuid"], str) and la["uuid"]
        assert isinstance(la["id"], int)
        asm = lq._assemble_session(conn, "sess-clean")
        assert la["uuid"] == asm["items"][-1]["anchor"]["uuid"]


def test_get_conversation_unknown_session_returns_none(tmp_path, monkeypatch):
    """An unknown session returns None (no head, hence no last_anchor) — the
    empty/unknown case the jump-to-latest control no-ops on."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    import importlib
    lq = importlib.import_module("_lib_conversation_query")
    with ns["open_cache_db"]() as conn:
        assert lq.get_conversation(conn, "does-not-exist") is None

# === 1C: _recompute_conversation_sessions fills the filter columns =========

def test_recompute_fills_filter_columns(tmp_path, monkeypatch):
    """The augmented full recompute fills project_label / cost_usd /
    cache_rebuild_count on the rollup for every session."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    import importlib
    cc = importlib.import_module("_cctally_cache")
    with ns["open_cache_db"]() as conn:
        cc._recompute_conversation_sessions(conn)  # full
        row = conn.execute(
            "SELECT project_label, cost_usd, cache_rebuild_count "
            "FROM conversation_sessions WHERE session_id='sess-rebuild'"
        ).fetchone()
        assert row is not None
        assert row[0] == "rebuild"        # project_label = basename(cwd)
        assert row[1] >= 0.0              # cost_usd
        assert row[2] == 1               # cache_rebuild_count (one flagged turn)
        clean = conn.execute(
            "SELECT cache_rebuild_count FROM conversation_sessions "
            "WHERE session_id='sess-clean'"
        ).fetchone()
        assert clean[0] == 0


# === Task 2: browse-list filters + facets endpoint ========================
#
# The seeded rail (see _seed_cache) is the fixture for every server-side filter
# test below. Its four sessions span the axes we filter on:
#   s1            project 'proj',    cost ~0.0175, rebuilds 0, 2026-06-01
#   s2            project 'other',   cost  0.0    , rebuilds 0, 2026-06-02
#   sess-clean    project 'clean',   cost ~0.10  , rebuilds 0, 2026-06-03
#   sess-rebuild  project 'rebuild', cost ~0.255 , rebuilds 1, 2026-06-04
# (the plan's illustrative `projA`/`cost_min=1.0` are replaced with these real
# seeded labels + thresholds so the assertions are non-vacuous.)


def _get_json(srv, path):
    """GET ``path`` from the booted server with a loopback Host header; return
    ``(status, parsed_json)``. The shared driver for the Task-2 HTTP filter
    tests (the same _boot harness the gate/rail tests use)."""
    from http.client import HTTPConnection
    c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("GET", path, headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, json.loads(body)


def test_facets_lists_projects_with_counts(tmp_path, monkeypatch):
    """GET /api/conversations/facets returns sorted distinct project labels with
    per-project conversation counts; empty/NULL labels are dropped."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations/facets")
        assert st == 200
        names = {p["project_label"] for p in body["projects"]}
        # Every seeded session has a non-empty cwd-derived label.
        assert {"proj", "other", "clean", "rebuild"} <= names
        assert all(p.get("count", 0) >= 1 for p in body["projects"])
        # Sorted ascending by label (the kernel's ORDER BY).
        labels = [p["project_label"] for p in body["projects"]]
        assert labels == sorted(labels)
    finally:
        srv.shutdown()


def test_filter_by_project(tmp_path, monkeypatch):
    """?projects=proj returns ONLY the 'proj'-labelled session (s1)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?projects=proj")
        assert st == 200
        assert {c["project_label"] for c in body["conversations"]} == {"proj"}
        assert {c["session_id"] for c in body["conversations"]} == {"s1"}
    finally:
        srv.shutdown()


def test_filter_by_project_multi_any(tmp_path, monkeypatch):
    """Multi-value project filter is ANY-of: ?projects=proj&projects=clean
    returns both labels' sessions and nothing else."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?projects=proj&projects=clean")
        assert st == 200
        assert {c["project_label"] for c in body["conversations"]} == {"proj", "clean"}
    finally:
        srv.shutdown()


def test_filter_by_cost_and_rebuilds(tmp_path, monkeypatch):
    """?cost_min=0.05&rebuild_min=1 keeps only sess-rebuild (cost ~0.255,
    rebuilds 1). s1/s2 fall below the cost floor; sess-clean has 0 rebuilds."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?cost_min=0.05&rebuild_min=1")
        assert st == 200
        sids = {c["session_id"] for c in body["conversations"]}
        assert sids == {"sess-rebuild"}, sids
        assert all(c["cost_usd"] >= 0.05 for c in body["conversations"])
    finally:
        srv.shutdown()


def test_filter_by_cost_max(tmp_path, monkeypatch):
    """?cost_max=0.05 keeps only the cheap sessions (s1, s2), excluding the
    pricier sess-clean / sess-rebuild."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?cost_max=0.05")
        assert st == 200
        sids = {c["session_id"] for c in body["conversations"]}
        assert sids == {"s1", "s2"}, sids
        assert all(c["cost_usd"] <= 0.05 for c in body["conversations"])
    finally:
        srv.shutdown()


def test_filter_by_date_range(tmp_path, monkeypatch):
    """?date_from / date_to bound on last_activity_utc (display-tz day
    boundaries). 2026-06-02..2026-06-03 keeps s2 (06-02) and sess-clean
    (06-03), drops s1 (06-01) and sess-rebuild (06-04)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(
            srv, "/api/conversations?date_from=2026-06-02&date_to=2026-06-03")
        assert st == 200
        sids = {c["session_id"] for c in body["conversations"]}
        assert sids == {"s2", "sess-clean"}, sids
    finally:
        srv.shutdown()


def test_filter_pagination_correct(tmp_path, monkeypatch):
    """Two filtered pages of limit=1 don't overlap (the predicate is applied in
    SQL before LIMIT/OFFSET, so pagination is correct over the filtered set)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        # cost_max=0.05 -> exactly {s1, s2}; page them one at a time.
        st, p1 = _get_json(srv, "/api/conversations?cost_max=0.05&limit=1&offset=0")
        st2, p2 = _get_json(srv, "/api/conversations?cost_max=0.05&limit=1&offset=1")
        assert st == 200 and st2 == 200
        assert len(p1["conversations"]) == 1 and len(p2["conversations"]) == 1
        ids = [c["session_id"] for c in p1["conversations"] + p2["conversations"]]
        assert len(ids) == len(set(ids))           # no overlap across pages
        assert set(ids) == {"s1", "s2"}
        assert p1["page"]["has_more"] is True       # more after the first page
        assert p2["page"]["has_more"] is False      # the filtered set is exhausted
    finally:
        srv.shutdown()


def test_filter_bad_cost_is_400(tmp_path, monkeypatch):
    """A non-numeric cost is a hard 400 (the handler validates types before the
    kernel; consistent with the other conversation endpoints)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?cost_min=abc")
        assert st == 400
        assert "error" in body
    finally:
        srv.shutdown()


def test_filter_bad_rebuild_is_400(tmp_path, monkeypatch):
    """A non-integer rebuild threshold is a hard 400."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?rebuild_min=lots")
        assert st == 400
    finally:
        srv.shutdown()


def test_filter_bad_date_is_400(tmp_path, monkeypatch):
    """A malformed date maps the date-helper ValueError to a 400."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?date_from=not-a-date")
        assert st == 400
    finally:
        srv.shutdown()


def test_filter_dual_branch_parity(tmp_path, monkeypatch):
    """The DATE axis is the only filter expressible in BOTH list-query branches
    (the rollup fast path AND the live GROUP BY fallback). For the same date
    bound they must return byte-identical session-id order (the reconcile
    invariant)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    import importlib
    lq = importlib.import_module("_lib_conversation_query")
    f = {"date_from": "2026-06-02T00:00:00Z", "date_to": None,
         "projects": None, "cost_min": None, "cost_max": None, "rebuild_min": None}
    with ns["open_cache_db"]() as conn:
        roll = lq._list_session_rows_rollup(conn, lq._SORTS["recent"], 50, 0, f)
        live = lq._list_session_rows_live(conn, lq._SORTS_LIVE["recent"], 50, 0, f)
        assert [r[0] for r in roll] == [r[0] for r in live]
        # Non-vacuous: the bound excludes s1 (06-01) but keeps the rest.
        ids = [r[0] for r in roll]
        assert "s1" not in ids and {"s2", "sess-clean", "sess-rebuild"} <= set(ids)


# === Finding 1: half-open precision-safe date bounds at day boundaries =====
#
# Stored last_activity_utc is raw JSONL-passthrough of MIXED precision — both
# whole-second `...SSZ` and millisecond `...SS.mmmZ` occur in real data. A naive
# whole-second lower bound + a `...23:59:59.999999Z` inclusive upper bound
# mis-compare lexicographically at day boundaries (ASCII `Z` 0x5A > `.` 0x2E and
# > digits, `.000Z` < `00Z`). The fix is a HALF-OPEN interval
# [start_of_day(date_from), start_of_next_day(date_to)) with 6-digit-microsecond
# `...SS.000000Z` bounds and a STRICT `<` upper. This regression seeds rows at
# the exact day boundaries in every precision and asserts the inclusion set.
_BOUNDARY_SESSIONS = {
    # session_id: (last_activity_utc, expected_in_single_day_filter)
    "bnd-mid-ms":   ("2026-06-04T00:00:00.000Z", True),   # midnight, ms precision
    "bnd-last-ms":  ("2026-06-04T23:59:59.999Z", True),   # last ms of the day
    "bnd-last-sec": ("2026-06-04T23:59:59Z",     True),   # whole-second last second
    "bnd-prev-ms":  ("2026-06-03T23:59:59.999Z", False),  # previous day, last ms
    "bnd-next-mid": ("2026-06-05T00:00:00.000Z", False),  # next-day midnight
}


def _seed_boundary_rows(ns):
    """Seed one single-message session per boundary timestamp, recompute the
    rollup, then pin each session's stored ``last_activity_utc`` to the exact
    mixed-precision boundary value. The recompute derives MAX(timestamp_utc)
    naturally; the explicit UPDATE guarantees the stored bytes are the precise
    boundary string under test (recompute preserves the raw string, but the
    UPDATE removes any ambiguity and still drives the real ``_rollup_where`` SQL
    and the real live ``HAVING`` for that row)."""
    cache = ns["open_cache_db"]()
    for i, (sid, (ts, _)) in enumerate(_BOUNDARY_SESSIONS.items()):
        cache.execute(
            "INSERT OR IGNORE INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, f"bu{i}", None, f"bnd{i}.jsonl", 0, ts, "human",
             "boundary probe", "[]", None, None, None,
             "/home/u/bnd", "main", 0),
        )
    import _cctally_cache as _cc
    _cc._recompute_conversation_sessions(cache)
    # Pin the precise mixed-precision stored value for each boundary row.
    for sid, (ts, _) in _BOUNDARY_SESSIONS.items():
        cache.execute(
            "UPDATE conversation_sessions SET last_activity_utc=?, started_utc=? "
            "WHERE session_id=?", (ts, ts, sid))
    cache.commit()
    cache.close()


def test_filter_date_boundary_inclusion_rollup(tmp_path, monkeypatch):
    """Single-day filter ?date_from=2026-06-04&date_to=2026-06-04 over rows whose
    stored last_activity_utc sits AT the day boundaries in mixed precision.

    RED against the pre-fix helper: the whole-second lower bound dropped the
    `.000Z` midnight row (`.000Z` < `00Z` lexically) and the `.999999Z`
    inclusive upper bound dropped both the `.999Z` last-ms row (`.999Z` >
    `.999999Z`) and the whole-second `23:59:59Z` row (`Z` > `.`). The half-open
    fix keeps all three same-day rows and excludes the neighbours."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        # Pin the server's display tz to UTC so the day-boundary bounds are
        # deterministic regardless of the dev machine's host-local fallback
        # (the seeded rows are stored as ...Z and the assertion is UTC-day-keyed).
        ns["DashboardHTTPHandler"].display_tz_pref_override = "utc"
        _seed_boundary_rows(ns)
        st, body = _get_json(
            srv, "/api/conversations?date_from=2026-06-04&date_to=2026-06-04")
        assert st == 200, (st, body)
        got = {c["session_id"] for c in body["conversations"]
               if c["session_id"].startswith("bnd-")}
        expected = {sid for sid, (_, keep) in _BOUNDARY_SESSIONS.items() if keep}
        assert got == expected, (got, expected)
    finally:
        srv.shutdown()


def test_filter_date_boundary_inclusion_live_branch_parity(tmp_path, monkeypatch):
    """The LIVE fallback's date-only HAVING (MAX(timestamp_utc) bounds) must
    agree with the rollup branch on the SAME boundary inclusion set. Drives the
    two kernel row-source helpers directly so both date predicates are
    exercised over the identical mixed-precision boundary rows."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import pathlib as _pl
    sys.path.insert(0, str(_pl.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)
    _seed_boundary_rows(ns)
    import importlib
    m = importlib.import_module("_lib_dashboard_dates")
    lq = importlib.import_module("_lib_conversation_query")
    df, dtt = m.parse_filter_date_range("2026-06-04", "2026-06-04", tz_name="Etc/UTC")
    f = {"date_from": df, "date_to": dtt, "projects": None,
         "cost_min": None, "cost_max": None, "rebuild_min": None}
    expected = {sid for sid, (_, keep) in _BOUNDARY_SESSIONS.items() if keep}
    with ns["open_cache_db"]() as conn:
        roll = lq._list_session_rows_rollup(conn, lq._SORTS["recent"], 200, 0, f)
        live = lq._list_session_rows_live(conn, lq._SORTS_LIVE["recent"], 200, 0, f)
        roll_bnd = {r[0] for r in roll if r[0].startswith("bnd-")}
        live_bnd = {r[0] for r in live if r[0].startswith("bnd-")}
        assert roll_bnd == expected, (roll_bnd, expected)
        assert live_bnd == expected, (live_bnd, expected)


# === Finding 2: filter_degraded positive case (live branch, rollup-only axis) =

def _arm_backfill_pending(ns):
    """Set the durable conversation_sessions_backfill_pending flag so
    _rollup_authoritative(conn) returns False and list_conversations takes the
    LIVE GROUP BY fallback (which cannot express the rollup-only axes)."""
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT OR REPLACE INTO cache_meta(key,value) "
        "VALUES('conversation_sessions_backfill_pending','1')")
    cache.commit()
    cache.close()


def test_filter_degraded_set_on_live_branch_rollup_only_axis(tmp_path, monkeypatch):
    """Under the live branch a rollup-only axis (e.g. cost_min) is SILENTLY not
    applied; the page must carry filter_degraded=True AND still include a session
    that cost-filtering would otherwise drop (proving the silent degradation the
    flag warns about)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        _arm_backfill_pending(ns)
        st, body = _get_json(srv, "/api/conversations?cost_min=0.05")
        assert st == 200, (st, body)
        assert body["page"].get("filter_degraded") is True, body["page"]
        sids = {c["session_id"] for c in body["conversations"]}
        # s1 (cost ~0.0175) is below the 0.05 floor; under a working cost filter
        # it would be dropped — its presence proves the axis was NOT applied.
        assert "s1" in sids, sids
    finally:
        srv.shutdown()


def test_filter_degraded_absent_on_authoritative_cost_filter(tmp_path, monkeypatch):
    """On the AUTHORITATIVE rollup path the same ?cost_min=0.05 applies cleanly:
    no filter_degraded flag, and the cheap sessions are actually dropped."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?cost_min=0.05")
        assert st == 200, (st, body)
        assert not body["page"].get("filter_degraded"), body["page"]
        sids = {c["session_id"] for c in body["conversations"]}
        assert "s1" not in sids, sids   # below the floor -> actually dropped
    finally:
        srv.shutdown()


def test_filter_degraded_absent_on_live_branch_date_only(tmp_path, monkeypatch):
    """A date-ONLY filter under the live branch is fully expressible (HAVING over
    MAX(timestamp_utc)), so it must NOT set filter_degraded."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        _arm_backfill_pending(ns)
        st, body = _get_json(srv, "/api/conversations?date_from=2026-06-02")
        assert st == 200, (st, body)
        assert not body["page"].get("filter_degraded"), body["page"]
    finally:
        srv.shutdown()



def test_conversations_model_filter_and_facets(tmp_path, monkeypatch):
    """#278 Theme C: the model-family axis parses (repeated + comma-joined) and
    restricts BOTH browse and search; the facets route gains a `models` array.
    The seeded sessions s1 / sess-clean / sess-rebuild use claude-opus-4-8; s2 is
    a human-only session with no model."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Browse: repeated ?models=opus restricts to the opus sessions.
        status, body = _get(port, "/api/conversations?models=opus")
        assert status == 200, (status, body)
        sids = {r["session_id"] for r in json.loads(body)["conversations"]}
        assert sids == {"s1", "sess-clean", "sess-rebuild"}, sids

        # Present-but-empty: a family with no sessions returns ZERO, not all.
        status, body = _get(port, "/api/conversations?models=haiku")
        assert status == 200, (status, body)
        assert json.loads(body)["conversations"] == []

        # Comma-joined single value is split (opus,sonnet -> opus; no sonnet).
        status, body = _get(port, "/api/conversations?models=opus,sonnet")
        assert status == 200, (status, body)
        sids = {r["session_id"] for r in json.loads(body)["conversations"]}
        assert sids == {"s1", "sess-clean", "sess-rebuild"}, sids

        # Search: the same axis restricts hits (only s1 carries "token").
        status, body = _get(port, "/api/conversation/search?q=token&models=opus")
        assert status == 200, (status, body)
        out = json.loads(body)
        assert {h["session_id"] for h in out["hits"]} == {"s1"}, out
        assert "filter_degraded" not in out
        status, body = _get(port, "/api/conversation/search?q=token&models=fable")
        assert status == 200, (status, body)
        assert json.loads(body)["hits"] == []

        # Facets: models array present; opus count = 3.
        status, body = _get(port, "/api/conversations/facets")
        assert status == 200, (status, body)
        fac = json.loads(body)
        counts = {m["family"]: m["count"] for m in fac["models"]}
        assert counts == {"opus": 3}, counts
    finally:
        srv.shutdown()
