"""The suite directory end to end through bin/cctally-test-all (#963 spec 5.4, 5.9; test regime reset, session 4).

The suite directory replaced the suite generation in session 4. These cases
are the generation lifecycle's surviving half, carried over from the deleted
tests/test_suite_generation_lifecycle.py: where the legs allocate, that a
successful or interrupted run leaves nothing behind, that a nested session
cannot rotate the main leg's temp away, that a refused removal fails the run
as infrastructure, and the phase rows. The failure path with a TERM-ignoring
descendant is tests/test_test_all_scheduler.py's
test_test_all_removes_its_suite_dir_after_reaping.

Every case drives the REAL aggregator in a scratch estate
(tests/test_authoritative_test_contract.py `_estate`) with its TMPDIR inside
this test's allocation. An estate carries no `.mirror-allowlist`, so it runs
under the published tree's profile, which the aggregator never refuses for
want of a supervisor; the supervisor's run directory is handed on anyway.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

import pytest

import _lib_test_temp as T
from test_authoritative_test_contract import _estate
from test_test_all_observability import _SIGINT_RESET_SHIM

REPO = pathlib.Path(__file__).resolve().parents[1]
SUITE_RE = re.compile(r"^cs\.[A-Za-z0-9]{6}$")


def _child_tmp(tmp_path):
    root = tmp_path / "child-tmp"
    root.mkdir(exist_ok=True)
    return root


def _env(est, tmp_path, extra=None):
    root = str(_child_tmp(tmp_path))
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(est)),
        "CCTALLY_TEST_ALL_OUTCOME_FILE": str(est / "outcome.json"),
        "CCTALLY_TEST_JOBS": "1",
        "TMPDIR": root, "TMP": root, "TEMP": root,
    }
    if os.environ.get("CCTALLY_RUNNER_RUN_DIR"):
        env["CCTALLY_RUNNER_RUN_DIR"] = os.environ["CCTALLY_RUNNER_RUN_DIR"]
    env.update(extra or {})
    return env


def _run(est, tmp_path, extra=None, args=(), timeout=100):
    outcome = est / "outcome.json"
    if outcome.exists():
        outcome.unlink()
    proc = subprocess.run([str(est / "bin" / "cctally-test-all"), *args],
                          env=_env(est, tmp_path, extra), capture_output=True,
                          text=True, timeout=timeout)
    proc.outcome_path = outcome  # type: ignore[attr-defined]
    return proc


def _suites(tmp_path):
    return sorted(p for p in _child_tmp(tmp_path).iterdir() if SUITE_RE.match(p.name))


def _leftovers(tmp_path):
    return sorted(p.name for p in _child_tmp(tmp_path).iterdir())


def _phases(ev):
    rows = [line.split("\t") for line in
            next(ev.rglob("phases.tsv")).read_text(encoding="utf-8").splitlines()]
    return rows, {name: int(value) for name, value in rows}


_PLACEMENT_PROBE = (
    "import os\n"
    "def test_records_its_tmp_path(tmp_path):\n"
    "    with open(os.environ['S1_PROBE'], 'w') as fh:\n"
    "        fh.write(os.path.realpath(str(tmp_path)))\n"
)


def test_the_main_leg_allocates_inside_the_suite_directory(tmp_path):
    # alpha sleeps one second, so its recorded harness time is at least 1 s.
    est = _estate(tmp_path, harness_sleep={"alpha": 1})
    (est / "tests" / "test_placement_probe.py").write_text(_PLACEMENT_PROBE, encoding="utf-8")
    probe = tmp_path / "probe.txt"
    # No evidence root: the temporary LOGDIR lives in the suite directory the
    # run removes before the wall freeze.
    r = _run(est, tmp_path, {"S1_PROBE": str(probe)})
    assert r.returncode == 0, r.stdout + r.stderr
    recorded = probe.read_text(encoding="utf-8")
    root = re.escape(os.path.realpath(str(_child_tmp(tmp_path))))
    assert re.match(root + r"/cs\.[A-Za-z0-9]{6}/m/", recorded), (
        "the main leg's tmp_path is not inside the suite directory's main basetemp: "
        + recorded)
    # R30: the console's slowest-harness lines are read before the removal.
    assert "Slowest harnesses:\n" in r.stdout, r.stdout
    slowest = r.stdout.split("Slowest harnesses:\n", 1)[1]
    assert re.search(r"^ +[1-9][0-9]*s  alpha$", slowest, re.M), (
        "a run without an evidence root printed no non-zero harness timing:\n" + slowest)
    assert ".progress/phase.tmp" not in r.stderr, (
        "the teardown wrote into the removed suite directory:\n" + r.stderr)
    assert _leftovers(tmp_path) == [], _leftovers(tmp_path)


def test_two_successful_runs_leave_their_tmpdir_empty(tmp_path):
    est = _estate(tmp_path)
    (est / "tests" / "test_placement_probe.py").write_text(_PLACEMENT_PROBE, encoding="utf-8")
    for n in (1, 2):
        r = _run(est, tmp_path, {"S1_PROBE": str(tmp_path / "probe.txt"),
                                 "CCTALLY_TEST_EVIDENCE_ROOT": str(tmp_path / "ev")})
        assert r.returncode == 0, r.stdout + r.stderr
        assert _leftovers(tmp_path) == [], (
            "successful run %d left %r under its TMPDIR" % (n, _leftovers(tmp_path)))


_SENTINEL_MAIN = (
    "import os\n"
    "def test_leaves_a_sentinel(tmp_path):\n"
    "    sentinel = tmp_path / 'sentinel'\n"
    "    sentinel.write_text('main')\n"
    "    with open(os.environ['S1_SENTINEL'], 'w') as fh:\n"
    "        fh.write(str(sentinel))\n"
)
_NESTED_SESSIONS = (
    "import os, subprocess, sys\n"
    "def test_nested_sessions_spare_the_main_leg(tmp_path):\n"
    "    sentinel = open(os.environ['S1_SENTINEL']).read()\n"
    "    result = os.environ['S1_NESTED_RESULT']\n"
    "    child = tmp_path / 'test_nested_child.py'\n"
    "    child.write_text('def test_child(tmp_path):\\n    assert tmp_path.is_dir()\\n')\n"
    "    for k in range(1, 5):\n"
    "        proc = subprocess.run([sys.executable, '-m', 'pytest', str(child), '-q',\n"
    "                               '-p', 'no:cacheprovider'], cwd=str(tmp_path),\n"
    "                              capture_output=True, text=True, timeout=60)\n"
    "        assert proc.returncode == 0, proc.stdout + proc.stderr\n"
    "        if not os.path.exists(sentinel):\n"
    "            open(result, 'w').write('lost %d' % k)\n"
    "            raise AssertionError('lost %d' % k)\n"
    "    open(result, 'w').write('survived')\n"
)


def test_the_main_basetemp_survives_nested_sessions_in_the_benchmark_leg(tmp_path):
    """Four IMPLICIT nested sessions (no --basetemp) run in the benchmark leg
    after the main leg released its lock. Sharing one numbered namespace with
    the legs, the second of them rotated the main leg's temp away."""
    est = _estate(tmp_path)
    (est / "tests" / "test_main_sentinel.py").write_text(_SENTINEL_MAIN, encoding="utf-8")
    (est / "tests" / "test_rebuild_benchmark.py").write_text(_NESTED_SESSIONS, encoding="utf-8")
    result = tmp_path / "nested-result.txt"
    r = _run(est, tmp_path, {"S1_SENTINEL": str(tmp_path / "sentinel-path.txt"),
                             "S1_NESTED_RESULT": str(result)})
    verdict = result.read_text(encoding="utf-8") if result.exists() else "never ran"
    assert verdict == "survived", (
        "the main leg's sentinel did not survive nested session %s\n%s"
        % (verdict.split()[-1], (r.stdout + r.stderr)[-3000:]))
    assert r.returncode == 0, r.stdout + r.stderr


