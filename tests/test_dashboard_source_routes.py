"""Source-qualified dashboard detail routes for #294 S4."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import shutil
import socketserver
import threading

import pytest

from _lib_dashboard_sources import (
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _state(source, now, data):
    return SourceDashboardState(
        source=source,
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version=f"{source}-v1",
        last_success_at=now,
        capabilities={
            "sessions": CapabilityRecord("supported"),
            "projects": CapabilityRecord("supported"),
            "quota": CapabilityRecord("supported"),
        },
        data=data,
    )


def _boot(ns, tmp_path, monkeypatch):
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    now = dt.datetime(2026, 7, 16, tzinfo=UTC)
    claude = _state("claude", now, {
        "sessions": {"rows": ({"key": "session:claude", "label": "Claude session"},)},
        "projects": {"rows": ({"key": "project:claude", "label": "Claude project"},)},
        "quota": {"blocks": ({"key": "block:claude", "label": "Claude block"},)},
    })
    codex = _state("codex", now, {
        "sessions": {"rows": ({"key": "session:codex", "label": "Codex session"},)},
        "projects": {"rows": ({"key": "project:codex", "label": "Codex project"},)},
        "quota": {"blocks": ({"key": "block:codex", "label": "Codex block"},)},
    })
    snap = ns["_empty_dashboard_snapshot"]()
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=1,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": compose_all_state(claude, codex)},
    )
    handler = ns["DashboardHTTPHandler"]
    handler.snapshot_ref = ns["_SnapshotRef"](snap)
    handler.hub = ns["SSEHub"]()
    handler.sync_lock = threading.Lock()
    handler.run_sync_now = staticmethod(lambda: None)
    handler.static_dir = ns["STATIC_DIR"]
    handler.cctally_host = "127.0.0.1"
    handler.cctally_expose_transcripts = False
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _boot_snapshot(ns, snap):
    handler = ns["DashboardHTTPHandler"]
    handler.snapshot_ref = ns["_SnapshotRef"](snap)
    handler.hub = ns["SSEHub"]()
    handler.sync_lock = threading.Lock()
    handler.run_sync_now = staticmethod(lambda: None)
    handler.static_dir = ns["STATIC_DIR"]
    handler.cctally_host = "127.0.0.1"
    handler.cctally_expose_transcripts = False
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _boot_real_codex(ns, tmp_path, monkeypatch, *, incomplete_metadata=False):
    """Publish a real two-root Codex state with colliding relative paths."""
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    roots = (tmp_path / "root-a", tmp_path / "root-b")
    for root, fixture in zip(roots, ("root-a-collision.jsonl", "root-b-collision.jsonl")):
        rollout = root / "sessions" / "2026" / "07" / "16" / "rollout-shared.jsonl"
        rollout.parent.mkdir(parents=True)
        shutil.copyfile(CORPUS / fixture, rollout)
    monkeypatch.setenv("CODEX_HOME", ",".join(str(root) for root in roots))

    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    now = dt.datetime(2026, 7, 16, 18, tzinfo=UTC)
    try:
        ns["sync_codex_cache"](cache)
        if incomplete_metadata:
            cache.execute(
                "UPDATE codex_session_entries SET conversation_key=NULL "
                "WHERE id=(SELECT id FROM codex_session_entries ORDER BY id LIMIT 1)"
            )
            cache.commit()
        # The collision fixtures intentionally carry odd 330/10020-minute
        # limits. Add coherent native 300/10080 evidence so this route suite
        # exercises a real current-cycle activity block rather than treating a
        # weekly quota summary as one.
        rooted_paths = tuple(cache.execute(
            "SELECT source_root_key, canonical_root_path FROM codex_source_roots "
            "ORDER BY source_root_key"
        ))
        for index, (root_key, root_path) in enumerate(rooted_paths):
            cache.executemany(
                "INSERT INTO quota_window_snapshots "
                "(source, source_root_key, source_path, line_offset, captured_at_utc, "
                "observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
                "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        "codex", root_key, f"{root_path}/fixture-5h.jsonl", 300 + index,
                        "2026-07-14T16:04:00+00:00", "fixture-5h",
                        "fixture-5h", "fixture-5h", "Fixture 5-hour limit", 300,
                        25.0, "2026-07-14T17:00:00+00:00",
                    ),
                    (
                        "codex", root_key, f"{root_path}/fixture-weekly.jsonl", 10_080 + index,
                        (now - dt.timedelta(minutes=1)).isoformat(), "fixture-weekly",
                        "fixture-weekly", "fixture-weekly", "Fixture weekly limit", 10_080,
                        25.0, (now + dt.timedelta(days=1)).isoformat(),
                    ),
                ),
            )
        ns["_cctally_cache"]._bump_codex_physical_mutation_seq(cache)
        cache.commit()
        ns["reconcile_codex_quota_projection"](
            source_root_keys=tuple(str(row[0]) for row in rooted_paths), now=now,
        )
        source_module = __import__("sys").modules["_cctally_dashboard_sources"]
        semantics = source_module.resolve_dashboard_source_semantics(
            {}, display_tz_name="UTC",
        )
        codex = source_module.build_codex_source_state(
            source_module.DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=dt.datetime(2026, 7, 1, tzinfo=UTC),
                now_utc=now,
                display_tz_name=semantics.display_tz_name,
                week_start_idx=semantics.week_start_idx,
                week_start_name=semantics.week_start_name,
                speed=semantics.speed,
                codex_budget=semantics.codex_budget,
            ),
            data_version="codex-real-v1",
        )
    finally:
        cache.close()
        stats.close()
    claude = _state("claude", now, {
        "sessions": {"rows": ()}, "projects": {"rows": ()},
        "quota": {"blocks": ()},
    })
    snap = ns["_empty_dashboard_snapshot"]()
    snap.generated_at = now
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=1,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={"claude": claude, "codex": codex, "all": compose_all_state(claude, codex)},
    )
    return (*_boot_snapshot(ns, snap), codex, roots)


def _seed_retained_route_history(ns, *, now, block_key):
    """Add large old history after the public keys have been published."""
    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    old_accounting_path = "/private/retained/native-accounting-history.jsonl"
    old_quota_path = "/private/retained/native-quota-history.jsonl"
    old_timestamp = (now - dt.timedelta(days=730)).isoformat()
    try:
        thread = cache.execute(
            "SELECT source_root_key, conversation_key, native_thread_id, root_thread_id "
            "FROM codex_conversation_threads ORDER BY source_root_key LIMIT 1"
        ).fetchone()
        assert thread is not None
        cache.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            "input_tokens, output_tokens, total_tokens, source_root_key, conversation_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    old_accounting_path, index, old_timestamp,
                    "native-retained-session", "gpt-5", 1, 1, 2,
                    thread[0], thread[1],
                )
                for index in range(100_000)
            ),
        )

        source_module = __import__("sys").modules["_cctally_dashboard_sources"]
        raw_block = None
        for row in stats.execute(
            "SELECT source_root_key, logical_limit_key, observed_slot, window_minutes, "
            "limit_id, resets_at_utc FROM quota_window_blocks "
            "WHERE source='codex' ORDER BY resets_at_utc DESC, source_root_key, "
            "logical_limit_key, observed_slot"
        ):
            candidate = source_module.dashboard_resource_key(
                "block", "codex", row[0], row[1], row[2], row[3], row[5],
            )
            if candidate == block_key:
                raw_block = row
                break
        assert raw_block is not None

        old_reset = (now - dt.timedelta(days=729)).isoformat()
        cache.executemany(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
            "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "codex", raw_block[0], old_quota_path, index, old_timestamp,
                    f"retained-slot-{index}", f"retained-limit-{index}",
                    f"retained-limit-id-{index}", "Retained native quota", 300,
                    10.0, old_reset,
                )
                for index in range(5_000)
            ),
        )
        active_old_capture = (now - dt.timedelta(days=60)).isoformat()
        cache.execute(
            "INSERT INTO quota_window_snapshots "
            "(source, source_root_key, source_path, line_offset, captured_at_utc, "
            "observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
            "used_percent, resets_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "codex", raw_block[0], "/private/retained/active-window.jsonl", 1,
                active_old_capture, raw_block[2], raw_block[1], raw_block[4],
                "Active retained quota", raw_block[3], 1.0, raw_block[5],
            ),
        )
        cache.commit()
        return {
            "active_old_capture": active_old_capture,
            "private_values": {
                str(value) for value in (
                    old_accounting_path, old_quota_path,
                    "/private/retained/active-window.jsonl",
                    "native-retained-session", thread[0], thread[1], thread[2], thread[3],
                    raw_block[0],
                    "retained-limit-4999", "retained-limit-id-4999",
                ) if value
            },
        }
    finally:
        cache.close()
        stats.close()


def _get(server, path):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        status = response.status
    finally:
        conn.close()
    payload = json.loads(body)
    return status, payload


def _close(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize(("source", "resource", "key"), [
    ("claude", "session", "session:claude"),
    ("claude", "project", "project:claude"),
    ("claude", "block", "block:claude"),
    ("codex", "session", "session:codex"),
    ("codex", "project", "project:codex"),
    ("codex", "block", "block:codex"),
])
def test_source_routes_return_provider_native_detail_not_published_summary_row(
    monkeypatch, tmp_path, source, resource, key,
):
    ns = load_script()
    dashboard = __import__("sys").modules["_cctally_dashboard"]
    server, thread = _boot(ns, tmp_path, monkeypatch)
    calls = []

    def _detail_builder(*, snapshot, source, resource, key):
        if not key.endswith(source):
            raise dashboard.SourceResourceNotFound()
        calls.append((snapshot, source, resource, key))
        return {
            "detail_kind": f"{source}_{resource}",
            "key": key,
            "provider_metric": 17,
        }

    monkeypatch.setattr(dashboard, "build_source_detail", _detail_builder)
    try:
        status, payload = _get(server, f"/api/source/{source}/{resource}/{key}")
        assert status == 200
        assert payload == {
            "source": source,
            "resource": resource,
            "data": {
                "detail_kind": f"{source}_{resource}",
                "key": key,
                "provider_metric": 17,
            },
        }
        assert len(calls) == 1
        assert calls[0][1:] == (source, resource, key)
        # The published summary row is deliberately not the response payload.
        assert "label" not in payload["data"]

        other_source = "claude" if source == "codex" else "codex"
        cross_status, cross_payload = _get(
            server, f"/api/source/{other_source}/{resource}/{key}",
        )
        assert cross_status == 404
        assert cross_payload == {
            "code": "source_resource_not_found",
            "error": "source resource not found",
        }
        assert len(calls) == 1
    finally:
        _close(server, thread)


def test_source_routes_do_not_return_published_summary_rows_as_details(
    monkeypatch, tmp_path,
):
    ns = load_script()
    dashboard = __import__("sys").modules["_cctally_dashboard"]
    server, thread = _boot(ns, tmp_path, monkeypatch)

    def _detail_builder(*, snapshot, source, resource, key):
        return {
            "detail_kind": "provider-native",
            "key": key,
            "provider_metric": 17,
        }

    monkeypatch.setattr(dashboard, "build_source_detail", _detail_builder)
    try:
        status, payload = _get(server, "/api/source/codex/session/session%3Acodex")
        assert status == 200
        assert payload["data"] == {
            "detail_kind": "provider-native",
            "key": "session:codex",
            "provider_metric": 17,
        }
        assert "label" not in payload["data"]
    finally:
        _close(server, thread)


def test_source_routes_do_not_ingest_or_parse_rollouts_on_detail_reads(
    monkeypatch, tmp_path,
):
    ns = load_script()
    dashboard = __import__("sys").modules["_cctally_dashboard"]
    server, thread = _boot(ns, tmp_path, monkeypatch)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("source detail read attempted ingest or rollout parsing")

    cache = __import__("sys").modules["_cctally_cache"]
    monkeypatch.setattr(dashboard, "sync_cache", _forbidden)
    monkeypatch.setattr(cache, "sync_codex_cache", _forbidden)
    monkeypatch.setattr(dashboard.pathlib.Path, "rglob", _forbidden)
    try:
        status, payload = _get(server, "/api/source/codex/session/session%3Acodex")
        assert status == 404
        assert payload == {
            "code": "source_resource_not_found",
            "error": "source resource not found",
        }
    finally:
        _close(server, thread)


def test_codex_source_routes_build_real_relational_native_details_with_collision_safety(
    monkeypatch, tmp_path,
):
    ns = load_script()
    server, thread, codex, roots = _boot_real_codex(ns, tmp_path, monkeypatch)
    try:
        session_rows = codex.data["sessions"]["rows"]
        assert len(session_rows) == 2
        assert len({row["key"] for row in session_rows}) == 2
        session_details = []
        for row in session_rows:
            status, payload = _get(
                server, f"/api/source/codex/session/{row['key']}",
            )
            assert status == 200
            detail = payload["data"]
            assert detail["detail_kind"] == "codex_session"
            assert detail["models"]
            assert detail["model_breakdowns"]
            assert detail["input_tokens"] > 0
            assert detail["total_tokens"] > 0
            session_details.append(detail)
        assert {detail["key"] for detail in session_details} == {
            row["key"] for row in session_rows
        }

        project_row = codex.data["projects"]["rows"][0]
        status, payload = _get(
            server, f"/api/source/codex/project/{project_row['key']}",
        )
        assert status == 200
        project = payload["data"]
        assert project["detail_kind"] == "codex_project"
        assert project["models"]
        assert project["sessions"]
        assert project["total_tokens"] > 0

        block_row = codex.data["quota"]["blocks"][0]
        assert block_row["window_minutes"] == 300
        assert block_row["model_breakdowns"]
        status, payload = _get(
            server, f"/api/source/codex/block/{block_row['key']}",
        )
        assert status == 200
        block = payload["data"]
        assert block["detail_kind"] == "codex_block"
        assert block["observations"]
        assert "milestones" in block
        assert isinstance(block["milestones"], list)
        assert block["forecast"]["status"]

        public = json.dumps(session_details + [project, block])
        for root in roots:
            assert str(root) not in public
        assert "11111111-1111-4111-8111-111111111111" not in public
        assert "root-thread-a" not in public
    finally:
        _close(server, thread)


def test_codex_source_routes_round_trip_published_rows_with_incomplete_project_metadata(
    monkeypatch, tmp_path,
):
    ns = load_script()
    server, thread, codex, roots = _boot_real_codex(
        ns, tmp_path, monkeypatch, incomplete_metadata=True,
    )
    try:
        assert codex.availability == "partial"
        assert codex.data["sessions"]["rows"]
        assert codex.data["projects"]["rows"]
        details = []

        for resource in ("session", "project"):
            for row in codex.data[f"{resource}s"]["rows"]:
                status, payload = _get(
                    server, f"/api/source/codex/{resource}/{row['key']}",
                )
                assert status == 200
                detail = payload["data"]
                assert detail["key"] == row["key"]
                assert detail["detail_kind"] == f"codex_{resource}"
                assert detail["metadata_availability"] == "partial"
                assert detail["total_tokens"] == row["total_tokens"]
                assert detail["cost_usd"] == pytest.approx(row["cost_usd"])
                details.append(detail)

        public = json.dumps(details)
        for root in roots:
            assert str(root) not in public
    finally:
        _close(server, thread)


def test_codex_source_routes_bound_real_relational_reads_over_retained_history(
    monkeypatch, tmp_path,
):
    ns = load_script()
    dashboard = __import__("sys").modules["_cctally_dashboard"]
    cache_module = __import__("sys").modules["_cctally_cache"]
    analytics = __import__("sys").modules["_cctally_source_analytics"]
    quota = __import__("sys").modules["_cctally_quota"]
    server, thread, codex, roots = _boot_real_codex(ns, tmp_path, monkeypatch)
    session_rows = codex.data["sessions"]["rows"]
    project_row = codex.data["projects"]["rows"][0]
    block_row = codex.data["quota"]["blocks"][0]
    seeded = _seed_retained_route_history(
        ns, now=dt.datetime(2026, 7, 16, 18, tzinfo=UTC), block_key=block_row["key"],
    )

    cache_sql = []
    stats_sql = []
    qualified_calls = []
    quota_calls = []
    original_open_cache = cache_module.open_cache_db
    original_open_stats = dashboard.open_db
    original_qualified = analytics.load_qualified_codex_entries
    original_quota = quota.load_codex_quota_observations

    def _traced_open_cache():
        conn = original_open_cache()
        conn.set_trace_callback(cache_sql.append)
        return conn

    def _traced_open_stats():
        conn = original_open_stats()
        conn.set_trace_callback(stats_sql.append)
        return conn

    def _traced_qualified(start, end, *, speed, sync=True, group="git-root", cache_conn=None):
        result = original_qualified(
            start, end, speed=speed, sync=sync, group=group, cache_conn=cache_conn,
        )
        qualified_calls.append({
            "start": start, "end": end, "sync": sync,
            "shared_connection": cache_conn is not None, "rows": len(result),
        })
        return result

    def _traced_quota(**kwargs):
        result = original_quota(**kwargs)
        quota_calls.append({
            name: value for name, value in kwargs.items() if name != "cache_conn"
        } | {
            "shared_connection": kwargs.get("cache_conn") is not None,
            "rows": len(result),
        })
        return result

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("source detail route attempted ingest or rollout parsing")

    monkeypatch.setattr(cache_module, "open_cache_db", _traced_open_cache)
    monkeypatch.setattr(dashboard, "open_db", _traced_open_stats)
    monkeypatch.setattr(analytics, "load_qualified_codex_entries", _traced_qualified)
    monkeypatch.setattr(quota, "load_codex_quota_observations", _traced_quota)
    monkeypatch.setattr(dashboard, "sync_cache", _forbidden)
    monkeypatch.setattr(cache_module, "sync_codex_cache", _forbidden)
    monkeypatch.setattr(pathlib.Path, "rglob", _forbidden)

    try:
        assert len(session_rows) == 2
        assert len({row["key"] for row in session_rows}) == 2
        session_details = []
        for row in session_rows:
            status, payload = _get(server, f"/api/source/codex/session/{row['key']}")
            assert status == 200
            assert payload["data"]["detail_kind"] == "codex_session"
            assert payload["data"]["total_tokens"] > 0
            session_details.append(payload["data"])
        assert {detail["key"] for detail in session_details} == {
            row["key"] for row in session_rows
        }

        status, payload = _get(
            server, f"/api/source/codex/project/{project_row['key']}",
        )
        assert status == 200
        project = payload["data"]
        assert project["detail_kind"] == "codex_project"
        assert 0 < len(project["sessions"]) < 100

        status, payload = _get(
            server, f"/api/source/codex/block/{block_row['key']}",
        )
        assert status == 200
        block = payload["data"]
        assert block["detail_kind"] == "codex_block"
        assert 0 < len(block["observations"]) <= 250
        assert "2026-07-14T16:04:00+00:00" in {
            item["captured_at"] for item in block["observations"]
        }

        # Two collision-safe session requests plus project and block are each
        # one bounded relational accounting load, regardless of 100k old rows.
        assert len(qualified_calls) == 4
        for call in qualified_calls:
            assert call["sync"] is False
            assert call["shared_connection"] is True
            assert call["end"] - call["start"] == dt.timedelta(
                days=365, microseconds=1,
            )
            assert call["rows"] < 100

        # Stage 1's presentation interface must cap rows in SQLite while
        # retaining an active window whose last capture predates the cutoff.
        assert len(quota_calls) == 4
        for call in quota_calls:
            assert call["shared_connection"] is True
            assert call["captured_at_or_after"] == dt.datetime(
                2026, 6, 11, 18, tzinfo=UTC,
            )
            assert call["active_at"] == dt.datetime(2026, 7, 16, 18, tzinfo=UTC)
            assert call["max_rows"] == 1000
            assert call["source_root_keys"]
            assert call["rows"] <= 1000

        normalized_cache_sql = [" ".join(statement.split()) for statement in cache_sql]
        accounting_queries = [
            statement for statement in normalized_cache_sql
            if "FROM codex_session_entries AS entries" in statement
        ]
        assert len(accounting_queries) == 4
        assert all("INDEXED BY idx_codex_entries_ts_root_conversation" in sql
                   for sql in accounting_queries)
        assert all("entries.timestamp_utc >=" in sql and "entries.timestamp_utc <" in sql
                   for sql in accounting_queries)
        quota_queries = [
            statement for statement in normalized_cache_sql
            if "FROM quota_window_snapshots" in statement
        ]
        assert len(quota_queries) == 4
        assert all("unixepoch(captured_at_utc) >=" in sql for sql in quota_queries)
        assert all("OR unixepoch(resets_at_utc) >" in sql for sql in quota_queries)
        assert all("LIMIT 1000" in sql for sql in quota_queries)

        normalized_stats_sql = [" ".join(statement.split()) for statement in stats_sql]
        block_queries = [
            statement for statement in normalized_stats_sql
            if "FROM quota_window_blocks WHERE source='codex'" in statement
        ]
        assert len(block_queries) == 1
        assert "LIMIT 250" in block_queries[0]

        public = json.dumps(session_details + [project, block])
        for root in roots:
            assert str(root) not in public
        for private_value in seeded["private_values"]:
            assert private_value not in public
    finally:
        _close(server, thread)


@pytest.mark.parametrize("path", [
    "/api/source/all/session/session%3Acodex",
    "/api/source/codex/unknown/session%3Acodex",
    "/api/source/codex/session/%ZZ",
])
def test_source_routes_reject_invalid_owners_resources_and_percent_encoding(monkeypatch, tmp_path, path):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, payload = _get(server, path)
        assert status == 400
        assert payload["code"] == "source_capability_unavailable"
        assert payload["error"] == "source capability unavailable"
    finally:
        _close(server, thread)


def test_source_route_logs_private_detail_without_returning_it(monkeypatch, tmp_path):
    ns = load_script()
    dashboard = __import__("sys").modules["_cctally_dashboard"]
    server, thread = _boot(ns, tmp_path, monkeypatch)
    logged = []
    canary = "/private/root source-fingerprint logical-limit native-conversation-id"

    def _boom(*_args, **_kwargs):
        raise RuntimeError(canary)

    def _log_error(self, fmt, *args):
        logged.append(fmt % args)

    monkeypatch.setattr(dashboard, "build_source_detail", _boom)
    monkeypatch.setattr(dashboard.DashboardHTTPHandler, "log_error", _log_error)
    try:
        status, payload = _get(server, "/api/source/codex/session/session%3Acodex")
        assert status == 400
        assert payload == {
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
        }
        assert canary not in json.dumps(payload)
        assert any(canary in item for item in logged)
    finally:
        _close(server, thread)
