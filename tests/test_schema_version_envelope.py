"""schemaVersion envelope regressions for the reporting --json surfaces that
have no dedicated golden harness (#279 S6 W1), plus the empty/edge emitters
(gate F1). Golden-covered surfaces (weekly/session/codex-*/blocks main/forecast
main/budget status/project/cache-report main) are pinned by their harness
re-bless; these subprocess checks cover the rest and the empty forms.

Each command runs against a fresh, isolated data dir (empty Claude corpus) so
the outputs are the empty/edge shapes. Only stdout is parsed (the cache
migration banner rides stderr).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally"


@pytest.fixture(scope="module")
def isolated_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("s6-envelope")
    data = base / "data"
    claude = base / "claude"
    (claude / "projects").mkdir(parents=True)
    data.mkdir(parents=True)
    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(data)
    env["CLAUDE_CONFIG_DIR"] = str(claude)
    return env


def _run_json(env, *args):
    proc = subprocess.run(
        [sys.executable, str(BIN), *args],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"{args} exited {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout)


def test_blocks_active_empty_json_stamped(isolated_env):
    out = _run_json(isolated_env, "blocks", "--active", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1
    assert out["blocks"] == []
    assert out["message"] == "No active block"


def test_report_empty_json_stamped(isolated_env):
    out = _run_json(isolated_env, "report", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out == {"schemaVersion": 1, "current": None, "trend": []}


def test_cache_report_empty_window_json_stamped(isolated_env):
    out = _run_json(
        isolated_env,
        "cache-report", "--since", "2030-01-01", "--until", "2030-01-01", "--json",
    )
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1
    assert out["totals"] is None


def test_percent_breakdown_json_stamped(isolated_env):
    out = _run_json(isolated_env, "percent-breakdown", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1


def test_range_cost_json_stamped(isolated_env):
    out = _run_json(
        isolated_env, "range-cost",
        "--start", "2026-07-01T00:00:00Z", "--end", "2026-07-02T00:00:00Z",
        "--json",
    )
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1


def test_sync_week_json_stamped(isolated_env):
    out = _run_json(isolated_env, "sync-week", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1


def test_telemetry_json_stamped(isolated_env):
    out = _run_json(isolated_env, "telemetry", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1


def test_budget_set_json_stamped(isolated_env):
    out = _run_json(isolated_env, "budget", "set", "100", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1
    assert out["status"] == "set"


def test_budget_unset_json_stamped(isolated_env):
    out = _run_json(isolated_env, "budget", "unset", "--json")
    assert list(out.keys())[0] == "schemaVersion"
    assert out["schemaVersion"] == 1
    assert out["status"] == "unset"
