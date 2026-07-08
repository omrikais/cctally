"""#179 — resumable conversation-enrichment reingest + non-blocking dashboard sync."""
import json
import os
import sqlite3
import pytest

from conftest import load_script, redirect_paths  # type: ignore


def _asst_line(uuid, msg_id, req_id, text, *, ts="2026-06-01T00:00:00Z",
               model="claude-opus-4-7"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": "s1",
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _user_line(uuid, text, *, ts="2026-06-01T00:01:00Z"):
    return json.dumps({
        "type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": ts,
        "message": {"role": "user", "content": text},
    }) + "\n"


ENRICH_FLAG = "conversation_reingest_enrichment_pending"
SHARED_FLAG = "conversation_reingest_pending"
CURSOR_KEY = "conversation_reingest_cursor"
GEN_KEY = "conversation_reingest_cursor_gen"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated cache.db + 4 seeded Claude JSONL files (a/b/c/d.jsonl), each
    already ingested once into conversation_messages. Returns
    (cache_mod, conn, projects, paths)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache_mod          # same module object load_script loaded
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ("a", "b", "c", "d"):
        p = projects / f"{name}.jsonl"
        p.write_text(_asst_line(f"u-{name}", f"m-{name}", f"r-{name}", f"hi {name}")
                     + _user_line(f"uu-{name}", f"ping {name}"))
        paths.append(p)
    conn = ns["open_cache_db"]()
    cache_mod.sync_cache(conn)                  # populate conversation_messages, no flag
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] >= 8
    yield cache_mod, conn, projects, paths
    try:
        conn.close()
    except Exception:
        pass


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO cache_meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _rowids_for(conn, path):
    return [r[0] for r in conn.execute(
        "SELECT id FROM conversation_messages WHERE source_path=? ORDER BY byte_offset",
        (str(path),))]


def test_resume_skips_done_files(env):
    cache_mod, conn, projects, paths = env
    a, b, c, d = paths
    _set_meta(conn, ENRICH_FLAG, "1")
    before_a, before_b = _rowids_for(conn, a), _rowids_for(conn, b)
    _set_meta(conn, CURSOR_KEY, str(b))         # pretend a + b already reingested
    _set_meta(conn, GEN_KEY, ENRICH_FLAG)       # matching generation
    cache_mod._resumable_reingest_conversation_messages(conn)
    # a + b untouched (same rowids); c + d were re-enriched (rowids bumped by autoincrement)
    assert _rowids_for(conn, a) == before_a
    assert _rowids_for(conn, b) == before_b
    assert _rowids_for(conn, c) and _rowids_for(conn, c)[0] > before_b[-1]
    assert _get_meta(conn, ENRICH_FLAG) is None and _get_meta(conn, CURSOR_KEY) is None


def test_completion_clears_flags_cursor_gen_no_churn(env):
    cache_mod, conn, projects, paths = env
    _set_meta(conn, ENRICH_FLAG, "1")
    # AUTOINCREMENT id never reuses values, so a from-cursor='' full reingest
    # that DELETEs+reinserts every file once must grow MAX(id) by EXACTLY
    # COUNT(*) — proving each file was re-enriched exactly once with no
    # clear+reinsert double-churn. (The original ingest already consumed the
    # first COUNT(*) ids; a clean single pass consumes the next COUNT(*).)
    mx_before = conn.execute("SELECT MAX(id) FROM conversation_messages").fetchone()[0]
    cache_mod._resumable_reingest_conversation_messages(conn)
    for k in (ENRICH_FLAG, SHARED_FLAG, "conversation_source_tool_use_reingest_pending",
              CURSOR_KEY, GEN_KEY):
        assert _get_meta(conn, k) is None
    n = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    mx = conn.execute("SELECT MAX(id) FROM conversation_messages").fetchone()[0]
    # exactly one reinsert per row this pass — no churn (no clear+reinsert loop)
    assert mx == mx_before + n
    # every on-disk row present exactly once (no dupes from a half-rolled write)
    distinct = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM conversation_messages "
        "GROUP BY source_path, byte_offset)").fetchone()[0]
    assert distinct == n


