"""The shell holder, bin/_lib_hold.py and bin/_lib-hold.sh (#1087 spec test 9).

Bash reaps a background job asynchronously, so a script that probes a job's
number and then signals it can signal whichever process was given that number
in between. The holder is the work's parent and only reaper and signals only
the work it holds unreaped. These cases detect a holder that signals outside
the group it holds, leaks its work, delays a clean stop, reports a group empty
while a member lives, hangs on a work that never exits, or loses the work's
status.

The settlement's emptiness check is decided in-process, over injected probe,
census and listing functions that disagree (plan §R G5): those cases are the
decisive ones, and they cover the Linux branch on every platform, because the
Linux real-process lane runs only at a release tag. The real-process cases run
the holder against the kernel on Darwin and Linux; they observe its signal
calls through the private trace file `CCTALLY_HOLD_TRACE` names, and every one
asserts that a canary in another group of this session and a canary in another
session receive nothing.
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

import pytest

import _lib_hold as H

REPO = pathlib.Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
HOLD = BIN / "_lib_hold.py"
LIB = BIN / "_lib-hold.sh"
PY = sys.executable
DARWIN = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
REAL = pytest.mark.skipif(not (DARWIN or LINUX), reason="the holder runs on Darwin and Linux only")
W = 4242


# ---- in-process doubles ----------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _errno(code):
    return OSError(code, os.strerror(code))


class Kernel:
    """Records every signal call with the clock; ``probe`` answers sig 0 and
    ``deliver`` answers the others (None = success, else an errno)."""

    def __init__(self, clock, probe=None, deliver=None, on_kill_round=None):
        self.clock = clock
        self.calls = []
        self.probe = probe
        self.deliver = deliver
        self.on_kill_round = on_kill_round
        self.rounds = 0

    def killpg(self, pg, sig):
        self.calls.append(("killpg", pg, sig, self.clock()))
        if sig == signal.SIGKILL:
            self.rounds += 1
            if self.on_kill_round:
                self.on_kill_round(self.rounds)
        code = self.probe(self) if sig == 0 and self.probe else (
            self.deliver if sig != 0 else None)
        if code:
            raise _errno(code)

    def kill(self, pid, sig):
        self.calls.append(("kill", pid, sig, self.clock()))

    def delivering(self):
        return [c[:3] for c in self.calls if c[2] != 0]


def _never(*_a, **_k):
    raise AssertionError("not consulted on this platform")


def _settle(kernel, clock, *, system, census=_never, listing=_never, exited=lambda: True,
            grace=1.0, kill_wait=2.0):
    return H.settle_group(W, exited=exited, killpg=kernel.killpg, kill=kernel.kill,
                          census=census, listing=listing, clock=clock, sleep=clock.sleep,
                          grace=grace, kill_wait=kill_wait, system=system)


# ---- §R G4/G5: the Darwin probe decides, a listing never does --------------

def test_darwin_an_empty_listing_is_never_complete_while_the_group_probe_succeeds():
    """A listing that is empty while killpg(W, 0) succeeds (a survivor the
    listing missed) ends at the kill wait with complete false, and the holder
    kept sending SIGKILL rounds; every call names W and none goes to W by pid
    once it has exited."""
    clock = Clock()
    kernel = Kernel(clock)  # every call succeeds: a signalable member lives
    out = _settle(kernel, clock, system="Darwin", listing=lambda: [])
    assert out["complete"] is False
    assert out["termed"] and out["killed"]
    assert clock.t >= 1.0 + 2.0, "the grace and the kill wait both ran out"
    kills = [c for c in kernel.calls if c[2] == signal.SIGKILL]
    assert len(kills) > 2, kills
    assert {c[1] for c in kernel.calls} == {W}
    assert not [c for c in kernel.calls if c[0] == "kill"], "W had exited"


@pytest.mark.parametrize("probe, listing, complete, survivors", [
    (errno.ESRCH, None, True, []),       # the group is gone: the moved leader
    (errno.EPERM, [], True, []),         # zombies only
    (errno.EPERM, [77], False, [77]),    # a live member it may not signal
    (errno.EPERM, None, False, []),      # the listing failed: not confirmed
    (errno.EINVAL, [], False, []),       # anything else: not confirmed
])
def test_darwin_probe_results(probe, listing, complete, survivors):
    clock = Clock()
    kernel = Kernel(clock, probe=lambda _k: probe, deliver=errno.EPERM)
    out = _settle(kernel, clock, system="Darwin", listing=lambda: listing)
    assert out["complete"] is complete
    assert out["survivors"] == survivors
    assert out["killed"], "a SIGKILL round always precedes the decision"
    if complete:
        assert clock.t < 0.5, "an empty group ends the grace at once"


# ---- §R G3/G5: the Linux census -------------------------------------------

def test_linux_an_empty_census_before_any_kill_round_only_ends_the_grace():
    """A census that finds nothing before the first SIGKILL round shortens the
    grace and decides nothing: a round is sent and the census is taken again
    before complete."""
    clock = Clock()
    events = []
    kernel = Kernel(clock)

    def census():
        events.append(("census", kernel.rounds))
        return set()

    out = _settle(kernel, clock, system="Linux", census=census)
    assert out["complete"] is True
    assert clock.t < 0.5, "the grace ended early"
    assert events[0] == ("census", 0)
    assert events[-1][1] >= 1, "complete was decided by a census after a round"
    assert kernel.delivering() == [("killpg", W, signal.SIGTERM), ("killpg", W, signal.SIGKILL)]


def _proc_tree(root, entries):
    """entries: pid -> (group, own state, {tid: state}) or a special marker."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "self").mkdir(exist_ok=True)
    for pid, spec in entries.items():
        base = root / str(pid)
        base.mkdir()
        if spec == "no-stat":
            continue
        if spec == "unreadable-stat":
            (base / "stat").mkdir()  # reading it fails with EISDIR
            continue
        group, state, tasks, comm = spec
        (base / "stat").write_text("%d (%s) %s 1 %d %d 0\n" % (pid, comm, state, group, group))
        if tasks is None:
            continue
        (base / "task").mkdir()
        for tid, tstate in tasks.items():
            (base / "task" / str(tid)).mkdir()
            (base / "task" / str(tid) / "stat").write_text(
                "%d (%s) %s 1 %d %d 0\n" % (tid, comm, tstate, group, group))


