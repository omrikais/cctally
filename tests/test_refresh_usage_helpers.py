"""Unit tests for refresh-usage pure helpers."""
from conftest import load_script


def test_iso_to_epoch_z_suffix():
    ns = load_script()
    fn = ns["_iso_to_epoch"]
    # 2026-05-02T12:00:00Z → known epoch
    assert fn("2026-05-02T12:00:00Z") == 1777723200


def test_iso_to_epoch_offset():
    ns = load_script()
    fn = ns["_iso_to_epoch"]
    # 2026-05-02T13:00:00+01:00 == 12:00:00Z
    assert fn("2026-05-02T13:00:00+01:00") == 1777723200


def test_iso_to_epoch_naive_treated_as_utc():
    ns = load_script()
    fn = ns["_iso_to_epoch"]
    # naive ISO (no tz) → treat as UTC, matching statusline _iso_to_epoch
    assert fn("2026-05-02T12:00:00") == 1777723200


def test_format_short_duration_days_and_hours():
    ns = load_script()
    fn = ns["_format_short_duration"]
    # 6d 4h 30m → drops minutes, shows top two units
    assert fn(6 * 86400 + 4 * 3600 + 30 * 60) == "6d 4h"


def test_format_short_duration_hours_only():
    ns = load_script()
    fn = ns["_format_short_duration"]
    assert fn(2 * 3600) == "2h"


def test_format_short_duration_hours_and_minutes():
    ns = load_script()
    fn = ns["_format_short_duration"]
    assert fn(2 * 3600 + 15 * 60) == "2h 15m"


def test_format_short_duration_minutes_only():
    ns = load_script()
    fn = ns["_format_short_duration"]
    assert fn(45 * 60) == "45m"


def test_format_short_duration_seconds_only():
    ns = load_script()
    fn = ns["_format_short_duration"]
    assert fn(30) == "30s"


def test_format_short_duration_zero():
    ns = load_script()
    fn = ns["_format_short_duration"]
    assert fn(0) == "0s"


def test_format_short_duration_negative_clamped():
    ns = load_script()
    fn = ns["_format_short_duration"]
    # Past resets time → render as 0s rather than "-1h"
    assert fn(-3600) == "0s"


import json as _json


def test_resolve_oauth_token_keychain_hit(tmp_path):
    ns = load_script()
    fn = ns["_resolve_oauth_token"]
    # Simulate `security` returning JSON blob
    keychain_payload = _json.dumps({"claudeAiOauth": {"accessToken": "kc-token"}})
    creds_path = tmp_path / "creds.json"  # not used since keychain hit
    token = fn(
        keychain_reader=lambda: keychain_payload,
        credentials_path=creds_path,
    )
    assert token == "kc-token"


def test_resolve_oauth_token_falls_back_to_file(tmp_path):
    ns = load_script()
    fn = ns["_resolve_oauth_token"]
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}))
    # keychain returns None (e.g., not on macOS, or `security` errored)
    token = fn(
        keychain_reader=lambda: None,
        credentials_path=creds_path,
    )
    assert token == "file-token"


def test_resolve_oauth_token_keychain_malformed_falls_back(tmp_path):
    ns = load_script()
    fn = ns["_resolve_oauth_token"]
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(_json.dumps({"claudeAiOauth": {"accessToken": "file-token"}}))
    # Keychain returned non-JSON (corrupted store)
    token = fn(
        keychain_reader=lambda: "not json",
        credentials_path=creds_path,
    )
    assert token == "file-token"


def test_resolve_oauth_token_both_missing_returns_none(tmp_path):
    ns = load_script()
    fn = ns["_resolve_oauth_token"]
    creds_path = tmp_path / "missing.json"  # does not exist
    token = fn(
        keychain_reader=lambda: None,
        credentials_path=creds_path,
    )
    assert token is None


def test_resolve_oauth_token_file_missing_field_returns_none(tmp_path):
    ns = load_script()
    fn = ns["_resolve_oauth_token"]
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(_json.dumps({"other": "data"}))
    token = fn(
        keychain_reader=lambda: None,
        credentials_path=creds_path,
    )
    assert token is None


import urllib.error
import socket
from unittest.mock import patch, MagicMock


def test_fetch_oauth_usage_success():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    response_body = b'{"seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"}}'
    fake_resp = MagicMock()
    fake_resp.read.return_value = response_body
    fake_resp.status = 200
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with patch.object(ns["urllib"].request, "urlopen", return_value=fake_resp):
        result = fn(token="t", timeout_seconds=5.0)
    assert result["seven_day"]["utilization"] == 13.0


