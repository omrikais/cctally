"""Real HTTP admission and cancellation regressions for progressive outlines."""

import base64
import concurrent.futures
import hashlib
import json
import resource
import socket
import sys
import threading
import time

from conftest import load_script
from test_conversation_endpoints import _boot, _get
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop


OUTLINE_DISCONNECT_BACKSTOP_SECONDS = 20.0


def _transfer(ns, builder):
    mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    token = mod._store_outline_transfer(builder)["token"]
    return mod, token, f"/api/conversation/outline-transfer/{token}?offset=0"


def _json_get(port, path):
    status, wire = _get(port, path)
    return status, json.loads(wire)


def _production_server(ns):
    handler = ns["DashboardHTTPHandler"]
    handler.cctally_host = "127.0.0.1"
    handler.cctally_expose_transcripts = False
    srv = sys.modules["_cctally_dashboard"]._QuietThreadingHTTPServer(
        ("127.0.0.1", 0), handler)
    srv._test_thread = start(srv)
    return srv


def test_more_than_64_http_joiners_are_bounded_and_recover():
    ns = load_script()
    srv = _production_server(ns)
    mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def builder(_handler, cancelled):
        calls.append(1)
        entered.set()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return True, {"session_id": "shared", "turns": [], "stats": {}}

    token = mod._store_outline_transfer(builder)["token"]
    path = f"/api/conversation/outline-transfer/{token}?offset=0"
    start = threading.Event()

    def caller():
        assert start.wait(PRESENCE_BACKSTOP_SECONDS)
        began = time.monotonic()
        status, body = _json_get(srv.server_address[1], path)
        return status, body, time.monotonic() - began

    rss_unit = 1 if sys.platform == "darwin" else 1024
    baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit
    peak_threads = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=72) as pool:
            futures = [pool.submit(caller) for _ in range(72)]
            try:
                start.set()
                assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    peak_threads = max(
                        peak_threads,
                        sum(worker.is_alive() for worker in srv._threads),
                    )
                    if sum(f.done() for f in futures) >= 56:
                        break
                    time.sleep(0.02)
                assert sum(f.done() for f in futures) >= 56
                release.set()
                results = [f.result(timeout=PRESENCE_BACKSTOP_SECONDS)
                           for f in futures]
            finally:
                release.set()
        assert sum(status == 503 for status, _body, _elapsed in results) >= 56
        assert all(body == {"error": "outline transfer busy"}
                   for status, body, _elapsed in results if status == 503)
        assert all(status in (200, 503) for status, _body, _elapsed in results)
        for status, body, _elapsed in results:
            if status == 200:
                chunk = base64.b64decode(body["chunk"])
                assert body["sha256"] == hashlib.sha256(chunk).hexdigest()
                assert body["offset"] == 0
                assert body["done"]
        assert len(calls) == 1
        assert _json_get(srv.server_address[1], path)[0] == 200
        latencies = sorted(elapsed for _status, _body, elapsed in results)
        statuses = [status for status, _body, _elapsed in results]
        print(json.dumps({
            "case": "outline-72-http",
            "statuses": {"200": statuses.count(200), "503": statuses.count(503)},
            "peakRequestThreads": peak_threads,
            "requestThreadCap": srv.max_request_threads,
            "rssBaselineBytes": baseline_rss,
            "rssPeakBytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * rss_unit,
            "p95LatencyMs": round(latencies[int(len(latencies) * .95) - 1] * 1000),
            "maxLatencyMs": round(latencies[-1] * 1000),
            "recovery": "200",
        }, sort_keys=True))
    finally:
        release.set()
        stop(srv, srv._test_thread)