def test_linux_census_counts_a_zombie_leader_with_a_live_thread_as_present(tmp_path):
    proc = tmp_path / "proc"
    _proc_tree(proc, {
        W: (W, "Z", {W: "Z"}, "work"),                      # W itself: excluded
        300: (W, "S", {300: "S"}, "odd ) name ("),          # live member
        301: (W, "Z", {301: "Z"}, "dead"),                  # dead member
        302: (W, "Z", {302: "Z", 303: "S"}, "zleader"),     # zombie leader, live thread
        304: (9999, "S", {304: "S"}, "stranger"),           # another group
        305: "unreadable-stat",                             # unknown: present
        306: "no-stat",                                     # vanished: gone
        307: (W, "S", None, "notasks"),                     # task dir vanished: gone
        308: (W, "X", {308: "X", 309: "Z"}, "exiting"),     # dead
    })
    assert H.linux_census(W, exclude=W, proc=str(proc)) == {300, 302, 305}
    assert H.linux_census(W, exclude=W, proc=str(tmp_path / "absent")) is None


def test_linux_a_post_kill_census_with_a_live_task_is_not_complete(tmp_path):
    """After a round, a member whose leader thread is a zombie but whose second
    thread lives is present; complete follows only when the thread is gone."""
    proc = tmp_path / "proc"
    _proc_tree(proc, {302: (W, "Z", {302: "Z", 303: "S"}, "zleader")})
    clock = Clock()

    def round_hook(n):
        if n == 3:
            (proc / "302" / "task" / "303" / "stat").write_text("303 (zleader) Z 1 %d %d 0\n" % (W, W))

    kernel = Kernel(clock, on_kill_round=round_hook)
    out = _settle(kernel, clock, system="Linux",
                  census=lambda: H.linux_census(W, exclude=W, proc=str(proc)))
    assert out["complete"] is True
    assert kernel.rounds == 3
    assert clock.t >= 1.0, "a live thread kept the grace running"


def test_linux_an_unreadable_proc_is_never_complete():
    clock = Clock()
    kernel = Kernel(clock)
    out = _settle(kernel, clock, system="Linux", census=lambda: None, listing=lambda: None)
    assert out["complete"] is False
    assert out["survivors"] == []


def test_a_failed_listing_names_no_survivor_and_the_census_alone_decides():
    clock = Clock()
    kernel = Kernel(clock)
    out = _settle(kernel, clock, system="Linux", census=lambda: set(), listing=lambda: None)
    assert out["complete"] is True and out["survivors"] == []


# ---- the non-reaping exit waits -------------------------------------------

class _Waitid:
    def __init__(self, after):
        self.calls = []
        self.after = after

    def __call__(self, idtype, pid, options):
        self.calls.append((idtype, pid, options))
        if len(self.calls) < self.after:
            return None
        return type("R", (), {"si_pid": pid})()


def test_the_linux_waiter_never_reaps(monkeypatch):
    for name, value in (("P_PID", 1), ("WEXITED", 4), ("WNOWAIT", 0x1000000), ("WNOHANG", 1)):
        monkeypatch.setattr(os, name, getattr(os, name, value), raising=False)
    fake = _Waitid(after=2)
    waiter = H.exit_wait(W, system="Linux", waitid=fake, sleep=lambda _s: None)
    assert isinstance(waiter, H.WaitidWaiter)
    assert waiter.exited() is False
    assert waiter.exited() is True and waiter.exited() is True
    for idtype, pid, options in fake.calls:
        assert (idtype, pid) == (os.P_PID, W)
        assert options & os.WNOWAIT and options & os.WNOHANG and options & os.WEXITED