def test_unreadable_middle_file_preserved_and_converges(env):
    cache_mod, conn, projects, paths = env
    a, b, c, d = paths
    _set_meta(conn, ENRICH_FLAG, "1")
    before_c = _rowids_for(conn, c)
    os.chmod(c, 0o000)                          # PermissionError (OSError) on open
    try:
        cache_mod._resumable_reingest_conversation_messages(conn)
    finally:
        os.chmod(c, 0o644)
    assert _rowids_for(conn, c) == before_c     # preserved, NOT dropped
    assert _rowids_for(conn, d)                 # d (after c) still reingested
    assert _get_meta(conn, ENRICH_FLAG) is None  # converged: flag cleared


def test_rollback_on_mid_file_write_then_resume(env, monkeypatch):
    cache_mod, conn, projects, paths = env
    a, b, c, d = paths
    _set_meta(conn, ENRICH_FLAG, "1")
    before_c = _rowids_for(conn, c)
    real = cache_mod._reingest_parse_file

    def poisoned(jp, path_str):
        if path_str == str(c):
            return [("too", "few", "cols")]     # wrong arity -> executemany raises after DELETE
        return real(jp, path_str)

    monkeypatch.setattr(cache_mod, "_reingest_parse_file", poisoned)
    with pytest.raises(Exception):
        cache_mod._resumable_reingest_conversation_messages(conn)
    assert _rowids_for(conn, c) == before_c     # rolled back — c intact
    assert _get_meta(conn, CURSOR_KEY) == str(b)  # cursor at last good file
    assert _get_meta(conn, ENRICH_FLAG) == "1"  # still pending
    monkeypatch.setattr(cache_mod, "_reingest_parse_file", real)
    cache_mod._resumable_reingest_conversation_messages(conn)  # clean resume
    assert _get_meta(conn, ENRICH_FLAG) is None and _get_meta(conn, CURSOR_KEY) is None


def test_generation_change_resets_cursor(env):
    cache_mod, conn, projects, paths = env
    a, b, c, d = paths
    before_a = _rowids_for(conn, a)
    _set_meta(conn, CURSOR_KEY, str(b))
    _set_meta(conn, GEN_KEY, ENRICH_FLAG)       # old generation = enrichment only
    _set_meta(conn, ENRICH_FLAG, "1")
    _set_meta(conn, SHARED_FLAG, "1")           # NEW flag -> generation differs
    cache_mod._resumable_reingest_conversation_messages(conn)
    assert _rowids_for(conn, a) and _rowids_for(conn, a)[0] > before_a[-1]  # a re-done
    assert _get_meta(conn, CURSOR_KEY) is None and _get_meta(conn, GEN_KEY) is None


def test_sync_cache_consumes_reingest_and_rebuild_clears_cursor(env):
    cache_mod, conn, projects, paths = env
    _set_meta(conn, ENRICH_FLAG, "1")
    cache_mod.sync_cache(conn)                  # integration: flag consumed via new helper
    assert _get_meta(conn, ENRICH_FLAG) is None
    # rebuild must also clear a stray cursor/gen
    _set_meta(conn, CURSOR_KEY, str(paths[1]))
    _set_meta(conn, GEN_KEY, ENRICH_FLAG)
    cache_mod.sync_cache(conn, rebuild=True)
    assert _get_meta(conn, CURSOR_KEY) is None and _get_meta(conn, GEN_KEY) is None


