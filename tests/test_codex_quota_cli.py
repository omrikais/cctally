"""End-to-end contracts for the canonical nested Codex quota CLI."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"
RESET = "2026-07-15T15:00:00Z"


def _iso(hour: int, minute: int = 0) -> str:
    return dt.datetime(2026, 7, 15, hour, minute, tzinfo=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _seed_quota(conn: sqlite3.Connection, *, root_key: str, source_path: str,
                captures: list[tuple[str, int, float]],
                limit_key: str = "limit-primary", slot: str = "primary",
                window_minutes: int = 330, reset_at: str = RESET) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO codex_source_roots
           (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
           VALUES (?, ?, ?, ?)""",
        (root_key, f"/codex/{root_key}", _iso(9), _iso(12)),
    )
    conn.executemany(
        """INSERT INTO quota_window_snapshots
           (source, source_root_key, source_path, line_offset,
            captured_at_utc, observed_slot, logical_limit_key, limit_id,
            limit_name, window_minutes, used_percent, resets_at_utc,
            plan_type, individual_limit_json, reached_type)
           VALUES ('codex', ?, ?, ?, ?, ?, ?, 'native-primary', 'Primary',
                   ?, ?, ?, 'pro', NULL, NULL)""",
        [
            (root_key, source_path, offset, captured, slot, limit_key,
             window_minutes, used_percent, reset_at)
            for captured, offset, used_percent in captures
        ],
    )


@pytest.fixture
def quota_home(tmp_path, monkeypatch):
    """Seed two same-looking roots plus current/future physical captures."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["open_cache_db"]()
    try:
        _seed_quota(
            cache, root_key="root-a", source_path="/codex/root-a/rollout.jsonl",
            captures=[(_iso(9), 10, 10.0), (_iso(10), 20, 12.4), (_iso(11), 30, 16.1)],
        )
        _seed_quota(
            cache, root_key="root-b", source_path="/codex/root-b/rollout.jsonl",
            captures=[(_iso(9), 10, 10.0), (_iso(10), 20, 12.4)],
        )
        # A second logical window makes exact (not prefix) selector behavior
        # observable without ever combining independent percentages.
        _seed_quota(
            cache, root_key="root-a", source_path="/codex/root-a/rollout.jsonl",
            captures=[(_iso(9), 40, 40.0), (_iso(10), 50, 41.0)],
            limit_key="limit-secondary", slot="secondary", window_minutes=60,
        )
        cache.executemany(
            """INSERT INTO codex_session_entries
               (source_path, line_offset, timestamp_utc, session_id, model,
                input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens, source_root_key)
               VALUES (?, ?, ?, 'session', 'gpt-5', ?, ?, ?, ?, ?, ?)""",
            [
                ("/codex/root-a/rollout.jsonl", 20, _iso(10), 1000, 0, 100, 0, 1100, "root-a"),
                ("/codex/root-a/rollout.jsonl", 30, _iso(11), 2000, 0, 200, 0, 2200, "root-a"),
            ],
        )
        cache.commit()
    finally:
        cache.close()
    config = tmp_path / ".local" / "share" / "cctally" / "config.json"
    config.write_text(json.dumps({"display": {"tz": "America/New_York"}}))
    return tmp_path


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        "TZ": "Etc/UTC",
        "PATH": "/usr/bin:/bin",
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
    }
    return subprocess.run(
        [sys.executable, str(BIN), "codex", "quota", *args],
        text=True, capture_output=True, env=env,
    )


def _json(home: Path, *args: str) -> dict:
    result = _run(home, *args, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _run_percent_breakdown(
    home: Path, source: str, *args: str,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(BIN)]
    if source == "codex":
        command.extend(("codex", "percent-breakdown"))
    else:
        command.append("percent-breakdown")
    env = {
        "HOME": str(home),
        "TZ": "Etc/UTC",
        "PATH": "/usr/bin:/bin",
        "NO_COLOR": "1",
        "CCTALLY_AS_OF": _iso(12),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
    }
    return subprocess.run(command + list(args), text=True, capture_output=True, env=env)


@pytest.fixture
def percent_breakdown_home(tmp_path, monkeypatch):
    """Seed one native 7-day cycle plus its correlated five-hour evidence."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    cache = ns["open_cache_db"]()
    try:
        _seed_quota(
            cache,
            root_key="root-weekly",
            source_path="/codex/root-weekly/weekly.jsonl",
            captures=[(_iso(9), 10, 10.0), (_iso(10), 20, 12.4), (_iso(11), 30, 16.1)],
            limit_key="limit-weekly",
            slot="primary",
            window_minutes=10_080,
        )
        # The provider's reset timestamp can jitter by seconds across captures
        # of one physical cycle.  The current-cycle convenience command must
        # follow the latest fresh baseline (as the dashboard hero does), not
        # treat every still-future jitter block as a separate active cycle.
        _seed_quota(
            cache,
            root_key="root-weekly",
            source_path="/codex/root-weekly/weekly-jitter.jsonl",
            captures=[(_iso(8), 5, 9.0), (_iso(9, 30), 15, 10.0)],
            limit_key="limit-weekly",
            slot="primary",
            window_minutes=10_080,
            reset_at="2026-07-15T15:00:01Z",
        )
        _seed_quota(
            cache,
            root_key="root-weekly",
            source_path="/codex/root-weekly/five-hour.jsonl",
            captures=[(_iso(9), 110, 20.0), (_iso(10), 120, 25.0), (_iso(11), 130, 30.0)],
            limit_key="limit-five-hour",
            slot="primary",
            window_minutes=300,
        )
        _seed_quota(
            cache,
            root_key="root-inactive",
            source_path="/codex/root-inactive/weekly.jsonl",
            captures=[(_iso(9), 10, 5.0), (_iso(10), 20, 6.0)],
            limit_key="limit-inactive",
            slot="primary",
            window_minutes=10_080,
            reset_at=_iso(11, 30),
        )
        cache.executemany(
            """INSERT INTO codex_session_entries
               (source_path, line_offset, timestamp_utc, session_id, model,
                input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens, source_root_key)
               VALUES (?, ?, ?, 'session', 'gpt-5', ?, ?, ?, ?, ?, ?)""",
            [
                ("/codex/root-weekly/weekly.jsonl", 20, _iso(10),
                 1000, 0, 100, 0, 1100, "root-weekly"),
                ("/codex/root-weekly/weekly.jsonl", 30, _iso(11),
                 2000, 0, 200, 0, 2200, "root-weekly"),
            ],
        )
        cache.commit()
    finally:
        cache.close()
    ns["reconcile_codex_quota_projection"](
        now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC),
    )
    config = tmp_path / ".local" / "share" / "cctally" / "config.json"
    config.write_text(json.dumps({"display": {"tz": "utc"}}))
    return tmp_path


