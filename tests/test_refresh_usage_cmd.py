"""Unit tests for cmd_refresh_usage orchestration + exit-code mapping."""
import argparse
import json as _json
import pathlib
import subprocess
import pytest

from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def _args(json=False, quiet=False, color="never", timeout=5.0):
    return argparse.Namespace(
        json=json, quiet=quiet, color=color, timeout=timeout,
    )


def _stub_token(monkeypatch, ns, token="tok"):
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: token)


def _stub_fetch_ok(monkeypatch, ns, api_response):
    monkeypatch.setitem(
        ns, "_fetch_oauth_usage",
        lambda token, timeout_seconds: api_response,
    )


def _stub_fetch_raises(monkeypatch, ns, exc):
    def boom(token, timeout_seconds):
        raise exc
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)


def _stub_record_ok(monkeypatch, ns, capture=None):
    def fake(record_args):
        if capture is not None:
            capture["args"] = record_args
        return 0
    monkeypatch.setitem(ns, "cmd_record_usage", fake)


def _stub_record_raises(monkeypatch, ns, exc):
    def boom(record_args):
        raise exc
    monkeypatch.setitem(ns, "cmd_record_usage", boom)


def _stub_cache_busted(monkeypatch, ns, state="busted"):
    monkeypatch.setitem(
        ns, "_bust_statusline_cache",
        lambda path=None: state,
    )


def test_cmd_refresh_usage_success_writes_one_liner(ns, monkeypatch, capsys):
    api_response = {
        "seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"},
        "five_hour": {"utilization": 9.0, "resets_at": "2026-04-26T09:00:00Z"},
    }
    captured = {}
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    _stub_record_ok(monkeypatch, ns, capture=captured)
    _stub_cache_busted(monkeypatch, ns)

    rc = ns["cmd_refresh_usage"](_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "7d 13%" in out
    assert "5h 9%" in out
    rec = captured["args"]
    assert rec.percent == 13.0
    assert int(rec.resets_at) == 1777723200
    assert rec.five_hour_percent == 9.0
    assert int(rec.five_hour_resets_at) == 1777194000


def test_cmd_refresh_usage_no_token_returns_2(ns, monkeypatch, capsys):
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: None)
    rc = ns["cmd_refresh_usage"](_args())
    err = capsys.readouterr().err
    assert rc == 2
    assert "no OAuth token" in err


def test_cmd_refresh_usage_network_error_returns_3(ns, monkeypatch, capsys):
    NetworkError = ns["RefreshUsageNetworkError"]
    _stub_token(monkeypatch, ns)
    _stub_fetch_raises(monkeypatch, ns, NetworkError("timed out after 5.0s"))
    rc = ns["cmd_refresh_usage"](_args())
    err = capsys.readouterr().err
    assert rc == 3
    assert "timed out" in err


def test_cmd_refresh_usage_malformed_returns_4(ns, monkeypatch, capsys):
    MalformedError = ns["RefreshUsageMalformedError"]
    _stub_token(monkeypatch, ns)
    _stub_fetch_raises(monkeypatch, ns, MalformedError("response was not JSON"))
    rc = ns["cmd_refresh_usage"](_args())
    err = capsys.readouterr().err
    assert rc == 4
    assert "not JSON" in err


def test_cmd_refresh_usage_record_failure_returns_5(ns, monkeypatch, capsys):
    api_response = {"seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"}}
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    _stub_record_raises(monkeypatch, ns, RuntimeError("db locked"))
    _stub_cache_busted(monkeypatch, ns)
    rc = ns["cmd_refresh_usage"](_args())
    err = capsys.readouterr().err
    assert rc == 5
    assert "db locked" in err


def test_cmd_refresh_usage_quiet_suppresses_stdout(ns, monkeypatch, capsys):
    api_response = {"seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"}}
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    _stub_record_ok(monkeypatch, ns)
    _stub_cache_busted(monkeypatch, ns)
    rc = ns["cmd_refresh_usage"](_args(quiet=True))
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""


