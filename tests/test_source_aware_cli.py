"""S3 CLI routing and nested Codex-budget config contracts.

The compatibility cases intentionally use subprocesses: they keep the four
historical Codex reports at their actual command boundary rather than merely
asserting against an in-process renderer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

import pytest

from conftest import load_script, redirect_paths
from _lib_quota import QuotaObservation, QuotaWindowIdentity  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
CCTALLY = REPO_ROOT / "bin" / "cctally"
CODEX_CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


@pytest.fixture(autouse=True)
def _disable_detached_update_check(monkeypatch):
    """Keep this module's command-boundary tests from leaving a detached worker."""
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")


def _parser():
    ns = load_script()
    return ns["build_parser"]()


def _run_cli(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "CCTALLY_DATA_DIR": str(tmp_path / "data"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "TZ": "Etc/UTC",
    })
    return subprocess.run(
        [sys.executable, str(CCTALLY), *argv],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _seed_real_codex_cli_data(tmp_path: Path, monkeypatch):
    """Build the live S1/S2 cache through its real fused-ingest path."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T12:00:00Z")
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn)
    finally:
        conn.close()
    return ns


def _seed_real_provider_cli_data(tmp_path: Path, monkeypatch):
    """Seed both providers through their real cache-ingest paths."""
    ns = _seed_real_codex_cli_data(tmp_path, monkeypatch)
    project = tmp_path / "data" / ".claude" / "projects" / "-workspace-demo"
    project.mkdir(parents=True)
    (project / "claude-session.jsonl").write_text(
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-14T11:30:00Z",
            "sessionId": "claude-provider-aware-session",
            "cwd": "/workspace/demo",
            "message": {
                "id": "claude-provider-aware-message",
                "model": "claude-opus-4-5-20251101",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 200,
                },
            },
        }) + "\n",
        encoding="utf-8",
    )
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_cache"](conn)
    finally:
        conn.close()
    assert stats.rows_changed == 1
    return ns


def _seed_real_claude_report_data(ns) -> None:
    """Add one real weekly fact so the legacy report handler has a trend row."""
    week_start = dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc)
    week_end = week_start + dt.timedelta(days=7)
    captured = dt.datetime(2026, 7, 15, 11, tzinfo=dt.timezone.utc)
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            "week_end_at, weekly_percent, page_url, source, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                captured.isoformat().replace("+00:00", "Z"),
                week_start.date().isoformat(), (week_end - dt.timedelta(seconds=1)).date().isoformat(),
                week_start.isoformat().replace("+00:00", "Z"),
                week_end.isoformat().replace("+00:00", "Z"), 37.0,
                None, "fixture", json.dumps({"fixture": True}),
            ),
        )
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, cost_usd) "
            "VALUES (?, ?, ?, ?)",
            (
                captured.isoformat().replace("+00:00", "Z"),
                week_start.date().isoformat(), (week_end - dt.timedelta(seconds=1)).date().isoformat(), 1.25,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _provider_entries(ns):
    """Two project/date/model identities for real parser-to-handler tests."""
    entry = ns["QualifiedCodexEntry"]
    utc = dt.timezone.utc
    return (
        entry(
            timestamp=dt.datetime(2026, 7, 14, 10, tzinfo=utc),
            source_root_key="root-a", conversation_key="conversation-a",
            project_key="project:" + "a" * 24, project_label="alpha",
            model="gpt-5", input_tokens=100, cached_input_tokens=10,
            output_tokens=20, reasoning_output_tokens=0, total_tokens=120,
            cost_usd=1.0,
        ),
        entry(
            timestamp=dt.datetime(2026, 7, 15, 11, tzinfo=utc),
            source_root_key="root-b", conversation_key="conversation-b",
            project_key="project:" + "b" * 24, project_label="beta",
            model="gpt-5.5", input_tokens=100, cached_input_tokens=90,
            output_tokens=20, reasoning_output_tokens=5, total_tokens=125,
            cost_usd=3.0,
        ),
    )


def _diff_control_entries(ns):
    """Multi-row changes that make every source-aware diff control observable."""
    entry = ns["QualifiedCodexEntry"]
    utc = dt.timezone.utc

    def row(day, label, model, cost):
        return entry(
            timestamp=dt.datetime(2026, 7, day, 12, tzinfo=utc),
            source_root_key="root-a", conversation_key=f"conversation-{model}-{day}",
            project_key="project:" + (model.replace("gpt-", "") * 24)[:24],
            project_label=label, model=model, input_tokens=100,
            cached_input_tokens=20, output_tokens=30, reasoning_output_tokens=0,
            total_tokens=130, cost_usd=cost,
        )

    return (
        row(14, "tiny", "gpt-tiny", 100.0),
        row(15, "tiny", "gpt-tiny", 100.01),
        row(14, "small", "gpt-small", 100.0),
        row(15, "small", "gpt-small", 100.5),
        row(14, "alpha", "gpt-alpha", 8.0),
        row(15, "alpha", "gpt-alpha", 9.0),
        row(14, "zeta", "gpt-zeta", 5.0),
        row(15, "zeta", "gpt-zeta", 9.0),
        row(14, "dropped", "gpt-dropped", 3.0),
        row(15, "new", "gpt-new", 2.0),
    )


def _install_provider_entries(ns, monkeypatch, entries):
    """Keep the test at ``main`` while replacing only the cache-read seam."""
    adapter = ns["_cctally_source_analytics"]
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: tuple(entries),
    )
    monkeypatch.setattr(
        adapter, "load_codex_accounting_entries",
        lambda *_args, **_kwargs: tuple(entries),
    )
    return adapter


def _provider_quota_observations() -> tuple[QuotaObservation, ...]:
    """Two native points create detail rows for one selected Codex block."""
    utc = dt.timezone.utc
    identity = QuotaWindowIdentity(
        source="codex", source_root_key="root-a", logical_limit_key="primary",
        observed_slot="primary", window_minutes=7 * 24 * 60,
    )
    reset_at = dt.datetime(2026, 7, 21, tzinfo=utc)
    return (
        QuotaObservation(
            identity=identity, captured_at=dt.datetime(2026, 7, 14, 9, tzinfo=utc),
            used_percent=10.0, resets_at=reset_at,
            source_path="/synthetic/quota-a.jsonl", line_offset=1,
        ),
        QuotaObservation(
            identity=identity, captured_at=dt.datetime(2026, 7, 15, 12, tzinfo=utc),
            used_percent=20.0, resets_at=reset_at,
            source_path="/synthetic/quota-a.jsonl", line_offset=2,
        ),
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["project"],
        ["diff", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15"],
        ["range-cost", "--start", "2026-07-14T00:00:00Z"],
        ["cache-report"],
        ["report"],
        ["codex-daily"],
        ["codex-monthly"],
        ["codex-weekly"],
        ["codex-session"],
    ],
)
@pytest.mark.parametrize(
    "invalid_share_args",
    [
        ["--output", "out.md"],
        ["--copy"],
        ["--format", "html", "--copy"],
        ["--format", "md", "--open"],
        ["--format", "svg", "--open", "--output", "-"],
    ],
)
def test_invalid_share_flags_exit_before_every_provider_or_destination_io(
    monkeypatch, capsys, argv, invalid_share_args,
):
    """Every share-capable family validates destination shape at main entry."""
    ns = load_script()
    touched = []

    def forbidden(*_args, **_kwargs):
        touched.append("io")
        pytest.fail("share validation performed I/O")

    for name in (
        "load_config", "_load_claude_config_for_args", "get_entries",
        "get_codex_entries", "sync_cache", "sync_codex_cache",
        "_share_render_and_emit", "_resolve_destination", "_emit",
    ):
        monkeypatch.setitem(ns, name, forbidden)

    with pytest.raises(SystemExit) as raised:
        ns["main"]([*argv, *invalid_share_args])

    assert raised.value.code == 2
    assert touched == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("argv", "expected_source"),
    [
        (["diff", "--a", "last-7d", "--b", "prev-7d"], "claude"),
        (["claude", "diff", "--a", "last-7d", "--b", "prev-7d"], "claude"),
        (["codex", "diff", "--a", "last-7d", "--b", "prev-7d"], "codex"),
        (["range-cost", "--start", "2026-07-14T00:00:00Z"], "claude"),
        (["claude", "range-cost", "--start", "2026-07-14T00:00:00Z"], "claude"),
        (["codex", "range-cost", "--start", "2026-07-14T00:00:00Z"], "codex"),
        (["cache-report"], "claude"),
        (["claude", "cache-report"], "claude"),
        (["codex", "cache-report"], "codex"),
    ],
)
def test_every_source_analytics_share_parser_has_reveal_projects_default(
    argv, expected_source,
):
    args = _parser().parse_args(argv)

    assert args.source == expected_source
    assert args.reveal_projects is False
    assert _parser().parse_args([*argv, "--reveal-projects"]).reveal_projects is True


@pytest.mark.parametrize(
    "argv",
    [
        ["diff", "--help"], ["claude", "diff", "--help"], ["codex", "diff", "--help"],
        ["range-cost", "--help"], ["claude", "range-cost", "--help"], ["codex", "range-cost", "--help"],
        ["cache-report", "--help"], ["claude", "cache-report", "--help"], ["codex", "cache-report", "--help"],
    ],
)
def test_every_source_analytics_share_help_exposes_reveal_projects(tmp_path, argv):
    result = _run_cli(tmp_path, *argv)

    assert result.returncode == 0, result.stderr
    assert "--reveal-projects" in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["diff", "--a", "last-7d", "--b", "prev-7d"],
        ["range-cost", "--start", "2026-07-14T00:00:00Z"],
        ["cache-report"],
    ],
)
def test_reveal_projects_invalid_share_combination_fails_before_io(monkeypatch, capsys, argv):
    """The newly exposed privacy flag reaches the shared pre-I/O validator."""
    ns = load_script()
    touched = []

    def forbidden(*_args, **_kwargs):
        touched.append("io")
        pytest.fail("share validation performed I/O")

    for name in (
        "load_config", "_load_claude_config_for_args", "get_entries",
        "get_codex_entries", "sync_cache", "sync_codex_cache",
        "_share_render_and_emit", "_resolve_destination", "_emit",
    ):
        monkeypatch.setitem(ns, name, forbidden)

    with pytest.raises(SystemExit) as raised:
        ns["main"]([*argv, "--reveal-projects", "--copy"])

    assert raised.value.code == 2
    assert "--copy requires --format" in capsys.readouterr().err
    assert touched == []


@pytest.mark.parametrize(
    ("argv", "required", "forbidden"),
    [
        (
            ["codex", "project", "--help"],
            ("Aggregate Codex project usage for calendar weeks.", "exact opaque project key",
             "project:0123456789abcdef01234567"),
            ("Claude subscription", "--sort used --order"),
        ),
        (
            ["codex", "report", "--help"],
            ("Codex quota-window", "Sync Codex accounting"),
            ("sync-week", "--week-start-name", "--mode", "--offline", "--project"),
        ),
    ],
)
def test_fixed_codex_analytics_help_matches_provider_native_runtime(
    tmp_path, argv, required, forbidden,
):
    result = _run_cli(tmp_path, *argv)

    assert result.returncode == 0, result.stderr
    for text in required:
        assert text in result.stdout
    for text in forbidden:
        assert text not in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "project", "--since", "2026-07-14", "--until", "2026-07-14", "--json"],
        ["codex", "diff", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15", "--json"],
        ["codex", "range-cost", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-14T23:59:59Z", "--json"],
        ["codex", "cache-report", "--since", "2026-07-14", "--until", "2026-07-15", "--json"],
        ["codex", "report", "--weeks", "1", "--json"],
    ],
)
def test_raw_codex_provider_commands_execute_against_real_fused_cache(
    tmp_path, monkeypatch, capsys, argv,
):
    ns = _seed_real_codex_cli_data(tmp_path, monkeypatch)

    assert ns["main"](argv) == 0
    wire = json.loads(capsys.readouterr().out)

    assert wire["source"] == "codex"
    assert wire["status"] in {"ok", "empty", "partial"}
    assert "/synthetic/root-a" not in repr(wire)


def test_real_codex_project_allocates_one_s2_logical_block_by_root_cost(
    tmp_path, monkeypatch, capsys,
):
    """The real project command reads S2 blocks rather than inventing a quota.

    Two separately qualified S1 conversations carry equal accounting cost inside
    the same native primary window.  The stale secondary window deliberately
    excludes both entries, so the provider result must preserve one unblended
    logical attribution and split its observed percent by cost.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    first = provider_root / "sessions" / "2026" / "07" / "15" / "red.jsonl"
    second = provider_root / "sessions" / "2026" / "07" / "15" / "blue.jsonl"
    first.parent.mkdir(parents=True)
    corpus = (CODEX_CORPUS / "modern-full.jsonl").read_text(encoding="utf-8")
    first.write_text(corpus, encoding="utf-8")
    second.write_text(
        corpus.replace("root-thread-a", "root-thread-blue")
        .replace("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
        .replace("project-red", "project-blue"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T12:00:00Z")

    assert ns["main"]([
        "codex", "project", "--since", "2026-07-14", "--until", "2026-07-14", "--json",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)
    rows = {row["displayLabel"]: row for row in wire["data"]["projects"]}

    assert set(rows) == {"project-red", "project-blue"}
    attributions = [rows[label]["quotaAttributions"] for label in sorted(rows)]
    assert all(len(value) == 1 for value in attributions)
    assert {value[0]["quotaKey"] for value in attributions}.__len__() == 1
    assert {value[0]["slot"] for value in attributions} == {"primary"}
    assert {value[0]["windowMinutes"] for value in attributions} == {330}
    assert {value[0]["usedPercent"] for value in attributions} == {12.5}
    assert [value[0]["attributedUsedPercent"] for value in attributions] == pytest.approx([6.25, 6.25])
    assert [value[0]["costPerPercent"] for value in attributions] == pytest.approx(
        [wire["data"]["totals"]["costUsd"] / 12.5] * 2,
    )

    adapter = ns["_cctally_source_analytics"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: ns["SourceResult"](
            "claude", "empty", {"projects": []},
        ),
    )
    assert ns["main"]([
        "project", "--source", "all", "--since", "2026-07-14",
        "--until", "2026-07-14", "--json",
    ]) == 0
    all_wire = json.loads(capsys.readouterr().out)
    all_rows = {row["displayLabel"]: row for row in all_wire["sources"][1]["data"]["projects"]}
    assert set(all_rows) == {"project-red", "project-blue"}
    all_attributions = [all_rows[label]["quotaAttributions"] for label in sorted(all_rows)]
    assert all(len(value) == 1 for value in all_attributions)
    assert {value[0]["quotaKey"] for value in all_attributions}.__len__() == 1
    assert [value[0]["attributedUsedPercent"] for value in all_attributions] == pytest.approx([6.25, 6.25])


def test_codex_project_keeps_accounting_when_s2_quota_state_is_unavailable(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    adapter = ns["_cctally_source_analytics"]
    monkeypatch.setattr(
        adapter, "load_codex_quota_observations",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("quota unavailable")),
    )

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16", "--json",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)
    assert wire["status"] == "partial"
    assert wire["warnings"] == [{
        "code": "quota_state_unavailable",
        "message": "Codex quota state is unavailable.",
    }]
    assert wire["data"]["totals"]["costUsd"] == 4.0
    assert all(row["quotaAttributions"] == [{
        "quotaKey": None,
        "slot": None,
        "windowMinutes": None,
        "resetAt": None,
        "usedPercent": None,
        "attributedUsedPercent": None,
        "costPerPercent": None,
        "status": "unavailable",
    }] for row in wire["data"]["projects"])

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16",
    ]) == 0
    assert "Partial: Codex quota state is unavailable." in capsys.readouterr().out

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16", "--format", "md", "--output", "-",
    ]) == 0
    assert "Codex quota state is unavailable." in capsys.readouterr().out

    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result("claude", "empty", {"projects": []}),
    )
    assert ns["main"]([
        "project", "--source", "all", "--since", "2026-07-14",
        "--until", "2026-07-16", "--json",
    ]) == 0
    all_wire = json.loads(capsys.readouterr().out)
    assert all_wire["sources"][1]["status"] == "partial"
    assert all_wire["sources"][1]["warnings"] == wire["warnings"]


