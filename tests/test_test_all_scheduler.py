"""Assertions for bin/cctally-test-all's worker model and phase isolation.

Most tests invoke the runner's side-effect-free CCTALLY_TEST_ALL_PLAN=1 dry-run
(which exits before any harness/pytest launch) with
CCTALLY_TEST_ALL_FAKE_NCPU pinning the core count. The execution test uses a
synthetic checkout and fake Python boundary to observe pytest phase isolation.

WARNING: calls through ``_plan`` must keep plan mode enabled. The one execution
test builds a synthetic checkout whose harnesses and Python boundary are fakes,
so it cannot recurse into the real suite. Short timeouts backstop both paths.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"
CONTRACT_LIB = BIN / "_lib-test-contract.sh"
# .mirror-allowlist excludes itself from the mirror, so its presence is exactly the
# marker bin/cctally-test-all uses to require the maintainer-local test-remote harness.
PRIVATE_TREE = (REPO / ".mirror-allowlist").exists()


def _plan(env_overrides, fake_ncpu="16", runner=RUNNER):
    env = {
        "PATH": __import__("os").environ["PATH"],
        "CCTALLY_TEST_ALL_PLAN": "1",
        "CCTALLY_TEST_ALL_FAKE_NCPU": fake_ncpu,
    }
    env.update(env_overrides)
    proc = subprocess.run(
        [str(runner)], env=env, capture_output=True, text=True, timeout=30
    )
    return proc


def _kv(stdout):
    out = {}
    for line in stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_unset_autotunes_outer_below_ncpu_16():
    p = _plan({}, fake_ncpu="16")
    assert p.returncode == 0, p.stderr
    kv = _kv(p.stdout)
    assert kv["ncpu"] == "16"
    assert kv["outer"] == "7"     # round(16*0.45)
    assert kv["inner"] == "4"     # min(4, outer)
    assert kv["pytest"] == "16"   # solo pytest keeps the full machine
    assert kv["reconcile_in_pool"] == "1"


def test_unset_autotune_ncpu_10():
    kv = _kv(_plan({}, fake_ncpu="10").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("5", "4", "10")


def test_explicit_budget_4_preserves_today():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "4"}, fake_ncpu="16").stdout)
    # Explicit budget = outer = pytest (today's meaning); inner capped at 4.
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("4", "4", "4")


def test_explicit_budget_2_preserves_today():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "2"}, fake_ncpu="16").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("2", "2", "2")


def test_serial_budget_1_is_fully_serial():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "1"}, fake_ncpu="16").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("1", "1", "1")


def test_explicit_role_overrides():
    kv = _kv(
        _plan(
            {
                "CCTALLY_OUTER_JOBS": "9",
                "CCTALLY_INNER_JOBS": "3",
                "CCTALLY_PYTEST_JOBS": "6",
            },
            fake_ncpu="16",
        ).stdout
    )
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("9", "3", "6")


def test_outer_only_override_keeps_full_machine_for_solo_pytest():
    kv = _kv(_plan({"CCTALLY_OUTER_JOBS": "2"}, fake_ncpu="4").stdout)
    assert (kv["outer"], kv["inner"], kv["pytest"]) == ("2", "2", "4")


def test_inner_override_independent_of_outer_default():
    kv = _kv(_plan({"CCTALLY_INNER_JOBS": "2"}, fake_ncpu="16").stdout)
    assert kv["outer"] == "7" and kv["inner"] == "2"


def test_rejects_zero():
    p = _plan({"CCTALLY_TEST_JOBS": "0"}, fake_ncpu="16")
    assert p.returncode == 2


def test_rejects_non_numeric():
    p = _plan({"CCTALLY_OUTER_JOBS": "abc"}, fake_ncpu="16")
    assert p.returncode == 2


@pytest.mark.skipif(
    not PRIVATE_TREE,
    reason="test-remote is maintainer-local; the public mirror ships no such harness",
)
def test_plan_explicitly_includes_test_remote_harness():
    p = _plan({})
    assert p.returncode == 0, p.stderr
    assert "test-remote" in _kv(p.stdout)["harnesses"].split()


def _expected_names(private):
    """Manifest rows for the synthetic checkout. test-remote is declared in the
    private profile whether or not the file is present, because its absence is
    itself one of the conditions under test."""
    names = [("codex-quota", "public"), ("source-aware", "public")]
    if private:
        names.append(("test-remote", "private"))
    return names


def _tree(tmp_path, *, private, test_remote_mode):
    """A synthetic checkout. ``private`` writes .mirror-allowlist, which is
    itself unmirrored and so marks the private tree; ``test_remote_mode`` is
    None to omit the maintainer-local harness entirely (the public shape)."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    bindir.mkdir(parents=True)
    runner = bindir / "cctally-test-all"
    shutil.copy2(RUNNER, runner)
    # The aggregator sources the contract library and reads the estate manifest
    # at admission (#529 S1), so a synthetic checkout must carry both or every
    # non-plan-mode case in this module fails on the fixture rather than on the
    # property it asserts.
    shutil.copy2(CONTRACT_LIB, bindir / "_lib-test-contract.sh")

    for name in ("codex-quota", "source-aware"):
        harness = bindir / f"cctally-{name}-test"
        harness.write_text("#!/usr/bin/env bash\nexit 0\n")
        harness.chmod(0o755)

    if private:
        # The allowlist has to agree with the declared visibilities below, or
        # admission reports visibility-drift instead of the property under
        # test: every harness is public except the maintainer-local
        # test-remote one.
        (repo / ".mirror-allowlist").write_text(
            "bin/cctally-test-all\nbin/_lib-*\ntests/**\nbin/cctally-*\n"
            "!bin/cctally-test-remote-test\n"
        )
        # A private tree with no classifier is now an admission refusal in its
        # own right (visibility-classifier-unavailable), so the scratch tree
        # must carry the classifier exactly as tests/test_authoritative_test_
        # contract.py::_estate does. The classifier is maintainer-local, so the
        # public mirror's own suite skips instead of failing on a file it never
        # received.
        if not (REPO / ".githooks").exists() or not (
            REPO / ".githooks" / "_match.py"
        ).exists():
            pytest.skip(
                "the allowlist classifier .githooks/_match.py is maintainer-local"
            )
        githooks = repo / ".githooks"
        githooks.mkdir(exist_ok=True)
        shutil.copy2(REPO / ".githooks" / "_match.py", githooks / "_match.py")
    if test_remote_mode is not None:
        test_remote = bindir / "cctally-test-remote-test"
        test_remote.write_text("#!/usr/bin/env bash\nexit 0\n")
        test_remote.chmod(test_remote_mode)

    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "authoritative-test-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "minHarnessRows": 0,
                "harnesses": [
                    {
                        "name": n,
                        "visibility": v,
                        "minCases": 0,
                        "countPolicy": "fixed",
                    }
                    for n, v in _expected_names(private)
                ],
                "capabilities": [],
                "forbiddenRegeneration": [],
            },
            indent=2,
        )
        + "\n"
    )
    return runner


