"""Hook-tick throttle, skip-if-fresh, and 429 fallback tests."""
import argparse
import datetime as dt
import fcntl
import io
import os
import sys
import threading
import time
import types

import pytest
from conftest import load_script

# #948: the OAuth refresh layer checks the plausibility band
# [now-30d, now+8d] before `cmd_record_usage` (stubbed here) is called, so a
# fixed reset date goes stale; keep the weekly reset three days ahead of now.
_RESETS_AT = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).replace(
    minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args, **kw: 0)

    api = {
        "seven_day": {"utilization": 22.0, "resets_at": _RESETS_AT},
    }
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: api)

    status, payload = ns["_hook_tick_oauth_refresh"]()
    assert status.startswith("ok(7d=22)")
    assert payload == api


def test_hook_tick_five_hour_inactive_null_resets(ns, monkeypatch, tmp_path):
    """Inactive 5h window (`five_hour.resets_at: null`) must not crash the hook
    OAuth path with AttributeError from _iso_to_epoch(None); 5h is dropped and
    the 7d record still lands. Parallel to the _refresh_usage_inproc regression."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: 30.0)

    captured = {}
    monkeypatch.setitem(ns, "cmd_record_usage",
                        lambda args, **kw: captured.update(vars(args)) or 0)

    api = {
        "seven_day": {"utilization": 22.0, "resets_at": _RESETS_AT},
        "five_hour": {"utilization": 0, "resets_at": None},
    }
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: api)

    status, payload = ns["_hook_tick_oauth_refresh"]()
    assert status == "ok(7d=22)"
    assert captured["five_hour_percent"] is None
    assert captured["five_hour_resets_at"] is None


def test_hook_tick_fetches_when_no_snapshot_exists(ns, monkeypatch, tmp_path):
    """_newest_snapshot_age_seconds returning None -> fetch attempted
    (no skip, since we have no fresh data to gate on)."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: None)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args, **kw: 0)

    api = {"seven_day": {"utilization": 1.0, "resets_at": _RESETS_AT}}
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


def test_hook_tick_rate_limit_arms_backoff(ns, monkeypatch, tmp_path):
    """A 429 must arm the shared backoff deadline so the next tick is
    suppressed rather than immediately re-polling (spec §4)."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: None)

    def boom(token, timeout_seconds):
        raise ns["RefreshUsageRateLimitError"]("HTTP 429 Too Many Requests")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0
    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status == "err(rate-limit)"
    assert ns["_oauth_backoff_remaining_seconds"]() > 0


def _run_hook_tick(ns, args, timeout):
    """Run `cmd_hook_tick` on a daemon thread; return its exit code, or
    "blocked" when it has not finished within ``timeout`` (the convoy)."""
    result = {}

    def body():
        result["rc"] = ns["cmd_hook_tick"](args)

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    worker.join(timeout)
    return result.get("rc", "blocked")


def test_throttle_lock_contention_skips_without_fetch(
        ns, monkeypatch, tmp_path, capsys):
    """#1064 spec section 7 test 6. Detects the hook-tick convoy: a Claude
    hook-tick blocked on `hook-tick.last-fetch.lock` behind another holder
    (one held it for up to an hour behind a reducer pass, with 18 hooks
    queued). While another process holds the lock, a `--foreground` run (via
    `--mock-oauth-response`) and an `--explain` run each finish, log
    `oauth=throttled(inflight)`, fetch nothing and leave the throttle marker
    untouched; `--explain` exits 0 and names the contention as its decision.
    Once the lock is free, the next run fetches exactly once."""
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "load_config", lambda: {})
    fetches = []

    def counted(timeout_seconds=5.0, throttle_seconds=None):
        fetches.append(throttle_seconds)
        return "ok(7d=22)", {"seven_day": {"utilization": 22.0}}

    monkeypatch.setitem(ns, "_hook_tick_oauth_refresh", counted)
    monkeypatch.setitem(ns, "_hook_tick_make_mock_refresh", lambda payload: counted)
    app_dir = ns["APP_DIR"]
    app_dir.mkdir(parents=True, exist_ok=True)
    lock_path = app_dir / "hook-tick.last-fetch.lock"
    marker = app_dir / "hook-tick.last-fetch"
    marker.touch()
    old = time.time() - 3600
    os.utime(marker, (old, old))
    log = app_dir / "logs" / "hook-tick.log"

    def args(**extra):
        values = dict(source="claude", explain=False, foreground=True,
                      no_oauth=False, throttle_seconds=15, event=None,
                      mock_oauth_response=None)
        values.update(extra)
        return argparse.Namespace(**values)

    def stdin():
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(
            b'{"hook_event_name": "Stop", "session_id": "s", "cwd": "/x"}')))

    # Another holder of the lock: a second open file description in this
    # process conflicts with the hook's own `flock` exactly as another
    # process's would (flock locks belong to the open file description).
    holder = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        stdin()
        # timing-budget: a skipped OAuth leg leaves only an empty local sync
        assert _run_hook_tick(ns, args(mock_oauth_response='{"seven_day": {}}'),
                              30.0) == 0, "the --foreground run queued on the lock"
        capsys.readouterr()
        assert _run_hook_tick(ns, args(explain=True, foreground=False), 30.0) == 0, (
            "the --explain run queued on the lock or did not exit 0")
        explained = capsys.readouterr().out
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert fetches == []
    assert os.stat(marker).st_mtime == old
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and all("oauth=throttled(inflight)" in l for l in lines), lines
    assert "→ threshold: 15s → throttled(inflight)" in explained, explained
    assert "→ status: throttled(inflight)" in explained, explained

    stdin()
    assert _run_hook_tick(ns, args(), 30.0) == 0
    assert len(fetches) == 1, "the run after the holder released did not fetch"
    assert os.stat(marker).st_mtime > old
    assert "oauth=ok(7d=22)" in log.read_text().splitlines()[-1]
