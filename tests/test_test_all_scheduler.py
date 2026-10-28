"""Assertions for bin/cctally-test-all's worker model, harness discovery and
pytest legs.

Most tests invoke the runner's side-effect-free CCTALLY_TEST_ALL_PLAN=1 dry-run
(which exits before any harness/pytest launch) with
CCTALLY_TEST_ALL_FAKE_NCPU pinning the core count. The execution tests use a
synthetic checkout and a fake Python boundary to observe the two pytest legs.

WARNING: calls through ``_plan`` must keep plan mode enabled. The execution
tests build a synthetic checkout whose harnesses and Python boundary are fakes,
so they cannot recurse into the real suite. Short timeouts backstop both paths.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

import _lib_test_selection as _sel
import _temp_hygiene as _hyg
from tests.helpers import contract_tree as _ct

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
RUNNER = BIN / "cctally-test-all"


def _plan(env_overrides, fake_ncpu="16", runner=RUNNER, args=()):
    env = {
        "PATH": os.environ["PATH"],
        "CCTALLY_TEST_ALL_PLAN": "1",
        "CCTALLY_TEST_ALL_FAKE_NCPU": fake_ncpu,
    }
    env.update(env_overrides)
    proc = subprocess.run(
        [str(runner), *args], env=env, capture_output=True, text=True, timeout=30
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


# ----------------------------------------------------------- synthetic trees

_BASE_HARNESSES = ("codex-quota", "source-aware", "reconcile")


def _tree(
    tmp_path,
    *,
    durations=None,
    validator_body=None,
    extra_harnesses=(),
    harness_bodies=None,
):
    """A synthetic checkout. ``durations`` writes tests/harness-duration-hints.tsv from an iterable of
    (name, seconds) pairs; None omits the file. ``validator_body`` replaces
    bin/_lib_harness_durations.py with the given text (see
    tests/helpers/contract_tree.copy_aggregator)."""
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    runner = _ct.copy_aggregator(bindir, durations_validator=validator_body)
    harness_bodies = harness_bodies or {}
    for name in (*_BASE_HARNESSES, *extra_harnesses):
        _ct.write_harness(
            bindir, name, harness_bodies.get(name, "#!/usr/bin/env bash\nexit 0\n")
        )
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    if durations is not None:
        (tests_dir / "harness-duration-hints.tsv").write_text(
            "".join("%s\t%s\n" % (name, secs) for name, secs in durations)
        )
    return runner


def _run(runner, env_overrides=None, args=()):
    """Drive the runner past plan mode. The child's suite directory lies inside
    the test's allocation and is removed after (#963 J4)."""
    parent = pathlib.Path(runner).parents[2]
    env = _hyg.child_aggregator_env(
        {"PATH": os.environ["PATH"], "CCTALLY_TEST_JOBS": "1"}, parent)
    env.update(env_overrides or {})
    proc = subprocess.run(
        [str(runner), *args], env=env, capture_output=True, text=True, timeout=60
    )
    _hyg.reclaim_child_suite_dirs(parent)
    return proc


# ------------------------------------------------------- harness discovery


def test_nonexecutable_harness_matching_the_glob_is_a_usage_error(tmp_path):
    """Codex Q10: a matching file that lost its executable bit is an error,
    never silently absent coverage, and the plan says so as loudly as a run."""
    runner = _tree(tmp_path)
    (runner.parent / "cctally-source-aware-test").chmod(0o644)
    plan = _plan({}, runner=runner)
    assert plan.returncode == 2, plan.stdout + plan.stderr
    assert "cctally-source-aware-test" in plan.stderr
    assert "not executable" in plan.stderr
    run = _run(runner)
    assert run.returncode == 2, run.stdout + run.stderr
    assert "not executable" in run.stderr