def test_codex_report_quota_failure_keeps_the_as_of_envelope_and_source_block(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-16T00:00:00Z")
    monkeypatch.setattr(
        adapter, "load_codex_quota_observations",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("quota unavailable")),
    )

    assert ns["main"](["report", "--source", "codex", "--json"]) == 0
    direct = json.loads(capsys.readouterr().out)
    assert direct["status"] == "partial"
    assert direct["data"]["asOf"] == "2026-07-16T00:00:00Z"
    assert direct["data"]["sections"] == [{
        "key": "quota-series",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "quota_state_unavailable",
            "message": "Codex quota state is unavailable.",
        }],
    }]
    assert direct["warnings"] == direct["data"]["sections"][0]["warnings"]

    assert ns["main"](["report", "--source", "codex"]) == 0
    assert "Partial: Codex quota state is unavailable." in capsys.readouterr().out

    assert ns["main"]([
        "report", "--source", "codex", "--format", "md", "--output", "-",
    ]) == 0
    assert "Codex quota state is unavailable." in capsys.readouterr().out

    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result("claude", "empty", {"current": None, "trend": []}),
    )
    assert ns["main"](["report", "--source", "all", "--json"]) == 0
    all_wire = json.loads(capsys.readouterr().out)
    assert all_wire["sources"][1]["status"] == "partial"
    assert all_wire["sources"][1]["data"] == direct["data"]

    assert ns["main"]([
        "report", "--source", "all", "--format", "md", "--output", "-",
    ]) == 0
    all_share = capsys.readouterr().out
    assert "**Claude**" in all_share and "**Codex**" in all_share
    assert "Codex quota state is unavailable." in all_share


