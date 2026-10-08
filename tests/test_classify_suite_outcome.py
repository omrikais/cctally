"""The schedule-only runtime-budget exception reads the suite's own outcome record.

`.github/scripts/classify_suite_outcome.py` decides whether a failed
`test-macos` suite failed ONLY because the runtime budget was exceeded, with
every test passing (operator decision D1(b) of #899). It exits 0 exactly then,
and `ci.yml` runs a marker step on that exit so background-health readers can
tell a budget-only schedule failure from a real one. Every other reading is
exit 1: the exception is narrow by construction.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "classify_suite_outcome.py"

# A real outcome record, copied verbatim from an authoritative full run whose
# only failure was the runtime budget (run 20260930T100924Z-39746-3644).
BUDGET_ONLY = {
    "budget": {"inner": 2, "outer": 4, "pytest": 10},
    "capabilities": {"agentmem": True, "fts5": True, "node": True, "pytest": True,
                     "pytest-timeout": True, "pytest-xdist": True, "rich": True},
    "coverage": {
        "mode": "full",
        "omittedHarnesses": [],
        "pytest": "full",
        "selectedHarnesses": [
            "5h-canonical", "account", "alerts", "alerts-dispatch", "bench", "blocks", "budget",
            "cache-report", "chokepoint", "codex-daily", "codex-monthly", "codex-quota",
            "codex-session", "codex-weekly", "config", "conversation", "daily", "daily-instances",
            "dashboard", "diff", "doc-lint", "doctor", "envelope-oracle", "explain",
            "five-hour-blocks", "fixture-cache", "forecast", "frontend", "hook-tick", "kill-server",
            "migrations", "mirror-public", "mirror-snapshot", "monthly", "npm-postinstall",
            "npm-shim", "percent-milestone-idempotency", "preflight", "pricing-check", "project",
            "quota", "reap", "reconcile", "record-credit", "record-usage-selfheal", "rederive",
            "release", "release-harness-isolation", "report", "session", "settings-api", "setup",
            "share", "share-v2", "shellcheck", "source-aware", "statusline", "subgroup",
            "test-remote", "tui", "update", "weekly",
        ],
    },
    "exitCode": 1,
    "failureClass": "product",
    "outcome": "fail",
    "passedCases": 23787,
    "pytestPassed": 20093,
    "reasons": [{"code": "runtime-budget-exceeded", "phase": "budget", "subject": "135.83 > 120"}],
    "schemaVersion": 1,
    "secondsPerThousandCases": 135.8,
    "totals": {"failed": 0, "passed": 3694},
    "wallSeconds": 3231,
}


def classifier():
    spec = importlib.util.spec_from_file_location("classify_suite_outcome", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def classify(tmp_path, doc, capsys=None):
    path = tmp_path / "outcome.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return classifier().main([str(path)])


def mutated(change):
    doc = copy.deepcopy(BUDGET_ONLY)
    change(doc)
    return doc


def test_budget_only_failure_qualifies(tmp_path, capsys):
    assert classify(tmp_path, BUDGET_ONLY) == 0
    assert "runtime budget is the only failure" in capsys.readouterr().out


def test_a_failed_case_never_qualifies(tmp_path):
    # A run with a failing test is a real failure, whatever its budget reason says.
    assert classify(tmp_path, mutated(lambda d: d["totals"].update(failed=1))) == 1


@pytest.mark.parametrize("value", [0, None, -1, "20093", True], ids=["zero", "null", "negative", "string", "bool"])
def test_zero_pytest_passes_never_qualifies(tmp_path, value):
    # A run that passed no pytest node proves nothing about the tests.
    assert classify(tmp_path, mutated(lambda d: d.update(pytestPassed=value))) == 1


def test_a_second_reason_never_qualifies(tmp_path):
    second = {"code": "pytest-failed", "phase": "pytest", "subject": "pytest"}
    assert classify(tmp_path, mutated(lambda d: d["reasons"].append(second))) == 1


def test_a_reason_in_another_phase_never_qualifies(tmp_path):
    assert classify(tmp_path, mutated(lambda d: d["reasons"][0].update(phase="harness"))) == 1
    assert classify(tmp_path, mutated(lambda d: d["reasons"][0].update(code="merge-runtime-budget-exceeded"))) == 1
    assert classify(tmp_path, mutated(lambda d: d.update(reasons=[]))) == 1


@pytest.mark.parametrize("change", [
    lambda d: d["coverage"].update(omittedHarnesses=["release"]),
    lambda d: d["coverage"].update(mode="tier-fast"),
    lambda d: d["coverage"].update(mode="merge-focused"),
    lambda d: d["coverage"].update(pytest="selected"),
    lambda d: d["coverage"].update(selectedHarnesses=[]),
    lambda d: d.pop("coverage"),
], ids=["omitted-harness", "tier", "merge-focused", "selected-pytest", "no-harness", "no-coverage"])
def test_omitted_coverage_never_qualifies(tmp_path, change):
    # Only a full-coverage run is full verification.
    assert classify(tmp_path, mutated(change)) == 1


@pytest.mark.parametrize("doc", [
    mutated(lambda d: d.update(schemaVersion=2)),
    mutated(lambda d: d.pop("schemaVersion")),
    mutated(lambda d: d.pop("totals")),
    mutated(lambda d: d.update(reasons={"code": "runtime-budget-exceeded"})),
    [BUDGET_ONLY],
    '{"schemaVersion": 1, "reasons":',
], ids=["schema-2", "no-schema", "no-totals", "reasons-not-list", "not-an-object", "malformed-json"])
def test_wrong_or_missing_schema_never_qualifies(tmp_path, doc):
    assert classify(tmp_path, doc) == 1


def test_a_missing_file_never_qualifies(tmp_path, capsys):
    assert classifier().main([str(tmp_path / "absent.json")]) == 1
    assert classifier().main([]) == 1
    assert capsys.readouterr().out.strip()


def test_the_classifier_runs_as_a_script(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "outcome.json"
    path.write_text(json.dumps(BUDGET_ONLY))
    assert subprocess.run([sys.executable, str(SCRIPT), str(path)]).returncode == 0
    path.write_text(json.dumps(mutated(lambda d: d["totals"].update(failed=2))))
    assert subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True).returncode == 1


def test_the_marker_steps_are_wired_schedule_only():
    # The workflow text the background-health reader depends on: the suite writes its
    # outcome record, a schedule-only classifier step reads it on failure, and the
    # marker step runs only on the classifier's success. The job still fails.
    import yaml

    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["test-macos"]["steps"]
    names = [step.get("name") for step in steps]
    suite = steps[names.index("Run cctally test suite")]
    outcome = suite["env"]["CCTALLY_TEST_ALL_OUTCOME_FILE"]
    assert "github.run_id" in outcome and "github.run_attempt" in outcome, outcome
    classify_step = steps[names.index("Classify the suite outcome")]
    assert classify_step["if"] == "failure() && github.event_name == 'schedule'"
    assert classify_step["id"] == "suite_outcome"
    assert classify_step["continue-on-error"] is True
    assert ".github/scripts/classify_suite_outcome.py" in classify_step["run"]
    assert classify_step["env"]["CCTALLY_TEST_ALL_OUTCOME_FILE"] == outcome
    marker = steps[names.index("Runtime budget is the only failure")]
    assert marker["if"] == "failure() && steps.suite_outcome.outcome == 'success'"
    assert names.index("Run cctally test suite") < names.index("Classify the suite outcome") \
        < names.index("Runtime budget is the only failure")
    assert "continue-on-error" not in suite and "continue-on-error" not in marker
