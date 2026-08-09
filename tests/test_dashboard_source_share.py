"""Source-safe dashboard share backend contract for #294 S4."""
from __future__ import annotations

import datetime as dt
import http.client
import json
import socketserver
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    CapabilityRecord,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc


def _quota_history(*, status, projected=82.5, current=61.0, now=None, stale_after=3600):
    """One Codex weekly quota history row with an explicit forecast status."""
    return {
        "key": "quota:codex:weekly",
        "source": "codex",
        "label": "Weekly limit",
        "observed_slot": "primary",
        "window_minutes": 10_080,
        "current_percent": current,
        "captured_at": (now or dt.datetime(2026, 7, 16, tzinfo=UTC)).isoformat(),
        "freshness": "stale" if status == "stale" else "fresh",
        "stale_after_seconds": stale_after,
        "forecast": {
            "status": status,
            "current_percent": current,
            "rate_percent_per_hour": 0.5,
            "projected_percent": projected,
            "resets_at": (
                (now or dt.datetime(2026, 7, 16, tzinfo=UTC)) + dt.timedelta(days=2)
            ).isoformat(),
            "remaining_seconds": 172_800,
            "sample_count": 6,
            "sample_span_seconds": 7_200,
            "confidence": "high",
        },
    }


def _state(source, now, *, total_cost, daily_label=None, quota_histories=()):
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
            "quota": {
                "blocks": (), "histories": tuple(quota_histories), "milestones": (),
            },
        },
    )


def _boot(ns, tmp_path, monkeypatch):
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    now = dt.datetime(2026, 7, 16, tzinfo=UTC)
    claude = _state("claude", now, total_cost=1.0)
    codex = _state("codex", now, total_cost=2.0)
    snap = ns["_empty_dashboard_snapshot"]()
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
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
            "data_digest_version_at_add": 2,
            "kernel_version": 1,
        },
    }


def _current_week_recipe(source, digest):
    return {
        "snapshot": {
            "panel": "current-week",
            "template_id": "current-week-recap",
            "source": source,
            "options": {
                "format": "html", "theme": "light",
                "reveal_projects": False, "no_branding": False,
                "show_chart": True, "show_table": True,
            },
            "data_digest_at_add": digest,
            "data_digest_version_at_add": 2,
            "kernel_version": 1,
        },
    }


def _set_hero_freshness(ns, *, claude=None, codex=None):
    snap = ns["DashboardHTTPHandler"].snapshot_ref.get()
    old_claude = snap.source_bundle.sources["claude"]
    old_codex = snap.source_bundle.sources["codex"]

    def changed(state, value):
        if value is None:
            return state
        domain_freshness = dict(state.domain_freshness)
        domain_freshness["hero"] = value
        return replace(state, domain_freshness=domain_freshness)

    new_claude = changed(old_claude, claude)
    new_codex = changed(old_codex, codex)
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={
            "claude": new_claude,
            "codex": new_codex,
            "all": compose_all_state(new_claude, new_codex),
        },
    )
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)
    return old_claude, old_codex, new_claude, new_codex


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


