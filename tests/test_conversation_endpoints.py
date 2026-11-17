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
import contextlib
import datetime as dt
import base64
import json
import pathlib
import socketserver
import sqlite3
import sys
import threading
import time

import pytest
from http.client import HTTPConnection

from _lib_dashboard_sources import SOURCE_SCHEMA_VERSION
from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, shorten_sse_keepalive, start, stop

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


def _make_snapshot(ns, *, with_codex_label=False):
    DataSnapshot = ns["DataSnapshot"]
    snapshot = DataSnapshot(
        current_week=None, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None,
        generated_at=dt.datetime(2026, 6, 3, 12, 0, tzinfo=dt.timezone.utc),
        percent_milestones=[], weekly_history=[],
        weekly_periods=[], monthly_periods=[],
        blocks_panel=[], daily_panel=[],
    )
    if with_codex_label:
        from _lib_dashboard_sources import (
            SourceDashboardBundle,
            SourceDashboardState,
            compose_all_state,
        )
        now = snapshot.generated_at
        key = "session:codex-private"
        claude = SourceDashboardState(
            source="claude", availability="empty", freshness="fresh",
            warnings=(), data_version="claude-v1", last_success_at=now,
            capabilities={}, data={"sessions": {"rows": ()}},
        )
        codex = SourceDashboardState(
            source="codex", availability="ok", freshness="fresh",
            warnings=(), data_version="codex-v1", last_success_at=now,
            capabilities={},
            data={"sessions": {"rows": ({"key": key, "source": "codex"},)}},
        )
        object.__setattr__(
            codex, "private_session_labels", {key: "Private first prompt"},
        )
        snapshot.source_bundle = SourceDashboardBundle(
            source_schema_version=SOURCE_SCHEMA_VERSION, default_source="claude",
            source_order=("claude", "codex", "all"),
            sources={
                "claude": claude,
                "codex": codex,
                "all": compose_all_state(claude, codex),
            },
        )
    return snapshot


def _boot(
    ns, tmp_path, monkeypatch, *, bind="127.0.0.1", expose=False,
    with_codex_label=False,
):
    """Seed the cache and start a server with the given bind/expose posture.

    Returns the running ThreadingTCPServer; the caller must tear it down with
    ``stop(srv, srv._test_thread)``, passing every SSE connection it opened.
    """
    redirect_paths(ns, monkeypatch, tmp_path)
    # #630 S2: the /api/events handler blocks in `q.get(timeout=...)` and only
    # learns its client has gone on the keep-alive write after that timeout —
    # and on a socket the peer has closed the FIRST write still succeeds. At
    # the shipped period that is two full timeouts, which is the whole
    # presence backstop, so stop() could not reap the handler at all.
    shorten_sse_keepalive(ns, monkeypatch)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    _seed_cache(ns)

    HandlerCls = ns["DashboardHTTPHandler"]
    # #787 / #861 spec 5.8: a reader never creates or bridges the transcript
    # store; dashboard startup does, before bind, as `cmd_dashboard` does.
    sys.modules["_cctally_dashboard"]._dashboard_startup_schema_migration()
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    HandlerCls.snapshot_ref = SnapshotRef(
        _make_snapshot(ns, with_codex_label=with_codex_label)
    )
    HandlerCls.hub = SSEHub()
    # #583 S3 §7: `/api/data` serves the most recently PUBLISHED state,
    # so a bare reference is not enough — seed the hub exactly as
    # `cmd_dashboard` does before the HTTP server binds.
    HandlerCls.hub.publish(HandlerCls.snapshot_ref.get())
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = bind
    HandlerCls.cctally_expose_transcripts = expose

    # Threading server (mirrors production's ThreadingHTTPServer) so a
    # long-lived SSE connection (`/api/events` blocks in a keep-alive loop)
    # does not wedge the single accept thread and starve later requests.
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv._test_thread = start(srv)
    return srv


def _get(port, path, *, host=None):
    """GET helper. When ``host`` is given, send it as the literal Host header
    (skip_host=True) so the gate sees a non-loopback authority."""
    c = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
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


def _delete(port, path):
    c = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
    c.request("DELETE", path)
    r = c.getresponse()
    body = r.read()
    status = r.status
    c.close()
    return status, body


def _get_ct(port, path, *, host=None):
    """GET helper returning ``(status, content_type, body)`` — for routes whose
    Content-Type matters (e.g. the export route's ``text/markdown``)."""
    c = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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

        # The progressive preflight is used inside Session detail even when a
        # cost-only session has no retained transcript. Ordinary absence must
        # not create a failed browser resource request; the legacy URL retains
        # its 404 contract for other clients.
        status, body = _get(
            port, "/api/conversation/does-not-exist/outline?progressive=1")
        assert status == 200, (status, body)
        assert json.loads(body) == {"status": "not_found"}

        # Privacy gate reused verbatim: LAN hostname + expose=False → 403.
        status, _ = _get(port, "/api/conversation/s1/outline",
                         host="machine.local:8789")
        assert status == 403
    finally:
        stop(srv, srv._test_thread)


def test_conversation_outline_progressive_reconstructs_legacy_body(tmp_path, monkeypatch):
    """The bounded first response defers full outline work; transfer is exact."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        status, legacy_wire = _get(port, "/api/conversation/s1/outline")
        assert status == 200
        cq_mod = ns["_load_sibling"]("_lib_conversation_query")
        transfer_mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
        opaque_token = "random-prefix-s1-random-suffix"
        monkeypatch.setattr(
            transfer_mod.secrets, "token_urlsafe", lambda _bytes: opaque_token)
        original_outline = cq_mod.get_conversation_outline
        calls = []

        def _counted_outline(*args, **kwargs):
            calls.append(1)
            return original_outline(*args, **kwargs)

        monkeypatch.setattr(cq_mod, "get_conversation_outline", _counted_outline)
        status, initial_wire = _get(
            port, "/api/conversation/s1/outline?progressive=1")
        assert status == 200
        assert len(initial_wire) <= 512 * 1024 + 2048
        initial = json.loads(initial_wire)
        assert initial["progressive"] == 1
        assert "summary" not in initial
        assert calls == []
        transfer = initial["transfer"]
        assert transfer["token"] == opaque_token, (
            "the ticket must come only from the opaque random-token factory; "
            "substring absence is invalid because random URL-safe output can "
            "contain a short session id by chance")
        assert "total" not in transfer and "sha256" not in transfer

        offset = 0
        chunks = []
        while True:
            status, chunk_wire = _get(
                port,
                f"/api/conversation/outline-transfer/{transfer['token']}?offset={offset}")
            assert status == 200
            chunk = json.loads(chunk_wire)
            assert chunk["offset"] == offset
            chunks.append(base64.b64decode(chunk["chunk"]))
            offset = chunk["next_offset"]
            if chunk["done"]:
                break
        assert calls == [1]
        assert b"".join(chunks) == legacy_wire

        status, _ = _get(
            port,
            f"/api/conversation/outline-transfer/{transfer['token']}?offset=999999")
        assert status == 400
        status, _ = _get(port, "/api/conversation/outline-transfer/missing?offset=0")
        assert status == 410
    finally:
        stop(srv, srv._test_thread)


def test_conversation_outline_progressive_preserves_claude_account_scope(
    tmp_path, monkeypatch,
):
    """A transfer builder must hydrate the same account-scoped bytes as preflight."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    account_a = "a" * 32
    account_b = "b" * 32
    try:
        cache_conn = ns["open_cache_db"]()
        try:
            cache_conn.execute(
                "UPDATE session_entries SET account_key=? WHERE msg_id='m1'",
                (account_b,),
            )
            cache_conn.commit()
        finally:
            cache_conn.close()
        conn = ns["open_conversations_db"]()
        try:
            conn.execute(
                "UPDATE conversation_messages SET account_key=? WHERE uuid='h1'",
                (account_a,),
            )
            conn.execute(
                "UPDATE conversation_messages SET account_key=? WHERE uuid='a1'",
                (account_b,),
            )
            import _cctally_cache as cache_mod
            cache_mod._recompute_conversation_sessions(conn, {"s1"})
            conn.commit()
        finally:
            conn.close()

        port = srv.server_address[1]
        status, legacy_wire = _get(
            port, f"/api/conversation/s1/outline?account={account_a}")
        assert status == 200
        assert b"hi" in legacy_wire
        assert b"token limit" not in legacy_wire

        status, initial_wire = _get(
            port,
            f"/api/conversation/s1/outline?progressive=1&account={account_a}",
        )
        assert status == 200
        transfer = json.loads(initial_wire)["transfer"]
        offset = 0
        chunks = []
        while True:
            status, chunk_wire = _get(
                port,
                f"/api/conversation/outline-transfer/{transfer['token']}"
                f"?offset={offset}",
            )
            assert status == 200
            chunk = json.loads(chunk_wire)
            chunks.append(base64.b64decode(chunk["chunk"]))
            offset = chunk["next_offset"]
            if chunk["done"]:
                break
        assert b"".join(chunks) == legacy_wire
    finally:
        stop(srv, srv._test_thread)


def test_outline_transfer_cache_bounds_pending_tickets_and_evicts_oversize():
    """Unmaterialized closures and rejected payloads cannot occupy the cache."""
    ns = load_script()
    transfer_mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    with transfer_mod._OUTLINE_TRANSFERS_LOCK:
        transfer_mod._OUTLINE_TRANSFERS.clear()
        transfer_mod._OUTLINE_TRANSFERS_BYTES = 0
    try:
        tokens = [
            transfer_mod._store_outline_transfer(lambda _handler: (True, {}))["token"]
            for _ in range(transfer_mod._OUTLINE_TRANSFER_COUNT_CAP + 1)
        ]
        assert len(transfer_mod._OUTLINE_TRANSFERS) == (
            transfer_mod._OUTLINE_TRANSFER_COUNT_CAP)
        pending_stats = dict(transfer_mod.outline_transfer_cache_stats())
        assert pending_stats["entryCount"] == (
            transfer_mod._OUTLINE_TRANSFER_COUNT_CAP)
        assert pending_stats["estimatedBytes"] <= pending_stats["maxBytes"]
        assert transfer_mod._outline_transfer_builder(tokens[0]) is None

        newest = tokens[-1]
        assert transfer_mod._materialize_outline_transfer(
            newest, b"x" * (transfer_mod._OUTLINE_TRANSFER_ITEM_CAP + 1)
        ) == "too_large"
        assert transfer_mod._outline_transfer_builder(newest) is None
        assert dict(transfer_mod.outline_transfer_cache_stats())["fallbackCount"] == 1
    finally:
        with transfer_mod._OUTLINE_TRANSFERS_LOCK:
            transfer_mod._OUTLINE_TRANSFERS.clear()
            transfer_mod._OUTLINE_TRANSFERS_BYTES = 0


