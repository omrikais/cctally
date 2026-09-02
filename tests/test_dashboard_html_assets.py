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

import gzip
import http.client
import threading
from html.parser import HTMLParser

from conftest import load_script

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, serve_dashboard, stop


class _AssetExtractor(HTMLParser):
    """Collect script src and stylesheet href values from a built HTML doc."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []
        self.modulepreloads: list[str] = []

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
            if href and "modulepreload" in rel.split():
                self.modulepreloads.append(href)


def _check(host: str, port: int, url_path: str) -> int:
    c = http.client.HTTPConnection(host, port, timeout=PRESENCE_BACKSTOP_SECONDS)
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
    srv, t, port = serve_dashboard(ns)
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
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
        stop(srv, t)


def test_dashboard_shell_javascript_stays_within_cold_load_budget() -> None:
    """Re-eagerly importing deferred features must breach the cold JS budget."""
    ns = load_script()
    html = (ns["STATIC_DIR"] / "dashboard.html").read_text()
    extractor = _AssetExtractor()
    extractor.feed(html)

    scripts = [
        ns["STATIC_DIR"] / url.removeprefix("/static/")
        for url in extractor.scripts + extractor.modulepreloads
        if url.startswith("/static/")
    ]
    assert scripts, "dashboard.html must load at least one local entry script"
    missing = [str(path) for path in scripts if not path.is_file()]
    assert missing == [], f"dashboard entry scripts are missing: {missing}"

    raw_bytes = sum(path.stat().st_size for path in scripts)
    gzip_bytes = sum(
        len(gzip.compress(path.read_bytes(), compresslevel=6, mtime=0))
        for path in scripts
    )
    assert raw_bytes <= 950_000, (
        f"cold entry JavaScript is {raw_bytes:,} bytes; budget is 950,000"
    )
    assert gzip_bytes <= 280_000, (
        f"cold entry JavaScript is {gzip_bytes:,} gzip bytes; budget is 280,000"
    )
