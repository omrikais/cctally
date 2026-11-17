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
import pytest
import sqlite3
import pathlib
import shutil
import socket as _socket
import sys
import threading
import time
import urllib.parse as _u
from http.client import HTTPConnection

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, read_event, read_no_event, start, stop

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


def _wire_handler(ns, monkeypatch, *, no_sync=False, expose=False,
                  bind="127.0.0.1"):
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
    srv.handle_error = lambda request, client_address: None
    srv._test_thread = start(srv)
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
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conn, rebuild=True)
    finally:
        conn.close()
    srv = _wire_handler(ns, monkeypatch, no_sync=no_sync)  # loads dashboard + conv
    # #787 / #861 spec 5.9: the stats cutover invalidates the Codex quota
    # index (#977 C2), and a live-tail request writer never rebuilds it. The
    # dashboard's owner sync rebuilds it on its first tick; this harness runs
    # no sync thread, so it settles the index the same way here.
    import _cctally_journal as jr
    with ns["_cctally_core"].establish_authority("process"):
        jr.ensure_quota_index(budgeted=False)
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


def _open_sse(port, path, *, timeout=15.0):
    s = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.settimeout(timeout)
    s.sendall((
        f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Connection: keep-alive\r\n\r\n").encode("utf-8"))
    return s



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
        stop(srv, srv._test_thread)


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
        stop(srv, srv._test_thread)


def test_privacy_gate_403_before_any_preflight(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, _body, _c = _get(port, _events_path(key), host="evil.example.com")
        assert status == 403
    finally:
        stop(srv, srv._test_thread)


# ── qualified frames carry conversationKey (both providers) ─────────────────


def test_codex_ready_and_tail_use_conversation_key(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            assert read_event(s, marker="event: ready").startswith("event: ready")
            # #630 S2: `ready` fires before resolve() and ingest(), so it does
            # not mean a baseline exists. This frame does, which is why the
            # one-second guess it replaced is gone.
            read_event(s, marker="event: baselined")  # the server says the baseline exists
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(_APPEND_LINE)
            frame = read_event(s, marker="event: tail")
            data = json.loads([ln for ln in frame.splitlines()
                               if ln.startswith("data: ")][0][len("data: "):])
            assert data.get("conversationKey") == key   # qualified frame
            assert "sessionId" not in data               # never the bare field
        finally:
            s.close()
    finally:
        stop(srv, srv._test_thread)


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
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_claude_conversations"](conn, rebuild=True)
    finally:
        conn.close()
    srv = _wire_handler(ns, monkeypatch)
    key = disp._mint_claude_conversation_key("s1")
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            # #630 S2: `ready` fires before resolve() and ingest(), so it does
            # not mean a baseline exists. This frame does, which is why the
            # one-second guess it replaces is gone.
            read_event(s, marker="event: baselined")  # the server says the baseline exists
            with open(jsonl, "a", encoding="utf-8") as fh:
                fh.write(_asst_line("a2", "m2", "r2", "world", sid="s1"))
            frame = read_event(s, marker="event: tail")
            data = json.loads([ln for ln in frame.splitlines()
                               if ln.startswith("data: ")][0][len("data: "):])
            assert data.get("conversationKey") == key
            assert "sessionId" not in data
        finally:
            s.close()
    finally:
        stop(srv, srv._test_thread)


def test_codex_subsequent_growth_re_detected(tmp_path, monkeypatch):
    """Two appends → two tails: the watch advances its cursor only to the
    committed offset, so growth after an ingest is re-detected next cycle."""
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            # #630 S2: `ready` fires before resolve() and ingest(), so it does
            # not mean a baseline exists. This frame does, which is why the
            # one-second guess it replaces is gone.
            read_event(s, marker="event: baselined")  # the server says the baseline exists
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(_APPEND_LINE)
            read_event(s, marker="event: tail")
            line2 = _APPEND_LINE.replace("appended turn", "second turn").replace(
                "13:00:00", "13:05:00")
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(line2)
            frame2 = read_event(s, marker="event: tail")
            assert frame2.startswith("event: tail")
        finally:
            s.close()
    finally:
        stop(srv, srv._test_thread)


# ── passivity + unrelated-mutation isolation ────────────────────────────────


def test_codex_passive_under_no_sync(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch, no_sync=True)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key))
        try:
            read_no_event(s, marker="event: ready", window=3.0)   # passive: no ready
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(_APPEND_LINE)
            read_no_event(s, marker="event: tail", window=3.0)   # frozen: no tail
        finally:
            s.close()
    finally:
        stop(srv, srv._test_thread)


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
    conn = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conn, rebuild=True)
    finally:
        conn.close()
    srv = _wire_handler(ns, monkeypatch)
    try:
        port = srv.server_address[1]
        s = _open_sse(port, _events_path(key_a))
        try:
            # #630 S2: `ready` fires before resolve() and ingest(), so it does
            # not mean a baseline exists. This frame does, which is why the
            # one-second guess it replaces is gone.
            read_event(s, marker="event: baselined")  # the server says the baseline exists
            with open(b, "a", encoding="utf-8") as fh:      # grow the UNRELATED file
                fh.write(_APPEND_LINE)
            read_no_event(s, marker="event: tail", window=3.0)   # no emit for A
        finally:
            s.close()
    finally:
        stop(srv, srv._test_thread)


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
            read_event(s, marker="event: ready")
            read_event(s, marker="event: baselined")  # the server says the baseline exists
            # Drop the child rollout under the same sessions tree (a new file no
            # table yet knows) — the frontier must find + ingest + widen + emit.
            child = provider_root / "sessions" / "2026" / "07" / "15" / "child.jsonl"
            shutil.copyfile(CORPUS / "nested-child.jsonl", child)
            frame = read_event(s, marker="event: tail")
            assert frame.startswith("event: tail")
        finally:
            s.close()
        # The child was ingested and joined the parent's widened file set.
        conn = ns["open_conversations_db"]()
        try:
            paths = set(cq.codex_conversation_source_paths(conn, parent_key))
        finally:
            conn.close()
        assert str(provider_root / "sessions" / "2026" / "07" / "15" /
                   "child.jsonl") in paths
    finally:
        stop(srv, srv._test_thread)


def test_codex_child_discovery_ingests_through_the_request_writer(
        tmp_path, monkeypatch):
    """Detects child discovery writing through the stream's reader (#787 /
    #861 item 1, reproduced): with a read-only reader, the transcript ingest
    discovery ran on the READER, every write failed (``files_failed``), and the
    child never joined the watch. Discovery's ingest must run in the writer
    phase, through the request entry at the request_writer ceiling."""
    ns = load_script()
    srv, provider_root, _rollout, parent_key = _boot_codex(
        ns, tmp_path, monkeypatch, scenario="nested-parent", fast=True)
    core = ns["_cctally_core"]
    cache = sys.modules["_cctally_cache"]
    access = sys.modules["_cctally_store_access"]
    child = provider_root / "sessions" / "2026" / "07" / "15" / "child.jsonl"
    calls = []
    real = cache.sync_codex_conversations

    def recording(conn, **kwargs):
        if str(child) in set(kwargs.get("only_paths") or ()):
            a = core.current_authority()
            calls.append((
                isinstance(conn, access._QueryConnection),
                a.kind if a else None,
                core.current_request_ingest("sync_codex_conversations")
                is not None))
        return real(conn, **kwargs)

    monkeypatch.setattr(cache, "sync_codex_conversations", recording)
    try:
        s = _open_sse(srv.server_address[1], _events_path(parent_key))
        try:
            read_event(s, marker="event: baselined")
            shutil.copyfile(CORPUS / "nested-child.jsonl", child)
            read_event(s, marker="event: tail")
        finally:
            s.close()
    finally:
        stop(srv, srv._test_thread)
    assert calls, "the child was never ingested"
    assert all(c == (False, "request_writer", True) for c in calls), calls


# ── #787 / #861 spec 5.9: pending owner work, then recovery ─────────────────

_QUOTA_LINE_TEMPLATE = json.loads(
    (CORPUS / "modern-full.jsonl").read_text().splitlines()[2])


def _quota_line():
    row = json.loads(json.dumps(_QUOTA_LINE_TEMPLATE))
    row["timestamp"] = "2026-07-15T13:30:00Z"
    row["payload"]["info"]["rate_limits"]["primary"]["used_percent"] = 61.5
    return json.dumps(row) + "\n"


def _quota_observed(ns):
    conn = sqlite3.connect(str(ns["_cctally_core"].CACHE_DB_PATH))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE used_percent=61.5").fetchone()[0] > 0
    finally:
        conn.close()


def _pending_case(case, ns):
    """Seed ``case``; returns ``(append_line, blocks, drain)``."""
    core = ns["_cctally_core"]
    cache = sys.modules["_cctally_cache"]
    import _cctally_journal as jr
    if case == "quota_index_missing":
        # Accounting work only: the transcript tail is not the accounting
        # cursor's, so it keeps tailing (as on the base), while the quota
        # observation waits, unlost, for the owner's rebuild and sync.
        journal = pathlib.Path(core.JOURNAL_DIR)
        for name in (jr._QUOTA_DEDUP_INDEX_NAME, jr._QUOTA_DEDUP_META_NAME):
            (journal / name).unlink(missing_ok=True)

        def drain():
            assert not _quota_observed(ns), "the request rebuilt the index"
            jr.ensure_quota_index(budgeted=False)
            c = ns["open_cache_db"]()
            try:
                ns["sync_codex_cache"](c)
            finally:
                c.close()
            assert _quota_observed(ns), "the deferred observation was lost"
        return _quota_line(), False, drain
    if case == "codex_contract_pending":
        conn = ns["open_conversations_db"]()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO cache_meta(key,value) VALUES"
                "('codex_conversation_contract_version','0-stale')")
            conn.commit()
        finally:
            conn.close()

        def drain():
            ns["open_conversations_db"]().close()
        return _APPEND_LINE, True, drain
    if case == "codex_maintenance_debt":
        conn = sqlite3.connect(str(core.CACHE_DB_PATH))
        try:
            conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) "
                         "VALUES(?, '1')", (cache.CODEX_REPLAY_FROM_ZERO_KEY,))
            conn.commit()
        finally:
            conn.close()

        def drain():
            c = ns["open_cache_db"]()
            try:
                ns["sync_codex_cache"](c)
            finally:
                c.close()
            c = ns["open_conversations_db"]()
            try:
                ns["sync_codex_conversations"](c)
            finally:
                c.close()
        return _APPEND_LINE, True, drain
    if case == "incompatible_stats":
        # Stats owner work (an epoch rebuild) is never the tail's: the tail
        # keeps tailing while the stats state stays the owner's.
        conn = sqlite3.connect(str(core.DB_PATH))
        try:
            epoch = int(core.STATS_INDEX_EPOCH)
            conn.execute(f"PRAGMA user_version={epoch + 1}")
            conn.commit()
        finally:
            conn.close()

        def drain():
            return None
        return _APPEND_LINE, False, drain
    raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "quota_index_missing", "incompatible_stats", "codex_contract_pending",
    "codex_maintenance_debt"])