def test_outline_transfer_concurrent_first_chunk_runs_one_builder(
    tmp_path, monkeypatch,
):
    """Two readers of one token share one server-side materialization."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    transfer_mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    entered = threading.Event()
    release = threading.Event()
    second_request_entered = threading.Event()
    calls = []
    request_count = 0
    request_lock = threading.Lock()

    def _builder(_handler, *args):
        calls.append(1)
        entered.set()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return True, {"session_id": "shared", "turns": [], "stats": {}}

    original = ns["DashboardHTTPHandler"]._handle_get_conversation_outline_transfer

    def _counted_request(handler, path):
        nonlocal request_count
        with request_lock:
            request_count += 1
            if request_count == 2:
                second_request_entered.set()
        return original(handler, path)

    monkeypatch.setattr(
        ns["DashboardHTTPHandler"],
        "_handle_get_conversation_outline_transfer",
        _counted_request,
    )
    token = transfer_mod._store_outline_transfer(_builder)["token"]
    path = f"/api/conversation/outline-transfer/{token}?offset=0"
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(_get(srv.server_address[1], path)))
        for _ in range(2)
    ]
    try:
        workers[0].start()
        assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
        workers[1].start()
        assert second_request_entered.wait(PRESENCE_BACKSTOP_SECONDS)
        release.set()
        for worker in workers:
            worker.join(PRESENCE_BACKSTOP_SECONDS)
            assert not worker.is_alive()
        assert [status for status, _body in results] == [200, 200]
        assert calls == [1]
        assert len({json.loads(body)["sha256"] for _status, body in results}) == 1
    finally:
        release.set()
        stop(srv, srv._test_thread)
        with transfer_mod._OUTLINE_TRANSFERS_LOCK:
            transfer_mod._OUTLINE_TRANSFERS.clear()
            transfer_mod._OUTLINE_TRANSFERS_BYTES = 0


def test_outline_transfer_delete_cancels_abandoned_server_work(
    tmp_path, monkeypatch,
):
    """A browser abort explicitly cancels the one in-flight token owner."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    transfer_mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    entered = threading.Event()
    cancellation_seen = threading.Event()
    fallback_release = threading.Event()

    def _builder(_handler, *args):
        cancelled = args[0] if args else (lambda: False)
        entered.set()
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        while time.monotonic() < deadline:
            if cancelled():
                cancellation_seen.set()
                break
            if fallback_release.wait(0.005):
                break
        return True, {"session_id": "abandoned", "turns": [], "stats": {}}

    token = transfer_mod._store_outline_transfer(_builder)["token"]
    path = f"/api/conversation/outline-transfer/{token}?offset=0"
    result = []
    worker = threading.Thread(
        target=lambda: result.append(_get(srv.server_address[1], path))
    )
    try:
        worker.start()
        assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
        status, _body = _delete(
            srv.server_address[1],
            f"/api/conversation/outline-transfer/{token}",
        )
        if status != 204:
            fallback_release.set()
        assert status == 204
        assert cancellation_seen.wait(PRESENCE_BACKSTOP_SECONDS)
        worker.join(PRESENCE_BACKSTOP_SECONDS)
        assert not worker.is_alive()
        assert result and result[0][0] == 410
        assert transfer_mod._outline_transfer_builder(token) is None
    finally:
        fallback_release.set()
        stop(srv, srv._test_thread)
        with transfer_mod._OUTLINE_TRANSFERS_LOCK:
            transfer_mod._OUTLINE_TRANSFERS.clear()
            transfer_mod._OUTLINE_TRANSFERS_BYTES = 0


def test_outline_transfer_obsolete_completion_cannot_replace_new_generation(
    monkeypatch,
):
    """A cancelled builder cannot publish through a reused transfer token."""
    ns = load_script()
    transfer_mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    monkeypatch.setattr(transfer_mod.secrets, "token_urlsafe", lambda _n: "fixed")

    old_builder = lambda *_args: (True, {"generation": "old"})
    new_builder = lambda *_args: (True, {"generation": "new"})
    token = transfer_mod._store_outline_transfer(old_builder)["token"]
    action, old_record, claimed_builder = transfer_mod._claim_outline_transfer(
        token)
    assert action == "build"
    assert claimed_builder is old_builder

    assert transfer_mod._store_outline_transfer(new_builder)["token"] == token
    assert old_record.cancelled.is_set()
    assert transfer_mod._materialize_outline_transfer(
        token, b'"old"', claim=old_record,
    ) == "expired"

    action, new_record, claimed_builder = transfer_mod._claim_outline_transfer(
        token)
    assert action == "build"
    assert new_record.builder is new_builder
    assert claimed_builder is new_builder


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


def test_conversations_display_and_filter_both_read_stored_cost(tmp_path, monkeypatch):
    """Pin the #302 rail-cost contract: on the authoritative rollup the rail's
    DISPLAYED cost is now the MATERIALIZED ``conversation_sessions.cost_usd``
    column (read straight off the rollup, no per-request ``_session_cost_map``
    re-scan of ``conversation_messages``), the SAME column the cost FILTER
    predicate (``_rollup_where``) compares. Display and filter therefore read one
    stored column — the intentional coupling the approved rail-cost decision
    accepts. Pricing-freshness is delivered by the pricing-fingerprint
    auto-invalidation (``_arm_rollup_backfill_on_pricing_change``, guarded by
    tests/test_rollup_pricing_fingerprint.py), NOT by a per-request live
    recompute — so this test SKEWS the stored column and asserts the display
    honors it, which is exactly the flip from the pre-#302 live-display contract
    (a regression to reading live would surface 0.0175 instead of the skew)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        # Baseline: s1's displayed cost is the materialized fill value (1000 input
        # + 500 output @ claude-opus-4-8 → 0.0175), stored on the rollup.
        status, body = _get(port, "/api/conversations")
        assert status == 200, (status, body)
        s1 = next(r for r in json.loads(body)["conversations"]
                  if r["session_id"] == "s1")
        assert abs(s1["cost_usd"] - 0.0175) < 1e-9, s1

        # Skew ONLY the stored rollup column for s1. Under #302 BOTH the display
        # and the filter read this column (no message change / pricing change, so
        # a plain read does not re-derive it).
        skewed = 999.0
        cache = ns["open_conversations_db"]()
        cache.execute(
            "UPDATE conversation_sessions SET cost_usd=? WHERE session_id=?",
            (skewed, "s1"))
        cache.commit()
        cache.close()

        # (a) DISPLAY reads the STORED (skewed) column — the #302 materialize
        # change. A regression to a live recompute would surface 0.0175 instead.
        status, body = _get(port, "/api/conversations")
        assert status == 200, (status, body)
        s1 = next(r for r in json.loads(body)["conversations"]
                  if r["session_id"] == "s1")
        assert abs(s1["cost_usd"] - skewed) < 1e-9, s1

        # (b) FILTER reads the SAME stored column. cost_min=500 admits s1
        # (stored 999 >= 500); cost_max=1 excludes it (stored 999 > 1) — display
        # and filter now agree because they read one column.
        status, body = _get(port, "/api/conversations?cost_min=500")
        assert status == 200, (status, body)
        sids = {r["session_id"] for r in json.loads(body)["conversations"]}
        assert "s1" in sids, sids

        status, body = _get(port, "/api/conversations?cost_max=1")
        assert status == 200, (status, body)
        rows = json.loads(body)["conversations"]
        sids = {r["session_id"] for r in rows}
        assert "s1" not in sids, sids
    finally:
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


def test_conversation_store_open_failure_is_private_and_truthful(
    tmp_path, monkeypatch, capsys,
):
    """A transcript-store open failure must stay a clean, privacy-safe 500.

    Every JSON conversation route shares the same open/query/close scaffold.
    The response names the transcript store generically, points diagnosis to
    ``doctor``, and never misattributes the failure to cache.db or reflects the
    raw exception. Detailed diagnostics remain in the server log.
    """
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        conv = sys.modules["_cctally_dashboard_conversation"]

        def _boom(account=None):
            raise OSError(
                "unable to open /private/secret/conversations.db; "
                "SQL: SELECT content FROM conversation_messages"
            )

        # #787 / #861: every conversation read opens its query session
        # through this one seam, resolved at request time. Seeding already
        # happened in _boot, so this only affects the live request path.
        monkeypatch.setattr(conv, "_open_conversation_session", _boom)
        routes = (
            "/api/conversations",
            "/api/conversations?source=claude",
            "/api/conversations/facets",
            "/api/conversations/facets?source=claude",
            "/api/conversation/s1",
            "/api/conversation/search?q=token",
            "/api/conversation/search?source=claude&q=token",
            "/api/conversation/s1/find?q=token",
            "/api/conversation/s1/outline",
            "/api/conversation/s1/prompts",
            "/api/conversation/s1/payload?tool_use_id=t1&which=result",
            "/api/conversation/s1/export",
            "/api/conversation/s1/anon-map",
            "/api/conversation/s1/media?tool_use_id=t1&index=0",
        )
        for route in routes:
            status, body = _get(port, route)
            assert status == 500, (route, status, body)
            payload = json.loads(body)
            assert payload == {
                "error": "transcript store unavailable; "
                "run cctally doctor for details"
            }, (route, payload)
            body_text = body.decode("utf-8")
            assert "cache" not in body_text.casefold(), (route, body)
            assert "/private/secret" not in body_text, (route, body)
            assert "SELECT content" not in body_text, (route, body)
    finally:
        stop(srv, srv._test_thread)
    server_log = capsys.readouterr().err
    assert "/private/secret/conversations.db" in server_log
    assert "SELECT content FROM conversation_messages" in server_log


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
        stop(srv, srv._test_thread)


def test_api_data_codex_labels_follow_the_request_gate_without_contamination(
    tmp_path, monkeypatch,
):
    ns = load_script()
    srv = _boot(
        ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False,
        with_codex_label=True,
    )
    try:
        port = srv.server_address[1]

        def labels(payload):
            # #583 S3 §4: `sources.all.data.providers` publishes null for both
            # members, so the physical `sources.codex` entry is the ONE place
            # the label travels. The mirrored read this used to make resolved
            # to the SAME row object, so it proved nothing the first read does
            # not; the mirror's nulling is asserted here instead.
            sources = payload["sources"]
            assert sources["all"]["data"]["providers"] == {
                "claude": None, "codex": None,
            }
            return (
                sources["codex"]["data"]["sessions"]["rows"][0].get("label"),
            )

        status, body = _get(port, "/api/data")
        opened = json.loads(body)
        assert status == 200 and opened["transcriptsEnabled"] is True
        assert labels(opened) == ("Private first prompt",)

        status, body = _get(port, "/api/data", host="machine.local:8789")
        closed = json.loads(body)
        assert status == 200 and closed["transcriptsEnabled"] is False
        assert labels(closed) == (None,)

        status, body = _get(port, "/api/data")
        reopened = json.loads(body)
        assert status == 200 and labels(reopened) == ("Private first prompt",)
    finally:
        stop(srv, srv._test_thread)


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

    cache = ns["open_conversations_db"]()
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
        stop(srv, srv._test_thread)


def test_payload_route_source_gone_returns_410(tmp_path, monkeypatch):
    """A row whose source_path points at a missing/rotated JSONL -> 410 (the
    documented consequence of storing only capped text)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        cache = ns["open_conversations_db"]()
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
        stop(srv, srv._test_thread)