def test_dashboard_initial_snapshot_never_syncs(monkeypatch, tmp_path):
    """#179 + #278 A1: the foreground initial snapshot must never run the heavy
    sync — that work is owned by the background _DashboardSyncThread.

    --no-sync keeps the full pre-bind build via _tui_build_snapshot(skip_sync=True).
    A NORMAL launch now uses the cheap A1 seed, which bypasses _tui_build_snapshot
    entirely and calls the current_week / forecast builders directly with
    skip_sync=True — so it likewise never ingests before the bind."""
    import types
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import cctally  # the loaded main module namespace
    import _cctally_tui as tui
    import _cctally_dashboard as dash

    # --no-sync: the full pre-bind build, skip_sync=True.
    cap_full = {}

    def fake_build(*a, **kw):
        cap_full["skip_sync"] = kw.get("skip_sync")
        return object()  # opaque snapshot; helper just returns it

    monkeypatch.setattr(cctally, "_tui_build_snapshot", fake_build)
    dash._dashboard_initial_snapshot(
        types.SimpleNamespace(no_sync=True), pinned_now=None,
        display_tz_pref_override=None)
    assert cap_full["skip_sync"] is True, "--no-sync must still skip_sync"

    # Normal launch: the cheap seed bypasses _tui_build_snapshot and calls the
    # headline builders directly with skip_sync=True (never ingesting).
    cap_full.clear()
    seed_calls = {}

    def fake_cw(conn, now_utc, *, skip_sync=False, **kw):
        seed_calls["cw"] = skip_sync
        return None

    def fake_fc(conn, now_utc, *, skip_sync=False, **kw):
        seed_calls["fc"] = skip_sync
        return None

    monkeypatch.setattr(tui, "_tui_build_current_week", fake_cw)
    monkeypatch.setattr(tui, "_tui_build_forecast_view", fake_fc)
    dash._dashboard_initial_snapshot(
        types.SimpleNamespace(no_sync=False), pinned_now=None,
        display_tz_pref_override=None)
    assert seed_calls == {"cw": True, "fc": True}
    assert "skip_sync" not in cap_full, (
        "a normal launch must NOT route through _tui_build_snapshot (cheap seed)"
    )


# --- U8-G1: real-server bind-before-sync (#179 regression, #217 S1) ---------
# A REAL ThreadingHTTPServer on an ephemeral port (mirrors production's
# _QuietThreadingHTTPServer) proving the HTTP port is bound and ACCEPTING before
# the heavy sync completes. The existing tests/test_dashboard_reingest_179.py
# coverage above is monkeypatch-level (asserts _dashboard_initial_snapshot uses
# skip_sync=True); this exercises the end-to-end ordering against a live socket.

import argparse     # noqa: E402
import socketserver  # noqa: E402
import threading     # noqa: E402
from http.client import HTTPConnection  # noqa: E402


def _make_snapshot(ns):
    return ns["_empty_dashboard_snapshot"]()