def test_producer_limit_timeout_and_failure_release_admission(
    tmp_path, monkeypatch,
):
    lifetime_backstop = 20.0
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    monkeypatch.setattr(mod, "_OUTLINE_TRANSFER_MAX_PRODUCERS", 1,
                        raising=False)
    monkeypatch.setattr(mod, "_OUTLINE_TRANSFER_REQUEST_DEADLINE", 0.3,
                        raising=False)
    entered = threading.Event()
    cancelled = threading.Event()
    release = threading.Event()

    def slow(_handler, is_cancelled):
        entered.set()
        while not release.wait(0.01):
            if is_cancelled():
                cancelled.set()
                break
        return True, {"session_id": "slow"}

    _mod, _token, slow_path = _transfer(ns, slow)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_json_get, srv.server_address[1], slow_path)
        try:
            assert entered.wait(lifetime_backstop)
            _mod, _token, blocked_path = _transfer(
                ns, lambda *_args: (True, {"session_id": "blocked"}))
            assert _json_get(srv.server_address[1], blocked_path) == (
                503, {"error": "outline transfer busy"})
            assert first.result(timeout=lifetime_backstop) == (
                504, {"error": "outline transfer timed out"})
            assert cancelled.wait(lifetime_backstop)
            deadline = time.monotonic() + lifetime_backstop
            while mod._OUTLINE_TRANSFERS_PRODUCERS and time.monotonic() < deadline:
                time.sleep(0.01)
            assert mod._OUTLINE_TRANSFERS_PRODUCERS == 0
            assert _json_get(srv.server_address[1], blocked_path)[0] == 200

            _mod, _token, failed_path = _transfer(
                ns, lambda *_args: (_ for _ in ()).throw(RuntimeError("boom")))
            assert _json_get(srv.server_address[1], failed_path) == (
                500, {"error": "outline transfer failed"})

            def degraded(handler, _cancelled):
                handler._respond_json(200, {"status": "degraded"})
                return False, None

            _mod, _token, degraded_path = _transfer(ns, degraded)
            assert _json_get(srv.server_address[1], degraded_path) == (
                200, {"status": "degraded"})
            _mod, _token, recovered_path = _transfer(
                ns, lambda *_args: (True, {"session_id": "recovered"}))
            assert _json_get(srv.server_address[1], recovered_path)[0] == 200
        finally:
            release.set()
            stop(srv, srv._test_thread)


def test_disconnect_and_expiry_cancel_active_producer(tmp_path, monkeypatch):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    monkeypatch.setattr(mod, "_OUTLINE_TRANSFER_REQUEST_DEADLINE", 2.0,
                        raising=False)
    monkeypatch.setattr(mod, "_OUTLINE_TRANSFER_TTL", 0.25)
    entered = threading.Event()
    cancelled = threading.Event()

    def builder(_handler, is_cancelled):
        entered.set()
        deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
        while time.monotonic() < deadline:
            if is_cancelled():
                cancelled.set()
                break
            time.sleep(0.01)
        return True, {"session_id": "late"}

    _mod, _token, path = _transfer(ns, builder)
    try:
        client = socket.create_connection(srv.server_address,
                                          timeout=OUTLINE_DISCONNECT_BACKSTOP_SECONDS)
        client.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        "Connection: close\r\n\r\n").encode("ascii"))
        assert entered.wait(OUTLINE_DISCONNECT_BACKSTOP_SECONDS)
        client.close()
        assert cancelled.wait(OUTLINE_DISCONNECT_BACKSTOP_SECONDS)
        assert _json_get(srv.server_address[1], path)[0] == 410

        entered.clear()
        cancelled.clear()
        _mod, _token, path = _transfer(ns, builder)
        assert _json_get(srv.server_address[1], path) == (
            410, {"error": "outline transfer expired"})
        assert cancelled.wait(OUTLINE_DISCONNECT_BACKSTOP_SECONDS)
    finally:
        stop(srv, srv._test_thread)


def test_replaced_generation_survives_old_producer_completion(
    tmp_path, monkeypatch,
):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, bind="127.0.0.1", expose=False)
    mod = ns["_load_sibling"]("_cctally_dashboard_conversation")
    monkeypatch.setattr(mod.secrets, "token_urlsafe", lambda _n: "same-token")
    entered = threading.Event()
    release = threading.Event()

    def old(_handler, _cancelled):
        entered.set()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return True, {"session_id": "old"}

    _mod, _token, path = _transfer(ns, old)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_json_get, srv.server_address[1], path)
        try:
            assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
            _transfer(ns, lambda *_args: (True, {"session_id": "new"}))
            assert first.result(timeout=PRESENCE_BACKSTOP_SECONDS)[0] == 410
            status, body = _json_get(srv.server_address[1], path)
            assert status == 200
            assert json.loads(base64.b64decode(body["chunk"])) == {
                "session_id": "new"}
            release.set()
            deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
            while mod._OUTLINE_TRANSFERS_PRODUCERS and time.monotonic() < deadline:
                time.sleep(0.01)
            assert mod._OUTLINE_TRANSFERS_PRODUCERS == 0
            status, body = _json_get(srv.server_address[1], path)
            assert status == 200
            assert json.loads(base64.b64decode(body["chunk"])) == {
                "session_id": "new"}
        finally:
            release.set()
            stop(srv, srv._test_thread)