def test_live_tail_pending_and_recovered(tmp_path, monkeypatch, case):
    """Detects a live-tail request that drains owner work itself (an inline
    quota-index rebuild, an epoch rebuild, a contract replay, a from-zero
    replay) or that loses the growth it could not ingest: the cycle reports
    the work to the owner and emits nothing it has not ingested, and once the
    owner drains it the same growth is tailed."""
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch, fast=True)
    core = ns["_cctally_core"]
    events = []
    notified = []
    monkeypatch.setattr(core, "CAPABILITY_OBSERVERS", [events.append])
    unregister = core.register_owner_attention(
        lambda store, r: notified.append((store, r.obligations)))
    stats_before = None
    try:
        s = _open_sse(srv.server_address[1], _events_path(key))
        try:
            read_event(s, marker="event: baselined")
            line, blocks, drain = _pending_case(case, ns)
            if case == "incompatible_stats":
                stats_before = pathlib.Path(core.DB_PATH).read_bytes()
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(line)
            if blocks:
                read_no_event(s, marker="event: tail", window=3.0)
                assert notified, "the pending work never reached the owner"
                with core.establish_authority("process"):
                    drain()
            read_event(s, marker="event: tail")
            if not blocks:
                with core.establish_authority("process"):
                    drain()
        finally:
            s.close()
    finally:
        unregister()
        stop(srv, srv._test_thread)
    assert all(e.permitted for e in events), [
        (e.entry, e.capability) for e in events if not e.permitted]
    request_events = [e for e in events if e.authority == "request_writer"]
    assert {e.capability for e in request_events} <= {
        "READ", "INGEST_CURRENT"}, sorted(
            {(e.entry, e.capability) for e in request_events})
    if case == "incompatible_stats":
        assert pathlib.Path(core.DB_PATH).read_bytes() == stats_before
        assert notified and {s for s, _o in notified} >= {"stats"}, notified
    else:
        assert any("codex_maintenance_debt" in o for _s, o in notified), notified