def test_a_platform_without_a_non_reaping_wait_has_no_waiter():
    assert H.exit_wait(W, system="Plan9") is None
    assert H.exit_wait_supported("Plan9") is False


@REAL
def test_the_platform_waiter_reports_an_exit_without_reaping():
    child = subprocess.Popen([PY, "-c", "import time; time.sleep(0.3)"])
    try:
        waiter = H.exit_wait(child.pid)
        assert waiter.exited() is False
        deadline = time.monotonic() + 10
        while not waiter.exited(timeout=0.1):
            assert time.monotonic() < deadline
        pid, status = os.waitpid(child.pid, 0)  # still ours to reap
        assert pid == child.pid and os.WIFEXITED(status)
        child.returncode = os.WEXITSTATUS(status)
        waiter.close()
    finally:
        if child.returncode is None:
            child.kill()
            child.wait()


# ---- main() with injected platform functions -------------------------------

class _FakeWork:
    def __init__(self, *_a, **_k):
        self.pid = 424242
        self.returncode = None


class _NeverExits:
    def exited(self, timeout=0.0):
        if timeout:
            time.sleep(min(timeout, 0.01))
        return False

    def close(self):
        pass


def _main(monkeypatch, tmp_path, scope, *, stop=True, supported=True, popen=_FakeWork):
    control = tmp_path / "ctl"
    control.mkdir()
    if stop:
        (control / "stop").write_text("")
    calls = []
    monkeypatch.setattr(H.signal, "signal", lambda *_a: None)
    monkeypatch.setattr(H.os, "getppid", lambda: 999)
    monkeypatch.setattr(H, "exit_wait_supported", lambda *_a: supported)
    monkeypatch.setattr(H, "exit_wait", lambda *_a, **_k: _NeverExits())
    monkeypatch.setattr(H.subprocess, "Popen", popen)
    monkeypatch.setattr(H.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(H.os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)))
    monkeypatch.setattr(H, "ps_listing", lambda _g: [])

    def waitpid(pid, _opts):
        raise AssertionError("waitpid(%d) on a work that never exited" % pid)

    monkeypatch.setattr(H.os, "waitpid", waitpid)
    monkeypatch.delenv(H.TRACE_ENV, raising=False)
    rc = H.main(["--control", str(control), "--parent", "999", "--scope", scope,
                 "--grace", "0.05", "--kill-wait", "0.05", "--", "work"])
    status = json.loads((control / "status").read_text())
    return rc, status, calls


@pytest.mark.parametrize("scope", ["child", "group"])
def test_a_work_that_never_exits_is_published_unreaped_without_a_blocking_reap(
        monkeypatch, tmp_path, scope):
    rc, status, calls = _main(monkeypatch, tmp_path, scope)
    assert rc == 137
    assert status["started"] is True and status["pid"] == 424242
    assert status["complete"] is False and status["reaped"] is False
    assert status["status_code"] is None and status["stopped"] is True
    assert status["termed"] and status["killed"]
    assert {pid for _c, pid, _s in calls} == {424242}
    assert ("kill", 424242, signal.SIGKILL) in calls


def test_no_non_reaping_wait_starts_nothing_and_exits_4(monkeypatch, tmp_path):
    def popen(*_a, **_k):
        raise AssertionError("started work without a non-reaping wait")

    rc, status, calls = _main(monkeypatch, tmp_path, "child", supported=False, popen=popen)
    assert rc == 4
    assert status["started"] is False and calls == []


def test_the_status_is_one_key_per_line_with_exactly_the_published_keys():
    text = H.status_text(H.blank_status())
    assert list(json.loads(text)) == list(H.STATUS_KEYS)
    assert len(text.strip().splitlines()) == len(H.STATUS_KEYS) + 2


# ---- real processes --------------------------------------------------------

CANARY = r'''
import os, signal, sys, time
log, stop = sys.argv[1], sys.argv[2]
deadline = time.time() + float(sys.argv[3])
def record(signum, _frame):
    with open(log, "a") as fh:
        fh.write("%d\n" % signum)
for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGCONT, signal.SIGUSR1):
    signal.signal(s, record)
if len(sys.argv) > 4:
    with open(sys.argv[4], "w") as fh:
        fh.write(str(os.getpid()))
open(log, "a").close()
while not os.path.exists(stop) and time.time() < deadline:
    time.sleep(0.05)
'''

SLEEPER = r'''
import os, signal, sys, time
if "ignore-term" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if len(sys.argv) > 1 and sys.argv[1] != "-":
    with open(sys.argv[1], "w") as fh:
        fh.write(str(os.getpid()))
end = time.time() + 30
while time.time() < end:
    time.sleep(0.05)
'''

