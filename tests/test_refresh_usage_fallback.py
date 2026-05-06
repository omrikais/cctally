"""Fallback contract tests for cmd_refresh_usage 429 path."""
import datetime as dt
import sqlite3
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def ns_with_paths(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_select_last_known_returns_none_when_table_empty(ns_with_paths):
    fn = ns_with_paths["_select_last_known_snapshot"]
    # Open + immediately close to create the schema.
    conn = ns_with_paths["open_db"]()
    conn.close()
    assert fn() is None


def test_select_last_known_returns_newest(ns_with_paths):
    """Schema (verified): captured_at_utc, week_start_date, week_end_date,
    week_start_at, week_end_at, weekly_percent, page_url, source,
    payload_json, five_hour_percent, five_hour_resets_at,
    five_hour_window_key. The 7-day reset_at is `week_end_at` (the
    OAuth seven_day.resets_at value cmd_record_usage stores there)."""
    ns = ns_with_paths
    conn = ns["open_db"]()
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        "week_end_at, weekly_percent, source, payload_json, "
        "five_hour_percent, five_hour_resets_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-30T11:00:00+00:00", "2026-04-26", "2026-05-02",
         "2026-04-26T12:00:00+00:00", "2026-05-02T12:00:00+00:00",
         50.0, "test", "{}", 3.0, "2026-04-30T15:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        "week_end_at, weekly_percent, source, payload_json, "
        "five_hour_percent, five_hour_resets_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-30T12:00:00+00:00", "2026-04-26", "2026-05-02",
         "2026-04-26T12:00:00+00:00", "2026-05-02T12:00:00+00:00",
         57.0, "test", "{}", 5.0, "2026-04-30T17:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    snap = ns["_select_last_known_snapshot"]()
    assert snap is not None
    assert snap["seven_day"]["used_percent"] == 57.0
    assert snap["five_hour"]["used_percent"] == 5.0
    assert snap["captured_at_utc"] == "2026-04-30T12:00:00+00:00"
    assert snap["source"] == "db-fallback"


import argparse
import json as _json


def test_cmd_refresh_usage_429_with_prior_snapshot_exits_zero(ns_with_paths, monkeypatch, capsys):
    ns = ns_with_paths
    conn = ns["open_db"]()
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        "week_end_at, weekly_percent, source, payload_json, "
        "five_hour_percent, five_hour_resets_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-30T11:59:30+00:00", "2026-04-26", "2026-05-02",
         "2026-04-26T12:00:00+00:00", "2026-05-02T12:00:00+00:00",
         57.0, "test", "{}", 5.0, "2026-04-30T17:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "load_config", lambda: {})
    def boom(token, timeout_seconds):
        raise ns["RefreshUsageRateLimitError"]("HTTP 429")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    args = argparse.Namespace(json=True, quiet=False, color="never", timeout=5.0)
    rc = ns["cmd_refresh_usage"](args)
    out = capsys.readouterr()

    assert rc == 0
    payload = _json.loads(out.out)
    assert payload["status"] == "rate_limited"
    assert payload["fallback"]["seven_day"]["used_percent"] == 57.0
    assert payload["fallback"]["source"] == "db-fallback"
    assert payload["freshness"]["label"] in ("fresh", "aging", "stale")
    assert "rate-limited" in out.err.lower()


def test_cmd_refresh_usage_429_without_prior_snapshot_exits_zero(ns_with_paths, monkeypatch, capsys):
    ns = ns_with_paths
    # Open db but insert nothing.
    conn = ns["open_db"]()
    conn.close()

    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "load_config", lambda: {})
    def boom(token, timeout_seconds):
        raise ns["RefreshUsageRateLimitError"]("HTTP 429")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    args = argparse.Namespace(json=True, quiet=False, color="never", timeout=5.0)
    rc = ns["cmd_refresh_usage"](args)
    out = capsys.readouterr()

    assert rc == 0
    payload = _json.loads(out.out)
    assert payload["status"] == "rate_limited"
    assert payload["fallback"] is None
    assert payload["freshness"] is None
    assert payload["reason"] == "no prior snapshot"
    assert "no last-known data" in out.err.lower()


def test_cmd_refresh_usage_429_text_mode_null_week_end_at(ns_with_paths, monkeypatch, capsys):
    """Text-mode 429 fallback must not crash when prior snapshot has NULL
    week_end_at (pre-migration row, or any row where the 7d resets_at
    boundary is unknown). Renderer should drop only the (in TTL) portion,
    keep the percent + db-fallback tag."""
    ns = ns_with_paths
    conn = ns["open_db"]()
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        "week_end_at, weekly_percent, source, payload_json, "
        "five_hour_percent, five_hour_resets_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-04-30T11:59:30+00:00", "2026-04-26", "2026-05-02",
         None, None,
         57.0, "test", "{}", None, None),
    )
    conn.commit()
    conn.close()

    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "load_config", lambda: {})
    def boom(token, timeout_seconds):
        raise ns["RefreshUsageRateLimitError"]("HTTP 429")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    args = argparse.Namespace(json=False, quiet=False, color="never", timeout=5.0)
    rc = ns["cmd_refresh_usage"](args)
    out = capsys.readouterr()

    assert rc == 0
    assert "7d 57%" in out.out
    assert "(in " not in out.out
    assert "[src:db-fallback" in out.out
    assert "cache:absent" in out.out
    assert "rate-limited" in out.err.lower()


def test_cmd_refresh_usage_500_still_exits_three(ns_with_paths, monkeypatch, capsys):
    """Non-429 network errors keep the existing exit 3 behavior."""
    ns = ns_with_paths
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **kw: "tok")
    monkeypatch.setitem(ns, "load_config", lambda: {})
    def boom(token, timeout_seconds):
        raise ns["RefreshUsageNetworkError"]("HTTP 500 Server Error")
    monkeypatch.setitem(ns, "_fetch_oauth_usage", boom)

    args = argparse.Namespace(json=False, quiet=False, color="never", timeout=5.0)
    rc = ns["cmd_refresh_usage"](args)
    assert rc == 3
