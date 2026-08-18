"""GET /api/debug/backend — loopback-only diagnostic endpoint (issue #276).

Structural (never golden) shape assertions + the two Codex-P1 gate cases:
a hostname Host (DNS-rebinding vector) 403s, and expose_transcripts=True does
NOT open this surface (it never consults expose and still requires a loopback
Host). The non-loopback-PEER 403 is covered exhaustively by the pure-gate
matrix in tests/test_transcript_access.py (peer 192.168.0.9 cases) — a real
socket from this test always has a loopback peer.
"""
import json
import socketserver
import sqlite3
import sys
import threading
from http.client import HTTPConnection

from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)
from conftest import load_script, redirect_paths  # type: ignore


def _boot(ns, tmp_path, monkeypatch, *, bind="127.0.0.1", expose=False):
    redirect_paths(ns, monkeypatch, tmp_path)
    H = ns["DashboardHTTPHandler"]
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.hub = ns["SSEHub"]()
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.static_dir = ns["STATIC_DIR"]
    H.cctally_host = bind
    H.cctally_expose_transcripts = expose
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


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
    c.close()
    return status, body


def _source_bundle(now):
    def state(source, *, cost):
        return SourceDashboardState(
            source=source,
            availability="ok",
            freshness="fresh",
            warnings=(),
            data_version=f"{source}-opaque-v1",
            last_success_at=now,
            capabilities={"sessions": CapabilityRecord("supported", "native")},
            data={
                "hero": {"cost_usd": cost, "total_tokens": 1},
                "projects": {"rows": ({"key": f"project:{source}"},)},
                "alerts": {"rows": ({"key": f"alert:{source}"},)},
            },
        )
    claude = state("claude", cost=1.0)
    codex = state("codex", cost=2.0)
    return SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": compose_all_state(claude, codex)},
    )


def test_debug_backend_shape_over_loopback(monkeypatch, tmp_path):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/debug/backend")
        assert status == 200
        payload = json.loads(body)
        assert payload["schemaVersion"] == 1
        assert set(payload) >= {"version", "dataset", "cache_state", "phases"}
        # tracing off in tests -> phases null + note
        assert payload["phases"] is None
        assert payload["note"] == "tracing_disabled"
        assert isinstance(payload["dataset"], dict)
        assert isinstance(payload["cache_state"], dict)
        # dataset row counts are safe cache-table names against a known-empty DB
        assert payload["dataset"].get("session_entries") == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_debug_backend_reports_safe_source_counts_and_never_raw_open_errors(
    monkeypatch, tmp_path,
):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    now = ns["dt"].datetime(2026, 7, 16, tzinfo=ns["dt"].timezone.utc)
    snap = ns["DashboardHTTPHandler"].snapshot_ref.get()
    snap.source_bundle = _source_bundle(now)
    cache = ns["open_cache_db"]()
    try:
        cache.executemany(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model) VALUES (?, ?, ?, ?)",
            [("/private/claude-a.jsonl", 1, "2026-07-16T00:00:00Z", "claude"),
             ("/private/claude-b.jsonl", 2, "2026-07-16T00:00:00Z", "claude")],
        )
        cache.execute(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, window_minutes, used_percent, resets_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("codex", "private-root", "/private/quota.jsonl", 1,
             "2026-07-16T00:00:00Z", "slot", "logical-limit", 300, 10.0,
             "2026-07-16T05:00:00Z"),
        )
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model) "
            "VALUES (?, ?, ?, ?, ?)",
            [("/private/codex-a.jsonl", 1, "2026-07-16T00:00:00Z", "a", "gpt-5"),
             ("/private/codex-b.jsonl", 2, "2026-07-16T00:00:00Z", "b", "gpt-5"),
             ("/private/codex-c.jsonl", 3, "2026-07-16T00:00:00Z", "c", "gpt-5")],
        )
        cache.commit()
    finally:
        cache.close()
    stats = ns["open_db"]()
    stats.close()
    try:
        port = srv.server_address[1]
        status, body = _get(port, "/api/debug/backend")
        assert status == 200
        payload = json.loads(body)
        assert payload["sources"]["claude"]["tables"]["session_entries"] == 2
        assert payload["sources"]["codex"]["tables"]["codex_session_entries"] == 3
        assert payload["sources"]["codex"]["tables"]["quota_window_snapshots"] == 1
        assert payload["sources"]["codex"]["tables"]["quota_window_blocks"] == 0
        assert payload["sources"]["codex"]["tables"]["quota_percent_milestones"] == 0
        assert payload["sources"]["codex"]["tables"]["quota_threshold_events"] == 0
        assert payload["sources"]["codex"]["resources"] == {"projects": 1, "alerts": 1}
        assert payload["sources"]["codex"]["data_version"] == "codex-opaque-v1"
        encoded = json.dumps(payload)
        assert "/private/" not in encoded
        assert "logical-limit" not in encoded

        import _lib_snapshot_cache as snapshot_cache

        def signature_failure(*_args, **_kwargs):
            raise sqlite3.Error("/private/root source-fingerprint logical-limit")

        monkeypatch.setattr(snapshot_cache, "compute_signature", signature_failure)
        status, body = _get(port, "/api/debug/backend")
        assert status == 200
        assert json.loads(body)["cache_state"]["signature"] == {"status": "unavailable"}
        assert "private/root" not in body.decode("utf-8")

        def source_open_failure():
            raise RuntimeError("/private/root source-fingerprint logical-limit native-conversation-id")

        monkeypatch.setattr(sys.modules["_cctally_dashboard"], "open_cache_db", source_open_failure)
        status, body = _get(port, "/api/debug/backend")
        assert status == 200
        failure = json.loads(body)
        assert failure["cache_state"] == {"status": "unavailable"}
        assert "private/root" not in json.dumps(failure)
    finally:
        srv.shutdown()
        srv.server_close()