def test_main_project_filters_keep_native_quota_denominator_for_project_model_and_all(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = tuple(
        entry.__class__(**{
            **entry.__dict__,
            "cost_usd": 1.0,
            "source_root_key": "root-a",
        })
        for entry in _provider_entries(ns)
    )
    adapter = _install_provider_entries(ns, monkeypatch, entries)
    utc = dt.timezone.utc
    identity = QuotaWindowIdentity(
        source="codex", source_root_key="root-a", logical_limit_key="primary",
        observed_slot="primary", window_minutes=7 * 24 * 60,
    )
    observations = (
        QuotaObservation(
            identity=identity, captured_at=dt.datetime(2026, 7, 14, 9, tzinfo=utc),
            used_percent=5.0, resets_at=dt.datetime(2026, 7, 21, tzinfo=utc),
            source_path="/synthetic/quota.jsonl", line_offset=1,
        ),
        QuotaObservation(
            identity=identity, captured_at=dt.datetime(2026, 7, 15, 12, tzinfo=utc),
            used_percent=10.0, resets_at=dt.datetime(2026, 7, 21, tzinfo=utc),
            source_path="/synthetic/quota.jsonl", line_offset=2,
        ),
    )
    monkeypatch.setattr(adapter, "load_codex_quota_observations", lambda: observations)
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T12:00:00Z")
    argv = ["project", "--source", "codex", "--since", "2026-07-14", "--until", "2026-07-15", "--json"]

    assert ns["main"](argv) == 0
    unfiltered = json.loads(capsys.readouterr().out)
    assert [row["quotaAttributions"][0]["attributedUsedPercent"] for row in unfiltered["data"]["projects"]] == pytest.approx([5.0, 5.0])

    assert ns["main"]([*argv, "--project", entries[0].project_key]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["data"]["projects"][0]["quotaAttributions"][0]["attributedUsedPercent"] == pytest.approx(5.0)

    assert ns["main"]([*argv, "--model", "gpt-5"]) == 0
    modeled = json.loads(capsys.readouterr().out)
    assert modeled["data"]["projects"][0]["quotaAttributions"][0]["attributedUsedPercent"] == pytest.approx(5.0)

    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result("claude", "empty", {"projects": []}),
    )
    all_argv = [
        "project", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-15",
        "--project", entries[0].project_key, "--json",
    ]
    assert ns["main"](all_argv) == 0
    all_wire = json.loads(capsys.readouterr().out)
    assert all_wire["sources"][1]["data"]["projects"][0]["quotaAttributions"][0]["attributedUsedPercent"] == pytest.approx(5.0)


@pytest.mark.parametrize("failure", ["broken-cache", "lock-contention"])
def test_real_codex_accounting_falls_back_without_project_metadata(
    tmp_path, monkeypatch, capsys, failure,
):
    """Accounting reuses the canonical direct parser; projects stay S1-only."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CODEX_CORPUS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-15T12:00:00Z")

    cache_module = ns["_cctally_cache"]
    direct_calls: list[tuple[dt.datetime, dt.datetime]] = []
    direct = cache_module._collect_codex_entries_direct

    def record_direct(start, end):
        direct_calls.append((start, end))
        return direct(start, end)

    monkeypatch.setattr(cache_module, "_collect_codex_entries_direct", record_direct)
    if failure == "broken-cache":
        def unavailable_cache():
            raise sqlite3.OperationalError("synthetic cache failure")

        monkeypatch.setitem(ns, "open_cache_db", unavailable_cache)
        monkeypatch.setattr(cache_module, "open_cache_db", unavailable_cache)
    else:
        contended = type("Stats", (), {"lock_contended": True})()
        monkeypatch.setitem(ns, "sync_codex_cache", lambda _conn: contended)
        monkeypatch.setattr(cache_module, "sync_codex_cache", lambda _conn: contended)

    assert ns["main"]([
        "codex", "range-cost", "--start", "2026-07-14T00:00:00Z",
        "--end", "2026-07-14T23:59:59Z", "--json",
    ]) == 0
    range_wire = json.loads(capsys.readouterr().out)
    assert range_wire["status"] == "ok"
    assert range_wire["data"]["totals"]["totalTokens"] == 1600

    assert ns["main"]([
        "codex", "cache-report", "--since", "2026-07-14", "--until", "2026-07-15", "--json",
    ]) == 0
    reuse_wire = json.loads(capsys.readouterr().out)
    assert reuse_wire["status"] == "partial"
    assert reuse_wire["data"]["totals"]["totalTokens"] == 1600

    assert ns["main"]([
        "codex", "diff", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--sync", "--json",
    ]) == 0
    diff_wire = json.loads(capsys.readouterr().out)
    assert diff_wire["status"] == "partial"
    overall = next(section for section in diff_wire["data"]["sections"] if section["key"] == "overall")
    assert overall["data"]["rows"][0]["a"]["total_tokens"] == 1600
    assert len(direct_calls) == 3

    assert ns["main"]([
        "codex", "project", "--since", "2026-07-14", "--until", "2026-07-14", "--json",
    ]) == 3
    project_wire = json.loads(capsys.readouterr().out)
    assert project_wire["status"] == "unavailable"
    assert len(direct_calls) == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["project", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-14", "--json"],
        ["diff", "--source", "all", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15", "--json"],
        ["range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-14T23:59:59Z", "--json"],
        ["cache-report", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-15", "--json"],
        ["report", "--source", "all", "--weeks", "1", "--json"],
    ],
)
def test_all_source_commands_compose_real_provider_blocks(
    tmp_path, monkeypatch, capsys, argv,
):
    ns = _seed_real_codex_cli_data(tmp_path, monkeypatch)

    assert ns["main"](argv) == 0
    wire = json.loads(capsys.readouterr().out)

    assert wire["source"] == "all"
    assert [block["source"] for block in wire["sources"]] == ["claude", "codex"]
    if argv[0] == "report":
        assert "combined" not in wire
    else:
        assert set(wire["combined"]) == {"costUsd", "totalTokens"} or set(wire["combined"]) == {"cost_usd", "total_tokens"}
    assert "/synthetic/root-a" not in repr(wire)


@pytest.mark.parametrize(
    ("argv", "claude_marker", "codex_marker"),
    [
        (
            ["project", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-15"],
            "project-1", "project-2",
        ),
        (
            ["diff", "--source", "all", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15"],
            "Overall", "Models",
        ),
        (
            ["range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-15T23:59:59Z"],
            "claude-opus-4-5-20251101", "1,600",
        ),
        (
            ["cache-report", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-15"],
            "57.1%", "25.0%",
        ),
        (
            ["report", "--source", "all", "--weeks", "1"],
            "Jul 13", "primary · 10080m",
        ),
    ],
)
def test_real_all_source_populated_share_keeps_provider_rows_separate_and_ordered(
    tmp_path, monkeypatch, capsys, argv, claude_marker, codex_marker,
):
    """Exercise the parser, real dual cache facts, and Claude-then-Codex composer."""
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)
    _seed_real_claude_report_data(ns)
    adapter = ns["_cctally_source_analytics"]
    monkeypatch.setattr(adapter, "load_codex_quota_observations", _provider_quota_observations)

    assert ns["main"]([*argv, "--format", "md", "--output", "-"]) == 0
    rendered = capsys.readouterr().out

    claude_at = rendered.index("**Claude**")
    codex_at = rendered.index("**Codex**")
    assert claude_at < codex_at
    assert claude_marker in rendered[claude_at:codex_at]
    assert codex_marker in rendered[codex_at:]


@pytest.mark.parametrize("fmt", ("md", "html", "svg"))
@pytest.mark.parametrize(
    ("argv", "marker"),
    [
        (
            ["diff", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15"],
            "$",
        ),
        (
            ["range-cost", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-15T23:59:59Z"],
            "claude-opus-4-5-20251101",
        ),
        (
            ["cache-report", "--since", "2026-07-14", "--until", "2026-07-15"],
            "2026-07-14",
        ),
    ],
)
def test_real_default_claude_share_for_newly_shareable_commands_all_formats(
    tmp_path, monkeypatch, capsys, argv, marker, fmt,
):
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)

    assert ns["main"]([*argv, "--format", fmt, "--output", "-"]) == 0
    rendered = capsys.readouterr().out

    assert "Claude" in rendered
    assert marker in rendered


@pytest.mark.parametrize(
    "argv",
    [
        ["diff", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15"],
        ["range-cost", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-15T23:59:59Z"],
        ["cache-report", "--since", "2026-07-14", "--until", "2026-07-15"],
    ],
)
def test_ordinary_default_claude_paths_match_explicit_claude_bytes(
    tmp_path, monkeypatch, capsys, argv,
):
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)

    assert ns["main"](argv) == 0
    default = capsys.readouterr()
    assert ns["main"]([*argv, "--source", "claude"]) == 0
    explicit = capsys.readouterr()

    # The startup migration diagnostic is emitted at most once per process;
    # ordinary command bytes are the stdout contract preserved by the route.
    assert explicit.out == default.out


@pytest.mark.parametrize(
    ("all_argv", "claude_argv"),
    [
        (
            ["project", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-14", "--json"],
            ["project", "--since", "2026-07-14", "--until", "2026-07-14", "--json"],
        ),
        (
            ["diff", "--source", "all", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15", "--json"],
            ["diff", "--a", "2026-07-14..2026-07-14", "--b", "2026-07-15..2026-07-15", "--json"],
        ),
        (
            ["range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-14T23:59:59Z", "--json"],
            ["range-cost", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-14T23:59:59Z", "--json"],
        ),
        (
            ["cache-report", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-15", "--json"],
            ["cache-report", "--since", "2026-07-14", "--until", "2026-07-15", "--json"],
        ),
        (
            ["report", "--source", "all", "--weeks", "1", "--json"],
            ["report", "--weeks", "1", "--json"],
        ),
    ],
)
def test_all_source_keeps_the_exact_structured_claude_json_block(
    tmp_path, monkeypatch, capsys, all_argv, claude_argv,
):
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)

    assert ns["main"](all_argv) == 0
    all_wire = json.loads(capsys.readouterr().out)
    assert ns["main"](claude_argv) == 0
    claude_wire = json.loads(capsys.readouterr().out)

    assert all_wire["sources"][0]["status"] in {"ok", "empty"}
    assert all_wire["sources"][0] == {
        "source": "claude",
        "status": all_wire["sources"][0]["status"],
        "data": claude_wire,
        "warnings": [],
    }
    codex_data = all_wire["sources"][1]["data"]
    if claude_argv[0] == "range-cost":
        assert all_wire["combined"] == {
            "costUsd": claude_wire["totalCostUSD"] + codex_data["totals"]["costUsd"],
            "totalTokens": sum(
                row["totalTokens"] for row in claude_wire["modelBreakdowns"]
            ) + codex_data["totals"]["totalTokens"],
        }
    elif claude_argv[0] == "project":
        assert all_wire["combined"] == {
            "costUsd": claude_wire["totals"]["costUsd"] + codex_data["totals"]["costUsd"],
            "totalTokens": sum(
                row["inputTokens"] + row["outputTokens"]
                + row["cacheWriteTokens"] + row["cacheReadTokens"]
                for row in claude_wire["projects"]
            ) + codex_data["totals"]["totalTokens"],
        }
    elif claude_argv[0] == "cache-report":
        assert all_wire["combined"] == {
            "costUsd": claude_wire["totals"]["cost"] + codex_data["totals"]["costUsd"],
            "totalTokens": claude_wire["totals"]["totalTokens"] + codex_data["totals"]["totalTokens"],
        }
    elif claude_argv[0] == "diff":
        overall = next(section for section in claude_wire["sections"] if section["name"] == "overall")
        legacy_row = overall["rows"][0]
        codex_combined = codex_data["combined"]
        assert all_wire["combined"]["cost_usd"] == {
            "a": legacy_row["a"]["cost_usd"] + codex_combined["cost_usd"]["a"],
            "b": legacy_row["b"]["cost_usd"] + codex_combined["cost_usd"]["b"],
            "delta": (
                legacy_row["b"]["cost_usd"] + codex_combined["cost_usd"]["b"]
                - legacy_row["a"]["cost_usd"] - codex_combined["cost_usd"]["a"]
            ),
        }
    else:
        assert "combined" not in all_wire


def test_raw_project_metadata_loss_keeps_the_all_source_claude_block(tmp_path, monkeypatch, capsys):
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DELETE FROM codex_conversation_threads")
        conn.commit()
    finally:
        conn.close()

    direct_argv = [
        "codex", "project", "--since", "2026-07-14", "--until", "2026-07-14", "--json",
    ]
    assert ns["main"](direct_argv) == 3
    direct = json.loads(capsys.readouterr().out)
    assert direct == {
        "schemaVersion": 1,
        "source": "codex",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "qualified_metadata_unavailable",
            "message": "Codex qualified project metadata is unavailable.",
        }],
    }

    all_argv = [
        "project", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-14", "--json",
    ]
    assert ns["main"](all_argv) == 0
    combined = json.loads(capsys.readouterr().out)
    assert combined["source"] == "all"
    assert combined["sources"][0]["source"] == "claude"
    assert combined["sources"][0]["status"] == "ok"
    assert combined["sources"][1] == {
        "source": "codex",
        "status": "unavailable",
        "data": None,
        "warnings": direct["warnings"],
    }


def test_raw_range_cost_total_only_keeps_the_bare_physical_number(tmp_path, monkeypatch, capsys):
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)
    bounds = ["--start", "2026-07-14T00:00:00Z", "--end", "2026-07-14T23:59:59Z"]

    assert ns["main"](["range-cost", *bounds, "--total-only", "--json"]) == 0
    claude_cost = float(capsys.readouterr().out)
    assert ns["main"](["codex", "range-cost", *bounds, "--total-only", "--json"]) == 0
    codex_cost = float(capsys.readouterr().out)
    assert ns["main"](["range-cost", "--source", "all", *bounds, "--total-only", "--json"]) == 0

    assert float(capsys.readouterr().out) == pytest.approx(claude_cost + codex_cost)


def test_all_source_diff_uses_calendar_week_windows_without_claude_anchor(
    tmp_path, monkeypatch, capsys,
):
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)

    assert ns["main"]([
        "diff", "--source", "all", "--a", "last-week", "--b", "this-week", "--json",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)

    claude, codex = wire["sources"]
    # The Claude leg used the already-resolved calendar bounds rather than
    # retrying its subscription-week parser (which has no anchor in this
    # fixture), so it remains available and can legitimately find the seeded
    # current-week entry.
    assert claude["status"] in {"ok", "empty"}
    assert claude["data"]["windows"]["a"]["label"] == "last-week"
    assert codex["data"]["windows"]["a"]["label"] == "last-week"


@pytest.mark.parametrize(
    ("source_argv", "is_all"),
    [
        (["codex", "diff"], False),
        (["diff", "--source", "all"], True),
    ],
)
def test_project_excluding_diff_uses_accounting_without_metadata_degradation(
    tmp_path, monkeypatch, capsys, source_argv, is_all,
):
    """Selected non-project sections stay complete even if a join would fail."""
    ns = _seed_real_provider_cli_data(tmp_path, monkeypatch)
    adapter = ns["_cctally_source_analytics"]
    qualified_reads: list[str] = []

    def unavailable_join(*_args, **_kwargs):
        qualified_reads.append("qualified")
        raise adapter.QualifiedMetadataUnavailable("metadata unavailable")

    monkeypatch.setattr(adapter, "load_qualified_codex_entries", unavailable_join)
    diff_args = [
        *source_argv,
        "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15",
        "--only", "models,token-reuse",
    ]

    assert ns["main"]([*diff_args, "--json"]) == 0
    wire = json.loads(capsys.readouterr().out)
    codex = wire["sources"][1] if is_all else wire
    assert codex["status"] == "ok"
    assert codex["warnings"] == []
    assert [section["key"] for section in codex["data"]["sections"]] == [
        "models", "token-reuse",
    ]
    assert qualified_reads == []

    assert ns["main"]([*diff_args, "--format", "md", "--output", "-"]) == 0
    rendered = capsys.readouterr().out
    assert "Codex" in rendered
    assert "Partial:" not in rendered
    assert "qualified project metadata is unavailable" not in rendered
    assert qualified_reads == []


def test_main_codex_project_applies_exact_project_model_sort_and_breakdown(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _provider_entries(ns))

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-15", "--model", "gpt", "--sort", "cost",
        "--order", "asc", "--breakdown", "--json",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)

    assert [row["displayLabel"] for row in wire["data"]["projects"]] == ["alpha", "beta"]
    assert wire["data"]["projects"][1]["models"][0]["model"] == "gpt-5.5"

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-15", "--model", "gpt", "--sort", "cost",
        "--order", "desc", "--json",
    ]) == 0
    descending = json.loads(capsys.readouterr().out)
    assert [row["displayLabel"] for row in descending["data"]["projects"]] == ["beta", "alpha"]

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-15", "--project", "beta", "--breakdown",
    ]) == 0
    assert "  - gpt-5.5:" in capsys.readouterr().out


def test_main_codex_rejects_malformed_opaque_project_before_cache_read(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: pytest.fail("project syntax must fail before I/O"),
    )

    assert ns["main"]([
        "project", "--source", "codex", "--project", "project:not-opaque",
    ]) == 2
    captured = capsys.readouterr()
    assert "invalid opaque project key" in captured.err
    assert captured.out == ""


def test_main_codex_range_and_cache_project_filters_require_qualified_metadata(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]

    def unavailable(*_args, **_kwargs):
        raise adapter.QualifiedMetadataUnavailable("unavailable")

    monkeypatch.setattr(adapter, "load_qualified_codex_entries", unavailable)
    monkeypatch.setattr(
        adapter, "load_codex_accounting_entries",
        lambda *_args, **_kwargs: pytest.fail("requested project must not fall back"),
    )

    bounds = ["--start", "2026-07-14T00:00:00Z", "--end", "2026-07-15T00:00:00Z"]
    assert ns["main"]([
        "range-cost", "--source", "codex", *bounds, "--project", "alpha", "--json",
    ]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"

    assert ns["main"]([
        "cache-report", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-15", "--project", "alpha", "--json",
    ]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_main_codex_cache_reuse_sort_and_diff_only_apply_to_real_result(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _provider_entries(ns))

    assert ns["main"]([
        "cache-report", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16", "--sort", "reuse", "--json",
    ]) == 0
    reuse_wire = json.loads(capsys.readouterr().out)
    rows = reuse_wire["data"]["sections"][0]["data"]["rows"]
    assert rows[0]["label"] == "2026-07-15"

    assert ns["main"]([
        "diff", "--source", "codex", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--only", "token-reuse", "--json",
    ]) == 0
    diff_wire = json.loads(capsys.readouterr().out)
    assert [section["key"] for section in diff_wire["data"]["sections"]] == ["token-reuse"]


def test_main_codex_cache_report_keeps_requested_bounds_with_sparse_and_empty_results(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    argv = [
        "cache-report", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16", "--json",
    ]
    assert ns["main"](argv) == 0
    sparse = json.loads(capsys.readouterr().out)
    assert sparse["data"]["start"] == "2026-07-14T00:00:00Z"
    assert sparse["data"]["end"] == "2026-07-17T00:00:00Z"

    assert ns["main"]([
        "cache-report", "--source", "codex", "--since", "2026-07-18",
        "--until", "2026-07-19", "--json",
    ]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["status"] == "empty"
    assert empty["data"]["start"] == "2026-07-18T00:00:00Z"
    assert empty["data"]["end"] == "2026-07-20T00:00:00Z"


def test_main_codex_diff_applies_noise_sort_top_and_only_to_multi_row_results(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _diff_control_entries(ns))
    base = [
        "diff", "--source", "codex", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--only", "models", "--json",
    ]

    assert ns["main"](base) == 0
    default_wire = json.loads(capsys.readouterr().out)
    default_rows = default_wire["data"]["sections"][0]["data"]["rows"]
    assert default_wire["data"]["options"]["smart_filter"] is True
    assert "gpt-tiny" not in [row["label"] for row in default_rows]
    assert "gpt-small" in [row["label"] for row in default_rows]

    assert ns["main"]([*base, "--all"]) == 0
    all_wire = json.loads(capsys.readouterr().out)
    all_rows = all_wire["data"]["sections"][0]["data"]["rows"]
    assert all_wire["data"]["options"]["smart_filter"] is False
    assert "gpt-tiny" in [row["label"] for row in all_rows]

    assert ns["main"]([*base, "--all", "--sort", "name"]) == 0
    name_rows = json.loads(capsys.readouterr().out)["data"]["sections"][0]["data"]["rows"]
    assert [row["label"] for row in name_rows] == sorted(row["label"] for row in name_rows)

    assert ns["main"]([*base, "--all", "--sort", "cost-a"]) == 0
    cost_rows = json.loads(capsys.readouterr().out)["data"]["sections"][0]["data"]["rows"]
    assert cost_rows[0]["label"] == "gpt-small"

    assert ns["main"]([*base, "--all", "--sort", "status"]) == 0
    status_wire = json.loads(capsys.readouterr().out)
    assert status_wire["data"]["options"]["sort"] == "status"
    status_rows = status_wire["data"]["sections"][0]["data"]["rows"]
    assert status_rows[0]["status"] == "dropped", status_rows
    assert status_rows[-1]["status"] == "new"

    assert ns["main"]([*base, "--all", "--top", "1"]) == 0
    top_rows = json.loads(capsys.readouterr().out)["data"]["sections"][0]["data"]["rows"]
    assert {row["status"] for row in top_rows} == {"changed", "dropped", "new"}
    assert sum(row["status"] == "changed" for row in top_rows) == 1

    assert ns["main"]([*base, "--min-delta", "1", "--min-delta-pct", "1"]) == 0
    threshold_rows = json.loads(capsys.readouterr().out)["data"]["sections"][0]["data"]["rows"]
    assert "gpt-small" not in [row["label"] for row in threshold_rows]


@pytest.mark.parametrize("source", ["codex", "all"])
def test_main_source_diff_rejects_mismatch_before_any_provider_io(
    tmp_path, monkeypatch, capsys, source,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    reads: list[str] = []
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: reads.append("codex") or pytest.fail("mismatch must fail before Codex I/O"),
    )
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: reads.append("claude") or pytest.fail("mismatch must fail before Claude I/O"),
    )

    assert ns["main"]([
        "diff", "--source", source, "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-16", "--json",
    ]) == 2
    captured = capsys.readouterr()
    assert "--allow-mismatch" in captured.err
    assert captured.out == ""
    assert reads == []


@pytest.mark.parametrize("source", ["codex", "all"])
def test_main_source_diff_rejects_deferred_with_before_any_provider_io(
    tmp_path, monkeypatch, capsys, source,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    reads: list[str] = []
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: reads.append("codex") or pytest.fail("--with must fail before Codex I/O"),
    )
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: reads.append("claude") or pytest.fail("--with must fail before Claude I/O"),
    )

    assert ns["main"]([
        "diff", "--source", source, "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--with", "trend", "--json",
    ]) == 1
    captured = capsys.readouterr()
    assert "not yet implemented" in captured.err
    assert captured.out == ""
    assert reads == []


def test_main_codex_diff_allow_mismatch_normalizes_window_accounting(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _diff_control_entries(ns))

    assert ns["main"]([
        "diff", "--source", "codex", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-16", "--allow-mismatch", "--only", "overall", "--json",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)
    assert wire["data"]["options"]["normalization"] == "per-day"
    assert wire["data"]["combined"]["cost_usd"]["b"] == pytest.approx(110.255)


def test_main_provider_terminals_render_reports_and_all_source_orders_providers(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result(
            "claude", "empty", {"totalCostUSD": 0.0, "modelBreakdowns": []},
        ),
    )

    assert ns["main"]([
        "project", "--source", "codex", "--since", "2026-07-14", "--until", "2026-07-15",
    ]) == 0
    populated = capsys.readouterr().out
    assert "Codex Project Report" in populated
    assert "alpha" in populated

    assert ns["main"]([
        "range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z",
        "--end", "2026-07-15T00:00:00Z",
    ]) == 0
    combined = capsys.readouterr().out
    assert combined.index("Claude Range Cost Report") < combined.index("Codex Range Cost Report")
    assert "Combined physical accounting" in combined


def test_codex_leaf_reconcile_receives_full_budget_outside_config_lock(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    config_module = ns["_cctally_config"]
    state = {"inside": False}
    calls: list[tuple[bool, dict]] = []

    @contextmanager
    def tracking_lock():
        state["inside"] = True
        try:
            yield
        finally:
            state["inside"] = False

    monkeypatch.setattr(config_module, "config_writer_lock", tracking_lock)
    monkeypatch.setitem(
        ns, "_reconcile_codex_budget_on_config_write",
        lambda budget: calls.append((state["inside"], budget)),
    )

    assert ns["cmd_config"](argparse.Namespace(
        action="set", key="budget.codex.amount_usd", value="200", emit_json=False,
    )) == 0
    assert calls == [(False, {
        "codex": {
            "amount_usd": 200.0,
            "period": "calendar-month",
            "alerts_enabled": False,
            "alert_thresholds": [90, 100],
            "projected_enabled": False,
        },
    })]


def test_codex_leaf_reconcile_performs_one_real_codex_action_outside_lock(
    tmp_path, monkeypatch,
):
    """A configured Codex leaf reaches the real forward-only reconciler once."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    config_module = ns["_cctally_config"]
    milestones = ns["_cctally_milestones"]
    state = {"inside": False}
    calls: list[tuple[bool, dict[str, object]]] = []

    @contextmanager
    def tracking_lock():
        state["inside"] = True
        try:
            yield
        finally:
            state["inside"] = False

    class _Connection:
        def close(self):
            pass

    def reconcile(_conn, **kwargs):
        calls.append((state["inside"], kwargs))

    monkeypatch.setattr(config_module, "config_writer_lock", tracking_lock)
    monkeypatch.setattr(milestones, "open_db", _Connection)
    monkeypatch.setattr(milestones, "_reconcile_budget_milestones_on_set", reconcile)

    # The amount write retains the default alerts-off state.  Toggling alerts
    # then has to traverse the full budget object and invoke the downstream
    # reconciler exactly once, after the config lock has been released.
    assert ns["cmd_config"](argparse.Namespace(
        action="set", key="budget.codex.amount_usd", value="200", emit_json=False,
    )) == 0
    assert calls == []
    assert ns["cmd_config"](argparse.Namespace(
        action="set", key="budget.codex.alerts_enabled", value="true", emit_json=False,
    )) == 0

    assert len(calls) == 1
    inside_lock, kwargs = calls[0]
    assert inside_lock is False
    assert {
        "vendor": kwargs["vendor"],
        "target": kwargs["target"],
        "thresholds": kwargs["thresholds"],
        "period": kwargs["period"],
    } == {
        "vendor": "codex",
        "target": 200.0,
        "thresholds": [90, 100],
        "period": "calendar-month",
    }


def test_main_codex_range_and_cache_apply_qualified_project_filter(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = _provider_entries(ns)
    _install_provider_entries(ns, monkeypatch, entries)
    project_key = entries[1].project_key

    assert ns["main"]([
        "range-cost", "--source", "codex", "--start", "2026-07-14T00:00:00Z",
        "--end", "2026-07-16T00:00:00Z", "--project", project_key, "--breakdown", "--json",
    ]) == 0
    range_wire = json.loads(capsys.readouterr().out)
    assert range_wire["data"]["totals"]["costUsd"] == 3.0
    assert [row["model"] for row in range_wire["data"]["models"]] == ["gpt-5.5"]

    assert ns["main"]([
        "cache-report", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16", "--project", project_key, "--json",
    ]) == 0
    cache_wire = json.loads(capsys.readouterr().out)
    rows = cache_wire["data"]["sections"][0]["data"]["rows"]
    assert [row["label"] for row in rows] == ["2026-07-15"]


def test_main_codex_and_all_range_json_keep_frozen_models_key_when_not_breaking_down(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter,
        "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result(
            "claude", "empty", {"totalCostUSD": 0.0, "modelBreakdowns": []},
        ),
    )
    bounds = ["--start", "2026-07-14T00:00:00Z", "--end", "2026-07-16T00:00:00Z"]

    assert ns["main"](["range-cost", "--source", "codex", *bounds, "--json"]) == 0
    direct = json.loads(capsys.readouterr().out)
    assert list(direct["data"])[-1] == "models"
    assert direct["data"]["models"] == []

    assert ns["main"](["range-cost", "--source", "all", *bounds, "--json"]) == 0
    combined = json.loads(capsys.readouterr().out)
    codex = combined["sources"][1]
    assert list(codex["data"])[-1] == "models"
    assert codex["data"]["models"] == []

    _install_provider_entries(ns, monkeypatch, ())
    assert ns["main"](["range-cost", "--source", "codex", *bounds, "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["status"] == "empty"
    assert empty["data"]["models"] == []


def test_main_codex_project_rejects_ambiguous_display_label(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = list(_provider_entries(ns))
    entries[1] = entries[1].__class__(
        **{**entries[1].__dict__, "project_label": "alpha"},
    )
    _install_provider_entries(ns, monkeypatch, entries)

    assert ns["main"]([
        "project", "--source", "codex", "--project", "alpha",
    ]) == 2
    captured = capsys.readouterr()
    assert "display label 'alpha' is ambiguous" in captured.err
    assert captured.out == ""


def test_main_codex_project_collision_labels_stay_distinct_in_terminal_and_share(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = list(_provider_entries(ns))
    entries[1] = entries[1].__class__(
        **{**entries[1].__dict__, "project_label": "alpha"},
    )
    _install_provider_entries(ns, monkeypatch, entries)
    argv = [
        "project", "--source", "codex", "--since", "2026-07-14",
        "--until", "2026-07-16",
    ]
    assert ns["main"](argv) == 0
    terminal = capsys.readouterr().out
    assert "- alpha (1):" in terminal
    assert "- alpha (2):" in terminal

    assert ns["main"]([
        *argv, "--format", "md", "--output", "-", "--reveal-projects",
    ]) == 0
    share = capsys.readouterr().out
    assert "alpha (1)" in share
    assert "alpha (2)" in share
    assert "root-a" not in share and "root-b" not in share


def test_emitted_collision_label_round_trips_through_project_json_terminal_and_share(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = list(_provider_entries(ns))
    entries[1] = entries[1].__class__(
        **{**entries[1].__dict__, "project_label": "alpha"},
    )
    _install_provider_entries(ns, monkeypatch, entries)
    base = ["project", "--source", "codex", "--since", "2026-07-14", "--until", "2026-07-16"]

    assert ns["main"]([*base, "--json"]) == 0
    initial = json.loads(capsys.readouterr().out)
    labels = [row["displayLabel"] for row in initial["data"]["projects"]]
    assert set(labels) == {"alpha (1)", "alpha (2)"}

    for label in labels:
        assert ns["main"]([*base, "--project", label, "--json"]) == 0
        selected = json.loads(capsys.readouterr().out)
        assert [row["displayLabel"] for row in selected["data"]["projects"]] == [label]

    assert ns["main"]([*base, "--project", labels[0]]) == 0
    terminal = capsys.readouterr().out
    assert f"- {labels[0]}:" in terminal
    assert f"- {labels[1]}:" not in terminal

    assert ns["main"]([
        *base, "--project", labels[1], "--format", "md", "--output", "-", "--reveal-projects",
    ]) == 0
    share = capsys.readouterr().out
    assert labels[1] in share
    assert labels[0] not in share


def test_codex_diff_collision_labels_round_trip_to_json_terminal_and_share(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = list(_provider_entries(ns))
    entries[1] = entries[1].__class__(
        **{**entries[1].__dict__, "project_label": "alpha"},
    )
    _install_provider_entries(ns, monkeypatch, entries)
    base = [
        "diff", "--source", "codex", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--only", "projects", "--all",
    ]

    assert ns["main"]([*base, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = payload["data"]["sections"][0]["data"]["rows"]
    assert {row["label"] for row in rows} == {"alpha (1)", "alpha (2)"}
    assert len({row["key"] for row in rows}) == 2
    assert {row["status"] for row in rows} == {"dropped", "new"}
    assert "root-a" not in repr(payload) and "root-b" not in repr(payload)

    assert ns["main"](base) == 0
    terminal = capsys.readouterr().out
    assert "alpha (1)" in terminal and "alpha (2)" in terminal
    assert "dropped" in terminal and "new" in terminal

    assert ns["main"]([*base, "--format", "md", "--output", "-"]) == 0
    scrubbed = capsys.readouterr().out
    assert "project-1" in scrubbed and "project-2" in scrubbed
    assert "alpha (1)" not in scrubbed and "alpha (2)" not in scrubbed
    assert "root-a" not in scrubbed and "root-b" not in scrubbed

    assert ns["main"]([*base, "--format", "md", "--output", "-", "--reveal-projects"]) == 0
    revealed = capsys.readouterr().out
    assert "alpha (1)" in revealed and "alpha (2)" in revealed
    assert "root-a" not in revealed and "root-b" not in revealed


def test_all_source_diff_terminal_keeps_native_rows_and_comparison_metrics(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _diff_control_entries(ns))
    source_result = ns["SourceResult"]
    claude_payload = {
        "combined": {
            "cost_usd": {"a": 1.0, "b": 2.0, "delta": 1.0},
            "total_tokens": {"a": 10, "b": 20, "delta": 10},
        },
        "sections": [{
            "name": "models",
            "rows": [{
                "key": "models:claude-test", "label": "claude-test", "status": "changed",
                "a": {"cost_usd": 1.0, "tokens_input": 5, "tokens_output": 5},
                "b": {"cost_usd": 2.0, "tokens_input": 10, "tokens_output": 10},
                "delta": {"cost_usd": 1.0, "tokens_input": 5, "tokens_output": 5},
            }],
        }],
    }
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result("claude", "ok", claude_payload),
    )

    assert ns["main"]([
        "diff", "--source", "all", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--all",
    ]) == 0
    terminal = capsys.readouterr().out
    assert "Claude Diff Report" in terminal
    assert "Codex Diff Report" in terminal
    assert "claude-test" in terminal
    assert "gpt-small" in terminal
    assert "A $1.000000 / 10 tokens; B $2.000000 / 20 tokens; Δ +$1.000000 / +10 tokens" in terminal
    assert "Combined physical accounting: A" in terminal
    assert "A " in terminal and "B " in terminal and "Δ " in terminal


def test_all_requested_project_preserves_claude_and_returns_three_when_codex_unavailable(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result(
            "claude", "empty", {"totalCostUSD": 0.0, "modelBreakdowns": []},
        ),
    )
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            adapter.QualifiedMetadataUnavailable("unavailable"),
        ),
    )

    assert ns["main"]([
        "range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z",
        "--end", "2026-07-15T00:00:00Z", "--project", "alpha", "--json",
    ]) == 3
    wire = json.loads(capsys.readouterr().out)
    assert [(source["source"], source["status"]) for source in wire["sources"]] == [
        ("claude", "empty"), ("codex", "unavailable"),
    ]


def test_provider_terminals_cover_empty_partial_and_unavailable_states(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]

    _install_provider_entries(ns, monkeypatch, ())
    assert ns["main"](["project", "--source", "codex"]) == 0
    assert capsys.readouterr().out == "Codex Project Report\nNo data.\n"

    entries = _provider_entries(ns)
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            adapter.QualifiedMetadataUnavailable("unavailable"),
        ),
    )
    monkeypatch.setattr(adapter, "load_codex_accounting_entries", lambda *_args, **_kwargs: entries)
    assert ns["main"]([
        "cache-report", "--source", "codex", "--since", "2026-07-14", "--until", "2026-07-16",
    ]) == 0
    partial = capsys.readouterr().out
    assert partial.startswith("Codex Token Reuse Report\nPartial: Codex qualified project metadata is unavailable.")
    assert "cached input" in partial

    assert ns["main"](["project", "--source", "codex"]) == 3
    unavailable = capsys.readouterr().out
    assert unavailable == (
        "Codex Project Report\n"
        "Unavailable: Codex qualified project metadata is unavailable.\n"
    )


def test_main_codex_report_sync_current_syncs_once_then_reconciles(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    calls: list[str] = []

    class _Cache:
        def close(self):
            calls.append("close")

    monkeypatch.setitem(ns, "open_cache_db", lambda: _Cache())
    monkeypatch.setitem(ns, "sync_codex_cache", lambda cache: calls.append("sync"))
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **_kwargs: calls.append("reconcile"),
    )
    monkeypatch.setattr(adapter, "load_codex_quota_observations", lambda: ())

    assert ns["main"](["report", "--source", "codex", "--sync-current", "--json"]) == 0
    wire = json.loads(capsys.readouterr().out)
    assert wire["status"] == "empty"
    assert calls == ["sync", "close", "reconcile"]


def test_report_sync_current_contention_keeps_direct_jsonl_accounting_and_one_reconciliation(
    tmp_path, monkeypatch, capsys,
):
    """A contended cache may be partial, so report uses the canonical reader's direct leg."""
    ns = _seed_real_codex_cli_data(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        root_key = conn.execute(
            "SELECT source_root_key FROM codex_source_roots ORDER BY source_root_key LIMIT 1"
        ).fetchone()[0]
        conn.execute("DELETE FROM codex_session_entries")
        conn.commit()
    finally:
        conn.close()
    utc = dt.timezone.utc
    identity = QuotaWindowIdentity(
        source="codex", source_root_key=root_key, logical_limit_key="primary",
        observed_slot="primary", window_minutes=7 * 24 * 60,
    )
    observation = QuotaObservation(
        identity=identity, captured_at=dt.datetime(2026, 7, 15, 11, tzinfo=utc),
        used_percent=10.0, resets_at=dt.datetime(2026, 7, 21, tzinfo=utc),
        source_path="/synthetic/quota.jsonl", line_offset=1,
    )
    adapter = ns["_cctally_source_analytics"]
    monkeypatch.setattr(adapter, "load_codex_quota_observations", lambda: (observation,))
    calls: list[str] = []
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda _cache: calls.append("sync") or ns["CodexIngestStats"](lock_contended=True),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **_kwargs: calls.append("reconcile"),
    )
    direct = ns["_collect_codex_entries_direct"]
    monkeypatch.setitem(
        ns, "_collect_codex_entries_direct",
        lambda start, end: calls.append("direct") or direct(start, end),
    )

    assert ns["main"](["report", "--source", "codex", "--sync-current", "--json"]) == 0
    wire = json.loads(capsys.readouterr().out)
    row = wire["data"]["sections"][0]["data"]["series"][0]["rows"][0]
    assert row["costUsd"] > 0
    assert calls.count("sync") == 1
    assert calls.count("reconcile") == 1
    assert calls.count("direct") == 1


def test_main_all_diff_normalizes_each_only_section_to_its_provider_leg(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    source_result = ns["SourceResult"]
    claude_only: list[str | None] = []
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda args, _command: (
            claude_only.append(args.only)
            or source_result("claude", "empty", {"sections": []})
        ),
    )

    assert ns["main"]([
        "diff", "--source", "all", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--only", "cache,models,token-reuse", "--json",
    ]) == 0
    wire = json.loads(capsys.readouterr().out)

    assert claude_only == ["cache,models"]
    assert [section["key"] for section in wire["sources"][1]["data"]["sections"]] == [
        "models", "token-reuse",
    ]


@pytest.mark.parametrize("source", ["codex", "all"])
def test_main_diff_without_sync_keeps_the_codex_cache_read_unsynced(
    tmp_path, monkeypatch, capsys, source,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    reads: list[bool] = []
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, sync, **_kwargs: reads.append(sync) or _provider_entries(ns),
    )
    if source == "all":
        source_result = ns["SourceResult"]
        monkeypatch.setattr(
            adapter, "_run_claude_json_adapter",
            lambda *_args, **_kwargs: source_result("claude", "empty", {"sections": []}),
        )

    assert ns["main"]([
        "diff", "--source", source, "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--json",
    ]) == 0
    capsys.readouterr()
    assert reads == [False]


def test_main_all_diff_syncs_each_provider_once_at_the_command_boundary(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    calls: list[tuple[str, bool]] = []
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, sync, **_kwargs: calls.append(("codex", sync)) or _provider_entries(ns),
    )
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda args, _command: (
            calls.append(("claude", args.sync))
            or source_result("claude", "empty", {"sections": []})
        ),
    )

    assert ns["main"]([
        "diff", "--source", "all", "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--sync", "--json",
    ]) == 0
    capsys.readouterr()
    assert calls == [("claude", True), ("codex", True)]


@pytest.mark.parametrize(
    ("source", "only"),
    [
        ("codex", ""),
        ("all", ""),
        ("codex", " \t "),
        ("all", ", ,"),
    ],
)
def test_main_rejects_explicit_empty_diff_only_before_provider_io(
    source, only, tmp_path, monkeypatch, capsys,
):
    """An explicit empty section list is usage, never a sync/read request."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    calls: list[tuple[str, bool | None]] = []
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, sync, **_kwargs: calls.append(("codex", sync)) or (),
    )
    monkeypatch.setattr(
        adapter, "load_codex_accounting_entries",
        lambda *_args, sync, **_kwargs: calls.append(("accounting", sync)) or (),
    )
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: (
            calls.append(("claude", None))
            or source_result("claude", "empty", {"sections": []})
        ),
    )

    assert ns["main"]([
        "diff", "--source", source, "--a", "2026-07-14..2026-07-14",
        "--b", "2026-07-15..2026-07-15", "--only", only, "--sync", "--json",
    ]) == 2
    captured = capsys.readouterr()
    assert "diff --only specified no sections" in captured.err
    assert captured.out == ""
    assert calls == []


def test_main_codex_range_breakdown_controls_json_and_terminal_models(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    bounds = ["--start", "2026-07-14T00:00:00Z", "--end", "2026-07-16T00:00:00Z"]

    assert ns["main"](["range-cost", "--source", "codex", *bounds, "--json"]) == 0
    compact = json.loads(capsys.readouterr().out)
    assert compact["data"]["models"] == []

    assert ns["main"](["range-cost", "--source", "codex", *bounds, "--breakdown"]) == 0
    expanded = capsys.readouterr().out
    assert "gpt-5" in expanded and "gpt-5.5" in expanded


def test_main_all_range_total_only_sums_each_physical_provider_once(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result(
            "claude", "ok", {"totalCostUSD": 2.5, "modelBreakdowns": []},
        ),
    )

    assert ns["main"]([
        "range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z",
        "--end", "2026-07-16T00:00:00Z", "--breakdown", "--total-only", "--json",
    ]) == 0
    assert capsys.readouterr().out == "6.500000000\n"


@pytest.mark.parametrize("source", ["codex", "all"])
def test_range_cost_total_only_renders_requested_share_artifact_before_bare_number(
    source, tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    if source == "all":
        monkeypatch.setattr(
            adapter, "_run_claude_json_adapter",
            lambda *_args, **_kwargs: ns["SourceResult"](
                "claude", "ok", {"totalCostUSD": 2.5, "modelBreakdowns": []},
            ),
        )
    bounds = ["--start", "2026-07-14T00:00:00Z", "--end", "2026-07-16T00:00:00Z"]
    base = ["range-cost", "--source", source, *bounds, "--total-only"]

    assert ns["main"]([*base, "--format", "md", "--output", "-"]) == 0
    stdout_artifact = capsys.readouterr().out
    assert "# Range Cost Report" in stdout_artifact
    assert "Codex" in stdout_artifact
    assert stdout_artifact.strip() != "4.000000000"

    output = tmp_path / f"{source}-range.md"
    assert ns["main"]([*base, "--format", "md", "--output", str(output)]) == 0
    assert capsys.readouterr().out == ""
    assert "# Range Cost Report" in output.read_text(encoding="utf-8")

    assert ns["main"]([*base, "--json"]) == 0
    assert float(capsys.readouterr().out) > 0


def test_all_cache_reuse_sort_normalizes_each_provider_leg_independently(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    entries = list(_provider_entries(ns))
    entries.append(entries[0].__class__(**{
        **entries[0].__dict__,
        "timestamp": dt.datetime(2026, 7, 16, 12, tzinfo=dt.timezone.utc),
        "conversation_key": "conversation-c",
        "project_key": "project:" + "c" * 24,
        "project_label": "gamma",
        "cached_input_tokens": 50,
    }))
    adapter = _install_provider_entries(ns, monkeypatch, entries)
    claude_sorts: list[str | None] = []
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda args, _command: (
            claude_sorts.append(args.sort)
            or ns["SourceResult"]("claude", "empty", {"days": []})
        ),
    )
    base = [
        "cache-report", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-16",
        "--json",
    ]

    assert ns["main"]([*base, "--sort", "reuse"]) == 0
    reuse = json.loads(capsys.readouterr().out)
    rows = reuse["sources"][1]["data"]["sections"][0]["data"]["rows"]
    assert claude_sorts == [None]
    assert [row["label"] for row in rows] == ["2026-07-15", "2026-07-16", "2026-07-14"]

    assert ns["main"]([*base, "--sort", "net"]) == 0
    defaulted = json.loads(capsys.readouterr().out)
    assert claude_sorts == [None, "net"]
    assert defaulted["sources"][1]["data"]["sections"][0]["data"]["rows"][0]["label"] == "2026-07-14"


def test_main_codex_and_all_report_detail_stays_with_each_logical_limit_block(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-16T00:00:00Z")
    adapter = _install_provider_entries(ns, monkeypatch, _provider_entries(ns))
    monkeypatch.setattr(adapter, "load_codex_quota_observations", _provider_quota_observations)

    assert ns["main"](["report", "--source", "codex", "--weeks", "1", "--detail", "--json"]) == 0
    codex = json.loads(capsys.readouterr().out)
    detail = codex["data"]["sections"][0]["data"]["series"][0]["rows"][0]["detail"]
    assert detail and detail[0]["percentThreshold"] == 11
    assert "combined" not in codex

    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result("claude", "empty", {"current": None, "trend": []}),
    )
    assert ns["main"](["report", "--source", "all", "--weeks", "1", "--detail", "--json"]) == 0
    combined = json.loads(capsys.readouterr().out)
    assert "combined" not in combined
    assert combined["sources"][1]["data"]["sections"][0]["data"]["series"][0]["rows"][0]["detail"]


@pytest.mark.parametrize("source", ["codex", "all"])
def test_main_cache_report_offline_does_not_disable_the_codex_read_sync(
    tmp_path, monkeypatch, capsys, source,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    reads: list[bool] = []
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, sync, **_kwargs: reads.append(sync) or _provider_entries(ns),
    )
    if source == "all":
        source_result = ns["SourceResult"]
        monkeypatch.setattr(
            adapter, "_run_claude_json_adapter",
            lambda *_args, **_kwargs: source_result("claude", "empty", {"days": []}),
        )

    assert ns["main"]([
        "cache-report", "--source", source, "--offline", "--since", "2026-07-14",
        "--until", "2026-07-16", "--json",
    ]) == 0
    capsys.readouterr()
    assert reads == [True]


def test_main_all_report_offline_does_not_disable_the_codex_accounting_sync(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-07-16T00:00:00Z")
    adapter = ns["_cctally_source_analytics"]
    reads: list[bool] = []
    source_result = ns["SourceResult"]
    monkeypatch.setattr(adapter, "load_codex_quota_observations", _provider_quota_observations)
    monkeypatch.setattr(
        adapter, "load_codex_accounting_entries",
        lambda *_args, sync, **_kwargs: reads.append(sync) or _provider_entries(ns),
    )
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda args, _command: (
            (args.offline is True)
            and source_result("claude", "empty", {"current": None, "trend": []})
        ),
    )

    assert ns["main"](["report", "--source", "all", "--offline", "--json"]) == 0
    capsys.readouterr()
    assert reads == [True]


@pytest.mark.parametrize("command,argv,attribute,expected", [
    ("project", ["--weeks", "2", "--model", "gpt", "--group", "full-path", "--breakdown"], "weeks", 2),
    ("diff", ["--a", "last-7d", "--b", "prev-7d", "--only", "overall,models", "--sync"], "sync", True),
    ("range-cost", ["--start", "2099-01-01T00:00:00Z", "--mode", "calculate", "--breakdown", "--total-only"], "total_only", True),
    ("cache-report", ["--by-session", "--offline", "--sort", "reuse", "--project", "repo"], "by_session", True),
    ("report", ["--weeks", "2", "--sync-current", "--detail", "--offline", "--mode", "calculate", "--project", "repo"], "detail", True),
])
def test_matrix_accepted_all_options_reach_the_real_provider_dispatch(
    command, argv, attribute, expected, monkeypatch,
):
    """Audit one non-default accepted option from every 10.1 matrix family."""
    ns = load_script()
    adapter_name = "cmd_source_" + command.replace("-", "_")
    seen: list[object] = []
    monkeypatch.setitem(ns, adapter_name, lambda args: seen.append(args) or 0)

    assert ns["main"]([command, "--source", "all", *argv]) == 0
    assert len(seen) == 1
    assert getattr(seen[0], attribute) == expected


@pytest.mark.parametrize("command", ["range-cost", "cache-report"])
def test_all_requested_project_degradation_keeps_sections_on_stdout_and_reason_on_stderr(
    command, tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    adapter = ns["_cctally_source_analytics"]
    source_result = ns["SourceResult"]
    monkeypatch.setattr(
        adapter, "_run_claude_json_adapter",
        lambda *_args, **_kwargs: source_result(
            "claude", "empty", {"totalCostUSD": 0.0, "modelBreakdowns": []},
        ),
    )
    monkeypatch.setattr(
        adapter, "load_qualified_codex_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            adapter.QualifiedMetadataUnavailable("unavailable"),
        ),
    )
    argv = (
        ["range-cost", "--source", "all", "--start", "2026-07-14T00:00:00Z", "--end", "2026-07-15T00:00:00Z"]
        if command == "range-cost" else
        ["cache-report", "--source", "all", "--since", "2026-07-14", "--until", "2026-07-15"]
    )

    assert ns["main"]([*argv, "--project", "alpha"]) == 3
    captured = capsys.readouterr()
    assert f"Claude {'Range Cost' if command == 'range-cost' else 'Token Reuse'} Report" in captured.out
    assert f"Codex {'Range Cost' if command == 'range-cost' else 'Token Reuse'} Report\nUnavailable." in captured.out
    assert captured.err == "cctally: Codex qualified project metadata is unavailable.\n"


def test_parser_flat_defaults_and_provider_subgroups_are_fixed_aliases():
    parser = _parser()

    flat = parser.parse_args(["project"])
    explicit = parser.parse_args(["project", "--source", "claude"])
    codex = parser.parse_args(["codex", "project"])
    claude = parser.parse_args(["claude", "project"])

    assert flat.source == explicit.source == claude.source == "claude"
    assert codex.source == "codex"
    assert flat.speed == "auto"
    with pytest.raises(SystemExit):
        parser.parse_args(["codex", "project", "--source", "claude"])


def test_parser_source_flag_matrix_preserves_existing_options_and_adds_reuse():
    parser = _parser()

    assert parser.parse_args([
        "cache-report", "--source", "codex", "--sort", "reuse", "--speed", "fast",
    ]).sort == "reuse"
    assert parser.parse_args([
        "report", "--source", "all", "--speed", "standard", "--detail",
    ]).source == "all"
    assert parser.parse_args([
        "range-cost", "--start", "2099-01-01T00:00:00Z",
        "--source", "claude", "--mode", "auto",
    ]).source == "claude"


def test_source_validation_rejects_claude_only_non_default_options_before_io():
    ns = load_script()
    parser = ns["build_parser"]()

    with pytest.raises(ValueError, match="--speed"):
        ns["_validate_source_args"](
            parser.parse_args(["project", "--source", "claude", "--speed", "fast"])
        )
    with pytest.raises(ValueError, match="--mode"):
        ns["_validate_source_args"](
            parser.parse_args([
                "range-cost", "--start", "2099-01-01T00:00:00Z",
                "--source", "codex", "--mode", "calculate",
            ])
        )


@pytest.mark.parametrize("source", ("codex", "all"))
@pytest.mark.parametrize("weeks", (0, -1))
def test_source_report_rejects_nonpositive_weeks_before_provider_io(
    source, weeks, monkeypatch, capsys,
):
    """Codex/all report validation cannot degrade into a quota read."""
    ns = load_script()
    adapter = ns["_cctally_source_analytics"]
    touched: list[str] = []

    def forbidden(name):
        def fail(*_args, **_kwargs):
            touched.append(name)
            pytest.fail(f"nonpositive report --weeks performed {name}")
        return fail

    monkeypatch.setattr(adapter, "_run_claude_json_adapter", forbidden("claude handler"))
    monkeypatch.setattr(adapter, "load_codex_quota_observations", forbidden("quota reader"))
    monkeypatch.setattr(adapter, "_source_entries", forbidden("accounting reader"))
    monkeypatch.setitem(ns, "sync_codex_cache", forbidden("cache sync"))
    monkeypatch.setitem(ns, "reconcile_codex_quota_projection", forbidden("quota reconcile"))

    assert ns["main"]([
        "report", "--source", source, "--weeks", str(weeks), "--json",
    ]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "cctally: report --weeks must be a positive integer\n"
    assert touched == []


def test_source_report_nonpositive_weeks_keeps_claude_legacy_validation():
    ns = load_script()
    args = ns["build_parser"]().parse_args([
        "report", "--source", "claude", "--weeks", "0",
    ])

    ns["_validate_source_args"](args)


def test_source_validation_does_not_apply_to_legacy_codex_report_speed():
    ns = load_script()
    args = ns["build_parser"]().parse_args(["codex-daily", "--speed", "fast"])

    ns["_validate_source_args"](args)


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["project", "--source", "codex", "--sort", "used"], "--sort used"),
        (["project", "--source", "all", "--sort", "used"], "--sort used"),
        (["cache-report", "--source", "claude", "--sort", "reuse"], "--sort reuse"),
        (["cache-report", "--source", "codex", "--sort", "net"], "--sort net"),
        (["diff", "--source", "claude", "--a", "last-7d", "--b", "prev-7d", "--only", "token-reuse"], "token-reuse"),
        (["diff", "--source", "codex", "--a", "last-7d", "--b", "prev-7d", "--only", "cache"], "--only cache"),
        (["report", "--source", "codex", "--mode", "calculate"], "--mode"),
    ],
)
def test_source_validation_rejects_inapplicable_provider_flags(argv, error):
    ns = load_script()
    args = ns["build_parser"]().parse_args(argv)

    with pytest.raises(ValueError, match=error):
        ns["_validate_source_args"](args)


@pytest.mark.parametrize(
    "argv",
    [
        ["cache-report", "--source", "all", "--sort", "reuse", "--speed", "fast"],
        ["diff", "--source", "all", "--a", "last-7d", "--b", "prev-7d", "--only", "token-reuse"],
        ["range-cost", "--source", "all", "--start", "2099-01-01T00:00:00Z", "--mode", "calculate"],
    ],
)
def test_source_validation_allows_provider_leg_options_on_all(argv):
    ns = load_script()

    ns["_validate_source_args"](ns["build_parser"]().parse_args(argv))


@pytest.mark.parametrize("command", ["codex-daily", "codex-monthly", "codex-weekly", "codex-session"])
def test_codex_report_parser_accepts_config_and_share_flags(command):
    parser = _parser()

    args = parser.parse_args([command, "--config", "/tmp/cfg.json", "--format", "md"])

    assert args.config == "/tmp/cfg.json"
    assert args.format == "md"
    with pytest.raises(SystemExit):
        parser.parse_args([command, "--format", "md", "--json"])


@pytest.mark.parametrize("command", ["codex-daily", "codex-monthly", "codex-weekly", "codex-session"])
def test_codex_report_config_path_compatibility_bytes_match_equivalent_default(tmp_path, command):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"display": {"tz": "utc"}}), encoding="utf-8")
    command_args = (command, "--since", "20990101", "--until", "20990102", "--tz", "utc")

    # Each process gets a fresh data dir so both sides include the same
    # first-open cache-migration diagnostic in their captured stderr bytes.
    baseline = _run_cli(tmp_path / "default", *command_args)
    with_config = _run_cli(tmp_path / "explicit", *command_args, "--config", str(config))

    assert (with_config.returncode, with_config.stdout, with_config.stderr) == (
        baseline.returncode, baseline.stdout, baseline.stderr,
    )


def test_codex_leaf_thresholds_sort_and_dedupe(tmp_path):
    _run_cli(tmp_path, "config", "set", "budget.codex.amount_usd", "200")

    out = _run_cli(tmp_path, "config", "set", "budget.codex.alert_thresholds", "100,90,90")

    assert out.returncode == 0, out.stderr
    assert out.stdout == "budget.codex.alert_thresholds=90,100\n"


def test_codex_leaf_json_is_unversioned_nested_echo(tmp_path):
    _run_cli(tmp_path, "config", "set", "budget.codex.amount_usd", "200")

    out = _run_cli(tmp_path, "config", "get", "budget.codex.period", "--json")

    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"budget": {"codex": {"period": "calendar-month"}}}
    assert "schemaVersion" not in out.stdout


def test_codex_leaf_requires_amount_and_unset_restores_default(tmp_path):
    missing = _run_cli(tmp_path, "config", "set", "budget.codex.period", "calendar-week")
    assert missing.returncode == 2

    assert _run_cli(tmp_path, "config", "set", "budget.codex.amount_usd", "200").returncode == 0
    assert _run_cli(tmp_path, "config", "set", "budget.codex.period", "calendar-week").returncode == 0
    assert _run_cli(tmp_path, "config", "unset", "budget.codex.period").returncode == 0
    restored = _run_cli(tmp_path, "config", "get", "budget.codex.period")
    assert restored.stdout == "budget.codex.period=calendar-month\n"


def test_codex_leaf_refuses_corrupt_existing_block_without_mutation(tmp_path):
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir(parents=True)
    original = '{"budget":{"codex":{"amount_usd":"bad"}}}\n'
    config_path.write_text(original, encoding="utf-8")

    result = _run_cli(tmp_path, "config", "set", "budget.codex.period", "calendar-week")

    assert result.returncode == 2
    assert config_path.read_text(encoding="utf-8") == original


def test_codex_leaf_drops_unknown_siblings_through_existing_validator(tmp_path):
    config_path = tmp_path / "data" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"budget":{"codex":{"amount_usd":200,"future_leaf":true}}}\n',
        encoding="utf-8",
    )

    result = _run_cli(tmp_path, "config", "set", "budget.codex.period", "calendar-week")

    assert result.returncode == 0, result.stderr
    assert "ignoring unknown budget.codex config key: future_leaf" in result.stderr
    assert "future_leaf" not in config_path.read_text(encoding="utf-8")


def test_codex_leaf_mutations_reconcile_once_per_remaining_configured_block(
    tmp_path, monkeypatch, capsys,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    calls: list[dict] = []
    monkeypatch.setitem(ns, "_reconcile_codex_budget_on_config_write", lambda block: calls.append(block))

    def invoke(action: str, key: str, value: str | None = None) -> int:
        return ns["cmd_config"](argparse.Namespace(
            action=action, key=key, value=value, emit_json=False,
        ))

    assert invoke("set", "budget.codex.amount_usd", "200") == 0
    assert invoke("set", "budget.codex.alerts_enabled", "true") == 0
    assert invoke("unset", "budget.codex.alerts_enabled") == 0
    assert len(calls) == 3
    assert all(call["codex"]["amount_usd"] == 200.0 for call in calls)
    assert invoke("unset", "budget.codex.amount_usd") == 0
    assert len(calls) == 3
    capsys.readouterr()