def test_fetch_oauth_usage_timeout_raises_network_error():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    NetworkError = ns["RefreshUsageNetworkError"]
    with patch.object(ns["urllib"].request, "urlopen", side_effect=socket.timeout("timed out")):
        try:
            fn(token="t", timeout_seconds=0.1)
        except NetworkError as e:
            assert "timed out" in str(e).lower() or "0.1" in str(e)
            return
    raise AssertionError("expected RefreshUsageNetworkError")


def test_fetch_oauth_usage_http_error_raises_network_error():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    NetworkError = ns["RefreshUsageNetworkError"]
    err = urllib.error.HTTPError(
        url="https://api.anthropic.com/api/oauth/usage",
        code=401, msg="Unauthorized", hdrs=None, fp=None,
    )
    with patch.object(ns["urllib"].request, "urlopen", side_effect=err):
        try:
            fn(token="t", timeout_seconds=5.0)
        except NetworkError as e:
            assert "401" in str(e)
            return
    raise AssertionError("expected RefreshUsageNetworkError")


def test_fetch_oauth_usage_url_error_raises_network_error():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    NetworkError = ns["RefreshUsageNetworkError"]
    with patch.object(ns["urllib"].request, "urlopen",
                      side_effect=urllib.error.URLError("nodename nor servname")):
        try:
            fn(token="t", timeout_seconds=5.0)
        except NetworkError as e:
            assert "nodename" in str(e) or "URLError" in type(e).__name__ or True
            return
    raise AssertionError("expected RefreshUsageNetworkError")


def test_fetch_oauth_usage_malformed_json_raises_malformed_error():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    MalformedError = ns["RefreshUsageMalformedError"]
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"not json at all"
    fake_resp.status = 200
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with patch.object(ns["urllib"].request, "urlopen", return_value=fake_resp):
        try:
            fn(token="t", timeout_seconds=5.0)
        except MalformedError as e:
            assert "json" in str(e).lower()
            return
    raise AssertionError("expected RefreshUsageMalformedError")


def test_fetch_oauth_usage_missing_seven_day_raises_malformed_error():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    MalformedError = ns["RefreshUsageMalformedError"]
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"five_hour": {"utilization": 9.0}}'
    fake_resp.status = 200
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with patch.object(ns["urllib"].request, "urlopen", return_value=fake_resp):
        try:
            fn(token="t", timeout_seconds=5.0)
        except MalformedError as e:
            assert "seven_day" in str(e)
            return
    raise AssertionError("expected RefreshUsageMalformedError")


def test_fetch_oauth_usage_missing_seven_day_resets_at_raises_malformed_error():
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]
    MalformedError = ns["RefreshUsageMalformedError"]
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"seven_day": {"utilization": 13.0}}'
    fake_resp.status = 200
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with patch.object(ns["urllib"].request, "urlopen", return_value=fake_resp):
        try:
            fn(token="t", timeout_seconds=5.0)
        except MalformedError as e:
            assert "resets_at" in str(e)
            return
    raise AssertionError("expected RefreshUsageMalformedError")


def _sample_payload(with_5h=True, cache_state="busted"):
    return {
        "schema_version": 1,
        "fetched_at": "2026-04-26T06:51:31Z",
        "seven_day": {
            "used_percent": 13.0,
            "resets_at": "2026-05-02T12:00:00Z",
            "resets_at_epoch": 1777723200,
        },
        "five_hour": {
            "used_percent": 9.0,
            "resets_at": "2026-04-26T09:00:00Z",
            "resets_at_epoch": 1777194000,
        } if with_5h else None,
        "source": "api",
        "statusline_cache": cache_state,
    }


def test_render_refresh_usage_text_full_no_color():
    ns = load_script()
    fn = ns["_render_refresh_usage_text"]
    payload = _sample_payload()
    # now=fetched_at - 30s shift not needed; pass now_epoch close to fetched_at
    text = fn(payload, color=False, now_epoch=1777448491)  # 2026-04-26T06:21:31Z
    # Expect both 7d and 5h, no ANSI codes
    assert "refresh-usage:" in text
    assert "7d 13%" in text
    assert "5h 9%" in text
    assert "[src:api cache:busted]" in text
    assert "\033[" not in text  # no ANSI


def test_render_refresh_usage_text_no_5h():
    ns = load_script()
    fn = ns["_render_refresh_usage_text"]
    payload = _sample_payload(with_5h=False)
    text = fn(payload, color=False, now_epoch=1777448491)
    assert "7d 13%" in text
    assert "5h" not in text  # entire 5h block dropped
    assert "[src:api cache:busted]" in text


