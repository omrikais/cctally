"""Retry-After parsing + shared 429 backoff state (spec 2026-07-17 §4).

The OAuth poll must never re-trip an anti-abuse ban: on a 429 it parses
`Retry-After` (delta-seconds AND HTTP-date) and sets a shared ABSOLUTE
backoff deadline; when the header is absent it uses conservative
exponential backoff (base * 2**consecutive_429, capped). Any successful
API response clears the deadline and resets the counter.
"""
import datetime as dt
import email.utils
import time

import pytest

from conftest import load_script, redirect_paths


def _load(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


# --- _parse_retry_after ----------------------------------------------------


def test_parse_retry_after_delta(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    assert ns["_parse_retry_after"]("120", 1000.0) == 1120.0


def test_parse_retry_after_httpdate(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    header = "Wed, 21 Oct 2026 07:28:00 GMT"
    expected = email.utils.parsedate_to_datetime(header).timestamp()
    got = ns["_parse_retry_after"](header, 0.0)
    assert got is not None
    assert abs(got - expected) < 1.0


def test_parse_retry_after_absent_or_bad(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    assert ns["_parse_retry_after"](None, 1000.0) is None
    assert ns["_parse_retry_after"]("", 1000.0) is None
    assert ns["_parse_retry_after"]("garbage", 1000.0) is None
    # Negative delta is not a valid Retry-After.
    assert ns["_parse_retry_after"]("-5", 1000.0) is None


def test_rate_limit_error_carries_deadline(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    exc = ns["RefreshUsageRateLimitError"]("HTTP 429", retry_after_deadline=1234.0)
    assert exc.retry_after_deadline == 1234.0
    # Backward-compat: message-only construction still defaults to None.
    assert ns["RefreshUsageRateLimitError"]("x").retry_after_deadline is None


# --- register / reset helpers ----------------------------------------------


def test_register_429_with_header_uses_deadline(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    now = time.time()
    d = ns["_oauth_backoff_register_429"](retry_after_deadline=now + 200, now=now)
    assert abs(d - (now + 200)) < 1.0
    assert 190 < ns["_oauth_backoff_remaining_seconds"]() <= 200


def test_register_429_headerless_exponential_and_cap(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    base = ns["OAUTH_BACKOFF_BASE_SECONDS"]
    cap = ns["OAUTH_BACKOFF_CAP_SECONDS"]
    now = time.time()

    d0 = ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=now)
    d1 = ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=now)
    d2 = ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=now)
    assert d0 - now == pytest.approx(base)          # 2**0
    assert d1 - now == pytest.approx(base * 2)       # 2**1
    assert d2 - now == pytest.approx(base * 4)       # 2**2

    # Enough further 429s to blow past the cap → clamped, never shortened.
    dlast = d2
    for _ in range(20):
        dlast = ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=now)
    assert dlast - now == pytest.approx(cap)


def test_reset_clears_deadline_and_counter(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    now = time.time()
    ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=now)
    ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=now)
    assert ns["_oauth_backoff_count"]() == 2
    assert ns["_oauth_backoff_remaining_seconds"]() > 0

    ns["_oauth_backoff_reset"]()
    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0
    assert ns["_oauth_backoff_count"]() == 0


# --- integration via _hook_tick_oauth_refresh ------------------------------


def _prime_hook(ns, monkeypatch):
    monkeypatch.setitem(ns, "load_config", lambda: {})
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    # No fresh snapshot → the pre-existing freshness gate does not skip.
    monkeypatch.setitem(ns, "_newest_snapshot_age_seconds", lambda: None)


def test_429_with_header_via_hook_sets_deadline(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)
    err = ns["RefreshUsageRateLimitError"](
        "HTTP 429", retry_after_deadline=time.time() + 250
    )

    def boom(token, timeout_seconds):
        raise err
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    status, payload = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status == "err(rate-limit)"
    assert payload is None
    assert ns["_oauth_backoff_remaining_seconds"]() > 200


def test_429_headerless_via_hook_uses_base(monkeypatch, tmp_path):
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)

    def boom(token, timeout_seconds):
        raise ns["RefreshUsageRateLimitError"]("HTTP 429")  # no Retry-After
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0
    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status == "err(rate-limit)"
    rem = ns["_oauth_backoff_remaining_seconds"]()
    base = ns["OAUTH_BACKOFF_BASE_SECONDS"]
    assert base - 2 < rem <= base + 1  # first 429 (count 0) → ~BASE


def test_ok_via_hook_resets_backoff_state(monkeypatch, tmp_path):
    """A successful API response clears the deadline AND the counter. Uses an
    ALREADY-EXPIRED backoff (remaining 0) so the poll is allowed to run even
    once Task 5's backoff gate is in place; the counter being reset to 0 is
    what proves the success path called the reset."""
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)

    # A prior 429 streak that has since expired: deadline in the past, count>0.
    ns["_oauth_backoff_register_429"](
        retry_after_deadline=time.time() - 10, now=time.time() - 70
    )
    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0
    assert ns["_oauth_backoff_count"]() == 1

    api = {"seven_day": {"utilization": 22.0, "resets_at": "2026-05-02T12:00:00Z"}}
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: api)

    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status.startswith("ok(")
    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0
    assert ns["_oauth_backoff_count"]() == 0  # counter reset by the success