def test_codex_percent_breakdown_matches_claude_visual_design_byte_for_byte(
    percent_breakdown_home,
):
    flags = (
        "--root-key", "root-weekly", "--limit-key", "limit-weekly",
        "--speed", "standard", "--tz", "utc",
    )
    codex_json = _run_percent_breakdown(
        percent_breakdown_home, "codex", *flags, "--json",
    )
    assert codex_json.returncode == 0, codex_json.stderr
    payload = json.loads(codex_json.stdout)
    assert list(payload)[0] == "schemaVersion"
    assert payload["source"] == "codex"
    assert [row["percentThreshold"] for row in payload["milestones"]] == list(range(11, 17))
    assert {row["fiveHourPercentAtCrossing"] for row in payload["milestones"]} == {25.0, 30.0}

    ns = load_script()
    stats = ns["open_db"]()
    try:
        stats.execute(
            """INSERT INTO weekly_usage_snapshots
               (captured_at_utc, week_start_date, week_end_date,
                week_start_at, week_end_at, weekly_percent,
                page_url, source, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, NULL, 'fixture', '{}')""",
            (
                _iso(12), payload["weekStartDate"], payload["weekEndDate"],
                payload["weekStartAt"], payload["weekEndAt"], 16.1,
            ),
        )
        stats.executemany(
            """INSERT INTO percent_milestones
               (captured_at_utc, week_start_date, week_end_date,
                week_start_at, week_end_at, percent_threshold,
                cumulative_cost_usd, marginal_cost_usd,
                usage_snapshot_id, cost_snapshot_id,
                five_hour_percent_at_crossing, reset_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, 0)""",
            [
                (
                    row["capturedAt"], payload["weekStartDate"], payload["weekEndDate"],
                    payload["weekStartAt"], payload["weekEndAt"],
                    row["percentThreshold"], row["cumulativeCostUSD"],
                    row["marginalCostUSD"], row["fiveHourPercentAtCrossing"],
                )
                for row in payload["milestones"]
            ],
        )
        stats.commit()
    finally:
        stats.close()

    claude = _run_percent_breakdown(
        percent_breakdown_home, "claude",
        "--week-start", payload["weekStartDate"], "--tz", "utc",
    )
    codex = _run_percent_breakdown(percent_breakdown_home, "codex", *flags)
    assert claude.returncode == 0, claude.stderr
    assert codex.returncode == 0, codex.stderr
    assert codex.stdout == claude.stdout


