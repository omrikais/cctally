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
import json
import pathlib
import re
import shutil
import sys
import threading
import urllib.parse as _u
from http.client import HTTPConnection

from conftest import load_script, redirect_paths

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
    srv.daemon_threads = True
    srv.handle_error = lambda request, client_address: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _boot(ns, tmp_path, monkeypatch, *, codex_scenarios=("modern-full",),
          claude_sids=("s1",), no_sync=False):
    """Seed a Codex provider + Claude sessions, start a dashboard. Returns
    ``(srv, provider_root, codex_keys, rollouts)`` where ``codex_keys`` maps a
    scenario name → its opaque ``v1.`` conversation key."""
    redirect_paths(ns, monkeypatch, tmp_path)
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
    c = HTTPConnection("127.0.0.1", port, timeout=8)
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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        assert s == 200 and f["status"] == "ok" and "anchors" in f
    finally:
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


def test_browse_malformed_cursor_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, _b, _c = _get(
            port, "/api/conversations?source=codex&cursor=" + _u.quote("has space"))
        assert s == 400
    finally:
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


def test_search_offset_with_source_is_400(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _keys, _r = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s, _b, _c = _get(port, "/api/conversation/search?source=codex&q=x&offset=1")
        assert s == 400
    finally:
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv.shutdown()


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
        srv_a.shutdown()

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
        srv_b.shutdown()

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
        srv.shutdown()
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