def test_real_server_binds_and_serves_before_sync_completes(tmp_path, monkeypatch):
    """The #179 invariant, end-to-end: with the background sync still mid-flight
    (a run_sync_now blocked on a test-held event), the bound HTTP port must
    ACCEPT and answer /api/data from the seeded snapshot — proving the bind does
    not wait on sync_cache. A pre-#179 ordering (sync before bind) would make the
    port unreachable until the event released."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    HandlerCls.snapshot_ref = SnapshotRef(_make_snapshot(ns))
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.no_sync = False
    HandlerCls.cctally_host = "127.0.0.1"

    # A "heavy sync" that blocks until the test releases it — stands in for a
    # long sync_cache / reingest. The background sync thread enters it and parks.
    sync_entered = threading.Event()
    release_sync = threading.Event()
    sync_completed = threading.Event()

    def blocking_sync():
        sync_entered.set()
        release_sync.wait(timeout=10)
        sync_completed.set()

    HandlerCls.run_sync_now = staticmethod(blocking_sync)

    # Background sync thread starts (and parks inside blocking_sync) BEFORE the
    # bind — the production ordering: _DashboardSyncThread.start() precedes the
    # ThreadingHTTPServer construction.
    sync_thread = threading.Thread(target=blocking_sync, daemon=True)
    sync_thread.start()
    assert sync_entered.wait(timeout=5), "sync thread did not start"

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    http_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    http_thread.start()
    try:
        port = srv.server_address[1]
        # The sync is STILL blocked — prove it, then prove the port answers.
        assert not sync_completed.is_set(), "sync should still be blocked"
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/api/data")
        r = c.getresponse()
        body = r.read()
        c.close()
        assert r.status == 200, (r.status, body)
        # Still blocked at the moment we got served — the bind did not wait.
        assert not sync_completed.is_set(), (
            "port answered only after sync completed — bind blocked on sync (#179)"
        )
    finally:
        release_sync.set()
        srv.shutdown()
        http_thread.join(timeout=3)
        sync_thread.join(timeout=3)


# --- #218 I-C.3: real cmd_dashboard bind-before-sync, at the production call site
# The test above proves the invariant against a HAND-BUILT ThreadingTCPServer.
# This drives the PRODUCTION cmd_dashboard far enough to construct its OWN real
# HTTP server (the bind) and proves that bind is reached while the background
# _DashboardSyncThread's sync is blocked mid-flight — the #179 ordering at the
# real call site. We stop AT the bind (a sentinel raised from the server ctor) so
# serve_forever / the browser open / the signal wait never run.


class _BindReached(Exception):
    """Raised from the patched server ctor right after the real bind to
    short-circuit cmd_dashboard before serve_forever."""


def test_real_cmd_dashboard_binds_before_background_sync_completes(tmp_path, monkeypatch):
    """The #179 invariant END-TO-END through cmd_dashboard: with the background
    _DashboardSyncThread's sync provably blocked (parked in a patched
    _make_run_sync_now), cmd_dashboard must still reach and complete the real
    ThreadingHTTPServer bind — proving the bind does not wait on the first
    background sync.

    Regression guard: a pre-#179 ordering that gated the bind on the first
    background sync would hang the worker on the blocked sync, so the bind event
    would never fire within the join timeout."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    (tmp_path / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    import _cctally_dashboard as dash   # same module object load_script loaded

    # Background sync seam: park the _DashboardSyncThread's sync indefinitely so
    # it is provably mid-flight (and can NEVER complete) for the whole run.
    sync_entered = threading.Event()
    park_forever = threading.Event()   # never set → the bg sync daemon parks

    def fake_make_run_sync_now(*a, **k):
        def _blocking(*aa, **kk):
            sync_entered.set()
            park_forever.wait()        # park (no spin); daemon dies at exit
        return _blocking
    monkeypatch.setattr(dash, "_make_run_sync_now", fake_make_run_sync_now)

    # Neutralize the update-check thread (no network / side effects).
    class _NoopThread(threading.Thread):
        def __init__(self, *a, **k):
            super().__init__(daemon=True)

        def run(self):
            pass
    monkeypatch.setattr(dash, "_DashboardUpdateCheckThread", _NoopThread)

    # Record the REAL bind, then stop cmd_dashboard before serve_forever.
    bound: dict = {}
    _RealServer = dash._QuietThreadingHTTPServer

    class _RecordingServer(_RealServer):
        def __init__(self, addr, handler):
            super().__init__(addr, handler)         # the REAL bind happens here
            bound["port"] = self.server_address[1]
            self.server_close()                     # release the listening socket
            raise _BindReached
    monkeypatch.setattr(dash, "_QuietThreadingHTTPServer", _RecordingServer)

    args = argparse.Namespace(host="127.0.0.1", port=0, no_browser=True,
                              no_sync=False, sync_interval=1000, tz=None)

    rc: dict = {}

    def run_it():
        try:
            rc["ret"] = dash.cmd_dashboard(args)
        except _BindReached:
            rc["bind_reached"] = True
        except BaseException as exc:                # surface anything unexpected
            rc["err"] = repr(exc)
    worker = threading.Thread(target=run_it, daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), f"cmd_dashboard hung before the bind; rc={rc}"
    assert rc.get("err") is None, rc["err"]
    # The real HTTP bind was reached even though the background sync can never
    # complete (park_forever is never set) — bind is not gated on sync (#179).
    assert rc.get("bind_reached") is True, rc
    assert bound.get("port", 0) > 0, rc
    # Non-vacuity: the background sync seam was genuinely exercised and parked, so
    # the "blocked sync" premise is real (not a sync that was skipped entirely).
    assert sync_entered.wait(timeout=2), "background sync seam was never exercised"


