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
        source_schema_version=1,
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
