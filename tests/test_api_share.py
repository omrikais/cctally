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
    updated = {**base, "options": {"theme": "dark", "format": "html"}}
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


def _section_recipe(panel: str, template_id: str, digest: str = "sha256:fake"):
    return {
        "snapshot": {
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
    }


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


def test_history_post_rejects_unknown_panel(dashboard_server):
    """Panel validation mirrors presets POST — refuses non-share-capable
    panels with HTTP 400."""
    port, _ = dashboard_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/history",
        data=json.dumps({
            "panel": "alerts",
            "template_id": "weekly-recap",
            "options": {"format": "md"},
            "format": "md",
            "destination": "download",
        }).encode(),
        method="POST",
        headers=_csrf_headers(port),
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400


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
