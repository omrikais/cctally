"""Section 7.5 contract: dashboard share path strips project labels via
`_lib_share._scrub` before render. Fail-closed (memory: *Anonymization
fails closed*): missing keys map to a sentinel, NEVER passthrough.

Boots the real DashboardHTTPHandler against a tmp HOME but seeds an
in-memory snapshot whose `projects_envelope` field carries real
display_key / bucket_path tokens. Asserts that across md / html / svg,
`reveal_projects=false` produces zero occurrences of any original
token; `reveal_projects=true` keeps them.
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request

import pytest

from conftest import load_script, redirect_paths


# Real keys + paths to seed into the snapshot. These are the tokens the
# anonymization canary asserts MUST NOT appear in the rendered output
# under `reveal_projects=false`. Spec §7.4 / §7.5.
_REAL_KEY_TOKENS = ("cctally-dev", "house-of-mass", "foo (repos)",
                    "foo (forks)")
_REAL_BUCKET_TOKENS = ("/repos/cctally-dev", "/repos/house-of-mass",
                       "/repos/foo", "/forks/foo")


def _seeded_projects_envelope() -> dict:
    """A minimal `projects_envelope` block carrying mixed real tokens —
    plain basename projects + a disambiguation-collision pair. The
    builder downstream (`_build_projects_share_panel_data`) reads
    `current_week.rows` for windowWeeks=1 (the panel share flow);
    trend.projects is also populated for completeness."""
    return {
        "current_week": {
            "week_label":      "wk May 18",
            "week_start_date": "2026-05-18",
            "week_start_at":   "2026-05-18T00:00:00Z",
            "total_cost_usd":  41.0,
            "rows": [
                {"key": "cctally-dev",   "bucket_path": "/repos/cctally-dev",
                 "cost_usd": 18.0, "attributed_pct": 9.0,
                 "sessions_count": 5},
                {"key": "house-of-mass", "bucket_path": "/repos/house-of-mass",
                 "cost_usd": 12.0, "attributed_pct": 6.0,
                 "sessions_count": 4},
                {"key": "foo (repos)",   "bucket_path": "/repos/foo",
                 "cost_usd":  7.0, "attributed_pct": 3.5,
                 "sessions_count": 2},
                {"key": "foo (forks)",   "bucket_path": "/forks/foo",
                 "cost_usd":  4.0, "attributed_pct": 2.0,
                 "sessions_count": 1},
            ],
        },
        "trend": {
            "window_weeks": 1,
            "weeks": [
                {"week_start_date": "2026-05-18",
                 "week_label":      "wk May 18",
                 "total_cost_usd":  41.0,
                 "total_pct":       20.5},
            ],
            "projects": [
                {"key": "cctally-dev",   "bucket_path": "/repos/cctally-dev",
                 "weekly_cost": [18.0], "weekly_pct": [9.0],
                 "first_seen_at": "2026-05-18T12:00:00Z",
                 "last_seen_at":  "2026-05-18T18:00:00Z",
                 "sessions_count_12w": 5},
                {"key": "house-of-mass", "bucket_path": "/repos/house-of-mass",
                 "weekly_cost": [12.0], "weekly_pct": [6.0],
                 "first_seen_at": "2026-05-18T12:00:00Z",
                 "last_seen_at":  "2026-05-18T18:00:00Z",
                 "sessions_count_12w": 4},
                {"key": "foo (repos)",   "bucket_path": "/repos/foo",
                 "weekly_cost":  [7.0], "weekly_pct": [3.5],
                 "first_seen_at": "2026-05-18T12:00:00Z",
                 "last_seen_at":  "2026-05-18T18:00:00Z",
                 "sessions_count_12w": 2},
                {"key": "foo (forks)",   "bucket_path": "/forks/foo",
                 "weekly_cost":  [4.0], "weekly_pct": [2.0],
                 "first_seen_at": "2026-05-18T12:00:00Z",
                 "last_seen_at":  "2026-05-18T18:00:00Z",
                 "sessions_count_12w": 1},
            ],
        },
    }


def _start_share_server_with_projects(ns, tmp_path, monkeypatch):
    """Boot a dashboard server whose snapshot has projects_envelope set."""
    redirect_paths(ns, monkeypatch, tmp_path)
    sys.path.insert(0, str(pathlib.Path(ns["__file__"]).resolve().parent))

    import socketserver
    HandlerCls = ns["DashboardHTTPHandler"]
    SnapshotRef = ns["_SnapshotRef"]
    SSEHub = ns["SSEHub"]

    snap = ns["_empty_dashboard_snapshot"]()
    # Mutate via dataclasses.replace so the dataclass stays frozen-like.
    import dataclasses
    snap = dataclasses.replace(snap, projects_envelope=_seeded_projects_envelope())

    HandlerCls.snapshot_ref = SnapshotRef(snap)
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
def projects_share_server(tmp_path, monkeypatch):
    ns = load_script()
    srv = _start_share_server_with_projects(ns, tmp_path, monkeypatch)
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def _csrf_headers(port: int) -> dict[str, str]:
    return {
        "Host": f"127.0.0.1:{port}",
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
    }


def _render(port: int, fmt: str, reveal: bool, template_id: str) -> bytes:
    req_body = json.dumps({
        "panel": "projects",
        "template_id": template_id,
        "options": {
            "format": fmt,
            "theme": "light",
            "reveal_projects": reveal,
            "no_branding": False,
            "top_n": 50,
            "period": {"kind": "current"},
            "project_allowlist": None,
            "show_chart": True,
            "show_table": True,
        },
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/share/render",
        data=req_body, method="POST",
        headers=_csrf_headers(port),
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        body = json.loads(r.read())
    rendered = body["body"]
    if isinstance(rendered, str):
        return rendered.encode("utf-8")
    return bytes(rendered)


@pytest.mark.parametrize("fmt", ["md", "html", "svg"])
@pytest.mark.parametrize("template_id",
                         ["projects-recap", "projects-visual", "projects-detail"])
def test_share_projects_no_original_tokens_when_anonymized(
    projects_share_server, fmt, template_id,
):
    """`reveal_projects=false` MUST strip both `display_key` AND
    `bucket_path` tokens across every archetype × every format.

    Fail-closed (memory: *Anonymization fails closed*): if any real
    token shows up in the rendered output, the scrubber has regressed —
    do NOT relax the assertion.
    """
    body = _render(projects_share_server, fmt, reveal=False,
                   template_id=template_id)
    for tok in _REAL_KEY_TOKENS + _REAL_BUCKET_TOKENS:
        assert tok.encode() not in body, (
            f"original token {tok!r} leaked into "
            f"{template_id} / {fmt} share artifact (reveal=false)"
        )


@pytest.mark.parametrize("fmt", ["md", "html", "svg"])
def test_share_projects_real_tokens_present_when_revealed(
    projects_share_server, fmt,
):
    """Sanity check: with `reveal_projects=true`, at least one real
    display_key should appear in the rendered output (otherwise the
    anonymized assertion above would tautologically pass)."""
    body = _render(projects_share_server, fmt, reveal=True,
                   template_id="projects-detail")
    real_present = any(
        tok.encode() in body for tok in _REAL_KEY_TOKENS
    )
    assert real_present, (
        f"no real display_key tokens in {fmt} share artifact "
        f"under reveal=true — sanity check failed"
    )
