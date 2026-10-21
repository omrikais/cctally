"""Route executing private workflow jobs to the existing Mac without fork exposure.

Interpret the checked-in `if` and `runs-on` expressions against representative
GitHub contexts. Literal label assertions alone would miss a swapped branch.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from test_ci_receipt_gate import _Parser, _context, _evaluate, _tokenize


ROOT = Path(__file__).resolve().parents[1]
MAC = ["self-hosted", "macOS", "cctally"]
HOSTED = ["ubuntu-latest"]


def _jobs(workflow):
    return yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]


def _route(job, context):
    if "if" in job and not _evaluate(" ".join(job["if"].split()), context):
        return None
    runs_on = job["runs-on"]
    if isinstance(runs_on, list):
        return runs_on
    if runs_on == "ubuntu-latest":
        return HOSTED
    # GitHub evaluates fromJSON's expression before selecting a runner. Reuse
    # the receipt-gate condition parser for its actual && / || operand values,
    # then decode the selected JSON array rather than searching for labels.
    match = re.fullmatch(r"\$\{\{ fromJSON\((.*)\) \}\}", runs_on)
    assert match, runs_on
    return json.loads(_Parser(_tokenize(match.group(1)), context).parse())


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", "pull_request"])
def test_all_executing_private_ci_jobs_use_the_mac(event):
    context = _context(event=event)
    jobs = _jobs("ci.yml")
    routed = {name: _route(job, context) for name, job in jobs.items()}
    assert routed["release-stamp-gate"] == MAC
    # Test regime reset, session 1: a push to main runs no full-suite job and
    # no e2e-reader; the receipt gate runs only for a same-repository PR, and
    # the scheduled run still runs test-macos.
    assert routed["receipt-gate"] == (MAC if event == "pull_request" else None)
    assert routed["test-macos"] == (None if event == "push" else MAC)
    assert routed["e2e-reader"] == (MAC if event == "schedule" else None)
    for name, route in routed.items():
        assert route is None or route == MAC, (event, name, route)


def test_private_fork_pr_never_executes_a_ci_job():
    context = _context(event="pull_request", head_repo="contributor/cctally-dev")
    assert all(_route(job, context) is None for job in _jobs("ci.yml").values())


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_public_mirror_keeps_the_hosted_classifier_and_pr_lane(event):
    context = _context(repository="omrikais/cctally", event=event,
                       head_repo="contributor/cctally")
    routed = {name: _route(job, context) for name, job in _jobs("ci.yml").items()}
    assert routed["release-stamp-gate"] == HOSTED
    assert routed["test-pr"] == (HOSTED if event == "pull_request" else None)
    assert all(route is None or route == HOSTED for route in routed.values())


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_doc_lint_private_trusted_runs_on_mac_and_public_on_hosted(event):
    job = _jobs("doc-lint.yml")["doc-lint"]
    assert _route(job, _context(event=event)) == MAC
    assert _route(job, _context(repository="omrikais/cctally", event=event,
                                head_repo="contributor/cctally")) == HOSTED
    assert _route(job, _context(event="pull_request", head_repo="contributor/fork")) is None


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_private_pricing_check_uses_mac_and_public_stays_skipped(event):
    job = _jobs("pricing-freshness.yml")["check"]
    assert _route(job, _context(event=event)) == MAC
    assert _route(job, _context(repository="omrikais/cctally", event=event)) is None


# #940 Part C (C4). The three workflows a runner service executes for this
# repository are mirrored, so they refer to the runner's storage only through
# the variable NAMES its listener exports, never to a host, an alias or an
# installed path. Built from fragments so this public file names none of them.
_RUNNER_WORKFLOWS = ("ci.yml", "pricing-freshness.yml", "doc-lint.yml")
_FORBIDDEN_WORKFLOW_TEXT = (
    ("an ssh alias", re.compile(r"\b[A-Za-z0-9.]+-(?:cmd|admin)\b")),
    ("a home directory", re.compile(r"/Users/")),
    ("a mounted volume", re.compile(r"/Volumes/")),
    ("an installed runner path",
     re.compile(r"\.local/(?:libexec|state)/cctally|\.config/cctally-runner")),
    ("a runner-home cache", re.compile(r"(?:\$HOME|~)/\.(?:cache|local)/cctally")),
)


def _steps(workflow, job):
    return _jobs(workflow)[job]["steps"]


def _step(workflow, job, name):
    return next(step for step in _steps(workflow, job) if step.get("name") == name)


@pytest.mark.parametrize("workflow", _RUNNER_WORKFLOWS)
def test_runner_workflows_name_no_alias_or_installed_path(workflow):
    text = (ROOT / ".github/workflows" / workflow).read_text()
    found = [
        (label, match.group(0))
        for label, pattern in _FORBIDDEN_WORKFLOW_TEXT
        for match in pattern.finditer(text)
    ]
    assert not found, (workflow, found)


def test_pricing_payload_lives_in_the_job_temp_for_producer_and_consumer(tmp_path):
    check = _step("pricing-freshness.yml", "check",
                  "Run pricing-check (capture rc — exit 1 = drift, not failure)")
    manage = _step("pricing-freshness.yml", "check", "Manage the pricing-drift issue")
    text = (ROOT / ".github/workflows/pricing-freshness.yml").read_text()
    assert "/tmp/pricing.json" not in text
    assert '"${RUNNER_TEMP:?}/pricing.json"' in manage["run"]
    # Execute the producer with the expensive command replaced: the payload
    # lands in the job's temp directory, and nowhere without one.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").write_text("#!/bin/sh\necho '{\"ok\": true}'\n")
    (fake_bin / "python3").chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "GITHUB_OUTPUT": str(tmp_path / "out")}
    # The step prepends brew's Python to PATH; neutralise that one line so the
    # fake interpreter answers instead of a real, networked pricing check.
    run = check["run"].replace("export PATH=", "true ")
    done = subprocess.run(
        ["bash", "-e", "-c", run], env={**env, "RUNNER_TEMP": str(runner_temp)},
        capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert (runner_temp / "pricing.json").read_text() == '{"ok": true}\n'
    refused = subprocess.run(
        ["bash", "-e", "-c", run], env=env, capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0
    assert "RUNNER_TEMP" in refused.stderr


def test_doc_lint_takes_no_runner_admission():
    for step in _steps("doc-lint.yml", "doc-lint"):
        command = str(step.get("run", ""))
        assert "_lib_runner_reservation" not in command, step
        assert "CCTALLY_RUNNER_" not in command, step