def _seed_background_payload_rows(ns, tmp_path, *, with_notification=True):
    """A backgrounded-MCP call: an mcp tool_use, its harness placeholder
    tool_result, and (optionally) the notification META row whose source_path
    points at a REAL attachment line on disk."""
    import sys as _sys
    _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
    import _lib_conversation as _lc

    task_id = "kravg1b9s"
    big = "Z" * 40000
    body = ("<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n<status>completed</status>\n"
            "<summary>MCP task kravg1b9 done.</summary>\n"
            '<result>{"threadId":"t1","content":"' + big + '"}</result>\n'
            "</task-notification>")
    obj = {"type": "attachment", "uuid": "bg_notif", "sessionId": "sbg",
           "timestamp": "2026-07-30T20:51:16.312Z",
           "attachment": {"type": "queued_command",
                          "commandMode": "task-notification", "prompt": body}}
    p = tmp_path / "background.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")

    placeholder = (
        f'MCP tool "codex/codex" is still running after 120s. It was moved to '
        f'the background as task {task_id} and keeps running; you\'ll receive a '
        f'notification with the result when it completes. To stop it, use '
        f'TaskStop with task_id "{task_id}".')
    # The call's own JSONL really exists on disk, so the PUBLIC which='result'
    # path would happily serve the placeholder text with a 200. Without this the
    # 410 assertion below would pass vacuously (a missing file is 410 anyway).
    call_path = tmp_path / "bg-call.jsonl"
    call_line0 = json.dumps({"message": {"content": [
        {"type": "tool_use", "id": "toolu_bg", "name": "mcp__codex__codex",
         "input": {"prompt": "review"}}]}})
    call_line1 = json.dumps({"message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_bg",
         "content": [{"type": "text", "text": placeholder}], "is_error": False}]}})
    with open(call_path, "w", encoding="utf-8") as fh:
        call_off0 = fh.tell()
        fh.write(call_line0 + "\n")
        call_off1 = fh.tell()
        fh.write(call_line1 + "\n")
    cache = ns["open_conversations_db"]()
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sbg", "bg_a", None, str(call_path), call_off0, "2026-07-30T20:40:00Z", "assistant",
         "", json.dumps([{"kind": "tool_use", "name": "mcp__codex__codex",
                          "input_summary": "{}", "id": "toolu_bg",
                          "preview": "codex"}]),
         _MODEL, "mbg", "rbg", None, None, 0))
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("sbg", "bg_r", None, str(call_path), call_off1, "2026-07-30T20:42:00Z",
         "tool_result", "",
         json.dumps([{"kind": "tool_result", "text": placeholder,
                      "truncated": False, "full_length": len(placeholder),
                      "is_error": False, "tool_use_id": "toolu_bg"}]),
         None, None, None, None, None, 0))
    if with_notification:
        row = _lc.parse_message_row(obj, 0)
        cache.execute(
            "INSERT OR IGNORE INTO conversation_messages "
            "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
            " is_sidechain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("sbg", "bg_notif", None, str(p), 0, "2026-07-30T20:51:16.312Z",
             "meta", "", row.blocks_json, None, None, None, None, None, 0))
    cache.commit()
    cache.close()
    return "toolu_bg", '{"threadId":"t1","content":"' + big + '"}'


def test_payload_route_serves_a_recovered_background_result(tmp_path, monkeypatch):
    """A public which=result request for a backgrounded-MCP call is resolved
    through the INTERNAL background_result carrier and answered with the full
    text under the unchanged public which:"result" discriminant."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        tuid, full = _seed_background_payload_rows(ns, tmp_path)
        status, body = _get(
            port, f"/api/conversation/sbg/payload?tool_use_id={tuid}&which=result")
        assert status == 200, (status, body)
        payload = json.loads(body)
        assert payload["which"] == "result"
        assert payload["text"] == full
        assert payload["full_length"] == len(full)
        assert payload["is_error"] is False
        # background_result is INTERNAL: it is never an accepted input value.
        status, _ = _get(
            port,
            f"/api/conversation/sbg/payload?tool_use_id={tuid}&which=background_result")
        assert status == 400
    finally:
        stop(srv, srv._test_thread)


def test_payload_route_background_notification_gone_returns_410(tmp_path, monkeypatch):
    """A KNOWN background placeholder whose notification row is gone -> 410,
    distinct from the 404 an unknown tool_use_id returns."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]
        tuid, _full = _seed_background_payload_rows(
            ns, tmp_path, with_notification=False)
        status, body = _get(
            port, f"/api/conversation/sbg/payload?tool_use_id={tuid}&which=result")
        assert status == 410, (status, body)
        assert "error" in json.loads(body)
        status, _ = _get(
            port, "/api/conversation/sbg/payload?tool_use_id=toolu_absent&which=result")
        assert status == 404
    finally:
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


def test_sse_codex_labels_follow_each_connection_transcript_gate(
    tmp_path, monkeypatch,
):
    ns = load_script()
    srv = _boot(
        ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False,
        with_codex_label=True,
    )
    try:
        port = srv.server_address[1]
        HandlerCls = ns["DashboardHTTPHandler"]
        snapshot = _make_snapshot(ns, with_codex_label=True)
        HandlerCls.hub.publish(snapshot)

        opened = _first_sse_update_envelope(port)
        assert opened["transcriptsEnabled"] is True
        assert (
            opened["sources"]["codex"]["data"]["sessions"]["rows"][0]["label"]
            == "Private first prompt"
        )
        # #583 S3 §4: the mirror publishes null; the physical entry above is
        # the one copy the label is injected into.
        assert opened["sources"]["all"]["data"]["providers"] == {
            "claude": None, "codex": None,
        }

        closed = _first_sse_update_envelope(
            port, host="machine.local:8789",
        )
        assert closed["transcriptsEnabled"] is False
        assert "label" not in (
            closed["sources"]["codex"]["data"]["sessions"]["rows"][0]
        )
        assert closed["sources"]["all"]["data"]["providers"] == {
            "claude": None, "codex": None,
        }
    finally:
        stop(srv, srv._test_thread)


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
    c = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
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

    cache = ns["open_conversations_db"]()
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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        cache = ns["open_conversations_db"]()
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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        cache = ns["open_conversations_db"]()
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
        stop(srv, srv._test_thread)


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
    with ns["open_conversations_db"]() as conn:
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
    with ns["open_conversations_db"]() as conn:
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
    with ns["open_conversations_db"]() as conn:
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
    with ns["open_conversations_db"]() as conn:
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
    with ns["open_conversations_db"]() as conn:
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
    c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=PRESENCE_BACKSTOP_SECONDS)
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
        stop(srv, srv._test_thread)


# --- #717: filter_degraded reaches every shaping site ----------------------
# `_arm_backfill_pending` is defined once, further down beside the live-branch
# case that introduced it. A second copy here bound the LATER definition for
# every caller in this module, so this one was dead code an edit could not
# reach.


def test_the_unqualified_claude_facets_route_carries_the_flag(tmp_path,
                                                              monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, before = _get_json(srv, "/api/conversations/facets")
        assert st == 200 and "filter_degraded" not in before
        assert before["projects"], "the fixture must have projects to lose"
        _arm_backfill_pending(ns)
        st, body = _get_json(srv, "/api/conversations/facets")
        assert st == 200
        assert body["filter_degraded"] is True
        assert body["projects"] == []
        assert body["models"], "model counts do not read the rollup"
    finally:
        stop(srv, srv._test_thread)


def test_the_qualified_claude_facets_route_carries_the_flag(tmp_path,
                                                            monkeypatch):
    """Two reshaping sites sit between the producer and the client —
    `neutral_facets` keeps only `status` and `facets`, and
    `_handle_qualified_facets` rebuilds the response again — and each drops
    unknown keys."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, before = _get_json(
            srv, "/api/conversations/facets?source=claude")
        assert st == 200 and "filter_degraded" not in before
        _arm_backfill_pending(ns)
        st, body = _get_json(srv, "/api/conversations/facets?source=claude")
        assert st == 200
        assert body["filter_degraded"] is True, (
            "the flag was dropped by neutral_facets or by the route's own "
            f"reconstruction: {body}")
        # NOT emptied on this path, unlike the unqualified one: these facets
        # are derived from browse rows that `list_conversations` still
        # produces in full through its live GROUP-BY fallback. Emptying them
        # would hide projects that are present.
        assert body["facets"]["projects"], (
            "the qualified path's facets come from live-capable rows and must "
            "survive a pending rollup")
    finally:
        stop(srv, srv._test_thread)


def test_the_qualified_claude_browse_route_carries_the_flag(tmp_path,
                                                            monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, before = _get_json(srv, "/api/conversations?source=claude")
        assert st == 200 and "filter_degraded" not in before
        _arm_backfill_pending(ns)
        st, body = _get_json(srv, "/api/conversations?source=claude")
        assert st == 200 and body["filter_degraded"] is True
        assert body["facets"]["projects"]
        assert body["rows"], "the live fallback still serves the rail"
    finally:
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


def test_filter_bad_rebuild_is_400(tmp_path, monkeypatch):
    """A non-integer rebuild threshold is a hard 400."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?rebuild_min=lots")
        assert st == 400
    finally:
        stop(srv, srv._test_thread)