LEFTOVER = r'''
import os, signal, sys, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    end = time.time() + 30
    while time.time() < end:
        time.sleep(0.05)
    os._exit(0)
with open(sys.argv[1], "w") as fh:
    fh.write(str(child))
os._exit(7)
'''

CHAIN = r'''
import os, signal, sys, time
deadline = float(sys.argv[2])
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while time.time() < deadline:
        if os.fork():
            os._exit(0)
    os._exit(0)
os._exit(0)
'''

MOVER = r'''
import os, sys, time
os.setpgid(0, int(sys.argv[1]))
open(sys.argv[2], "w").close()
end = time.time() + 30
while time.time() < end:
    time.sleep(0.05)
'''

STOPPER = r'''
import os, signal, sys, time
with open(sys.argv[1], "w") as fh:
    fh.write(str(os.getpid()))
os.kill(os.getpid(), signal.SIGSTOP)
if len(sys.argv) > 2:
    open(sys.argv[2], "w").close()
    sys.exit(0)
end = time.time() + 30
while time.time() < end:
    time.sleep(0.05)
'''

PARENT_OF_GRANDCHILD = r'''
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild = subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3], "60", sys.argv[4]])
end = time.time() + 30
while time.time() < end:
    time.sleep(0.05)
'''


def _state(pid):
    """`ps` state of ``pid``: '' when gone."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return out.stdout.strip()


def _wait_gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _state(pid)
        if not state or "Z" in state:
            return True
        time.sleep(0.05)
    return False


def _wait_file(path, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size >= 0:
            return True
        time.sleep(0.02)
    return False


def _read_pid(path, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text().strip()
        except FileNotFoundError:
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(0.02)
    raise AssertionError("no pid in %s" % path)


class Canaries:
    """One canary in another group of this session, one in another session."""

    def __init__(self, tmp_path):
        self.stop = tmp_path / "canary.stop"
        self.logs = [tmp_path / "canary.group.log", tmp_path / "canary.session.log"]
        self.procs = [
            subprocess.Popen([PY, "-c", CANARY, str(self.logs[0]), str(self.stop), "120"],
                             preexec_fn=os.setpgrp),
            subprocess.Popen([PY, "-c", CANARY, str(self.logs[1]), str(self.stop), "120"],
                             start_new_session=True),
        ]
        for log in self.logs:
            assert _wait_file(log)

    @property
    def group(self):
        return self.procs[0].pid

    def check(self):
        for proc, log in zip(self.procs, self.logs):
            assert proc.poll() is None, "a canary died"
            assert log.read_text() == "", "a canary was signalled: %r" % log.read_text()

    def close(self):
        self.stop.write_text("")
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()  # the test's own unreaped child
                proc.wait()


@pytest.fixture
def canaries(tmp_path):
    c = Canaries(tmp_path)
    try:
        yield c
    finally:
        c.close()


class Held:
    def __init__(self, tmp_path, scope, argv, *, grace=5.0, kill_wait=5.0, parent=None,
                 name="hold", env=None, launch=()):
        self.control = tmp_path / name
        self.control.mkdir()
        self.trace_path = tmp_path / (name + ".trace")
        full_env = dict(os.environ)
        full_env[H.TRACE_ENV] = str(self.trace_path)
        full_env.update(env or {})
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            [PY, *launch, str(HOLD), "--control", str(self.control), "--parent",
             str(os.getpid() if parent is None else parent), "--scope", scope,
             "--grace", str(grace), "--kill-wait", str(kill_wait), "--", *argv],
            env=full_env)

    def stop(self):
        (self.control / "stop").write_text("")

    def wait(self, timeout=30):
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise
        self.elapsed = time.monotonic() - self.started
        return rc

    def status(self):
        return json.loads((self.control / "status").read_text())

    def trace(self):
        if not self.trace_path.exists():
            return []
        return [json.loads(line) for line in self.trace_path.read_text().splitlines()]

    def delivering(self):
        return [t for t in self.trace() if t[2] != 0]


def _names_only(held):
    status = held.status()
    assert {t[1] for t in held.trace()} <= {status["pid"]}, held.trace()


@REAL
def test_a_clean_child_exit_signals_nothing_and_publishes_atomically(tmp_path, canaries):
    seen = []
    stop = threading.Event()
    path = tmp_path / "hold" / "status"

    def reader():
        while not stop.is_set():
            try:
                text = path.read_text()
            except FileNotFoundError:
                time.sleep(0.002)
                continue
            seen.append(json.loads(text))  # a partial file would not parse
            time.sleep(0.002)

    held = Held(tmp_path, "child", [PY, "-c", "pass"])
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    rc = held.wait()
    time.sleep(0.05)
    stop.set()
    thread.join(5)
    assert rc == 0 and held.elapsed < 3.0, "no grace delay (grace is 5 s)"
    assert held.trace() == []
    status = held.status()
    assert status["complete"] is True and status["reaped"] is True and status["rc"] == 0
    assert seen and all(s["complete"] is True for s in seen)
    canaries.check()


# Runs the holder in a process that ignores SIGCHLD. Linux keeps an ignored
# SIGCHLD across exec, so a launcher that ignores it hands the ignore to the
# holder; Darwin clears the auto-reap on exec, so the ignore is set here, in
# the holder's own process, which reproduces it on both.
_SIGCHLD_IGNORING_LAUNCH = ("-c", "import runpy, signal, sys\n"
                            "signal.signal(signal.SIGCHLD, signal.SIG_IGN)\n"
                            "sys.argv = sys.argv[1:]\n"
                            "runpy.run_path(sys.argv[0], run_name='__main__')\n")


@REAL
@pytest.mark.parametrize("scope", ["child", "group"])
def test_an_inherited_sigchld_ignore_does_not_auto_reap_the_work(tmp_path, canaries, scope):
    """With SIGCHLD ignored (a launcher's ignore, inherited through exec on
    Linux) the kernel reaps the holder's work on its own: the holder's reap
    then fails, and its failure path signals by pid a number it no longer
    holds. The holder restores SIGCHLD's default before it starts the work,
    so it reaps the work itself and publishes the work's status."""
    held = Held(tmp_path, scope, ["sh", "-c", "exit 7"], grace=0.5, kill_wait=2,
                launch=_SIGCHLD_IGNORING_LAUNCH)
    rc = held.wait()
    status = held.status()
    assert rc == 7, (rc, status)
    assert status["started"] is True and status["reaped"] is True, status
    assert status["rc"] == 7 and status["complete"] is True, status
    assert not [t for t in held.delivering() if t[0] == "kill"], held.trace()
    _names_only(held)
    canaries.check()