def _admit(runner, env_overrides=None):
    """Drive the runner past plan mode so admission actually runs. Admission
    precedes LOGDIR creation, so a refusal never reaches the harness pool."""
    env = {"PATH": os.environ["PATH"], "CCTALLY_TEST_JOBS": "1"}
    env.update(env_overrides or {})
    return subprocess.run(
        [str(runner)], env=env, capture_output=True, text=True, timeout=60
    )


def test_non_executable_test_remote_is_a_manifest_admission_error(tmp_path):
    """The manifest, not the retired `required_harnesses` list, produces this.
    A lost mode bit reports as a mode problem, never as an absent harness."""
    runner = _tree(tmp_path, private=True, test_remote_mode=0o644)
    p = _admit(runner)
    assert p.returncode == 3, p.stdout + p.stderr
    assert "not executable" in p.stderr
    assert "test-remote" in p.stderr
    assert "manifest row 'test-remote' has no" not in p.stderr


def test_missing_test_remote_is_a_manifest_admission_error_in_private_tree(
        tmp_path):
    """Deleting it outright is as bad as losing its executable bit (#446),
    but the two are now distinct reason codes."""
    runner = _tree(tmp_path, private=True, test_remote_mode=None)
    p = _admit(runner)
    assert p.returncode == 3, p.stdout + p.stderr
    assert "manifest row 'test-remote' has no" in p.stderr