def test_filter_bad_date_is_400(tmp_path, monkeypatch):
    """A malformed date maps the date-helper ValueError to a 400."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        st, body = _get_json(srv, "/api/conversations?date_from=not-a-date")
        assert st == 400
    finally:
        stop(srv, srv._test_thread)


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
    with ns["open_conversations_db"]() as conn:
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
    cache = ns["open_conversations_db"]()
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
        stop(srv, srv._test_thread)


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
    with ns["open_conversations_db"]() as conn:
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
    cache = ns["open_conversations_db"](attach_cache=False)
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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)



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
        stop(srv, srv._test_thread)


# ---- #281 S4: anonymized export param + anon-map route ---------------------

def _seed_anon_session(ns):
    """Insert a session whose rendered prose carries an OBSERVED identity token
    (the seeded cwd) so the anonymized export visibly differs from raw."""
    cache = ns["open_conversations_db"]()
    cache.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,parent_uuid,source_path,byte_offset,timestamp_utc,"
        " entry_type,text,blocks_json,model,msg_id,req_id,cwd,git_branch,"
        " is_sidechain) VALUES('sanon','ah1',NULL,'anon.jsonl',0,"
        "'2026-06-03T00:00:00Z','human','edited /home/u/proj/secret.py here',"
        "'[]',NULL,NULL,NULL,'/home/u/proj','main',0)")
    cache.commit()
    cache.close()


def test_conversation_export_anonymize_param(tmp_path, monkeypatch):
    """``?anonymize=1`` scrubs the body; absent/``0`` are byte-identical raw;
    blank/duplicate/non-0-1 are strict-parse 400s BEFORE the kernel (spec §5)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        _seed_anon_session(ns)
        port = srv.server_address[1]
        base = "/api/conversation/sanon/export?scope=all"

        # Raw (no param) and anonymize=0 are byte-identical (R4 raw-unchanged).
        st, ct, raw = _get_ct(port, base)
        assert st == 200 and ct.startswith("text/markdown"), (st, ct)
        st0, _, raw0 = _get_ct(port, base + "&anonymize=0")
        assert st0 == 200 and raw0 == raw
        assert b"/home/u/proj" in raw               # identity token present in raw

        # anonymize=1 → scrubbed; the identity token is gone; body differs.
        st1, ct1, anon = _get_ct(port, base + "&anonymize=1")
        assert st1 == 200 and ct1.startswith("text/markdown"), (st1, ct1)
        assert b"/home/u/proj" not in anon
        assert anon != raw

        # Strict parse (before the kernel): blank / duplicate / non-0-1 → 400.
        for bad in ("&anonymize=", "&anonymize=1&anonymize=0",
                    "&anonymize=true", "&anonymize=2"):
            st, _, _ = _get_ct(port, base + bad)
            assert st == 400, bad
    finally:
        stop(srv, srv._test_thread)


def test_conversation_anon_map_route(tmp_path, monkeypatch):
    """``/api/conversation/<sid>/anon-map`` returns the plan_to_wire shape;
    404 unknown sid; 403 under the LAN-hostname gate; and route-ordering proof
    that the suffix does NOT fall through to the detail catch-all (spec §5)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    try:
        port = srv.server_address[1]

        st, body = _get(port, "/api/conversation/s1/anon-map")
        assert st == 200, (st, body)
        wire = json.loads(body)
        assert set(wire) == {"tokens", "patterns"}
        assert all(set(t) == {"text", "replacement", "bounded"}
                   for t in wire["tokens"])
        assert all(set(p) == {"name", "source", "ignoreCase", "keepGroup1"}
                   for p in wire["patterns"])
        assert wire["patterns"], "production secret patterns must be present"
        # Precedence: NOT the detail catch-all (items/page) nor the export md.
        assert "items" not in wire and "page" not in wire

        # Unknown sid → 404 (sibling envelope discipline).
        st, _ = _get(port, "/api/conversation/does-not-exist/anon-map")
        assert st == 404

        # Privacy gate reused: LAN hostname + expose=False → 403.
        st, _ = _get(port, "/api/conversation/s1/anon-map",
                     host="machine.local:8789")
        assert st == 403
    finally:
        stop(srv, srv._test_thread)


# ── live-tail accounting (the viewer's cost comes from the accounting store) ──


def _conversation_module():
    return load_script()["_load_sibling"]("_cctally_dashboard_conversation")


def test_the_live_tail_accounting_helper_targets_only_the_changed_paths(
    monkeypatch,
):
    """The tail advances exactly what it saw grow, and always closes the store.

    A live tail resolves the set of source files that changed this cycle. The
    accounting ingest must be targeted at that set: a bare walk here would put
    the whole provider estate on the SSE cycle, which is the cost the dashboard
    frontier exists to avoid.
    """
    mod = _conversation_module()
    closed = []

    class _Conn:
        def close(self):
            closed.append(True)

    conn = _Conn()

    @contextlib.contextmanager
    def _writer(store):
        seen["store"] = store
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(mod, "_request_writer", _writer)
    seen = {}

    def _sync(active, only_paths):
        seen["conn"] = active
        seen["paths"] = only_paths
        return sys.modules["_cctally_cache"].RequestIngestResult(
            ingested=True, pending=frozenset())

    class _Handler:
        def log_error(self, *_args):
            raise AssertionError("a successful sync must log nothing")

    mod._advance_live_tail_accounting(
        _Handler(), ["/a.jsonl", "/b.jsonl"], _sync, "codex")

    assert seen["store"] == "cache"
    assert seen["conn"] is conn
    assert seen["paths"] == {"/a.jsonl", "/b.jsonl"}
    assert closed == [True], "the accounting connection was left open"


@pytest.mark.parametrize("failing", ["open", "sync"])
def test_a_failed_live_tail_accounting_sync_never_breaks_the_stream(
    monkeypatch, failing,
):
    """Cost is an enhancement to the tail, never a precondition for it.

    Stalling the turns a reader is watching is strictly worse than showing them
    beside a stale figure, so both the store open and the ingest must be inside
    the guard. The injected failure is deliberately not a ``sqlite3`` error:
    narrowing the catch to database errors would let an ``OSError`` from opening
    the store escape and kill the stream.
    """
    mod = _conversation_module()
    closed = []

    class _Conn:
        def close(self):
            closed.append(True)

    @contextlib.contextmanager
    def _writer(_store):
        if failing == "open":
            raise RuntimeError("accounting store unavailable")
        conn = _Conn()
        try:
            yield conn
        finally:
            conn.close()

    def _sync(_active, _only_paths):
        raise RuntimeError("forced ingest failure")

    monkeypatch.setattr(mod, "_request_writer", _writer)
    logged = []

    class _Handler:
        def log_error(self, fmt, *args):
            logged.append(fmt % args)

    mod._advance_live_tail_accounting(_Handler(), ["/a.jsonl"], _sync, "codex")

    assert logged, "a swallowed accounting failure must still be logged"
    assert "accounting sync failed" in logged[0]
    if failing == "sync":
        assert closed == [True], "the connection leaked on the failure path"


def test_every_live_tail_ingest_advances_accounting():
    """No live-tail route may advance the transcript alone.

    Claude turn cost is read from ``session_entries`` and Codex from
    ``codex_session_entries``, both in the accounting store, so a route that
    ingests only the transcript streams new turns whose cost never moves. The
    count is pinned as well as the predicate: a fourth route added later fails
    here rather than passing because the three known ones are still correct.
    """
    import ast

    mod = _conversation_module()
    tree = ast.parse(pathlib.Path(mod.__file__).read_text())
    ingests = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_ingest"
    ]
    assert len(ingests) == 3, (
        "the live-tail route set changed; give the new route an accounting "
        f"advance and update this count: {[n.lineno for n in ingests]}"
    )
    for fn in ingests:
        called = {
            node.func.id for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called & {"_advance_live_tail_accounting", "sync_codex_cache"}, (
            f"the _ingest at line {fn.lineno} advances no accounting store"
        )


# --- #780: the read-only reader opener -------------------------------------
#
# `apply_policy` emits `PRAGMA auto_vacuum=…` then `PRAGMA journal_mode=…`,
# both write-capable, so a read route must not route through it. `mode=ro`
# still permits the TEMP views account scoping needs; `PRAGMA query_only=ON`
# does not, so it is deliberately never set.


@pytest.fixture
def readonly_ns(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_conversations_db"]()
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
        " text,blocks_json,account_key) "
        "VALUES('ro-s1','ro-u1','/ro.jsonl',0,'2026-06-01T00:00:00Z',"
        "'assistant','readable','[]',NULL)")
    conn.commit()
    conn.close()
    return ns


def _readonly_write_capable(sql: str) -> bool:
    head = sql.strip().split(None, 2)
    if not head:
        return False
    verb = head[0].upper()
    if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
                "REPLACE", "VACUUM", "REINDEX", "COMMIT", "BEGIN"}:
        # A TEMP CREATE is the one permitted write: account scoping needs it,
        # and it never reaches `main`.
        return "TEMP" not in sql.upper() and "TEMPORARY" not in sql.upper()
    if verb == "PRAGMA":
        lowered = sql.lower()
        return ("auto_vacuum" in lowered and "=" in lowered) or (
            "journal_mode" in lowered and "=" in lowered)
    return False


def test_the_readonly_opener_executes_no_write_capable_statement(readonly_ns):
    ns = readonly_ns
    cache = ns["_cctally_cache"]
    store_mod = cache._cctally_store
    seen: list[str] = []
    previous = store_mod._TRACE_HOOK
    store_mod._TRACE_HOOK = seen.append
    try:
        conn = cache.open_conversations_db_readonly()
    finally:
        store_mod._TRACE_HOOK = previous
    try:
        offenders = [sql for sql in seen if _readonly_write_capable(sql)]
        assert offenders == [], (
            f"the read-only opener ran write-capable statements: {offenders}")
        assert seen, "the trace hook must have been installed"
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_readonly_opener_runs_none_of_the_mutating_open_steps(
        readonly_ns, monkeypatch):
    ns = readonly_ns
    cache = ns["_cctally_cache"]
    called: list[str] = []

    def _forbid(name):
        def _boom(*a, **k):
            called.append(name)
            raise AssertionError(f"the read-only opener called {name}")
        return _boom

    for target, name in (
        (cache, "open_cache_db"),
        (cache, "_run_pending_migrations"),
        (cache, "_import_legacy_conversation_rows"),
        (cache, "_ensure_codex_conversation_contract"),
        (cache, "_harden_conversation_sidecars"),
        (cache, "_conversations_open_guarded"),
        (cache._cctally_store, "apply_policy"),
        (cache._cctally_store, "open_index"),
        (cache._cctally_db_sib, "_apply_conversations_schema"),
    ):
        monkeypatch.setattr(target, name, _forbid(name))
    import os as _os
    monkeypatch.setattr(_os, "chmod", _forbid("os.chmod"))
    conn = cache.open_conversations_db_readonly()
    try:
        assert called == []
    finally:
        conn.close()


def test_the_readonly_connection_refuses_writes_to_main(readonly_ns):
    cache = readonly_ns["_cctally_cache"]
    conn = cache.open_conversations_db_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM conversation_messages")
    finally:
        conn.close()


def test_account_scoping_still_works_on_the_readonly_connection(readonly_ns):
    """The TEMP views survive `mode=ro`. This is why `PRAGMA query_only` is
    never set: it breaks exactly these writes."""
    cache = readonly_ns["_cctally_cache"]
    conn = cache.open_conversations_db_readonly()
    try:
        cache.scope_conversations_db_to_account(conn, "a" * 32)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_temp_master WHERE type='view'"
        ).fetchone()[0] > 0
    finally:
        conn.close()


def test_the_readonly_opener_refuses_a_schema_behind_store(readonly_ns,
                                                           monkeypatch):
    cache = readonly_ns["_cctally_cache"]
    store_mod = cache._cctally_store
    woken = []
    monkeypatch.setattr(cache, "SCHEMA_WAKE_HOOK", lambda: woken.append(1))
    monkeypatch.setattr(
        store_mod, "schema_state",
        lambda conn, store, *, schema="main": (
            "behind" if store == "conversations" else "current"))
    with pytest.raises(cache.SchemaBehind) as excinfo:
        cache.open_conversations_db_readonly()
    assert excinfo.value.reason == "schema_behind"
    assert woken == [1], "behind wakes a writer; it never migrates here"


def test_the_readonly_opener_fails_closed_on_a_schema_ahead_store(
        readonly_ns, monkeypatch):
    cache = readonly_ns["_cctally_cache"]
    store_mod = cache._cctally_store
    woken = []
    monkeypatch.setattr(cache, "SCHEMA_WAKE_HOOK", lambda: woken.append(1))
    monkeypatch.setattr(
        store_mod, "schema_state",
        lambda conn, store, *, schema="main": "ahead")
    with pytest.raises(cache.SchemaAhead) as excinfo:
        cache.open_conversations_db_readonly()
    assert excinfo.value.reason == "schema_ahead"
    assert woken == [], "ahead attempts no recovery"


def test_the_readonly_opener_gates_the_attached_cache_store_too(readonly_ns,
                                                                monkeypatch):
    cache = readonly_ns["_cctally_cache"]
    store_mod = cache._cctally_store
    seen = []

    def probing(conn, store, *, schema="main"):
        seen.append((store, schema))
        return "current" if store == "conversations" else "behind"

    monkeypatch.setattr(store_mod, "schema_state", probing)
    with pytest.raises(cache.SchemaBehind):
        cache.open_conversations_db_readonly()
    assert ("cache", "cache_db") in seen


def test_the_readonly_opener_declines_rather_than_queueing_behind_maintenance(
        readonly_ns):
    import fcntl as _fcntl

    cache = readonly_ns["_cctally_cache"]
    core = readonly_ns["_cctally_core"]
    holder = open(str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH), "a+")
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX)
        with pytest.raises(cache.MaintenanceInProgress) as excinfo:
            cache.open_conversations_db_readonly()
        assert excinfo.value.reason == "maintenance"
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        holder.close()


def test_every_reader_refusal_is_catchable_as_a_database_error(readonly_ns):
    """The route boundary already catches `sqlite3.DatabaseError` and
    `OSError`, so the new conditions must not escape it as a fresh 500."""
    cache = readonly_ns["_cctally_cache"]
    for cls in (cache.ConversationReaderUnavailable, cache.MaintenanceInProgress,
                cache.SchemaBehind, cache.SchemaAhead):
        assert issubclass(cls, sqlite3.DatabaseError)


# --- #780: every read route degrades instead of returning a 5xx -------------

_DEGRADING_ROUTES = (
    "/api/conversations",
    "/api/conversations/facets",
    "/api/conversation/search?q=hello",
    "/api/conversation/s1",
    "/api/conversation/s1/outline",
    "/api/conversation/s1/find?q=hello",
    "/api/conversation/s1/export",
)
# The live-tail preflight once took the full opener, which waits on the
# maintenance flock with no timeout, so a stream opened during a rebuild held a
# server thread for the rebuild's whole duration — measured at 716.9 s. §4a
# names the live-tail preflight among the routes that must fail soft; it reads
# through a query session (#787 / #861 spec 5.9) and answers with the same
# typed degraded envelope every other route serves. The cases below drive that
# with a REAL held flock.


@pytest.mark.parametrize("reason,factory", [
    ("maintenance", "MaintenanceInProgress"),
    ("schema_behind", "SchemaBehind"),
    ("schema_ahead", "SchemaAhead"),
])
def test_no_read_route_returns_a_5xx_when_the_reader_cannot_be_admitted(
        tmp_path, monkeypatch, reason, factory):
    """A rebuild, a reclaim pass and a schema-behind store all reach the routes
    through the same typed condition. Each used to arrive as a 500, which is
    exactly the maintenance-induced 5xx #780 exists to remove."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    cls = getattr(dash, factory)

    conv = sys.modules["_cctally_dashboard_conversation"]

    def refusing(*a, **k):
        raise cls("simulated")

    monkeypatch.setattr(conv, "_open_conversation_session", refusing)
    try:
        for route in _DEGRADING_ROUTES:
            st, body = _get_json(srv, route)
            assert st < 500, (route, st, body)
            assert st == 200, (route, st, body)
            assert body.get("status") == "degraded", (route, body)
            assert body.get("degraded_reason") == reason, (route, body)
            rendered = json.dumps(body)
            assert "sqlite" not in rendered.lower(), route
            assert "SELECT" not in rendered, route
            assert str(tmp_path) not in rendered, route
    finally:
        stop(srv, srv._test_thread)