def test_debug_backend_403_on_hostname_host(monkeypatch, tmp_path):
    # A hostname Host from a loopback peer is a DNS-rebinding vector -> 403.
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        status, _ = _get(port, "/api/debug/backend", host="evil.example.com")
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_debug_backend_403_even_with_expose_transcripts(monkeypatch, tmp_path):
    # expose_transcripts=True + an IP-literal LAN Host (which the TRANSCRIPT
    # gate WOULD allow under expose) must STILL 403 here: this surface never
    # consults expose and requires a loopback Host as defense-in-depth.
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="0.0.0.0", expose=True)
    try:
        port = srv.server_address[1]
        status, _ = _get(
            port, "/api/debug/backend", host="192.168.0.9:%d" % port
        )
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


# ── #583 S1 §3.1/§3.2: the tick record and the runtime trace arm ────────────


def _post(port, path, body, *, host=None, origin=None, token=None,
          raw_body=None):
    """POST with full control over Host, Origin and the bearer."""
    c = HTTPConnection("127.0.0.1", port, timeout=5)
    payload = raw_body if raw_body is not None else json.dumps(body).encode()
    c.putrequest("POST", path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    if origin is not None:
        c.putheader("Origin", origin)
    if token is not None:
        c.putheader("Authorization", f"Bearer {token}")
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(payload)))
    c.endheaders()
    c.send(payload)
    r = c.getresponse()
    status, raw = r.status, r.read()
    c.close()
    return status, raw


