"""The Linux matrix lane must not collapse the solo pytest phase (#529 S5 §2.4).

`CCTALLY_TEST_JOBS` is the backward-compatible combined budget: setting it to
2 caps the outer shell pool AND the later, otherwise-solo pytest phase, so all
three roles resolve to 2 across a three-version Python matrix. The lane wants a
narrow outer pool only. `test-pr` in ci.yml already carries the corrected form
on the same hosted runner class, and this lane must match it.

The fleet-constant rule that governs the two LAN runners does not bind a hosted
lane: a hosted GitHub runner is a separately identified lane that no routing
decision substitutes for another, so letting pytest auto-tune to its detected
core count costs no determinism.

These assertions read the job's parsed `env:` MAPPING rather than scanning the
job text. A substring scan cannot tell a real mapping entry from the same
characters inside a comment or a `run:` block, and it passes on a file a real
YAML consumer cannot parse at all.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci-linux-matrix.yml"
JOB = "test-linux"


def _job_env(path: Path, job: str) -> dict[str, str]:
    """Return the `env:` mapping of one job, parsed by indentation.

    Deliberately NOT PyYAML: it is not a declared dependency of this estate and
    no CI lane installs it, so a skip-guarded gate would never run in the lanes
    it protects. `test_a_real_yaml_parser_agrees` below corroborates this parse
    wherever PyYAML happens to exist.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    env: dict[str, str] = {}
    in_job = False
    in_env = False
    for line in lines:
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith(":"):
            in_job = line.strip() == f"{job}:"
            in_env = False
            continue
        if not in_job:
            continue
        if line.startswith("    ") and not line.startswith("     "):
            # A key at job-body depth. `env:` opens the mapping; any other key
            # (`steps:`, `runs-on:`, …) closes it.
            in_env = line.strip() == "env:"
            continue
        if not in_env:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith("      ") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        # Strip a trailing inline comment, then the quotes YAML scalars carry.
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def test_the_env_parse_is_not_vacuous() -> None:
    """An empty parse would pass every `not in` assertion below for free."""
    env = _job_env(WORKFLOW, JOB)

    assert env, f"parsed no env entries for job {JOB!r} in {WORKFLOW.name}"
    # Two anchors whose values are load-bearing elsewhere in the estate, so a
    # parser that silently returned the wrong shape cannot satisfy them.
    assert env.get("TZ") == "Etc/UTC", env
    assert env.get("CCTALLY_AUTHORITATIVE_RUN") == "1", env


def test_matrix_lane_does_not_collapse_solo_pytest_workers() -> None:
    env = _job_env(WORKFLOW, JOB)

    assert env.get("CCTALLY_OUTER_JOBS") == "2", (
        "the matrix lane must pin only the outer pool; it reads "
        f"{env.get('CCTALLY_OUTER_JOBS')!r}"
    )
    assert "CCTALLY_TEST_JOBS" not in env, (
        "CCTALLY_TEST_JOBS is the combined budget and collapses pytest to the "
        "same value; the lane must set CCTALLY_OUTER_JOBS instead"
    )


def test_matrix_lane_leaves_inner_and_pytest_unpinned() -> None:
    """Inner derives to min(4, OUTER)=2; pytest auto-tunes to the hosted cores.

    Pinning either here would re-collapse the lane by a different route.
    """
    env = _job_env(WORKFLOW, JOB)

    assert "CCTALLY_INNER_JOBS" not in env, env
    assert "CCTALLY_PYTEST_JOBS" not in env, env


def test_a_real_yaml_parser_agrees() -> None:
    """Corroborate the hand parse, and prove the file loads as a document.

    This is the case that may skip, because all it adds is corroboration. The
    gates above do not depend on PyYAML for the reason given in `_job_env`.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML is not installed here")
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    real = loaded["jobs"][JOB]["env"]

    mine = _job_env(WORKFLOW, JOB)
    for key, value in real.items():
        assert key in mine, f"hand parser missed {key!r}"
        if isinstance(value, str) and "${{" not in value:
            assert mine[key] == value, key
    assert set(mine) == set(real)