def _get_raw(srv, path):
    """GET ``path`` and return ``(status, raw_bytes)``.

    `/export` answers Markdown, not JSON, so a status-and-bytes fetch is the
    only shape that covers the whole route set.
    """
    from http.client import HTTPConnection
    c = HTTPConnection("127.0.0.1", srv.server_address[1],
                       timeout=PRESENCE_BACKSTOP_SECONDS)
    c.request("GET", path, headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def _materialize_conversation_store(ns):
    """Create `conversations.db` and its maintenance lock, and put one session
    in it.

    `_boot` seeds `cache.db` only, so the transcript store is absent and the
    read-only opener answers `ConversationReaderUnavailable` — the deliberate
    first-run fallback to the full opener, which is not the steady state these
    cases are about.
    """
    conn = ns["open_conversations_db"]()
    conn.execute(
        "INSERT OR IGNORE INTO conversation_messages "
        "(session_id,uuid,source_path,byte_offset,timestamp_utc,entry_type,"
        " text,blocks_json,account_key) "
        "VALUES('s1','s1-u1','/s1.jsonl',0,'2026-06-01T00:00:00Z',"
        "'assistant','hello there','[]',NULL)")
    conn.commit()
    conn.close()


def _spy_on_the_reader(monkeypatch, dash):
    """Record the exception class each read admission actually raised.

    Wraps the REAL opener rather than replacing it, so the condition under
    test is the one the store is in and not a class the test chose.
    """
    conv = sys.modules["_cctally_dashboard_conversation"]
    real = conv._open_conversation_session
    raised = []

    def spying(*a, **k):
        try:
            return real(*a, **k)
        except BaseException as exc:
            raised.append(type(exc).__name__)
            raise

    monkeypatch.setattr(conv, "_open_conversation_session", spying)
    return raised


def test_a_real_rebuild_holding_the_maintenance_lock_degrades_every_route(
        tmp_path, monkeypatch):
    """Driven, not simulated. A rebuild takes the maintenance flock
    EXCLUSIVELY — `_prepare_claude_conversation_maintenance` is what does it —
    and this holds that same lock on a second descriptor, which is the state a
    reader meets during one. Nothing here names an exception class; the
    assertion reads back which class the real opener raised."""
    import fcntl as _fcntl

    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    core = ns["_cctally_core"]
    _materialize_conversation_store(ns)
    raised = _spy_on_the_reader(monkeypatch, dash)
    holder = open(str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH), "a+")
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX)
        for route in _DEGRADING_ROUTES:
            st, body = _get_json(srv, route)
            assert st == 200, (route, st, body)
            assert body.get("status") == "degraded", (route, body)
            assert body.get("degraded_reason") == "maintenance", (route, body)
        assert set(raised) == {"MaintenanceInProgress"}, (
            "a real held maintenance lock must reach the routes as the typed "
            f"maintenance condition, not as something else: {sorted(set(raised))}")
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        holder.close()
        stop(srv, srv._test_thread)