def test_discovery_needs_no_manifest(tmp_path):
    """The harness set is the executable glob, sorted, with reconcile last."""
    runner = _tree(tmp_path, extra_harnesses=("alpha", "zeta"))
    p = _plan({}, runner=runner)
    assert p.returncode == 0, p.stderr
    assert _kv(p.stdout)["harnesses"] == (
        "alpha codex-quota source-aware zeta reconcile"
    )


# ------------------------------------- supervision and the suite directory


_REFUSAL = "cctally-test-all: run it through bin/cctally-test-remote bin/cctally-test-all"


def _fake_uname(tmp_path, system):
    fake = tmp_path / ("uname-" + system)
    fake.mkdir(exist_ok=True)
    (fake / "uname").write_text("#!/bin/sh\necho %s\n" % system)
    (fake / "uname").chmod(0o755)
    return fake


def _unsupervised_env(tmp_path, system, **extra):
    """A child environment on a faked platform, with the caller's run
    directory removed unless ``extra`` names one."""
    env = _hyg.child_aggregator_env({
        "PATH": f"{_fake_uname(tmp_path, system)}{os.pathsep}{os.environ['PATH']}",
        "CCTALLY_TEST_JOBS": "1",
    }, tmp_path)
    env.pop("CCTALLY_RUNNER_RUN_DIR", None)
    env.update(extra)
    return env


def _private(runner):
    """Mark a synthetic checkout as the private tree: `.mirror-allowlist` is
    the aggregator's profile discriminator, and the published tree has none."""
    (pathlib.Path(runner).parents[1] / ".mirror-allowlist").write_text("", encoding="utf-8")
    return runner