def test_current_week_hero_freshness_changes_disclosure_digest_and_composer_drift(
    monkeypatch, tmp_path,
):
    """RED for #400: weekly rows and provider data_version stay byte-identical
    while the panel-local hero freshness changes."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    note = "Codex current-week spend is based on stale provider-cycle evidence."
    try:
        fresh_by_format = {}
        for fmt in ("md", "html", "svg"):
            status, result = _render(
                server,
                source_marker="codex",
                panel="current-week",
                template_id="current-week-recap",
                options={"format": fmt},
            )
            assert status == 200
            assert note not in result["body"]
            assert "$2.00" in result["body"]
            fresh_by_format[fmt] = result

        assert len({
            result["snapshot"]["data_digest"]
            for result in fresh_by_format.values()
        }) == 1
        fresh_digest = fresh_by_format["md"]["snapshot"]["data_digest"]

        status, weekly_fresh = _render(
            server,
            source_marker="codex",
            panel="weekly",
            template_id="weekly-recap",
        )
        assert status == 200

        old_claude, old_codex, new_claude, new_codex = _set_hero_freshness(
            ns, codex="stale",
        )
        assert old_codex.data_version == new_codex.data_version == "codex-v1"
        assert old_codex.data["periods"]["weekly"] == new_codex.data["periods"]["weekly"]
        assert old_claude.data_version == new_claude.data_version == "claude-v1"

        stale_by_format = {}
        for fmt in ("md", "html", "svg"):
            status, result = _render(
                server,
                source_marker="codex",
                panel="current-week",
                template_id="current-week-recap",
                options={"format": fmt},
            )
            assert status == 200
            assert note in result["body"]
            assert "$2.00" in result["body"]
            stale_by_format[fmt] = result

        stale_digests = {
            result["snapshot"]["data_digest"]
            for result in stale_by_format.values()
        }
        assert len(stale_digests) == 1
        stale_digest = stale_by_format["md"]["snapshot"]["data_digest"]
        assert stale_digest != fresh_digest

        status, weekly_stale = _render(
            server,
            source_marker="codex",
            panel="weekly",
            template_id="weekly-recap",
        )
        assert status == 200
        assert weekly_stale["snapshot"]["data_digest"] == weekly_fresh["snapshot"]["data_digest"]
        assert note not in weekly_stale["body"]

        status, composed = _request(server, "POST", "/api/share/compose", {
            "title": "Current week", "theme": "light", "format": "html",
            "no_branding": False, "reveal_projects": False,
            "sections": [_current_week_recipe("codex", fresh_digest)],
        })
        assert status == 200
        section = composed["snapshot"]["section_results"][0]
        assert section["data_digest_at_add"] == fresh_digest
        assert section["data_digest_now"] == stale_digest
        assert section["drift_detected"] is True
        assert note in composed["body"]
        assert "$2.00" in composed["body"]

        status, refreshed = _request(server, "POST", "/api/share/compose", {
            "title": "Current week", "theme": "light", "format": "html",
            "no_branding": False, "reveal_projects": False,
            "sections": [_current_week_recipe("codex", stale_digest)],
        })
        assert status == 200
        section = refreshed["snapshot"]["section_results"][0]
        assert section["data_digest_now"] == stale_digest
        assert section["drift_detected"] is False
        assert note in refreshed["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("claude_freshness", "codex_freshness", "expected", "absent"),
    (
        ("fresh", "stale", "Codex current-week spend", "Claude current-week spend"),
        ("stale", "fresh", "Claude current-week spend", "Codex current-week spend"),
    ),
)
def test_all_current_week_disclosure_stays_provider_local(
    monkeypatch, tmp_path, claude_freshness, codex_freshness, expected, absent,
):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        _set_hero_freshness(
            ns, claude=claude_freshness, codex=codex_freshness,
        )
        for fmt in ("md", "html", "svg"):
            status, result = _render(
                server,
                source_marker="all",
                panel="current-week",
                template_id="current-week-recap",
                options={"format": fmt},
            )
            assert status == 200
            assert expected in result["body"]
            assert absent not in result["body"]
            assert "Claude" in result["body"]
            assert "Codex" in result["body"]
            assert "$2.00" in result["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_explicit_claude_current_week_retains_actual_and_digests_hero_freshness(
    monkeypatch, tmp_path,
):
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    note = "Claude current-week spend is based on stale provider-cycle evidence."
    try:
        status, fresh = _render(
            server,
            source_marker="claude",
            panel="current-week",
            template_id="current-week-recap",
        )
        assert status == 200
        assert note not in fresh["body"]
        assert "$0.00" in fresh["body"]

        old_claude, _old_codex, new_claude, _new_codex = _set_hero_freshness(
            ns, claude="stale",
        )
        assert old_claude.data_version == new_claude.data_version == "claude-v1"
        assert (
            old_claude.data["periods"]["weekly"]
            == new_claude.data["periods"]["weekly"]
        )

        status, stale = _render(
            server,
            source_marker="claude",
            panel="current-week",
            template_id="current-week-recap",
        )
        assert status == 200
        assert note in stale["body"]
        assert "$0.00" in stale["body"]
        assert stale["snapshot"]["data_digest"] != fresh["snapshot"]["data_digest"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_source_less_current_week_keeps_legacy_shape_when_hero_is_stale(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-08-01T12:00:00Z")
    ns = load_script()
    monkeypatch.setitem(
        ns, "_share_now_utc_iso", lambda: "2026-08-01T12:00:00Z"
    )
    server, thread = _boot(ns, tmp_path, monkeypatch)
    try:
        status, fresh = _render(
            server,
            source_marker=None,
            panel="current-week",
            template_id="current-week-recap",
        )
        assert status == 200
        _set_hero_freshness(ns, claude="stale")
        monkeypatch.setitem(
            ns, "_share_now_utc_iso", lambda: "2026-08-01T12:00:01Z"
        )
        status, stale = _render(
            server,
            source_marker=None,
            panel="current-week",
            template_id="current-week-recap",
        )
        assert status == 200
        assert stale["body"] == fresh["body"]
        assert stale["content_type"] == fresh["content_type"]
        assert fresh["snapshot"]["generated_at"] == "2026-08-01T12:00:00Z"
        assert stale["snapshot"] == {
            **fresh["snapshot"],
            "generated_at": "2026-08-01T12:00:01Z",
        }
        assert "stale provider-cycle evidence" not in stale["body"]
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
            source_schema_version=SOURCE_SCHEMA_VERSION, default_source="claude",
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


# =========================================================================
# #350 spec §3.6 — the shared forecast never publishes a stale projection.
#
# Both the build and the idle clock deliberately PRESERVE `projected_percent`
# alongside `status = "stale"`. Today that is masked because a stale Codex source
# collapses to `unavailable` in the share adapter; §3.4 unmasks it by keeping the
# source coherent, so a shared forecast would publish a stale projection in
# violation of "Projections blank. Actuals stay."
# =========================================================================


def _boot_forecast(ns, tmp_path, monkeypatch, *, status, projected=82.5):
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    now = dt.datetime(2026, 7, 16, tzinfo=UTC)
    claude = _state("claude", now, total_cost=1.0)
    codex = _state(
        "codex", now, total_cost=2.0,
        quota_histories=(_quota_history(status=status, projected=projected, now=now),),
    )
    snap = ns["_empty_dashboard_snapshot"]()
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
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


def _forecast_recipe():
    return {
        "panel": "forecast", "template_id": "forecast-recap", "source": "codex",
        "options": {"format": "md"},
    }


@pytest.mark.parametrize(
    "status", ("stale", "future", "insufficient-history", "unavailable"),
)
def test_share_forecast_blanks_a_projection_whose_status_is_not_ok(
    monkeypatch, tmp_path, status,
):
    ns = load_script()
    server, thread = _boot_forecast(ns, tmp_path, monkeypatch, status=status)
    try:
        status_code, forecast = _request(
            server, "POST", "/api/share/render", _forecast_recipe(),
        )
        assert status_code == 200
        # The backward-looking actual is untouched — only the projection blanks.
        assert "| 61.0% | \u2014 |" in forecast["body"]
        assert "82.5%" not in forecast["body"]

        # Same rule on the compose path.
        status_code, composed = _request(server, "POST", "/api/share/compose", {
            "title": "Forecast", "theme": "light", "format": "html",
            "no_branding": False, "reveal_projects": False,
            "sections": [{
                "snapshot": {
                    "panel": "forecast", "template_id": "forecast-recap",
                    "source": "codex",
                    "options": {
                        "format": "html", "theme": "light",
                        "reveal_projects": False, "no_branding": False,
                        "show_chart": True, "show_table": True,
                    },
                    "data_digest_at_add": "sha256:outdated",
                    "kernel_version": 1,
                },
            }],
        })
        assert status_code == 200
        assert "82.5%" not in composed["body"]
        assert "61.0%" in composed["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_share_forecast_publishes_an_ok_projection_unchanged(monkeypatch, tmp_path):
    """Non-vacuity: an `ok` forecast still publishes its projection verbatim."""
    ns = load_script()
    server, thread = _boot_forecast(ns, tmp_path, monkeypatch, status="ok")
    try:
        status_code, forecast = _request(
            server, "POST", "/api/share/render", _forecast_recipe(),
        )
        assert status_code == 200
        assert "| 61.0% | 82.5% |" in forecast["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_share_forecast_blanks_after_an_idle_clock_turns_the_status_stale(
    monkeypatch, tmp_path,
):
    """The clock re-stamps `forecast.status = "stale"` while PRESERVING
    `projected_percent`, so the share gate must read the status, not the value."""
    import _cctally_dashboard_sources as source_module

    ns = load_script()
    server, thread = _boot_forecast(ns, tmp_path, monkeypatch, status="ok")
    try:
        snap = ns["DashboardHTTPHandler"].snapshot_ref.get()
        bundle = snap.source_bundle
        idle_now = dt.datetime(2026, 7, 16, tzinfo=UTC) + dt.timedelta(hours=4)
        clocked = source_module.refresh_codex_source_clock(
            bundle.sources["codex"], now_utc=idle_now,
        )
        history = clocked.data["quota"]["histories"][0]
        assert history["forecast"]["status"] == "stale"
        assert history["forecast"]["projected_percent"] is not None

        claude = bundle.sources["claude"]
        snap.source_bundle = SourceDashboardBundle(
            source_schema_version=SOURCE_SCHEMA_VERSION,
            default_source="claude",
            source_order=("claude", "codex", "all"),
            sources={
                "claude": claude, "codex": clocked,
                "all": compose_all_state(claude, clocked),
            },
        )
        ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)

        status_code, forecast = _request(
            server, "POST", "/api/share/render", _forecast_recipe(),
        )
        assert status_code == 200
        assert "| 61.0% | \u2014 |" in forecast["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# =====================================================================
# #503 S2 review — M5: the sixth yearless date cell.
#
# `bin/_cctally_dashboard_sources.py` renders each Codex quota block's
# `label` as `%H:%M %b %d` for the dashboard chip, and the Codex source
# `blocks` share panel put that presentational string straight into the
# artifact's `Quota` column. Every other blocks artifact states a full
# ISO instant (`2026-05-07T05:00:00Z`), so the same fact was spelled two
# ways and the Codex one named no year.
# =====================================================================

def _m5_blocks_state(now):
    state = _state("codex", now, total_cost=2.0)
    blocks = ({
        "key": "block:codex:1",
        "source": "codex",
        # Exactly what `_blocks_wire` emits for the dashboard chip.
        "label": "13:00 May 07 UTC",
        "start_at": dt.datetime(2026, 5, 7, 13, 0, tzinfo=UTC).isoformat(),
        "resets_at": dt.datetime(2026, 5, 7, 18, 0, tzinfo=UTC).isoformat(),
        "current_percent": 32.5,
        "window_minutes": 300,
        "cost_usd": 1.85,
    },)
    data = dict(state.data)
    data["quota"] = {**data["quota"], "blocks": blocks}
    return replace(state, data=data)


def test_a_codex_blocks_artifact_states_a_full_iso_block_start(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    ls = ns["_share_load_lib"]()
    share = ns["_load_sibling"]("_cctally_dashboard_share")
    now = dt.datetime(2026, 7, 16, tzinfo=UTC)
    snap = share._build_codex_source_share_snapshot(
        ls, state=_m5_blocks_state(now), panel="blocks",
        template_id="blocks-recap",
        options={"format": "md", "theme": "light", "reveal_projects": False,
                 "display_tz": "Etc/UTC"},
    )
    cell = snap.rows[0].cells["label"]
    assert cell.text == "2026-05-07T13:00:00Z", cell.text
    assert "May" not in cell.text


@pytest.mark.parametrize("row,expected", [
    # No usable start AND no reset: nothing identifies the row.
    ({"label": "13:00 May 07 UTC"}, None),
    ({"label": "13:00 May 07 UTC", "start_at": ""}, None),
    ({"label": "13:00 May 07 UTC", "start_at": "not-a-timestamp"}, None),
    # The row still carries its own reset instant, which names the block
    # and states its year. Discarding it for `(unknown)` threw away
    # information the row was holding (#503 S2 third review).
    ({"label": "13:00 May 07 UTC",
      "resets_at": "2026-05-07T18:00:00Z"}, "resets 2026-05-07T18:00:00Z"),
    ({"label": "13:00 May 07 UTC", "start_at": "not-a-timestamp",
      "resets_at": "2026-05-07T18:00:00+00:00"},
     "resets 2026-05-07T18:00:00Z"),
    # A parseable start always wins, and is stated bare.
    ({"label": "13:00 May 07 UTC", "start_at": "2026-05-07T13:00:00Z",
      "resets_at": "2026-05-07T18:00:00Z"}, "2026-05-07T13:00:00Z"),
])
def test_a_block_row_states_a_full_instant_or_says_it_has_none(
        row, expected, tmp_path, monkeypatch):
    """The fallback reached for `label`, which is only ever the yearless
    chip — so the fix's own escape hatch put the string back.

    Neither D4 tripwire could see it: the module scan does not cover
    `bin/_cctally_dashboard_sources.py`, where the chip is formatted, and
    the golden scan has no committed artifact for this panel (#503 S2
    second review N4).
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    ls = ns["_share_load_lib"]()
    share = ns["_load_sibling"]("_cctally_dashboard_share")
    label = share._codex_block_label(ls, row)
    assert not _M5_YEARLESS_MONTH.search(label), label
    assert label == (share._CODEX_BLOCK_LABEL_UNKNOWN if expected is None
                     else expected)


