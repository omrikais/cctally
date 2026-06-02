"""Per-project budget config + schema foundation (Task 1, spec §4.1/§4.2/§5.1).

Three coverage blocks:
  1. ``_get_budget_config`` validation of the two new leaves
     (``budget.projects`` dict-of-str→positive-number, and
     ``budget.project_alerts_enabled`` bool), exercised through the
     isolated kernel loader so a cached ``_cctally_core`` never reads the
     real prod DB ([HOME-only test loader reads prod DB] gotcha).
  2. The new ``project_budget_milestones`` table + the
     ``insert_project_budget_milestone`` helper's UNIQUE-dedup rowcount
     contract (1 on a new (week, project, threshold), 0 on a repeat, 1 for
     a distinct project_key under the same (week, threshold)).
  3. ``config get/set/unset`` round-trips for both new leaves via the CLI
     (a real subprocess against a scratch ``CCTALLY_DATA_DIR`` — mirrors
     ``tests/test_alerts_config_projected.py``'s ``_run_cli``). ``projects``
     is a dict, so it round-trips as JSON; ``project_alerts_enabled`` is a
     plain boolean leaf.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


# ── Block 1: _get_budget_config validation of the new leaves ─────────────────
#
# Loaded through load_script() + redirect_paths() so the kernel's path
# constants point at the per-test tmp dir, NOT the developer's real
# ~/.local/share/cctally (the [HOME-only test loader reads prod DB] gotcha).


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_defaults_project_keys(ns):
    """An absent budget block surfaces the new defaults: projects {} and
    project_alerts_enabled False (opt-in, default OFF — projected-axis
    precedent)."""
    cfg = ns["_get_budget_config"]({})
    assert cfg["projects"] == {}
    assert cfg["project_alerts_enabled"] is False


def test_projects_must_be_object(ns):
    """A list value (not an object) for budget.projects is rejected."""
    with pytest.raises(ns["_BudgetConfigError"]):
        ns["_get_budget_config"]({"budget": {"projects": [1, 2]}})


def test_projects_negative_value_rejected(ns):
    """A non-positive value for a project budget is rejected."""
    with pytest.raises(ns["_BudgetConfigError"]):
        ns["_get_budget_config"]({"budget": {"projects": {"/a": -5}}})


def test_projects_bool_value_rejected(ns):
    """A bool value (True) is NOT a valid numeric budget — rejected (mirrors
    the weekly_usd / alert_thresholds bool rejection)."""
    with pytest.raises(ns["_BudgetConfigError"]):
        ns["_get_budget_config"]({"budget": {"projects": {"/a": True}}})


def test_projects_valid_value_coerced_to_float(ns):
    """A positive number is accepted and returned float-coerced, keyed by the
    canonical git-root path string."""
    cfg = ns["_get_budget_config"]({"budget": {"projects": {"/a": 25.0}}})
    assert cfg["projects"] == {"/a": 25.0}
    assert isinstance(cfg["projects"]["/a"], float)


def test_project_alerts_enabled_must_be_bool(ns):
    """A string ("yes") for project_alerts_enabled is rejected — it must be a
    real JSON bool (mirrors alerts_enabled / projected_enabled)."""
    with pytest.raises(ns["_BudgetConfigError"]):
        ns["_get_budget_config"](
            {"budget": {"project_alerts_enabled": "yes"}}
        )


# ── Block 2: project_budget_milestones table + insert helper ─────────────────


def test_insert_project_budget_milestone_dedup_rowcount(ns):
    """The helper's INSERT OR IGNORE returns rowcount 1 on a genuinely new
    (week, project, threshold), 0 on a repeat of the SAME triple, and 1 again
    for a DIFFERENT project_key under the same (week, threshold) — proving
    project_key is part of the UNIQUE dedup key."""
    insert = ns["insert_project_budget_milestone"]
    conn = ns["open_db"]()
    try:
        week = "2026-06-02T00:00:00+00:00"
        first = insert(
            conn,
            week_start_at=week,
            project_key="/repos/foo",
            threshold=100,
            budget_usd=25.0,
            spent_usd=26.0,
            consumption_pct=104.0,
            commit=False,
        )
        assert first == 1  # genuinely new crossing

        repeat = insert(
            conn,
            week_start_at=week,
            project_key="/repos/foo",
            threshold=100,
            budget_usd=25.0,
            spent_usd=27.0,
            consumption_pct=108.0,
            commit=False,
        )
        assert repeat == 0  # UNIQUE(week, project_key, threshold) dedup

        other_project = insert(
            conn,
            week_start_at=week,
            project_key="/repos/bar",
            threshold=100,
            budget_usd=50.0,
            spent_usd=51.0,
            consumption_pct=102.0,
            commit=True,
        )
        assert other_project == 1  # distinct project_key → distinct row

        rows = conn.execute(
            "SELECT project_key, threshold, budget_usd, spent_usd, "
            "consumption_pct, alerted_at FROM project_budget_milestones "
            "ORDER BY project_key"
        ).fetchall()
        assert len(rows) == 2
        # alerted_at left NULL by the insert (caller stamps it — set-then-dispatch).
        assert all(r["alerted_at"] is None for r in rows)
    finally:
        conn.close()


# ── Block 3: config get/set/unset round-trip via the CLI ─────────────────────
#
# A real subprocess against a scratch CCTALLY_DATA_DIR (mirrors
# tests/test_alerts_config_projected.py::_run_cli) so the persisted
# config.json shape is exercised end-to-end.


def _run_cli(data_dir, *args):
    import os

    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(data_dir)
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    return subprocess.run(
        [sys.executable, str(_BIN / "cctally"), *args],
        capture_output=True, text=True, env=env,
    )


def test_config_projects_json_round_trip(tmp_path):
    """`config set budget.projects '<json-object>'` persists, and
    `config get budget.projects` emits JSON that parses back to the
    float-coerced map."""
    set_res = _run_cli(
        tmp_path, "config", "set", "budget.projects", '{"/a": 25}'
    )
    assert set_res.returncode == 0, set_res.stderr
    get_res = _run_cli(tmp_path, "config", "get", "budget.projects")
    assert get_res.returncode == 0, get_res.stderr
    # Output is `budget.projects=<json>`; the value parses to the float map.
    rhs = get_res.stdout.strip().split("=", 1)[1]
    assert json.loads(rhs) == {"/a": 25.0}


def test_config_project_alerts_enabled_bool_round_trip(tmp_path):
    """`config set budget.project_alerts_enabled true` round-trips as the
    canonical boolean leaf (`true`)."""
    set_res = _run_cli(
        tmp_path, "config", "set", "budget.project_alerts_enabled", "true"
    )
    assert set_res.returncode == 0, set_res.stderr
    get_res = _run_cli(
        tmp_path, "config", "get", "budget.project_alerts_enabled"
    )
    assert get_res.returncode == 0, get_res.stderr
    assert get_res.stdout.strip().endswith("=true")


def test_config_projects_non_object_exit_2(tmp_path):
    """A JSON array (non-object) for budget.projects is rejected with exit 2."""
    res = _run_cli(tmp_path, "config", "set", "budget.projects", "[1,2]")
    assert res.returncode == 2, res.stdout + res.stderr


def test_config_unset_projects_clears_leaf(tmp_path):
    """`config unset budget.projects` drops the leaf, leaving the {} default."""
    _run_cli(tmp_path, "config", "set", "budget.projects", '{"/a": 25}')
    unset_res = _run_cli(tmp_path, "config", "unset", "budget.projects")
    assert unset_res.returncode == 0, unset_res.stderr
    get_res = _run_cli(tmp_path, "config", "get", "budget.projects")
    assert get_res.returncode == 0, get_res.stderr
    rhs = get_res.stdout.strip().split("=", 1)[1]
    assert json.loads(rhs) == {}


def test_config_unset_project_alerts_enabled_clears_leaf(tmp_path):
    """`config unset budget.project_alerts_enabled` restores the False default."""
    _run_cli(tmp_path, "config", "set", "budget.project_alerts_enabled", "true")
    unset_res = _run_cli(
        tmp_path, "config", "unset", "budget.project_alerts_enabled"
    )
    assert unset_res.returncode == 0, unset_res.stderr
    get_res = _run_cli(
        tmp_path, "config", "get", "budget.project_alerts_enabled"
    )
    assert get_res.returncode == 0, get_res.stderr
    assert get_res.stdout.strip().endswith("=false")
