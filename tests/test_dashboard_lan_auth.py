"""Per-run LAN authentication tests for the dashboard API (issue #282)."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    loaded = load_script()
    redirect_paths(loaded, monkeypatch, tmp_path)
    return loaded


def _serve(ns, token):
    handler = ns["DashboardHTTPHandler"]
    handler.hub = ns["SSEHub"]()
    snapshot = ns["_empty_dashboard_snapshot"]()
    handler.snapshot_ref = ns["_SnapshotRef"](snapshot)
    handler.static_dir = ns["STATIC_DIR"]
    handler.sync_lock = threading.Lock()
    handler.run_sync_now = staticmethod(lambda: None)
    handler.run_sync_now_locked = staticmethod(lambda: None)
    handler.no_sync = False
    handler.display_tz_pref_override = None
    handler.cctally_api_token = token
    server = ns["ThreadingHTTPServer"](("127.0.0.1", 0), handler)
    server.handle_error = lambda request, client_address: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(server, method, path, *, headers=(), body=b"", stream=False):
    conn = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=3
    )
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    supplied = list(headers.items()) if isinstance(headers, dict) else list(headers)
    if not any(name.lower() == "host" for name, _ in supplied):
        supplied.append(("Host", f"127.0.0.1:{server.server_address[1]}"))
    if body and not any(name.lower() == "content-length" for name, _ in supplied):
        supplied.append(("Content-Length", str(len(body))))
    for name, value in supplied:
        conn.putheader(name, value)
    conn.endheaders(body)
    response = conn.getresponse()
    if stream:
        return conn, response, None
    payload = response.read()
    conn.close()
    return None, response, payload


def test_token_minting_uses_robust_loopback_classification(ns):
    calls = []

    def factory(size):
        calls.append(size)
        return "run-token"

    for host in ("localhost", "127.0.0.1", "127.7.8.9", "::1", "[::1]"):
        assert ns["_dashboard_lan_auth_token"](
            host, True, token_factory=factory
        ) is None
    assert ns["_dashboard_lan_auth_token"](
        "0.0.0.0", True, token_factory=factory
    ) == "run-token"
    assert ns["_dashboard_lan_auth_token"](
        "192.0.2.10", False, token_factory=factory
    ) is None
    assert calls == [32]


def test_auth_url_uses_fragment_and_preserves_plain_url_without_token(ns):
    url = "http://192.0.2.10:8789/"
    assert ns["_dashboard_auth_url"](url, None) == url
    assert ns["_dashboard_auth_url"](url, "a/b+c=") == (
        "http://192.0.2.10:8789/#token=a%2Fb%2Bc%3D"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/data"),
        ("GET", "/api/not-a-route"),
        ("POST", "/api/not-a-route"),
        ("PUT", "/api/settings"),
        ("DELETE", "/api/not-a-route"),
        ("PATCH", "/api/settings"),
        ("HEAD", "/api/data"),
        ("OPTIONS", "/api/data"),
    ],
)
def test_token_enabled_api_rejects_before_route_dispatch(ns, method, path):
    server, thread = _serve(ns, "run-token")
    try:
        _, response, payload = _request(server, method, path)
        assert response.status == 401
        assert response.getheader("WWW-Authenticate") == "Bearer"
        if method != "HEAD":
            assert json.loads(payload) == {"error": "unauthorized"}
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_exact_bearer_bootstraps_cookie_then_cookie_reaches_data_and_sse(ns):
    server, thread = _serve(ns, "run-token")
    stream_conn = None
    try:
        _, auth, payload = _request(
            server,
            "POST",
            "/api/auth",
            headers={"Authorization": "Bearer run-token"},
        )
        assert auth.status == 204
        assert payload == b""
        set_cookie = auth.getheader("Set-Cookie")
        assert set_cookie == (
            "cctally_dashboard_token=run-token; Path=/api; "
            "HttpOnly; SameSite=Strict"
        )
        cookie = set_cookie.split(";", 1)[0]

        _, data, _ = _request(
            server, "GET", "/api/data", headers={"Cookie": cookie}
        )
        assert data.status == 200

        stream_conn, events, _ = _request(
            server, "GET", "/api/events", headers={"Cookie": cookie}, stream=True
        )
        assert events.status == 200
        assert events.getheader("Content-Type").startswith("text/event-stream")
    finally:
        if stream_conn is not None:
            stream_conn.close()
        server.shutdown()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "run-token"},
        {"Authorization": "bearer run-token"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer é"},
        [("Authorization", "Bearer run-token"),
         ("Authorization", "Bearer run-token")],
        {"Cookie": "cctally_dashboard_token=wrong"},
        {"Cookie": "cctally_dashboard_token=é"},
        {"Authorization": "Bearer wrong",
         "Cookie": "cctally_dashboard_token=run-token"},
    ],
)
def test_wrong_malformed_or_explicitly_bad_credentials_reject(ns, headers):
    server, thread = _serve(ns, "run-token")
    try:
        _, response, payload = _request(
            server, "GET", "/api/data", headers=headers
        )
        assert response.status == 401
        assert response.getheader("WWW-Authenticate") == "Bearer"
        assert json.loads(payload) == {"error": "unauthorized"}
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_authenticated_sync_still_applies_origin_csrf(ns):
    server, thread = _serve(ns, "run-token")
    try:
        _, response, _ = _request(
            server,
            "POST",
            "/api/sync",
            headers={
                "Authorization": "Bearer run-token",
                "Origin": "http://evil.example",
            },
            body=b"{}",
        )
        assert response.status == 403
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_token_null_preserves_loopback_api_and_hides_bootstrap_route(ns):
    server, thread = _serve(ns, None)
    try:
        _, data, _ = _request(server, "GET", "/api/data")
        assert data.status == 200
        _, auth, _ = _request(server, "POST", "/api/auth")
        assert auth.status == 404
    finally:
        server.shutdown()
        thread.join(timeout=2)