@REAL
def test_a_clean_group_exit_sends_one_term_and_one_kill_round_to_the_held_group(tmp_path, canaries):
    held = Held(tmp_path, "group", [PY, "-c", "pass"])
    rc = held.wait()
    status = held.status()
    assert rc == 0 and held.elapsed < 3.0, "no grace delay (grace is 5 s)"
    w = status["pid"]
    assert [t[:3] for t in held.delivering()] == [
        ["killpg", w, signal.SIGTERM], ["killpg", w, signal.SIGKILL]]
    if DARWIN:
        assert [t[3] for t in held.delivering()] == ["EPERM", "EPERM"], "only a zombie was there"
    assert status["complete"] is True and status["survivors"] == []
    _names_only(held)
    canaries.check()


@REAL
def test_a_group_leftover_is_killed_and_the_work_status_kept(tmp_path, canaries):
    pidfile = tmp_path / "member.pid"
    held = Held(tmp_path, "group", [PY, "-c", LEFTOVER, str(pidfile)], grace=0.5, kill_wait=3)
    rc = held.wait()
    status = held.status()
    member = _read_pid(pidfile)
    assert rc == 7 and status["status_code"] == 7
    assert status["termed"] and status["killed"] and status["complete"] is True
    assert _wait_gone(member)
    assert held.elapsed < 0.5 + 3 + 3
    _names_only(held)
    canaries.check()


@REAL
def test_a_forking_member_is_never_reported_gone_while_a_link_lives(tmp_path, canaries):
    """Smoke check (§R G5): the decisive cases are in-process above."""
    marker = "cctally-hold-chain-%d" % os.getpid()
    deadline = time.time() + 20
    held = Held(tmp_path, "group", [PY, "-c", CHAIN, marker, str(deadline)],
                grace=0.3, kill_wait=3)
    held.wait(timeout=60)
    status = held.status()

    def links():
        out = subprocess.run(["ps", "-A", "-o", "stat=", "-o", "command="],
                             capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if marker in line and "Z" not in line.split()[0]]

    if status["complete"]:
        assert links() == [], links()
    else:
        assert status["survivors"], status
        end = time.time() + 30
        while links() and time.time() < end:
            time.sleep(0.2)
    _names_only(held)
    canaries.check()


@REAL
def test_a_group_stop_reaches_a_running_leader(tmp_path, canaries):
    held = Held(tmp_path, "group", [PY, "-c", SLEEPER, "-"], grace=2, kill_wait=3)
    time.sleep(0.5)
    held.stop()
    rc = held.wait()
    status = held.status()
    assert rc == 128 + signal.SIGTERM and status["signal"] == signal.SIGTERM
    assert status["stopped"] and status["complete"] is True
    _names_only(held)
    canaries.check()


@REAL
def test_a_leader_that_moved_to_another_group_is_stopped_by_pid_and_completes(tmp_path, canaries):
    """§R G4: once the moved leader exits, killpg(W, 0) answers ESRCH, which
    is complete; the group it joined (a canary's) receives nothing."""
    ready = tmp_path / "moved"
    held = Held(tmp_path, "group", [PY, "-c", MOVER, str(canaries.group), str(ready)],
                grace=2, kill_wait=3)
    assert _wait_file(ready)
    held.stop()
    rc = held.wait()
    status = held.status()
    w = status["pid"]
    assert rc == 128 + signal.SIGTERM
    assert ["kill", w, signal.SIGTERM, "ok"] in held.trace()
    assert status["complete"] is True
    _names_only(held)
    canaries.check()


