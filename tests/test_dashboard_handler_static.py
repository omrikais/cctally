"""HTTP behavior tests for the static-serving path of DashboardHTTPHandler."""
import gzip
import http.client
import threading
import time
import pathlib

from conftest import load_script

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, serve_dashboard, stop


def _request(port, path, *, headers=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS
    )
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


def _dashboard_server():
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    return ns, *serve_dashboard(ns)


def _hashed_javascript_asset(ns):
    candidates = sorted((ns["STATIC_DIR"] / "assets").glob("*.js"))
    assert candidates, "the committed dashboard build must contain JavaScript"
    return max(candidates, key=lambda candidate: candidate.stat().st_size)


def test_hashed_asset_negotiates_gzip_and_immutable_caching():
    """Removing compression or immutable caching must retransmit the main bundle."""
    ns, srv, thread, port = _dashboard_server()
    try:
        asset = _hashed_javascript_asset(ns)
        status, headers, body = _request(
            port,
            f"/static/assets/{asset.name}",
            headers={"Accept-Encoding": "br, gzip"},
        )

        assert status == 200
        assert headers["Content-Encoding"] == "gzip"
        assert headers["Vary"] == "Accept-Encoding"
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert int(headers["Content-Length"]) == len(body)
        assert gzip.decompress(body) == asset.read_bytes()
        assert headers["ETag"].startswith('"') and headers["ETag"].endswith('"')
    finally:
        stop(srv, thread)


def test_hashed_asset_revalidates_each_content_encoding_by_etag():
    """Dropping variant ETags must not turn a gzip validator into an identity 304."""
    ns, srv, thread, port = _dashboard_server()
    try:
        asset = _hashed_javascript_asset(ns)
        path = f"/static/assets/{asset.name}"
        first_status, first_headers, _ = _request(
            port, path, headers={"Accept-Encoding": "gzip"}
        )
        assert first_status == 200

        status, headers, body = _request(
            port,
            path,
            headers={
                "Accept-Encoding": "gzip",
                "If-None-Match": first_headers["ETag"],
            },
        )
        assert status == 304
        assert body == b""
        assert headers["ETag"] == first_headers["ETag"]
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable"

        identity_status, identity_headers, identity_body = _request(
            port,
            path,
            headers={
                "Accept-Encoding": "gzip;q=0",
                "If-None-Match": first_headers["ETag"],
            },
        )
        assert identity_status == 200
        assert "Content-Encoding" not in identity_headers
        assert identity_headers["ETag"] != first_headers["ETag"]
        assert identity_body == asset.read_bytes()
    finally:
        stop(srv, thread)


def test_dashboard_shell_stays_mutable_and_revalidates():
    """Treating the mutable shell as immutable must never mix dashboard builds."""
    ns, srv, thread, port = _dashboard_server()
    try:
        status, headers, body = _request(port, "/")
        assert status == 200
        assert body == (ns["STATIC_DIR"] / "dashboard.html").read_bytes()
        assert headers["Cache-Control"] == "no-cache"
        assert "immutable" not in headers["Cache-Control"]

        status, revalidated_headers, revalidated_body = _request(
            port, "/", headers={"If-None-Match": headers["ETag"]}
        )
        assert status == 304
        assert revalidated_body == b""
        assert revalidated_headers["ETag"] == headers["ETag"]
        assert revalidated_headers["Cache-Control"] == "no-cache"
    finally:
        stop(srv, thread)