def test_codex_percent_breakdown_auto_selects_the_only_active_weekly_identity(
    percent_breakdown_home,
):
    result = _run_percent_breakdown(
        percent_breakdown_home, "codex", "--speed", "standard", "--json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["identity"]["sourceRootKey"] == "root-weekly"
    assert payload["identity"]["logicalLimitKey"] == "limit-weekly"


def test_all_five_canonical_nested_leaves_parse_and_stamp_json(quota_home):
    commands = {
        "history": ("--no-sync", "--since", "2026-07-15"),
        "statusline": ("--no-sync", "--as-of", _iso(12)),
        "forecast": ("--no-sync", "--as-of", _iso(12)),
        "blocks": ("--no-sync", "--since", "2026-07-15"),
        "breakdown": (
            "--no-sync", "--root-key", "root-a", "--limit-key", "limit-primary",
            "--reset-at", "2026-07-15T15:00:00+00:00", "--speed", "standard",
        ),
    }
    for command, flags in commands.items():
        payload = _json(quota_home, command, *flags)
        assert list(payload)[0] == "schemaVersion"
        assert payload["schemaVersion"] == 1
        assert payload["source"] == "codex"
        assert payload["freshnessSource"] == "local-rollout"


def test_history_uses_exact_selectors_and_display_timezone_date_range(quota_home):
    payload = _json(
        quota_home, "history", "--no-sync", "--root-key", "root-a",
        "--limit-key", "limit-primary", "--since", "2026-07-15",
        "--until", "2026-07-16",
    )
    assert len(payload["windows"]) == 1
    window = payload["windows"][0]
    assert window["identity"]["sourceRootKey"] == "root-a"
    assert window["identity"]["logicalLimitKey"] == "limit-primary"
    assert window["identity"]["windowMinutes"] == 330
    assert window["observations"][-1]["sourcePathKey"] != "/codex/root-a/rollout.jsonl"

    missing = _run(
        quota_home, "history", "--no-sync", "--root-key", "root",
        "--limit-key", "limit-primary",
    )
    assert missing.returncode == 2
    assert "root-a" in missing.stderr and "limit-primary" in missing.stderr


def test_statusline_and_forecast_keep_future_capture_and_null_baseline_rules(quota_home):
    status = _json(quota_home, "statusline", "--no-sync", "--as-of", _iso(8))
    assert {row["status"] for row in status["windows"]} == {"future"}
    assert all(row["current"] is None for row in status["windows"])

    forecast = _json(quota_home, "forecast", "--no-sync", "--as-of", _iso(8))
    assert {row["status"] for row in forecast["forecasts"]} == {"future"}
    assert all(row["currentPercent"] is None for row in forecast["forecasts"])
    assert all(row["sampleCount"] == 0 for row in forecast["forecasts"])


def test_statusline_future_semantics_distinguish_prior_and_clock_skew(quota_home):
    cache_path = quota_home / ".local" / "share" / "cctally" / "cache.db"
    with sqlite3.connect(cache_path) as cache:
        _seed_quota(
            cache, root_key="prior-future", source_path="/codex/prior-future/rollout.jsonl",
            captures=[(_iso(11), 10, 20.0), (_iso(12, 10), 20, 25.0)],
        )
        _seed_quota(
            cache, root_key="skew-only", source_path="/codex/skew-only/rollout.jsonl",
            captures=[(_iso(12, 5), 10, 30.0)],
        )
        cache.commit()

    prior = _json(
        quota_home, "statusline", "--no-sync", "--root-key", "prior-future",
        "--as-of", _iso(12),
    )["windows"][0]
    assert prior["freshness"]["state"] == "future"
    assert prior["status"] == "future"
    assert prior["current"]["usedPercent"] == 20.0
    prior_text = _run(
        quota_home, "statusline", "--no-sync", "--root-key", "prior-future",
        "--as-of", _iso(12),
    )
    assert "20.0%" in prior_text.stdout and "FUTURE DATA" in prior_text.stdout

    skew = _json(
        quota_home, "statusline", "--no-sync", "--root-key", "skew-only",
        "--as-of", _iso(12),
    )["windows"][0]
    assert skew["freshness"]["state"] == "fresh"
    assert skew["status"] == "unavailable"
    assert skew["current"] is None
    skew_text = _run(
        quota_home, "statusline", "--no-sync", "--root-key", "skew-only",
        "--as-of", _iso(12),
    )
    assert "unavailable" in skew_text.stdout
    assert "FUTURE DATA" not in skew_text.stdout


def test_timezone_ranges_are_non_vacuous_and_until_is_exclusive(quota_home):
    cache_path = quota_home / ".local" / "share" / "cctally" / "cache.db"
    with sqlite3.connect(cache_path) as cache:
        _seed_quota(
            cache, root_key="boundary-root", source_path="/codex/boundary/rollout.jsonl",
            limit_key="limit-boundary", captures=[
                ("2026-07-15T03:59:00Z", 10, 1.0),
                ("2026-07-15T04:00:00Z", 20, 2.0),
                ("2026-07-16T03:59:00Z", 30, 3.0),
                ("2026-07-16T04:00:00Z", 40, 4.0),
            ],
        )
        cache.commit()

    flags = (
        "history", "--no-sync", "--root-key", "boundary-root",
        "--limit-key", "limit-boundary", "--since", "2026-07-15",
        "--until", "2026-07-16",
    )
    ny = _json(quota_home, *flags)["windows"][0]["observations"]
    assert [row["capturedAt"] for row in ny] == [
        "2026-07-15T04:00:00Z", "2026-07-16T03:59:00Z",
    ]

    config = quota_home / ".local" / "share" / "cctally" / "config.json"
    config.write_text(json.dumps({"display": {"tz": "utc"}}))
    utc = _json(quota_home, *flags)["windows"][0]["observations"]
    assert [row["capturedAt"] for row in utc] == [
        "2026-07-15T03:59:00Z", "2026-07-15T04:00:00Z",
    ]


def test_timestamp_normalization_and_usage_errors_are_exact(quota_home):
    naive = _json(
        quota_home, "statusline", "--no-sync", "--root-key", "root-a",
        "--limit-key", "limit-primary", "--as-of", "2026-07-15T12:00:00",
    )
    explicit_z = _json(
        quota_home, "statusline", "--no-sync", "--root-key", "root-a",
        "--limit-key", "limit-primary", "--as-of", "2026-07-15T12:00:00Z",
    )
    assert naive == explicit_z

    reset_payloads = [
        _json(
            quota_home, "breakdown", "--no-sync", "--root-key", "root-a",
            "--limit-key", "limit-primary", "--reset-at", reset,
        )
        for reset in (
            "2026-07-15T15:00:00",
            "2026-07-15T15:00:00Z",
            "2026-07-15T18:00:00+03:00",
        )
    ]
    for payload in reset_payloads:
        payload.pop("generatedAt")
    assert reset_payloads[0] == reset_payloads[1] == reset_payloads[2]

    for reset, message in (
        ("2026-07-15", "date-only"),
        ("definitely-not-a-timestamp", "invalid --reset-at"),
    ):
        result = _run(
            quota_home, "breakdown", "--no-sync", "--root-key", "root-a",
            "--limit-key", "limit-primary", "--reset-at", reset,
        )
        assert result.returncode == 2
        assert message in result.stderr.lower()


def test_fixture_harness_asserts_expected_exit_statuses():
    harness = (REPO / "bin" / "cctally-codex-quota-test").read_text()
    assert "expected_exit" in harness
    assert "actual_exit" in harness
    assert "golden-$leaf-$mode.exit" in harness


def test_blocks_and_breakdown_normalize_reset_offset_and_keep_speed_contract(quota_home):
    blocks = _json(quota_home, "blocks", "--no-sync", "--since", "2026-07-15")
    assert any(block["identity"]["sourceRootKey"] == "root-b" for block in blocks["blocks"])

    selected = _json(
        quota_home, "breakdown", "--no-sync", "--root-key", "root-a",
        "--limit-key", "limit-primary", "--reset-at", "2026-07-15T18:00:00+03:00",
        "--speed", "fast",
    )
    assert selected["speed"] == "fast"
    assert selected["identity"]["sourceRootKey"] == "root-a"
    assert [row["percent"] for row in selected["milestones"]] == list(range(11, 17))

    date_only = _run(
        quota_home, "breakdown", "--no-sync", "--root-key", "root-a",
        "--limit-key", "limit-primary", "--reset-at", "2026-07-15",
    )
    assert date_only.returncode == 2
    assert "date-only" in date_only.stderr.lower()