@pytest.mark.parametrize("case", [
    "quota_index_missing", "codex_contract_pending", "codex_maintenance_debt"])
def test_live_tail_pending_woken_owner_resolves(tmp_path, monkeypatch, case):
    """#787 / #861 I3b: the dashboard's owner attention closes the loop the
    test above drives by hand. Detects a sync-mode dashboard in which the
    pending owner work a live tail reports (a missing quota index, a stale
    Codex contract, a from-zero replay) is never drained, because the request
    thread no longer runs the owner work and no owner is woken: the real
    `_dashboard_owner_attention` wakes the sync loop (and, for Codex debt,
    the conversations schema owner), no test call drains anything, and the
    same growth is then tailed.

    The sync loop's iteration here is the owner work the sync thread's build
    performs for the state (the case's own drain); the conversations half is
    the real `_wake_schema_writer`."""
    ns = load_script()
    srv, _root, rollout, key = _boot_codex(ns, tmp_path, monkeypatch, fast=True)
    core = ns["_cctally_core"]
    dash = sys.modules["_cctally_dashboard"]
    event = threading.Event()
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_EVENT", event)
    monkeypatch.setattr(dash, "_OWNER_ATTENTION_NO_SYNC", False)
    monkeypatch.setattr(dash, "_SCHEMA_WAKE_RUN_DERIVATIONS", True)
    # Notifications coalesce process-wide; the test above sends the same ones.
    monkeypatch.setattr(core, "_OWNER_ATTENTION_LAST", {})
    dash._reset_schema_wake_state()
    armed = {}
    passes = []
    drained = []

    def owner_iteration():
        passes.append(core.current_authority().kind)
        drain = armed.get("drain")
        if drain is not None and case != "codex_contract_pending":
            drain()
            drained.append(case)

    stop_loop = threading.Event()
    loop = threading.Thread(target=dash._owner_thread_target(
        lambda: dash._dashboard_sync_loop(
            stop=stop_loop, interval=3600.0, run_iteration=owner_iteration,
            attention=event)), daemon=True)
    unregister = core.register_owner_attention(dash._dashboard_owner_attention)
    loop.start()
    try:
        s = _open_sse(srv.server_address[1], _events_path(key))
        try:
            read_event(s, marker="event: baselined")
            line, _blocks, drain = _pending_case(case, ns)
            armed["drain"] = drain
            with open(rollout, "a", encoding="utf-8") as fh:
                fh.write(line)
            read_event(s, marker="event: tail")
            # A quota-index deferral does not hold the transcript tail; the
            # owner still has to be woken to recover the observation.
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not (
                    len(passes) >= 2
                    and (drained or case == "codex_contract_pending")):
                time.sleep(0.05)
        finally:
            s.close()
    finally:
        unregister()
        stop_loop.set()
        loop.join(timeout=10)
        dash._join_schema_wake_thread()
        stop(srv, srv._test_thread)
    assert len(passes) >= 2, "the owner was never woken (interval is 1 h)"
    assert set(passes) == {"process"}, passes
    if case != "codex_contract_pending":
        assert drained, "the woken owner did not drain the pending work"


# ── dispatch-level preflight unit (all three statuses, no server) ────────────


def test_neutral_events_preflight_statuses(tmp_path, monkeypatch):
    ns = load_script()
    srv, _root, _rollout, key = _boot_codex(ns, tmp_path, monkeypatch)
    stop(srv, srv._test_thread)
    conn = ns["open_conversations_db"]()
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
