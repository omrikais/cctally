"""Every CI lane that runs the authoritative bundle must provision the frontend estate.

``bin/cctally-frontend-test`` (#463 S1, finding F23) puts the whole vitest estate
and the React hooks lint gate INSIDE ``bin/cctally-test-all``: the aggregator
discovers it through its ``bin/cctally-*-test`` glob, and the harness FAILS —
deliberately, never skips — when either required frontend binary is absent.

That makes ``npm ci`` a hard requirement of the bundle rather than a per-lane
convenience. ``.github/workflows/ci-linux-matrix.yml`` previously ran
``actions/setup-node`` without ``npm ci`` on purpose, which after F23 failed the
lane on every tag push, on the weekly cron and on the private manual pre-cut
gate, across all three Python versions — and, because that workflow publishes to
the public mirror, on the public repository as well.

The fix is the provisioning step, not a weakened harness. A declared skip
modelled on ``CCTALLY_AGENTMEM_TEST_POLICY`` was considered and rejected: F23
exists because the frontend estate sat outside the authoritative bundle, and a
lane where it silently does not sit inside it re-creates a weaker form of the
same hole. "The bundle passed" must mean one thing on every lane.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci-linux-matrix.yml",
)


def _workflow_job_blocks(text: str) -> list[str]:
    """One block per job under the top-level ``jobs:`` key.

    Splitting on every two-space key would also cut on ``on:``'s children
    (``push:``, ``schedule:``), which folds the trigger prose into the first job
    block and lets a COMMENT satisfy an assertion meant for a step. The scan
    therefore starts after the ``jobs:`` line.
    """
    lines = text.splitlines()
    try:
        first = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    except StopIteration:  # pragma: no cover - a workflow always has jobs
        return []
    starts = [
        index
        for index, line in enumerate(lines)
        if index > first
        and line.startswith("  ")
        and not line.startswith("    ")
        and not line.lstrip().startswith("#")
        and line.rstrip().endswith(":")
    ]
    starts.append(len(lines))
    return ["\n".join(lines[start:end]) for start, end in zip(starts, starts[1:])]


def _step_directives(block: str) -> list[str]:
    """Every non-comment step line, stripped. Never a comment, so prose about a
    command can never be mistaken for the command."""
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_frontend_harness_is_discoverable_and_fails_loudly() -> None:
    harness = ROOT / "bin" / "cctally-frontend-test"
    assert harness.exists()
    # A non-executable harness is SILENTLY skipped by the aggregator's glob
    # (bin/cctally-test-all:122), which is the failure mode this whole contract
    # exists to prevent.
    assert harness.stat().st_mode & 0o111, "bin/cctally-frontend-test must be executable"
    text = harness.read_text()
    assert "node_modules/.bin/vitest" in text
    # The missing-vitest branch reports a FAIL line and exits non-zero. A SKIP
    # there would recreate the gap F23 closes, so pin the loud path rather than
    # the word, which the header legitimately uses to describe the divergence.
    assert "FAIL frontend-estate: vitest is not installed" in text
    assert "passed: 0   failed: 1" in text


def test_the_frontend_harness_runs_hooks_lint_and_fails_loudly() -> None:
    harness = ROOT / "bin" / "cctally-frontend-test"
    text = harness.read_text()
    assert "node_modules/.bin/eslint" in text
    assert "npm run lint" in text
    assert "FAIL hooks-lint: eslint is not installed" in text
    assert "FAIL hooks-lint: npm run lint failed" in text


def test_every_bundle_ci_job_provisions_the_frontend_estate() -> None:
    seen = 0
    for relative in _WORKFLOWS:
        blocks = _workflow_job_blocks((ROOT / relative).read_text())
        bundle_jobs = [
            block
            for block in blocks
            if any("bin/cctally-test-all" in line for line in _step_directives(block))
        ]
        assert bundle_jobs, relative
        for block in bundle_jobs:
            seen += 1
            job = block.splitlines()[0].strip()
            directives = _step_directives(block)
            assert any("uses: actions/setup-node" in line for line in directives), (
                f"{relative} {job}: runs bin/cctally-test-all with no node runtime"
            )
            # Match the COMMAND, not one exact spelling of the line. The previous
            # `line == "run: npm ci"` failed a legitimate refactor to `run: |` or
            # to `npm ci --prefix dashboard/web`, which is a false alarm about
            # workflow formatting rather than about the contract.
            assert any(re.search(r"\bnpm ci\b", line) for line in directives), (
                f"{relative} {job}: runs bin/cctally-test-all without `npm ci`, so "
                "bin/cctally-frontend-test fails for want of vitest"
            )
            # ... and it must install into dashboard/web. A root-level `npm ci`
            # satisfied the command check above while installing nothing vitest
            # can use, so the estate would still fail for want of vitest.
            assert any(
                "working-directory: dashboard/web" in line
                or "--prefix dashboard/web" in line
                for line in directives
            ), (
                f"{relative} {job}: `npm ci` is not scoped to dashboard/web, so it "
                "installs no vitest for bin/cctally-frontend-test"
            )
    # Three lanes run the bundle: test-macos and test-pr in ci.yml, test-linux in
    # ci-linux-matrix.yml. A drop below three means a lane was renamed or removed
    # and this contract stopped covering it.
    assert seen >= 3, f"expected at least three bundle lanes, found {seen}"
