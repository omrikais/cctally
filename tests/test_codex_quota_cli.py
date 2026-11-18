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
        "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF": "1",
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
        "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF": "1",
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
    """The two providers render the same milestones with the same bytes.

    The Claude rows below are seeded with one `usage_snapshot_id` PER
    CROSSING, which is what the Codex rows they mirror are: each Codex
    crossing is its own observation. They previously all carried
    `usage_snapshot_id = 1`, and four of them share a `capturedAt`, so under
    #750 S3's observation-gap classifier the Claude side correctly reported a
    back-filled run that the Codex data cannot have. That divergence is real
    and intended — a Claude week with a genuine gap states it and Codex has
    no snapshot identity to state one from — so the parity this test asserts
    is over milestones that are not back-filled on either side.
    """
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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0)""",
            [
                (
                    row["capturedAt"], payload["weekStartDate"], payload["weekEndDate"],
                    payload["weekStartAt"], payload["weekEndAt"],
                    row["percentThreshold"], row["cumulativeCostUSD"],
                    row["marginalCostUSD"], snapshot_id,
                    row["fiveHourPercentAtCrossing"],
                )
                for snapshot_id, row in enumerate(payload["milestones"], start=1)
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


# ── #1108 T9: quota presentation while a relocated root's debt is pending ──

RELOCATED_AS_OF = "2026-07-14T12:30:00Z"
RETIRED_ONLY_RESET = "2026-07-14T06:00:00Z"


def _relocated_quota_store(tmp_path, monkeypatch):
    """The #1108 relocation store, with the corpus's long window made weekly."""
    from test_source_aware_cli import CODEX_CORPUS, _relocated_codex_store

    corpus = (CODEX_CORPUS / "modern-full.jsonl").read_text(encoding="utf-8")
    assert '"window_minutes":10020' in corpus
    return _relocated_codex_store(
        tmp_path, monkeypatch, as_of=RELOCATED_AS_OF,
        corpus=corpus.replace('"window_minutes":10020', '"window_minutes":10080'),
    )


def _main_out(ns, capsys, *argv: str) -> tuple[int, str]:
    code = ns["main"](list(argv))
    return code, capsys.readouterr().out


def _window_roots(payload: dict) -> set[str]:
    return {window["identity"]["sourceRootKey"] for window in payload["windows"]}


