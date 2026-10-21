"""S1, S2(b) and the phase rows end to end through bin/cctally-test-all (#963, spec 5.4-5.5, 5.9, 9.1).

B1 lives in the mirror-private tests/test_suite_generation_budget.py: the
runtime budget is enforced only on a private estate.

Every case drives the REAL aggregator in a scratch estate
(tests/test_authoritative_test_contract.py `_estate`) with its TMPDIR inside
this test's allocation, so the child's suite generation, retained by design in
the failure cases, lives and dies with the test (spec 5.4, scope of U5).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _lib_test_temp as T
from test_authoritative_test_contract import _carry, _estate
from test_test_all_observability import _SIGINT_RESET_SHIM

REPO = Path(__file__).resolve().parents[1]
GEN_RE = re.compile(r"^ctg-[0-9a-f]{8}$")
DAY = 86400


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


def _gens(tmp_path):
    return sorted(p for p in _child_tmp(tmp_path).iterdir() if GEN_RE.match(p.name))


def _leftovers(tmp_path):
    return sorted(p.name for p in _child_tmp(tmp_path).iterdir())


def _marker(gen):
    return json.loads((gen / "owner.json").read_text(encoding="utf-8"))


def _rewind(gen, key, seconds):
    """Move one recorded instant back: how the test clock crosses U5's 24 hours."""
    doc = _marker(gen)
    doc[key] -= seconds
    (gen / "owner.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc[key]


def _iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _drop_child_generations(tmp_path):
    for gen in _gens(tmp_path):
        res = T.remove(str(gen))
        assert res.removed, res.findings


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


def test_the_main_leg_allocates_inside_the_suite_generation(tmp_path):
    # alpha sleeps one second, so its recorded harness time is at least 1 s.
    est = _estate(tmp_path, harness_sleep={"alpha": 1})
    (est / "tests" / "test_placement_probe.py").write_text(_PLACEMENT_PROBE, encoding="utf-8")
    probe = tmp_path / "probe.txt"
    # No evidence root: the temporary LOGDIR lives in the generation the success
    # path removes before the budget freeze.
    r = _run(est, tmp_path, {"S1_PROBE": str(probe)})
    assert r.returncode == 0, r.stdout + r.stderr
    recorded = probe.read_text(encoding="utf-8")
    root = re.escape(os.path.realpath(str(_child_tmp(tmp_path))))
    assert re.match(root + r"/ctg-[0-9a-f]{8}/m/", recorded), (
        "the main leg's tmp_path is not inside a suite generation's main basetemp: "
        + recorded)
    # R30: the console's slowest-harness lines are read before the removal.
    assert "Slowest harnesses:\n" in r.stdout, r.stdout
    slowest = r.stdout.split("Slowest harnesses:\n", 1)[1]
    assert re.search(r"^ +[1-9][0-9]*s  alpha$", slowest, re.M), (
        "a run without an evidence root printed no non-zero harness timing:\n" + slowest)
    assert ".progress/phase.tmp" not in r.stderr, (
        "the teardown wrote into the removed generation:\n" + r.stderr)


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
        "the main leg's sentinel did not survive nested session %s" % verdict.split()[-1])
    assert r.returncode == 0, r.stdout + r.stderr


# 963-CD-G3 (esc-db01a377fca6): every leg inherits the descendant token of
# each enclosing run and of its own, across both owner-only scrub boundaries.
_TOKEN_PROBE = (
    "import json, os\n"
    "def test_records_the_generation_tokens():\n"
    "    gen = os.path.dirname(os.environ['PYTEST_DEBUG_TEMPROOT'])\n"
    "    with open(os.path.join(gen, 'owner.json')) as fh:\n"
    "        own = json.load(fh)['descendantToken']\n"
    "    with open(os.environ['S1_PROBE'], 'w') as fh:\n"
    "        json.dump({'own': own, 'seen': os.environ.get('CCTALLY_SUITE_GENERATIONS')}, fh)\n"
)
_TOKEN_HARNESS = (
    "#!/usr/bin/env bash\n"
    "printf '%s' \"${CCTALLY_SUITE_GENERATIONS-unset}\" > \"$S1_HARNESS_PROBE\"\n"
    "printf 'passed: 5   failed: 0\\n'\n"
)