# A harness that plants, beside the suite directory's own entries, a chain of
# directories whose deep end no absolute path can name (longer than PATH_MAX on
# both macOS and Linux). The kernel walks by absolute path, so it reports an
# inspection error there and refuses the removal: a portable refusal, where a
# hard-linked immutable file needs chflags, which Linux lacks.
_PLANTING_HARNESS = (
    "#!/usr/bin/env bash\n"
    "if [ -n \"${S1_PLANT:-}\" ]; then\n"
    "  python3 -B -c '\n"
    "import os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
    "for name in [\"planted\"] + [\"d\" * 200] * 24:\n"
    "    os.mkdir(name, dir_fd=fd)\n"
    "    nxt = os.open(name, os.O_RDONLY, dir_fd=fd)\n"
    "    os.close(fd)\n"
    "    fd = nxt\n"
    "os.close(fd)\n"
    "' \"$(dirname \"$PYTEST_DEBUG_TEMPROOT\")\" || exit 1\n"
    "fi\n"
    "printf 'passed: 5   failed: 0\\n'\n"
)


def _unplant(suite):
    """Remove the planted chain descriptor-relatively, deepest first, then the
    rest of the suite directory through the kernel."""
    if not (suite / "planted").exists():
        return
    fds, names = [os.open(str(suite / "planted"), os.O_RDONLY)], []
    try:
        while True:
            kids = os.listdir(fds[-1])
            if not kids:
                break
            names.append(kids[0])
            fds.append(os.open(kids[0], os.O_RDONLY, dir_fd=fds[-1]))
        for i in range(len(names) - 1, -1, -1):
            os.close(fds.pop())
            os.rmdir(names[i], dir_fd=fds[i])
    finally:
        for fd in fds:
            os.close(fd)
    res = T.remove(str(suite))
    assert res.removed, res.findings


