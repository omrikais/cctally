"""Unit tests for the in-process refresh-usage helper."""
from conftest import load_script, redirect_paths


def test_refresh_inproc_ok(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    fake_api = {
        "seven_day": {"utilization": 42.5, "resets_at": "2026-05-10T00:00:00Z"},
        "five_hour": {"utilization": 5.0, "resets_at": "2026-05-03T05:00:00Z"},
    }
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: fake_api)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: None)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "ok"
    assert result.fallback is False
    assert result.reason is None


def test_refresh_inproc_no_token(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: None)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "no_oauth_token"


def test_refresh_inproc_rate_limited(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    err = ns["RefreshUsageRateLimitError"]("hit 429")
    def _raise(token, timeout_seconds):
        raise err
    monkeypatch.setitem(ns, "_fetch_oauth_usage", _raise)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "rate_limited"
    assert result.fallback is True


def test_refresh_inproc_fetch_failed(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    def _raise(token, timeout_seconds):
        raise ns["RefreshUsageNetworkError"]("DNS fail")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", _raise)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "fetch_failed"


def test_refresh_inproc_parse_failed_malformed(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    def _raise(token, timeout_seconds):
        raise ns["RefreshUsageMalformedError"]("bad shape")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", _raise)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "parse_failed"


def test_refresh_inproc_parse_failed_seven_day_fields(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    bad_api = {"seven_day": {"utilization": "not-a-float", "resets_at": "x"}}
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: bad_api)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: None)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "parse_failed"


def test_refresh_inproc_record_failed(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    fake_api = {
        "seven_day": {"utilization": 42.5, "resets_at": "2026-05-10T00:00:00Z"},
    }
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: fake_api)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 7)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "record_failed"


def test_refresh_inproc_test_env_stub(monkeypatch, tmp_path):
    """CCTALLY_TEST_REFRESH_RESULT bypasses real OAuth path for harness use."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_TEST_REFRESH_RESULT", "rate_limited")

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "rate_limited"
    assert result.fallback is True