def test_test_all_refuses_unsupervised_on_darwin(tmp_path):
    """Session 4: on Darwin the private tree's aggregator runs only inside a
    supervised run, whose CCTALLY_RUNNER_RUN_DIR contains TMPDIR. Without it,
    or with TMPDIR outside it, the run is refused with exit 2 before anything
    executes. Hosted Linux CI runs it unsupervised, so the refusal is
    Darwin-only, and plan mode, which executes nothing, is never refused."""
    runner = _private(_tree(tmp_path))
    args = ("--harness", "codex-quota")

    def run(env):
        return subprocess.run([str(runner), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    bare = run(_unsupervised_env(tmp_path, "Darwin"))
    assert bare.returncode == 2, bare.stdout + bare.stderr
    assert _REFUSAL in bare.stderr.splitlines(), bare.stderr

    elsewhere = tmp_path / "other-run"
    elsewhere.mkdir()
    outside = run(_unsupervised_env(tmp_path, "Darwin",
                                    CCTALLY_RUNNER_RUN_DIR=str(elsewhere)))
    assert outside.returncode == 2, outside.stdout + outside.stderr
    assert _REFUSAL in outside.stderr.splitlines(), outside.stderr

    supervised = run(_unsupervised_env(tmp_path, "Darwin",
                                       CCTALLY_RUNNER_RUN_DIR=str(tmp_path)))
    assert supervised.returncode == 3, supervised.stdout + supervised.stderr
    assert _REFUSAL not in supervised.stderr

    linux = run(_unsupervised_env(tmp_path, "Linux"))
    assert linux.returncode == 3, linux.stdout + linux.stderr
    assert _REFUSAL not in linux.stderr

    plan = _plan({"PATH": f"{tmp_path / 'uname-Darwin'}{os.pathsep}{os.environ['PATH']}"},
                 runner=runner)
    assert plan.returncode == 0, plan.stderr
    _hyg.reclaim_child_suite_dirs(tmp_path)


def test_the_published_tree_is_never_refused_on_darwin(tmp_path):
    """F2 (session 4 review): the published tree carries no runner client, and
    its CONTRIBUTING.md tells a contributor to run bin/cctally-test-all, so it
    behaves there exactly as it did before session 4: unsupervised on Darwin,
    the run executes."""
    runner = _tree(tmp_path)
    proc = subprocess.run([str(runner), "--harness", "codex-quota"],
                          env=_unsupervised_env(tmp_path, "Darwin"),
                          capture_output=True, text=True, timeout=60)
    _hyg.reclaim_child_suite_dirs(tmp_path)
    assert _REFUSAL not in proc.stderr, proc.stderr
    assert proc.returncode == 3, proc.stdout + proc.stderr


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_test_all_removes_its_suite_dir_after_reaping(tmp_path):
    """Session 4 (spec C8): the suite directory is ``cs.XXXXXX`` beneath the
    TMPDIR the run inherits, holding PYTEST_DEBUG_TEMPROOT. At exit the run
    TERMs every process group it started, KILLs what outlives the grace and
    removes the directory: a harness that leaves a TERM-ignoring descendant
    behind in the pool's group neither keeps it alive nor keeps the directory."""
    record = tmp_path / "lingering.txt"
    body = (
        "#!/usr/bin/env bash\n"
        "trap '' TERM\n"
        "sleep 300 </dev/null >/dev/null 2>&1 &\n"
        "printf '%%s\\n%%s\\n' \"$!\" \"$PYTEST_DEBUG_TEMPROOT\" > %s\n"
        "exit 0\n" % record
    )
    runner = _tree(tmp_path, extra_harnesses=("lingering",),
                   harness_bodies={"lingering": body})
    env = _hyg.child_aggregator_env(
        {"PATH": os.environ["PATH"], "CCTALLY_TEST_JOBS": "1"}, tmp_path)
    child_tmp = pathlib.Path(env["TMPDIR"])
    pid = None
    try:
        proc = subprocess.run([str(runner), "--harness", "lingering"], env=env,
                              capture_output=True, text=True, timeout=90)
        assert proc.returncode == 3, proc.stdout + proc.stderr
        pid_text, temproot = record.read_text(encoding="utf-8").splitlines()
        pid = int(pid_text)
        suite = pathlib.Path(temproot).parent
        assert suite.parent == child_tmp, (suite, child_tmp)
        assert re.match(r"^cs\.[A-Za-z0-9]{6}$", suite.name), suite.name
        assert pathlib.Path(temproot).name == "t"
        assert not _pid_alive(pid), "the lingering descendant %d outlived the run" % pid
        assert not suite.exists(), "the suite directory %s was not removed" % suite
        assert [p.name for p in child_tmp.iterdir()] == []
    finally:
        if pid is not None and _pid_alive(pid):
            os.kill(pid, 9)


def test_the_linux_matrix_variable_is_no_longer_a_profile(tmp_path):
    """The dead private Docker profile is gone: the variable selects nothing
    and refuses nothing."""
    runner = _tree(tmp_path)
    base = _plan({}, runner=runner)
    with_var = _plan({"CCTALLY_LINUX_MATRIX_RUN": "1"}, runner=runner)
    assert with_var.returncode == 0, with_var.stderr
    assert _kv(with_var.stdout)["mode"] == "full"
    assert with_var.stdout == base.stdout


@pytest.mark.parametrize("flag,extra", [
    ("--tier-fast", ("main",)),
    ("--merge-gate", ("main",)),
    ("--merge-gate-worker", ("main",)),
    ("--with-estate-check", ()),
])
def test_retired_flags_are_usage_errors(tmp_path, flag, extra):
    runner = _tree(tmp_path)
    p = _plan({}, runner=runner, args=(flag, *extra))
    assert p.returncode == 2, p.stdout + p.stderr
    assert flag in p.stderr
    assert "retired" in p.stderr


# -------------------------------------------------- the harness exit status

_SUMMARY_LIES_PASS = (
    "#!/usr/bin/env bash\nprintf 'passed: 0   failed: 3\\n'\nexit 0\n"
)
_SUMMARY_LIES_FAIL = (
    "#!/usr/bin/env bash\nprintf 'passed: 5   failed: 0\\n'\nexit 1\n"
)
_NO_SUMMARY_EXIT_2 = "#!/usr/bin/env bash\necho 'could not start'\nexit 2\n"


def _outcome_reasons(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc, {(r["code"], r["subject"]) for r in doc["reasons"]}


def test_harness_exit_status_decides_pass_fail_incomplete(tmp_path):
    """Codex Q10: 0 passes, 1 fails, anything else is incomplete. The summary
    line is diagnostic only: a harness that exits 0 while printing failures
    passes, and one that exits 1 while printing none fails."""
    runner = _tree(
        tmp_path,
        extra_harnesses=("liar-pass", "liar-fail", "broken"),
        harness_bodies={
            "liar-pass": _SUMMARY_LIES_PASS,
            "liar-fail": _SUMMARY_LIES_FAIL,
            "broken": _NO_SUMMARY_EXIT_2,
        },
    )
    outcome = tmp_path / "outcome.json"
    p = _run(runner, {"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)},
             args=("--harness", "liar-pass", "--harness", "liar-fail",
                   "--harness", "broken"))
    doc, reasons = _outcome_reasons(outcome)
    harness_reasons = {r for r in reasons if r[0] != "deliberate-subset"}
    assert harness_reasons == {
        ("harness-failed", "liar-fail"),
        ("harness-incomplete", "broken"),
    }, (doc, p.stderr)
    assert doc["failureClass"] == "incomplete"
    assert p.returncode == 3


def test_a_harness_killed_by_a_signal_is_infrastructure(tmp_path):
    runner = _tree(
        tmp_path,
        extra_harnesses=("killed",),
        harness_bodies={"killed": "#!/usr/bin/env bash\nkill -9 $$\n"},
    )
    outcome = tmp_path / "outcome.json"
    _run(runner, {"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)},
         args=("--harness", "killed"))
    _doc, reasons = _outcome_reasons(outcome)
    assert ("harness-killed", "killed") in reasons


# ------------------------------------------------------------ the pytest legs

# The files the SERIAL entries of bin/_lib_test_selection.py name, derived
# rather than restated (two are mirror-private, and a fake tree writes them
# all whatever the profile).
_SERIAL_FILES = tuple(sorted({entry.split("::")[0] for entry in _sel.SERIAL}))
_STORM_H1 = ("tests/test_stats_writer_storm_386.py::"
             "test_h1_multiwriter_baseline_stays_intact")
_STORM_READER = ("tests/test_stats_writer_storm_386.py::"
                 "test_dashboard_source_reader_releases_rollback_snapshot")


def _quarantine_entry(node):
    return {'id': node, 'evidence': 'inconsistent on unchanged code', 'invariant': 'x',
            'issue': '#1', 'replacement': 'none needed', 'reviewBy': '2026-10-27'}


def _fake_pytest_tree(tmp_path, *, serial_files=_SERIAL_FILES, quarantine=None,
                      pytest_exit=None, extra_env=None):
    """A synthetic checkout whose pytest boundary is a recording fake.

    Everything but `python3 -m pytest` DELEGATES to the real interpreter,
    because the aggregator builds its legs and encodes the outcome with
    stdlib Python. ``pytest_exit`` maps a leg (`bulk` for the one carrying
    `tests/`, `serial` for the other) to the status the fake returns.

    Returns `(runner, calls, env)`; `calls` is the file every faked pytest
    argv is appended to, in invocation order, prefixed with the PYTEST_ADDOPTS
    the leg was given.
    """
    repo = tmp_path / "repo"
    bindir = repo / "bin"
    tests_dir = repo / "tests"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    runner = _ct.copy_aggregator(bindir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    for name in _BASE_HARNESSES:
        _ct.write_harness(
            bindir, name,
            "#!/usr/bin/env bash\nprintf 'passed: 1   failed: 0\\n'\n")
    for rel in serial_files:
        (repo / rel).write_text("# executed by the fake pytest below\n",
                                encoding="utf-8")
    if quarantine is not None:
        (tests_dir / "quarantine.json").write_text(json.dumps(quarantine))
    calls = tmp_path / "pytest-calls"
    fake_python = fake_bin / "python3"
    exits = pytest_exit or {}
    fake_python.write_text(
        """#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
    if [ "${3:-}" = "--version" ]; then
        exit 0
    fi
    printf 'ADDOPTS=[%%s] %%s\\n' "${PYTEST_ADDOPTS:-}" "$*" >> "$CCTALLY_FAKE_PYTHON_LOG"
    leg=serial
    for a in "$@"; do [ "$a" = tests/ ] && leg=bulk; done
    if [ "$leg" = bulk ]; then rc=%s; else rc=%s; fi
    echo "1 passed in 0.01s"
    exit "$rc"
fi
if [ "$1" = "-c" ]; then
    case "$2" in
        "import xdist"|"import pytest_timeout") exit 0 ;;
    esac
fi
exec "$CCTALLY_REAL_PYTHON3" "$@"
""" % (exits.get("bulk", 0), exits.get("serial", 0)),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = _hyg.child_aggregator_env({
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CCTALLY_FAKE_PYTHON_LOG": str(calls),
        "CCTALLY_REAL_PYTHON3": sys.executable,
        "CCTALLY_TEST_JOBS": "2",
    }, tmp_path)
    env.update(extra_env or {})
    return runner, calls, env


def _drive(tmp_path, runner, env):
    proc = subprocess.run(
        [str(runner)], env=env, capture_output=True, text=True, timeout=60
    )
    _hyg.reclaim_child_suite_dirs(tmp_path)
    return proc


def _legs(calls):
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    bulk = [line for line in lines if " tests/ " in line + " "]
    serial = [line for line in lines if line not in bulk]
    return bulk, serial


def test_bulk_leg_ignores_serial_files_and_deselects_serial_and_quarantined_nodes(
        tmp_path):
    flaky = "tests/test_alpha.py::test_flaky"
    runner, calls, env = _fake_pytest_tree(
        tmp_path, quarantine=[_quarantine_entry(flaky)])
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (bulk,), (serial,) = _legs(calls)
    serial_files = [entry for entry in _sel.SERIAL if "::" not in entry]
    assert "tests/test_rebuild_benchmark.py" in serial_files
    for rel in serial_files:
        assert f"--ignore={rel}" in bulk, bulk
        assert rel in serial.split(), serial
    for node in (_STORM_H1, _STORM_READER):
        assert f"--deselect={node}" in bulk, bulk
        assert node in serial.split(), serial
    assert f"--deselect={flaky}" in bulk, bulk
    assert f"--deselect={flaky}" in serial, serial
    assert " -n 2" in bulk
    assert " -n " not in serial
    # The retired estate observer is no longer loaded by either leg.
    assert "_estate_leg_plugin" not in bulk + serial


def test_a_serial_target_absent_from_the_tree_is_not_selected(tmp_path):
    """The public mirror carries fewer tests: an entry naming a file the tree
    does not have is neither ignored nor run."""
    runner, calls, env = _fake_pytest_tree(
        tmp_path, serial_files=("tests/test_rebuild_benchmark.py",))
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (bulk,), (serial,) = _legs(calls)
    assert "test_quota_mutation_coverage" not in bulk + serial
    assert serial.split()[1:].count("tests/test_rebuild_benchmark.py") == 1


def test_leg_emptied_by_selection_is_omitted(tmp_path):
    """Selection empties the serial leg when every serial target is absent or
    quarantined; the leg is then omitted rather than run to an exit 5."""
    runner, calls, env = _fake_pytest_tree(
        tmp_path,
        serial_files=("tests/test_stats_writer_storm_386.py",),
        quarantine=[_quarantine_entry(_STORM_H1),
                    _quarantine_entry(_STORM_READER)],
        pytest_exit={"serial": 5},
    )
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    bulk, serial = _legs(calls)
    assert len(bulk) == 1 and serial == [], calls.read_text()


def test_pytest_exit_5_on_a_nonempty_leg_is_incomplete(tmp_path):
    outcome = tmp_path / "outcome.json"
    runner, calls, env = _fake_pytest_tree(
        tmp_path, pytest_exit={"serial": 5},
        extra_env={"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)})
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    doc, reasons = _outcome_reasons(outcome)
    assert doc["failureClass"] == "incomplete"
    assert ("pytest-collected-nothing", "benchmark") in reasons, reasons


def test_pytest_exit_5_on_the_bulk_leg_is_incomplete(tmp_path):
    # The bulk leg is never emptied by the selection (it always carries
    # `tests/`), so collecting nothing there is a run that verified nothing:
    # it must be incomplete, never the pass a zero-failure leg would read as.
    outcome = tmp_path / "outcome.json"
    runner, calls, env = _fake_pytest_tree(
        tmp_path, pytest_exit={"bulk": 5},
        extra_env={"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)})
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    doc, reasons = _outcome_reasons(outcome)
    assert doc["failureClass"] == "incomplete"
    assert ("pytest-collected-nothing", "pytest") in reasons, reasons


def test_pytest_addopts_does_not_reach_either_execution_leg(tmp_path):
    """#648 M13: `PYTEST_ADDOPTS=--ignore=tests/<file>.py` would narrow a run,
    so the execution legs are sanitized."""
    runner, calls, env = _fake_pytest_tree(
        tmp_path,
        extra_env={"PYTEST_ADDOPTS": "--ignore=tests/test_rebuild_benchmark.py"},
    )
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, lines
    for line in lines:
        assert line.startswith("ADDOPTS=[] "), line
    assert "PYTEST_ADDOPTS" in proc.stderr


def test_the_durations_variable_is_the_supported_route_addopts_used_to_be(
        tmp_path):
    """A narrow variable that cannot NARROW anything replaces the
    `PYTEST_ADDOPTS=--durations=0` route the sanitizing removed."""
    runner, calls, env = _fake_pytest_tree(
        tmp_path, extra_env={"CCTALLY_PYTEST_DURATIONS": "1"},
    )
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for line in calls.read_text(encoding="utf-8").splitlines():
        assert "--durations=0" in line, line


def test_a_tree_without_a_quarantine_list_deselects_nothing(tmp_path):
    # The public mirror may carry no list: nothing is deselected.
    runner, calls, env = _fake_pytest_tree(tmp_path)
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--deselect=tests/test_alpha.py" not in calls.read_text()


def test_the_leg_durations_are_printed(tmp_path):
    """The first reduced full run records its leg durations from this line."""
    runner, _calls, env = _fake_pytest_tree(tmp_path)
    proc = _drive(tmp_path, runner, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    legs = [line for line in proc.stdout.splitlines()
            if line.startswith("Pytest legs:")]
    assert len(legs) == 1, proc.stdout
    assert "bulk=" in legs[0] and "serial=" in legs[0]


# ------------------------------------------- the effective-configuration plan
#
# With all three roles explicitly exported, the internal BUDGET intermediate no
# longer influences any worker count (#529 S5 section 2.3).


def test_plan_budget_reports_the_effective_configuration_not_the_core_count():
    env = {
        "CCTALLY_OUTER_JOBS": "4",
        "CCTALLY_INNER_JOBS": "2",
        "CCTALLY_PYTEST_JOBS": "10",
    }
    small = _plan(env, fake_ncpu="8")
    large = _plan(env, fake_ncpu="16")
    assert small.returncode == 0, small.stderr
    assert large.returncode == 0, large.stderr
    assert _kv(small.stdout)["budget"] == "4/2/10", small.stdout
    assert _kv(large.stdout)["budget"] == "4/2/10", large.stdout

    def _without_ncpu(text):
        return [line for line in text.splitlines() if not line.startswith("ncpu=")]

    assert _without_ncpu(small.stdout) == _without_ncpu(large.stdout)


def test_plan_budget_still_reflects_a_combined_knob():
    kv = _kv(_plan({"CCTALLY_TEST_JOBS": "4"}, fake_ncpu="16").stdout)
    assert kv["budget"] == "4/4/4"


# ------------------------------------------------- plan mode and subset runs


def test_plan_mode_reports_the_subset_selection(tmp_path):
    runner = _tree(tmp_path)
    p = _plan({}, runner=runner, args=("--harness", "source-aware"))
    assert p.returncode == 0, p.stdout + p.stderr
    kv = _kv(p.stdout)
    assert kv["mode"] == "subset"
    assert kv["pytest_disposition"] == "skipped"
    assert kv["harnesses"] == "source-aware"
    assert "selected" not in kv, kv


def test_plan_mode_reports_a_full_run_as_full(tmp_path):
    subset = _kv(
        _plan({}, runner=_tree(tmp_path), args=("--harness", "source-aware")).stdout
    )
    full = _kv(_plan({}).stdout)
    assert full["mode"] == "full"
    assert full["pytest_disposition"] == "full"
    names = full["harnesses"].split()
    assert len(names) > len(subset["harnesses"].split())
    assert names[-1] == "reconcile", names


def test_plan_mode_with_a_subset_stays_side_effect_free(tmp_path):
    runner = _tree(tmp_path)
    evidence = tmp_path / "ev"
    p = _plan(
        {"CCTALLY_TEST_EVIDENCE_ROOT": str(evidence)},
        runner=runner,
        args=("--harness", "source-aware"),
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert not evidence.exists(), "plan mode must remain side-effect free"


def test_plan_mode_still_rejects_an_unknown_harness(tmp_path):
    runner = _tree(tmp_path)
    p = _plan({}, runner=runner, args=("--harness", "no-such-harness"))
    assert p.returncode == 2, p.stdout + p.stderr
    assert "no-such-harness" in p.stderr


def test_plan_mode_with_a_subset_publishes_no_outcome_record(tmp_path):
    runner = _tree(tmp_path)
    outcome = tmp_path / "outcome.json"
    p = _plan(
        {"CCTALLY_TEST_ALL_OUTCOME_FILE": str(outcome)},
        runner=runner,
        args=("--harness", "source-aware"),
    )
    assert p.returncode == 2, p.stdout + p.stderr
    assert not outcome.exists()


# ------------------------------------------- dispatch order vs report order
#
# preserve-item 29: the printed harness row order is deterministic and
# independent of dispatch order. Duration rows are HINTS (Codex Q10): a
# missing, stale or malformed row orders nothing and suppresses nothing.


def test_plan_reports_dispatch_order_and_scheduler_mode_separately(tmp_path):
    runner = _tree(tmp_path)
    lines = _kv(_plan({}, runner=runner).stdout)
    assert "harnesses" in lines and "dispatch_harnesses" in lines
    assert lines["scheduler"] == "fallback"
    assert lines["dispatch_harnesses"] == lines["harnesses"]


def test_dispatch_is_longest_first_and_ties_break_by_report_position(tmp_path):
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt"
    assert lines["harnesses"] == "codex-quota source-aware reconcile"
    assert lines["dispatch_harnesses"] == "source-aware reconcile codex-quota"


def test_missing_duration_hint_never_suppresses_a_harness(tmp_path):
    """A table that names one harness, a harness nobody has timed and a row for
    a harness that no longer exists still dispatches every discovered harness:
    unhinted ones first, in report order, then the hinted ones longest-first."""
    runner = _tree(
        tmp_path,
        extra_harnesses=("new",),
        durations=[("source-aware", 40), ("retired-long-ago", 99),
                   ("codex-quota", "not-a-number")],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt", proc.stderr
    assert lines["harnesses"] == "codex-quota new source-aware reconcile"
    assert lines["dispatch_harnesses"] == "codex-quota new reconcile source-aware"
    # And a real run executes the unhinted harness.
    sentinel = tmp_path / "new-ran"
    (runner.parent / "cctally-new-test").write_text(
        "#!/usr/bin/env bash\ntouch %s\n" % sentinel)
    _run(runner, args=("--harness", "new"))
    assert sentinel.exists()


def test_plan_mode_writes_no_bytecode_into_the_tree_it_reads(tmp_path):
    """Preserve-item 25: plan mode stays side-effect-free; `python3 -B` keeps
    the scheduler's import from writing bin/__pycache__."""
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    assert _kv(proc.stdout)["scheduler"] == "lpt", proc.stdout + proc.stderr
    repo = runner.parent.parent
    caches = sorted(str(p.relative_to(repo)) for p in repo.rglob("__pycache__"))
    assert caches == [], caches


@pytest.mark.parametrize(
    "mutation,expected_reason",
    [
        ("missing", "no duration hints"),
        ("validator-unimportable", "no duration hint reader"),
    ],
)
def test_without_usable_hints_dispatch_falls_back_to_report_order(
    tmp_path, mutation, expected_reason
):
    rows = [("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)]
    kwargs = {}
    if mutation == "missing":
        rows = None
    elif mutation == "validator-unimportable":
        kwargs["validator_body"] = (
            'raise ImportError("synthetic: this module cannot be loaded")\n'
        )
    runner = _tree(tmp_path, durations=rows, **kwargs)
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "fallback"
    assert lines["dispatch_harnesses"] == lines["harnesses"]
    assert expected_reason in proc.stderr, proc.stderr


def test_a_fallback_diagnostic_never_reaches_the_dispatch_list(tmp_path):
    """The capture takes stdout alone: PYTHONVERBOSE writes import lines to
    stderr on a zero exit, and none of them may become a harness name."""
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)],
    )
    proc = _plan({"PYTHONVERBOSE": "1"}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt", proc.stdout + proc.stderr
    assert lines["dispatch_harnesses"] == "source-aware codex-quota reconcile"


def test_a_good_table_produces_no_diagnostic_at_all(tmp_path):
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 5)],
    )
    proc = _plan({}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    assert _kv(proc.stdout)["scheduler"] == "lpt", proc.stdout
    blank = [line for line in proc.stderr.splitlines() if not line.strip()]
    assert not blank, repr(proc.stderr)
    assert "scheduler:" not in proc.stderr, proc.stderr


def test_the_dispatch_override_forces_report_order_on_an_identical_tree(tmp_path):
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    baseline = _kv(_plan({}, runner=runner).stdout)
    proc = _plan({"CCTALLY_TEST_ALL_DISPATCH": "report"}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "report-forced"
    assert lines["dispatch_harnesses"] == lines["harnesses"]
    assert baseline["scheduler"] == "lpt"
    assert baseline["dispatch_harnesses"] != baseline["harnesses"]
    assert lines["harnesses"] == baseline["harnesses"]


def test_the_dispatch_override_accepts_the_shipped_arm_explicitly(tmp_path):
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    proc = _plan({"CCTALLY_TEST_ALL_DISPATCH": "lpt"}, runner=runner)
    assert proc.returncode == 0, proc.stderr
    lines = _kv(proc.stdout)
    assert lines["scheduler"] == "lpt"
    assert lines["dispatch_harnesses"] == "source-aware reconcile codex-quota"


def test_an_unrecognized_dispatch_override_refuses_rather_than_guessing(tmp_path):
    runner = _tree(
        tmp_path,
        durations=[("codex-quota", 10), ("source-aware", 40), ("reconcile", 40)],
    )
    proc = _plan({"CCTALLY_TEST_ALL_DISPATCH": "reoprt"}, runner=runner)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "CCTALLY_TEST_ALL_DISPATCH" in proc.stderr
    assert "reoprt" in proc.stderr
