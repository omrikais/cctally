"""_acquire_cache_flock: timeout=None is a single non-blocking attempt;
timeout>0 retries until the deadline; returns True iff the lock is held."""
from __future__ import annotations
import fcntl, json, time
import pytest
from conftest import load_script, redirect_paths


@pytest.fixture
def ns_paths(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def test_non_blocking_returns_false_when_held(ns_paths):
    ns = ns_paths
    acquire = ns["_acquire_cache_flock"]
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        me = open(lock_path, "w")
        assert acquire(me, timeout=None) is False
        me.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


def test_timeout_gives_up_after_deadline(ns_paths):
    ns = ns_paths
    acquire = ns["_acquire_cache_flock"]
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        me = open(lock_path, "w")
        t0 = time.monotonic()
        assert acquire(me, timeout=0.5) is False
        assert time.monotonic() - t0 >= 0.4
        me.close()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN); holder.close()


def test_acquires_when_free(ns_paths):
    ns = ns_paths
    acquire = ns["_acquire_cache_flock"]
    lock_path = ns["_cctally_core"].CACHE_LOCK_PATH
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    me = open(lock_path, "w")
    assert acquire(me, timeout=None) is True
    fcntl.flock(me, fcntl.LOCK_UN); me.close()


# --- #1055: the explicit deadline and the Claude hook's drain wait (T3) -----

import os  # noqa: E402
import pathlib  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]


def _adm():
    import _lib_codex_drain_admission as adm
    return adm


def _cc():
    return sys.modules["_cctally_cache"]