_M5_YEARLESS_MONTH = __import__("re").compile(
    # Hardcoded literal, deliberately NOT imported from any production
    # constant: a tripwire that reads the value it guards cannot fail
    # when that value changes.
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b"
    r"(?!,?\s*\d{4})"
)

_M5_SHARE_MODULES = (
    "bin/_lib_share.py",
    "bin/_lib_share_templates.py",
    "bin/_cctally_share.py",
    "bin/_cctally_source_analytics.py",
    "bin/_cctally_codex.py",
    "bin/_cctally_dashboard_share.py",
)

# `(module, literal)` pairs that render something OTHER than a share
# artifact and may keep a yearless date. Empty today; an entry here is an
# explicit decision, which is the point of the tripwire.
_M5_ALLOWED_YEARLESS_FORMATS: set = set()


def test_no_share_builder_module_formats_a_yearless_date():
    """#503 S2 D4/F15 asserted six `%b %d` titles and four date cells
    became full ISO. Nothing stopped a seventh from being written."""
    import ast as _ast
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    offenders = []
    for rel in _M5_SHARE_MODULES:
        tree = _ast.parse((root / rel).read_text(encoding="utf-8"))
        docstrings = set()
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                                 _ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], _ast.Expr)
                        and isinstance(body[0].value, _ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    docstrings.add(id(body[0].value))
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and ("%b %d" in node.value or "%b %-d" in node.value)
                    and (rel, node.value) not in _M5_ALLOWED_YEARLESS_FORMATS):
                offenders.append((rel, node.lineno, node.value))
    assert not offenders, offenders


