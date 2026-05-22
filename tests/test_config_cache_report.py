"""Settings validation tests for the ``cache_report`` config block.

Covers ``_validate_cache_report_settings`` (the pure-fn validator) and
the ``POST /api/settings`` integration (handler round-trip: invalid
blocks → HTTP 400 with ``{error, field}``; valid blocks → 200 + the
merged block on the next envelope). Matches the project convention
of HTTP 400 (not 422) for every block validation error at
``_cctally_dashboard.py:4587-4602``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import conftest  # noqa: E402


def _bootstrap():
    """Load bin/cctally and return its dashboard sibling."""
    conftest.load_script()
    return sys.modules["_cctally_dashboard"]


def test_validate_accepts_default():
    dash = _bootstrap()
    block = {"anomaly_threshold_pp": 15}
    result = dash._validate_cache_report_settings(block)
    assert result == {"anomaly_threshold_pp": 15}


def test_validate_accepts_low_bound():
    dash = _bootstrap()
    result = dash._validate_cache_report_settings({"anomaly_threshold_pp": 1})
    assert result == {"anomaly_threshold_pp": 1}


def test_validate_accepts_high_bound():
    dash = _bootstrap()
    result = dash._validate_cache_report_settings({"anomaly_threshold_pp": 100})
    assert result == {"anomaly_threshold_pp": 100}


def test_validate_rejects_negative_threshold():
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings({"anomaly_threshold_pp": -1})
    assert exc.value.field == "anomaly_threshold_pp"


def test_validate_rejects_zero_threshold():
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings({"anomaly_threshold_pp": 0})
    assert exc.value.field == "anomaly_threshold_pp"


def test_validate_rejects_over_100_threshold():
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings({"anomaly_threshold_pp": 101})
    assert exc.value.field == "anomaly_threshold_pp"


def test_validate_rejects_string_threshold():
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings({"anomaly_threshold_pp": "abc"})
    assert exc.value.field == "anomaly_threshold_pp"


def test_validate_rejects_float_threshold():
    """The threshold is documented as an integer; floats are rejected so
    a future ``15.0`` slip-up gets caught at validation, not at
    integer-only math downstream."""
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings({"anomaly_threshold_pp": 15.0})
    assert exc.value.field == "anomaly_threshold_pp"


def test_validate_rejects_bool_threshold():
    """bool is an int subclass in Python; reject it explicitly (mirrors
    the ``update.check.ttl_hours`` precedent at
    _cctally_dashboard.py:4598-4601)."""
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings({"anomaly_threshold_pp": True})
    assert exc.value.field == "anomaly_threshold_pp"


def test_validate_rejects_unknown_key():
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings(
            {"anomaly_threshold_pp": 15, "frobnicate": True}
        )
    assert "unknown" in str(exc.value).lower()


def test_validate_rejects_anomaly_window_days_in_v1():
    """v1 deliberately rejects ``anomaly_window_days`` from the config
    block — only ``anomaly_threshold_pp`` is user-configurable
    (spec §6.1, F10 tracks lifting this)."""
    dash = _bootstrap()
    with pytest.raises(dash._CacheReportConfigError) as exc:
        dash._validate_cache_report_settings(
            {"anomaly_threshold_pp": 15, "anomaly_window_days": 14}
        )
    assert "unknown" in str(exc.value).lower() or "anomaly_window_days" in str(exc.value)


def test_validate_accepts_empty_block_returns_no_keys():
    """Empty block is accepted and returns an empty dict.

    Partial-PUT semantics: the validator only carries forward keys the
    caller explicitly supplied. Defaults are resolved at handler
    merge-time so a combined save with an empty ``cache_report`` does
    NOT clobber a previously persisted threshold (see H3 in the
    /check-review pass). The HTTP round-trip below covers this end-to-
    end via ``test_http_cache_report_empty_block_preserves_persisted``.
    """
    dash = _bootstrap()
    result = dash._validate_cache_report_settings({})
    assert result == {}


# ---------------------------------------------------------------------------
# HTTP integration — POST /api/settings round-trip
# ---------------------------------------------------------------------------

import http.client
import json
import threading

from conftest import load_script, redirect_paths


def _serve(ns, host="127.0.0.1", port=0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _wire_handlers(ns):
    """Minimal wiring so the POST settings handler can run (mirrors
    tests/test_dashboard_csrf.py)."""
    ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
    ns["DashboardHTTPHandler"].sync_lock = threading.Lock()
    ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(lambda: None)
    ns["DashboardHTTPHandler"].no_sync = False
    ns["DashboardHTTPHandler"].display_tz_pref_override = None


def _post_json(host, port, path, body, *, origin_host=None):
    """POST a JSON body with matched Host + Origin (loopback contract)."""
    c = http.client.HTTPConnection(host, port, timeout=2)
    raw = json.dumps(body).encode()
    host_header = f"{host}:{port}"
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(raw)))
    c.putheader("Host", host_header)
    c.putheader("Origin", f"http://{origin_host or host_header}")
    c.endheaders()
    c.send(raw)
    r = c.getresponse()
    payload = r.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(payload) if payload else None
    except json.JSONDecodeError:
        parsed = payload
    return r.status, parsed


def test_http_cache_report_valid_round_trip(monkeypatch, tmp_path):
    """Valid block returns 200 + the echoed cache_report block."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": {"anomaly_threshold_pp": 25}},
        )
        assert status == 200
        assert body is not None
        assert body["cache_report"] == {"anomaly_threshold_pp": 25}
        # The persisted config carries the new threshold.
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg.get("cache_report", {}).get("anomaly_threshold_pp") == 25
    finally:
        srv.shutdown()