@REAL
def test_a_child_scope_stop_signals_only_the_work(tmp_path, canaries):
    gc_log, gc_stop, gc_pid = tmp_path / "gc.log", tmp_path / "gc.stop", tmp_path / "gc.pid"
    held = Held(tmp_path, "child",
                [PY, "-c", PARENT_OF_GRANDCHILD, CANARY, str(gc_log), str(gc_stop), str(gc_pid)],
                grace=0.5, kill_wait=3)
    try:
        grandchild = _read_pid(gc_pid)
        assert _wait_file(gc_log)
        held.stop()
        held.wait()
        status = held.status()
        w = status["pid"]
        assert [t[:3] for t in held.delivering()] == [
            ["kill", w, signal.SIGTERM], ["kill", w, signal.SIGKILL]]
        assert status["complete"] is True and status["signal"] == signal.SIGKILL
        assert _state(grandchild) and gc_log.read_text() == "", "the grandchild was signalled"
    finally:
        gc_stop.write_text("")
    canaries.check()


@REAL
def test_cont_resumes_a_member_that_stopped_itself(tmp_path, canaries):
    pidfile, resumed = tmp_path / "stopped.pid", tmp_path / "resumed"
    held = Held(tmp_path, "group", [PY, "-c", STOPPER, str(pidfile), str(resumed)],
                grace=1, kill_wait=3)
    w = _read_pid(pidfile)
    deadline = time.monotonic() + 10
    while "T" not in _state(w):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    (held.control / "cont").write_text("")
    assert held.wait() == 0
    assert resumed.exists() and not (held.control / "cont").exists()
    assert ["kill", w, signal.SIGCONT, "ok"] in held.trace()
    _names_only(held)
    canaries.check()


@REAL
def test_a_launcher_already_gone_starts_nothing(tmp_path, canaries):
    ran = tmp_path / "ran"
    held = Held(tmp_path, "child", [PY, "-c", "open(%r, 'w')" % str(ran)], parent=1)
    assert held.wait() == 3
    assert held.status()["started"] is False and not ran.exists() and held.trace() == []
    canaries.check()


def _bash(script, *args, **kw):
    return subprocess.Popen(["bash", "-c", script, "_", *map(str, args)], **kw)


@REAL
def test_a_launcher_killed_while_the_work_runs_lets_the_holder_settle(tmp_path, canaries):
    control, pidfile = tmp_path / "ctl", tmp_path / "w.pid"
    launcher = _bash(
        '. "$1"; hold_start "$2" child --grace 0.5 --kill-wait 1 -- "$3" -c "$4" "$5" ignore-term; wait',
        LIB, control, PY, SLEEPER, pidfile)
    w = _read_pid(pidfile)
    launcher.kill()  # the test's own unreaped child
    launcher.wait()
    killed_at = time.monotonic()
    while not (control / "status").exists():
        assert time.monotonic() - killed_at < 0.5 + 1 + 1 + 2, "the holder did not settle"
        time.sleep(0.02)
    status = json.loads((control / "status").read_text())
    assert status["stopped"] and status["killed"] and status["complete"] is True
    assert status["pid"] == w
    canaries.check()


