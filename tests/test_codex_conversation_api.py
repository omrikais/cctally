"""#294 S7 — dual-form conversation route layer end-to-end (spec §2 / §6.1).

Boots a real ``DashboardHTTPHandler`` against a fixture cache.db seeded with BOTH
a Codex provider (the ``tests/fixtures/codex-parity/v1`` corpus) and a genuine
Claude session, and drives every conversation route over real HTTP:

- lexical dual-form entity dispatch (v1 Codex + v1 Claude → the neutral envelope;
  bare UUID **and** bare non-UUID ids → the legacy path byte-identical; malformed
  ``v1.*`` → the neutral 404);
- strict ``?source=`` parsing on the three collection routes (blank / duplicate /
  ``all`` / unknown → 400; legacy-only axes with ``source`` present → 400; the
  qualified ``limit`` bounds + malformed-cursor 400s; the facets full rejection set);
- two-page browse pagination over real HTTP for BOTH providers (raw
  conversation-key cursors echo back unmodified);
- the §2.3 status → HTTP transport table (export's markdown leg, payload's 410
  ``gone``, the SSE preflight answered as JSON before any SSE bytes);
- the privacy gate 403 for qualified requests BEFORE any capability answer;
- the Codex payload ``block_key`` selector at the route boundary (disambiguated
  pairs, a call-id-less call whose ``which=output`` is 404, a structural-only
  mutation → 410 ``gone``, the containment guard against a symlink escape);
- Codex export scope rejection; the media capability gate;
- the two anonymization acceptance rows on the ``secret-canary`` fixture plus the
  mixed-database bare-Claude byte-stability regression (§3.6).

The deep payload magnitude/containment KERNEL invariants (1,000,000-char ceiling,
etc.) are proven at the kernel level in ``tests/test_codex_conversation_normalization.py``
(spec §6.2); this file certifies the ROUTE transport of those outcomes.
"""
from __future__ import annotations

import datetime as dt
import base64
import json
import pathlib
import re
import shutil
import sys
import threading
import urllib.parse as _u
from http.client import HTTPConnection

from conftest import load_script, redirect_paths_without_conversation_retention
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_conversation_dispatch as disp  # noqa: E402

CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"
_MODEL = "claude-opus-4-8"


# ── Claude seed ──────────────────────────────────────────────────────────────


def _claude_lines(sid, *, cwd="/home/u/proj-claude"):
    user = json.dumps({
        "type": "user", "uuid": f"{sid}-h1", "sessionId": sid,
        "timestamp": "2026-06-01T00:00:00.000Z", "cwd": cwd,
        "message": {"role": "user",
                    "content": [{"type": "text",
                                 "text": "synthetic claude prompt about widgets"}]}}) + "\n"
    asst = json.dumps({
        "type": "assistant", "uuid": f"{sid}-a1", "parentUuid": f"{sid}-h1",
        "sessionId": sid, "timestamp": "2026-06-01T00:00:05.000Z", "cwd": cwd,
        "requestId": f"{sid}-r1",
        "message": {"id": f"{sid}-m1", "model": _MODEL, "role": "assistant",
                    "content": [{"type": "text",
                                 "text": "synthetic claude reply about gadgets"}],
                    "usage": {"input_tokens": 10, "output_tokens": 20,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}}}) + "\n"
    return user + asst


# ── handler wiring ───────────────────────────────────────────────────────────


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
    import socketserver
    HandlerCls = ns["DashboardHTTPHandler"]
    HandlerCls.snapshot_ref = ns["_SnapshotRef"](_make_snapshot(ns))
    HandlerCls.hub = ns["SSEHub"]()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.cctally_host = bind
    HandlerCls.cctally_expose_transcripts = expose
    HandlerCls.no_sync = no_sync
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv.handle_error = lambda request, client_address: None
    srv._test_thread = start(srv)
    return srv


def _boot(ns, tmp_path, monkeypatch, *, codex_scenarios=("modern-full",),
          claude_sids=("s1",), no_sync=False):
    """Seed a Codex provider + Claude sessions, start a dashboard. Returns
    ``(srv, provider_root, codex_keys, rollouts)`` where ``codex_keys`` maps a
    scenario name → its opaque ``v1.`` conversation key."""
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))
    provider_root = tmp_path / "provider"
    rollouts = {}
    if codex_scenarios:
        for i, scen in enumerate(codex_scenarios):
            rollout = (provider_root / "sessions" / "2026" / "07"
                       / f"{15 + i:02d}" / f"{scen}.jsonl")
            rollout.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CORPUS / f"{scen}.jsonl", rollout)
            rollouts[scen] = rollout
        monkeypatch.setenv("CODEX_HOME", str(provider_root))
    for sid in claude_sids:
        proj = tmp_path / ".claude" / "projects" / f"-proj-{sid}"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text(_claude_lines(sid))
    conn = ns["open_cache_db"]()
    codex_keys = {}
    try:
        if codex_scenarios:
            ns["sync_codex_cache"](conn, rebuild=True)
        if claude_sids:
            ns["sync_cache"](conn, rebuild=True)
            import _cctally_cache as _cc
            _cc._recompute_conversation_sessions(conn)
            conn.commit()
        for scen in codex_scenarios:
            row = conn.execute(
                "SELECT conversation_key FROM codex_conversation_threads "
                "WHERE source_path LIKE ?", (f"%/{scen}.jsonl",)).fetchone()
            if row:
                codex_keys[scen] = row[0]
    finally:
        conn.close()
    conversations = ns["open_conversations_db"]()
    try:
        if codex_scenarios:
            ns["sync_codex_conversations"](conversations, rebuild=True)
        if claude_sids:
            ns["sync_claude_conversations"](conversations, rebuild=True)
    finally:
        conversations.close()
    srv = _wire_handler(ns, no_sync=no_sync)
    return srv, provider_root, codex_keys, rollouts


def _claude_key(sid="s1"):
    return disp._mint_claude_conversation_key(sid)


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def _get(port, path, *, host=None):
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
    ctype = r.getheader("Content-Type")
    c.close()
    return status, body, ctype


def _get_json(port, path, *, host=None):
    status, body, ctype = _get(port, path, host=host)
    parsed = json.loads(body) if body else None
    return status, parsed, ctype


def _entity_path(key, suffix=""):
    return f"/api/conversation/{_u.quote(key, safe='')}{suffix}"


# ── §2.1 lexical dual-form entity dispatch ───────────────────────────────────


