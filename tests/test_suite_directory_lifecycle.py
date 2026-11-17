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
# and holds the run in the pool for S1_SLEEP seconds (30 by default): long
# enough to outlive the signal, and bounded, so it ends by itself.
_SLOW_HARNESS = (
    "#!/usr/bin/env bash\n"
    "if [ -n \"${S1_READY:-}\" ]; then\n"
    "  ps -o pgid= -p $$ | tr -d ' ' > \"$S1_READY.tmp\" && mv \"$S1_READY.tmp\" \"$S1_READY\"\n"
    "  sleep \"${S1_SLEEP:-30}\"\n"
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


def _wait_group_gone(pgid, timeout=20):
    """Wait until group ``pgid`` has no member, probing with signal 0 only.
    The default bound covers the holder's 5 s grace and 5 s kill wait."""
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
    try:
        pool = _wait_for(ready, proc)
        assert len(_suites(tmp_path)) == 1, _leftovers(tmp_path)
        proc.send_signal(getattr(signal, "SIG" + signame))
        assert proc.wait(timeout=60) == expected
        _wait_group_gone(pool)
    finally:
        # Never a signal to the pool's group number from here: once its
        # holder has reaped the leader, the number is no longer this run's.
        if proc.poll() is None:
            proc.kill()
            proc.wait()
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


# ---- process groups: settled by their holders, never signalled by the run --
#
# #1087: a process group's number is this run's only while its leader is an
# unreaped child, and bash reaps a finished background job on its own, so a
# `kill` from this script could reach whichever process was given the number
# next (another repository's listener runs unconfined as the same user on the
# runners). The pool and each pytest leg therefore run under a group-scope
# holder (bin/_lib_hold.py), their leader's parent and only reaper, which
# signals the group only while it holds that leader; the aggregator asks the
# holder to stop and reads the settlement it publishes.
#
# `kill` is a bash builtin, so a PATH shim cannot see it. BASH_ENV can: bash
# sources it before the script, and a function named `kill` shadows the
# builtin. The tracer records every call, with its status, and still performs
# it. The holders record their own signal calls through CCTALLY_HOLD_TRACE.
_KILL_TRACER = (
    "kill() {\n"
    "    local _trace_rc=0\n"
    "    builtin kill \"$@\" || _trace_rc=$?\n"
    "    printf '%s|%s|%s\\n' \"$$\" \"$*\" \"$_trace_rc\" >> \"$S1_KILL_LOG\"\n"
    "    return \"$_trace_rc\"\n"
    "}\n"
)

# A pool harness that leaves a process behind in the pool's group: it traps
# TERM, recording that it received it, and publishes its group before the
# harness exits. Self-bounded at 30 s.
_LEFTOVER_PY = (
    "import os, signal, sys, time\n"
    "def _termed(*_):\n"
    "    open(sys.argv[2], 'w').write('termed')\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, _termed)\n"
    "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpgrp()))\n"
    "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
    "time.sleep(30)\n"
)
_LEFTOVER_HARNESS = (
    "#!/usr/bin/env bash\n"
    "if [ -n \"${S1_LEFTOVER:-}\" ]; then\n"
    "  python3 -c \"$S1_LEFTOVER_PY\" \"$S1_LEFTOVER\" \"$S1_TERMED\" >/dev/null 2>&1 &\n"
    "  while [ ! -f \"$S1_LEFTOVER\" ]; do sleep 0.1; done\n"
    "fi\n"
    "printf 'passed: 5   failed: 0\\n'\n"
)


def _aggregator_kills(log, pid):
    """The aggregator's own `kill` calls (its reporter subshell shares `$$`)."""
    calls = []
    for line in log.read_text(encoding="utf-8").splitlines():
        caller, args, rc = line.split("|", 2)
        if caller == str(pid):
            calls.append((args.split(), int(rc)))
    return calls


def _holder_signals(trace):
    """Every signal a holder delivered, as (call, target, signal), in order.
    Signal 0 probes are left out: they deliver nothing."""
    out = []
    if not trace.exists():
        return out
    for line in trace.read_text(encoding="utf-8").splitlines():
        call, target, sig, _result = json.loads(line)
        if sig != 0:
            out.append((call, target, sig))
    return out


def test_the_run_signals_nothing_and_its_holders_settle_each_group(tmp_path):
    """#1087 (replaces the F1 probe-then-signal case): the aggregator sends no
    signal to any process number; the pool and the main leg are each settled
    by their holder, which TERMs the whole group, so a pool harness's leftover
    in the pool's group receives TERM and is gone, and no survivor is noted."""
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_LEFTOVER_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    tracer = tmp_path / "kill-tracer.sh"
    tracer.write_text(_KILL_TRACER, encoding="utf-8")
    log = tmp_path / "kill.log"
    log.write_text("", encoding="utf-8")
    trace = tmp_path / "hold-trace.jsonl"
    leftover, termed = tmp_path / "leftover.pgid", tmp_path / "leftover.termed"
    proc = subprocess.Popen(
        [str(est / "bin" / "cctally-test-all")],
        env=_env(est, tmp_path, {
            "BASH_ENV": str(tracer), "S1_KILL_LOG": str(log),
            "CCTALLY_HOLD_TRACE": str(trace),
            "S1_LEFTOVER": str(leftover), "S1_TERMED": str(termed),
            "S1_LEFTOVER_PY": _LEFTOVER_PY,
        }),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=100)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, out
    delivering = [(argv, rc) for argv, rc in _aggregator_kills(log, proc.pid)
                  if not argv or argv[0] != "-0"]
    assert delivering == [], "the aggregator signalled a process number: %r" % delivering
    terms = [target for call, target, sig in _holder_signals(trace)
             if call == "killpg" and sig == signal.SIGTERM]
    # Non-vacuity: the pool's and the main leg's holders both settled a group.
    assert len(set(terms)) >= 2, _holder_signals(trace)
    # The pool is the first group settled, and the leftover was in it.
    assert int(leftover.read_text(encoding="utf-8")) == terms[0], (terms, out)
    assert termed.read_text(encoding="utf-8") == "termed", out
    assert "could not confirm its process group empty" not in out, out
    assert "exited without publishing its settlement" not in out, out
    assert _leftovers(tmp_path) == [], _leftovers(tmp_path)


def _wait_path(path, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def _aggregator_processes(est):
    """Processes still running the estate's aggregator (its reporter subshell
    carries the same command line). Read with ps; nothing is signalled."""
    needle = str(est / "bin" / "cctally-test-all")
    listing = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "command="],
                             capture_output=True, text=True, timeout=30).stdout
    return [line for line in listing.splitlines() if needle in line]