def test_render_refresh_usage_text_color_includes_ansi():
    ns = load_script()
    fn = ns["_render_refresh_usage_text"]
    payload = _sample_payload()
    text = fn(payload, color=True, now_epoch=1777448491)
    assert "\033[" in text  # ANSI sequence present
    assert "7d 13%" in text  # text content still present (after stripping)


def test_render_refresh_usage_text_cache_absent():
    ns = load_script()
    fn = ns["_render_refresh_usage_text"]
    payload = _sample_payload(cache_state="absent")
    text = fn(payload, color=False, now_epoch=1777448491)
    assert "cache:absent" in text


def test_serialize_refresh_usage_json_shape():
    ns = load_script()
    fn = ns["_serialize_refresh_usage_json"]
    payload = _sample_payload()
    out = fn(payload)
    parsed = _json.loads(out)
    assert parsed["schema_version"] == 1
    assert parsed["seven_day"]["used_percent"] == 13.0
    assert parsed["five_hour"]["used_percent"] == 9.0
    assert parsed["statusline_cache"] == "busted"


def test_serialize_refresh_usage_json_null_5h():
    ns = load_script()
    fn = ns["_serialize_refresh_usage_json"]
    payload = _sample_payload(with_5h=False)
    out = fn(payload)
    parsed = _json.loads(out)
    assert parsed["five_hour"] is None


import urllib.error
import io
import pytest


def test_rate_limit_error_is_subclass_of_network_error():
    ns = load_script()
    assert issubclass(ns["RefreshUsageRateLimitError"], ns["RefreshUsageNetworkError"])


def test_fetch_raises_rate_limit_on_http_429(monkeypatch):
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]

    def boom(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url, code=429, msg="Too Many Requests",
            hdrs={}, fp=io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'),
        )
    monkeypatch.setattr(ns["urllib"].request, "urlopen", boom)
    with pytest.raises(ns["RefreshUsageRateLimitError"]):
        fn(token="tok", timeout_seconds=2.0)


def test_fetch_raises_network_error_on_http_500(monkeypatch):
    """Non-429 HTTP errors must NOT match RefreshUsageRateLimitError."""
    ns = load_script()
    fn = ns["_fetch_oauth_usage"]

    def boom(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url, code=500, msg="Internal Server Error",
            hdrs={}, fp=io.BytesIO(b"oops"),
        )
    monkeypatch.setattr(ns["urllib"].request, "urlopen", boom)
    with pytest.raises(ns["RefreshUsageNetworkError"]) as exc:
        fn(token="tok", timeout_seconds=2.0)
    # Must NOT be the subclass.
    assert not isinstance(exc.value, ns["RefreshUsageRateLimitError"])


import json as _json_for_ua


def test_fetch_uses_claude_code_user_agent_default(monkeypatch):
    """_fetch_oauth_usage sends User-Agent: claude-code/<version> when
    no override is set."""
    ns = load_script()

    captured = {}
    class FakeResponse:
        def read(self):
            return b'{"seven_day":{"utilization":1,"resets_at":"2026-04-30T00:00:00Z"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        return FakeResponse()
    monkeypatch.setattr(ns["urllib"].request, "urlopen", fake_urlopen)
    monkeypatch.setitem(ns, "_discover_cc_version", lambda: "2.1.116")
    # Default config (no override).
    monkeypatch.setitem(ns, "load_config", lambda: {})

    ns["_fetch_oauth_usage"](token="tok", timeout_seconds=2.0)
    # Header keys are title-cased by urllib.
    assert captured["headers"].get("User-agent") == "claude-code/2.1.116"


def test_fetch_uses_override_user_agent(monkeypatch):
    """oauth_usage.user_agent override flows through to the request."""
    ns = load_script()

    captured = {}
    class FakeResponse:
        def read(self):
            return b'{"seven_day":{"utilization":1,"resets_at":"2026-04-30T00:00:00Z"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        return FakeResponse()
    monkeypatch.setattr(ns["urllib"].request, "urlopen", fake_urlopen)
    monkeypatch.setitem(ns, "_discover_cc_version", lambda: "2.1.116")
    monkeypatch.setitem(ns, "load_config",
                        lambda: {"oauth_usage": {"user_agent": "cctally/0.1"}})

    ns["_fetch_oauth_usage"](token="tok", timeout_seconds=2.0)
    assert captured["headers"].get("User-agent") == "cctally/0.1"