def test_plan_mode_still_exits_before_admission(tmp_path):
    """Plan mode must stay side-effect-free: it exits before the manifest is
    even parsed, so a tree that admission would refuse still plans."""
    runner = _tree(tmp_path, private=True, test_remote_mode=None)
    p = _plan({}, runner=runner)
    assert p.returncode == 0, p.stderr
    assert "harnesses=" in p.stdout


def test_public_subset_tree_does_not_require_the_private_test_remote_harness(
        tmp_path):
    """The public mirror ships no bin/cctally-test-remote-test — .mirror-allowlist
    excludes both it and the wrapper as maintainer-local tooling, and excludes
    itself. Requiring it there hard-fails the public CI matrix before a single
    harness runs (issue #131: the public matrix runs the shipped subset only)."""
    runner = _tree(tmp_path, private=False, test_remote_mode=None)
    p = _plan({}, runner=runner)
    assert p.returncode == 0, p.stderr
    assert "test-remote" not in _kv(p.stdout)["harnesses"].split()


def test_wall_clock_rebuild_benchmark_runs_outside_xdist(tmp_path):
    """The lock-hold SLA must not compete with the parallel pytest estate."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    tests_dir = repo / "tests"
    fake_bin = tmp_path / "fake-bin"
    bindir.mkdir(parents=True)
    tests_dir.mkdir()
    fake_bin.mkdir()
    runner = bindir / "cctally-test-all"
    shutil.copy2(RUNNER, runner)
    shutil.copy2(CONTRACT_LIB, bindir / "_lib-test-contract.sh")

    for name in ("codex-quota", "source-aware", "reconcile"):
        harness = bindir / f"cctally-{name}-test"
        harness.write_text(
            "#!/usr/bin/env bash\nprintf 'passed: 1   failed: 0\\n'\n",
            encoding="utf-8",
        )
        harness.chmod(0o755)

    (tests_dir / "authoritative-test-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "minHarnessRows": 0,
                "harnesses": [
                    {
                        "name": n,
                        "visibility": "public",
                        "minCases": 0,
                        "countPolicy": "fixed",
                    }
                    for n in ("codex-quota", "source-aware", "reconcile")
                ],
                "capabilities": [],
                "forbiddenRegeneration": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (tests_dir / "test_rebuild_benchmark.py").write_text(
        "# execution is represented by the fake pytest below\n",
        encoding="utf-8",
    )
    calls = tmp_path / "pytest-calls"
    fake_python = fake_bin / "python3"
    # The pytest boundary is faked; everything else DELEGATES to the real
    # interpreter, because the contract library parses the manifest and encodes
    # the outcome object with stdlib json (`python3 -` / `python3 -c`). A stub
    # that answered 2 to every other form would fail the fixture, not the
    # property under test. The two optional-plugin probes keep their fixed
    # answers so this case stays independent of what is pip-installed.
    fake_python.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
    if [ "${3:-}" = "--version" ]; then
        exit 0
    fi
    printf '%s\\n' "$*" >> "$CCTALLY_FAKE_PYTHON_LOG"
    exit 0
fi
if [ "$1" = "-c" ]; then
    case "$2" in
        "import xdist"|"import pytest_timeout") exit 0 ;;
    esac
fi
exec "$CCTALLY_REAL_PYTHON3" "$@"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CCTALLY_FAKE_PYTHON_LOG": str(calls),
        "CCTALLY_REAL_PYTHON3": sys.executable,
        "CCTALLY_TEST_JOBS": "2",
    }
    proc = subprocess.run(
        [str(runner)], env=env, capture_output=True, text=True, timeout=30
    )

    assert proc.returncode == 0, proc.stderr
    invocations = calls.read_text(encoding="utf-8").splitlines()
    assert len(invocations) == 2, invocations
    bulk, benchmark = invocations
    assert "tests/" in bulk
    assert "--ignore=tests/test_rebuild_benchmark.py" in bulk
    assert " -n 2" in bulk
    assert "tests/test_rebuild_benchmark.py" in benchmark
    assert " -n " not in benchmark
