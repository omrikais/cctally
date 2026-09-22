"""Route executing private workflow jobs to the existing Mac without fork exposure.

Interpret the checked-in `if` and `runs-on` expressions against representative
GitHub contexts. Literal label assertions alone would miss a swapped branch.
"""
import json
import re
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
    if event in ("push", "pull_request"):
        assert routed["receipt-gate"] == MAC
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