def test_warm_shell_304_skips_read_and_gzip_then_changed_build_invalidates(
    tmp_path, monkeypatch,
):
    shell = tmp_path / "dashboard.html"
    shell.write_bytes(b"<html>build one</html>")
    ns, srv, thread, port = _dashboard_server()
    ns["DashboardHTTPHandler"].static_dir = tmp_path
    real_read = pathlib.Path.read_bytes
    real_compress = gzip.compress
    counts = {"read": 0, "gzip": 0}

    def counted_read(path):
        if path == shell:
            counts["read"] += 1
        return real_read(path)

    def counted_compress(*args, **kwargs):
        counts["gzip"] += 1
        return real_compress(*args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counted_read)
    monkeypatch.setattr(gzip, "compress", counted_compress)
    try:
        path_headers = {"Accept-Encoding": "gzip"}
        status, first, body = _request(port, "/", headers=path_headers)
        assert status == 200
        assert gzip.decompress(body) == b"<html>build one</html>"
        assert counts == {"read": 1, "gzip": 1}

        status, warm, body = _request(port, "/", headers={
            **path_headers, "If-None-Match": first["ETag"],
        })
        assert status == 304 and body == b""
        assert warm["ETag"] == first["ETag"]
        assert warm["Vary"] == "Accept-Encoding"
        assert counts == {"read": 1, "gzip": 1}

        # A warm 200 still needs the actual bytes; this cache holds validators,
        # not potentially stale response bodies.
        status, repeated, body = _request(port, "/", headers=path_headers)
        assert status == 200 and gzip.decompress(body) == b"<html>build one</html>"
        assert repeated["ETag"] == first["ETag"]
        assert counts == {"read": 2, "gzip": 2}

        status, identity, body = _request(port, "/", headers={
            "Accept-Encoding": "gzip;q=0", "If-None-Match": first["ETag"],
        })
        assert status == 200 and body == b"<html>build one</html>"
        assert identity["ETag"] != first["ETag"]
        assert identity["Vary"] == "Accept-Encoding"
        assert counts == {"read": 3, "gzip": 2}
        status, _, body = _request(port, "/", headers={
            "Accept-Encoding": "gzip;q=0", "If-None-Match": identity["ETag"],
        })
        assert status == 304 and body == b""
        assert counts == {"read": 3, "gzip": 2}

        shell.write_bytes(b"<html>build two</html>")
        status, changed, body = _request(port, "/", headers={
            **path_headers, "If-None-Match": first["ETag"],
        })
        assert status == 200
        assert gzip.decompress(body) == b"<html>build two</html>"
        assert changed["ETag"] != first["ETag"]
        assert counts == {"read": 4, "gzip": 3}
        print({"case": "shell-revalidation", "readCalls": counts["read"],
               "gzipCalls": counts["gzip"], "warmGzip304ReadCalls": 0,
               "warmGzip304CompressCalls": 0,
               "warmIdentity304ReadCalls": 0,
               "warmIdentity304CompressCalls": 0,
               "changedBuildStatus": 200})
    finally:
        stop(srv, thread)


def test_static_placeholder_served_with_200():
    ns = load_script()
    # Seed the handler's hub so /api/events wouldn't fail (not tested here).
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = serve_dashboard(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/static/placeholder.txt")
        r = c.getresponse()
        body = r.read().decode()
        assert r.status == 200, f"status={r.status}"
        assert "placeholder" in body
    finally:
        stop(srv, t)


def test_static_404_on_missing_file():
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = serve_dashboard(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/static/does-not-exist.css")
        r = c.getresponse()
        r.read()
        assert r.status == 404
    finally:
        stop(srv, t)


def test_static_denies_path_traversal():
    """Must not serve files outside STATIC_DIR."""
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = serve_dashboard(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/static/../../bin/cctally")
        r = c.getresponse()
        r.read()
        assert r.status == 400
    finally:
        stop(srv, t)


def test_static_denies_percent_encoded_traversal():
    """Percent-encoded `..` bypasses lexical check but must be caught by containment."""
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = serve_dashboard(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
        c.request("GET", "/static/%2e%2e/%2e%2e/bin/cctally")
        r = c.getresponse()
        r.read()
        # 403 is the deterministic outcome: lexical check doesn't fire
        # (rel contains no literal ".."), but relative_to() rejects the
        # resolved path as outside STATIC_DIR.
        assert r.status in (403, 404)
    finally:
        stop(srv, t)