def test_no_committed_share_artifact_states_a_yearless_date():
    """The output-level half of the same tripwire: a format string moved
    to another module would still be caught here."""
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    offenders = []
    for tree_name in ("share", "share-v2", "source-aware", "budget"):
        base = root / "tests" / "fixtures" / tree_name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            # ARTIFACT formats only. The Codex `terminal` and `json`
            # goldens legitimately carry `Jul 01, 2026`, whose year the
            # table wraps onto the next line.
            if not (name.endswith((".md.golden", ".html.golden",
                                   ".svg.golden"))
                    or path.parent.name == "compose"
                    or (name.startswith("golden-fmt-md")
                        or name.startswith("golden-fmt-html")
                        or name.startswith("golden-fmt-svg"))):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in _M5_YEARLESS_MONTH.finditer(text):
                offenders.append((str(path.relative_to(root)), match.group(0)))
    assert not offenders, offenders[:20]


# =====================================================================
# #503 S3 §4 — the drift digest asserts a data change
#
# The digest must hash a canonical projection of the BUILT, PRE-TOGGLE
# ShareSnapshots and nothing else. Today it hashes ambient process state:
# `claude_panel_data` carries `_share_now_utc()` readings at microsecond
# resolution (`projects.period_end`, `current-week.kpi_days_remaining`),
# and `providers[]` carries `data_version` plus a whole source-state
# domain the artifact never draws. The tests below are the reproduction
# and its discrimination controls.
# =====================================================================

_S3_T0 = dt.datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


class _S3Clock:
    """A settable `_share_now_utc` so a render can be driven at an instant."""

    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance_ms(self, milliseconds):
        self.moment = self.moment + dt.timedelta(milliseconds=milliseconds)


