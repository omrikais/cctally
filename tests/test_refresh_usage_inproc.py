"""Unit tests for the in-process refresh-usage helper."""
import argparse
import json
import time

from conftest import load_script, redirect_paths


def _newest_source(ns):
    """The `source` of the most-recent weekly_usage_snapshots row, or None."""
    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT source FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row["source"] if row is not None else None


def _record_args(*, percent, resets_at, five_percent=None, five_resets_at=None):
    """Build the exact record-usage shape used by OAuth authority tests."""
    return argparse.Namespace(
        percent=percent,
        resets_at=str(resets_at),
        five_hour_percent=five_percent,
        five_hour_resets_at=(
            str(five_resets_at) if five_resets_at is not None else None
        ),
        source="api",
    )


def test_authoritative_equality_crash_leaves_seven_day_fail_closed(
        monkeypatch, tmp_path):
    """An equal OAuth write may deduplicate in SQLite, but a crash after it
    must leave a durable seven-day inflight tombstone.  The stale spool value
    then remains ineligible and cannot replay over the equal authority."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    now = int(time.time())
    resets_at = now + 3 * 86400
    seed = _record_args(percent=20.0, resets_at=resets_at)
    assert ns["cmd_record_usage"](seed) == 0

    ns["_atomic_write_json"](
        ns["STATUSLINE_CANDIDATE_DIR"] / ("a" * 64 + ".json"),
        {
            "schemaVersion": 1,
            "receivedAt": now,
            "sevenDay": {"percent": 21.0, "resetsAt": resets_at},
        },
    )
    monkeypatch.setitem(
        ns,
        "_after_authoritative_record",
        lambda: (_ for _ in ()).throw(RuntimeError("injected crash")),
    )

    result = ns["_authoritative_record_usage"](seed, {"sevenDay"})

    assert result.status == "record_failed"
    tombstone = json.loads(ns["STATUSLINE_AUTHORITATIVE_7D_PATH"].read_text())
    assert tombstone["state"] == "inflight"
    ns["_statusline_reduce_and_publish"]()
    conn = ns["open_db"]()
    try:
        newest = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert newest["weekly_percent"] == 20.0


def test_authoritative_seven_day_only_does_not_tombstone_five_hour(
        monkeypatch, tmp_path):
    """A 7d-only OAuth payload must not invalidate unrelated 5h work."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    now = int(time.time())

    result = ns["_authoritative_record_usage"](
        _record_args(percent=20.0, resets_at=now + 3 * 86400),
        {"sevenDay"},
    )

    assert result.status == "ok"
    seven = json.loads(ns["STATUSLINE_AUTHORITATIVE_7D_PATH"].read_text())
    assert seven["state"] == "committed"
    assert not ns["STATUSLINE_AUTHORITATIVE_5H_PATH"].exists()
    # The inclusive cutoff covers the entire accepted five-second future-skew
    # interval; a candidate admitted at exactly completion+5 cannot replay.
    ns["_atomic_write_json"](
        ns["STATUSLINE_CANDIDATE_DIR"] / ("b" * 64 + ".json"),
        {
            "schemaVersion": 1,
            "receivedAt": seven["blockReceivedAtThrough"],
            "sevenDay": {"percent": 21.0, "resetsAt": now + 3 * 86400},
        },
    )
    ns["_statusline_reduce_and_publish"]()
    conn = ns["open_db"]()
    try:
        newest = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert newest["weekly_percent"] == 20.0


def test_refresh_keeps_backoff_when_authoritative_record_fails(
        monkeypatch, tmp_path):
    """A successful fetch is not a successful authoritative observation.
    Keep the existing 429 state until the write-ahead/final protocol commits."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")
    monkeypatch.setitem(
        ns,
        "_fetch_oauth_usage",
        lambda token, timeout_seconds: {
            "seven_day": {
                "utilization": 20.0,
                "resets_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 86400)
                ),
            }
        },
    )
    monkeypatch.setitem(ns, "cmd_record_usage", lambda args: 7)
    ns["_oauth_backoff_register_429"](retry_after_deadline=None, now=time.time())
    assert ns["_oauth_backoff_count"]() == 1

    result = ns["_refresh_usage_inproc"]()

    assert result.status == "record_failed"
    assert ns["_oauth_backoff_count"]() == 1


def test_refresh_inproc_writes_source_api(monkeypatch, tmp_path):
    """The OAuth-fed record must be labeled source='api', not the previously
    hard-coded 'statusline'. Drives _refresh_usage_inproc with a mocked ok
    fetch and lets the REAL cmd_record_usage write the row, then reads it back.

    resets_at is a near-future epoch so cmd_record_usage's plausibility guard
    (weekly band [now-30d, now+8d]) accepts it and a row actually lands."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    now = int(time.time())
    seven_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3 * 86400)
    )
    fake_api = {"seven_day": {"utilization": 37.0, "resets_at": seven_iso}}
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: fake_api)
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: "absent")

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "ok"
    assert _newest_source(ns) == "api"


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


def test_refresh_inproc_five_hour_inactive_null_resets(monkeypatch, tmp_path):
    """Inactive 5h window: API returns `five_hour.resets_at: null` (key present,
    value null). Must NOT raise AttributeError from _iso_to_epoch(None) — the 5h
    segment is dropped cleanly and the 7d data still records. Regression for the
    'NoneType object has no attribute strip' crash on `cctally refresh-usage`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "tok")

    captured = {}
    fake_api = {
        "seven_day": {"utilization": 42.5, "resets_at": "2026-05-10T00:00:00Z"},
        "five_hour": {"utilization": 0, "resets_at": None},
    }
    monkeypatch.setitem(ns, "_fetch_oauth_usage",
                        lambda token, timeout_seconds: fake_api)
    monkeypatch.setitem(ns, "cmd_record_usage",
                        lambda args: captured.update(vars(args)) or 0)
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: None)

    result = ns["_refresh_usage_inproc"]()
    assert result.status == "ok"
    assert result.payload["five_hour"] is None
    assert result.payload["seven_day"]["used_percent"] == 42.5
    # 5h is dropped at the source, so record-usage gets no 5h fields.
    assert captured["five_hour_percent"] is None
    assert captured["five_hour_resets_at"] is None


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
