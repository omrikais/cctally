"""Regression test: every asset referenced by dashboard.html resolves via the
Python handler.

Historical bug: Vite emitted absolute asset URLs like
``/assets/index-*.js`` while ``DashboardHTTPHandler`` only serves ``/`` and
``/static/*``. Every built-asset request 404'd. Setting ``base: '/static/'``
in ``vite.config.ts`` fixes the emission; this test locks that in — it
fetches ``/``, parses ``<script src="...">`` and
``<link rel="stylesheet" href="...">`` out of the response body, and
asserts each local URL (anything starting with ``/``) returns 200.

It also asserts that at least one script AND one stylesheet are referenced,
so a future regression that produces an empty HTML shell cannot silently
pass the check.
"""
from __future__ import annotations

import http.client
import threading
from html.parser import HTMLParser

from conftest import load_script


class _AssetExtractor(HTMLParser):
    """Collect script src and stylesheet href values from a built HTML doc."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs}
        if tag == "script":
            src = a.get("src")
            if src:
                self.scripts.append(src)
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href and "stylesheet" in rel.split():
                self.stylesheets.append(href)


def _serve_once(ns, host: str = "127.0.0.1", port: int = 0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _check(host: str, port: int, url_path: str) -> int:
    c = http.client.HTTPConnection(host, port, timeout=2)
    c.request("GET", url_path)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_dashboard_html_references_all_resolvable_assets() -> None:
    ns = load_script()
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    srv, t, port = _serve_once(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        c.request("GET", "/")
        r = c.getresponse()
        assert r.status == 200, f"GET / status={r.status}"
        body = r.read().decode()
        c.close()

        extractor = _AssetExtractor()
        extractor.feed(body)

        # Guard against a future empty-HTML regression that would silently
        # pass the "every referenced asset loads" loop below.
        assert len(extractor.scripts) >= 1, (
            "dashboard.html must reference at least one <script src>; "
            f"got {extractor.scripts!r}"
        )
        assert len(extractor.stylesheets) >= 1, (
            "dashboard.html must reference at least one stylesheet <link>; "
            f"got {extractor.stylesheets!r}"
        )

        for url in extractor.scripts + extractor.stylesheets:
            if not url.startswith("/"):
                # Remote/protocol-relative URLs are out of scope here.
                continue
            status = _check("127.0.0.1", port, url)
            assert status == 200, (
                f"asset {url!r} did not resolve via the Python handler "
                f"(status={status}); check vite base config and the "
                f"dashboard/static/ build output"
            )
    finally:
        srv.shutdown()
        t.join(timeout=2)