@REAL
def test_hold_stop_after_an_outside_kill_of_the_holder_reports_incomplete(tmp_path, canaries):
    control, stopfile = tmp_path / "ctl", tmp_path / "w.stop"
    shell = _bash(
        '. "$1"; hold_start "$2" child -- "$3" -c "$4" "$5" "$6" 60 >/dev/null; echo "$HOLD_PID"; '
        'read -r _; hold_stop "$HOLD_PID" "$2"; '
        'echo "complete=$HOLD_COMPLETE published=$HOLD_PUBLISHED rc=$HOLD_RC"',
        LIB, control, PY, CANARY, tmp_path / "w.log", stopfile,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        holder = int(shell.stdout.readline())
        assert _wait_file(tmp_path / "w.log")
        os.kill(holder, signal.SIGKILL)  # alive, so this number is still the holder's
        shell.stdin.write("go\n")
        shell.stdin.flush()
        out = shell.stdout.read()
        assert shell.wait(timeout=30) == 0
        assert "complete=0 published=0 rc=137" in out, out
        assert (tmp_path / "w.log").read_text() == "", "nothing signalled the work"
    finally:
        stopfile.write_text("")
        shell.stdin.close()
        shell.stdout.close()
    canaries.check()


@REAL
def test_nested_scopes_leave_nothing_of_the_outer_group(tmp_path, canaries):
    """rederive's shape: a group-scope holder whose work runs a child-scope
    holder whose work stops itself, beside a TERM-ignoring server."""
    inner, stopped, server, ready = (tmp_path / "inner", tmp_path / "stopped.pid",
                                     tmp_path / "server.pid", tmp_path / "ready")
    work = (
        '. "$1"; export CCTALLY_HOLD_TRACE="$2.trace"; '
        'hold_start "$2" child -- "$3" -c "$4" "$5"; '
        '"$3" -c "$6" "$7" ignore-term & echo "$HOLD_PID" > "$8"; wait')
    held = Held(tmp_path, "group",
                ["bash", "-c", work, "_", str(LIB), str(inner), PY, STOPPER, str(stopped),
                 SLEEPER, str(server), str(ready)], grace=0.5, kill_wait=3)
    pids = [_read_pid(stopped), _read_pid(server), _read_pid(ready)]
    deadline = time.monotonic() + 10
    while "T" not in _state(pids[0]):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    held.stop()
    held.wait()
    status = held.status()
    assert status["complete"] is True, status
    assert held.elapsed < 0.5 + 3 + 15
    for pid in pids:
        assert _wait_gone(pid), (pid, _state(pid))
    _names_only(held)
    canaries.check()


THREAD_LEADER = r'''
import ctypes, os, signal, sys, threading, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
beat = sys.argv[1]
def live():
    end = time.time() + 30
    while time.time() < end:
        with open(beat, "w") as fh:
            fh.write(str(time.time()))
        time.sleep(0.05)
threading.Thread(target=live).start()
with open(sys.argv[2], "w") as fh:
    fh.write(str(os.getpid()))
ctypes.CDLL(None).pthread_exit(None)
'''


@pytest.mark.skipif(not LINUX, reason="a zombie thread-group leader is the Linux census's case")
def test_linux_a_zombie_leader_with_a_live_thread_is_never_reported_gone(tmp_path, canaries):
    """§R G3's control: the member's leader thread exits while a second thread
    lives; the census must keep the grace running and must not publish
    complete until that thread is gone."""
    beat, member_pid = tmp_path / "beat", tmp_path / "member.pid"
    spawn = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], "
             "sys.argv[2], sys.argv[3]]); import time; time.sleep(0.5)")
    held = Held(tmp_path, "group", [PY, "-c", spawn, THREAD_LEADER, str(beat), str(member_pid)],
                grace=1.5, kill_wait=3)
    member = _read_pid(member_pid)
    held.wait()
    status = held.status()
    assert status["complete"] is True and status["killed"]
    # W sleeps 0.5 s before exiting; a census blind to the live thread would
    # end the grace at once instead of after its 1.5 s.
    assert held.elapsed >= 1.9, "the live thread kept the grace from ending early"
    last = beat.stat().st_mtime
    time.sleep(0.3)
    assert beat.stat().st_mtime == last, "the live thread outlived complete"
    assert _wait_gone(member)
    canaries.check()


# ---- static: shell scripts never signal a process number (#1087) -----------
#
# Bash reaps a finished background job on its own, so a script's `kill "$pid"`
# can reach whichever process was given the number next. Scripts in bin/
# therefore start stoppable work under the holder and signal no variable pid:
# only `kill -0` (a liveness probe, no delivery), `kill -l` and a signal to the
# script itself (`$$`) remain. And only bin/cctally-test-all starts a
# group-scope holder (its pool and pytest legs): a nested group holder inside a
# harness would move work out of the pool's group, beyond the pool holder's
# settlement.

_KILL_WORD = re.compile(r"(?<![\w.$/-])kill(?=[ \t])")
_KILL_END = re.compile(r"[;|&)`\n]")
# Signals sent by name or pattern (`pkill`, `killall`) or to numbers read from
# stdin (`xargs … kill`): none names a process the script holds, so each is
# flagged whatever its arguments.
_UNHELD_KILL = re.compile(r"(?<![\w.$/-])(?:pkill|killall)(?![\w.-])"
                          r"|(?<![\w.$/-])xargs(?:[ \t][^;|&)`\n]*)?[ \t](?:\S*/)?kill(?![\w.-])")
_GROUP_HOLD = re.compile(r"(?<![\w-])hold_start\s+(?:\"[^\"]*\"|\S+)\s+group\b")


def _shell_scripts(root=BIN):
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix == ".py":
            continue
        if path.suffix == ".sh":
            yield path
            continue
        with path.open("rb") as fh:
            first = fh.readline()
        if first.startswith(b"#!") and (b"bash" in first or b"/sh" in first):
            yield path


def _code_lines(text):
    for number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield number, line


