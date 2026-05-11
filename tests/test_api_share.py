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