def test_every_leg_inherits_its_ancestors_descendant_tokens_and_its_own(tmp_path):
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_TOKEN_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    (est / "tests" / "test_token_probe.py").write_text(_TOKEN_PROBE, encoding="utf-8")
    ancestor = "cctg." + "a" * 32
    leg_probe, harness_probe = tmp_path / "leg.json", tmp_path / "harness.txt"
    r = _run(est, tmp_path, {"S1_PROBE": str(leg_probe), "S1_HARNESS_PROBE": str(harness_probe),
                             "CCTALLY_SUITE_GENERATIONS": ancestor})
    assert r.returncode == 0, r.stdout + r.stderr
    leg = json.loads(leg_probe.read_text(encoding="utf-8"))
    assert re.match(r"^cctg\.[0-9a-f]{32}$", leg["own"]), leg
    assert (leg["seen"] or "").split() == [ancestor, leg["own"]], leg
    assert harness_probe.read_text(encoding="utf-8").split() == [ancestor, leg["own"]]


_SEALS = (
    "import os\n"
    "def test_seals_its_tmp_path(tmp_path):\n"
    "    sealed = tmp_path / 'sealed'\n"
    "    sealed.mkdir()\n"
    "    (sealed / 'f').write_text('x')\n"
    "    sealed.chmod(0o500)\n"
    "    assert os.environ.get('S1_PASS') == '1'\n"
)