# --- #217 S1 / U6: migration-017 nested-agent reingest end-to-end -------------
# A >16 KB nested-subagent grandchild result whose `agentId:` trailer lands past
# the 16 KB tool_result clip. After the ingest-time structured agent_id stamp +
# the migration-017 offset-0 reingest, the grandchild RE-LINKS on existing
# history (vs. degrading to a flat top-level card pre-fix).

NESTED_FLAG = "conversation_reingest_nested_agent_pending"


def _spawn_and_grandchild_lines():
    """An assistant spawn (Task tool_use) + a user grandchild tool_result whose
    string content carries an agentId: trailer PAST the 16 KB cap."""
    spawn = json.dumps({
        "type": "assistant", "uuid": "a-spawn", "sessionId": "s1",
        "requestId": "r1", "timestamp": "2026-06-01T00:00:00Z",
        "message": {"role": "assistant", "id": "m1", "model": "claude-opus-4-7",
                    "content": [{"type": "tool_use", "name": "Task", "id": "tu-g",
                                 "input": {"description": "deep audit",
                                           "subagent_type": "Explore"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}}}) + "\n"
    filler = "F" * 16500   # pushes the agentId: trailer past the 16 KB cap
    body = (filler + "\nagentId: eeee5555 (use SendMessage to continue)\n"
            "<usage>subagent_tokens: 321 tool_uses: 4 duration_ms: 88</usage>")
    result = json.dumps({
        "type": "user", "uuid": "u-result", "sessionId": "s1",
        "timestamp": "2026-06-01T00:01:00Z",
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "content": body,
                                 "tool_use_id": "tu-g"}]}}) + "\n"
    return spawn + result


def _grandchild_links(ns):
    """True iff session s1's grandchild eeee5555 links into subagent_meta."""
    import _lib_conversation_query as cq
    conn = ns["open_cache_db"]()
    try:
        out = cq.get_conversation(conn, "s1")
    finally:
        conn.close()
    return out is not None and "eeee5555" in (out.get("subagent_meta") or {})


def test_017_flag_consumed_by_sync_and_shared_flag_untouched(tmp_path, monkeypatch):
    """sync_cache consumes the DISTINCT nested-agent reingest flag (clears it via
    the offset-0 backfill) and never arms/clears the SHARED flag in its place."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache_mod
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "s1.jsonl").write_text(_spawn_and_grandchild_lines())

    conn = ns["open_cache_db"]()
    cache_mod.sync_cache(conn)                          # initial ingest
    # Arm ONLY the distinct 017 flag (simulating migration 017 having run).
    _set_meta(conn, NESTED_FLAG, "1")
    conn.close()

    conn = ns["open_cache_db"]()
    cache_mod.sync_cache(conn)                          # consumes the flag
    assert _get_meta(conn, NESTED_FLAG) is None, "sync must clear the 017 flag"
    assert _get_meta(conn, SHARED_FLAG) is None, \
        "sync must NOT arm/leave the shared conversation_reingest_pending flag"
    conn.close()


def test_017_grandchild_over_16kb_relinks_after_reingest(tmp_path, monkeypatch):
    """The load-bearing end-to-end #217 S1 / U6 case: a >16 KB grandchild links
    after ingest (the structured stamp runs over the full raw), and STILL links
    after a forced offset-0 reingest (the migration-017 backfill path)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache_mod
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "s1.jsonl").write_text(_spawn_and_grandchild_lines())

    conn = ns["open_cache_db"]()
    cache_mod.sync_cache(conn)
    conn.close()
    assert _grandchild_links(ns), "grandchild must link after the initial ingest stamp"

    # Force the migration-017 reingest path: arm the flag, re-sync, confirm the
    # offset-0 re-parse re-derives the structured agent_id and the link survives.
    conn = ns["open_cache_db"]()
    _set_meta(conn, NESTED_FLAG, "1")
    cache_mod.sync_cache(conn)
    assert _get_meta(conn, NESTED_FLAG) is None
    conn.close()
    assert _grandchild_links(ns), "grandchild must STILL link after the 017 reingest"