def test_detail_v1_codex_returns_neutral_envelope(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body, ctype = _get_json(port, _entity_path(keys["modern-full"]))
        assert status == 200
        assert "application/json" in ctype
        assert body["status"] == "ok"
        assert body["conversation_key"] == keys["modern-full"]
        assert "items" in body           # neutral detail shape
    finally:
        stop(srv, srv._test_thread)


def test_detail_v1_claude_returns_neutral_envelope(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        key = _claude_key("s1")
        status, body, ctype = _get_json(port, _entity_path(key))
        assert status == 200
        assert "application/json" in ctype
        assert body["status"] == "ok"
        assert body["conversation_key"] == key
        assert "items" in body
    finally:
        stop(srv, srv._test_thread)


def test_detail_bare_uuid_is_legacy_path(tmp_path, monkeypatch):
    """A bare (non-v1) id never touches the resolver — the legacy Claude handler
    runs. An unknown session is the legacy plain-text 404, NOT a neutral JSON body."""
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body, ctype = _get(
            port, _entity_path("11111111-1111-4111-8111-111111111111"))
        assert status == 404
        # legacy send_error → text/html, not the neutral JSON envelope.
        assert b'"status"' not in body
    finally:
        stop(srv, srv._test_thread)


def test_detail_bare_non_uuid_id_stays_legacy(tmp_path, monkeypatch):
    """A bare non-UUID id like ``s1`` must reach the legacy handler (it is a real
    Claude session here) — proving the legacy path is not narrowed to UUID shape."""
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body, ctype = _get_json(port, _entity_path("s1"))
        assert status == 200
        # legacy detail carries session_id (snake); the neutral envelope carries
        # conversation_key. This is the LEGACY shape, unchanged.
        assert body.get("session_id") == "s1"
        assert "conversation_key" not in body
    finally:
        stop(srv, srv._test_thread)


def test_malformed_v1_key_is_neutral_404(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for suffix in ("", "/outline", "/prompts", "/find"):
            status, body, ctype = _get_json(
                port, _entity_path("v1.not-a-real-key", suffix))
            assert status == 404, suffix
            assert "application/json" in ctype, suffix
            assert body["status"] == "not_found", suffix
    finally:
        stop(srv, srv._test_thread)


def test_outline_prompts_find_v1_codex(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        key = keys["modern-full"]
        s, o, _ = _get_json(port, _entity_path(key, "/outline"))
        assert s == 200 and o["status"] == "ok" and "turns" in o
        s, p, _ = _get_json(port, _entity_path(key, "/prompts"))
        assert s == 200 and p["status"] == "ok" and "prompts" in p
        s, f, _ = _get_json(
            port, _entity_path(key, "/find") + "?q=Synthetic&kind=all")
        assert s == 200 and f["status"] == "ready"
        assert f["schema_version"] == 2
        assert f["semantics"] == "occurrence"
        assert "occurrences" in f["page"]
    finally:
        stop(srv, srv._test_thread)


def test_progressive_outline_v1_codex_reconstructs_exact_body(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        path = _entity_path(keys["modern-full"], "/outline")
        status, legacy_wire, _ = _get(port, path)
        assert status == 200
        status, initial, _ = _get_json(port, path + "?progressive=1")
        assert status == 200
        assert initial["progressive"] == 1
        assert "summary" not in initial
        transfer = initial["transfer"]
        assert "total" not in transfer and "sha256" not in transfer

        offset = 0
        chunks = []
        while True:
            status, chunk, _ = _get_json(
                port,
                "/api/conversation/outline-transfer/"
                f"{transfer['token']}?offset={offset}",
            )
            assert status == 200
            chunks.append(base64.b64decode(chunk["chunk"]))
            offset = chunk["next_offset"]
            if chunk["done"]:
                break
        assert b"".join(chunks) == legacy_wire
    finally:
        stop(srv, srv._test_thread)


def test_find_bad_kind_is_400_for_qualified(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        # title/files are search-only kinds; /find rejects them with a 400.
        s, _b, _ = _get_json(
            port, _entity_path(keys["modern-full"], "/find") + "?q=x&kind=title")
        assert s == 400
    finally:
        stop(srv, srv._test_thread)


def test_find_v1_codex_cursor_validation_and_staleness(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        path = _entity_path(keys["modern-full"], "/find")
        malformed, body, _ = _get_json(
            port, path + "?q=Synthetic&limit=1&cursor=garbage")
        assert malformed == 400
        assert body == {"error": "invalid find cursor"}
        for suffix in (
            "&limit=0",
            "&limit=201",
            "&limit=nope",
            "&direction=sideways",
            "&cursor=garbage&around=o1.anything",
        ):
            invalid, _body, _ = _get_json(port, path + "?q=Synthetic" + suffix)
            assert invalid == 400, suffix

        status, first, _ = _get_json(port, path + "?q=Synthetic&limit=1")
        assert status == 200
        cursor = first["page"]["next_cursor"]
        assert cursor is not None
        conn = ns["open_conversations_db"]()
        try:
            conn.execute(
                "UPDATE cache_meta SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='codex_find_projection_generation'"
            )
            conn.commit()
        finally:
            conn.close()
        stale, body, _ = _get_json(
            port,
            path + "?q=Synthetic&limit=1&cursor=" + _u.quote(cursor, safe=""),
        )
        assert stale == 409
        assert body == {"error": "stale find cursor"}
    finally:
        stop(srv, srv._test_thread)


def test_find_paging_params_are_codex_only_and_claude_bytes_stay_frozen(
    tmp_path, monkeypatch,
):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        qualified = _entity_path(_claude_key("s1"), "/find")
        bare = _entity_path("s1", "/find")
        suffix = "?q=synthetic&kind=all"
        paging = "&limit=not-an-int&cursor=garbage&direction=sideways&around=x"
        assert _get(port, qualified + suffix) == _get(port, qualified + suffix + paging)
        assert _get(port, bare + suffix) == _get(port, bare + suffix + paging)
    finally:
        stop(srv, srv._test_thread)


# ── §2.3 transport: export markdown leg + scope rejection ────────────────────


def test_export_v1_codex_markdown_leg(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body, ctype = _get(port, _entity_path(keys["modern-full"], "/export"))
        assert status == 200
        assert "text/markdown" in ctype
        assert body.startswith(b"#")            # markdown, not JSON
    finally:
        stop(srv, srv._test_thread)


def test_export_v1_codex_nondefault_scope_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body, ctype = _get_json(
            port, _entity_path(keys["modern-full"], "/export") + "?scope=chat")
        assert status == 400
        assert body["status"] == "validation_error"
        assert body["reason"] == "scope"
    finally:
        stop(srv, srv._test_thread)


def test_export_v1_claude_scopes_still_work(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        key = _claude_key("s1")
        status, body, ctype = _get(
            port, _entity_path(key, "/export") + "?scope=chat")
        assert status == 200
        assert "text/markdown" in ctype
    finally:
        stop(srv, srv._test_thread)


# ── §3.4 payload transport (route level) ─────────────────────────────────────


def _codex_tool_blocks(port, key):
    _s, detail, _c = _get_json(port, _entity_path(key))
    blocks = []
    for it in detail["items"]:
        for b in it.get("blocks", []):
            if b.get("kind") == "tool_call":
                blocks.append(b)
    return blocks


def test_payload_v1_codex_disambiguates_pairs(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        key = keys["modern-full"]
        by_call = {b.get("call_id"): b for b in _codex_tool_blocks(port, key)}
        # THREE identified call/output pairs, each addressed by its own block_key.
        for call_id in ("fn-1", "custom-1", "search-1"):
            bk = by_call[call_id]["block_key"]
            s_call, call, _ = _get_json(
                port, _entity_path(key, "/payload")
                + f"?block_key={_u.quote(bk)}&which=call")
            s_out, out, _ = _get_json(
                port, _entity_path(key, "/payload")
                + f"?block_key={_u.quote(bk)}&which=output")
            assert s_call == 200 and call["status"] == "ok" and call["content"]
            assert s_out == 200 and out["status"] == "ok" and out["content"]
        # the call-id-less web_search_call is call-only: which=output → 404.
        ws_bk = by_call[None]["block_key"]
        s_ws, ws, _ = _get_json(
            port, _entity_path(key, "/payload")
            + f"?block_key={_u.quote(ws_bk)}&which=output")
        assert s_ws == 404 and ws["status"] == "not_found"
    finally:
        stop(srv, srv._test_thread)


def test_payload_v1_codex_patch_event_is_full_and_structured(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=("session-b-card-wire",))
    try:
        port = srv.server_address[1]
        key = keys["session-b-card-wire"]
        _status, detail, _ctype = _get_json(port, _entity_path(key))
        blocks = [block for item in detail["items"] for block in item["blocks"]]
        direct = next(block for block in blocks if block.get("call_id") == "direct-patch")
        event_key = direct["detail"]["card"]["completion"]["event_block_key"]
        status, body, _ = _get_json(
            port, _entity_path(key, "/payload")
            + f"?block_key={_u.quote(event_key)}&which=event")
        assert status == 200 and body["status"] == "ok"
        assert body["card"]["has_diff"] is True
        assert body["card"]["files"][3]["move_path"] == "synthetic-new.txt"
        assert body["card"]["files"][0]["unified_diff"].startswith("--- /dev/null")
    finally:
        stop(srv, srv._test_thread)


def test_session_c_qualified_detail_and_payload_keep_exact_child_proof(
    tmp_path, monkeypatch,
):
    ns = load_script()
    scenarios = (
        "session-c-secondary-tools", "session-c-child-proven",
        "session-c-child-ambiguous-a", "session-c-child-ambiguous-b",
    )
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=scenarios)
    try:
        port = srv.server_address[1]
        parent = keys["session-c-secondary-tools"]
        child = keys["session-c-child-proven"]
        status, detail, _ = _get_json(port, _entity_path(parent))
        assert status == 200 and detail["status"] == "ok"
        calls = {
            block.get("call_id"): block
            for item in detail["items"] for block in item["blocks"]
            if block["kind"] == "tool_call"
        }
        assert calls["spawn-proven"]["detail"]["card"]["child_conversation"] == {
            "conversation_key": child,
            "role": "cctally_reviewer",
            "nickname": "Synthetic Child",
        }
        assert "child_conversation" not in \
            calls["spawn-ambiguous"]["detail"]["card"]
        web = calls["web-ok"]
        event_key = web["detail"]["card"]["completion"]["event_block_key"]
        event_status, event, _ = _get_json(
            port, _entity_path(parent, "/payload")
            + f"?block_key={_u.quote(event_key)}&which=event")
        assert event_status == 200 and event["status"] == "ok"
        assert event["card"]["results"][0]["url"] == "https://example.test/result"
        call_status, call, _ = _get_json(
            port, _entity_path(parent, "/payload")
            + f"?block_key={_u.quote(web['block_key'])}&which=call")
        assert call_status == 200 and call["status"] == "ok"
        assert "synthetic web query" in call["content"]
    finally:
        stop(srv, srv._test_thread)


def test_session_d_qualified_wire_and_raw_marker_payload(
    tmp_path, monkeypatch,
):
    scenario = "session-d-reasoning-lifecycle-markers"
    ns = load_script()
    srv, _root, keys, _rollouts = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=(scenario,))
    try:
        port = srv.server_address[1]
        key = keys[scenario]
        status, detail, _ = _get_json(port, _entity_path(key))
        assert status == 200 and detail["status"] == "ok"
        blocks = [block for item in detail["items"] for block in item["blocks"]]
        marker = next(block for block in blocks
                      if (block.get("detail") or {}).get("markers"))
        assert marker["text"] == "Synthetic closeout prose remains visible."
        assert "/synthetic/project" not in json.dumps(marker)
        payload_status, payload, _ = _get_json(
            port, _entity_path(key, "/payload")
            + f"?block_key={_u.quote(marker['block_key'])}&which=event")
        assert payload_status == 200 and payload["status"] == "ok"
        assert "::git-create-pr" in payload["content"]

        outline_status, outline, _ = _get_json(
            port, _entity_path(key, "/outline"))
        assert outline_status == 200
        assert outline["stats"]["items"] == detail["page"]["total"]
        find_status, found, _ = _get_json(
            port, _entity_path(key, "/find")
            + "?q=Inspecting%20synthetic%20state&kind=thinking")
        assert find_status == 200 and found["total"] == 2
        export_status, export_bytes, _ = _get(
            port, _entity_path(key, "/export"))
        assert export_status == 200
        assert "::git-create-branch" in export_bytes.decode("utf-8")
    finally:
        stop(srv, srv._test_thread)


def test_payload_gone_on_structural_mutation_is_410(tmp_path, monkeypatch):
    """A structural-only mutation of the source line (call_id changed, extracted
    content identical) → 410 gone at the route (validated against the stored full
    record, not the content digest)."""
    ns = load_script()
    srv, _root, keys, rollouts = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        key = keys["modern-full"]
        by_call = {b.get("call_id"): b for b in _codex_tool_blocks(port, key)}
        bk = by_call["fn-1"]["block_key"]
        # Mutate the function_call line's call_id in place (same byte length).
        path = rollouts["modern-full"]
        text = path.read_text(encoding="utf-8")
        assert '"call_id": "fn-1"' in text or '"call_id":"fn-1"' in text
        text = text.replace('"fn-1"', '"fnX1"')
        path.write_text(text, encoding="utf-8")
        s, body, _ = _get_json(
            port, _entity_path(key, "/payload")
            + f"?block_key={_u.quote(bk)}&which=call")
        assert s == 410
        assert body["status"] == "gone"
    finally:
        stop(srv, srv._test_thread)


def test_payload_v1_claude_uses_legacy_selector(tmp_path, monkeypatch):
    """A v1.claude payload request keeps the tool_use_id + which={input,result}
    selector; an unknown tool_use_id → 404 JSON (never a 500)."""
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        key = _claude_key("s1")
        s, body, _ = _get_json(
            port, _entity_path(key, "/payload") + "?tool_use_id=nope&which=result")
        assert s == 404
    finally:
        stop(srv, srv._test_thread)


# ── §3.5 media capability gate ───────────────────────────────────────────────


def test_media_v1_codex_capability_unsupported(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, body, ctype = _get_json(
            port, _entity_path(keys["modern-full"], "/media")
            + "?tool_use_id=x&index=0")
        assert s == 404
        assert "application/json" in ctype
        assert body["status"] == "capability_unsupported"
        assert body["source"] == "codex"
    finally:
        stop(srv, srv._test_thread)


def test_media_privacy_gate_403_before_capability(tmp_path, monkeypatch):
    """The Host/loopback privacy gate is the first act — a rebinding Host is a 403
    BEFORE any capability answer, even for a Codex media request."""
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, _b, _c = _get(
            port, _entity_path(keys["modern-full"], "/media") + "?tool_use_id=x&index=0",
            host="evil.example.com")
        assert s == 403
    finally:
        stop(srv, srv._test_thread)


def test_media_v1_unresolvable_is_neutral_404(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, body, _ = _get_json(
            port, _entity_path("v1.garbagekey", "/media") + "?tool_use_id=x&index=0")
        assert s == 404
        assert body["status"] == "not_found"
    finally:
        stop(srv, srv._test_thread)


# ── §2.5 privacy gate before capability for JSON entity routes ───────────────


def test_privacy_gate_403_before_qualified_answer(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for suffix in ("", "/outline", "/prompts", "/export", "/anon-map"):
            s, _b, _c = _get(
                port, _entity_path(keys["modern-full"], suffix),
                host="rebind.example.com")
            assert s == 403, suffix
    finally:
        stop(srv, srv._test_thread)


# ── §2.2 strict ?source= on collection routes ────────────────────────────────


def test_browse_source_rejections(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for qs in ("source=", "source=all", "source=both",
                   "source=claude&source=codex"):
            s, _b, _c = _get(port, f"/api/conversations?{qs}")
            assert s == 400, qs
    finally:
        stop(srv, srv._test_thread)


def test_browse_legacy_axis_with_source_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for axis in ("sort=recent", "offset=1", "date_from=2026-01-01",
                     "projects=x", "cost_min=1", "rebuild_min=1", "models=opus",
                     "q=x", "kind=all"):
            s, _b, _c = _get(port, f"/api/conversations?source=codex&{axis}")
            assert s == 400, axis
    finally:
        stop(srv, srv._test_thread)


def test_browse_limit_bounds(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for bad in ("0", "501", "-1", "abc", "1.5", ""):
            s, _b, _c = _get(port, f"/api/conversations?source=codex&limit={bad}")
            assert s == 400, bad
        s, _b, _c = _get(port, "/api/conversations?source=codex&limit=500")
        assert s == 200
    finally:
        stop(srv, srv._test_thread)


def test_browse_malformed_cursor_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, _b, _c = _get(
            port, "/api/conversations?source=codex&cursor=" + _u.quote("has space"))
        assert s == 400
    finally:
        stop(srv, srv._test_thread)


def test_browse_source_absent_is_legacy(tmp_path, monkeypatch):
    """No ?source= → the legacy browse response, unchanged."""
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, body, _c = _get_json(port, "/api/conversations")
        assert s == 200
        assert "conversations" in body      # legacy envelope key
    finally:
        stop(srv, srv._test_thread)


def test_facets_rejects_every_other_recognized_param(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for axis in ("project_key=x", "model=opus", "limit=5", "cursor=v1.x",
                     "q=hi", "kind=all", "sort=recent", "projects=x"):
            s, _b, _c = _get(port, f"/api/conversations/facets?source=codex&{axis}")
            assert s == 400, axis
    finally:
        stop(srv, srv._test_thread)


def test_facets_qualified_status_tagged_both_providers(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        for source in ("codex", "claude"):
            s, body, _c = _get_json(
                port, f"/api/conversations/facets?source={source}")
            assert s == 200, source
            assert body["status"] == "ok", source
            assert set(body["facets"]) == {"projects", "models"}, source
    finally:
        stop(srv, srv._test_thread)


def test_search_offset_with_source_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, _b, _c = _get(port, "/api/conversation/search?source=codex&q=x&offset=1")
        assert s == 400
    finally:
        stop(srv, srv._test_thread)


def test_search_malformed_cursor_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        # '!!!' is not valid base64url → the search cursor decode fails → 400.
        s, _b, _c = _get(
            port, "/api/conversation/search?source=codex&q=x&cursor="
            + _u.quote("!!!bad"))
        assert s == 400
    finally:
        stop(srv, srv._test_thread)


def test_search_qualified_returns_neutral_envelope(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, body, _c = _get_json(
            port, "/api/conversation/search?source=codex&q=Synthetic")
        assert s == 200
        assert body["status"] == "ok"
        assert "hits" in body
    finally:
        stop(srv, srv._test_thread)


def test_account_qualifier_reaches_collection_and_entity_queries(
        tmp_path, monkeypatch):
    """#347 route proof: fresh fixtures are stably unattributed, so an explicit
    account qualifier must preserve their browse/search/detail visibility. The
    cross-account exclusion itself is pinned by test_conversation_account_dimension.
    """
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, browse, _c = _get_json(
            port, "/api/conversations?source=codex&account=unattributed"
        )
        assert s == 200
        assert browse["status"] == "ok"
        assert [row["conversation_key"] for row in browse["rows"]] == [
            keys["modern-full"]
        ]

        s, search, _c = _get_json(
            port,
            "/api/conversation/search?source=codex&account=unattributed&q=Synthetic",
        )
        assert s == 200
        assert search["status"] == "ok"
        assert search["total"] > 0

        s, detail, _c = _get_json(
            port,
            _entity_path(keys["modern-full"]) + "?account=unattributed",
        )
        assert s == 200
        assert detail["status"] == "ok"
    finally:
        stop(srv, srv._test_thread)


def test_account_scoped_browse_keeps_unscoped_codex_project_identity(
        tmp_path, monkeypatch):
    """#497 HTTP regression: account focus partitions leaves, not projects."""
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch,
        codex_scenarios=("modern-full", "secret-canary"),
        claude_sids=(),
    )
    account_a = "a" * 32
    account_b = "b" * 32
    assignments = {
        keys["modern-full"]: account_a,
        keys["secret-canary"]: account_b,
    }
    conversations = ns["open_conversations_db"]()
    try:
        for conversation_key, account_key in assignments.items():
            conversations.execute(
                "UPDATE codex_conversation_messages SET account_key=? "
                "WHERE conversation_key=?",
                (account_key, conversation_key),
            )
        conversations.commit()
    finally:
        conversations.close()
    accounting = ns["open_cache_db"]()
    try:
        for conversation_key, account_key in assignments.items():
            accounting.execute(
                "UPDATE codex_session_entries SET account_key=? "
                "WHERE conversation_key=?",
                (account_key, conversation_key),
            )
        accounting.commit()
    finally:
        accounting.close()

    try:
        port = srv.server_address[1]
        status, merged, _ctype = _get_json(
            port, "/api/conversations?source=codex"
        )
        assert status == 200
        merged_projects = {
            row["conversation_key"]: (row["project_key"], row["project_label"])
            for row in merged["rows"]
        }

        for conversation_key, account_key in assignments.items():
            status, scoped, _ctype = _get_json(
                port,
                "/api/conversations?source=codex&account=" + account_key,
            )
            assert status == 200
            assert [row["conversation_key"] for row in scoped["rows"]] == [
                conversation_key
            ]
            assert (
                scoped["rows"][0]["project_key"],
                scoped["rows"][0]["project_label"],
            ) == merged_projects[conversation_key]
            status, facets, _ctype = _get_json(
                port,
                "/api/conversations/facets?source=codex&account=" + account_key,
            )
            assert status == 200
            assert facets["facets"]["projects"] == [{
                "project_key": merged_projects[conversation_key][0],
                "project_label": merged_projects[conversation_key][1],
                "count": 1,
            }]
    finally:
        stop(srv, srv._test_thread)


# ── §6.1 two-page browse pagination over real HTTP (both providers) ──────────


def _browse_page(port, source, cursor=None):
    path = f"/api/conversations?source={source}&limit=1"
    if cursor is not None:
        path += "&cursor=" + _u.quote(cursor, safe="")
    s, body, _c = _get_json(port, path)
    assert s == 200
    return body


def test_browse_two_page_codex_cursor_echoes(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(
        ns, tmp_path, monkeypatch,
        codex_scenarios=("modern-full", "nested-parent"), claude_sids=())
    try:
        port = srv.server_address[1]
        p1 = _browse_page(port, "codex")
        assert len(p1["rows"]) == 1
        cursor = p1["page"]["cursor"]
        assert cursor and cursor.startswith("v1.")       # raw conversation key
        p2 = _browse_page(port, "codex", cursor)
        assert len(p2["rows"]) == 1
        assert p1["rows"][0]["conversation_key"] != p2["rows"][0]["conversation_key"]
    finally:
        stop(srv, srv._test_thread)


def test_browse_two_page_claude_cursor_echoes(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=(),
        claude_sids=("s1", "s2"))
    try:
        port = srv.server_address[1]
        p1 = _browse_page(port, "claude")
        assert len(p1["rows"]) == 1
        cursor = p1["page"]["cursor"]
        assert cursor and cursor.startswith("v1.")
        p2 = _browse_page(port, "claude", cursor)
        assert len(p2["rows"]) == 1
        assert p1["rows"][0]["conversation_key"] != p2["rows"][0]["conversation_key"]
    finally:
        stop(srv, srv._test_thread)


# ── §4.3 search cursor round-trip over real HTTP (base64url external form) ────


def test_search_cursor_roundtrip_over_http(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, p1, _c = _get_json(
            port, "/api/conversation/search?source=codex&q=Synthetic&limit=1")
        assert s == 200 and p1["status"] == "ok"
        # The modern-full slice carries several 'Synthetic…' items (distinct
        # item_keys), so 'Synthetic' at limit=1 MUST yield a second page. Fail
        # loudly if the corpus ever thins below that guarantee.
        assert p1["total"] >= 2, p1["total"]
        cursor = p1["page"]["cursor"]
        assert cursor is not None
        # The external cursor is base64url over the kernel cursor — the raw NUL
        # separator never leaks into the wire form.
        assert "\x00" not in cursor
        s2, p2, _c = _get_json(
            port, "/api/conversation/search?source=codex&q=Synthetic&limit=1"
            "&cursor=" + _u.quote(cursor, safe=""))
        assert s2 == 200 and p2["status"] == "ok"
        first = p1["hits"][0]
        second = p2["hits"][0]
        assert (first["conversation_key"], first["item_key"]) != \
               (second["conversation_key"], second["item_key"])
    finally:
        stop(srv, srv._test_thread)


# ── SSE preflight at the route (JSON before any SSE bytes) ────────────────────


def test_events_v1_unresolvable_is_json_404(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, body, ctype = _get_json(port, _entity_path("v1.garbage", "/events"))
        assert s == 404
        assert "application/json" in ctype       # JSON, not an SSE stream
        assert body["status"] == "not_found"
    finally:
        stop(srv, srv._test_thread)


# ── §3.6 anonymization + mixed-database byte stability (route level) ─────────


def test_anon_map_v1_codex_includes_roots_and_scrubs(tmp_path, monkeypatch):
    """The qualified anon-map serves a plan whose wire form scrubs the Codex
    provider root — the codex-anon-plan-includes-roots acceptance row."""
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=("root-a-collision",),
        claude_sids=())
    try:
        port = srv.server_address[1]
        key = keys["root-a-collision"]
        s, wire, ctype = _get_json(port, _entity_path(key, "/anon-map"))
        assert s == 200 and "application/json" in ctype
        # The wire plan must carry a replacement keyed on the observed Codex root.
        flat = json.dumps(wire)
        assert "/synthetic/root-a/project-red" in flat or "project-red" in flat
    finally:
        stop(srv, srv._test_thread)


def test_scoped_claude_anon_map_excludes_other_account_file_path(
        tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=(),
        claude_sids=("s1", "s2"),
    )
    account_a = "a" * 32
    account_b = "b" * 32
    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "UPDATE conversation_messages SET account_key=? WHERE session_id='s1'",
            (account_a,),
        )
        conversations.execute(
            "UPDATE conversation_messages SET account_key=? WHERE session_id='s2'",
            (account_b,),
        )
        conversations.commit()
    finally:
        conversations.close()
    accounting = ns["open_cache_db"]()
    try:
        accounting.execute(
            "UPDATE session_files SET project_path=?,account_key=? "
            "WHERE session_id='s2'",
            ("/Users/bravo/private-project", account_b),
        )
        accounting.commit()
    finally:
        accounting.close()
    try:
        port = srv.server_address[1]
        path = _entity_path(_claude_key("s1"), "/anon-map")
        s, wire, ctype = _get_json(
            port, path + f"?account={account_a}"
        )
        assert s == 200 and "application/json" in ctype
        assert "/Users/bravo/private-project" not in json.dumps(wire)
    finally:
        stop(srv, srv._test_thread)


def test_scoped_codex_anonymization_refuses_ambiguous_thread_cwd(
        tmp_path, monkeypatch):
    """A surviving B row does not prove a conversation-level CWD is B-owned."""
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=("modern-full",),
        claude_sids=())
    account_a = "a" * 32
    account_b = "b" * 32
    alice_path = "/Users/alice/account-a-project"
    conversations = ns["open_conversations_db"]()
    try:
        key = keys["modern-full"]
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key=?",
            (account_b,),
        )
        conversations.execute(
            "UPDATE codex_conversation_events SET account_key=?",
            (account_a,),
        )
        conversations.commit()
    finally:
        conversations.close()
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE codex_conversation_threads SET cwd=? "
            "WHERE conversation_key=?", (alice_path, key),
        )
        cache.commit()
    finally:
        cache.close()
    try:
        port = srv.server_address[1]
        for suffix in ("/export?anonymize=1", "/anon-map"):
            separator = "&" if "?" in suffix else "?"
            path = _entity_path(key, suffix) + f"{separator}account={account_b}"
            if suffix.startswith("/export"):
                status, raw_body, content_type = _get(port, path)
                body = json.loads(raw_body) if raw_body.startswith(b"{") else None
            else:
                status, body, content_type = _get_json(port, path)
            assert status == 409, (suffix, status, body)
            assert "application/json" in content_type
            assert body["status"] == "anonymization_unavailable"
            assert body["ambiguous_cwd_rows"] == 2  # CWD + provider root
            assert body["reason"] == "ambiguous_account_provenance"
            assert "undecodable_cwd_rows" not in body
            assert "markdown" not in body
            assert "tokens" not in body
    finally:
        stop(srv, srv._test_thread)


def test_scoped_codex_anonymization_uses_only_proven_account_paths(
        tmp_path, monkeypatch):
    """A proven A path is scrubbed while B's root never reaches A's map."""
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch,
        codex_scenarios=("modern-full",),
        claude_sids=())
    account_a = "a" * 32
    account_b = "b" * 32
    key_a = keys["modern-full"]
    alice_path = "/Users/alice/account-a-project"
    bravo_path = "/Users/bravo/account-b-project"
    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key=? "
            "WHERE conversation_key=?", (account_a, key_a),
        )
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key=? "
            "WHERE id=(SELECT MAX(id) FROM codex_conversation_messages "
            "WHERE conversation_key=?)", (account_b, key_a),
        )
        conversations.execute(
            "UPDATE codex_conversation_events SET account_key=?",
            (account_a,),
        )
        source_root_key = conversations.execute(
            "SELECT source_root_key FROM codex_conversation_events "
            "WHERE conversation_key=? LIMIT 1", (key_a,),
        ).fetchone()[0]
        conversations.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path,line_offset,source_root_key,conversation_key,"
            "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("account-a.jsonl", 0, source_root_key, key_a, "a-thread",
             "a-thread", "session_meta",
             json.dumps({"type": "session_meta", "payload": {
                 "cwd": alice_path,
             }}), account_a),
        )
        conversations.execute(
            "INSERT INTO codex_conversation_events "
            "(source_path,line_offset,source_root_key,conversation_key,"
            "native_thread_id,root_thread_id,record_type,payload_json,account_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("foreign.jsonl", 0, "foreign-root", "v1.foreign", "foreign",
             "foreign", "session_meta",
             json.dumps({"type": "session_meta", "payload": {
                 "cwd": bravo_path,
             }}), account_b),
        )
        conversations.execute(
            "UPDATE codex_conversation_messages SET text=? "
            "WHERE id=(SELECT MIN(id) FROM codex_conversation_messages "
            "WHERE conversation_key=? AND account_key=?)",
            (f"A says {alice_path}", key_a, account_a),
        )
        conversations.commit()
    finally:
        conversations.close()
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE codex_conversation_threads SET cwd=? "
            "WHERE conversation_key=?", (alice_path, key_a),
        )
        cache.execute(
            "UPDATE codex_source_roots SET canonical_root_path=? "
            "WHERE source_root_key=(SELECT source_root_key FROM "
            "codex_conversation_threads WHERE conversation_key=?)",
            ("/Users/alice/provider-root", key_a),
        )
        cache.execute(
            "INSERT INTO codex_source_roots "
            "(source_root_key,canonical_root_path,first_seen_utc,last_seen_utc) "
            "VALUES(?,?,?,?)",
            ("foreign-root", "/Users/bravo/provider-root",
             "2026-08-04T00:00:00Z", "2026-08-04T00:00:00Z"),
        )
        cache.commit()
    finally:
        cache.close()
    try:
        port = srv.server_address[1]
        status, wire, _ctype = _get_json(
            port, _entity_path(key_a, "/anon-map")
            + f"?account={account_a}")
        assert status == 200, wire
        flat = json.dumps(wire)
        assert alice_path in flat
        assert "/Users/alice/provider-root" in flat
        assert bravo_path not in flat
        assert "/Users/bravo/provider-root" not in flat

        status, body, _ctype = _get(
            port, _entity_path(key_a, "/export")
            + f"?anonymize=1&account={account_a}")
        assert status == 200
        assert alice_path.encode() not in body
        assert bravo_path.encode() not in body
        assert b"A says" in body
    finally:
        stop(srv, srv._test_thread)


def test_anon_privacy_gate_secret_canary(tmp_path, monkeypatch):
    """The anonymization privacy gate — a qualified export of the secret-canary
    scrubs the documented secret patterns end to end (the
    codex-anonymization-privacy-gate row). The secret-canary rollout now carries a
    real turned conversation (§294 S7 F1), so this is an honest route-level proof:
    resolve the v1 key, GET /export?anonymize=1, assert every canary token is
    absent while the surrounding prose survives, then assert the raw leg still
    carries the tokens (non-vacuity)."""
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=("secret-canary",),
        claude_sids=())
    try:
        port = srv.server_address[1]
        key = keys["secret-canary"]        # secret-canary normalizes to one thread
        # Anonymized export: every canary token scrubbed; surrounding text survives.
        s_anon, anon_body, _c = _get(
            port, _entity_path(key, "/export") + "?anonymize=1")
        assert s_anon == 200
        assert b"sk-fixture-not-a-secret" not in anon_body      # api-key shape
        assert b"Bearer fixture-token" not in anon_body         # bearer token shape
        assert b"/synthetic/root-a/project-red" not in anon_body  # provider root
        assert b"project-red" not in anon_body                    # project label
        assert b"Canary widget configuration prompt" in anon_body  # prose survives
        # Raw export (no anonymize): the same tokens ARE present — proving the
        # anonymized assertions above are non-vacuous.
        s_raw, raw_body, _c = _get(port, _entity_path(key, "/export"))
        assert s_raw == 200
        assert b"sk-fixture-not-a-secret" in raw_body
        assert b"Bearer fixture-token" in raw_body
        assert b"Canary widget configuration prompt" in raw_body
    finally:
        stop(srv, srv._test_thread)


def test_mixed_db_bare_claude_export_bytes_unchanged(tmp_path, monkeypatch):
    """Codex rows present must NOT change bare-Claude anonymized export bytes
    (the §3.6 mixed-database regression, at the route boundary)."""
    ns_a = load_script()
    srv_a, _r, _k, _ro = _boot(ns_a, tmp_path / "a", monkeypatch,
                               codex_scenarios=(), claude_sids=("s1",))
    try:
        port = srv_a.server_address[1]
        s, bytes_claude_only, _c = _get(
            port, _entity_path("s1", "/export") + "?anonymize=1")
        assert s == 200
    finally:
        stop(srv_a, srv_a._test_thread)

    ns_b = load_script()
    srv_b, _r, _k, _ro = _boot(ns_b, tmp_path / "b", monkeypatch,
                               codex_scenarios=("modern-full", "root-a-collision"),
                               claude_sids=("s1",))
    try:
        port = srv_b.server_address[1]
        s, bytes_mixed, _c = _get(
            port, _entity_path("s1", "/export") + "?anonymize=1")
        assert s == 200
    finally:
        stop(srv_b, srv_b._test_thread)

    assert bytes_claude_only == bytes_mixed


# ── C4: Codex export renderer golden (staling triggers pinned) ───────────────

_EXPORT_GOLDEN = (REPO_ROOT / "tests" / "fixtures" / "codex-conversation-export"
                  / "modern-full.export.md")


def _mask_export(md: str) -> str:
    """Neutralize the only pricing-dependent bytes (dollar figures) and the opaque
    conversation-key tails so the golden stays stable across a pricing sync while
    still pinning the renderer's structure, prose, token labels, and ref shapes."""
    md = re.sub(r"\$\d+\.\d{4}", "$MONEY", md)
    md = re.sub(r"v1\.[A-Za-z0-9_-]+", "v1.<KEY>", md)
    return md


def test_codex_export_golden_and_no_staling_trigger_leak(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch, claude_sids=())
    key = keys["modern-full"]
    try:
        conn = ns["open_conversations_db"]()
        try:
            disp2 = ns["_load_sibling"]("_lib_conversation_dispatch")
            env1 = disp2.neutral_export(conn, key, scope="all",
                                        effective_speed="standard")
            env2 = disp2.neutral_export(conn, key, scope="all",
                                        effective_speed="standard")
        finally:
            conn.close()
    finally:
        stop(srv, srv._test_thread)
    assert env1["status"] == "ok"
    md = env1["markdown"]
    # Determinism: a fixed DB + speed renders byte-identically.
    assert md == env2["markdown"]
    # No release-version leak: the renderer embeds no semver-shaped token.
    assert not re.search(r"\b\d+\.\d+\.\d+\b", md), md
    # Provider-native token vocabulary only (never Claude cache words).
    assert "cached_input" in md and "reasoning_output" in md
    assert "cache_read" not in md and "cache_creation" not in md
    # Golden compare (pricing/key masked).
    assert _mask_export(md) == _EXPORT_GOLDEN.read_text(encoding="utf-8")


# ── #463 S1 — Phase C hydration is a hard failure, and the route reports it ───


def test_page_hydration_miss_raises_and_the_route_answers_500(tmp_path, monkeypatch):
    """Phase C raises instead of degrading, and the raise reaches the client as a
    500 rather than an unhandled traceback.

    The narrow index pass and the wide hydrating read select from the SAME table
    on the same conversation key, so a position present in one and absent from
    the other means the two reads disagree. Degrading to the narrow row would
    render an EMPTY block, because the narrow row carries no ``text`` — a silent
    wrong answer. Raising converts that into a loud one, and this test pins both
    the message (it must name the count and the first missing positions, so an
    operator can act on it) and the transport.
    """
    import pytest

    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch, claude_sids=())
    key = keys["modern-full"]
    q = ns["_load_sibling"]("_lib_codex_conversation_query")
    real_load = q._load_rows_at_positions
    dropped = {}

    def _drop_one(conn, conversation_key, positions):
        hydrated = real_load(conn, conversation_key, positions)
        if hydrated:
            victim = sorted(hydrated)[0]
            dropped["position"] = victim
            hydrated.pop(victim)
        return hydrated

    monkeypatch.setattr(q, "_load_rows_at_positions", _drop_one)
    try:
        conn = ns["open_conversations_db"]()
        try:
            with pytest.raises(RuntimeError) as excinfo:
                q.get_codex_conversation(conn, key, effective_speed="standard")
        finally:
            conn.close()
        message = str(excinfo.value)
        # The count of misses, not just "something went wrong".
        assert "missed 1 of " in message, message
        # The first missing positions, so the miss is locatable.
        assert "first missing positions:" in message, message
        assert repr(dropped["position"][0]) in message, message

        # The same failure over real HTTP: a 500, with a body, not a traceback.
        port = srv.server_address[1]
        status, body, ctype = _get(port, _entity_path(key))
        assert status == 500, (status, body)
        assert "application/json" in (ctype or ""), ctype
        assert json.loads(body).get("error"), body
    finally:
        stop(srv, srv._test_thread)


# ── #463 S3 — the export path is insensitive to the read-time enrichment ─────


def test_s3_export_is_unchanged_by_the_external_agent_marker(tmp_path, monkeypatch):
    """Spec section 6.4, the half `modern-full` cannot witness.

    The byte-frozen `modern-full` golden covers an assistant row and four tool
    renderings, but it carries no external-agent marker, so it can only show that
    detection found nothing. This exports a conversation that DOES carry one and
    asserts the marker still renders as ordinary assistant prose, verbatim.

    Two properties are at stake. The exporter reads `text` and `detail.name` and
    never `detail.external_call`, so the addition cannot move a byte; and the
    detection must write to `external_call` and never to `markers`, because that
    key is what selects which rows the export path hydrates from retained
    payloads — 729 assistant rows already carry it.
    """
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch,
                                 codex_scenarios=("tool-legibility",),
                                 claude_sids=())
    key = keys["tool-legibility"]
    try:
        conn = ns["open_conversations_db"]()
        try:
            disp2 = ns["_load_sibling"]("_lib_conversation_dispatch")
            env = disp2.neutral_export(conn, key, scope="all",
                                       effective_speed="standard")
            q = ns["_load_sibling"]("_lib_codex_conversation_query")
            detail = q.get_codex_conversation(
                conn, key, effective_speed="standard", limit=0)
            stored_markers = conn.execute(
                "SELECT COUNT(*) FROM codex_conversation_messages "
                "WHERE conversation_key = ? AND detail_json LIKE '%\"markers\"%'",
                (key,)).fetchone()[0]
        finally:
            conn.close()
    finally:
        stop(srv, srv._test_thread)
    assert env["status"] == "ok"
    md = env["markdown"]
    # Non-vacuity: the detail envelope really did detect the marker, so the
    # export assertion below is about a conversation where the new path fired.
    detected = [b for item in detail["items"] for b in item["blocks"]
                if (b.get("detail") or {}).get("external_call")]
    assert len(detected) == 1
    # The exported prose is the authored text, unrestructured.
    assert "[external_agent_tool_call: ToolSearch]" in md
    assert 'input: {"query": "select:SyntheticAlpha,SyntheticBeta"}' in md
    # Detection never wrote into `markers`, so the export path's payload
    # hydration selector is untouched.
    assert stored_markers == 0
    assert "external_call" not in md


def _raw_string_lines(rollout_path):
    """Every LINE of every string the retained rollout holds, as a set.

    Used to attribute a token in a rendered surface: a line that is a member of
    this set was rendered verbatim from provider bytes, while a line that is not
    was composed by a decoder.

    Line MEMBERSHIP, not substring containment against a joined blob: a derived
    line consisting of nothing but the bare integer `70001` is a substring of
    almost any blob carrying the token, so a containment test would attribute a
    decoder's own output to the provider and pass while the property failed.
    """
    parts = []

    def walk(value):
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                parts.append(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for line in rollout_path.read_text().splitlines():
        if line.strip():
            walk(json.loads(line))
    return {segment.strip()
            for part in parts for segment in part.splitlines()
            if segment.strip()}


# ── #463 S3 §6.5 — the privacy gate over the tool-legibility surfaces ────────


# Every provider session id the tool-legibility rollout holds. Named once so a
# new session added to the fixture cannot quietly escape the leak assertions.
_S3_PROVIDER_SESSION_IDS = ("70001", "70002", "70003", "70004")


def test_s3_no_raw_session_id_reaches_any_served_route(tmp_path, monkeypatch):
    """Spec §4.3 / §6.5, at the ROUTE boundary.

    The provider's own `session_id` (70001, 70002) is present in the underlying
    rollout arguments and in the harness preamble of a retained output. The
    reader is served a conversation-local ordinal instead, so the token must be
    ABSENT from the detail envelope, the anon-map plan and an anonymized
    export — not scrubbed, absent. It cannot be scrubbed: these are short
    integers, and a rule that replaced a bare `70001` would corrupt arbitrary
    text elsewhere in the conversation.

    THREE boundaries are recorded (wire contract §8), and this test asserts each
    of them positively rather than leaving it implied: `detail.args` is the
    pre-existing generic disclosure, the payload readback's `content` is the raw
    re-read record, and the byte-frozen export renders provider prose verbatim.
    Each is asserted to still carry the token, because a boundary that silently
    stopped carrying it would mean the recorded rule no longer describes the
    code. What is asserted as ABSENT is every S3-derived surface: the cards, the
    session index and the anon plan.
    """
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=("tool-legibility",),
        claude_sids=())
    try:
        port = srv.server_address[1]
        key = keys["tool-legibility"]

        s, detail, _c = _get_json(port, _entity_path(key, ""))
        assert s == 200 and detail["status"] == "ok"
        # Non-vacuity: the enrichment under test really is on this envelope.
        assert detail["session_index"]["sessions"]["1"]["ordinal"] == 1
        cards_seen = 0
        stdin_block_keys = []
        args_carrying_the_token = 0
        for item in detail["items"]:
            for block in item["blocks"]:
                if (block.get("detail") or {}).get("name") == "write_stdin":
                    stdin_block_keys.append(block["block_key"])
                args = (block.get("detail") or {}).get("args") or ""
                if any(token in args for token in _S3_PROVIDER_SESSION_IDS):
                    args_carrying_the_token += 1
                for card in ((block.get("detail") or {}).get("card"),
                             ((block.get("output") or {}).get("detail") or {}).get("card")):
                    if card is None:
                        continue
                    cards_seen += 1
                    blob = json.dumps(card)
                    for token in _S3_PROVIDER_SESSION_IDS:
                        assert token not in blob, (token, card)
        # Non-vacuity: the loop really did examine cards.
        assert cards_seen > 0
        for token in _S3_PROVIDER_SESSION_IDS:
            assert token not in json.dumps(detail["session_index"]), token

        # Boundary 1 — `detail.args` is the pre-existing generic disclosure and
        # is stored at ingest, so rewriting it would break the read-time-only
        # rule. It still shows the provider's own argument JSON verbatim.
        assert args_carrying_the_token > 0

        # Boundary 2 — the payload readback serves the raw re-read record, which
        # is the route's entire purpose. Its `card` is decoded by the same
        # kernel the paged route uses, so it must still publish the ordinal.
        assert stdin_block_keys
        for block_key in stdin_block_keys:
            s_pay, payload, _c = _get_json(
                port, _entity_path(key, "/payload")
                + f"?block_key={_u.quote(block_key)}&which=call")
            assert s_pay == 200 and payload["status"] == "ok"
            assert any(token in payload["content"] for token in _S3_PROVIDER_SESSION_IDS), payload["content"]
            card_blob = json.dumps(payload.get("card"))
            for token in _S3_PROVIDER_SESSION_IDS:
                assert token not in card_blob, (token, card_blob)

        # The anon-map plan: the token is never offered to the client as a
        # replacement, because it was never published in the first place.
        s_map, plan, _c = _get_json(port, _entity_path(key, "/anon-map"))
        assert s_map == 200
        for token in _S3_PROVIDER_SESSION_IDS:
            assert token not in json.dumps(plan), token

        # Boundary 3 — an anonymized export, which is a DIFFERENT code path
        # from the anon map: the export body is scrubbed server-side, while
        # per-card copy is scrubbed client-side through the plan above. A test
        # of one is not a test of the other.
        #
        # The export renders the provider's own bytes verbatim and is
        # byte-frozen, so the token DOES survive there — in the raw `exec`
        # program source, in a retained output's harness preamble, and in the
        # raw `write_stdin` arguments. That is the §8 boundary showing through
        # a prose surface, not a leak S3 introduced, and it cannot be closed by
        # scrubbing: the token is a short integer, so a rule that replaced a
        # bare `70001` would corrupt arbitrary text elsewhere (spec §4.3).
        #
        # So the property asserted here is the one that is both true and worth
        # having: every export line carrying the token is provider bytes
        # rendered verbatim. No field S3 derives can put it there, because a
        # derived field would produce a line the retained payload does not
        # contain.
        s_exp, exported, _c = _get(
            port, _entity_path(key, "/export") + "?anonymize=1")
        assert s_exp == 200
        raw_lines = _raw_string_lines(CORPUS / "tool-legibility.jsonl")
        hits = [line for line in exported.decode().splitlines()
                if any(token in line for token in _S3_PROVIDER_SESSION_IDS)]
        # Non-vacuity, in both directions: the export really did render this
        # conversation, and the token really is present to be attributed.
        assert hits, exported[:400]
        for line in hits:
            assert line.strip() in raw_lines, line
    finally:
        stop(srv, srv._test_thread)


def test_s3_patch_card_paths_are_covered_by_the_anon_plan(tmp_path, monkeypatch):
    """Spec §4.3 / §6.5 — patch file paths are surfaced as STRUCTURED card
    fields rather than inside a raw blob, but they are the same token class the
    plan already draws from authoritative roots, so no new anon source is
    required. This asserts that: every path the patch event card publishes is a
    token the served plan can scrub, and applying the plan removes it.
    """
    ns = load_script()
    srv, _root, keys, _r = _boot(
        ns, tmp_path, monkeypatch, codex_scenarios=("tool-legibility",),
        claude_sids=())
    try:
        port = srv.server_address[1]
        key = keys["tool-legibility"]
        s, detail, _c = _get_json(port, _entity_path(key, ""))
        assert s == 200
        paths = [f["path"]
                 for item in detail["items"] for block in item["blocks"]
                 for f in (((block.get("detail") or {}).get("card") or {}).get("files") or [])
                 if f.get("path")]
        # Non-vacuity: the dict-shaped patch event really did decode per file.
        assert any(p.startswith("/synthetic/root-a/project-red") for p in paths), paths

        s_map, plan, _c = _get_json(port, _entity_path(key, "/anon-map"))
        assert s_map == 200
        tokens = {t["text"]: t["replacement"] for t in plan["tokens"]}
        for path in paths:
            if not path.startswith("/synthetic/"):
                continue
            # The plan scrubs by longest-token replacement over known roots, so
            # the covering token is a prefix of the card path.
            assert any(path.startswith(token) for token in tokens), (path, sorted(tokens))
    finally:
        stop(srv, srv._test_thread)


# ── #850 §4.9 — the two anonymized routes fail closed (A24, A25) ────────────

_A850_BAD = b"/synthetic/\xffproject"
_A850_ACCOUNT = "c" * 32
_A850_REFUSAL = {
    "status": "anonymization_unavailable",
    "undecodable_cwd_rows": 1,
    "remedy": "cctally cache-sync --source codex --rebuild",
}


def _a850_corrupt(ns, conversation_key):
    """Make the thread's `cwd` undecodable and give the rows one owner.

    The account stamp is what keeps the scoped arms non-vacuous: an
    account-scoped request whose conversation is filtered out answers 404 for
    a reason that has nothing to do with the refusal under test.
    """
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE codex_conversation_threads SET cwd = CAST(? AS TEXT) "
            "WHERE conversation_key = ?", (_A850_BAD, conversation_key))
        cache.commit()
    finally:
        cache.close()
    conversations = ns["open_conversations_db"]()
    try:
        conversations.execute(
            "UPDATE codex_conversation_messages SET account_key = ?",
            (_A850_ACCOUNT,))
        conversations.commit()
    finally:
        conversations.close()


def test_850_a24_both_anonymized_routes_refuse_with_the_typed_status(
    tmp_path, monkeypatch,
):
    """A24. `/export?anonymize=1` and `/anon-map` answer 409 with the typed
    body, scoped and unscoped; the raw export and the Claude key are
    untouched. All four refusal arms fail today: the planner swallows the
    decode error and hands back a plan whose vocabulary is silently short."""
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        codex_key = keys["modern-full"]
        _a850_corrupt(ns, codex_key)
        for suffix in ("/export?anonymize=1", "/anon-map"):
            for account in ("", f"&account={_A850_ACCOUNT}"
                            if "?" in suffix else f"?account={_A850_ACCOUNT}"):
                path = _entity_path(codex_key, suffix) + account
                status, body, _ctype = _get_json(port, path)
                assert status == 409, (path, status, body)
                assert body == _A850_REFUSAL, (path, body)

        status, body, ctype = _get(port, _entity_path(codex_key, "/export"))
        assert status == 200 and b"#" in body
        assert "text/markdown" in ctype

        claude = _claude_key("s1")
        status, _body, _ctype = _get(
            port, _entity_path(claude, "/export?anonymize=1"))
        assert status == 200
        status, body, _ctype = _get_json(port, _entity_path(claude, "/anon-map"))
        assert status == 200 and body is not None
    finally:
        stop(srv, srv._test_thread)


def test_850_a25_the_detail_route_opens_the_affected_conversation(
    tmp_path, monkeypatch,
):
    """A25's route arm. `_rollup_fields` called `_thread_facts` unconditionally,
    so the detail route for a conversation whose thread carries an undecodable
    `cwd` collapsed to a 500."""
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        codex_key = keys["modern-full"]
        _a850_corrupt(ns, codex_key)
        status, body, _ctype = _get_json(port, _entity_path(codex_key))
        assert status == 200, body
        assert body["status"] == "ok"
        status, scoped, _ctype = _get_json(
            port, _entity_path(codex_key) + f"?account={_A850_ACCOUNT}")
        assert status == 200, scoped
    finally:
        stop(srv, srv._test_thread)


def test_850_both_anonymized_routes_refuse_when_the_count_read_fails(
    tmp_path, monkeypatch,
):
    """M5 is fail-closed on BOTH routes. The planner's count answered a
    `sqlite3.Error` with zero, so `/export?anonymize=1` handed back a
    transcript and `/anon-map` handed back a token map, each built from a scrub
    vocabulary the store had never established. Neither may emit anything now:
    the error reaches `_run_conversation_query`'s handler and is answered as a
    typed JSON error on a non-2xx status.
    """
    ns = load_script()
    srv, _root, keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        codex_key = keys["modern-full"]
        # The raw-table reference is resolved at call time; pointing it at a
        # table that is not there makes the count statement raise
        # `OperationalError: no such table`. The viewer's own key set binds the
        # resolver at import time, so only the planner leg fails.
        import _lib_codex_metadata as metadata
        monkeypatch.setattr(
            metadata, "resolve_codex_threads_table",
            lambda conn: "main.codex_conversation_threads_absent")

        for suffix in ("/export?anonymize=1", "/anon-map"):
            path = _entity_path(codex_key, suffix)
            status, body, ctype = _get_json(port, path)
            assert status == 500, (path, status, body)
            assert "application/json" in (ctype or ""), (path, ctype)
            assert "no such table" in body["error"], (path, body)
            assert "markdown" not in body, (path, body)
            assert "tokens" not in body, (path, body)

        # The raw export is not part of the anonymized contract and is
        # unaffected: it never reaches the planner.
        status, body, ctype = _get(port, _entity_path(codex_key, "/export"))
        assert status == 200 and b"#" in body
        assert "text/markdown" in ctype
    finally:
        stop(srv, srv._test_thread)