def test_a_real_schema_behind_store_degrades_every_route(tmp_path, monkeypatch):
    """Driven, not simulated: the store's own `user_version` is moved back, so
    the tri-state probe reads `behind` off the file rather than off a patch."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    cache = ns["_cctally_cache"]
    core = ns["_cctally_core"]
    # The wake-up would advance the store back to head on its background
    # thread and un-do the condition mid-run. The condition under test is the
    # route's answer while the store IS behind.
    monkeypatch.setattr(cache, "SCHEMA_WAKE_HOOK", None)
    _materialize_conversation_store(ns)
    raised = _spy_on_the_reader(monkeypatch, dash)
    writer = sqlite3.connect(str(core.CONVERSATIONS_DB_PATH))
    head = writer.execute("PRAGMA user_version").fetchone()[0]
    assert head > 0, "the fixture store must be at a real head to move back"
    writer.execute(f"PRAGMA user_version={head - 1}")
    writer.commit()
    writer.close()
    try:
        for route in _DEGRADING_ROUTES:
            st, body = _get_json(srv, route)
            assert st == 200, (route, st, body)
            assert body.get("status") == "degraded", (route, body)
            assert body.get("degraded_reason") == "schema_behind", (route, body)
        assert set(raised) == {"SchemaBehind"}, sorted(set(raised))
    finally:
        writer = sqlite3.connect(str(core.CONVERSATIONS_DB_PATH))
        writer.execute(f"PRAGMA user_version={head}")
        writer.commit()
        writer.close()
        stop(srv, srv._test_thread)


def test_a_real_reclaim_pass_does_not_degrade_or_5xx_any_route(tmp_path,
                                                               monkeypatch):
    """The reclaim half of §4d's claim, and the one that is NOT a refusal.

    Reclaim downgrades the maintenance flock to SHARED before it runs, so a
    reader is admitted; what it holds is the SQLite WRITE lock, for one chunk
    at a time. Every route must therefore keep serving normally — no 5xx and
    no degraded envelope — while a real `incremental_vacuum` chunk is in
    flight against the same file."""
    import fcntl as _fcntl
    import threading

    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    core = ns["_cctally_core"]
    _materialize_conversation_store(ns)
    raised = _spy_on_the_reader(monkeypatch, dash)

    holding = threading.Event()
    release = threading.Event()
    failed = []

    def reclaimer():
        conn = sqlite3.connect(str(core.CONVERSATIONS_DB_PATH), timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA incremental_vacuum(8)")
            holding.set()
            release.wait(30)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            failed.append(repr(exc))
            holding.set()
        finally:
            conn.close()

    # Shared maintenance admission, exactly as reclaim leaves it.
    shared = open(str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH), "a+")
    _fcntl.flock(shared, _fcntl.LOCK_SH)
    worker = threading.Thread(target=reclaimer, daemon=True)
    worker.start()
    try:
        assert holding.wait(30), "the reclaim thread never took the write lock"
        assert not failed, failed
        for route in _DEGRADING_ROUTES:
            st, raw = _get_raw(srv, route)
            assert st < 500, (route, st, raw[:200])
            assert b'"degraded"' not in raw, (
                "reclaim holds the WRITE lock and readers are `mode=ro`, so a "
                f"read must not be refused: {route} {raw[:200]!r}")
        assert raised == [], (
            f"no read admission may fail during reclaim: {raised}")
    finally:
        release.set()
        worker.join(30)
        _fcntl.flock(shared, _fcntl.LOCK_UN)
        shared.close()
        stop(srv, srv._test_thread)


_LIVE_TAIL_ROUTES = (
    "/api/conversation/s1/events",
    "/api/conversation/s1/events?account=acc-1",
    "/api/conversation/v1.claude.s1/events",
)


def test_a_real_held_maintenance_lock_degrades_the_live_tail_preflight(
        tmp_path, monkeypatch):
    """The live-tail preflight is the route §4a names and the route that was
    left blocking.

    The live-tail read connection once took the FULL opener, whose
    maintenance admission is a plain `LOCK_SH` with no timeout, so a stream
    opened during a rebuild waited for the rebuild — 716.9 s in the measured
    run — holding a server thread the whole time. It is now a query session
    under interactive admission (#787 / #861 spec 5.9). The lock here is a real `LOCK_EX` on the real
    maintenance file, which is the state `_prepare_claude_conversation_maintenance`
    leaves a reader in.
    """
    import fcntl as _fcntl

    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    core = ns["_cctally_core"]
    _materialize_conversation_store(ns)
    holder = open(str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH), "a+")
    try:
        _fcntl.flock(holder, _fcntl.LOCK_EX)
        for route in _LIVE_TAIL_ROUTES:
            # The claim "it did not queue behind the flock" needs no wall-clock
            # assertion: a request that queued cannot answer at all while the
            # lock is held, so `_get_json`'s own client budget is what fails,
            # and a degraded 200 is proof the preflight refused instead.
            st, body = _get_json(srv, route)
            assert st == 200, (route, st, body)
            assert body.get("status") == "degraded", (route, body)
            assert body.get("degraded_reason") == "maintenance", (route, body)
            rendered = json.dumps(body)
            assert "sqlite" not in rendered.lower(), route
            assert str(tmp_path) not in rendered, route
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        holder.close()
        stop(srv, srv._test_thread)


def test_a_schema_behind_reader_asks_a_writer_to_advance_the_store(
        tmp_path, monkeypatch):
    """`--no-sync` disables the self-heal and the conversation sync thread, so
    without an owner a reader would be degraded forever with no process willing
    to advance the schema. `cmd_dashboard` arms this hook in EVERY mode."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["_cctally_cache"]
    conn = ns["open_conversations_db"]()
    conn.close()
    woken = []
    monkeypatch.setattr(cache, "SCHEMA_WAKE_HOOK", lambda: woken.append(1))
    monkeypatch.setattr(
        cache._cctally_store, "schema_state",
        lambda conn, store, *, schema="main": "behind")
    with pytest.raises(cache.SchemaBehind):
        cache.open_conversations_db_readonly()
    assert woken == [1]


def test_the_wake_up_does_not_run_the_migration_on_the_calling_thread(
        tmp_path, monkeypatch):
    """`_gate_reader_schema` calls `SCHEMA_WAKE_HOOK` inline, so whatever the
    hook does happens on the request thread that is about to return a degraded
    envelope. Migration 009 plus its backfill was measured at 39.55 s on the
    production-shaped store, and both the spec ("The opener never migrates from
    a request thread") and this module's own retry comment forbid holding a
    request for it. The wake-up must dispatch and return."""
    import threading

    load_script()
    dash = sys.modules["_cctally_dashboard"]
    started = threading.Event()
    release = threading.Event()
    ran_on = {}

    def blocking_migration(**_kwargs):  # run_derivations (#787)
        ran_on["thread"] = threading.current_thread().ident
        started.set()
        release.wait(20)

    monkeypatch.setattr(
        dash, "_dashboard_startup_schema_migration", blocking_migration)
    dash._reset_schema_wake_state()
    caller = threading.current_thread().ident
    try:
        dash._wake_schema_writer()
        assert started.wait(10), "the wake-up never dispatched the migration"
        assert ran_on["thread"] != caller, (
            "the migration ran on the thread that asked for it")
    finally:
        release.set()
        dash._join_schema_wake_thread(timeout=10)


def test_concurrent_wake_ups_start_exactly_one_migration(tmp_path, monkeypatch):
    """Every schema-behind request calls the hook, so an unguarded dispatch
    would start one full open per request against a store that is already
    behind."""
    import threading

    load_script()
    dash = sys.modules["_cctally_dashboard"]
    started = threading.Event()
    release = threading.Event()
    runs = []
    lock = threading.Lock()

    def blocking_migration(**_kwargs):  # run_derivations (#787)
        with lock:
            runs.append(1)
        started.set()
        release.wait(20)

    monkeypatch.setattr(
        dash, "_dashboard_startup_schema_migration", blocking_migration)
    dash._reset_schema_wake_state()
    try:
        dash._wake_schema_writer()
        assert started.wait(10)
        for _ in range(8):
            dash._wake_schema_writer()
        assert runs == [1], (
            "a migration was already in flight; a second one adds contention "
            f"rather than progress: {runs}")
    finally:
        release.set()
        dash._join_schema_wake_thread(timeout=10)


def test_a_later_wake_up_still_dispatches_after_the_first_finishes(
        tmp_path, monkeypatch):
    """The guard suppresses a CONCURRENT start, not every future one — a store
    that falls behind again must still be able to wake a writer."""
    import threading

    load_script()
    dash = sys.modules["_cctally_dashboard"]
    runs = []

    def quick_migration(**_kwargs):  # run_derivations (#787)
        runs.append(threading.current_thread().ident)

    monkeypatch.setattr(
        dash, "_dashboard_startup_schema_migration", quick_migration)
    dash._reset_schema_wake_state()
    try:
        dash._wake_schema_writer()
        dash._join_schema_wake_thread(timeout=10)
        dash._wake_schema_writer()
        dash._join_schema_wake_thread(timeout=10)
        assert len(runs) == 2, runs
    finally:
        dash._join_schema_wake_thread(timeout=10)


def test_a_second_wake_up_cannot_slip_between_the_assignment_and_the_start(
        tmp_path, monkeypatch):
    """The guard is `_SCHEMA_WAKE_THREAD is not None and is_alive()`, and
    `is_alive()` is False for a thread that has been constructed and assigned
    but not yet started.

    The assignment happened under `_SCHEMA_WAKE_LOCK` and `start()` happened
    outside it, so a second caller arriving in that window saw a non-None,
    not-alive thread and passed the guard — dispatching the second 39.55-second
    migration the guard exists to prevent. The window is opened here for real,
    by holding the first `start()` until the second call has run.
    """
    import threading

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = sys.modules["_cctally_dashboard"]
    runs = []
    runs_lock = threading.Lock()
    finish_migration = threading.Event()

    def blocking_migration(**_kwargs):  # run_derivations (#787)
        with runs_lock:
            runs.append(threading.current_thread().name)
        # The migration is still IN FLIGHT while the second caller reaches the
        # guard. Letting it finish first would make a second dispatch correct —
        # the guard is per-flight, which
        # `test_a_later_wake_up_still_dispatches_after_the_first_finishes`
        # covers — and this case would then pass for the wrong reason.
        finish_migration.wait(30)

    monkeypatch.setattr(
        dash, "_dashboard_startup_schema_migration", blocking_migration)
    dash._reset_schema_wake_state()

    plain_thread = threading.Thread
    first_start_entered = threading.Event()
    release_first_start = threading.Event()
    gated = {"used": False}

    class GatedStart(plain_thread):
        def start(self):
            if self.name == "cctally-schema-wake" and not gated["used"]:
                gated["used"] = True
                first_start_entered.set()
                release_first_start.wait(20)
            super().start()

    monkeypatch.setattr(dash.threading, "Thread", GatedStart)
    first = plain_thread(target=dash._wake_schema_writer, daemon=True)
    second = plain_thread(target=dash._wake_schema_writer, daemon=True)
    try:
        first.start()
        assert first_start_entered.wait(10), (
            "the first wake-up never reached start()")
        second.start()
        # The window: the first thread is constructed and assigned but not
        # started, so `is_alive()` is False. With the start outside the lock the
        # second caller passes the guard here; with it inside, the second caller
        # is blocked on the lock and gets no chance to. A second caller that
        # passed the guard has already returned by now, and one blocked on the
        # lock stays blocked however long this waits.
        # timing-budget: the short budget IS the claim.
        second.join(2.0)
        release_first_start.set()
        first.join(10)
        second.join(10)
        assert runs == ["cctally-schema-wake"], (
            "a second migration was dispatched into the window between the "
            f"assignment and the start: {runs}")
    finally:
        release_first_start.set()
        finish_migration.set()
        dash._join_schema_wake_thread(timeout=10)
        dash._reset_schema_wake_state()


def test_the_startup_schema_owner_runs_in_both_modes(tmp_path, monkeypatch):
    """The schema-only startup open is what `--no-sync` otherwise has no owner
    for. It opens and closes; it ingests nothing."""
    import ast

    load_script()
    dash_path = pathlib.Path(
        sys.modules["_cctally_dashboard"].__file__)
    tree = ast.parse(dash_path.read_text())
    cmd = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_dashboard"
    )
    calls = [
        node for node in ast.walk(cmd)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_dashboard_startup_schema_migration"
    ]
    assert len(calls) == 1, (
        "cmd_dashboard must advance the schema exactly once at startup")
    guarded = [
        node for node in ast.walk(cmd)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            and inner.func.id == "_dashboard_startup_schema_migration"
            for inner in ast.walk(node)
        )
    ]
    assert guarded == [], (
        "the startup schema owner must not sit behind an `if` — a --no-sync "
        "run is exactly the mode that has no other owner"
    )
    # ORDERING. Counting the call and refusing an `if` around it still admits a
    # call placed AFTER the server starts serving, which would leave every
    # request between startup and that line degraded. Pin it ahead of the
    # thread that runs `serve_forever`.
    serving = [
        node.lineno for node in ast.walk(cmd)
        if isinstance(node, ast.Attribute) and node.attr == "serve_forever"
    ]
    assert serving, "cmd_dashboard no longer starts the HTTP server here"
    assert calls[0].lineno < min(serving), (
        "the schema owner must run BEFORE the server begins serving: "
        f"{calls[0].lineno} vs {min(serving)}")