def _s3_pin_clock(ns, monkeypatch, moment=_S3_T0):
    clock = _S3Clock(moment)
    monkeypatch.setitem(ns, "_share_now_utc", clock)
    monkeypatch.setitem(
        ns, "_share_now_utc_iso",
        lambda: clock.moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    # Deterministic + DB-free: the current-week builder aggregates
    # `[week_start, now]` through this helper on every call.
    monkeypatch.setitem(ns, "_share_top_projects_for_range",
                        lambda *_args, **_kwargs: [])
    return clock


def _s3_projects_envelope(*, cost=12.5):
    return {
        "current_week": {
            "week_start_at": (_S3_T0 - dt.timedelta(days=3)).isoformat(),
            "total_cost_usd": cost,
            "rows": [{
                "key": "alpha", "bucket_path": "/repos/alpha",
                "cost_usd": cost, "attributed_pct": 4.0, "sessions_count": 3,
            }],
        },
        "trend": {},
    }


def _s3_current_week(*, spent_usd=9.0, week_end_at=None):
    return SimpleNamespace(
        week_start_at=_S3_T0 - dt.timedelta(days=3),
        week_end_at=week_end_at or (_S3_T0 + dt.timedelta(days=4)),
        used_pct=42.0,
        five_hour_pct=None,
        five_hour_resets_at=None,
        spent_usd=spent_usd,
        dollars_per_percent=0.21,
        latest_snapshot_at=_S3_T0,
    )


def _s3_install_claude_panels(ns, *, projects=None, current_week=None):
    """Give the served DataSnapshot the Claude panel state a share reads."""
    snap = ns["DashboardHTTPHandler"].snapshot_ref.get()
    snap.projects_envelope = (
        _s3_projects_envelope() if projects is None else projects
    )
    snap.current_week = (
        _s3_current_week() if current_week is None else current_week
    )
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)
    return snap


def _s3_replace_source(ns, source, state):
    """Swap one provider state into the served bundle, recomposing `all`."""
    snap = ns["DashboardHTTPHandler"].snapshot_ref.get()
    sources = dict(snap.source_bundle.sources)
    sources[source] = state
    claude = sources["claude"]
    codex = sources["codex"]
    snap.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={
            "claude": claude, "codex": codex,
            "all": compose_all_state(claude, codex),
        },
    )
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)
    return state


def _s3_codex_state(ns):
    return ns["DashboardHTTPHandler"].snapshot_ref.get().source_bundle.sources["codex"]


def _s3_with_data(state, data):
    return replace(state, data=data)


def _s3_digest(server, **render_kwargs):
    status, result = _render(server, **render_kwargs)
    assert status == 200, result
    return result["snapshot"]["data_digest"], result


def _s3_recipe(*, panel, template_id, source, digest, options=None):
    return {
        "snapshot": {
            "panel": panel,
            "template_id": template_id,
            "source": source,
            "options": {
                "format": "md", "theme": "light",
                "reveal_projects": False, "no_branding": False,
                **(options or {}),
            },
            "data_digest_at_add": digest,
            "data_digest_version_at_add": 2,
            "kernel_version": 1,
        },
    }


@pytest.mark.parametrize(
    ("panel", "template_id"),
    (("projects", "projects-recap"), ("current-week", "current-week-recap")),
)
def test_share_digest_is_stable_across_ten_milliseconds(
    monkeypatch, tmp_path, panel, template_id,
):
    """#503 S3 §4 reproduction: with every source datum frozen, two renders
    ten milliseconds apart inside one civil day must produce one digest.

    Today they do not: `_share_now_utc()` reaches the digest raw through
    `claude_panel_data` (`projects.period_end`, `current-week`'s
    `kpi_days_remaining`), and `_data_digest` serializes a datetime with
    `default=str` — microsecond resolution.
    """
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    clock = _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        digests = []
        for offset_ms in (0, 10):
            clock.moment = _S3_T0 + dt.timedelta(milliseconds=offset_ms)
            digest, _ = _s3_digest(
                server, source_marker="claude", panel=panel,
                template_id=template_id,
            )
            digests.append(digest)
        assert digests[0] == digests[1], (
            "a 10 ms clock delta moved the digest; a wall-clock reading "
            "reaches it raw"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("panel", "template_id"),
    (("projects", "projects-recap"), ("current-week", "current-week-recap")),
)
def test_share_digest_ten_milliseconds_later_does_not_compose_as_drift(
    monkeypatch, tmp_path, panel, template_id,
):
    """The compose half of the same reproduction: a section added at t and
    composed at t+10 ms is not outdated."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    clock = _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        at_add, _ = _s3_digest(
            server, source_marker="claude", panel=panel,
            template_id=template_id,
        )
        clock.advance_ms(10)
        status, composed = _request(server, "POST", "/api/share/compose", {
            "title": "Ten milliseconds", "theme": "light", "format": "md",
            "no_branding": False, "reveal_projects": False,
            "sections": [_s3_recipe(
                panel=panel, template_id=template_id, source="claude",
                digest=at_add,
            )],
        })
        assert status == 200, composed
        section = composed["snapshot"]["section_results"][0]
        assert section["data_digest_now"] == at_add
        assert section["drift_detected"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_share_digest_changes_when_a_rendered_project_cost_changes(
    monkeypatch, tmp_path,
):
    """The inverse control. Without it the timing test above could pass
    against a digest that never moves at all."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        before, first = _s3_digest(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
        )
        assert "12.50" in first["body"]

        _s3_install_claude_panels(
            ns, projects=_s3_projects_envelope(cost=99.5),
        )
        after, second = _s3_digest(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
        )
        assert "99.50" in second["body"]
        assert after != before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# --- The eight discrimination controls (spec §4 Verification table) ---


