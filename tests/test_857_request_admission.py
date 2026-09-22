"""Real HTTP regression for the dashboard's request-thread boundary."""

import concurrent.futures
import http.client
import json
import resource
import socket
import sys
import threading
import time

from http.server import BaseHTTPRequestHandler

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_route import (
    WINDOW_END, _WINDOW_QUERY, _boot, _nonterminating_diagnosis_worker,
    _run_bounded_in_process,
)
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop


def _get(port, path):
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS,
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_active_request_cap_returns_http_503_and_recovers():
    load_script()
    server_class = sys.modules["_cctally_dashboard"]._QuietThreadingHTTPServer
    entered = threading.Condition()
    release = threading.Event()
    active = 0
    peak = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal active, peak
            with entered:
                active += 1
                peak = max(peak, active)
                entered.notify_all()
            try:
                if self.path == "/hold":
                    assert release.wait(PRESENCE_BACKSTOP_SECONDS)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with entered:
                    active -= 1
                    entered.notify_all()

        def log_message(self, *_args):
            pass

    class SmallServer(server_class):
        max_request_threads = 2

    server = SmallServer(("127.0.0.1", 0), Handler)
    thread = start(server)
    port = server.server_address[1]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_get, port, "/hold")
            second = pool.submit(_get, port, "/hold")
            with entered:
                assert entered.wait_for(
                    lambda: active == 2, timeout=PRESENCE_BACKSTOP_SECONDS,
                )
            status, headers, body = _get(port, "/fast")
            assert status == 503
            assert json.loads(body) == {"error": "server busy"}
            assert headers["Cache-Control"] == "no-store"
            assert peak == 2
            release.set()
            assert first.result()[0] == second.result()[0] == 200
        assert _get(port, "/fast")[0] == 200
    finally:
        release.set()
        stop(server, thread)


def test_dashboard_listener_has_burst_backlog():
    load_script()
    server_class = sys.modules["_cctally_dashboard"]._QuietThreadingHTTPServer
    assert server_class.request_queue_size >= 128


def test_slow_rejected_client_does_not_block_accept_loop():
    load_script()
    server_class = sys.modules["_cctally_dashboard"]._QuietThreadingHTTPServer
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/hold":
                entered.set()
                assert release.wait(PRESENCE_BACKSTOP_SECONDS)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    class SmallServer(server_class):
        max_request_threads = 1

    server = SmallServer(("127.0.0.1", 0), Handler)
    thread = start(server)
    port = server.server_address[1]
    slow = None
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            held = pool.submit(_get, port, "/hold")
            assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
            slow = socket.create_connection(
                ("127.0.0.1", port), timeout=PRESENCE_BACKSTOP_SECONDS,
            )
            status, _headers, body = _get(port, "/fast")
            assert (status, json.loads(body)) == (
                503, {"error": "server busy"},
            )
            # The held request is still blocked here, so this completed 503
            # proves the slow rejected peer did not block the accept loop.
            assert not release.is_set()
            release.set()
            assert held.result()[0] == 200
    finally:
        release.set()
        if slow is not None:
            slow.close()
        stop(server, thread)


