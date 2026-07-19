"""Source-safe dashboard share backend contract for #294 S4."""
from __future__ import annotations

import datetime as dt
import http.client
import json
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


def _state(source, now, *, total_cost, daily_label=None):
    return SourceDashboardState(
        source=source,
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version=f"{source}-v1",
        last_success_at=now,
        capabilities={"sessions": CapabilityRecord("supported", "inclusive-input-tokens")},
        data={
            "hero": {"cost_usd": total_cost, "total_tokens": 1200},
            "sessions": {
                "total_sessions": 1,
                "total_cost_usd": total_cost,
                "total_tokens": 1200,
                "rows": ({
                    "key": f"session:{source}", "label": f"{source.title()} session",
                    "cost_usd": total_cost, "total_tokens": 1200,
                    "last_activity": now.isoformat(),
                },),
            },
            "projects": {
                "total_cost_usd": total_cost,
                "total_tokens": 1200,
                "rows": ({
                    "key": f"project:{source}", "label": f"{source.title()} project",
                    "cost_usd": total_cost, "total_tokens": 1200,
                },),
            },
            "periods": {
                "daily": {
                    "total_cost_usd": total_cost,
                    "total_tokens": 1200,
                    "rows": ({
                        "label": daily_label or f"{source.title()} current day",
                        "cost_usd": total_cost,
                        "total_tokens": 1200,
                    },),
                },
                "monthly": {
                    "total_cost_usd": total_cost,
                    "total_tokens": 1200,
                    "display_tz": "UTC",
                    "rows": ({
                        "label": "2026-07", "cost_usd": total_cost,
                        "total_tokens": 1200,
                    },),
                },
                "weekly": {
                    "total_cost_usd": total_cost,
                    "total_tokens": 1200,
                    "display_tz": "UTC",
                    "rows": ({
                        "label": "2026-07-13", "cost_usd": total_cost,
                        "total_tokens": 1200,
                    },),
                },
            },
            "quota": {"blocks": (), "histories": (), "milestones": ()},
        },
    )


def _boot(ns, tmp_path, monkeypatch):
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    now = dt.datetime(2026, 7, 16, tzinfo=UTC)
    claude = _state("claude", now, total_cost=1.0)
    codex = _state("codex", now, total_cost=2.0)
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
    handler.run_sync_now_locked = staticmethod(lambda: None)
    handler.no_sync = True
    handler.display_tz_pref_override = None
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _render(
    server, *, source_marker, panel="sessions", template_id="sessions-recap",
    options=None,
):
    payload = {
        "panel": panel,
        "template_id": template_id,
        "options": {"format": "md", "theme": "light", "reveal_projects": False},
    }
    if options:
        payload["options"].update(options)
    if source_marker is not None:
        payload["source"] = source_marker
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST", "/api/share/render", body=json.dumps(payload),
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        body = json.loads(response.read())
        return response.status, body
    finally:
        conn.close()


def _request(server, method, path, payload=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {"Host": f"127.0.0.1:{port}"}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers.update({
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
            })
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read()) if response.length != 0 else None
    finally:
        conn.close()


def _sessions_recipe(source):
    return {
        "snapshot": {
            "panel": "sessions",
            "template_id": "sessions-recap",
            "source": source,
            "options": {
                "format": "html", "theme": "light",
                "reveal_projects": False, "no_branding": False,
                "show_chart": True, "show_table": True,
            },
            "data_digest_at_add": "sha256:outdated",
            "kernel_version": 1,
        },
    }