def _s3_codex_blocks_state(ns, *, block_minute=0, history_remaining=172_800):
    """A Codex state whose quota domain carries one drawn block row plus
    history the blocks artifact never draws."""
    base = _s3_codex_state(ns)
    day = dt.datetime(2026, 7, 16, tzinfo=UTC)
    start_at = day + dt.timedelta(hours=9, minutes=block_minute)
    data = dict(base.data)
    data["quota"] = {
        "blocks": ({
            "start_at": start_at.isoformat(),
            "resets_at": (start_at + dt.timedelta(hours=5)).isoformat(),
            "current_percent": 40.0,
        },),
        "histories": ({
            "key": "quota:codex:weekly",
            "current_percent": 61.0,
            "forecast": {
                "status": "ok", "projected_percent": 82.5,
                "remaining_seconds": history_remaining,
            },
        },),
        "milestones": (),
    }
    return _s3_with_data(base, data)


def test_s3_control_codex_trend_and_forecast_content_moves_the_digest(
    monkeypatch, tmp_path,
):
    """Control 1. `_share_state_domain` routes `trend` and `forecast` to
    domains Codex does not publish, so today only `data_version` separates
    two different Codex Trend/Forecast artifacts."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    base = _s3_codex_state(ns)
    _s3_replace_source(ns, "codex", _s3_with_data(base, {
        **dict(base.data),
        "quota": {
            "blocks": (),
            "histories": ({"key": "quota:codex:weekly", "label": "Weekly limit",
                           "current_percent": 61.0,
                           "forecast": {"status": "ok",
                                        "projected_percent": 82.5}},),
            "milestones": (),
        },
    }))
    try:
        trend_before, trend_first = _s3_digest(
            server, source_marker="codex", panel="trend",
            template_id="trend-recap",
        )
        forecast_before, forecast_first = _s3_digest(
            server, source_marker="codex", panel="forecast",
            template_id="forecast-recap",
        )
        assert "61.0%" in forecast_first["body"]

        state = _s3_codex_state(ns)
        data = dict(state.data)
        periods = {key: dict(value) for key, value in data["periods"].items()}
        weekly_rows = [dict(row) for row in periods["weekly"]["rows"]]
        weekly_rows[0]["cost_usd"] = 88.25
        periods["weekly"] = {**periods["weekly"], "rows": tuple(weekly_rows),
                             "total_cost_usd": 88.25}
        data["periods"] = periods
        histories = [dict(row) for row in data["quota"]["histories"]]
        histories[0]["current_percent"] = 77.5
        data["quota"] = {**data["quota"], "histories": tuple(histories)}
        # data_version is deliberately NOT advanced: the control must prove
        # rendered CONTENT is the signal.
        _s3_replace_source(ns, "codex", _s3_with_data(state, data))

        trend_after, trend_second = _s3_digest(
            server, source_marker="codex", panel="trend",
            template_id="trend-recap",
        )
        forecast_after, forecast_second = _s3_digest(
            server, source_marker="codex", panel="forecast",
            template_id="forecast-recap",
        )
        assert trend_second["body"] != trend_first["body"]
        assert "77.5%" in forecast_second["body"]
        assert trend_after != trend_before
        assert forecast_after != forecast_before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_control_all_mode_codex_only_change_moves_the_digest(
    monkeypatch, tmp_path,
):
    """Control 2. Both child snapshots are in the projection, so a change
    confined to the Codex child still moves the composite digest."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        before, first = _s3_digest(
            server, source_marker="all", panel="weekly",
            template_id="weekly-recap",
        )
        state = _s3_codex_state(ns)
        data = dict(state.data)
        periods = {key: dict(value) for key, value in data["periods"].items()}
        rows = [dict(row) for row in periods["weekly"]["rows"]]
        rows[0]["cost_usd"] = 55.5
        periods["weekly"] = {**periods["weekly"], "rows": tuple(rows),
                             "total_cost_usd": 55.5}
        data["periods"] = periods
        _s3_replace_source(ns, "codex", _s3_with_data(state, data))

        after, second = _s3_digest(
            server, source_marker="all", panel="weekly",
            template_id="weekly-recap",
        )
        assert second["body"] != first["body"]
        assert after != before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("panel", "template_id"),
    (
        ("current-week", "current-week-recap"),
        ("trend", "trend-recap"),
        ("forecast", "forecast-recap"),
        ("daily", "daily-recap"),
        ("monthly", "monthly-recap"),
        ("weekly", "weekly-recap"),
        ("blocks", "blocks-recap"),
        ("sessions", "sessions-recap"),
        ("projects", "projects-recap"),
    ),
)
@pytest.mark.parametrize("availability", ("partial", "empty"))
def test_s3_control_codex_availability_change_moves_the_digest(
    monkeypatch, tmp_path, panel, template_id, availability,
):
    """Control 3. Every Codex panel projects state availability into its
    built snapshot, visible source chrome, and version-2 drift digest.

    Keep all underlying rows fixed: this isolates the state transition from
    the generic Codex builder's row-count-derived default.
    """
    ns = load_script()
    share_lib = ns["_share_load_lib"]()
    rendered = []
    original_render = share_lib.render

    def capture(snapshot, **kwargs):
        rendered.append(snapshot)
        return original_render(snapshot, **kwargs)

    monkeypatch.setattr(share_lib, "render", capture)
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    try:
        before, first = _s3_digest(
            server, source_marker="codex", panel=panel,
            template_id=template_id,
        )
        assert rendered[-1].availability == "ok"
        assert rendered[-1].availability_reason is None

        state = _s3_codex_state(ns)
        _s3_replace_source(ns, "codex", replace(state, availability=availability))

        after, second = _s3_digest(
            server, source_marker="codex", panel=panel,
            template_id=template_id,
        )
        expected = "unavailable" if availability == "partial" else "empty"
        expected_reason = (
            "source data unavailable" if expected == "unavailable" else None
        )
        expected_chrome = (
            "Unavailable: source data unavailable"
            if expected == "unavailable" else "No data"
        )
        assert rendered[-1].availability == expected
        assert rendered[-1].availability_reason == expected_reason
        assert expected_chrome in second["body"]
        assert second["body"] != first["body"]
        assert after != before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_control_codex_blocks_reclock_leaves_the_digest_alone(
    monkeypatch, tmp_path,
):
    """Control 4. `blocks` draws only `quota.blocks`; histories, forecast
    seconds and the summary never enter the built snapshot, so an idle
    reclock must not drift a basket section."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_replace_source(ns, "codex", _s3_codex_blocks_state(ns))
    try:
        before, first = _s3_digest(
            server, source_marker="codex", panel="blocks",
            template_id="blocks-recap",
        )
        _s3_replace_source(
            ns, "codex",
            _s3_codex_blocks_state(ns, history_remaining=169_200),
        )
        after, second = _s3_digest(
            server, source_marker="codex", panel="blocks",
            template_id="blocks-recap",
        )
        assert second["body"] == first["body"]
        assert after == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_control_two_same_day_codex_blocks_windows_move_the_digest(
    monkeypatch, tmp_path,
):
    """Control 5 — the pre-toggle control. With `show_table: false` the
    rows are the only field carrying window identity, and the toggle
    erases them before the digest is taken."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_replace_source(ns, "codex", _s3_codex_blocks_state(ns, block_minute=0))
    try:
        before, first = _s3_digest(
            server, source_marker="codex", panel="blocks",
            template_id="blocks-recap", options={"show_table": False},
        )
        _s3_replace_source(
            ns, "codex", _s3_codex_blocks_state(ns, block_minute=45),
        )
        after, second = _s3_digest(
            server, source_marker="codex", panel="blocks",
            template_id="blocks-recap", options={"show_table": False},
        )
        # The hidden table is what makes the two artifacts identical.
        assert second["body"] == first["body"]
        assert after != before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_control_current_week_visual_ignores_days_remaining(
    monkeypatch, tmp_path,
):
    """Control 6. `current-week-visual` never places the KPI in rows,
    chart or totals, so moving the reset instant alone is not a data
    change for that template."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        before, first = _s3_digest(
            server, source_marker="claude", panel="current-week",
            template_id="current-week-visual",
        )
        _s3_install_claude_panels(ns, current_week=_s3_current_week(
            week_end_at=_S3_T0 + dt.timedelta(days=6),
        ))
        after, second = _s3_digest(
            server, source_marker="claude", panel="current-week",
            template_id="current-week-visual",
        )
        assert second["body"] == first["body"]
        assert after == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_control_data_version_alone_leaves_the_digest_alone(
    monkeypatch, tmp_path,
):
    """Control 7. No snapshot builder consumes `data_version`; it is
    provider bookkeeping, not a rendered value."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    try:
        before, first = _s3_digest(server, source_marker="codex")
        state = _s3_codex_state(ns)
        _s3_replace_source(ns, "codex", replace(state, data_version="codex-v9"))
        after, second = _s3_digest(server, source_marker="codex")
        assert second["body"] == first["body"]
        assert after == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_control_content_toggles_leave_the_digest_alone(
    monkeypatch, tmp_path,
):
    """Control 8. `show_chart` / `show_table` are render knobs — the
    comment above the render digest already promises they are not drift."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        both_on, shown = _s3_digest(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
            options={"show_chart": True, "show_table": True},
        )
        both_off, hidden = _s3_digest(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
            options={"show_chart": False, "show_table": False},
        )
        assert hidden["body"] != shown["body"]
        assert both_off == both_on
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# The four render-only knobs, one variation at a time against a fixed
# baseline. Spec §4 states the contract as covering all four together —
# "no current template builder reads any of those four (`format`, `theme`,
# `reveal_projects`, `no_branding`)" — so a matrix that varied `format`
# alone defended a quarter of it. Each entry differs from the baseline in
# exactly one knob, which is what makes a failure name the guilty knob.
_S3_RENDER_ONLY_VARIANTS = (
    ("format=md",            {"format": "md"}),
    ("format=html",          {"format": "html"}),
    ("format=svg",           {"format": "svg"}),
    ("theme=dark",           {"format": "md", "theme": "dark"}),
    ("reveal_projects=True", {"format": "md", "reveal_projects": True}),
    ("no_branding=True",     {"format": "md", "no_branding": True}),
)


def test_share_digest_ignores_every_render_only_knob_across_the_registry(
    monkeypatch, tmp_path,
):
    """The widened form of the single-template assertion above.

    The digest is render-knob-independent by CONTRACT, not by construction:
    compose inserts `format`, `theme`, `reveal_projects` and `no_branding`
    into builder options before the snapshot is built, and what makes the
    digest survive that is only that no current builder reads those four.
    Driving the whole registry × every source × all four knobs is what keeps
    a future builder from quietly starting to read one of them.

    `reveal_projects` is also where the anonymization claim is pinned: the
    digest hashes the snapshots the builders produced, and every scrub
    happens later inside `render()` / `compose()`, so flipping the privacy
    knob must leave every section's digest byte-identical.
    """
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        # Warms the late-loaded registry into sys.modules; the handler and
        # this test then read the same ShareTemplate instances.
        assert _render(server, source_marker="claude")[0] == 200
        tpl_mod = __import__("sys").modules["_lib_share_templates"]
        assert len(tpl_mod.SHARE_TEMPLATES) == 27

        checked = []
        for source in ("claude", "codex", "all"):
            for template in tpl_mod.SHARE_TEMPLATES:
                digests = {}
                statuses = set()
                for label, options in _S3_RENDER_ONLY_VARIANTS:
                    status, result = _render(
                        server, source_marker=source, panel=template.panel,
                        template_id=template.id, options=options,
                    )
                    statuses.add(status)
                    if status == 200:
                        digests[label] = result["snapshot"]["data_digest"]
                assert len(statuses) == 1, (source, template.id, statuses)
                if statuses == {200}:
                    # Non-vacuity per cell: every knob variation actually
                    # produced a digest to compare, so a silently-dropped
                    # axis cannot masquerade as agreement.
                    assert len(digests) == len(_S3_RENDER_ONLY_VARIANTS), (
                        source, template.id, sorted(digests),
                    )
                    assert len(set(digests.values())) == 1, (
                        source, template.id, digests,
                    )
                    checked.append((source, template.id))
        # Non-vacuity: the matrix must actually have rendered a full
        # registry's worth of combinations, not silently degraded to none.
        assert len(checked) >= 27, checked
        assert len(_S3_RENDER_ONLY_VARIANTS) == 6, _S3_RENDER_ONLY_VARIANTS
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_a_section_stored_under_an_older_digest_version_is_not_drifted(
    monkeypatch, tmp_path,
):
    """A6. Redefining the digest must not badge every stored basket section
    Outdated exactly once. A missing or older `data_digest_version_at_add`
    is NOT COMPARABLE, and not comparable is not drifted."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        stale, _ = _s3_digest(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
        )
        # A genuine data change, so a COMPARABLE digest would drift.
        _s3_install_claude_panels(ns, projects=_s3_projects_envelope(cost=99.5))

        def compose_with(version):
            recipe = _s3_recipe(
                panel="projects", template_id="projects-recap",
                source="claude", digest=stale,
            )
            if version is None:
                recipe["snapshot"].pop("data_digest_version_at_add")
            else:
                recipe["snapshot"]["data_digest_version_at_add"] = version
            status, composed = _request(server, "POST", "/api/share/compose", {
                "title": "Version gate", "theme": "light", "format": "md",
                "no_branding": False, "reveal_projects": False,
                "sections": [recipe],
            })
            assert status == 200, composed
            return composed["snapshot"]["section_results"][0]

        for version in (None, 1):
            result = compose_with(version)
            assert result["digest_comparable"] is False
            assert result["drift_detected"] is False
            assert result["data_digest_now"] != stale

        current = compose_with(2)
        assert current["digest_comparable"] is True
        assert current["drift_detected"] is True
        assert current["data_digest_version"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_a_failing_digest_projection_still_answers_with_an_empty_digest(
    monkeypatch, tmp_path,
):
    """The projection runs INSIDE the render handler's defensive `try`.

    `_share_digest_value` raises `TypeError` on a value it cannot serialize,
    which is deliberate — it keeps the empty-digest fallback defensive rather
    than letting a `repr` be hashed. But `do_POST` has no exception guard, so
    building the projection outside that `try` turned a raise into a dropped
    connection instead of the artifact plus the empty digest the fallback
    promises. The compose handler always had the call inside its guard.
    """
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    _s3_install_claude_panels(ns)
    try:
        # One real render first, so the share module is loaded and the
        # baseline shows the digest is normally non-empty here.
        status, healthy = _render(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
        )
        assert status == 200, healthy
        assert healthy["snapshot"]["data_digest"].startswith("sha256:")

        share = __import__("sys").modules["_cctally_dashboard_share"]

        def _unserializable(**_kwargs):
            raise TypeError("unsupported value in the digest projection")

        monkeypatch.setattr(share, "_share_digest_input", _unserializable)

        status, result = _render(
            server, source_marker="claude", panel="projects",
            template_id="projects-recap",
        )
        # The artifact still ships; only the digest degrades.
        assert status == 200, result
        assert result["body"]
        assert result["snapshot"]["data_digest"] == ""
        assert result["snapshot"]["data_digest_version"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_s3_render_stamps_the_digest_version(monkeypatch, tmp_path):
    """The version travels on the render response so the client can store it
    beside the digest it belongs to."""
    ns = load_script()
    server, thread = _boot(ns, tmp_path, monkeypatch)
    _s3_pin_clock(ns, monkeypatch)
    try:
        for source_marker in (None, "claude", "codex", "all"):
            status, result = _render(server, source_marker=source_marker)
            assert status == 200, result
            assert result["snapshot"]["data_digest_version"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
