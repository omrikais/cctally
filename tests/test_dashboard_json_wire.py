"""Strict JSON wire-contract coverage for dashboard HTTP/SSE boundaries."""
import http.client
import ast
import json
import pathlib
import sys
import threading
import time

import pytest

from conftest import load_script


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Keep doctor/dashboard reads away from the developer's live data."""
    (tmp_path / ".local" / "share" / "cctally").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _reject_nonfinite(token: str):
    raise ValueError(f"non-finite JSON token: {token}")


def _strict_loads(raw: str):
    return json.loads(raw, parse_constant=_reject_nonfinite)


def _get(ns, path: str):
    srv = ns["ThreadingHTTPServer"](
        ("127.0.0.1", 0), ns["DashboardHTTPHandler"]
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", srv.server_address[1], timeout=3
        )
        conn.request("GET", path)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result
    finally:
        srv.shutdown()
        thread.join(timeout=2)
        srv.server_close()


def test_api_doctor_absent_statusline_markers_emit_nullable_strict_json():
    """Missing freshness markers must not leak bare ``Infinity`` to browsers."""
    ns = load_script()
    srv = ns["ThreadingHTTPServer"](
        ("127.0.0.1", 0), ns["DashboardHTTPHandler"]
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection(
            "127.0.0.1", srv.server_address[1], timeout=3
        )
        conn.request("GET", "/api/doctor")
        response = conn.getresponse()
        raw = response.read().decode("utf-8")

        assert response.status == 200
        assert all(token not in raw for token in ("NaN", "Infinity", "-Infinity"))
        payload = _strict_loads(raw)
        data_category = next(
            category for category in payload["categories"]
            if category["id"] == "data"
        )
        pipeline = next(
            check for check in data_category["checks"]
            if check["id"] == "data.statusline_pipeline"
        )
        assert pipeline["summary"] == "no recent regular-pool timer observed"
        assert pipeline["details"]["transport_age_seconds"] is None
        assert pipeline["details"]["selected_age_seconds"] is None
        conn.close()
    finally:
        srv.shutdown()
        thread.join(timeout=2)
        srv.server_close()


def test_api_data_recursively_normalizes_nonfinite_values(monkeypatch):
    ns = load_script()
    dash = sys.modules["_cctally_dashboard"]
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    monkeypatch.setattr(
        dash,
        "snapshot_to_envelope",
        lambda *_args, **_kwargs: {
            "finite": 4.5,
            "nested": [{"nan": float("nan")}, (float("inf"), -float("inf"))],
        },
    )

    status, headers, raw_bytes = _get(ns, "/api/data")
    raw = raw_bytes.decode("utf-8")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    payload = _strict_loads(raw)
    assert isinstance(payload.pop("transcriptsEnabled"), bool)
    assert payload == {
        "finite": 4.5,
        "nested": [{"nan": None}, [None, None]],
    }


def test_api_events_recursively_normalizes_nonfinite_values(monkeypatch):
    ns = load_script()
    dash = sys.modules["_cctally_dashboard"]
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)
    monkeypatch.setattr(
        dash,
        "snapshot_to_envelope",
        lambda *_args, **_kwargs: {
            "nested": {"positive": float("inf"), "negative": -float("inf")},
        },
    )
    srv = ns["ThreadingHTTPServer"](
        ("127.0.0.1", 0), ns["DashboardHTTPHandler"]
    )
    srv.handle_error = lambda request, client_address: None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        hub.publish(snap)
        conn = http.client.HTTPConnection(
            "127.0.0.1", srv.server_address[1], timeout=3
        )
        conn.request("GET", "/api/events")
        response = conn.getresponse()
        frame = b""
        deadline = time.monotonic() + 2
        while b"\n\n" not in frame and time.monotonic() < deadline:
            frame += response.fp.read1(4096)
        data_line = next(
            line for line in frame.decode("utf-8").splitlines()
            if line.startswith("data: ")
        )
        payload = _strict_loads(data_line.removeprefix("data: "))
        assert isinstance(payload.pop("transcriptsEnabled"), bool)
        assert payload == {
            "nested": {"positive": None, "negative": None},
        }
        conn.close()
    finally:
        srv.shutdown()
        thread.join(timeout=2)
        srv.server_close()


def test_api_data_unsupported_objects_fail_with_clean_json_500(monkeypatch):
    ns = load_script()
    dash = sys.modules["_cctally_dashboard"]
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    monkeypatch.setattr(
        dash,
        "snapshot_to_envelope",
        lambda *_args, **_kwargs: {"unsupported": object()},
    )

    status, _, raw = _get(ns, "/api/data")

    assert status == 500
    assert _strict_loads(raw.decode("utf-8")) == {"error": "internal error"}


def test_dashboard_modules_have_no_permissive_json_dumps_calls():
    """New HTTP/SSE emitters must use the shared strict wire chokepoint.

    The source-identity hashes are not outbound frames; they remain an
    explicitly equivalent audited boundary with ``allow_nan=False``.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    for path in sorted((root / "bin").glob("_cctally_dashboard*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_dump_aliases = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "json"
            for alias in node.names
            if alias.name in {"dump", "dumps"}
        ]
        assert imported_dump_aliases == [], path.name
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "json"
            and node.func.attr in {"dump", "dumps"}
        ]
        if path.name != "_cctally_dashboard_sources.py":
            assert calls == [], path.name
            continue
        assert calls, "the audited source-identity hashes unexpectedly disappeared"
        for call in calls:
            allow_nan = next(
                (kw.value for kw in call.keywords if kw.arg == "allow_nan"),
                None,
            )
            assert isinstance(allow_nan, ast.Constant) and allow_nan.value is False