def test_diagnosis_real_http_burst_has_bounded_waiters_and_recovers(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", WINDOW_END.isoformat().replace("+00:00", "Z"))
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    server, thread, client = _boot(ns)
    dash = sys.modules["_cctally_dashboard"]
    admission = dash._DiagnosisAdmission()
    monkeypatch.setattr(dash, "_DIAGNOSIS_ADMISSION", admission)
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    real_build = sources.build_diagnosis
    entered = threading.Event()
    release = threading.Event()
    start = threading.Event()
    build_count = 0
    build_lock = threading.Lock()
    paths = [
        f"/api/diagnosis?window={_WINDOW_QUERY}&source={source}"
        for source in ("all", "claude") for _ in range(40)
    ]

    def blocked_build(*args, **kwargs):
        nonlocal build_count
        with build_lock:
            build_count += 1
        entered.set()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return real_build(*args, **kwargs)

    def call(path):
        assert start.wait(PRESENCE_BACKSTOP_SECONDS)
        began = time.monotonic()
        response = client.get(path)
        return response.status, response.json, time.monotonic() - began

    monkeypatch.setattr(sources, "build_diagnosis", blocked_build)
    _run_bounded_in_process(monkeypatch, sources)
    rss_unit = 1 if sys.platform == "darwin" else 1024
    baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit
    peak_threads = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=80) as pool:
            futures = [pool.submit(call, path) for path in paths]
            try:
                start.set()
                assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    peak_threads = max(
                        peak_threads,
                        sum(worker.is_alive() for worker in server._threads),
                    )
                    if sum(f.done() for f in futures) >= 48:
                        break
                    time.sleep(0.01)
                assert sum(f.done() for f in futures) >= 48
                release.set()
                results = [f.result(timeout=PRESENCE_BACKSTOP_SECONDS)
                           for f in futures]
            finally:
                release.set()
        statuses = [status for status, _body, _elapsed in results]
        assert statuses.count(503) >= 48
        assert all(status in (200, 503) for status in statuses)
        assert build_count == 2
        assert peak_threads <= server.max_request_threads
        assert client.get(paths[0]).status == 200
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit
        latencies = sorted(elapsed for _status, _body, elapsed in results)
        print(json.dumps({
            "case": "diagnosis-80-http",
            "statuses": {"200": statuses.count(200), "503": statuses.count(503)},
            "peakRequestThreads": peak_threads,
            "requestThreadCap": server.max_request_threads,
            "rssBaselineBytes": baseline_rss,
            "rssPeakBytes": peak_rss,
            "p95LatencyMs": round(latencies[int(len(latencies) * .95) - 1] * 1000),
            "maxLatencyMs": round(latencies[-1] * 1000),
            "recovery": "200",
        }, sort_keys=True))
    finally:
        stop(server, thread)


def test_disconnected_diagnosis_waiter_releases_its_admission_slot(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", WINDOW_END.isoformat().replace("+00:00", "Z"))
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    server, thread, client = _boot(ns)
    dash = sys.modules["_cctally_dashboard"]
    admission = dash._DiagnosisAdmission(max_callers=1, max_per_key=1)
    monkeypatch.setattr(dash, "_DIAGNOSIS_ADMISSION", admission)
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    real_build = sources.build_diagnosis
    entered = threading.Event()
    release = threading.Event()
    path = f"/api/diagnosis?window={_WINDOW_QUERY}&source=claude"

    def blocked_build(*args, **kwargs):
        entered.set()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(sources, "build_diagnosis", blocked_build)
    _run_bounded_in_process(monkeypatch, sources)
    connection = socket.create_connection(server.server_address)
    try:
        connection.sendall(
            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
        )
        assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
        with admission._changed:
            assert admission._callers == 1
        connection.close()
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: admission._callers == 0, timeout=10.0,
            ), "closed client retained its diagnosis waiter slot"
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fresh = pool.submit(client.get, path)
            with admission._changed:
                assert admission._changed.wait_for(
                    lambda: admission._callers == 1, timeout=10.0,
                )
            release.set()
            assert fresh.result(timeout=PRESENCE_BACKSTOP_SECONDS).status == 200
    finally:
        connection.close()
        release.set()
        stop(server, thread)


def test_http_diagnosis_recovers_after_nonterminating_process_is_retired(
    tmp_path, monkeypatch,
):
    """A timed-out physical producer cannot consume the sole slot forever."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv(
        "CCTALLY_AS_OF", WINDOW_END.isoformat().replace("+00:00", "Z"),
    )
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    server, thread, client = _boot(ns)
    dash = sys.modules["_cctally_dashboard"]
    admission = dash._DiagnosisAdmission(deadline_seconds=1.5)
    monkeypatch.setattr(dash, "_DIAGNOSIS_ADMISSION", admission)
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    real_bounded = sources.build_diagnosis_bounded
    calls = 0

    def first_process_never_returns(*args, timeout_seconds, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return sources._run_process_with_deadline(
                _nonterminating_diagnosis_worker, (),
                timeout_seconds=timeout_seconds,
            )
        return real_bounded(
            *args, timeout_seconds=timeout_seconds, **kwargs,
        )

    monkeypatch.setattr(
        sources, "build_diagnosis_bounded", first_process_never_returns,
    )
    path = f"/api/diagnosis?window={_WINDOW_QUERY}&source=claude"
    try:
        timed_out = client.get(path)
        assert timed_out.status == 503
        assert timed_out.json == {
            "error": "diagnosis timed out; retry shortly",
            "code": "diagnosis_timeout",
        }
        recovered = client.get(path)
        assert recovered.status == 200, recovered.body
        assert calls == 2
    finally:
        stop(server, thread)