def test_debug_backend_reports_the_tick_record_with_tracing_off(
    monkeypatch, tmp_path,
):
    """`phases: null` must stop being the whole answer (spec §3.1)."""
    import _lib_tick_stats as ts
    ns = load_script()
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("full")
    t.set_codex_regime("active")
    t.mark_ingest(5_000_000)
    t.mark_build(7_000_000)
    t.finish(published_ns=11, published_at="2026-08-15T00:00:00+00:00")
    ts.note_cache_open_failure("weekly")

    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        status, body = _get(srv.server_address[1], "/api/debug/backend")
        assert status == 200
        payload = json.loads(body)
        assert payload["phases"] is None, "precondition: tracing is off here"

        tick = payload["tick"]
        assert tick["dispatch_counts"] == {"idle": 0, "full": 1, "degraded": 0}
        assert tick["cache_open_failures"] == {"daily": 0, "weekly": 1,
                                               "monthly": 0}
        assert tick["tick_seq"] == 1
        assert len(tick["records"]) == 1
        record = tick["records"][0]
        assert set(record) == {
            "seq", "started_ns", "ended_ns", "duration_ns", "ingest_ran",
            "ingest_ns", "builder_ns", "dispatch", "codex_regime",
            "publication", "cold", "published_ns", "published_at", "period_ns",
            "cache_pin_ns",
        }, f"the wire names drifted from spec §1.1: {sorted(record)}"
        assert record["dispatch"] == "full"
        assert record["codex_regime"] == "active"
        assert record["ingest_ns"] == 5_000_000
        assert record["builder_ns"] == 7_000_000
        assert record["period_ns"] is None

        tracing = payload["tracing"]
        assert tracing == {"requested": False, "applied": False,
                           "applies_at": "none"}
        # §3.1 wanted the stored tree's instant surfaced so a GET after
        # `--trace off` cannot present an old tree as current. It was already
        # there: `generated_at` IS that instant, read from the same slot the
        # tree comes from. A separate `phases_generated_at` key was added and
        # measured byte-identical to it, so it was removed rather than kept.
        assert "generated_at" in payload
        assert "phases_generated_at" not in payload, (
            "the duplicate key is back")
        assert payload["schemaVersion"] == 1, "both keys are additive"
    finally:
        ts.reset_for_tests()
        srv.shutdown()
        srv.server_close()


def test_the_tick_record_leaks_no_path_and_no_prose(monkeypatch, tmp_path):
    """Preserve 18: timings, counts and enum names only."""
    import _lib_tick_stats as ts
    ns = load_script()
    ts.reset_for_tests()
    t = ts.begin_tick()
    t.set_dispatch("idle")
    t.finish(published_ns=1, published_at="2026-08-15T00:00:00+00:00")
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        status, body = _get(srv.server_address[1], "/api/debug/backend")
        assert status == 200
        encoded = json.dumps(json.loads(body)["tick"])
        assert "/" not in encoded.replace("\\/", ""), (
            f"a path-shaped value reached the tick block: {encoded}")
        for value in json.loads(body)["tick"]["records"][0].values():
            assert isinstance(value, (int, bool, str, type(None)))
    finally:
        ts.reset_for_tests()
        srv.shutdown()
        srv.server_close()


def test_debug_backend_publishes_conversation_passes_via_the_real_recorder(
    monkeypatch, tmp_path,
):
    """#583 S4. Populated through `record_conversation_pass`, not a hand-built
    snapshot, so the endpoint and the recorder are proven connected."""
    import _lib_tick_stats as ts
    ns = load_script()
    ts.reset_for_tests()
    ts.record_conversation_pass(
        seq=1, started_ns=0, ended_ns=2_000_000_000,
        duration_ns=2_000_000_000, cpu_ns=1_500_000_000, status="ok",
    )
    ts.record_conversation_pass(
        seq=2, started_ns=4_000_000_000, ended_ns=5_000_000_000,
        duration_ns=1_000_000_000, cpu_ns=500_000_000,
        status="store_unavailable",
    )
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        status, body = _get(srv.server_address[1], "/api/debug/backend")
        assert status == 200
        payload = json.loads(body)
        rows = payload["tick"]["conversation_sync"]
        assert len(rows) == 2
        assert rows[0]["cpu_ns"] == 1_500_000_000
        # Forward interval: the second pass's start finalizes the FIRST
        # record's period, and the newest record has no successor yet.
        assert rows[0]["period_ns"] == 4_000_000_000
        assert rows[1]["period_ns"] is None
        assert rows[1]["status"] == "store_unavailable"
        for row in rows:
            assert set(row) == {
                "seq", "started_ns", "ended_ns", "duration_ns",
                "cpu_ns", "period_ns", "status",
            }, "no field may carry free text"
            for value in row.values():
                assert isinstance(value, (int, str, type(None)))
        # Preserve 18, for the second ring too.
        encoded = json.dumps(payload["tick"])
        assert "/" not in encoded.replace("\\/", "")
    finally:
        ts.reset_for_tests()
        srv.shutdown()
        srv.server_close()


