"""Plan-mode assertions for bin/cctally-test-all's three-role worker model (#292).

Invokes the runner's side-effect-free CCTALLY_TEST_ALL_PLAN=1 dry-run (which
exits before any harness/pytest launch) with CCTALLY_TEST_ALL_FAKE_NCPU pinning
the core count, and asserts the resolved outer/inner/pytest split + validation.

WARNING: only ever runs the runner in PLAN mode. Running it without plan mode
would execute the whole suite (which re-invokes this runner). A short timeout
is a backstop against that.
"""
import pathlib
import shutil
import subprocess

RUNNER = pathlib.Path(__file__).resolve().parents[1] / "bin" / "cctally-test-all"


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


def test_plan_explicitly_includes_test_remote_harness():
    p = _plan({})
    assert p.returncode == 0, p.stderr
    assert "test-remote" in _kv(p.stdout)["harnesses"].split()


def _tree(tmp_path, *, private, test_remote_mode):
    """A synthetic checkout. ``private`` writes .mirror-allowlist, which is
    itself unmirrored and so marks the private tree; ``test_remote_mode`` is
    None to omit the maintainer-local harness entirely (the public shape)."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    bindir.mkdir(parents=True)
    runner = bindir / "cctally-test-all"
    shutil.copy2(RUNNER, runner)

    for name in ("codex-quota", "source-aware"):
        harness = bindir / f"cctally-{name}-test"
        harness.write_text("#!/usr/bin/env bash\nexit 0\n")
        harness.chmod(0o755)

    if private:
        (repo / ".mirror-allowlist").write_text("bin/cctally-test-all\n")
    if test_remote_mode is not None:
        test_remote = bindir / "cctally-test-remote-test"
        test_remote.write_text("#!/usr/bin/env bash\nexit 0\n")
        test_remote.chmod(test_remote_mode)
    return runner


def test_non_executable_test_remote_is_a_required_harness_error(tmp_path):
    runner = _tree(tmp_path, private=True, test_remote_mode=0o644)
    p = _plan({}, runner=runner)
    assert p.returncode == 2
    assert "required harness is not executable: test-remote" in p.stderr


def test_missing_test_remote_is_a_required_harness_error_in_private_tree(
        tmp_path):
    """Deleting it outright is as bad as losing its executable bit (#446)."""
    runner = _tree(tmp_path, private=True, test_remote_mode=None)
    p = _plan({}, runner=runner)
    assert p.returncode == 2
    assert "required harness is not executable: test-remote" in p.stderr


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