def test_a_failed_generation_is_removed_once_reaped(tmp_path):
    """Test regime reset (session 1): a failed run whose recorded process groups
    are all established gone removes its generation at exit, sealed entries
    included, exactly as a successful run does; nothing waits 24 hours."""
    est = _estate(tmp_path)
    (est / "tests" / "test_seals.py").write_text(_SEALS, encoding="utf-8")
    r = _run(est, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert _leftovers(tmp_path) == [], (
        "a failed run whose groups were reaped kept %r" % _leftovers(tmp_path))


# The reaper's CLI contract when a recorded group is not established gone: one
# line per group on stderr and exit 1 (bin/_lib_test_generation.py `reap`). A
# group that outlives SIGKILL cannot be produced portably, so the scratch
# estate's copy of the kernel answers that way when S1_REAP_SURVIVOR is set;
# reap_groups' own survivor detection is covered in tests/test_suite_generation.py.
_REAP_SURVIVOR_SHIM = (
    "\n\nif __name__ == \"__main__\" and sys.argv[1:2] == [\"reap\"] \\\n"
    "        and os.environ.get(\"S1_REAP_SURVIVOR\") == \"1\":\n"
    "    sys.stderr.write(\"%ssuite generation: process group 1 was not established \"\n"
    "                     \"as terminated: it survives SIGKILL\\n\" % PREFIX)\n"
    "    sys.exit(1)\n"
)


def _with_reap_survivor(est):
    kernel = est / "bin" / "_lib_test_generation.py"
    text = kernel.read_text(encoding="utf-8")
    entry = '\n\nif __name__ == "__main__":\n    sys.exit(main())\n'
    assert text.endswith(entry), "the kernel's entry point moved"
    kernel.write_text(text[:-len(entry)] + _REAP_SURVIVOR_SHIM + entry, encoding="utf-8")
    return est


def test_a_generation_with_a_surviving_descendant_is_kept(tmp_path):
    """A generation is never removed while a recorded descendant may still run:
    the run releases it and marks it for the unchanged 24-hour retention, which
    a later run's sweep reports and then reclaims."""
    est = _with_reap_survivor(_estate(tmp_path))
    (est / "tests" / "test_seals.py").write_text(_SEALS, encoding="utf-8")
    first = _run(est, tmp_path, {"S1_REAP_SURVIVOR": "1"})
    assert first.returncode == 1, first.stdout + first.stderr
    assert "was not established as terminated: it survives SIGKILL" in first.stderr, (
        first.stderr)
    gens = _gens(tmp_path)
    assert len(gens) == 1, (
        "the generation was removed while a descendant survived: %r" % _leftovers(tmp_path))
    gen = gens[0]
    doc = _marker(gen)
    assert doc["state"] == "failed" and doc["terminalAt"] is not None, doc
    assert T.inspect(str(gen)).findings == [], "the retained generation was not released"
    second = _run(est, tmp_path, {"S1_PASS": "1"})
    assert second.returncode == 0, second.stdout + second.stderr
    tok = gen.name[len("ctg-"):]
    assert ("cctally-test-all: kept suite generation %s: failed at %s; retained until %s"
            % (tok, _iso(doc["terminalAt"]), _iso(doc["terminalAt"] + DAY))) in second.stderr, (
        second.stderr)
    assert _gens(tmp_path) == [gen]
    ended = _rewind(gen, "terminalAt", DAY + 3600)
    third = _run(est, tmp_path, {"S1_PASS": "1"})
    assert third.returncode == 0, third.stdout + third.stderr
    assert ("cctally-test-all: reclaimed suite generation %s (ended %s)"
            % (tok, _iso(ended))) in third.stderr, third.stderr
    assert _leftovers(tmp_path) == []



# Test regime reset fix round (B2): a test that leaves a process in a session
# of its own, outside every recorded group, which inherits the run's
# descendant token and outlives the run.
_DETACHES = (
    "import os, subprocess, sys\n"
    "def test_leaves_a_detached_descendant():\n"
    "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],\n"
    "                             start_new_session=True, stdin=subprocess.DEVNULL,\n"
    "                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "    with open(os.environ['S1_DETACHED'], 'w') as fh:\n"
    "        fh.write(str(child.pid))\n"
    "    assert os.environ.get('S1_PASS') == '1'\n"
)


def test_a_failed_generation_whose_detached_token_holder_survives_is_kept(tmp_path):
    """Review Focus 4: a failed run never removes its generation while a
    descendant still runs, including one outside every recorded process group
    that only the descendant-token scan can see. It is kept, released and
    marked for the 24-hour retention instead (the no-holder case is
    test_a_failed_generation_is_removed_once_reaped)."""
    est = _estate(tmp_path)
    (est / "tests" / "test_detaches.py").write_text(_DETACHES, encoding="utf-8")
    pidfile = tmp_path / "detached.pid"
    child = None
    try:
        r = _run(est, tmp_path, {"S1_DETACHED": str(pidfile)})
        assert r.returncode == 1, r.stdout + r.stderr
        child = int(pidfile.read_text(encoding="utf-8"))
        assert os.getpgid(child) == child, "the holder is not outside the recorded groups"
        gens = _gens(tmp_path)
        assert len(gens) == 1, (
            "the generation was removed while a descendant carrying its token survived: %r\n%s"
            % (_leftovers(tmp_path), r.stderr))
        doc = _marker(gens[0])
        assert doc["state"] == "failed" and doc["terminalAt"] is not None, doc
        assert "outside the recorded process groups still carries its descendant token" in r.stderr, (
            r.stderr)
        assert T.inspect(str(gens[0])).findings == [], "the retained generation was not released"
    finally:
        if child is not None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _drop_child_generations(tmp_path)


# A harness that plants, beside the suite generation's own entries, a chain of
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


def _unplant(gen):
    """Remove the planted chain descriptor-relatively, deepest first, then the
    rest of the generation through the kernel."""
    if not (gen / "planted").exists():
        return
    fds, names = [os.open(str(gen / "planted"), os.O_RDONLY)], []
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
    res = T.remove(str(gen))
    assert res.removed, res.findings


def test_a_refused_success_path_removal_fails_the_run_as_infrastructure(tmp_path):
    """F-1 (spec 5.4, J9): a run whose tests all passed but whose own
    generation the kernel refuses to remove keeps that generation, marked
    failed for U5's retention, and fails as infrastructure with
    temp-generation-cleanup-failed."""
    est = _estate(tmp_path)
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_PLANTING_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    r = _run(est, tmp_path, {"S1_PLANT": "1"})
    gens = _gens(tmp_path)
    try:
        assert len(gens) == 1, (
            "the refused generation was not kept: %r\n%s" % (_leftovers(tmp_path),
                                                              r.stdout + r.stderr))
        gen = gens[0]
        tok = gen.name[len("ctg-"):]
        assert (gen / "planted").is_dir(), "the harness planted nothing:\n" + r.stdout + r.stderr
        assert r.returncode == 3, r.stdout + r.stderr
        out = json.loads(r.outcome_path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
        assert out["failureClass"] == "infrastructure", out
        assert any(x["code"] == "temp-generation-cleanup-failed" and x["subject"] == tok
                   for x in out["reasons"]), out["reasons"]
        # 963-CD-G1: through the REAL carrier bin/cctally-test-remote applies,
        # whose phase set is closed. A phase it does not list is rewritten to
        # outcome-record-malformed, losing the code, the token and the class.
        carried, carried_rc = _carry(tmp_path, out, r.returncode)
        assert carried_rc == 3 and carried["failureClass"] == "infrastructure", carried
        assert any(x["code"] == "temp-generation-cleanup-failed" and x["subject"] == tok
                   and x["phase"] == "pytest" for x in carried["reasons"]), carried["reasons"]
        doc = _marker(gen)
        assert doc["state"] == "failed" and doc["terminalAt"] is not None, doc
        assert ("cctally-test-all: suite generation %s: inspection error" % tok) in r.stderr, (
            r.stderr)
    finally:
        for gen in gens:
            _unplant(gen)
    assert _leftovers(tmp_path) == []


# A harness that, when S1_READY is set, publishes its process group (the pool's)
# and holds the run in the pool for 30 s: long enough to outlive the signal in
# the interrupted cases and the second run (timeout 25 s) in the killed case.
_SLOW_HARNESS = (
    "#!/usr/bin/env bash\n"
    "if [ -n \"${S1_READY:-}\" ]; then\n"
    "  ps -o pgid= -p $$ | tr -d ' ' > \"$S1_READY.tmp\" && mv \"$S1_READY.tmp\" \"$S1_READY\"\n"
    "  sleep 30\n"
    "fi\n"
    "printf 'passed: 5   failed: 0\\n'\n"
)


def _make_slow(est):
    harness = est / "bin" / "cctally-alpha-test"
    harness.write_text(_SLOW_HARNESS, encoding="utf-8")
    harness.chmod(0o755)
    return est


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
        time.sleep(0.1)
    pytest.fail("process group %d outlived SIGKILL" % pgid, pytrace=False)


@pytest.mark.parametrize("signame,expected", [("TERM", 143), ("INT", 130)])
def test_an_interrupted_generation_is_removed_once_reaped(tmp_path, signame, expected):
    """An interrupted run stops and reaps its recorded groups (here the pool,
    asleep in its harness), and once they are established gone it removes its
    generation rather than keeping it for 24 hours."""
    est = _make_slow(_estate(tmp_path))
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGINT_RESET_SHIM, str(est / "bin" / "cctally-test-all")],
        env=_env(est, tmp_path, {"S1_READY": str(ready)}),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pool = None
    try:
        pool = _wait_for(ready, proc)
        gens = _gens(tmp_path)
        assert len(gens) == 1, _leftovers(tmp_path)
        deadline = time.monotonic() + 15
        while pool not in _marker(gens[0])["groups"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pool in _marker(gens[0])["groups"], _marker(gens[0])
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


def test_a_killed_run_is_reclaimed_only_after_its_death_and_the_retention(tmp_path):
    est = _make_slow(_estate(tmp_path))
    ready = tmp_path / "ready"
    proc = subprocess.Popen([str(est / "bin" / "cctally-test-all")],
                            env=_env(est, tmp_path, {"S1_READY": str(ready)}),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    pool = None
    try:
        pool = _wait_for(ready, proc)
        gens = _gens(tmp_path)
        assert len(gens) == 1, (
            "no suite generation exists while the run is live: %r" % _leftovers(tmp_path))
        gen = gens[0]
        # The harness can publish its group before the parent's add-group has
        # rewritten the marker (both start at the launch), so wait for it.
        deadline = time.monotonic() + 15
        while pool not in _marker(gen)["groups"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pool in _marker(gen)["groups"], _marker(gen)
        os.kill(proc.pid, signal.SIGKILL)          # the owner only: its pool lives on
        proc.wait(timeout=30)
        tok = gen.name[len("ctg-"):]
        # The pool sleeps 30 s, so it outlives this run, whose exit sweep is
        # what must see it; the timeout keeps the run inside that window.
        second = _run(est, tmp_path, timeout=25)
        assert second.returncode == 0, second.stdout + second.stderr
        assert ("cctally-test-all: kept suite generation %s: a recorded process group survives"
                % tok) in second.stderr, second.stderr
        assert _marker(gen)["ownershipEndedAt"] is None
    finally:
        _kill_group(proc.pid)
        if pool:
            _kill_group(pool)
            _wait_group_gone(pool)
    third = _run(est, tmp_path)
    assert third.returncode == 0, third.stdout + third.stderr
    ended = _marker(gen)["ownershipEndedAt"]
    assert ended is not None, "the death was not recorded when it was first established"
    assert ("cctally-test-all: kept suite generation %s: ownership ended at %s; retained until %s"
            % (tok, _iso(ended), _iso(ended + DAY))) in third.stderr, third.stderr
    ended = _rewind(gen, "ownershipEndedAt", DAY + 3600)
    fourth = _run(est, tmp_path)
    assert fourth.returncode == 0, fourth.stdout + fourth.stderr
    assert ("cctally-test-all: reclaimed suite generation %s (ended %s)"
            % (tok, _iso(ended))) in fourth.stderr, fourth.stderr
    assert _leftovers(tmp_path) == []


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



_S2_INNER = '''\
import json, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _temp_hygiene as hyg


def test_launches_an_expected_failure_child(tmp_path):
    est = Path(__file__).resolve().parents[1]
    env = hyg.child_aggregator_env(dict(os.environ), tmp_path)
    env.pop("S2_PROBE", None)
    env.update({"S2_CHILD_FAIL": "1",
                "S2_CHILD_ROOT": str(tmp_path / "child-root.txt"),
                "CCTALLY_TEST_ALL_OUTCOME_FILE": str(tmp_path / "child-outcome.json")})
    proc = subprocess.run([str(est / "bin" / "cctally-test-all"), "--harness", "alpha"],
                          env=env, capture_output=True, text=True, timeout=90)
    child_tmp = Path(env["TMPDIR"])

    def gens():
        if not child_tmp.is_dir():
            return []
        return sorted(p for p in child_tmp.iterdir() if p.name.startswith("ctg-"))

    outcome = json.loads((tmp_path / "child-outcome.json").read_text())
    root = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    probe = {
        "rc": proc.returncode,
        "codes": sorted({r["code"] for r in outcome["reasons"]}),
        "gens": [os.path.realpath(str(p)) for p in gens()],
        "states": [json.loads((p / "owner.json").read_text())["state"]
                   for p in gens() if (p / "owner.json").exists()],
        "outer": os.path.realpath(os.path.dirname(root)) if root else None,
        "child": os.path.realpath(os.path.dirname(
            (tmp_path / "child-root.txt").read_text())),
    }
    hyg.reclaim_child_generations(tmp_path)
    probe["after"] = [p.name for p in gens()]
    Path(os.environ["S2_PROBE"]).write_text(json.dumps(probe))
'''


def test_an_expected_failure_child_aggregator_stays_inside_the_outer_generation(tmp_path):
    """S2(b). A successful outer run whose test launches a child aggregator that
    is meant to fail: the child's generation lies inside the outer generation,
    the failed child removes it at exit once its groups are reaped (test regime
    reset, session 1), and nothing created during the run remains under the
    inherited TMPDIR."""
    est = _estate(tmp_path)
    alpha = est / "bin" / "cctally-alpha-test"
    # The failing child reports its failure in its summary as well as its exit
    # status, so the contract files it as harness-failed rather than as an
    # exit/summary mismatch.
    alpha.write_text("#!/usr/bin/env bash\n"
                     "if [ \"${S2_CHILD_FAIL:-}\" = 1 ]; then\n"
                     "  printf '%s' \"$PYTEST_DEBUG_TEMPROOT\" > \"$S2_CHILD_ROOT\"\n"
                     "  printf 'passed: 4   failed: 1\\n'; exit 1\n"
                     "fi\n"
                     "printf 'passed: 5   failed: 0\\n'\nexit 0\n", encoding="utf-8")
    alpha.chmod(0o755)
    shutil.copy2(REPO / "tests" / "_temp_hygiene.py", est / "tests" / "_temp_hygiene.py")
    (est / "tests" / "test_s2_child.py").write_text(_S2_INNER, encoding="utf-8")
    probe_path = tmp_path / "s2-probe.json"
    r = _run(est, tmp_path, {"S2_PROBE": str(probe_path)})
    assert _leftovers(tmp_path) == [], (
        "after the outer success, %r remain under the inherited TMPDIR" % _leftovers(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    assert probe["rc"] != 0 and "harness-failed" in probe["codes"], probe
    assert probe["outer"], probe
    assert probe["child"].startswith(probe["outer"] + os.sep), (
        "the child's generation is not inside the outer generation", probe)
    assert probe["gens"] == [] and probe["states"] == [], (
        "the failed child kept its reaped generation", probe)
    assert probe["after"] == [], probe