def test_an_empty_conversation_ring_publishes_an_empty_list_not_a_missing_key(
    monkeypatch, tmp_path,
):
    """`--no-sync` never starts the thread, so an empty ring is a reachable
    steady state the reader must be able to distinguish from an old server."""
    import _lib_tick_stats as ts
    ns = load_script()
    ts.reset_for_tests()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        status, body = _get(srv.server_address[1], "/api/debug/backend")
        assert status == 200
        assert json.loads(body)["tick"]["conversation_sync"] == []
    finally:
        ts.reset_for_tests()
        srv.shutdown()
        srv.server_close()


def test_trace_post_records_the_request_without_applying_it(
    monkeypatch, tmp_path,
):
    import _lib_perf as perf
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    port = srv.server_address[1]
    try:
        perf.set_enabled(False)
        status, body = _post(port, "/api/debug/backend/trace", {"enabled": True},
                             origin=f"http://127.0.0.1:{port}")
        assert status == 200, body
        assert json.loads(body) == {
            "requested": True, "applied": False,
            "applies_at": "next_authoritative_build",
        }
        assert perf.enabled() is False, (
            "the POST applied the flip itself instead of deferring it")
        assert perf.apply_pending() is True
        assert perf.enabled() is True
    finally:
        perf.request_enabled(False)
        perf.apply_pending()
        perf.set_enabled(False)
        srv.shutdown()
        srv.server_close()


def test_trace_post_rejects_every_other_body_shape(monkeypatch, tmp_path):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    port = srv.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    try:
        for raw in (b"", b"{", b"[]", b'{"enabled": "yes"}',
                    b'{"enabled": 1}', b"{}", b'{"enabled": true, "x": 1}'):
            status, body = _post(port, "/api/debug/backend/trace", None,
                                 origin=origin, raw_body=raw)
            assert status == 400, f"{raw!r} was accepted: {status} {body!r}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_trace_post_403s_without_an_origin(monkeypatch, tmp_path):
    """The behaviour the CLI in Task 16 must work with (spec §3.2)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    port = srv.server_address[1]
    try:
        status, _ = _post(port, "/api/debug/backend/trace", {"enabled": True})
        assert status == 403
        status, _ = _post(port, "/api/debug/backend/trace", {"enabled": True},
                          origin="http://evil.example.com")
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_trace_post_403s_on_a_hostname_host(monkeypatch, tmp_path):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    port = srv.server_address[1]
    try:
        status, _ = _post(port, "/api/debug/backend/trace", {"enabled": True},
                          host=f"evil.example.com:{port}",
                          origin=f"http://evil.example.com:{port}")
        assert status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_trace_post_401s_when_the_bearer_is_required_and_absent(
    monkeypatch, tmp_path,
):
    """Auth runs FIRST, before the loopback and CSRF layers (spec §3.2)."""
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    port = srv.server_address[1]
    handler = ns["DashboardHTTPHandler"]
    handler.cctally_api_token = "s3cret"
    try:
        status, _ = _post(port, "/api/debug/backend/trace", {"enabled": True},
                          origin=f"http://127.0.0.1:{port}")
        assert status == 401
        status, _ = _post(port, "/api/debug/backend/trace", {"enabled": True},
                          origin=f"http://127.0.0.1:{port}", token="wrong")
        assert status == 401
        # A bad Host with a good bearer still fails the loopback layer, which
        # proves auth is not the only gate.
        status, _ = _post(port, "/api/debug/backend/trace", {"enabled": True},
                          host=f"evil.example.com:{port}",
                          origin=f"http://evil.example.com:{port}",
                          token="s3cret")
        assert status == 403
        status, body = _post(port, "/api/debug/backend/trace",
                             {"enabled": False},
                             origin=f"http://127.0.0.1:{port}", token="s3cret")
        assert status == 200, body
    finally:
        handler.cctally_api_token = None
        srv.shutdown()
        srv.server_close()