def _seed_retired_only_weekly_cycle(ns, root_key: str) -> None:
    """Copy one retired-root weekly capture into an older cycle only it holds."""
    conn = ns["open_cache_db"]()
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(quota_window_snapshots)") if row[1] != "id"]
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM quota_window_snapshots "
            "WHERE source_root_key = ? AND window_minutes = 10080 LIMIT 1",
            (root_key,)).fetchone()
        assert row is not None, "non-vacuity: the retired root holds no weekly window"
        values = dict(zip(columns, row))
        values.update(
            captured_at_utc="2026-07-13T10:00:00Z",
            resets_at_utc=RETIRED_ONLY_RESET,
            line_offset=values["line_offset"] + 1_000_000,
        )
        if "canonical_resets_at_utc" in values:
            values["canonical_resets_at_utc"] = RETIRED_ONLY_RESET
        conn.execute(
            f"INSERT INTO quota_window_snapshots ({', '.join(values)}) "
            f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("argv", [
    ("codex", "quota", "history", "--json"),
    ("codex", "percent-breakdown", "--speed", "standard", "--json"),
])
def test_relocated_codex_root_counts_once_in_quota_presentation(
    tmp_path, monkeypatch, capsys, argv,
):
    """#1108 T9 (D4): quota presentation reads the configured roots first.

    Failure it detects: while a relocated root's identities wait for the
    prune drain, `codex quota history` listed every quota window twice (once
    per root key) and the default `codex percent-breakdown` found two fresh
    weekly cycles and exited 2 as ambiguous, until the drain ran.
    """
    ns, old_key, new_key, drain = _relocated_quota_store(tmp_path, monkeypatch)

    pending_code, pending = _main_out(ns, capsys, *argv)
    drain()
    drained_code, drained = _main_out(ns, capsys, *argv)

    assert drained_code == 0
    assert new_key in drained and old_key not in drained, (
        f"non-vacuity: the drained oracle does not name the current root: {drained!r}")
    assert (pending_code, pending) == (drained_code, drained)


def test_explicit_retired_root_and_retired_only_cycle_keep_retained_history(
    tmp_path, monkeypatch, capsys,
):
    """#1108 T9: retained-history navigation resolves as before the scope.

    Failure it detects: the current-root view hiding history the user names.
    An explicit `--root-key` of the retired root must still select its
    complete history, and `--reset-at` on a cycle only the retired root holds
    must fall back to the complete retained set rather than exit 2.
    """
    ns, old_key, new_key, _drain = _relocated_quota_store(tmp_path, monkeypatch)
    _seed_retired_only_weekly_cycle(ns, old_key)

    code, retired = _main_out(
        ns, capsys, "codex", "quota", "history", "--root-key", old_key, "--json")
    assert code == 0
    code, current = _main_out(
        ns, capsys, "codex", "quota", "history", "--root-key", new_key, "--json")
    assert code == 0
    retired, current = json.loads(retired), json.loads(current)
    assert _window_roots(retired) == {old_key}
    assert _window_roots(current) == {new_key}
    # The relocation re-read the same rollouts, so the retired root holds
    # every current window plus the seeded retired-only cycle.
    assert len(retired["windows"]) == len(current["windows"])

    code, out = _main_out(
        ns, capsys, "codex", "percent-breakdown", "--reset-at", RETIRED_ONLY_RESET,
        "--speed", "standard", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["identity"]["sourceRootKey"] == old_key
    assert payload["weekEndAt"] == RETIRED_ONLY_RESET


def test_unregistered_configured_quota_root_stays_visible_and_physical_reads_keep_every_root(
    tmp_path, monkeypatch, capsys,
):
    """#1108 T9: the scope tests the loaded population, never the registry.

    Failure it detects: (1) a registry allowlist dropping a configured,
    recognizable root whose quota observations have no `codex_source_roots`
    row (it must stay visible while the retired root is withheld); (2) the
    presentation scope leaking into the physical readers, so the projection
    reconcile (and the alert arming it drives) stopped reading the retired
    root's observations before the drain removed them.
    """
    ns, old_key, new_key, _drain = _relocated_quota_store(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_source_roots WHERE source_root_key = ?", (new_key,))
        conn.commit()
    finally:
        conn.close()
    _seed_retired_only_weekly_cycle(ns, old_key)  # dirties the retired root's group
    quota = sys.modules["_cctally_quota"]
    real_load = quota.load_codex_quota_observations
    root_sets: list[object] = []

    def spy(*args, **kwargs):
        root_sets.append(kwargs.get("source_root_keys"))
        return real_load(*args, **kwargs)

    monkeypatch.setattr(quota, "load_codex_quota_observations", spy)
    code, out = _main_out(ns, capsys, "codex", "quota", "history", "--no-sync", "--json")

    assert code == 0, out
    assert _window_roots(json.loads(out)) == {new_key}
    assert any(
        keys is not None and old_key in keys for keys in root_sets
    ), f"the reconcile did not read the retired root: {root_sets!r}"


def _seed_codex_root_accounts(ns, *, old_key: str, new_key: str) -> dict[str, str]:
    """Two real Codex accounts, each owning one root's quota windows."""
    import _cctally_journal as jr
    import _lib_accounts
    import _lib_journal as lj

    keys = {}
    for label, root in (("carol", old_key), ("dave", new_key)):
        natural = f"acct-{label}\x00{label}@x.com"
        keys[label] = _lib_accounts.account_key("codex", natural)
        jr.append_record(lj.make_account_observe(
            at="2026-07-01T00:00:00Z", account_key=keys[label], provider="codex",
            natural_id=natural, email=f"{label}@x.com", plan_type="plus",
            label=label, label_source="auto"))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    conn = ns["open_cache_db"]()
    try:
        for label, root in (("carol", old_key), ("dave", new_key)):
            conn.execute(
                "UPDATE quota_window_snapshots SET account_key = ? "
                "WHERE source_root_key = ?", (keys[label], root))
        conn.commit()
    finally:
        conn.close()
    ns["reconcile_codex_quota_projection"](
        now=dt.datetime.fromisoformat(RELOCATED_AS_OF.replace("Z", "+00:00")))
    return keys


@pytest.mark.parametrize("label", ["carol", "dave"])
def test_percent_breakdown_account_selects_that_account_s_cycle(
    tmp_path, monkeypatch, capsys, label,
):
    """#1108 F4: `codex percent-breakdown --account` scopes the cycle.

    Failure it detects: the flag was parsed and ignored, so current-root-first
    narrowing rendered the CURRENT root's cycle (dave's) for `--account carol`,
    whose only cycle is on the retired root. The account filter applies before
    the default narrowing, keeps the current-first-then-retained fallback, and
    decorates the payload exactly as the other `--account` quota commands do.
    """
    ns, old_key, new_key, _drain = _relocated_quota_store(tmp_path, monkeypatch)
    keys = _seed_codex_root_accounts(ns, old_key=old_key, new_key=new_key)
    code, out = _main_out(
        ns, capsys, "codex", "percent-breakdown", "--account", label,
        "--speed", "standard", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["identity"]["sourceRootKey"] == (
        old_key if label == "carol" else new_key)
    assert payload["accountKey"] == keys[label]
    assert payload["accountLabel"] == label
    code, out = _main_out(
        ns, capsys, "codex", "percent-breakdown", "--speed", "standard", "--json")
    assert code == 0 and "accountKey" not in json.loads(out), (
        "R8: no decoration without an explicit --account")


def test_percent_breakdown_unknown_account_exits_2(tmp_path, monkeypatch, capsys):
    """#1108 F4: an unresolvable `--account` reference is refused (exit 2)
    instead of silently rendering some other account's cycle."""
    ns, old_key, new_key, _drain = _relocated_quota_store(tmp_path, monkeypatch)
    _seed_codex_root_accounts(ns, old_key=old_key, new_key=new_key)
    code = ns["main"]([
        "codex", "percent-breakdown", "--account", "nobody-xyz",
        "--speed", "standard", "--json"])
    captured = capsys.readouterr()
    assert code == 2, captured.out
    assert captured.out == ""


def _seed_shared_root_accounts(tmp_path, monkeypatch):
    """One root and one weekly reset held by two real accounts, carol and
    dave, with distinct spending and distinct five-hour percents. Dave's
    five-hour captures are the newer ones before each of carol's crossings,
    and dave spends far more on the same root."""
    import _cctally_journal as jr
    import _lib_accounts
    import _lib_journal as lj

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(12))
    keys = {}
    for label in ("carol", "dave"):
        natural = f"acct-{label}\x00{label}@x.com"
        keys[label] = _lib_accounts.account_key("codex", natural)
        jr.append_record(lj.make_account_observe(
            at="2026-07-01T00:00:00Z", account_key=keys[label], provider="codex",
            natural_id=natural, email=f"{label}@x.com", plan_type="plus",
            label=label, label_source="auto"))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    captures = {
        # (weekly captures, five-hour captures, accounting rows)
        "carol": ([(_iso(9), 10, 10.0), (_iso(10), 20, 12.4), (_iso(11), 30, 16.1)],
                  [(_iso(8), 110, 25.0), (_iso(9), 120, 30.0)],
                  [(20, _iso(10), 1000, 100), (30, _iso(11), 2000, 200)]),
        "dave": ([(_iso(9), 10, 40.0), (_iso(10), 20, 42.5), (_iso(11), 30, 45.2)],
                 [(_iso(9, 30), 110, 70.0), (_iso(10, 30), 120, 80.0)],
                 [(20, _iso(10), 50_000, 9_000), (30, _iso(11), 90_000, 9_000)]),
    }
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "INSERT INTO codex_source_roots (source_root_key, canonical_root_path,"
            " first_seen_utc, last_seen_utc) VALUES ('root-shared',"
            " '/codex/root-shared', ?, ?)", (_iso(8), _iso(12)))
        for label, (weekly, five_hour, accounting) in captures.items():
            for path, limit_key, window, reset, rows in (
                (f"/codex/root-shared/{label}-weekly.jsonl", "limit-weekly",
                 10_080, RESET, weekly),
                (f"/codex/root-shared/{label}-five-hour.jsonl", "limit-five-hour",
                 300, "2026-07-15T13:00:00Z", five_hour),
            ):
                cache.executemany(
                    """INSERT INTO quota_window_snapshots
                       (source, source_root_key, source_path, line_offset,
                        captured_at_utc, observed_slot, logical_limit_key,
                        limit_id, limit_name, window_minutes, used_percent,
                        resets_at_utc, plan_type, individual_limit_json,
                        reached_type, account_key)
                       VALUES ('codex', 'root-shared', ?, ?, ?, 'primary', ?,
                               'native-primary', 'Primary', ?, ?, ?, 'pro',
                               NULL, NULL, ?)""",
                    [(path, offset, captured, limit_key, window, used, reset,
                      keys[label]) for captured, offset, used in rows])
            cache.executemany(
                """INSERT INTO codex_session_entries
                   (source_path, line_offset, timestamp_utc, session_id, model,
                    input_tokens, cached_input_tokens, output_tokens,
                    reasoning_output_tokens, total_tokens, source_root_key,
                    account_key)
                   VALUES (?, ?, ?, ?, 'gpt-5', ?, 0, ?, 0, ?, 'root-shared', ?)""",
                [(f"/codex/root-shared/{label}-weekly.jsonl", offset, stamp,
                  f"session-{label}", tokens_in, tokens_out,
                  tokens_in + tokens_out, keys[label])
                 for offset, stamp, tokens_in, tokens_out in accounting])
        cache.commit()
    finally:
        cache.close()
    ns["reconcile_codex_quota_projection"](
        now=dt.datetime(2026, 7, 15, 12, tzinfo=UTC))
    return ns, keys


def test_percent_breakdown_account_scopes_its_cost_and_five_hour_reads(
    tmp_path, monkeypatch, capsys,
):
    """#1108 V2: `codex percent-breakdown --account` on a root and reset two
    accounts share. Failure it detects: the cycle was carol's but the
    accounting read and the five-hour correlation stayed merged, so carol's
    ladder priced dave's spend and showed dave's newer five-hour percent."""
    ns, keys = _seed_shared_root_accounts(tmp_path, monkeypatch)
    argv = ("codex", "percent-breakdown", "--account", "carol",
            "--reset-at", RESET, "--speed", "standard", "--json")
    code, out = _main_out(ns, capsys, *argv)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["accountKey"] == keys["carol"]
    milestones = payload["milestones"]
    assert [row["percentThreshold"] for row in milestones] == list(range(11, 17)), (
        "non-vacuity: carol's ladder")
    # Carol's own cost: the same ladder once dave's spend is gone.
    conn = ns["open_cache_db"]()
    try:
        removed = conn.execute(
            "DELETE FROM codex_session_entries WHERE account_key = ?",
            (keys["dave"],)).rowcount
        conn.commit()
    finally:
        conn.close()
    assert removed == 2, "non-vacuity: dave's spend was not seeded"
    code, out = _main_out(ns, capsys, *argv)
    assert code == 0, out
    alone = json.loads(out)["milestones"]
    assert [row["cumulativeCostUSD"] for row in milestones] == [
        row["cumulativeCostUSD"] for row in alone], (
        "carol's ladder priced another account's spend")
    assert {row["fiveHourPercentAtCrossing"] for row in milestones} == {30.0}, (
        "carol's crossings show another account's five-hour percent")


def test_quota_breakdown_account_scopes_its_cost(tmp_path, monkeypatch, capsys):
    """#1108, the twin of V2: `codex quota breakdown --account` on a root and
    reset two accounts share. Failure it detects: the identity was carol's
    but the accounting read stayed merged, so carol's milestones priced
    dave's spend."""
    ns, keys = _seed_shared_root_accounts(tmp_path, monkeypatch)
    argv = ("codex", "quota", "breakdown", "--account", "carol",
            "--limit-key", "limit-weekly", "--reset-at", RESET,
            "--speed", "standard", "--json")
    code, out = _main_out(ns, capsys, *argv)
    assert code == 0, out
    payload = json.loads(out)
    assert payload["accountKey"] == keys["carol"]
    milestones = payload["milestones"]
    assert milestones, "non-vacuity: carol's milestones"
    conn = ns["open_cache_db"]()
    try:
        removed = conn.execute(
            "DELETE FROM codex_session_entries WHERE account_key = ?",
            (keys["dave"],)).rowcount
        conn.commit()
    finally:
        conn.close()
    assert removed == 2, "non-vacuity: dave's spend was not seeded"
    code, out = _main_out(ns, capsys, *argv)
    assert code == 0, out
    alone = json.loads(out)["milestones"]
    assert [row["costUSD"] for row in milestones] == [
        row["costUSD"] for row in alone], (
        "carol's milestones priced another account's spend")


@pytest.mark.parametrize("leaf", ["history", "blocks"])
def test_retired_only_historical_range_falls_back_to_retained_history(
    tmp_path, monkeypatch, capsys, leaf,
):
    """#1108 F5: range eligibility is part of the current-root-first
    selection. Failure it detects: `--since/--until` were applied after the
    current population was chosen, so a historical range only the retired
    root covers rendered empty, where `main` returns the retained rows."""
    ns, old_key, _new_key, _drain = _relocated_quota_store(tmp_path, monkeypatch)
    _seed_retired_only_weekly_cycle(ns, old_key)
    code, out = _main_out(
        ns, capsys, "codex", "quota", leaf, "--no-sync",
        "--since", "2026-07-13T09:00:00Z", "--until", "2026-07-13T11:00:00Z",
        "--json")
    assert code == 0, out
    payload = json.loads(out)
    rows = payload["windows" if leaf == "history" else "blocks"]
    assert rows, f"the retired-only range rendered empty: {payload!r}"
    assert {row["identity"]["sourceRootKey"] for row in rows} == {old_key}
    if leaf == "blocks":
        assert {row["resetAt"] for row in rows} == {RETIRED_ONLY_RESET}
