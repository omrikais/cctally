"""The scheduled-CI exception needs exact, fresh, pre-workload refusal proof."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/classify_reservation_outcome.py"
RUN_ID = "macos-123-2"
INVOCATION = "a" * 32


def proof(**changes):
    return {"schemaVersion": 1, "runId": RUN_ID, "invocationId": INVOCATION,
            "cause": "reservation-wait-exhausted", "reservationState": "busy",
            "workloadStarted": False, "waitSeconds": 300.0, "waitedSeconds": 300.2,
            "observedAt": "2026-10-08T07:00:00+00:00", **changes}


def classify(tmp_path, doc, *, event="schedule", run_id=RUN_ID, invocation=INVOCATION,
             outcome=False):
    record = tmp_path / "refusal.json"
    if doc is not None:
        record.write_text(json.dumps(doc))
    suite = tmp_path / "outcome.json"
    if outcome:
        suite.write_text("even an unreadable suite outcome disproves this exception")
    return subprocess.run([sys.executable, str(SCRIPT), "--record", str(record),
                           "--event-name", event, "--run-id", run_id,
                           "--invocation-id", invocation, "--suite-outcome", str(suite)],
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("state", ["busy", "unknown"])
def test_exact_scheduled_pre_workload_refusal_is_unavailable(tmp_path, state):
    result = classify(tmp_path, proof(reservationState=state))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "full verification unavailable" in result.stdout


@pytest.mark.parametrize("changes", [
    {"schemaVersion": True}, {"schemaVersion": 2}, {"runId": "macos-123-1"},
    {"invocationId": "b" * 32}, {"cause": "coordination-unavailable"},
    {"reservationState": "idle"}, {"workloadStarted": True}, {"workloadStarted": 0},
    {"waitSeconds": 1800}, {"waitSeconds": True}, {"waitedSeconds": 299.9},
    {"waitedSeconds": float("nan")}, {"observedAt": "yesterday"},
    {"observedAt": "2026-10-08T07:00:00"}, {"processes": [{"argv": "private"}]},
])
def test_malformed_stale_or_unsanitized_proof_is_rejected(tmp_path, changes):
    result = classify(tmp_path, proof(**changes))
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request"])
def test_only_schedule_can_use_the_missing_verification_exception(tmp_path, event):
    assert classify(tmp_path, proof(), event=event).returncode == 1


@pytest.mark.parametrize("case", ["absent", "suite-started", "same-attempt-stale"])
def test_ambiguous_exit_and_stale_same_attempt_do_not_get_the_exception(tmp_path, case):
    result = classify(tmp_path, None if case == "absent" else proof(),
                      outcome=case == "suite-started",
                      invocation="b" * 32 if case == "same-attempt-stale" else INVOCATION)
    assert result.returncode == 1, result.stdout + result.stderr


def test_duplicate_json_keys_and_symlinks_are_not_authentic_proof(tmp_path):
    path = tmp_path / "refusal.json"
    text = json.dumps(proof())
    path.write_text(text[:-1] + ', "workloadStarted": false}')
    mod_spec = importlib.util.spec_from_file_location("ci_reservation", SCRIPT)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    args = ["--record", str(path), "--event-name", "schedule", "--run-id", RUN_ID,
            "--invocation-id", INVOCATION, "--suite-outcome", str(tmp_path / "outcome.json")]
    assert mod.main(args) == 1
    target = tmp_path / "valid.json"
    target.write_text(text)
    link = tmp_path / "linked.json"
    link.symlink_to(target)
    args[1] = str(link)
    assert mod.main(args) == 1


def test_ci_pins_admission_budget_and_exclusive_schedule_markers():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["test-macos"]
    assert job["timeout-minutes"] == 70
    steps = {step.get("name"): step for step in job["steps"]}
    suite = steps["Run cctally test suite"]
    assert "--wait 300" in suite["run"]
    assert '--refusal-record "$CCTALLY_RESERVATION_REFUSAL_FILE"' in suite["run"]
    assert '--invocation-id "$CCTALLY_RESERVATION_INVOCATION"' in suite["run"]
    assert "uuid.uuid4().hex" in suite["run"] and '"$GITHUB_ENV"' in suite["run"]
    assert suite["env"]["CCTALLY_RESERVATION_REFUSAL_FILE"].endswith(
        "/cctally-reservation-refusals/macos-${{ github.run_id }}-${{ github.run_attempt }}.json")
    classifier = steps["Classify the reservation outcome"]
    assert "failure()" in classifier["if"] and "github.event_name == 'schedule'" in classifier["if"]
    assert classifier["continue-on-error"] is True
    assert '--suite-outcome "$CCTALLY_TEST_ALL_OUTCOME_FILE"' in classifier["run"]
    marker = steps["Full verification unavailable: reservation wait exhausted"]
    assert "steps.reservation_outcome.outcome == 'success'" in marker["if"]
    # The runtime budget is report-only (test regime reset, session 1), so the
    # budget-only classifier and its marker step are gone.
    assert "Classify the suite outcome" not in steps
    assert "Runtime budget is the only failure" not in steps
    assert (suite["env"]["CCTALLY_TEST_JOBS"], suite["env"]["CCTALLY_INNER_JOBS"],
            suite["env"]["CCTALLY_PYTEST_JOBS"]) == ("4", "2", "10")


def test_the_quarantine_leg_runs_only_after_a_started_suite_and_inside_its_own_reservation():
    # Test regime reset fix round (B1): the scheduled report-only quarantine leg
    # takes the same physical-host reservation as the suite, never runs after a
    # refused reservation or a suite workload that never started, and is
    # bounded by its own step timeout well inside the job's.
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["test-macos"]
    steps = {step.get("name"): step for step in job["steps"]}
    suite = steps["Run cctally test suite"]
    leg = steps["Run the quarantined tests (report only)"]
    assert "github.event_name == 'schedule'" in leg["if"], leg["if"]
    assert "steps.reservation_outcome.outcome != 'success'" in leg["if"], leg["if"]
    assert leg["continue-on-error"] is True
    assert isinstance(leg["timeout-minutes"], int) and 0 < leg["timeout-minutes"] <= 10, leg
    assert leg["timeout-minutes"] < job["timeout-minutes"]
    run = leg["run"]
    assert leg["env"]["CCTALLY_TEST_ALL_OUTCOME_FILE"] == suite["env"]["CCTALLY_TEST_ALL_OUTCOME_FILE"]
    assert leg["env"]["CCTALLY_TEST_RUN_ID"] == suite["env"]["CCTALLY_TEST_RUN_ID"]
    # The suite's outcome record is the proof its workload started; without it
    # the leg exits before taking any reservation.
    started = run.index('-e "$CCTALLY_TEST_ALL_OUTCOME_FILE"')
    reservation = run.index("python3 bin/_lib_runner_reservation.py run")
    assert started < reservation, run
    head, sep, workload = run[reservation:].partition(" -- ")
    assert sep, run
    assert '--root "${CCTALLY_RUNNER_RESERVATION_ROOT:?}"' in head
    assert "--wait " in head and '--evidence "${CCTALLY_RUNNER_OBSERVATIONS_ROOT:?}/ci/' in head
    assert "pytest" not in head and '"-m", "pytest"' in workload, run