def test_hook_record_failure_keeps_backoff_state(monkeypatch, tmp_path):
    """Automatic OAuth must not clear a prior 429 streak until the complete
    authoritative writer protocol succeeds, rather than merely after fetch."""
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 7)
    ns["_oauth_backoff_register_429"](
        retry_after_deadline=time.time() - 10, now=time.time() - 70
    )
    assert ns["_oauth_backoff_count"]() == 1
    monkeypatch.setitem(
        ns,
        "_fetch_oauth_usage",
        lambda token, timeout_seconds: {
            "seven_day": {
                "utilization": 22.0,
                "resets_at": "2026-05-02T12:00:00Z",
            }
        },
    )

    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)

    assert status == "err(record-usage=7)"
    assert ns["_oauth_backoff_count"]() == 1


def test_hook_rechecks_selected_freshness_after_lock(monkeypatch, tmp_path):
    """A publisher that updates selected freshness while this tick waits for
    the writer lock suppresses the OAuth request after acquisition."""
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)

    class _LockThatPublishes:
        def __enter__(self):
            ns["_statusline_observe_touch"]()
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setitem(ns, "_selected_state_lock", _LockThatPublishes)
    monkeypatch.setitem(
        ns,
        "_fetch_oauth_usage",
        lambda token, timeout_seconds: (_ for _ in ()).throw(
            AssertionError("must recheck freshness after the lock")
        ),
    )

    status, payload = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)

    assert status.startswith("skipped(statusline-fresh")
    assert payload is None


# --- backfill gate: observation marker + backoff (Task 5, spec §4) ---------


def _ok_api():
    return {"seven_day": {"utilization": 5.0, "resets_at": "2026-05-02T12:00:00Z"}}


def test_backfill_skips_when_statusline_fresh(monkeypatch, tmp_path):
    """A recently-fed observation marker means the statusline is the live
    writer — the OAuth poll must NOT fetch (demoted to backfill)."""
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)

    called = {"n": 0}

    def boom(token, timeout_seconds):
        called["n"] += 1
        raise AssertionError("must not fetch while the statusline is fresh")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    ns["_statusline_observe_touch"]()  # marker fresh
    status, payload = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status.startswith("skipped(statusline-fresh")
    assert payload is None
    assert called["n"] == 0


def test_backfill_skips_within_backoff(monkeypatch, tmp_path):
    """A pending 429 cooldown suppresses the automatic poll (no marker, so
    the statusline-fresh gate passes and the backoff gate fires)."""
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)

    called = {"n": 0}

    def boom(token, timeout_seconds):
        called["n"] += 1
        raise AssertionError("must not fetch while a backoff is pending")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    ns["_oauth_backoff_set"](time.time() + 300)
    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status.startswith("skipped(backoff")
    assert called["n"] == 0


def test_backfill_attempts_when_statusline_stale(monkeypatch, tmp_path):
    """When the statusline fed a while ago (marker aged beyond the backfill
    window) the poll backfills."""
    import os as _os
    import _cctally_core

    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)

    called = {"n": 0}

    def ok(token, timeout_seconds):
        called["n"] += 1
        return _ok_api()
    monkeypatch.setitem(ns, "_fetch_oauth_usage", ok)

    ns["_statusline_observe_touch"]()
    old = time.time() - (float(ns["OAUTH_BACKFILL_STALE_SECONDS"]) + 60)
    _os.utime(_cctally_core.STATUSLINE_OBSERVE_MARKER_PATH, (old, old))

    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status.startswith("ok(")
    assert called["n"] == 1


def test_backfill_attempts_when_marker_absent(monkeypatch, tmp_path):
    """No observation marker at all → infinitely stale → backfill proceeds."""
    ns = _load(monkeypatch, tmp_path)
    _prime_hook(ns, monkeypatch)
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)

    called = {"n": 0}

    def ok(token, timeout_seconds):
        called["n"] += 1
        return _ok_api()
    monkeypatch.setitem(ns, "_fetch_oauth_usage", ok)

    status, _ = ns["_hook_tick_oauth_refresh"](throttle_seconds=0)
    assert status.startswith("ok(")
    assert called["n"] == 1


def test_force_refresh_429_advances_shared_deadline(monkeypatch, tmp_path):
    """Force-refresh bypasses the gates, but a resulting 429 MUST advance the
    shared backoff deadline so automatic polling still honors it."""
    ns = _load(monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")
    err = ns["RefreshUsageRateLimitError"](
        "429", retry_after_deadline=time.time() + 150
    )

    def boom(token, timeout_seconds):
        raise err
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0
    result = ns["_refresh_usage_inproc"]()
    assert result.status == "rate_limited"
    assert result.fallback is True
    assert ns["_oauth_backoff_remaining_seconds"]() > 100


def test_force_refresh_bypasses_backoff_gate_and_clears_on_ok(monkeypatch, tmp_path):
    """A user-initiated force-refresh fetches even while a backoff is pending
    (bypasses the gate); a successful response clears the shared deadline."""
    ns = _load(monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 0)
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: "absent")

    ns["_oauth_backoff_set"](time.time() + 300)  # pending cooldown

    called = {"n": 0}

    def ok(token, timeout_seconds):
        called["n"] += 1
        return _ok_api()
    monkeypatch.setitem(ns, "_fetch_oauth_usage", ok)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "ok"
    assert called["n"] == 1  # fetched despite the pending backoff
    assert ns["_oauth_backoff_remaining_seconds"]() == 0.0  # success cleared it