def signalling_kills(text):
    """The `kill` calls in shell ``text`` that deliver a signal to a pid held
    in a variable, as (line number, line)."""
    found = []
    for number, line in _code_lines(text):
        if _UNHELD_KILL.search(line):
            found.append((number, line.strip()))
            continue
        for match in _KILL_WORD.finditer(line):
            rest = line[match.end():]
            cut = _KILL_END.search(rest)
            if cut:
                rest = rest[:cut.start()]
            rest = re.sub(r"\s\d?>&?\S*", " ", " " + rest)   # redirections
            try:
                words = shlex.split(rest)
            except ValueError:
                words = rest.split()
            sig, targets, i = None, [], 0
            while i < len(words):
                word = words[i]
                if word == "--":
                    targets.extend(words[i + 1:])
                    break
                if word in ("-s", "-n") and i + 1 < len(words):
                    sig, i = words[i + 1], i + 2
                    continue
                if word.startswith("-") and "$" not in word:
                    sig, i = word[1:], i + 1
                    continue
                targets.append(word)
                i += 1
            if sig in ("0", "l", "L"):
                continue
            if any("$" in t and t.lstrip("-") != "$$" for t in targets):
                found.append((number, line.strip()))
    return found


# b957702e1's bin/_lib-kill-server.sh, the two lines that signalled a pid the
# harness might no longer hold: the control the check must fail on where git
# history is absent (the public mirror).
_BASE_KILL_SERVER_LINES = (
    "    kill -TERM \"$pid\" 2>/dev/null || true\n"
    "        kill -KILL \"$pid\" 2>/dev/null || true\n"
)


def test_the_static_check_fails_on_the_base_kill_server_helper():
    """Non-vacuity for the check below, against the helper #1087 replaced."""
    base = subprocess.run(["git", "-C", str(REPO), "show", "b957702e1:bin/_lib-kill-server.sh"],
                          capture_output=True, text=True)
    text = base.stdout if base.returncode == 0 and base.stdout else _BASE_KILL_SERVER_LINES
    flagged = signalling_kills(text)
    assert [line for _, line in flagged] == [
        'kill -TERM "$pid" 2>/dev/null || true',
        'kill -KILL "$pid" 2>/dev/null || true',
    ], flagged


@pytest.mark.parametrize("line,flagged", [
    ('kill "$pid"', True),
    ('kill -9 -- "$pid"', True),
    ('kill -s TERM "$job"', True),
    ('kill -TERM -"$pgid" 2>/dev/null', True),
    ('for p in $PIDS; do kill "$p"; done', True),
    ('command kill "$pid"', True),
    ('builtin kill -TERM "$pid"', True),
    ('echo "$pid" | xargs kill', True),
    ('xargs -n1 kill -TERM < "$pids"', True),
    ('pgrep -f srv | xargs /bin/kill -9', True),
    ('pkill -f "cctally dashboard"', True),
    ('killall python3 2>/dev/null', True),
    ('command kill -0 "$pid"', False),
    ('# pkill -f x in a comment', False),
    ('skill_level=3', False),
    ('kill -0 "$pid" 2>/dev/null || break', False),
    ('kill -s 0 "$pid"', False),
    ('kill -TERM $$', False),
    ('os.kill(pid, signal.SIGTERM)', False),
    ('# kill -TERM "$pid" in a comment', False),
])
def test_the_static_check_reads_each_kill_form(line, flagged):
    assert bool(signalling_kills(line + "\n")) is flagged


def _signalling_scripts(root):
    offenders = {}
    for path in _shell_scripts(root):
        found = signalling_kills(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            offenders[path.name] = found
    return offenders


def test_the_bin_guard_flags_a_synthetic_command_kill_script(tmp_path):
    """Non-vacuity for the guard below over a directory: a script that
    signals a held number through `command kill` is found by its shebang."""
    (tmp_path / "cctally-fake-test").write_text(
        "#!/usr/bin/env bash\nsleep 5 &\npid=$!\ncommand kill \"$pid\"\n", encoding="utf-8")
    (tmp_path / "_lib_fake.py").write_text("os.kill(pid, 9)\n", encoding="utf-8")
    assert _signalling_scripts(tmp_path) == {"cctally-fake-test": [(4, 'command kill "$pid"')]}


def test_no_shell_script_in_bin_signals_a_process_number():
    """A bin/ shell script that signals a process number it may no longer
    hold (a `kill` of a variable pid, or `pkill`, `killall`, `xargs … kill`)
    can signal another user process that was given the number. bin/ has no
    script subdirectories (only __pycache__), so its top level is every
    script."""
    scripts = list(_shell_scripts())
    assert any(p.name == "cctally-test-all" for p in scripts), scripts
    assert not [p for p in BIN.iterdir() if p.is_dir() and p.name != "__pycache__"]
    offenders = _signalling_scripts(BIN)
    assert offenders == {}, (
        "bin/ shell scripts signal a process number they may no longer hold; "
        "start the work under bin/_lib-hold.sh instead: %r" % offenders)


def test_only_the_aggregator_starts_a_group_scope_holder():
    users = {}
    for path in _shell_scripts():
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = [n for n, line in _code_lines(text) if _GROUP_HOLD.search(line)]
        if hits:
            users[path.name] = hits
    # Non-vacuity: the pool and the pytest legs.
    assert len(users.pop("cctally-test-all", [])) >= 2, users
    assert users == {}, "only bin/cctally-test-all may start a group-scope holder: %r" % users