def test_a_refused_removal_fails_a_passing_run_as_infrastructure(tmp_path):
    """F-1 (spec 5.4, J9): a run whose tests all passed but whose own suite
    directory the kernel refuses to remove keeps that directory and fails as
    infrastructure with temp-generation-cleanup-failed, in the closed phase
    set's `pytest` phase (U7: no new phase)."""
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_PLANTING_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    r = _run(est, tmp_path, {"S1_PLANT": "1"})
    suites = _suites(tmp_path)
    try:
        assert len(suites) == 1, (
            "the refused suite directory was not kept: %r\n%s" % (_leftovers(tmp_path),
                                                                  r.stdout + r.stderr))
        suite = suites[0]
        assert (suite / "planted").is_dir(), "the harness planted nothing:\n" + r.stdout + r.stderr
        assert r.returncode == 3, r.stdout + r.stderr
        out = json.loads(r.outcome_path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
        assert out["failureClass"] == "infrastructure", out
        assert any(x["code"] == "temp-generation-cleanup-failed" and x["subject"] == suite.name
                   and x["phase"] == "pytest" for x in out["reasons"]), out["reasons"]
        assert ("suite directory %s could not be removed" % suite.name) in r.stderr, r.stderr
    finally:
        for suite in suites:
            _unplant(suite)
    assert _leftovers(tmp_path) == []


# A harness that, when S1_READY is set, publishes its process group (the pool's)
# and holds the run in the pool for 30 s: long enough to outlive the signal.
_SLOW_HARNESS = (
    "#!/usr/bin/env bash\n"
    "if [ -n \"${S1_READY:-}\" ]; then\n"
    "  ps -o pgid= -p $$ | tr -d ' ' > \"$S1_READY.tmp\" && mv \"$S1_READY.tmp\" \"$S1_READY\"\n"
    "  sleep 30\n"
    "fi\n"
    "printf 'passed: 5   failed: 0\\n'\n"
)


def _wait_for(path, proc, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text(encoding="utf-8").strip())
        if proc.poll() is not None:
            pytest.fail("the run exited (%s) before its pool started" % proc.returncode,
                        pytrace=False)
        time.sleep(0.1)
    pytest.fail("the run's pool never started", pytrace=False)


def _kill_group(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_group_gone(pgid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # #1006: macOS answers EPERM for a group whose last members are
            # zombies. The group still exists, so keep waiting.
            pass
        time.sleep(0.1)
    pytest.fail("process group %d outlived SIGKILL" % pgid, pytrace=False)


@pytest.mark.parametrize("signame,expected", [("TERM", 143), ("INT", 130)])
def test_an_interrupted_run_removes_its_suite_dir_once_reaped(tmp_path, signame, expected):
    """An interrupted run reaps the groups it started (here the pool, asleep in
    its harness) and, once they are gone, removes its suite directory."""
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_SLOW_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGINT_RESET_SHIM, str(est / "bin" / "cctally-test-all")],
        env=_env(est, tmp_path, {"S1_READY": str(ready)}),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pool = None
    try:
        pool = _wait_for(ready, proc)
        assert len(_suites(tmp_path)) == 1, _leftovers(tmp_path)
        proc.send_signal(getattr(signal, "SIG" + signame))
        assert proc.wait(timeout=60) == expected
        _wait_group_gone(pool)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pool:
            _kill_group(pool)
    assert _leftovers(tmp_path) == [], (
        "an interrupted run whose groups were reaped kept %r" % _leftovers(tmp_path))


def test_phases_tsv_records_both_pytest_legs_and_the_cleanup(tmp_path):
    est = _estate(tmp_path)
    (est / "tests" / "test_rebuild_benchmark.py").write_text(
        "def test_benchmark():\n    assert True\n", encoding="utf-8")
    ev = tmp_path / "ev"
    r = _run(est, tmp_path, {"CCTALLY_TEST_EVIDENCE_ROOT": str(ev)})
    assert r.returncode == 0, r.stdout + r.stderr
    rows, v = _phases(ev)
    names = [name for name, _ in rows]
    assert {"pytest-main", "pytest-benchmark", "cleanup"} <= set(names), (
        "phases.tsv lacks the pytest-main/pytest-benchmark/cleanup rows: %r" % names)
    assert names[:3] == ["total", "shell-pool", "pytest"], names
    assert v["pytest-main"] + v["pytest-benchmark"] <= v["pytest"] <= v["total"], v
    timing = r.stdout.split("Timing:", 1)[1].splitlines()[0]
    assert "cleanup" not in timing and "pytest-main" not in timing, timing


# ---- process groups: signalled only while this run still holds the number --
#
# A process group number is this run's only while the group has a member or
# its leader is an unwaited child: once the leader is waited and the last
# member is gone the number is free, and another process (another repository's
# listener runs unconfined as the same user on these hosts) can lead a group
# with it. The run therefore reaps each group right after its leader's `wait`
# returns, sends TERM or KILL only to a group a probe has just found present,
# and never addresses a group again once a probe found it gone.
#
# `kill` is a bash builtin, so a PATH shim cannot see it. BASH_ENV can: bash
# sources it before the script, and a function named `kill` shadows the
# builtin. The tracer records every call the aggregator makes, with its status
# and stderr, and still performs it.
_KILL_TRACER = (
    "kill() {\n"
    "    local _trace_err _trace_rc\n"
    "    _trace_err=$(builtin kill \"$@\" 2>&1 >/dev/null)\n"
    "    _trace_rc=$?\n"
    "    [ -z \"$_trace_err\" ] || printf '%s\\n' \"$_trace_err\" >&2\n"
    "    printf '%s|%s|%s|%s\\n' \"$$\" \"$*\" \"$_trace_rc\" \"$_trace_err\" >> \"$S1_KILL_LOG\"\n"
    "    return \"$_trace_rc\"\n"
    "}\n"
)


def _group_calls(log, pid):
    """The aggregator's own `kill <sig> -<group>` calls, in order."""
    calls = []
    for line in log.read_text(encoding="utf-8").splitlines():
        caller, args, rc, err = line.split("|", 3)
        argv = args.split()
        if caller != str(pid) or len(argv) != 2:
            continue
        sig, target = argv
        if target.startswith("-") and target[1:].isdigit():
            calls.append((sig, int(target[1:]), int(rc), err))
    return calls


def test_a_group_is_signalled_only_after_a_probe_found_it_present(tmp_path):
    """F1 (session 4 review): the run used to keep every group number it
    started and TERM and KILL all of them again at exit, long after each
    leader was waited, so a reused number could take another process group's
    signals. Each group is now reaped once, right after its leader's `wait`:
    a probe first, a signal only to a group the probe found present, and
    nothing at all to a group once a probe found it gone."""
    est = _estate(tmp_path)
    tracer = tmp_path / "kill-tracer.sh"
    tracer.write_text(_KILL_TRACER, encoding="utf-8")
    log = tmp_path / "kill.log"
    log.write_text("", encoding="utf-8")
    proc = subprocess.Popen([str(est / "bin" / "cctally-test-all")],
                            env=_env(est, tmp_path, {"BASH_ENV": str(tracer),
                                                     "S1_KILL_LOG": str(log)}),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=100)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, out
    calls = _group_calls(log, proc.pid)
    last = {}
    for sig, group, rc, err in calls:
        assert last.get(group) != "gone", (
            "group %d was addressed again after a probe found it gone: %r" % (group, calls))
        if sig == "-0":
            last[group] = "gone" if (rc != 0 and "No such process" in err) else "present"
        else:
            assert last.get(group) == "present", (
                "%s was sent to group %d without a probe that found it present: %r"
                % (sig, group, calls))
    # Non-vacuity: the pool and the main pytest leg are both reaped, each
    # through a probe that found it gone.
    gone = [group for group, verdict in last.items() if verdict == "gone"]
    assert len(gone) >= 2, calls
    assert _leftovers(tmp_path) == [], _leftovers(tmp_path)


def _group_probe(err, rc):
    """Run the aggregator's own `_suite_group_present` against a stub `kill`
    that fails with ``err`` and status ``rc``; return the probe's status."""
    source = (REPO / "bin" / "cctally-test-all").read_text(encoding="utf-8")
    match = re.search(r"^_suite_group_present\(\) \{[^\n]*\n.*?^\}\n", source, re.M | re.S)
    assert match, "bin/cctally-test-all defines no _suite_group_present"
    stub = "kill() { [ -z \"$STUB_ERR\" ] || printf '%s\\n' \"$STUB_ERR\" >&2; return \"$STUB_RC\"; }\n"
    proc = subprocess.run(["bash", "-c", stub + match.group(0) + '_suite_group_present 12345'],
                          env={"PATH": os.environ["PATH"], "STUB_ERR": err, "STUB_RC": str(rc)},
                          capture_output=True, text=True, timeout=30)
    return proc.returncode


def test_only_no_such_process_counts_as_a_gone_group():
    """Review Focus 3 and #1006: only ESRCH means the group is gone. EPERM is
    what macOS answers for a group whose last members are zombies, and the
    number is still that group's until they are reaped, so it counts as
    present; so does any failure the probe cannot read (fail closed)."""
    assert _group_probe("", 0) == 0
    assert _group_probe("bash: kill: (-12345) - No such process", 1) == 1
    assert _group_probe("bash: kill: (-12345) - Operation not permitted", 1) == 0
    assert _group_probe("bash: kill: something unexpected", 1) == 0