def test_source_share_defaults_omitted_source_to_legacy_claude_response(monkeypatch, tmp_path):
    ns = load_script()
    share_lib = ns["_share_load_lib"]()
    rendered = []
    original_render = share_lib.render

    def _capture_render(snap, **kwargs):
        rendered.append(snap)
        return original_render(snap, **kwargs)

    monkeypatch.setattr(share_lib, "render", _capture_render)
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, omitted = _render(server, source_marker=None)
        assert status == 200
        assert "source" not in omitted["snapshot"]
        assert rendered[-1].source == "claude"
        assert rendered[-1].source_label is None

        status, explicit = _render(server, source_marker="claude")
        assert status == 200
        assert explicit["snapshot"]["source"] == "claude"
        assert explicit["snapshot"]["data_digest"] != omitted["snapshot"]["data_digest"]
        assert rendered[-1].source == "claude"
        assert rendered[-1].source_label == "Claude"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_share_uses_native_codex_snapshot_and_labeled_all_composition(monkeypatch, tmp_path):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        codex_status, codex = _render(server, source_marker="codex")
        assert codex_status == 200
        assert codex["snapshot"]["source"] == "codex"
        assert "Codex" in codex["body"]

        all_status, combined = _render(server, source_marker="all")
        assert all_status == 200
        assert combined["snapshot"]["source"] == "all"
        assert "Claude" in combined["body"]
        assert "Codex" in combined["body"]
        assert combined["snapshot"]["data_digest"] != codex["snapshot"]["data_digest"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_source_share_uses_the_requested_provider_panel_rows(monkeypatch, tmp_path):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, result = _render(
            server,
            source_marker="codex",
            panel="projects",
            template_id="projects-recap",
            options={"reveal_projects": True},
        )
        assert status == 200
        assert "Codex project" in result["body"]
        assert "Codex session" not in result["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_codex_current_week_share_uses_canonical_week_period_and_token_chrome(
    monkeypatch, tmp_path,
):
    ns = load_script()
    lib = ns["_share_load_lib"]()
    rendered = []
    original = lib.render

    def capture(snapshot, **kwargs):
        rendered.append(snapshot)
        return original(snapshot, **kwargs)

    monkeypatch.setattr(lib, "render", capture)
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, result = _render(
            server,
            source_marker="codex",
            panel="current-week",
            template_id="current-week-recap",
        )
        assert status == 200
        snapshot = rendered[-1]
        assert snapshot.source == "codex"
        assert snapshot.source_label == "Codex"
        assert snapshot.period.start == dt.datetime(2026, 7, 13, tzinfo=UTC)
        assert snapshot.period.end == dt.datetime(2026, 7, 16, tzinfo=UTC)
        assert (snapshot.period.end - snapshot.period.start) > dt.timedelta(days=1)
        assert [column.label for column in snapshot.columns] == ["Week", "Tokens", "$ Cost"]
        assert "Current data" not in result["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_codex_render_branches_before_claude_panel_build(monkeypatch, tmp_path):
    ns = load_script()
    share = __import__("sys").modules["_cctally_dashboard_share"]

    def claude_failure(*_args, **_kwargs):
        raise RuntimeError("claude-only panel builder failed")

    monkeypatch.setattr(share, "_build_share_panel_data", claude_failure)
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, result = _render(server, source_marker="codex")
        assert status == 200
        assert result["snapshot"]["source"] == "codex"
        status, body = _render(server, source_marker="claude")
        assert status == 500
        assert body == {
            "code": "source_render_failed",
            "error": "source render failed",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_codex_data_change_updates_digest_and_compose_drift(monkeypatch, tmp_path):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, first = _render(server, source_marker="codex")
        assert status == 200
        first_digest = first["snapshot"]["data_digest"]

        snap = ns["DashboardHTTPHandler"].snapshot_ref.get()
        old_codex = snap.source_bundle.sources["codex"]
        changed_data = dict(old_codex.data)
        changed_sessions = dict(changed_data["sessions"])
        changed_rows = [dict(row) for row in changed_sessions["rows"]]
        changed_rows[0]["total_tokens"] = 9999
        changed_sessions["rows"] = tuple(changed_rows)
        changed_data["sessions"] = changed_sessions
        changed_codex = SourceDashboardState(
            source="codex", availability="ok", freshness="fresh", warnings=(),
            data_version="codex-v2", last_success_at=old_codex.last_success_at,
            capabilities=old_codex.capabilities, data=changed_data,
        )
        claude = snap.source_bundle.sources["claude"]
        snap.source_bundle = SourceDashboardBundle(
            source_schema_version=1, default_source="claude",
            source_order=("claude", "codex", "all"),
            sources={
                "claude": claude, "codex": changed_codex,
                "all": compose_all_state(claude, changed_codex),
            },
        )

        status, second = _render(server, source_marker="codex")
        assert status == 200
        assert second["snapshot"]["data_digest"] != first_digest
        recipe = _sessions_recipe("codex")
        recipe["snapshot"]["data_digest_at_add"] = first_digest
        status, composed = _request(server, "POST", "/api/share/compose", {
            "title": "Drift", "theme": "light", "format": "html",
            "no_branding": False, "reveal_projects": False,
            "sections": [recipe],
        })
        assert status == 200
        result = composed["snapshot"]["section_results"][0]
        assert result["data_digest_now"] == second["snapshot"]["data_digest"]
        assert result["drift_detected"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("endpoint", ["render", "compose"])
def test_source_share_exceptions_log_private_canaries_and_return_generic_envelopes(
    monkeypatch, tmp_path, endpoint,
):
    ns = load_script()
    share = __import__("sys").modules["_cctally_dashboard_share"]
    canary = "/private/root fingerprint:abc native-conversation-id"
    logged = []

    def provider_failure(*_args, **_kwargs):
        raise RuntimeError(canary)

    def log_error(self, fmt, *args):
        logged.append(fmt % args)

    monkeypatch.setattr(share, "_share_codex_state_for_period", provider_failure)
    monkeypatch.setattr(ns["DashboardHTTPHandler"], "log_error", log_error)
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        if endpoint == "render":
            status, body = _render(server, source_marker="codex")
        else:
            status, body = _request(server, "POST", "/api/share/compose", {
                "title": "Private", "theme": "light", "format": "html",
                "no_branding": False, "reveal_projects": False,
                "sections": [_sessions_recipe("codex")],
            })
        assert status == 500
        assert body == {
            "code": "source_render_failed",
            "error": "source render failed",
        }
        assert canary not in json.dumps(body)
        assert any(canary in line for line in logged)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("source", ["codex", "all"])
def test_noncurrent_source_share_rebuilds_codex_provider_state_without_sync(
    monkeypatch, tmp_path, source,
):
    ns = load_script()
    share = __import__("sys").modules["_cctally_dashboard_share"]
    now = dt.datetime(2026, 7, 16, tzinfo=UTC)
    rebuilt = _state("codex", now, total_cost=7.0, daily_label="Prior Codex day")
    calls = []

    def _rebuild(data_snap, *, panel, options):
        calls.append((data_snap, panel, options))
        return rebuilt

    monkeypatch.setattr(share, "_share_codex_state_for_period", _rebuild)
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, result = _render(
            server,
            source_marker=source,
            panel="daily",
            template_id="daily-recap",
            options={"period": {"kind": "previous"}},
        )
        assert status == 200
        assert "Prior Codex day" in result["body"]
        assert "Codex current day" not in result["body"]
        assert len(calls) == 1
        _, panel, options = calls[0]
        assert panel == "daily"
        assert options["period"] == {"kind": "previous"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_share_rejects_unknown_source_with_generic_capability_error(monkeypatch, tmp_path):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, body = _render(server, source_marker="source-root-canary")
        assert status == 400
        assert body == {
            "code": "source_capability_unavailable",
            "error": "source capability unavailable",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_identity_round_trips_presets_history_and_legacy_records_without_mutating_them(
    monkeypatch, tmp_path,
):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    legacy = {
        "share": {
            "presets": {"sessions": {"legacy": {
                "template_id": "sessions-recap", "options": {},
            }}},
            "history": [{
                "recipe_id": "legacy", "panel": "sessions",
                "template_id": "sessions-recap", "options": {},
                "format": "md", "destination": "download",
            }],
        },
    }
    ns["CONFIG_PATH"].write_text(json.dumps(legacy), encoding="utf-8")
    try:
        status, presets = _request(server, "GET", "/api/share/presets")
        assert status == 200
        assert presets["presets"]["sessions"]["legacy"]["source"] == "claude"
        status, history = _request(server, "GET", "/api/share/history")
        assert status == 200
        assert history["history"][0]["source"] == "claude"
        # Reading a legacy recipe only resolves its source in the response.
        assert json.loads(ns["CONFIG_PATH"].read_text(encoding="utf-8")) == legacy

        status, saved = _request(server, "POST", "/api/share/presets", {
            "panel": "sessions", "name": "codex-recap",
            "template_id": "sessions-recap", "options": {}, "source": "codex",
        })
        assert status == 200
        assert saved["source"] == "codex"
        status, recorded = _request(server, "POST", "/api/share/history", {
            "panel": "sessions", "template_id": "sessions-recap",
            "options": {"format": "md"}, "source": "all",
            "format": "md", "destination": "download",
        })
        assert status == 200
        assert recorded["source"] == "all"

        stored = json.loads(ns["CONFIG_PATH"].read_text(encoding="utf-8"))
        assert stored["share"]["presets"]["sessions"]["codex-recap"]["source"] == "codex"
        assert stored["share"]["history"][-1]["source"] == "all"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_compose_expands_all_and_supports_native_forecast(monkeypatch, tmp_path):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, composed = _request(server, "POST", "/api/share/compose", {
            "title": "Provider composition", "theme": "light", "format": "html",
            "no_branding": False, "reveal_projects": False,
            "sections": [_sessions_recipe("codex"), _sessions_recipe("all")],
        })
        assert status == 200
        assert [row["source"] for row in composed["snapshot"]["section_results"]] == [
            "codex", "all",
        ]
        assert composed["body"].count('<section class="share-section"') == 3
        assert "Claude" in composed["body"] and "Codex" in composed["body"]

        status, forecast = _request(server, "POST", "/api/share/render", {
            "panel": "forecast", "template_id": "forecast-recap",
            "source": "codex", "options": {"format": "md"},
        })
        assert status == 200
        assert forecast["snapshot"]["source"] == "codex"
        assert "Forecast" in forecast["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