def _hold(path, *, shared=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    return fd


def _free(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _exclusive_free(path) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _paths(ns):
    core = ns["_cctally_core"]
    return (core.CACHE_LOCK_PATH, core.CACHE_LOCK_CODEX_PATH,
            core.APP_DIR / _adm().GATE_FILE_NAME)


class _Attempts:
    """Count ``LOCK_EX | LOCK_NB`` attempts made through ``fcntl.flock``."""

    def __init__(self, monkeypatch):
        self.count = 0
        real = fcntl.flock

        def flock(fd, op):
            if op == fcntl.LOCK_EX | fcntl.LOCK_NB:
                self.count += 1
            return real(fd, op)

        monkeypatch.setattr(fcntl, "flock", flock)


class _FakeTime:
    def __init__(self, monkeypatch, start=1000.0):
        self.now = start
        self.slept: list = []
        monkeypatch.setattr(time, "monotonic", lambda: self.now)

        def sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        monkeypatch.setattr(time, "sleep", sleep)


@pytest.mark.parametrize("held", [False, True])
def test_zero_timeout_makes_exactly_one_attempt(ns_paths, monkeypatch, held):
    """Detects: the explicit-deadline rule leaking into the legacy form, so a
    ``lock_timeout=0`` caller (conversation verification) could never acquire
    a free lock, or would retry a held one."""
    lock_path = _paths(ns_paths)[0]
    holder = _hold(lock_path) if held else None
    me = open(lock_path, "w")
    try:
        attempts = _Attempts(monkeypatch)
        assert ns_paths["_acquire_cache_flock"](me, timeout=0) is (not held)
        assert attempts.count == 1
    finally:
        me.close()
        if holder is not None:
            _free(holder)


def test_a_past_deadline_makes_no_attempt(ns_paths, monkeypatch):
    """Detects: a waiting hook that takes the writer lock after its deadline
    (an acquisition the drain no longer yields for, and a hook past its
    bound)."""
    lock_path = _paths(ns_paths)[0]
    me = open(lock_path, "w")
    try:
        attempts = _Attempts(monkeypatch)
        assert ns_paths["_acquire_cache_flock"](
            me, timeout=None, deadline=time.monotonic() - 0.001) is False
        assert attempts.count == 0
    finally:
        me.close()


def test_a_future_deadline_polls_with_clamped_steps(ns_paths, monkeypatch):
    """Detects: a poll that sleeps past the deadline (overrunning the hook's
    bound) or ignores the given step."""
    lock_path = _paths(ns_paths)[0]
    holder = _hold(lock_path)
    me = open(lock_path, "w")
    try:
        clock = _FakeTime(monkeypatch)
        attempts = _Attempts(monkeypatch)
        assert ns_paths["_acquire_cache_flock"](
            me, timeout=None, poll=0.05, deadline=clock.now + 0.12) is False
        assert clock.slept == pytest.approx([0.05, 0.05, 0.02])
        assert attempts.count == 3
    finally:
        me.close()
        _free(holder)


class _Trace:
    """Record the waiting path's probes, registrations and flock meta."""

    def __init__(self, ns, monkeypatch):
        adm, cc = _adm(), _cc()
        self.probes: list = []
        self.registered: list = []
        self.meta: list = []
        self.released_with_writer_free: list = []
        self.on_register = None
        real_busy = adm.lock_busy
        real_register = adm.ClaudeWaiter.register.__func__
        real_release = adm.ClaudeWaiter.release
        lock_path = _paths(ns)[0]

        def busy(path):
            self.probes.append(pathlib.Path(path).name)
            return real_busy(path)

        def register(cls, app_dir, **kwargs):
            waiter = real_register(cls, app_dir, **kwargs)
            self.registered.append(waiter is not None)
            if waiter is not None and self.on_register is not None:
                self.on_register()
            return waiter

        def release(waiter):
            if waiter._fd is not None:
                self.released_with_writer_free.append(_exclusive_free(lock_path))
            return real_release(waiter)

        real_phase = cc._perf.phase
        trace = self

        class Phase:
            def __init__(self, name):
                self._name, self._real = name, real_phase(name)

            def __enter__(self):
                self._real.__enter__()
                return self

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

            def set_meta(self, **meta):
                if self._name == "flock":
                    trace.meta.append(meta)
                return self._real.set_meta(**meta)

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(adm, "lock_busy", busy)
        monkeypatch.setattr(adm.ClaudeWaiter, "register", classmethod(register))
        monkeypatch.setattr(adm.ClaudeWaiter, "release", release)
        monkeypatch.setattr(cc._perf, "phase", Phase)


def _hook_sync(ns):
    conn = ns["open_cache_db"]()
    try:
        return ns["sync_cache"](
            conn, drain_wait_seconds=_adm().CLAUDE_HOOK_DRAIN_WAIT_SECONDS)
    finally:
        conn.close()


@pytest.mark.parametrize("case", ["no-contention", "claude-vs-claude",
                                  "gate-only", "provider-only",
                                  "existing-waiter"])
def test_the_hook_waits_only_behind_an_observed_drain_unit(
        ns_paths, monkeypatch, case):
    """Detects: a hook that waits where today it must not, turning a hook
    storm (Claude against Claude, a Codex hook, a conversation unit) into a
    queue of waiting processes, and a second concurrent waiter."""
    ns = ns_paths
    ns["open_cache_db"]().close()
    claude, provider, gate = _paths(ns)
    held = {"no-contention": (), "claude-vs-claude": (claude,),
            "gate-only": (claude, gate), "provider-only": (claude, provider),
            "existing-waiter": (claude, provider, gate)}[case]
    fds = [_hold(path) for path in held]
    other = None
    if case == "existing-waiter":
        other = _adm().ClaudeWaiter.register(
            ns["APP_DIR"], deadline_ns=_adm()._monotonic_ns() + 5_000_000_000)
    trace = _Trace(ns, monkeypatch)
    try:
        t0 = time.monotonic()
        stats = _hook_sync(ns)
        elapsed = time.monotonic() - t0
    finally:
        if other is not None:
            other.release()
        for fd in fds:
            _free(fd)
    assert stats.lock_contended is (case != "no-contention")
    expected_probes = {
        "no-contention": [], "claude-vs-claude": [provider.name],
        "gate-only": [provider.name],
        "provider-only": [provider.name, gate.name],
        "existing-waiter": [provider.name, gate.name]}[case]
    assert trace.probes == expected_probes
    assert trace.registered == ([False] if case == "existing-waiter" else [])
    assert trace.meta[0]["drain_wait"] == {
        "no-contention": "none", "claude-vs-claude": "not_drain",
        "gate-only": "not_drain", "provider-only": "not_drain",
        "existing-waiter": "busy_waiter"}[case]
    assert elapsed < 2.0, "the hook waited"


def test_the_hook_times_out_and_releases_its_waiter(
        ns_paths, monkeypatch, capsys):
    """Detects: a waiting hook that overruns its bound, returns anything but
    today's contended result, or leaves its waiter registered (or its record
    readable) after giving up."""
    ns = ns_paths
    adm = _adm()
    ns["open_cache_db"]().close()
    monkeypatch.setattr(adm, "CLAUDE_HOOK_DRAIN_WAIT_SECONDS", 0.3)
    fds = [_hold(path) for path in _paths(ns)]
    trace = _Trace(ns, monkeypatch)
    try:
        capsys.readouterr()
        t0 = time.monotonic()
        stats = _hook_sync(ns)
        elapsed = time.monotonic() - t0
    finally:
        for fd in fds:
            _free(fd)
    assert stats.lock_contended and stats.rows_changed == 0
    assert "[cache] sync already in progress; using existing cache" in (
        capsys.readouterr().err)
    assert 0.3 <= elapsed < 2.0, elapsed
    assert trace.registered == [True] and trace.meta[0]["drain_wait"] == "timeout"
    assert trace.meta[0]["drain_wait_seconds"] >= 0.3
    waiter_file = ns["APP_DIR"] / adm.CLAUDE_WAITER_FILE_NAME
    assert waiter_file.stat().st_size == 0
    assert adm.fresh_requests(ns["APP_DIR"]) == []
    again = adm.ClaudeWaiter.register(
        ns["APP_DIR"], deadline_ns=adm._monotonic_ns() + 1_000_000_000)
    assert again is not None
    again.release()


def test_the_hook_acquires_behind_a_drain_unit_and_marks_its_hold(
        ns_paths, monkeypatch):
    """Detects: a registered hook that never acquires the writer lock once the
    unit releases it, a hook that never marks its hold, and a waiter released
    before the writer unlock. (The hold record's expiry is pinned in
    tests/test_codex_drain_admission.py.)"""
    ns = ns_paths
    adm = _adm()
    ns["open_cache_db"]().close()
    fds = [_hold(path) for path in _paths(ns)]
    trace = _Trace(ns, monkeypatch)
    records: list = []
    real_mark = adm.ClaudeWaiter.mark_holding

    def mark_holding(waiter):
        real_mark(waiter)
        records.append(adm.claude_waiter_fresh(ns["APP_DIR"]))

    monkeypatch.setattr(adm.ClaudeWaiter, "mark_holding", mark_holding)
    trace.on_register = lambda: _free(fds.pop(0))     # the unit finishes
    try:
        stats = _hook_sync(ns)
    finally:
        for fd in fds:
            _free(fd)
    assert not stats.lock_contended
    assert trace.meta[0]["drain_wait"] == "acquired"
    assert records == [True]
    assert trace.released_with_writer_free == [True]
    assert adm.fresh_requests(ns["APP_DIR"]) == []


def test_a_raising_cleanup_releases_the_writer_then_the_waiter(
        ns_paths, monkeypatch):
    """Detects: an exception in the marker cleanup that leaks cache.db.lock or
    the waiter (idling every drain), releases them in the wrong order, or is
    swallowed into a successful ingestion."""
    ns = ns_paths
    cc = _cc()
    ns["open_cache_db"]().close()
    fds = [_hold(path) for path in _paths(ns)]
    trace = _Trace(ns, monkeypatch)
    trace.on_register = lambda: _free(fds.pop(0))

    def walk():
        raise OSError("walk failed")
        yield  # pragma: no cover

    def withdraw(conn, key):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(cc, "_iter_claude_jsonl_files", walk)
    monkeypatch.setattr(cc, "_withdraw_cache_meta_marker", withdraw)
    try:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            _hook_sync(ns)
    finally:
        for fd in fds:
            _free(fd)
    assert trace.released_with_writer_free == [True]
    claude = _paths(ns)[0]
    assert _exclusive_free(claude)
    assert _adm().fresh_requests(ns["APP_DIR"]) == []


@pytest.mark.parametrize("observed", ["recent", "expired", "none"])
def test_drain_backoff_takes_the_short_step_only_after_a_recent_waiter(
        ns_paths, monkeypatch, observed):
    """Detects: a drain that idles a whole 5 s step after the waiter it was
    refused for vanished, and a hint that expedites retries against an
    ordinary writer long after the waiter left."""
    adm, cc = _adm(), _cc()
    clock = {"now": 10_000_000_000_000}
    slept: list = []
    monkeypatch.setattr(adm, "_monotonic_ns", lambda: clock["now"])
    monkeypatch.setattr(adm, "_sleep", slept.append)
    observations = {}
    if observed != "none":
        age = {"recent": adm.CLAUDE_WAITER_RECENT_SECONDS - 0.001,
               "expired": adm.CLAUDE_WAITER_RECENT_SECONDS}[observed]
        observations[os.path.realpath(ns_paths["APP_DIR"])] = (
            clock["now"] - int(age * 1_000_000_000))
    monkeypatch.setattr(adm, "_claude_waiter_observed", observations)
    cc._drain_backoff(adm)
    assert slept == [adm.SHORT_POLL_SECONDS if observed == "recent"
                     else adm.POLL_SECONDS]


_STOPPED_HOOK = r"""
import json, pathlib, sys, time, fcntl
sys.path[:0] = [sys.argv[1] + "/tests", sys.argv[1] + "/bin"]
import pytest
from conftest import load_script, redirect_paths
mp = pytest.MonkeyPatch()
ns = load_script()
redirect_paths(ns, mp, pathlib.Path(sys.argv[2]))
import _lib_codex_drain_admission as adm
real_register = adm.ClaudeWaiter.register.__func__
state = {"deadline": None, "late": 0}

def register(cls, app_dir, **kwargs):
    state["deadline"] = kwargs["deadline_ns"]
    waiter = real_register(cls, app_dir, **kwargs)
    print("registered" if waiter is not None else "refused", flush=True)
    return waiter

adm.ClaudeWaiter.register = classmethod(register)
real_flock = fcntl.flock

def flock(fd, op):
    if (op == fcntl.LOCK_EX | fcntl.LOCK_NB and state["deadline"] is not None
            and time.monotonic_ns() >= state["deadline"]):
        state["late"] += 1
    return real_flock(fd, op)

fcntl.flock = flock
conn = ns["open_cache_db"]()
stats = ns["sync_cache"](conn, drain_wait_seconds=1.0)
print(json.dumps({"contended": stats.lock_contended, "late": state["late"]}),
      flush=True)
"""


def test_a_stopped_hook_makes_no_attempt_after_its_deadline(ns_paths):
    """Real clock, real process. Detects: a hook stopped while registered that,
    once continued past its deadline, still takes the writer lock (an
    acquisition the drain resumed without, and a hook beyond its bound)."""
    ns = ns_paths
    adm = _adm()
    ns["open_cache_db"]().close()
    fds = [_hold(path) for path in _paths(ns)]
    home = pathlib.Path(os.environ["HOME"])
    child = subprocess.Popen(
        [sys.executable, "-c", _STOPPED_HOOK, str(REPO), str(home)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "registered"
        os.kill(child.pid, signal.SIGSTOP)
        end = time.monotonic() + 5
        while (adm.fresh_requests(ns["APP_DIR"])
               and time.monotonic() < end):
            time.sleep(0.01)
        assert adm.fresh_requests(ns["APP_DIR"]) == [], (
            "the stopped hook's record never expired")
        while fds:
            _free(fds.pop())                    # the writer lock is now free
        os.kill(child.pid, signal.SIGCONT)
        result = json.loads(child.stdout.readline())
        assert result == {"contended": True, "late": 0}, result
        assert child.wait(10) == 0
    finally:
        for fd in fds:
            _free(fd)
        try:
            child.kill()
        except OSError:
            pass
        child.wait(5)
        child.stdout.close()


def test_a_recovery_rerun_gets_no_second_wait(ns_paths, monkeypatch):
    """F2 (#1055): one wait per hook process. Detects: a hook-tick Claude leg
    whose recovery rerun (after a classified corruption) waits behind the
    drain a second time, doubling the hook's time behind a drain unit, and a
    waiter still registered while the store is being recovered."""
    import argparse
    import sqlite3
    ns = ns_paths
    adm, cc = _adm(), _cc()
    record = sys.modules["_cctally_record"]
    ns["open_cache_db"]().close()
    fds = [_hold(path) for path in _paths(ns)]
    trace = _Trace(ns, monkeypatch)
    trace.on_register = lambda: _free(fds.pop(0))     # the unit finishes
    calls: list = []
    real_sync = ns["sync_cache"]

    def sync(conn, **kwargs):
        calls.append(kwargs)
        return real_sync(conn, **kwargs)

    walked = {"n": 0}
    real_walk = cc._iter_claude_jsonl_files

    def walk():
        walked["n"] += 1
        if walked["n"] == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_walk()

    at_recovery: list = []

    def recover(exc, *, origin, active_conn):
        at_recovery.append((origin, _exclusive_free(_paths(ns)[0]),
                            adm.fresh_requests(ns["APP_DIR"])))
        fds[:0] = [_hold(_paths(ns)[0])]   # a drain unit holds it again
        active_conn.close()                # as the real recovery does
        return True

    monkeypatch.setitem(ns, "sync_cache", sync)
    monkeypatch.setattr(cc, "_iter_claude_jsonl_files", walk)
    monkeypatch.setattr(cc, "_recover_corrupt_cache", recover)
    monkeypatch.setattr(record, "_hook_tick_read_stdin_event", lambda *a, **k: {
        "event": "Stop", "session_id": "s", "transcript_path": "", "cwd": ""})
    try:
        t0 = time.monotonic()
        assert ns["cmd_hook_tick"](argparse.Namespace(
            explain=False, foreground=True, no_oauth=True,
            throttle_seconds=None, event=None, mock_oauth_response=None,
            source=None)) == 0
        elapsed = time.monotonic() - t0
    finally:
        for fd in fds:
            _free(fd)
    assert [c.get("drain_wait_seconds") for c in calls] == [
        adm.CLAUDE_HOOK_DRAIN_WAIT_SECONDS, None], calls
    assert at_recovery == [("hook.claude.sync", True, [])], (
        "the waiter or the writer lock was still held during recovery",
        at_recovery)
    assert trace.registered == [True], "the rerun registered a second waiter"
    assert [m["drain_wait"] for m in trace.meta] == ["acquired", "none"]
    assert elapsed < adm.CLAUDE_HOOK_DRAIN_WAIT_SECONDS
