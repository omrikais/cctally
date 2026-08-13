"""Tests for /api/share/* HTTP endpoints.

Boots the real DashboardHTTPHandler against a tmp-dir-redirected install
and exercises the share endpoints end-to-end (HTTP wire, CSRF gate,
late-imported template registry, kernel render).

Fixture pattern mirrors tests/test_dashboard_api_block.py — minimal
wiring, no seeded entries (empty snapshot suffices for these contracts).
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

import pytest

from conftest import load_script, redirect_paths


def _start_share_dashboard_server(ns, tmp_path, monkeypatch):
    """Boot DashboardHTTPHandler with an empty snapshot for share endpoint tests.

    Mirrors tests/test_dashboard_api_block.py's `_start_dashboard_server` —
    same TCPServer + daemon-thread idiom — but skips the cache-seeding step
    since /api/share/* endpoints do not read from `cache.db` (panel_data
    comes from `snapshot_ref`, which we wire to an empty snapshot here).
    """
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    import socketserver
    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    HandlerCls.snapshot_ref = SnapshotRef(ns["_empty_dashboard_snapshot"]())
    HandlerCls.hub = SSEHub()
    HandlerCls.sync_lock = threading.Lock()
    HandlerCls.run_sync_now = staticmethod(lambda: None)
    HandlerCls.run_sync_now_locked = staticmethod(lambda: None)
    HandlerCls.no_sync = False
    HandlerCls.display_tz_pref_override = None

    srv = socketserver.TCPServer(("127.0.0.1", 0), HandlerCls)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture
def dashboard_server(tmp_path, monkeypatch):
    """Spin up the cctally dashboard server on a random port.

    Yields (port, csrf_token); csrf_token is None for v2 (Origin/Host parity).
    """
    ns = load_script()
    srv = _start_share_dashboard_server(ns, tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        yield port, None
    finally:
        srv.shutdown()


# ---------- M1.5 — GET /api/share/templates ----------


def test_share_templates_returns_panel_templates(dashboard_server):
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/templates?panel=weekly",
        headers={"Host": f"127.0.0.1:{port}"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    assert body["panel"] == "weekly"
    ids = [t["id"] for t in body["templates"]]
    assert "weekly-recap" in ids


def test_share_templates_rejects_unknown_panel(dashboard_server):
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/templates?panel=alerts",
        headers={"Host": f"127.0.0.1:{port}"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    body = json.loads(exc.value.read())
    assert "error" in body


# ---------- M1.6 — POST /api/share/render ----------


def _csrf_headers(port):
    return {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
    }


def test_share_render_returns_body_and_snapshot(dashboard_server):
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly",
        "template_id": "weekly-recap",
        "options": {"format": "svg", "theme": "light", "reveal_projects": True,
                    "no_branding": False, "top_n": 5,
                    "period": {"kind": "current"},
                    "project_allowlist": None,
                    "show_chart": True, "show_table": True},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    assert body["content_type"] == "image/svg+xml"
    assert body["body"].startswith("<svg") or body["body"].lstrip().startswith("<svg")
    snap = body["snapshot"]
    assert snap["panel"] == "weekly"
    assert snap["template_id"] == "weekly-recap"
    assert snap["data_digest"].startswith("sha256:")
    assert isinstance(snap["kernel_version"], int)


def test_share_render_md_frontmatter_carries_template_id(dashboard_server):
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly",
        "template_id": "weekly-recap",
        "options": {"format": "md", "theme": "light", "reveal_projects": True,
                    "no_branding": False, "top_n": 5,
                    "period": {"kind": "current"},
                    "project_allowlist": None,
                    "show_chart": True, "show_table": True},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    assert body["content_type"] == "text/markdown"
    assert "\ntemplate_id: weekly-recap\n" in body["body"]


def test_share_render_rejects_unknown_template(dashboard_server):
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly", "template_id": "weekly-bogus",
        "options": {"format": "md"},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST",
        headers=_csrf_headers(port),
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    err = json.loads(exc.value.read())
    assert "template" in err["error"].lower()


def test_share_render_accepts_null_top_n(dashboard_server):
    """Codex P2 on PR #35 — Knobs.tsx emits `top_n: null` when the
    Top-N input is cleared (Knobs.tsx:43). The validator must treat
    null as "not provided" rather than 400-ing every preview/export
    until the user types a number. Regression for the rejection path
    is exercised by test_share_render_rejects_invalid_top_n below."""
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {"format": "md", "top_n": None,
                    "period": {"kind": "current"}},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    assert body["content_type"] == "text/markdown"


def test_share_render_rejects_invalid_top_n(dashboard_server):
    """Belt to the null-acceptance test above: non-null invalid values
    (0, negative ints, non-ints, bool) MUST still 400 with the
    same error envelope."""
    port, _ = dashboard_server
    for bad in (0, -3, "five", True):
        req_body = json.dumps({
            "panel": "weekly", "template_id": "weekly-recap",
            "options": {"format": "md", "top_n": bad},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=req_body, method="POST",
            headers=_csrf_headers(port),
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400, f"expected 400 for top_n={bad!r}"
        err = json.loads(exc.value.read())
        assert err["field"] == "options.top_n"


def test_share_render_show_chart_false_strips_chart(dashboard_server):
    """Codex P2 on PR #35 — `show_chart=False` must drop the chart from
    the rendered output. SVG carries an `<svg>` body whose only LineChart
    we can probe via the absence of `<polyline`; MD includes the chart's
    `_emit_md` output if the chart is present."""
    port, _ = dashboard_server
    base = {"panel": "weekly", "template_id": "weekly-recap"}
    common_opts = {"format": "svg", "theme": "light",
                   "reveal_projects": True, "no_branding": False,
                   "period": {"kind": "current"}}
    # With chart (control).
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=json.dumps({**base, "options": {**common_opts,
                              "show_chart": True, "show_table": True}}).encode(),
            method="POST", headers=_csrf_headers(port),
        ), timeout=5,
    ) as r:
        with_chart = json.loads(r.read())["body"]
    # Without chart.
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=json.dumps({**base, "options": {**common_opts,
                              "show_chart": False, "show_table": True}}).encode(),
            method="POST", headers=_csrf_headers(port),
        ), timeout=5,
    ) as r:
        without_chart = json.loads(r.read())["body"]
    # The SVG chart renderer emits `<polyline` for the LineChart trace.
    # Stripping `chart` from the snapshot must remove that element.
    assert "<polyline" in with_chart, "control: chart-on should render polyline"
    assert "<polyline" not in without_chart, "show_chart=False must strip chart"


def test_share_render_show_table_false_strips_table(dashboard_server):
    """Codex P2 on PR #35 — `show_table=False` must drop the table from
    the rendered output. Probe via MD format where the table renders as
    a pipe-delimited block."""
    port, _ = dashboard_server
    base = {"panel": "daily", "template_id": "daily-recap"}
    common_opts = {"format": "md", "theme": "light",
                   "reveal_projects": True, "no_branding": True,
                   "period": {"kind": "current"}}
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=json.dumps({**base, "options": {**common_opts,
                              "show_chart": True, "show_table": True}}).encode(),
            method="POST", headers=_csrf_headers(port),
        ), timeout=5,
    ) as r:
        with_table = json.loads(r.read())["body"]
    with urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=json.dumps({**base, "options": {**common_opts,
                              "show_chart": True, "show_table": False}}).encode(),
            method="POST", headers=_csrf_headers(port),
        ), timeout=5,
    ) as r:
        without_table = json.loads(r.read())["body"]
    # MD table heading divider `| --- |` is the canonical signal.
    assert "---" in with_table, "control: show_table=True should render the table"
    # Without table, MD body must have no pipe-table rows. (Frontmatter
    # would carry `---` so we strip --no-branding by setting no_branding
    # = True via the request options above.)
    assert " | " not in without_table, (
        "show_table=False must strip pipe-table rows from MD body"
    )


def test_share_render_accepts_period_current(dashboard_server):
    """Codex P2 on PR #35 — `kind='current'` is the default (no override);
    server must accept and render without DB re-query."""
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {"format": "md", "period": {"kind": "current"}},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST", headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200


def test_share_render_rejects_unknown_period_kind(dashboard_server):
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {"format": "md", "period": {"kind": "bogus"}},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST", headers=_csrf_headers(port),
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    err = json.loads(exc.value.read())
    assert err["field"] == "options.period.kind"
    assert "bogus" in err["error"]


def test_share_render_rejects_period_override_for_forecast(dashboard_server):
    """forecast/current-week panels have intrinsic period semantics —
    `kind='previous'` and `kind='custom'` are meaningless and rejected
    with 400."""
    port, _ = dashboard_server
    for panel, tpl in [("forecast", "forecast-recap"),
                        ("current-week", "current-week-recap")]:
        for kind in ("previous", "custom"):
            req_body = json.dumps({
                "panel": panel, "template_id": tpl,
                "options": {"format": "md", "period": {"kind": kind,
                                                        "start": "2026-05-01",
                                                        "end": "2026-05-07"}},
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/share/render",
                data=req_body, method="POST", headers=_csrf_headers(port),
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 400, (
                f"expected 400 for panel={panel} kind={kind}"
            )
            err = json.loads(exc.value.read())
            assert err["field"] == "options.period.kind"


def test_share_render_rejects_custom_period_without_start_end(dashboard_server):
    port, _ = dashboard_server
    for bad_period in (
        {"kind": "custom"},                                      # no dates
        {"kind": "custom", "start": "2026-05-04"},                # no end
        {"kind": "custom", "start": "", "end": "2026-05-10"},     # empty start
    ):
        req_body = json.dumps({
            "panel": "weekly", "template_id": "weekly-recap",
            "options": {"format": "md", "period": bad_period},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=req_body, method="POST", headers=_csrf_headers(port),
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400, f"expected 400 for {bad_period!r}"
        err = json.loads(exc.value.read())
        assert err["field"].startswith("options.period")


def test_share_render_rejects_inverted_custom_range(dashboard_server):
    """end <= start → 400 (spec §6.2: "invalid options: custom-period range inverted")."""
    port, _ = dashboard_server
    req_body = json.dumps({
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {"format": "md", "period": {"kind": "custom",
                                                "start": "2026-05-10",
                                                "end": "2026-05-04"}},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST", headers=_csrf_headers(port),
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    err = json.loads(exc.value.read())
    assert "after start" in err["error"].lower() or "inverted" in err["error"].lower()


def test_share_render_previous_period_accepted_for_overridable_panels(dashboard_server):
    """`kind='previous'` opens a DB connection and re-builds the panel
    field with shifted now_utc. On an empty test DB this still returns
    200 with an empty / zero-stub body — the wiring works without
    crashing the handler."""
    port, _ = dashboard_server
    for panel, tpl in [
        ("weekly", "weekly-recap"),
        ("daily", "daily-recap"),
        ("monthly", "monthly-recap"),
        ("trend", "trend-recap"),
        ("blocks", "blocks-recap"),
    ]:
        req_body = json.dumps({
            "panel": panel, "template_id": tpl,
            "options": {"format": "md", "period": {"kind": "previous"}},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=req_body, method="POST", headers=_csrf_headers(port),
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200, f"panel={panel}"
            body = json.loads(r.read())
            assert body["content_type"] == "text/markdown"


def test_share_render_custom_period_accepted_for_overridable_panels(dashboard_server):
    """`kind='custom'` with a valid range re-builds via DB and renders."""
    port, _ = dashboard_server
    custom = {"kind": "custom", "start": "2026-04-27", "end": "2026-05-04"}
    for panel, tpl in [
        ("weekly", "weekly-recap"),
        ("daily", "daily-recap"),
        ("trend", "trend-recap"),
    ]:
        req_body = json.dumps({
            "panel": panel, "template_id": tpl,
            "options": {"format": "md", "period": custom},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/render",
            data=req_body, method="POST", headers=_csrf_headers(port),
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200, f"panel={panel}"


def test_share_render_csrf_blocks_cross_origin(dashboard_server):
    port, _ = dashboard_server
    req_body = json.dumps({"panel": "weekly", "template_id": "weekly-recap",
                            "options": {"format": "md"}}).encode()
    bad_headers = {
        "Host": f"127.0.0.1:{port}",
        "Origin": "http://evil.example",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST", headers=bad_headers,
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


# ---------- M2.3 — /api/share/presets CRUD ----------


def test_presets_initial_get_returns_empty(dashboard_server):
    port, _ = dashboard_server
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/presets", timeout=5,
    ) as r:
        body = json.loads(r.read())
    assert body == {"presets": {}}


def test_presets_post_roundtrip_then_get(dashboard_server):
    port, _ = dashboard_server
    payload = {
        "panel": "weekly", "name": "team-monday",
        "template_id": "weekly-recap",
        "options": {"theme": "dark", "format": "md",
                    "reveal_projects": False, "top_n": 5},
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        post_body = json.loads(r.read())
    assert post_body["panel"] == "weekly"
    assert post_body["name"] == "team-monday"
    assert "saved_at" in post_body

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/presets", timeout=5,
    ) as r:
        get_body = json.loads(r.read())
    assert get_body["presets"]["weekly"]["team-monday"]["template_id"] == "weekly-recap"
    assert get_body["presets"]["weekly"]["team-monday"]["options"]["theme"] == "dark"


def test_presets_post_overwrites_same_name(dashboard_server):
    port, _ = dashboard_server
    base = {"panel": "weekly", "name": "team-monday",
            "template_id": "weekly-recap",
            "options": {"theme": "light", "format": "md"}}
    # `overwrite` is now required to replace an existing name (#503 S3 §1);
    # without it the second POST is a 409, which the conflict test below
    # pins. This test keeps pinning that an overwrite REPLACES rather than
    # appending a second record.
    updated = {**base, "overwrite": True,
               "options": {"theme": "dark", "format": "html"}}
    for payload in (base, updated):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/presets",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={**_csrf_headers(port), "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/presets", timeout=5,
    ) as r:
        body = json.loads(r.read())
    presets = body["presets"]["weekly"]
    assert len(presets) == 1, "second POST should overwrite, not append"
    assert presets["team-monday"]["options"]["theme"] == "dark"


def test_presets_delete_removes_entry(dashboard_server):
    port, _ = dashboard_server
    payload = {"panel": "weekly", "name": "team-monday",
               "template_id": "weekly-recap",
               "options": {"theme": "light", "format": "md"}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5):
        pass
    del_req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets/weekly/team-monday",
        method="DELETE",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(del_req, timeout=5) as r:
        assert r.status == 204
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/presets", timeout=5,
    ) as r:
        body = json.loads(r.read())
    assert body["presets"] == {}


# ---------- #503 S3 §1 — rename is one atomic, server-authoritative op ----
#
# Rename used to be a client-side create-then-delete (`ManagePresetsModal`
# issued savePreset then deletePreset), which rebuilt the record from four
# fields. It therefore dropped `source`, reset `saved_at`, and silently
# overwrote whatever preset already held the target name.


def _presets_post(port, payload, path="/api/share/presets"):
    """POST to a preset endpoint, returning `(status, body)` for 4xx too."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def _presets_get(port):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/presets", timeout=5,
    ) as r:
        return json.loads(r.read())["presets"]


def _seed_preset(port, name, *, panel="weekly", template_id="weekly-recap",
                 source="codex", theme="dark", overwrite=False):
    status, body = _presets_post(port, {
        "panel": panel, "name": name, "template_id": template_id,
        "source": source, "overwrite": overwrite,
        "options": {"theme": theme, "format": "md"},
    })
    assert status == 200, (status, body)
    return body


def test_presets_rename_preserves_source_and_saved_at(dashboard_server):
    """A1. The record MOVES; nothing reconstructs it, so the fields the
    client never sends survive."""
    port, _ = dashboard_server
    saved = _seed_preset(port, "team-monday", source="codex")
    status, body = _presets_post(port, {
        "panel": "weekly", "from_name": "team-monday",
        "to_name": "team-tuesday",
    }, path="/api/share/presets/rename")
    assert status == 200, (status, body)

    presets = _presets_get(port)["weekly"]
    assert "team-monday" not in presets
    moved = presets["team-tuesday"]
    assert moved["source"] == "codex"
    assert moved["saved_at"] == saved["saved_at"]
    assert moved["template_id"] == "weekly-recap"
    assert moved["options"]["theme"] == "dark"


def test_presets_rename_onto_an_existing_name_conflicts(dashboard_server):
    """A2. Decided under the writer lock, so a stale client preflight cannot
    destroy the target."""
    port, _ = dashboard_server
    _seed_preset(port, "team-monday", source="codex", theme="dark")
    _seed_preset(port, "deep-dive", source="claude", theme="light")

    status, body = _presets_post(port, {
        "panel": "weekly", "from_name": "team-monday", "to_name": "deep-dive",
    }, path="/api/share/presets/rename")
    assert status == 409
    assert body["code"] == "preset_name_conflict"
    assert body["field"] == "to_name"

    presets = _presets_get(port)["weekly"]
    assert set(presets) == {"team-monday", "deep-dive"}
    assert presets["deep-dive"]["source"] == "claude"
    assert presets["team-monday"]["source"] == "codex"


def test_presets_rename_with_overwrite_replaces_and_removes_the_source(
    dashboard_server,
):
    port, _ = dashboard_server
    moving = _seed_preset(port, "team-monday", source="codex", theme="dark")
    _seed_preset(port, "deep-dive", source="claude", theme="light")

    status, body = _presets_post(port, {
        "panel": "weekly", "from_name": "team-monday", "to_name": "deep-dive",
        "overwrite": True,
    }, path="/api/share/presets/rename")
    assert status == 200, (status, body)

    presets = _presets_get(port)["weekly"]
    assert set(presets) == {"deep-dive"}
    assert presets["deep-dive"]["source"] == "codex"
    assert presets["deep-dive"]["saved_at"] == moving["saved_at"]


def test_presets_self_rename_is_rejected_and_keeps_the_record(dashboard_server):
    """A2's second half: a move-then-delete on one key deletes the record."""
    port, _ = dashboard_server
    _seed_preset(port, "team-monday", source="codex")
    status, body = _presets_post(port, {
        "panel": "weekly", "from_name": "team-monday",
        "to_name": "team-monday",
    }, path="/api/share/presets/rename")
    assert status == 400
    assert body["field"] == "to_name"

    presets = _presets_get(port)["weekly"]
    assert presets["team-monday"]["source"] == "codex"


def test_presets_rename_of_a_missing_preset_is_404(dashboard_server):
    port, _ = dashboard_server
    status, body = _presets_post(port, {
        "panel": "weekly", "from_name": "nope", "to_name": "still-nope",
    }, path="/api/share/presets/rename")
    assert status == 404
    assert body["error"]


def test_presets_rename_validates_its_names(dashboard_server):
    port, _ = dashboard_server
    _seed_preset(port, "team-monday")
    for payload, field in (
        ({"panel": "not-a-panel", "from_name": "team-monday",
          "to_name": "x"}, "panel"),
        ({"panel": "weekly", "from_name": "team-monday",
          "to_name": "a/b"}, "to_name"),
        ({"panel": "weekly", "from_name": "team-monday",
          "to_name": ""}, "to_name"),
        ({"panel": "weekly", "from_name": "", "to_name": "x"}, "from_name"),
    ):
        status, body = _presets_post(
            port, payload, path="/api/share/presets/rename")
        assert status == 400, (payload, status, body)
        assert body["field"] == field, (payload, body)


def test_presets_rename_csrf_gate(dashboard_server):
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets/rename",
        data=json.dumps({"panel": "weekly", "from_name": "a",
                         "to_name": "b"}).encode(),
        method="POST",
        headers={"Origin": "http://evil.example",
                 "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


def test_presets_save_onto_an_existing_name_conflicts(dashboard_server):
    """The save POST used to overwrite unconditionally and ignore an
    `overwrite` field entirely. Absent means false — the fail-safe
    direction, and what makes this compatible with any older caller."""
    port, _ = dashboard_server
    first = _seed_preset(port, "team-monday", source="codex", theme="dark")

    status, body = _presets_post(port, {
        "panel": "weekly", "name": "team-monday",
        "template_id": "weekly-recap", "source": "claude",
        "options": {"theme": "light", "format": "html"},
    })
    assert status == 409
    assert body["code"] == "preset_name_conflict"
    assert body["field"] == "name"

    presets = _presets_get(port)["weekly"]
    assert presets["team-monday"]["source"] == "codex"
    assert presets["team-monday"]["options"]["theme"] == "dark"
    assert presets["team-monday"]["saved_at"] == first["saved_at"]


def test_presets_save_with_explicit_false_overwrite_also_conflicts(
    dashboard_server,
):
    port, _ = dashboard_server
    _seed_preset(port, "team-monday")
    status, body = _presets_post(port, {
        "panel": "weekly", "name": "team-monday", "overwrite": False,
        "template_id": "weekly-recap",
        "options": {"theme": "light", "format": "html"},
    })
    assert status == 409
    assert body["code"] == "preset_name_conflict"


@pytest.mark.parametrize("path", (
    "/api/share/presets",
    "/api/share/presets/rename",
))
def test_preset_mutations_reject_non_boolean_overwrite(
    dashboard_server, path,
):
    """A truthy JSON string must not bypass either conflict gate."""
    port, _ = dashboard_server
    _seed_preset(port, "team-monday", source="codex", theme="dark")
    _seed_preset(port, "deep-dive", source="claude", theme="light")
    payload = (
        {
            "panel": "weekly", "name": "team-monday",
            "template_id": "weekly-recap", "source": "claude",
            "options": {"theme": "light", "format": "html"},
            "overwrite": "false",
        }
        if path.endswith("presets") else
        {
            "panel": "weekly", "from_name": "team-monday",
            "to_name": "deep-dive", "overwrite": "false",
        }
    )

    status, body = _presets_post(port, payload, path=path)

    assert status == 400, body
    assert body["field"] == "overwrite"
    presets = _presets_get(port)["weekly"]
    assert presets["team-monday"]["source"] == "codex"
    assert presets["deep-dive"]["source"] == "claude"


def test_presets_post_csrf_gate(dashboard_server):
    """POST without matching Origin returns 403."""
    port, _ = dashboard_server
    payload = {"panel": "weekly", "name": "x", "template_id": "weekly-recap",
               "options": {}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Origin": "http://evil.example.com",
                 "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raised = None
    except urllib.error.HTTPError as e:
        raised = e
    assert raised is not None and raised.code == 403


def test_presets_post_rejects_unknown_panel(dashboard_server):
    port, _ = dashboard_server
    payload = {"panel": "alerts", "name": "x", "template_id": "x",
               "options": {}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raised = None
    except urllib.error.HTTPError as e:
        raised = e
    assert raised is not None and raised.code == 400
    assert json.loads(raised.read())["field"] == "panel"


def test_presets_post_rejects_unknown_template_id(dashboard_server):
    """Persisting a preset with a template_id that no longer exists would
    leak a recipe into config.json that fails every later replay through
    /api/share/render. Mirror the render/compose `get_template` lookup."""
    port, _ = dashboard_server
    payload = {"panel": "weekly", "name": "team-monday",
               "template_id": "weekly-bogus", "options": {"format": "md"}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    err = json.loads(exc.value.read())
    assert err["field"] == "template_id"
    assert "weekly-bogus" in err["error"]


def test_presets_post_rejects_wrong_panel_template(dashboard_server):
    """A `weekly-recap` template stored against `panel='daily'` would
    surface a dropdown row that 400s on every replay. Reject at write
    time with the same panel-mismatch envelope as /api/share/render."""
    port, _ = dashboard_server
    payload = {"panel": "daily", "name": "x",
               "template_id": "weekly-recap", "options": {"format": "md"}}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/presets",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    err = json.loads(exc.value.read())
    assert err["field"] == "template_id"
    assert "belongs to panel" in err["error"]


# ---------- M3.2 — POST /api/share/compose ----------


def _compose_request(port: int, sections: list[dict], **overrides):
    payload = {
        "title": "Test compose",
        "theme": "light",
        "format": "html",
        "no_branding": False,
        "reveal_projects": False,
        "sections": sections,
    }
    payload.update(overrides)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/compose",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _section_recipe(panel: str, template_id: str, digest: str = "sha256:fake",
                    digest_version: "int | None" = 2):
    """One compose section recipe.

    `digest_version` defaults to the CURRENT `_SHARE_DATA_DIGEST_VERSION`
    (#503 S3 §4): two digests are compared only when the stored version
    matches, so a recipe that omits it is deliberately not comparable and
    can never report drift. Pass `None` to exercise that legacy case.
    """
    snapshot = {
        "panel": panel, "template_id": template_id,
        "options": {
            "format": "html", "theme": "light",
            "reveal_projects": True, "no_branding": False,
            "top_n": 5, "show_chart": True, "show_table": True,
            "period": None, "project_allowlist": None,
        },
        "data_digest_at_add": digest,
        "kernel_version": 1,
    }
    if digest_version is not None:
        snapshot["data_digest_version_at_add"] = digest_version
    return {"snapshot": snapshot}


def test_compose_single_section_round_trip(dashboard_server):
    port, _ = dashboard_server
    resp = _compose_request(port, [_section_recipe("weekly", "weekly-recap")])
    assert resp["content_type"] == "text/html"
    assert resp["body"].startswith("<!DOCTYPE")
    assert resp["body"].count("<html") == 1
    assert resp["body"].count('<section class="share-section"') == 1


def test_compose_multi_section_in_order(dashboard_server):
    port, _ = dashboard_server
    sections = [
        _section_recipe("weekly", "weekly-recap"),
        _section_recipe("trend", "trend-recap"),
        _section_recipe("forecast", "forecast-recap"),
    ]
    resp = _compose_request(port, sections)
    assert resp["body"].count('<section class="share-section"') == 3
    # Section order preserved by header presence + ordering. Use the
    # composite H1 title to confirm the wrapper exists, then check the
    # three section blocks appear in declared order via the section
    # opening tags themselves.
    body = resp["body"]
    sec_positions = []
    pos = 0
    for _ in range(3):
        idx = body.find('<section class="share-section"', pos)
        assert idx >= 0
        sec_positions.append(idx)
        pos = idx + 1
    assert sec_positions == sorted(sec_positions)


def test_compose_response_carries_section_drift_flags(dashboard_server):
    port, _ = dashboard_server
    # Both sections supply a digest that won't match the freshly-computed
    # one (we passed "sha256:fake") → both should be drift_detected.
    sections = [_section_recipe("weekly", "weekly-recap"),
                _section_recipe("daily",  "daily-recap")]
    resp = _compose_request(port, sections)
    assert "section_results" in resp["snapshot"]
    results = resp["snapshot"]["section_results"]
    assert len(results) == 2
    for r in results:
        assert r["drift_detected"] is True
        assert r["data_digest_at_add"] == "sha256:fake"
        assert r["data_digest_now"].startswith("sha256:")


def test_compose_ignores_client_supplied_body(dashboard_server):
    """Server must re-render from recipe; client `body` must be silently ignored."""
    port, _ = dashboard_server
    malicious = "<svg>real-name-leak-here</svg>"
    section = _section_recipe("weekly", "weekly-recap")
    # Plant a body field on the section — server MUST NOT echo it.
    section["body"] = malicious
    section["content_type"] = "image/svg+xml"
    resp = _compose_request(port, [section], format="svg")
    assert "real-name-leak-here" not in resp["body"], (
        "server echoed client-supplied body — privacy chokepoint broken"
    )


def test_compose_rejects_invalid_template_in_section(dashboard_server):
    port, _ = dashboard_server
    section = _section_recipe("weekly", "definitely-not-a-real-template")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/compose",
        data=json.dumps({"title": "X", "theme": "light", "format": "html",
                         "no_branding": False, "reveal_projects": False,
                         "sections": [section]}).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raised = None
    except urllib.error.HTTPError as e:
        raised = e
    assert raised is not None and raised.code == 400


def test_compose_csrf_gate(dashboard_server):
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/compose",
        data=json.dumps({"title": "X", "theme": "light", "format": "html",
                         "no_branding": False, "reveal_projects": False,
                         "sections": [_section_recipe("weekly", "weekly-recap")]}).encode(),
        method="POST",
        headers={"Origin": "http://evil.example.com",
                 "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raised = None
    except urllib.error.HTTPError as e:
        raised = e
    assert raised is not None and raised.code == 403


# ---------- M4.3 — /api/share/history ring buffer ----------


def test_history_initial_get_returns_empty(dashboard_server):
    """First call on a fresh config — no `share.history` key — must shape
    the response as `{"history": []}` so the frontend's parser doesn't
    have to special-case missing keys."""
    port, _ = dashboard_server
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/history", timeout=5,
    ) as r:
        body = json.loads(r.read())
    assert body == {"history": []}


def test_history_post_appends_and_trims_to_20(dashboard_server):
    """Spec §11.4 — server-side ring buffer caps at 20, FIFO trim so the
    newest entry is always the last element."""
    port, _ = dashboard_server
    for _ in range(25):
        payload = {
            "panel": "weekly",
            "template_id": "weekly-recap",
            "options": {"format": "md"},
            "format": "md",
            "destination": "download",
        }
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/history",
            data=json.dumps(payload).encode(),
            method="POST",
            headers=_csrf_headers(port),
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 200
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/history", timeout=5,
    ) as r:
        body = json.loads(r.read())
    assert len(body["history"]) == 20
    # FIFO trim: oldest dropped, newest at the end. `exported_at` is a
    # monotonic UTC timestamp so the last entry must be >= the first.
    assert body["history"][-1]["exported_at"] >= body["history"][0]["exported_at"]


def test_history_post_returns_recipe_with_server_fields(dashboard_server):
    """POST response carries the persisted record so the client can show
    it in the dropdown without an extra GET round-trip."""
    port, _ = dashboard_server
    payload = {
        "panel": "weekly",
        "template_id": "weekly-recap",
        "options": {"format": "md", "theme": "light"},
        "format": "md",
        "destination": "download",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    assert body["panel"] == "weekly"
    assert body["template_id"] == "weekly-recap"
    assert body["format"] == "md"
    assert body["destination"] == "download"
    assert isinstance(body.get("recipe_id"), str) and body["recipe_id"]
    assert isinstance(body.get("exported_at"), str) and body["exported_at"]


def test_history_post_preserves_valid_account_and_omits_agnostic(dashboard_server):
    """Account is additive metadata; legacy history bytes keep no null field."""
    port, _ = dashboard_server
    account = "b" * 32
    base = {
        "panel": "weekly",
        "template_id": "weekly-recap",
        "options": {"format": "md"},
        "format": "md",
        "destination": "download",
        "source": "codex",
    }
    records = []
    for payload in ({**base, "account": account}, base):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/share/history",
            data=json.dumps(payload).encode(), method="POST",
            headers=_csrf_headers(port),
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            records.append(json.loads(r.read()))
    assert records[0]["account"] == account
    assert "account" not in records[1]


@pytest.mark.parametrize("row_kind", ("panel", "composed"))
def test_history_post_rejects_unknown_panel(dashboard_server, row_kind):
    """Panel validation mirrors presets POST — refuses non-share-capable
    panels with HTTP 400.

    Parameterized over both history branches (#503 S3 §3): a composed row's
    sections are held to exactly the invariants a panel row is, because a
    section that would 400 on replay poisons the dropdown the same way.
    """
    port, _ = dashboard_server
    recipe = {
        "panel": "alerts",
        "template_id": "weekly-recap",
        "options": {"format": "md"},
    }
    payload = (
        {**recipe, "format": "md", "destination": "download"}
        if row_kind == "panel" else _composed_row(sections=[recipe])
    )
    status, body = _history_post(port, payload)
    assert status == 400, body
    assert body["field"] == (
        "panel" if row_kind == "panel" else "sections[0].panel")


@pytest.mark.parametrize("row_kind", ("panel", "composed"))
def test_history_post_rejects_unknown_template_id(dashboard_server, row_kind):
    """History records are persisted recipes, so an unknown `template_id`
    poisons the recent-shares dropdown with a row that 400s on replay.
    Mirror the render/compose `get_template` lookup — in both branches."""
    port, _ = dashboard_server
    recipe = {
        "panel": "weekly",
        "template_id": "weekly-bogus",
        "options": {"format": "md"},
    }
    payload = (
        {**recipe, "format": "md", "destination": "download"}
        if row_kind == "panel" else _composed_row(sections=[recipe])
    )
    status, err = _history_post(port, payload)
    assert status == 400, err
    assert err["field"] == (
        "template_id" if row_kind == "panel" else "sections[0].template_id")
    assert "weekly-bogus" in err["error"]


@pytest.mark.parametrize("row_kind", ("panel", "composed"))
def test_history_post_rejects_wrong_panel_template(dashboard_server, row_kind):
    """`weekly-recap` recorded against `panel='daily'` would land in the
    ring buffer and surface a recent-shares row that 400s on every replay.
    Reject at write time with the same envelope as /api/share/render — in
    both branches."""
    port, _ = dashboard_server
    recipe = {
        "panel": "daily",
        "template_id": "weekly-recap",
        "options": {"format": "md"},
    }
    payload = (
        {**recipe, "format": "md", "destination": "download"}
        if row_kind == "panel" else _composed_row(sections=[recipe])
    )
    status, err = _history_post(port, payload)
    assert status == 400, err
    assert err["field"] == (
        "template_id" if row_kind == "panel" else "sections[0].template_id")
    assert "belongs to panel" in err["error"]


# ---------- #503 S3 §3 — history is a discriminated union on `kind` -------
#
# A composed export used to record nothing at all: the history record was
# structurally single-panel (the server rejects a payload whose template_id
# does not belong to its panel), so the five composer export handlers had
# nowhere to write. A composed row carries `panel: null`, which an older
# client's `h.panel === panel` filter evaluates false, so it hides rather
# than mis-renders it. No migration: a missing `kind` reads as "panel".


def _history_post(port, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def _history_get(port):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/history", timeout=5,
    ) as r:
        return json.loads(r.read())["history"]


def _composed_row(sections=None, **overrides):
    payload = {
        "kind": "composed",
        "sections": sections if sections is not None else [
            {"panel": "weekly", "template_id": "weekly-recap",
             "options": {"top_n": 5}, "source": "claude"},
            {"panel": "daily", "template_id": "daily-visual",
             "options": {}, "source": "codex"},
        ],
        "composite": {
            "title": "Monday roundup", "theme": "dark",
            "reveal_projects": False, "no_branding": True,
        },
        "format": "md",
        "destination": "download",
    }
    payload.update(overrides)
    return payload


def test_history_composed_row_round_trips(dashboard_server):
    """A3's server half."""
    port, _ = dashboard_server
    status, record = _history_post(port, _composed_row())
    assert status == 200, record
    assert record["kind"] == "composed"
    assert record["panel"] is None
    assert record["recipe_id"]
    assert record["exported_at"]
    assert [s["panel"] for s in record["sections"]] == ["weekly", "daily"]
    assert record["sections"][1]["source"] == "codex"
    assert record["composite"]["title"] == "Monday roundup"
    assert "template_id" not in record
    assert "source" not in record

    rows = _history_get(port)
    assert len(rows) == 1
    assert rows[0] == record


def test_history_legacy_row_reads_as_panel_without_being_written_back(
    dashboard_server, tmp_path,
):
    """A4. A `config.json` written before `kind` existed must read and
    recall correctly, and a GET must stay read-only."""
    port, _ = dashboard_server
    status, _ = _history_post(port, {
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {"format": "md"}, "format": "md",
        "destination": "download",
    })
    assert status == 200

    config_path = tmp_path / ".local" / "share" / "cctally" / "config.json"
    stored = json.loads(config_path.read_text())
    # Simulate the pre-#503-S3 bytes: strip the discriminator entirely.
    stored["share"]["history"][0].pop("kind", None)
    config_path.write_text(json.dumps(stored))
    before = config_path.read_text()

    rows = _history_get(port)
    assert rows[0]["kind"] == "panel"
    assert rows[0]["panel"] == "weekly"
    assert rows[0]["source"] == "claude"
    assert config_path.read_text() == before, "GET must not write config back"


def test_history_rejects_an_unknown_kind(dashboard_server):
    port, _ = dashboard_server
    status, body = _history_post(port, {
        "kind": "composite", "panel": "weekly",
        "template_id": "weekly-recap", "options": {},
    })
    assert status == 400
    assert body["field"] == "kind"


@pytest.mark.parametrize("count", (0, 21))
def test_history_composed_row_bounds_its_section_list(dashboard_server, count):
    """Twenty is the basket cap, so the row is bounded by construction."""
    port, _ = dashboard_server
    section = {"panel": "weekly", "template_id": "weekly-recap",
               "options": {}, "source": "claude"}
    status, body = _history_post(
        port, _composed_row(sections=[dict(section)] * count))
    assert status == 400, body
    assert body["field"] == "sections"


@pytest.mark.parametrize(("section", "field"), (
    ({"panel": "weekly", "template_id": "weekly-recap", "options": []},
     "sections[0].options"),
    ({"panel": "weekly", "template_id": "weekly-recap", "options": {},
      "account": "not-a-key"}, "sections[0].account"),
    ({"panel": "weekly", "template_id": "weekly-recap", "options": {},
      "source": "gemini"}, "sections[0].source"),
    ("not-an-object", "sections[0]"),
))
def test_history_composed_row_validates_every_section(
    dashboard_server, section, field,
):
    """The panel-membership and template-ownership halves live on the three
    parameterized pinning tests above; these are the remaining per-section
    invariants."""
    port, _ = dashboard_server
    status, body = _history_post(port, _composed_row(sections=[section]))
    assert status == 400, body
    assert body["field"] == field, body


@pytest.mark.parametrize(("composite", "field"), (
    ({"theme": "dark"}, "composite.title"),
    ({"title": "T", "theme": "neon"}, "composite.theme"),
    ({"title": "T", "theme": "dark", "reveal_projects": "yes"},
     "composite.reveal_projects"),
    ({"title": "T", "theme": "dark", "no_branding": 1},
     "composite.no_branding"),
))
def test_history_composed_row_validates_its_composite_knobs(
    dashboard_server, composite, field,
):
    port, _ = dashboard_server
    status, body = _history_post(port, _composed_row(composite=composite))
    assert status == 400, body
    assert body["field"] == field, body


@pytest.mark.parametrize(("payload", "field"), (
    ({"kind": "composed", "panel": "weekly"}, "panel"),
    ({"kind": "composed", "template_id": "weekly-recap"}, "template_id"),
    ({"kind": "composed", "source": "codex"}, "source"),
    ({"kind": "composed", "options": {"top_n": 3}}, "options"),
    ({"kind": "panel", "panel": "weekly", "template_id": "weekly-recap",
      "options": {}, "sections": []}, "sections"),
    ({"kind": "panel", "panel": "weekly", "template_id": "weekly-recap",
      "options": {}, "composite": {"title": "T"}}, "composite"),
))
def test_history_rejects_a_row_that_mixes_the_two_branches(
    dashboard_server, payload, field,
):
    port, _ = dashboard_server
    body_payload = dict(payload)
    if body_payload.get("kind") == "composed":
        base = _composed_row()
        base.update(body_payload)
        body_payload = base
    status, body = _history_post(port, body_payload)
    assert status == 400, body
    assert body["field"] == field, body


def test_history_composed_and_panel_rows_share_one_ring(dashboard_server):
    port, _ = dashboard_server
    assert _history_post(port, _composed_row())[0] == 200
    assert _history_post(port, {
        "panel": "weekly", "template_id": "weekly-recap",
        "options": {}, "format": "md", "destination": "clipboard",
    })[0] == 200
    rows = _history_get(port)
    assert [row["kind"] for row in rows] == ["composed", "panel"]


def test_history_post_csrf_gate(dashboard_server):
    """Cross-origin POST must be refused with 403, matching the other
    write surfaces (presets, settings, alerts/test)."""
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        data=json.dumps({
            "panel": "weekly",
            "template_id": "weekly-recap",
            "options": {"format": "md"},
            "format": "md",
            "destination": "download",
        }).encode(),
        method="POST",
        headers={"Origin": "http://evil.example.com",
                 "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


def test_history_delete_clears_buffer(dashboard_server):
    """DELETE empties the ring buffer entirely; subsequent GET returns
    `{"history": []}` like the first-run case."""
    port, _ = dashboard_server
    payload = {
        "panel": "weekly",
        "template_id": "weekly-recap",
        "options": {"format": "md"},
        "format": "md",
        "destination": "download",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5):
        pass
    del_req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        method="DELETE",
        headers={"Origin": f"http://127.0.0.1:{port}",
                 "Host": f"127.0.0.1:{port}"},
    )
    with urllib.request.urlopen(del_req, timeout=5) as r:
        assert r.status == 204
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/share/history", timeout=5,
    ) as r:
        body = json.loads(r.read())
    assert body == {"history": []}


def test_history_delete_csrf_gate(dashboard_server):
    """Cross-origin DELETE must be refused with 403."""
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        method="DELETE",
        headers={"Origin": "http://evil.example.com"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 403


# ---------- #279 S1 F3 — share POST body cap ----------


_SHARE_POST_CAP = 64 * 1024


@pytest.mark.parametrize("endpoint", ["render", "compose", "presets", "history"])
def test_share_post_body_cap(dashboard_server, endpoint):
    """Oversized (> 64 KiB) POST bodies to the share endpoints are rejected
    with 400 before the body is read/parsed. Under-cap happy paths stay
    covered by the render/compose/presets/history success tests above.

    Slow-loris / memory-exhaustion hardening: every other POST (settings,
    alerts test) already caps at 4 KB; the share composer legitimately sends
    larger multi-panel payloads, so these cap at a generous 64 KiB.
    """
    port, _ = dashboard_server
    big = b"{" + b" " * (_SHARE_POST_CAP + 1) + b"}"   # Content-Length > cap
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/{endpoint}",
        data=big, method="POST", headers=_csrf_headers(port),
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
    body = json.loads(exc.value.read())
    assert "too large" in body["error"].lower()


# ---------- #503 S1 F3 — an omitted `reveal_projects` must fail closed ------
#
# The point of these tests is that they OMIT the field. No test in this file
# did, which is exactly why `/api/share/render` shipped defaulting it OPEN
# while `/api/share/compose` defaulted it closed — the two endpoints
# disagreed and nothing noticed.


def _render_request(port: int, *, panel: str, template_id: str, options: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=json.dumps({
            "panel": panel, "template_id": template_id, "options": options,
        }).encode(),
        method="POST", headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_render_anonymizes_when_reveal_projects_is_absent(dashboard_server):
    port, _ = dashboard_server
    body = _render_request(
        port, panel="weekly", template_id="weekly-recap",
        options={"format": "md", "theme": "light", "no_branding": False,
                 "top_n": 5, "period": {"kind": "current"},
                 "project_allowlist": None},
    )["body"]
    assert "anonymized: true" in body


@pytest.mark.parametrize("path,payload,field", (
    (
        "/api/share/render",
        {
            "panel": "weekly", "template_id": "weekly-recap",
            "options": {
                "format": "md", "theme": "light",
                "reveal_projects": "false",
            },
        },
        "options.reveal_projects",
    ),
    (
        "/api/share/compose",
        {
            "title": "Composite", "theme": "light", "format": "md",
            "reveal_projects": "false",
            "sections": [_section_recipe("weekly", "weekly-recap")],
        },
        "reveal_projects",
    ),
))
def test_share_endpoints_reject_non_boolean_reveal_projects(
    dashboard_server, path, payload, field,
):
    """The string ``false`` must never become a truthy privacy opt-out."""
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(), method="POST",
        headers={**_csrf_headers(port), "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400
    body = json.loads(exc.value.read())
    assert body["field"] == field


def test_render_projects_panel_claims_anonymized_when_the_field_is_absent(
        dashboard_server):
    """#503 S2 D5: the claim moved from the Projects subtitle to the facts
    strip, which states it for EVERY panel rather than for this one, and
    reads the provenance marker rather than a request field. An absent
    `reveal_projects` must still produce the anonymized claim."""
    body = _render_request(
        dashboard_server[0], panel="projects", template_id="projects-recap",
        options={"format": "md", "theme": "light", "no_branding": False,
                 "top_n": 5, "period": {"kind": "current"},
                 "project_allowlist": None},
    )["body"]
    assert "anonymized: true" in body
    assert "real project names" not in body
    assert "real projects" not in body


@pytest.fixture
def dashboard_server_with_projects(tmp_path, monkeypatch):
    """A server whose snapshot carries REAL project rows.

    The default fixture boots on `_empty_dashboard_snapshot()`, which has no
    projects at all — so a mode assertion is the only thing an anonymization
    test can make against it, and F3 is about a silent REVEAL. This fixture
    supplies labels that would be visible if the endpoint's default resolved
    open.
    """
    ns = load_script()
    srv = _start_share_dashboard_server(ns, tmp_path, monkeypatch)
    snap = ns["_empty_dashboard_snapshot"]()
    snap.projects_envelope = {
        "current_week": {
            "rows": [
                {"key": "cctally-dev", "bucket_path": "/repos/cctally-dev",
                 "cost_usd": 12.50, "attributed_pct": 4.2, "sessions_count": 7},
                {"key": "acme-billing", "bucket_path": "/repos/acme-billing",
                 "cost_usd": 3.25, "attributed_pct": 1.1, "sessions_count": 2},
            ],
            "total_cost_usd": 15.75,
        },
        "trend": {},
    }
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)
    try:
        yield srv.server_address[1], None
    finally:
        srv.shutdown()


def test_render_replaces_a_real_project_label_when_reveal_is_absent(
        dashboard_server_with_projects):
    """An omitted `reveal_projects` must ANONYMIZE, not merely claim to.

    The mode assertion above proves the default resolves closed. This proves
    the closed default actually reached the labels: a real project name is
    replaced by its alias on the render endpoint with the field omitted,
    which is the disclosure F3 describes.
    """
    port, _ = dashboard_server_with_projects
    body = _render_request(
        port, panel="projects", template_id="projects-detail",
        options={"format": "md", "theme": "light", "no_branding": False,
                 "top_n": 50, "period": {"kind": "current"},
                 "project_allowlist": None},
    )["body"]
    assert "project-1" in body
    assert "project-2" in body
    assert "cctally-dev" not in body
    assert "acme-billing" not in body


def test_render_reveals_a_real_project_label_when_asked(
        dashboard_server_with_projects):
    """Non-vacuity for the test above: the labels ARE reachable."""
    port, _ = dashboard_server_with_projects
    body = _render_request(
        port, panel="projects", template_id="projects-detail",
        options={"format": "md", "theme": "light", "no_branding": False,
                 "top_n": 50, "period": {"kind": "current"},
                 "reveal_projects": True,
                 "project_allowlist": None},
    )["body"]
    assert "acme-billing" in body


# --- #503 S1 B1: the render response reports whether the export carries
# project names at all, so the modal's status line can stop over-warning ----

def test_render_reports_no_project_names_for_a_metric_only_template(
        dashboard_server_with_projects):
    """`forecast` renders no project name in either privacy mode, so the
    modal must not claim the export will show real ones. The server derives
    the flag from the same snapshot it just rendered."""
    payload = _render_request(
        dashboard_server_with_projects[0],
        panel="forecast", template_id="forecast-recap",
        options={"format": "md", "theme": "light", "no_branding": False,
                 "top_n": 5, "period": {"kind": "current"},
                 "project_allowlist": None},
    )
    assert payload["has_project_names"] is False


def test_render_reports_project_names_for_a_project_bearing_template(
        dashboard_server_with_projects):
    """Non-vacuity for the assertion above: the flag is not hardcoded False."""
    payload = _render_request(
        dashboard_server_with_projects[0],
        panel="projects", template_id="projects-detail",
        options={"format": "md", "theme": "light", "no_branding": False,
                 "top_n": 50, "period": {"kind": "current"},
                 "project_allowlist": None},
    )
    assert payload["has_project_names"] is True


def test_the_flag_does_not_depend_on_the_privacy_mode(
        dashboard_server_with_projects):
    """It answers "does this export contain project names at all", which is a
    property of the DATA. Making it mode-dependent would flip the neutral
    state on toggle, which is exactly the false statement being removed."""
    port, _ = dashboard_server_with_projects
    base = {"format": "md", "theme": "light", "no_branding": False,
            "top_n": 50, "period": {"kind": "current"},
            "project_allowlist": None}
    anon = _render_request(port, panel="projects",
                           template_id="projects-detail", options=dict(base))
    reveal = _render_request(port, panel="projects",
                             template_id="projects-detail",
                             options={**base, "reveal_projects": True})
    assert anon["has_project_names"] == reveal["has_project_names"] is True


def test_compose_anonymizes_when_reveal_projects_is_absent(dashboard_server):
    port, _ = dashboard_server
    section = _section_recipe("weekly", "weekly-recap")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/compose",
        data=json.dumps({
            "title": "Composite", "theme": "light", "format": "md",
            "no_branding": False, "sections": [section],
        }).encode(),
        method="POST", headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())["body"]
    assert "anonymized: true" in body


def test_projects_subtitle_states_only_its_count():
    import importlib.util as _ilu
    import sys as _sys
    if "_lib_share_templates" in _sys.modules:
        _T = _sys.modules["_lib_share_templates"]
    else:
        _tpl = pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib_share_templates.py"
        _spec = _ilu.spec_from_file_location("_lib_share_templates", _tpl)
        _T = _ilu.module_from_spec(_spec)
        _sys.modules["_lib_share_templates"] = _T
        _spec.loader.exec_module(_T)
    # #503 S2 D5 — the subtitle keeps ONLY its count. The privacy claim
    # is now the facts strip's, which reads the provenance marker rather
    # than this options dict, so an omitted field cannot make it wrong.
    assert _T._projects_subtitle(3) == "3 projects"
    assert _T._projects_subtitle(3) == "3 projects"  # no privacy clause
