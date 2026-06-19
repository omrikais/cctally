"""Test for the /favicon.ico dashboard route (#207 D11).

Boots the real DashboardHTTPHandler against a tmp-dir-redirected install
(mirrors tests/test_api_share.py) and asserts GET /favicon.ico serves the
SVG favicon (200, image/svg+xml). `static_dir` defaults to the built
`dashboard/static`, into which Vite copies `public/favicon.svg` on build.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import urllib.request

import pytest

from conftest import load_script, redirect_paths


def _start_dashboard_server(ns, tmp_path, monkeypatch):
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
    ns = load_script()
    srv = _start_dashboard_server(ns, tmp_path, monkeypatch)
    try:
        yield "127.0.0.1", srv.server_address[1]
    finally:
        srv.shutdown()


def test_favicon_ico_served(dashboard_server):
    host, port = dashboard_server
    req = urllib.request.Request(
        f"http://{host}:{port}/favicon.ico",
        headers={"Host": f"{host}:{port}"},
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 200
        assert r.headers["Content-Type"] == "image/svg+xml"
        assert r.read(5).startswith(b"<")  # SVG document opener