def test_the_startup_schema_owner_actually_advances_a_behind_store(
        tmp_path, monkeypatch):
    """The behavioural half. The AST case above pins where the call sits; this
    one pins that the call does the job — a store genuinely behind head is
    refused by the read-only opener, the owner runs, and the same opener is
    then admitted. Nothing here patches `schema_state`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["_cctally_cache"]
    dash = sys.modules["_cctally_dashboard"]
    core = ns["_cctally_core"]
    conn = ns["open_conversations_db"]()
    conn.close()

    writer = sqlite3.connect(str(core.CONVERSATIONS_DB_PATH))
    head = writer.execute("PRAGMA user_version").fetchone()[0]
    assert head > 0
    writer.execute(f"PRAGMA user_version={head - 1}")
    writer.commit()
    writer.close()

    monkeypatch.setattr(cache, "SCHEMA_WAKE_HOOK", None)
    with pytest.raises(cache.SchemaBehind):
        cache.open_conversations_db_readonly()

    dash._dashboard_startup_schema_migration()

    reader = cache.open_conversations_db_readonly()
    try:
        assert reader.execute(
            "SELECT COUNT(*) FROM conversation_messages").fetchone() is not None
    finally:
        reader.close()


# --- #787 / #861 spec 5.8: one transport mapping for every reader family -----
#
# Detects a conversation reader that answers a store state with the wrong HTTP
# status or degraded reason, or that creates or migrates a store from a request
# thread (an absent store used to be created by the request's fallback, D3).
# Each row drives a real store state where one is reachable from a test; the
# legacy bridge is reached through its predicate, which the session itself
# reads. The routes are the six reader families of the 5.8 table.

_TRANSPORT_ROWS = (
    # state, degraded_reason (sync), degraded_reason (--no-sync), HTTP status
    ("maintenance", "maintenance", "maintenance", 200),
    ("busy", "maintenance", "maintenance", 200),
    ("legacy_bridge_pending", "maintenance", "legacy_bridge_pending", 200),
    ("schema_behind", "schema_behind", "schema_behind", 200),
    ("schema_ahead", "schema_ahead", "schema_ahead", 200),
    ("absent", "unavailable", "unavailable", 200),
    ("recovery_pending", "unavailable", "unavailable", 200),
    ("unavailable", "unavailable", "unavailable", 200),
    ("corrupt", None, None, 500),
)

#: (route family, log label of its degraded shape, path builder)
_TRANSPORT_ROUTES = (
    ("browse", "/api/conversations", lambda tok: "/api/conversations"),
    ("detail", "/api/conversation", lambda tok: "/api/conversation/s1"),
    ("search", "/api/conversation/search",
     lambda tok: "/api/conversation/search?q=hello"),
    ("outline producer", "/api/conversation/outline-transfer",
     lambda tok: f"/api/conversation/outline-transfer/{tok}?offset=0"),
    ("bare live-tail", "/api/conversation/events",
     lambda tok: "/api/conversation/s1/events"),
    ("qualified live-tail", "/api/conversation/events",
     lambda tok: "/api/conversation/v1.claude.s1/events"),
)


def _client_retryable(reason):
    """The retry remedy the client offers for ``reason`` (its own table), so
    the matrix checks the 5.8 retryable column against what a reader sees."""
    import re
    src = (pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "web"
           / "src" / "lib" / "conversationTransport.ts").read_text()
    match = re.search(
        r"\b%s: \{\s*reason: '%s',.*?retryable: (true|false)" % (reason, reason),
        src, re.S)
    if match is None:
        match = re.search(
            r"CONVERSATION_DEGRADED_FALLBACK\b[^=]*= \{.*?retryable: (true|false)",
            src, re.S)
    return match.group(1) == "true"


def _induce_store_state(state, ns, monkeypatch):
    """Put the stores into ``state``; returns a cleanup callable."""
    import fcntl as _fcntl
    import os as _os

    core = ns["_cctally_core"]
    cache = ns["_cctally_cache"]
    db = sys.modules["_cctally_db"]
    conv_path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    monkeypatch.setattr(cache, "SCHEMA_WAKE_HOOK", None)
    if state == "maintenance":
        holder = open(str(core.CONVERSATIONS_LOCK_MAINTENANCE_PATH), "a+")
        _fcntl.flock(holder, _fcntl.LOCK_EX)

        def release():
            _fcntl.flock(holder, _fcntl.LOCK_UN)
            holder.close()
        return release
    if state == "busy":
        # A writer holding the file EXCLUSIVE: SQLite reports the store busy
        # to every reader for as long as it holds it.
        writer = sqlite3.connect(str(conv_path), timeout=0)
        writer.execute("PRAGMA locking_mode=EXCLUSIVE")
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("CREATE TABLE IF NOT EXISTS _t787_busy(x)")

        def release():
            writer.rollback()
            writer.close()
        return release
    if state == "legacy_bridge_pending":
        monkeypatch.setattr(cache, "conversation_legacy_bridge_pending",
                            lambda conn: True)
        return lambda: None
    if state in ("schema_behind", "schema_ahead"):
        writer = sqlite3.connect(str(conv_path))
        head = writer.execute("PRAGMA user_version").fetchone()[0]
        writer.execute(
            f"PRAGMA user_version={head - 1 if state == 'schema_behind' else head + 1}")
        writer.commit()
        writer.close()
        return lambda: None
    if state == "absent":
        for suffix in ("", "-wal", "-shm"):
            pathlib.Path(f"{conv_path}{suffix}").unlink(missing_ok=True)
        return lambda: None
    if state == "recovery_pending":
        marker = db._repair_marker_path(pathlib.Path(core.CACHE_DB_PATH))
        marker.write_text("{}")
        return lambda: marker.unlink(missing_ok=True)
    if state == "unavailable":
        app_dir = pathlib.Path(core.APP_DIR)
        mode = app_dir.stat().st_mode & 0o777
        _os.chmod(app_dir, 0o755)
        return lambda: _os.chmod(app_dir, mode)
    if state == "corrupt":
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{conv_path}{suffix}").unlink(missing_ok=True)
        conv_path.write_bytes(b"this is not a sqlite database " * 256)
        return lambda: None
    raise AssertionError(state)


@pytest.mark.parametrize("no_sync", [False, True], ids=["sync", "no-sync"])
@pytest.mark.parametrize("state,sync_reason,no_sync_reason,status",
                         _TRANSPORT_ROWS, ids=[r[0] for r in _TRANSPORT_ROWS])
def test_degraded_transport_matrix(tmp_path, monkeypatch, state, sync_reason,
                                   no_sync_reason, status, no_sync):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    dash = sys.modules["_cctally_dashboard"]
    conv = sys.modules["_cctally_dashboard_conversation"]
    core = ns["_cctally_core"]
    monkeypatch.setattr(ns["DashboardHTTPHandler"], "no_sync", no_sync)
    # Dashboard startup creates and migrates the stores before bind.
    dash._dashboard_startup_schema_migration()
    port = srv.server_address[1]
    st, raw = _get(port, "/api/conversation/s1/outline?progressive=1")
    assert st == 200, raw
    token = json.loads(raw)["transfer"]["token"]
    expected_reason = no_sync_reason if no_sync else sync_reason
    cleanup = _induce_store_state(state, ns, monkeypatch)
    try:
        for family, label, build in _TRANSPORT_ROUTES:
            if status == 500 and family.endswith("live-tail"):
                # B6: an unscoped live-tail keeps main's passive keep-alive
                # stream for an unreadable store (the test below).
                continue
            st, raw = _get(port, build(token))
            assert st == status, (family, st, raw[:300])
            body = json.loads(raw)
            rendered = raw.decode("utf-8", "replace")
            assert "sqlite" not in rendered.lower(), (family, rendered)
            assert str(tmp_path) not in rendered, (family, rendered)
            if status == 500:
                assert set(body) == {"error"}, (family, body)
                continue
            assert body.get("status") == "degraded", (family, body)
            assert body.get("degraded_reason") == expected_reason, (family, body)
            shape = conv._CONVERSATION_DEGRADED_SHAPES.get(label, {})
            assert set(body) == set(shape) | {"status", "degraded_reason"}, (
                family, body)
            assert _client_retryable(expected_reason) is (
                expected_reason in ("maintenance", "unavailable")), (
                family, expected_reason)
        if state == "absent":
            # D3: a reader never creates the store; the owner does.
            assert not pathlib.Path(core.CONVERSATIONS_DB_PATH).exists()
    finally:
        cleanup()
        stop(srv, srv._test_thread)


def _sse_head(port, path, *, seconds=1.5):
    """``(status line, headers, body bytes read within ``seconds``)`` of one
    GET, over a raw socket so an open event stream does not block the read."""
    import socket as _socket
    s = _socket.create_connection(("127.0.0.1", port), timeout=seconds)
    try:
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   "Connection: close\r\n\r\n").encode())
        buf = b""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                chunk = s.recv(4096)
            except _socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    return lines[0], "\n".join(lines[1:]).lower(), body


@pytest.mark.parametrize("no_sync", [False, True], ids=["sync", "no-sync"])
@pytest.mark.parametrize("store", ["conversations", "cache"])
def test_an_unreadable_store_keeps_the_live_tail_a_passive_stream(
        tmp_path, monkeypatch, store, no_sync):
    """B6 (#787 / #861 revision 32): with a corrupt transcript or accounting
    store, `main` answers an unscoped conversation live-tail (bare or `v1.`)
    with a passive keep-alive event stream, the client's backstop tick
    still surfacing turns; a stream scoped to an account, bare or `v1.`,
    fails closed before any byte. Detects the live-tail answering the read
    routes' 500 envelope instead, the extra 500 the browser comparison saw,
    a passive stream that claims to be live (`event: ready`), and a scoped
    `v1.` stream served the unscoped passive stream."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    conv = sys.modules["_cctally_dashboard_conversation"]
    core = ns["_cctally_core"]
    monkeypatch.setattr(conv, "_LIVE_TAIL_KEEPALIVE", 0.2)
    monkeypatch.setattr(ns["DashboardHTTPHandler"], "no_sync", no_sync)
    monkeypatch.setattr(ns["_cctally_cache"], "SCHEMA_WAKE_HOOK", None)
    target = pathlib.Path(core.CONVERSATIONS_DB_PATH if store == "conversations"
                          else core.CACHE_DB_PATH)
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{target}{suffix}").unlink(missing_ok=True)
    target.write_bytes(b"this is not a sqlite database " * 256)
    port = srv.server_address[1]
    try:
        for path in ("/api/conversation/s1/events",
                     "/api/conversation/v1.claude.s1/events"):
            status, headers, body = _sse_head(port, path)
            assert status.split()[1] == "200", (path, status, body[:200])
            assert "content-type: text/event-stream" in headers, (path, headers)
            assert b": keep-alive" in body, (path, body[:200])
            assert b"event: ready" not in body, (path, body[:200])
        # F2: a stream scoped to an account, bare or `v1.`, fails closed.
        # Read through `_sse_head` so a wrongly served stream is a bounded
        # failure rather than a hung read.
        for path in ("/api/conversation/s1/events?account=acc-1",
                     "/api/conversation/v1.claude.s1/events?account=acc-1"):
            status, headers, body = _sse_head(port, path)
            assert status.split()[1] == "500", (path, status, body[:200])
            assert set(json.loads(body)) == {"error"}, (path, body[:200])
    finally:
        stop(srv, srv._test_thread)


@pytest.mark.parametrize("no_sync", [False, True], ids=["sync", "no-sync"])
def test_a_live_cache_repair_serves_what_main_served_without_cache_db(
        tmp_path, monkeypatch, no_sync):
    """Round 7 (#787 / #861, spec 5.11 and 5.12 N2): while another process
    holds a live cache.db repair marker, `main` attached cache.db raw to every
    conversation read and answered the live-tail with a passive stream after
    its full opener refused cache.db. conversations.db is not under repair, so
    the routes whose answer `main` read from conversations.db alone (the
    browse list and its facets, over an authoritative rollup, unscoped) are
    answered byte-identically to the pre-repair answer, and the live-tail is
    a passive keep-alive stream; the reader and the outline, which price
    turns and flag cache rebuilds from cache.db's session_entries, a list
    over a pending rollup backfill (its costs come from session_entries) and
    every account-scoped read keep the typed maintenance answer. Detects the
    conversation list and live-tail diverging from `main` during a repair,
    and any new connection to cache.db while the repair holds its marker."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    conv = sys.modules["_cctally_dashboard_conversation"]
    core = ns["_cctally_core"]
    import _cctally_store_access as access
    db = sys.modules["_cctally_db"]
    monkeypatch.setattr(conv, "_LIVE_TAIL_KEEPALIVE", 0.2)
    monkeypatch.setattr(ns["DashboardHTTPHandler"], "no_sync", no_sync)
    monkeypatch.setattr(ns["_cctally_cache"], "SCHEMA_WAKE_HOOK", None)
    conv_path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    cache_path = pathlib.Path(core.CACHE_DB_PATH)
    # A synced store: the browse rollup is authoritative.
    c = sqlite3.connect(conv_path)
    c.execute("DELETE FROM cache_meta "
              "WHERE key='conversation_sessions_backfill_pending'")
    c.commit()
    c.close()
    port = srv.server_address[1]
    served = ("/api/conversations", "/api/conversations/facets",
              "/api/conversations?source=claude",
              "/api/conversations/facets?source=claude")
    before = {path: _get(port, path) for path in served}
    for path, (st, raw) in before.items():
        assert st == 200 and "degraded_reason" not in json.loads(raw), (
            path, raw[:200])
    opened = []
    real_uri, real_connect = access._ro_uri, access._connect_ro

    def _uri(path):
        opened.append(pathlib.Path(path).name)
        return real_uri(path)

    def _connect(store, path, *a, **k):
        opened.append(pathlib.Path(path).name)
        return real_connect(store, path, *a, **k)

    monkeypatch.setattr(access, "_ro_uri", _uri)
    monkeypatch.setattr(access, "_connect_ro", _connect)
    claim, why = db._claim_repair_marker(cache_path)
    assert claim is not None, why
    try:
        for path in served:
            assert _get(port, path) == before[path], path
        for path in ("/api/conversation/s1", "/api/conversation/s1/outline",
                     "/api/conversation/sess-rebuild/outline",
                     "/api/conversations?account=acc-1",
                     "/api/conversations?source=codex"):
            st, raw = _get(port, path)
            assert st == 200, (path, raw[:200])
            assert json.loads(raw)["degraded_reason"] == "maintenance", (
                path, raw[:200])
        for path in ("/api/conversation/s1/events",
                     "/api/conversation/v1.claude.s1/events"):
            status, headers, body = _sse_head(port, path)
            assert status.split()[1] == "200", (path, status, body[:200])
            assert "content-type: text/event-stream" in headers, (path, headers)
            assert b": keep-alive" in body, (path, body[:200])
            assert b"event: ready" not in body, (path, body[:200])
        # F2: an account-scoped stream, bare or `v1.`, keeps its typed
        # maintenance answer before any SSE byte.
        for path in ("/api/conversation/s1/events?account=acc-1",
                     "/api/conversation/v1.claude.s1/events?account=acc-1"):
            status, headers, body = _sse_head(port, path)
            assert "text/event-stream" not in headers, (path, body[:200])
            assert status.split()[1] == "200", (path, status, body[:200])
            assert json.loads(body)["degraded_reason"] == "maintenance", (
                path, body[:200])
        c = sqlite3.connect(conv_path)
        c.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                  "VALUES('conversation_sessions_backfill_pending', '1')")
        c.commit()
        c.close()
        st, raw = _get(port, "/api/conversations")
        assert st == 200 and json.loads(raw)["degraded_reason"] == "maintenance", (
            raw[:200])
        assert cache_path.name not in opened, opened
        assert conv_path.name in opened, opened
    finally:
        db._release_repair_marker(cache_path, claim)
        stop(srv, srv._test_thread)


@pytest.mark.parametrize("path", ["/api/conversations",
                                  "/api/conversations/facets"])
def test_a_backfill_flag_flipping_mid_request_never_reaches_session_entries(
        tmp_path, monkeypatch, path):
    """F7 (#787 / #861 round 9): during a live cache repair the cache-free
    browse unit decides "the rollup is authoritative" and then runs the
    kernel, which reads the flag again. Both reads must see one snapshot:
    if a writer sets `conversation_sessions_backfill_pending` between them
    (a repair ending, its owner pass re-arming the backfill), the kernel
    must not take the live branch on a connection without cache.db. Detects
    the 500 `OperationalError: no such table: session_entries` that a
    second, fresh read of the flag produced."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    conv = sys.modules["_cctally_dashboard_conversation"]
    lcq = sys.modules["_lib_conversation_query"]
    core = ns["_cctally_core"]
    db = sys.modules["_cctally_db"]
    monkeypatch.setattr(ns["_cctally_cache"], "SCHEMA_WAKE_HOOK", None)
    conv_path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    cache_path = pathlib.Path(core.CACHE_DB_PATH)
    c = sqlite3.connect(conv_path)
    c.execute("DELETE FROM cache_meta "
              "WHERE key='conversation_sessions_backfill_pending'")
    c.commit()
    c.close()
    port = srv.server_address[1]
    st, before = _get(port, path)
    assert st == 200, before[:200]
    real = lcq._rollup_authoritative
    flipped, answers = [], []

    def flip_after_first(conn):
        answer = real(conn)
        answers.append(answer)
        if not flipped:
            flipped.append(answer)
            w = sqlite3.connect(conv_path, timeout=5)
            w.execute("INSERT OR REPLACE INTO cache_meta(key, value) "
                      "VALUES('conversation_sessions_backfill_pending', '1')")
            w.commit()
            w.close()
        return answer

    monkeypatch.setattr(lcq, "_rollup_authoritative", flip_after_first)
    claim, why = db._claim_repair_marker(cache_path)
    assert claim is not None, why
    try:
        st, raw = _get(port, path)
    finally:
        db._release_repair_marker(cache_path, claim)
        stop(srv, srv._test_thread)
    assert flipped == [True], "premise: the predicate read an authoritative rollup"
    assert len(answers) >= 2, ("non-vacuity: the kernel read the flag after "
                               "the predicate", answers)
    assert st == 200, (st, raw[:300])
    assert raw == before, raw[:300]


# ── #787 / #861 I3b: violations never reach a publication (spec 5.2) ───────


def _swallow_a_write(core):
    """A reader that swallows its own refusal: the pattern the latch exists
    for, since a broad ``except BaseException`` would otherwise hide it."""
    try:
        core.require_capability("WRITE", entry="swallowing_reader")
    except BaseException:  # noqa: BLE001
        pass


@pytest.mark.parametrize("loop", ["sync", "obligation"])
def test_owner_thread_survives_violation(tmp_path, monkeypatch, loop):
    """Review Focus 3. Detects an owner loop that dies on a violation (a
    BaseException no ``except Exception`` catches, so the dashboard's only
    drainer would end) or that publishes the violating pass: the first pass
    narrows, swallows a refusal and tries to publish, which refuses; the loop
    logs it, keeps running, and the next pass publishes."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = ns["_cctally_core"]
    dash = sys.modules["_cctally_dashboard"]
    ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    published, passes = [], []
    stop_event = threading.Event()
    attention = threading.Event()

    def one_pass():
        passes.append(core.current_authority().kind)
        marker = f"pass-{len(passes)}"
        if len(passes) == 1:
            with core.authority_scope("query", policy="interactive"):
                _swallow_a_write(core)
        import dataclasses
        ref.set(dataclasses.replace(ref.get(), last_sync_error=marker))
        published.append(marker)
        if len(passes) >= 2:
            stop_event.set()
            attention.set()
        else:
            attention.set()

    if loop == "sync":
        body = lambda: dash._dashboard_sync_loop(  # noqa: E731
            stop=stop_event, interval=0.05, run_iteration=one_pass)
    else:
        body = lambda: dash._dashboard_obligation_loop(  # noqa: E731
            stop=stop_event, attention=attention, interval=0.05,
            drain=one_pass, outstanding=lambda: True)
    thread = threading.Thread(target=dash._owner_thread_target(body),
                              daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "the owner loop never finished its passes"
    assert passes[:2] == ["process", "process"], passes
    assert published == ["pass-2"], published
    assert ref.get().last_sync_error == "pass-2"


def test_share_swallowed_violation_not_published(tmp_path, monkeypatch):
    """Detects a share render that publishes output built after a refused
    capability its reader swallowed: the route's query ceiling refuses the
    write, the latch records it, and the response writer answers the
    privacy-safe 500 instead of the rendered artifact."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = ns["_cctally_core"]
    share = sys.modules["_cctally_dashboard_share"]
    real_tz = share._share_resolved_display_tz

    def swallowing_tz(*a, **k):
        _swallow_a_write(core)
        return real_tz(*a, **k)

    monkeypatch.setattr(share, "_share_resolved_display_tz", swallowing_tz)
    H = ns["DashboardHTTPHandler"]
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.hub = ns["SSEHub"]()
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.run_sync_now_locked = staticmethod(lambda: None)
    H.no_sync = False
    H.display_tz_pref_override = None
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    srv._test_thread = start(srv)
    port = srv.server_address[1]
    payload = json.dumps({
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {"format": "svg", "theme": "light",
                    "reveal_projects": True, "no_branding": False,
                    "top_n": 5, "period": {"kind": "current"},
                    "project_allowlist": None,
                    "show_chart": True, "show_table": True},
    }).encode()
    try:
        c = HTTPConnection("127.0.0.1", port,
                           timeout=PRESENCE_BACKSTOP_SECONDS)
        c.putrequest("POST", "/api/share/render", skip_host=True)
        c.putheader("Host", f"127.0.0.1:{port}")
        c.putheader("Origin", f"http://127.0.0.1:{port}")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(len(payload)))
        c.endheaders()
        c.send(payload)
        r = c.getresponse()
        status, raw = r.status, r.read()
        c.close()
    finally:
        stop(srv, srv._test_thread)
    assert status == 500, raw[:200]
    assert json.loads(raw) == {"error": "internal error"}
    assert b"<svg" not in raw