def test_a_run_killed_during_its_pool_leaves_no_member_of_the_pool_group(tmp_path):
    """#1087: SIGKILL gives the aggregator no teardown at all. The pool's
    holder sees its launcher gone and settles the group by itself, within its
    grace and kill wait, and publishes a complete settlement."""
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_SLOW_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    ready = tmp_path / "ready"
    proc = subprocess.Popen([str(est / "bin" / "cctally-test-all")],
                            env=_env(est, tmp_path, {"S1_READY": str(ready)}),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pool = _wait_for(ready, proc)
        suites = _suites(tmp_path)
        assert len(suites) == 1, _leftovers(tmp_path)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    status_path = suites[0] / "hold.pool" / "status"
    assert _wait_path(status_path), "the pool holder published no settlement"
    _wait_group_gone(pool)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["stopped"] is True and status["complete"] is True, status
    assert status["reaped"] is True, status
    deadline = time.monotonic() + 30
    while _aggregator_processes(est) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _aggregator_processes(est) == [], _aggregator_processes(est)
    res = T.remove(str(suites[0]))
    assert res.removed, res.findings
    assert _leftovers(tmp_path) == []


def test_a_pool_holder_killed_from_outside_keeps_the_suite_directory(tmp_path):
    """#1087: a holder that dies before it publishes leaves its group
    unsettled, so the run cannot know the group is empty. It notes that, keeps
    its suite directory and fails as infrastructure, and it signals nothing."""
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_SLOW_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    ready = tmp_path / "ready"
    errors = tmp_path / "stderr.txt"
    with open(errors, "w", encoding="utf-8") as err:
        proc = subprocess.Popen([str(est / "bin" / "cctally-test-all")],
                                env=_env(est, tmp_path, {"S1_READY": str(ready),
                                                         "S1_SLEEP": "4"}),
                                stdout=subprocess.DEVNULL, stderr=err)
        try:
            pool = _wait_for(ready, proc)
            suites = _suites(tmp_path)
            assert len(suites) == 1, _leftovers(tmp_path)
            # The holder is the aggregator's live, unwaited child here: the
            # harness it holds is still asleep.
            holder = int((suites[0] / "hold.pool" / "holder").read_text(encoding="utf-8"))
            os.kill(holder, signal.SIGKILL)
            rc = proc.wait(timeout=100)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    stderr = errors.read_text(encoding="utf-8")
    # The orphaned pool ends by itself once its harness's 4 s sleep is over.
    _wait_group_gone(pool)
    try:
        assert rc == 3, stderr
        assert ("suite directory %s: the pool holder exited without publishing its settlement"
                % suites[0].name) in stderr, stderr
        assert suites[0].is_dir(), "the unsettled suite directory was removed"
        assert not (suites[0] / "hold.pool" / "status").exists()
        out = json.loads((est / "outcome.json").read_text(encoding="utf-8"))
        assert out["failureClass"] == "infrastructure", out
    finally:
        res = T.remove(str(suites[0]))
        assert res.removed, res.findings
    assert _leftovers(tmp_path) == []


# The estate's holder, wrapped so that each settlement it publishes is also
# copied out of the suite directory (which the run removes) for the case to
# read: the work's own status, as the aggregator read it.
_STATUS_COPYING_HOLDER = (
    "import os, shutil, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
    "import _lib_hold_real\n"
    "try:\n"
    "    rc = _lib_hold_real.main()\n"
    "finally:\n"
    "    control = sys.argv[sys.argv.index('--control') + 1]\n"
    "    try:\n"
    "        shutil.copy(os.path.join(control, 'status'),\n"
    "                    os.path.join(os.environ['S1_HOLD_STATUS'], os.path.basename(control)))\n"
    "    except OSError:\n"
    "        pass\n"
    "sys.exit(rc)\n"
)


@pytest.mark.parametrize("failing", ["pool-harness", "pytest-test"])
def test_a_failure_reaches_the_verdict_through_its_holder(tmp_path, failing):
    """#1087 spec test 10: under the holders a failure must still decide the
    run. A pool harness that exits 1 fails the run through its `.exit` file,
    while the pool holder's own status (xargs') stays 0; a failing pytest
    test reaches `leg_rc` through its leg's holder as a product failure, not
    infrastructure. Either way the suite directory is removed. Detects a
    holder change that turned a red estate green, or a product failure into
    an infrastructure one."""
    if failing == "pool-harness":
        est = _estate(tmp_path, harness_output={"alpha": "passed: 3   failed: 2\n"},
                      harness_exit={"alpha": 1})
    else:
        est = _estate(tmp_path)
        (est / "tests" / "test_scratch_smoke.py").write_text(
            "def test_bad():\n    assert False\n", encoding="utf-8")
    real = est / "bin" / "_lib_hold.py"
    real.rename(est / "bin" / "_lib_hold_real.py")
    real.write_text(_STATUS_COPYING_HOLDER, encoding="utf-8")
    statuses = tmp_path / "statuses"
    statuses.mkdir()
    r = _run(est, tmp_path, {"S1_HOLD_STATUS": str(statuses)})
    assert r.returncode == 1, r.stdout + r.stderr
    out = json.loads(r.outcome_path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert out["failureClass"] == "product", out
    codes = {(x["code"], x["subject"]) for x in out["reasons"]}
    pool = json.loads((statuses / "hold.pool").read_text(encoding="utf-8"))
    assert pool["rc"] == 0 and pool["complete"] is True, pool
    leg = json.loads((statuses / "hold.pytest").read_text(encoding="utf-8"))
    assert leg["complete"] is True, leg
    if failing == "pool-harness":
        assert ("harness-failed", "alpha") in codes, out["reasons"]
        assert leg["rc"] == 0, leg
    else:
        assert any(code == "pytest-failed" for code, _ in codes), out["reasons"]
        assert leg["rc"] == 1, leg
    assert _leftovers(tmp_path) == [], _leftovers(tmp_path)


def test_a_pytest_leg_killed_by_sigkill_is_infrastructure(tmp_path):
    """#1087: the leg's status now comes from its holder. A leg killed by a
    signal is not a product failure: the holder reports 128 + the signal, which
    classifies as pytest-internal, and the run still removes its directory."""
    est = _estate(tmp_path)
    # The leg's own pytest process is killed: under xdist that is the
    # worker's parent (the controller), otherwise the test's own process.
    (est / "tests" / "test_scratch_smoke.py").write_text(
        "import os, signal\n"
        "def test_dies():\n"
        "    target = os.getppid() if os.environ.get('PYTEST_XDIST_WORKER') else os.getpid()\n"
        "    os.kill(target, signal.SIGKILL)\n", encoding="utf-8")
    r = _run(est, tmp_path)
    assert r.returncode == 3, r.stdout + r.stderr
    out = json.loads(r.outcome_path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert out["failureClass"] == "infrastructure", out
    codes = {x["code"] for x in out["reasons"]}
    assert "pytest-internal" in codes and "pytest-failed" not in codes, out["reasons"]
    assert _leftovers(tmp_path) == [], _leftovers(tmp_path)


def test_a_stalled_reporter_is_left_to_exit_and_never_signalled(tmp_path):
    """#1087: the reporter's join used to TERM a reporter still running after
    its 10 s bound, by a job number bash may already have reaped. It now notes
    the reporter and leaves it; the run still finishes and removes its suite
    directory, and the reporter, which records any signal it receives while
    stalled, records none."""
    # alpha sleeps 2 s, so the reporter acknowledges the pool phase before it
    # sees a completion and stalls; the stall outlasts the whole run.
    est = _estate(tmp_path, harness_sleep={"alpha": 2})
    r = _run(est, tmp_path, {"CCTALLY_TEST_ALL_REPORTER_STALL_S": "40"}, timeout=150)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("the progress reporter had not exited 10 s after its stop marker; "
            "left to exit on its own") in r.stderr, r.stderr
    assert "progress reporter received" not in r.stderr, r.stderr
    assert _leftovers(tmp_path) == [], _leftovers(tmp_path)
