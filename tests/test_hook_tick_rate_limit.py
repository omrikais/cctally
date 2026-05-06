"""Hook-tick throttle, skip-if-fresh, and 429 fallback tests."""
import pytest
from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


def test_hook_tick_throttle_default_is_15s(ns, monkeypatch):
    """Default throttle reads from oauth_usage config = 15s."""
    monkeypatch.setitem(ns, "load_config", lambda: {})
    cfg = ns["_get_oauth_usage_config"](ns["load_config"]())
    assert cfg["throttle_seconds"] == 15


def test_hook_tick_throttle_respects_override(ns, monkeypatch):
    monkeypatch.setitem(
        ns, "load_config",
        lambda: {"oauth_usage": {"throttle_seconds": 60}},
    )
    cfg = ns["_get_oauth_usage_config"](ns["load_config"]())
    assert cfg["throttle_seconds"] == 60


def test_hook_tick_skips_when_snapshot_within_throttle(ns, monkeypatch, tmp_path):
    """Snapshot 5s old + throttle 15s -> skipped, no fetch attempted.

    Mocks _newest_snapshot_age_seconds directly to avoid time-mocking;
    age is the only signal _hook_tick_oauth_refresh consults from the
    DB-side helper, so this isolates the skip-if-fresh logic cleanly.
    """
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: 5.0)

    fetch_called = {"n": 0}
    def boom(token, timeout_seconds):
        fetch_called["n"] += 1
        raise AssertionError("must not be called when fresh")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    status, payload = ns["_hook_tick_oauth_refresh"]()
    assert status.startswith("skipped(fresh:")
    assert payload is None
    assert fetch_called["n"] == 0


def test_hook_tick_fetches_when_snapshot_older_than_throttle(ns, monkeypatch, tmp_path):
    """Snapshot 30s old + throttle 15s -> fetch attempted."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: 30.0)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)

    api = {
        "seven_day": {"utilization": 22.0, "resets_at": "2026-05-02T12:00:00Z"},
    }
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: api)

    status, payload = ns["_hook_tick_oauth_refresh"]()
    assert status.startswith("ok(7d=22)")
    assert payload == api


def test_hook_tick_fetches_when_no_snapshot_exists(ns, monkeypatch, tmp_path):
    """_newest_snapshot_age_seconds returning None -> fetch attempted
    (no skip, since we have no fresh data to gate on)."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: None)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)

    api = {"seven_day": {"utilization": 1.0, "resets_at": "2026-05-02T12:00:00Z"}}
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: api)

    status, payload = ns["_hook_tick_oauth_refresh"]()
    assert status.startswith("ok(")
    assert payload == api


def test_hook_tick_handles_rate_limit_gracefully(ns, monkeypatch, tmp_path):
    """RefreshUsageRateLimitError -> err(rate-limit), no DB write, no exception."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")

    record_called = {"n": 0}
    def fake_record(args):
        record_called["n"] += 1
        return 0
    monkeypatch.setitem(ns, "cmd_record_usage", fake_record)

    def boom(token, timeout_seconds):
        raise ns["RefreshUsageRateLimitError"]("HTTP 429 Too Many Requests")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    status, payload = ns["_hook_tick_oauth_refresh"]()
    assert status == "err(rate-limit)"
    assert payload is None
    assert record_called["n"] == 0