def test_cmd_refresh_usage_json_emits_schema(ns, monkeypatch, capsys):
    api_response = {"seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"}}
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    _stub_record_ok(monkeypatch, ns)
    _stub_cache_busted(monkeypatch, ns)
    rc = ns["cmd_refresh_usage"](_args(json=True))
    out = capsys.readouterr().out
    assert rc == 0
    parsed = _json.loads(out)
    assert parsed["schema_version"] == 1
    assert parsed["seven_day"]["used_percent"] == 13.0
    assert parsed["five_hour"] is None
    assert parsed["statusline_cache"] == "busted"


def test_refresh_usage_help_subprocess():
    """Smoke: --help must exit 0 and mention key flags."""
    script = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"
    proc = subprocess.run(
        ["python3", str(script), "refresh-usage", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--json" in proc.stdout
    assert "--quiet" in proc.stdout
    assert "--color" in proc.stdout
    assert "--timeout" in proc.stdout


def test_cmd_refresh_usage_nonnumeric_seven_day_utilization_returns_4(ns, monkeypatch, capsys):
    api_response = {
        "seven_day": {"utilization": "not-a-number", "resets_at": "2026-05-02T12:00:00Z"},
    }
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    rc = ns["cmd_refresh_usage"](_args())
    err = capsys.readouterr().err
    assert rc == 4
    assert "unparseable seven_day" in err


def test_cmd_refresh_usage_unparseable_seven_day_resets_at_returns_4(ns, monkeypatch, capsys):
    api_response = {
        "seven_day": {"utilization": 13.0, "resets_at": "not-iso-at-all"},
    }
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    rc = ns["cmd_refresh_usage"](_args())
    err = capsys.readouterr().err
    assert rc == 4
    assert "unparseable seven_day" in err


def test_cmd_refresh_usage_malformed_five_hour_silently_dropped(ns, monkeypatch, capsys):
    api_response = {
        "seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"},
        "five_hour": {"utilization": "garbage", "resets_at": "also-bad"},
    }
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    _stub_record_ok(monkeypatch, ns)
    _stub_cache_busted(monkeypatch, ns)
    rc = ns["cmd_refresh_usage"](_args(json=True))
    captured = capsys.readouterr()
    assert rc == 0
    parsed = _json.loads(captured.out)
    assert parsed["seven_day"]["used_percent"] == 13.0
    assert parsed["five_hour"] is None
    assert "ignoring unparseable five_hour" in captured.err


def test_cmd_refresh_usage_success_json_carries_freshness(ns, monkeypatch, capsys):
    api_response = {
        "seven_day": {"utilization": 13.0, "resets_at": "2026-05-02T12:00:00Z"},
    }
    _stub_token(monkeypatch, ns)
    _stub_fetch_ok(monkeypatch, ns, api_response)
    _stub_record_ok(monkeypatch, ns)
    _stub_cache_busted(monkeypatch, ns)
    monkeypatch.setitem(ns, "load_config", lambda: {})

    rc = ns["cmd_refresh_usage"](_args(json=True))
    out = capsys.readouterr().out
    assert rc == 0
    payload = _json.loads(out)
    assert payload["freshness"]["label"] == "fresh"
    assert payload["freshness"]["age_seconds"] == 0
    # captured_at_utc is the moment we fetched.
    assert payload["freshness"]["captured_at"] is not None


def test_cmd_refresh_usage_passes_user_agent_through(ns, monkeypatch, capsys):
    """End-to-end: cmd_refresh_usage builds a request with the resolved UA."""
    captured = {}
    class FakeResponse:
        def read(self):
            return b'{"seven_day":{"utilization":12.0,"resets_at":"2026-05-02T12:00:00Z"}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        return FakeResponse()
    monkeypatch.setattr(ns["urllib"].request, "urlopen", fake_urlopen)
    monkeypatch.setitem(ns, "_discover_cc_version", lambda: "2.1.116")
    monkeypatch.setitem(ns, "load_config", lambda: {})
    _stub_token(monkeypatch, ns)
    _stub_record_ok(monkeypatch, ns)
    _stub_cache_busted(monkeypatch, ns)

    rc = ns["cmd_refresh_usage"](_args())
    assert rc == 0
    assert captured["headers"].get("User-agent") == "claude-code/2.1.116"