def test_http_cache_report_invalid_threshold_returns_400(monkeypatch, tmp_path):
    """Out-of-range threshold → 400 with field='anomaly_threshold_pp'."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": {"anomaly_threshold_pp": -1}},
        )
        assert status == 400, body
        assert body is not None
        assert body.get("field") == "anomaly_threshold_pp"
    finally:
        srv.shutdown()


def test_http_cache_report_unknown_inner_key_returns_400(monkeypatch, tmp_path):
    """Unknown key inside the cache_report block → 400."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": {
                "anomaly_threshold_pp": 15,
                "anomaly_window_days": 14,  # v1 rejects this
            }},
        )
        assert status == 400, body
    finally:
        srv.shutdown()


def test_http_cache_report_non_dict_block_returns_400(monkeypatch, tmp_path):
    """cache_report: "abc" → 400 with field='cache_report'."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": "not-a-dict"},
        )
        assert status == 400, body
        assert body is not None
        assert body.get("field") == "cache_report"
    finally:
        srv.shutdown()


def test_http_top_level_unknown_key_still_rejected(monkeypatch, tmp_path):
    """The cache_report addition does NOT widen the top-level allowlist
    to anything else; unknown top-level keys still 400."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"frobnicate": {"x": 1}},
        )
        assert status == 400, body
    finally:
        srv.shutdown()


def test_http_cache_report_combined_save_with_display(monkeypatch, tmp_path):
    """Combined save: ``cache_report`` + ``display`` both validate and persist."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {
                "cache_report": {"anomaly_threshold_pp": 20},
                "display": {"tz": "Etc/UTC"},
            },
        )
        assert status == 200, body
        assert body["cache_report"] == {"anomaly_threshold_pp": 20}
        assert body["display"]["resolved_tz"] == "Etc/UTC"
    finally:
        srv.shutdown()


def test_http_cache_report_empty_block_preserves_persisted(
    monkeypatch, tmp_path,
):
    """An empty ``cache_report: {}`` must NOT clobber a previously
    persisted ``anomaly_threshold_pp``.

    Regression for H3 (/check-review round 4): the prior handler
    unconditionally replaced the whole ``cache_report`` block with
    the validator's defaulted value, so a combined save that omitted
    ``anomaly_threshold_pp`` silently overwrote the user's 42 with the
    default 15. The validator now returns only keys present in the
    request; the handler merges them into the existing block.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    # Pre-seed config.json with a non-default threshold.
    ns["CONFIG_PATH"].write_text(
        json.dumps({"cache_report": {"anomaly_threshold_pp": 42}}),
    )
    srv, t, port = _serve(ns)
    try:
        # Empty cache_report block (representative of a combined save
        # whose UI hasn't touched the cache-report tab).
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": {}},
        )
        assert status == 200, body
        # Echo carries the persisted value, not the default.
        assert body["cache_report"] == {"anomaly_threshold_pp": 42}
        # And the persisted config still has 42.
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg.get("cache_report", {}).get("anomaly_threshold_pp") == 42
    finally:
        srv.shutdown()


def test_http_cache_report_empty_block_with_no_persisted_uses_default(
    monkeypatch, tmp_path,
):
    """Empty block with no prior config.json carries the documented
    default (15) on the echo. The persisted block ends up empty until
    the user actually saves a value — that's intentional (avoid
    materializing defaults that downstream readers don't care about)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": {}},
        )
        assert status == 200, body
        # Echo defaults to 15 when nothing is persisted.
        assert body["cache_report"] == {"anomaly_threshold_pp": 15}
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        # cache_report block was written (it's the merge target), but
        # it doesn't carry an explicit threshold yet.
        assert "anomaly_threshold_pp" not in cfg.get("cache_report", {})
    finally:
        srv.shutdown()


def test_http_cache_report_partial_save_overwrites_existing_value(
    monkeypatch, tmp_path,
):
    """When the request explicitly carries a new threshold, it overrides
    the persisted value (sanity check for the partial-PUT merge — keys
    present in the input MUST take precedence)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _wire_handlers(ns)
    ns["CONFIG_PATH"].write_text(
        json.dumps({"cache_report": {"anomaly_threshold_pp": 42}}),
    )
    srv, t, port = _serve(ns)
    try:
        status, body = _post_json(
            "127.0.0.1", port, "/api/settings",
            {"cache_report": {"anomaly_threshold_pp": 30}},
        )
        assert status == 200, body
        assert body["cache_report"] == {"anomaly_threshold_pp": 30}
        cfg = json.loads(ns["CONFIG_PATH"].read_text())
        assert cfg["cache_report"]["anomaly_threshold_pp"] == 30
    finally:
        srv.shutdown()
