from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def _job(name: str, next_name: str | None = None) -> str:
    workflow = _workflow()
    job = workflow.split(f"\n  {name}:\n", 1)[1]
    if next_name is not None:
        job = job.split(f"\n  {next_name}:\n", 1)[0]
    return job


def _test_pr_job() -> str:
    return _job("test-pr")


def test_private_same_repo_pr_uses_self_hosted_macos_authority() -> None:
    job = _job("test-macos", "dashboard-build-stability")

    assert "github.event_name == 'pull_request'" in job
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job


def test_hosted_pr_lane_is_reserved_for_public_mirror_prs() -> None:
    job = _test_pr_job()

    assert "github.repository == 'omrikais/cctally'" in job
    assert "github.event.pull_request.head.repo.full_name == github.repository ||" not in job


def test_hosted_pr_lane_does_not_collapse_solo_pytest_workers() -> None:
    job = _test_pr_job()

    # CCTALLY_TEST_JOBS is the backward-compatible combined budget: setting it
    # to 2 caps both the shell pool AND the later, otherwise-solo pytest phase.
    # The hosted lane needs a narrow outer pool but should leave pytest at the
    # runner's detected core count.
    assert 'CCTALLY_OUTER_JOBS: "2"' in job
    assert "CCTALLY_TEST_JOBS:" not in job


def test_hosted_pr_suite_step_has_its_own_timeout() -> None:
    job = _test_pr_job()

    # The job also installs dependencies and runs Vitest/Playwright. Bound the
    # aggregate shell+pytest phase separately so a genuinely wedged harness
    # cannot consume the whole 60-minute job timeout and hide the stuck phase.
    assert "timeout --kill-after=30s 30m bin/cctally-test-all" in job
