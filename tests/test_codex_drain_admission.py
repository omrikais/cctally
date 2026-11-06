"""#968 spec 5.7: waiting requests and the one drain-admission gate."""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))
import _lib_codex_drain_admission as adm  # noqa: E402


class Clock:
    def __init__(self, start=10_000_000_000_000):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(adm, "_monotonic_ns", c)
    return c


def test_requests_are_unique_immutable_and_expire_after_sixty_seconds(
        tmp_path, clock):
    a = adm.publish_waiting_request(tmp_path, root_keys=["k1"])
    b = adm.publish_waiting_request(tmp_path, root_keys=["k1"])
    assert a and b and a != b                       # concurrent publishers
    assert sorted(adm.fresh_requests(tmp_path)) == sorted([a, b])
    clock.now += 59_999_999_999
    assert len(adm.fresh_requests(tmp_path)) == 2
    clock.now += 2
    assert adm.fresh_requests(tmp_path) == []
    assert list((tmp_path / adm.REQUEST_DIR_NAME).glob("req-*.json")) == []


def test_observation_and_restart_never_renew_a_request(tmp_path, clock):
    adm.publish_waiting_request(tmp_path, root_keys=[])
    for _ in range(5):
        clock.now += 10_000_000_000
        adm.fresh_requests(tmp_path)               # a worker observing it
    clock.now += 10_000_000_001                    # 60 s + 1 ns after publication
    assert adm.fresh_requests(tmp_path) == []


def test_malformed_negative_and_wall_stale_records_never_hold_a_yield(
        tmp_path, clock):
    d = tmp_path / adm.REQUEST_DIR_NAME
    d.mkdir()
    (d / "req-1-1-bad.json").write_text("{not json")
    (d / "req-2-1-future.json").write_text(json.dumps(
        {"v": 1, "id": "x", "roots": [], "mono_ns": clock.now + 10**12}))
    old = d / "req-3-1-old.json"
    old.write_text(json.dumps(
        {"v": 1, "id": "y", "roots": [], "mono_ns": clock.now}))
    os.utime(old, (1, 1))                          # wall age far beyond 120 s
    assert adm.fresh_requests(tmp_path) == []
    assert list(d.glob("req-*.json")) == []


def test_admission_rechecks_after_the_gate_and_after_the_store_locks(
        tmp_path, clock):
    events = []
    held = {"locks": False}

    def try_locks():
        held["locks"] = True
        return "LOCKS"

    def release_locks(token):
        assert token == "LOCKS"
        held["locks"] = False

    def on_check(stage):
        events.append(stage)
        if stage == "after-gate":
            adm.publish_waiting_request(tmp_path, root_keys=[])  # hook lands

    result = adm.admit(tmp_path, try_locks, release_locks, on_check=on_check)
    assert not result.admitted
    assert events == ["before-gate", "after-gate"]
    assert held["locks"] is False                  # nothing left acquired
    gate = adm.DrainGate(tmp_path)
    assert gate.try_acquire()                      # the gate was released too
    gate.release()


def test_a_request_landing_while_the_store_locks_are_taken_stops_admission(
        tmp_path, clock):
    """Review P2-6: the third check (after the store locks) is reachable, and
    a stop there releases the store locks and the gate."""
    events = []
    held = {"locks": False}

    def try_locks():
        adm.publish_waiting_request(tmp_path, root_keys=[])  # hook lands now
        held["locks"] = True
        return "LOCKS"

    def release_locks(token):
        assert token == "LOCKS"
        held["locks"] = False

    result = adm.admit(tmp_path, try_locks, release_locks,
                       on_check=events.append)
    assert not result.admitted
    assert events == ["before-gate", "after-gate", "after-store-locks"]
    assert held["locks"] is False
    gate = adm.DrainGate(tmp_path)
    assert gate.try_acquire()
    gate.release()


def test_a_future_wall_mtime_is_fresh_but_never_outlives_sixty_seconds(
        tmp_path, clock):
    """Review P3-5: a negative wall age (the record's mtime is ahead of the
    wall clock, as after a backward wall-clock step) never deletes a request
    early; the single 60 s monotonic lifetime still ends it."""
    rid = adm.publish_waiting_request(tmp_path, root_keys=[])
    record = tmp_path / adm.REQUEST_DIR_NAME / f"req-{rid}.json"
    future = time.time() + 3600
    os.utime(record, (future, future))
    assert adm.fresh_requests(tmp_path) == [rid]
    clock.now += adm.REQUEST_TTL_NS - 1
    assert adm.fresh_requests(tmp_path) == [rid]
    clock.now += 1
    assert adm.fresh_requests(tmp_path) == []


def test_finish_calls_the_post_release_seam_after_the_gate_is_free(
        tmp_path, clock, monkeypatch):
    """Review P2-7: the fairness test synchronizes on lock release through
    this seam, never on a sleep."""
    seen = []

    def after_finish():
        gate = adm.DrainGate(tmp_path)
        seen.append(gate.try_acquire())
        gate.release()

    monkeypatch.setattr(adm, "_after_finish", after_finish)
    result = adm.admit(tmp_path, lambda: "L", lambda token: None)
    adm.finish(result, lambda token: None)
    assert seen == [True]


def test_admitted_work_holds_the_gate_until_finish(tmp_path, clock):
    result = adm.admit(tmp_path, lambda: "L", lambda token: None)
    assert result.admitted
    other = adm.DrainGate(tmp_path)
    assert not other.try_acquire()                 # a second worker cannot queue
    adm.finish(result, lambda token: None)
    assert other.try_acquire()
    other.release()


def test_store_lock_contention_is_a_yield_not_a_wait(tmp_path, clock):
    result = adm.admit(tmp_path, lambda: None, lambda token: None)
    assert not result.admitted and result.store_locks is None


# --- #1055: the Claude waiter (spec 4.1-4.3, T2) ---------------------------

import fcntl  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
SECOND_NS = 1_000_000_000


def _waiter_path(app_dir) -> pathlib.Path:
    return pathlib.Path(app_dir) / adm.CLAUDE_WAITER_FILE_NAME


def _register(app_dir, clock, seconds=1.0):
    waiter = adm.ClaudeWaiter.register(
        app_dir, deadline_ns=clock.now + int(seconds * SECOND_NS))
    assert waiter is not None
    return waiter


def _hold_with_record(app_dir, content: bytes) -> int:
    """An exclusive holder of the waiter file carrying ``content``."""
    fd = os.open(_waiter_path(app_dir), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.pwrite(fd, content, 0)
    os.ftruncate(fd, len(content))
    return fd


def _release_holder(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


@pytest.fixture
def no_hint(monkeypatch):
    monkeypatch.setattr(adm, "_claude_waiter_observed", {})


@pytest.mark.parametrize("request_dir", ["present", "absent", "unreadable"])
def test_a_fresh_waiter_is_the_sentinel_after_the_codex_ids(
        tmp_path, clock, no_hint, request_dir):
    """Detects: a waiter drains never see because ``codex-drain-requests/`` is
    missing or unreadable (the early return), a sentinel that displaces or
    reorders Codex ids, and a value that changes with no waiter present."""
    rid = None
    if request_dir == "present":
        rid = adm.publish_waiting_request(tmp_path, root_keys=[])
    elif request_dir == "unreadable":
        (tmp_path / adm.REQUEST_DIR_NAME).write_text("not a directory")
    before = adm.fresh_requests(tmp_path)
    assert before == ([rid] if rid else [])
    waiter = _register(tmp_path, clock)
    try:
        assert adm.fresh_requests(tmp_path) == before + [adm.CLAUDE_WAITER_SENTINEL]
    finally:
        waiter.release()
    assert adm.fresh_requests(tmp_path) == before
    assert _waiter_path(tmp_path).exists(), "the waiter file is never unlinked"


def test_the_probe_creates_nothing_and_holds_nothing(tmp_path, clock, no_hint):
    """Detects: a drain-side probe that creates the waiter file or keeps a
    shared lock that would refuse the next hook's registration."""
    assert adm.claude_waiter_fresh(tmp_path) is False
    assert not _waiter_path(tmp_path).exists()
    _register(tmp_path, clock).release()
    assert adm.claude_waiter_fresh(tmp_path) is False
    waiter = _register(tmp_path, clock)        # the probe left no shared lock
    waiter.release()


@pytest.mark.parametrize("case", [
    "empty", "unparseable", "wrong-version", "at-expiry", "past-expiry",
    "beyond-bound", "just-fresh", "at-bound"])
def test_only_an_unexpired_bounded_record_is_fresh(tmp_path, clock, no_hint, case):
    """Detects: a held lock alone idling every drain (a stopped hook between
    lock and record write), an expired record that keeps a drain waiting, and
    a torn or corrupt expiry far in the future creating a long stall."""
    bound = int(max(adm.CLAUDE_HOOK_DRAIN_WAIT_SECONDS,
                    adm.CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS) * SECOND_NS)
    expiry = {"at-expiry": clock.now, "past-expiry": clock.now - 1,
              "beyond-bound": clock.now + bound + 1,
              "just-fresh": clock.now + 1,
              "at-bound": clock.now + bound}.get(case, clock.now + SECOND_NS)
    content = {
        "empty": b"",
        "unparseable": b"cctally-claude-waiter v1 123 soon\n",
        "wrong-version": f"cctally-claude-waiter v2 123 {expiry}\n".encode(),
    }.get(case, f"cctally-claude-waiter v1 123 {expiry}\n".encode())
    fd = _hold_with_record(tmp_path, content)
    try:
        expected = case in ("just-fresh", "at-bound")
        assert adm.claude_waiter_fresh(tmp_path) is expected
        assert (adm.CLAUDE_WAITER_SENTINEL in adm.fresh_requests(tmp_path)) is expected
    finally:
        _release_holder(fd)


def test_release_truncates_the_record_before_unlocking(
        tmp_path, clock, no_hint, monkeypatch):
    """Detects: a release that unlocks first, so a racing probe could read a
    live-looking record from a lock a later process holds, and a release that
    raises or leaves the descriptor open when called twice."""
    waiter = _register(tmp_path, clock)
    seen = []
    real_flock = fcntl.flock

    def flock(fd, op):
        if op == fcntl.LOCK_UN:
            seen.append(os.fstat(fd).st_size)
        return real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    waiter.release()
    waiter.release()                            # idempotent
    monkeypatch.setattr(fcntl, "flock", real_flock)
    assert seen == [0]
    assert _waiter_path(tmp_path).stat().st_size == 0


def test_mark_holding_rewrites_the_record_with_the_hold_allowance(
        tmp_path, clock, no_hint):
    """Detects: a waiter whose freshness ends at its acquisition deadline while
    it still syncs (the drain would retake the lock mid-ingest), and one whose
    hold never ends (a stopped hook after its writer unlock)."""
    waiter = _register(tmp_path, clock, seconds=1.0)
    try:
        clock.now += 900_000_000
        waiter.mark_holding()
        record = _waiter_path(tmp_path).read_bytes()
        expiry = int(record.split()[-1])
        assert record == f"cctally-claude-waiter v1 {os.getpid()} {expiry}\n".encode()
        hold = int(adm.CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS * SECOND_NS)
        assert expiry == clock.now + hold
        clock.now += hold - 1
        assert adm.claude_waiter_fresh(tmp_path)
        clock.now += 1
        assert not adm.claude_waiter_fresh(tmp_path)
    finally:
        waiter.release()


def test_one_waiter_at_a_time_and_a_shared_probe_refuses_registration(
        tmp_path, clock, no_hint):
    """Detects: two hooks waiting at once (a hook storm becoming a queue of
    waiting processes), and a registration that retries or blocks when a
    drain's shared probe coincides with it."""
    first = _register(tmp_path, clock)
    assert adm.ClaudeWaiter.register(
        tmp_path, deadline_ns=clock.now + SECOND_NS) is None
    first.release()
    probe = os.open(_waiter_path(tmp_path), os.O_RDONLY)
    try:
        fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert adm.ClaudeWaiter.register(
            tmp_path, deadline_ns=clock.now + SECOND_NS) is None
    finally:
        os.close(probe)
    _register(tmp_path, clock).release()


@pytest.mark.parametrize("stage", ["before-gate", "after-gate",
                                   "after-store-locks"])
def test_admission_refuses_at_every_check_while_a_waiter_is_fresh(
        tmp_path, clock, no_hint, stage):
    """Detects: a drain unit admitted past a registered Claude waiter at any
    of the three checks, and a refusal that leaves the gate or the store
    locks held."""
    events, held, waiters = [], {"locks": False}, []

    def try_locks():
        held["locks"] = True
        if stage == "after-store-locks":
            waiters.append(_register(tmp_path, clock))
        return "LOCKS"

    def release_locks(token):
        held["locks"] = False

    def on_check(name):
        events.append(name)
        if name == stage and stage != "after-store-locks":
            waiters.append(_register(tmp_path, clock))

    try:
        result = adm.admit(tmp_path, try_locks, release_locks, on_check=on_check)
    finally:
        for waiter in waiters:
            waiter.release()
    assert not result.admitted
    order = ["before-gate", "after-gate", "after-store-locks"]
    assert events == order[:order.index(stage) + 1]
    assert held["locks"] is False
    gate = adm.DrainGate(tmp_path)
    assert gate.try_acquire()
    gate.release()


def _sleeping(clock, monkeypatch, on_sleep=None):
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if on_sleep is not None:
            on_sleep(len(slept))
        clock.now += int(seconds * SECOND_NS)

    monkeypatch.setattr(adm, "_sleep", sleep)
    return slept


def test_waiting_out_the_sentinel_alone_takes_short_steps(
        tmp_path, clock, no_hint, monkeypatch):
    """Detects: a drain that idles a whole 5 s poll step per Claude waiter
    (the executor losing seconds to every yield)."""
    waiter = _register(tmp_path, clock, seconds=1.0)
    expiry = clock.now + SECOND_NS
    slept = _sleeping(clock, monkeypatch)
    try:
        adm.wait_until_no_fresh_requests(tmp_path)
    finally:
        waiter.release()
    assert slept and set(slept) == {adm.SHORT_POLL_SECONDS}
    assert expiry <= clock.now < expiry + int(adm.SHORT_POLL_SECONDS * SECOND_NS) + 1


def test_waiting_reclassifies_after_every_sleep(
        tmp_path, clock, no_hint, monkeypatch):
    """Detects: a Codex request waited out in short steps (a busy spin for its
    60 s lifetime), and a set classified once instead of after each sleep."""
    waiter = _register(tmp_path, clock, seconds=1.0)

    def on_sleep(count):
        if count == 3:
            adm.publish_waiting_request(tmp_path, root_keys=[])

    slept = _sleeping(clock, monkeypatch, on_sleep)
    try:
        adm.wait_until_no_fresh_requests(tmp_path)
    finally:
        waiter.release()
    assert slept[:3] == [adm.SHORT_POLL_SECONDS] * 3
    assert slept[3:] and set(slept[3:]) == {adm.POLL_SECONDS}
    assert adm.fresh_requests(tmp_path) == []


def test_waiting_with_a_mixed_set_keeps_the_codex_step(
        tmp_path, clock, no_hint, monkeypatch):
    """Detects: a fresh Claude waiter shortening the poll step while a Codex
    request is also fresh, and a wait that ignores ``should_continue``."""
    adm.publish_waiting_request(tmp_path, root_keys=[])
    waiter = _register(tmp_path, clock, seconds=1.0)
    slept = _sleeping(clock, monkeypatch)
    try:
        adm.wait_until_no_fresh_requests(tmp_path, should_continue=lambda: False)
        assert slept == []
        adm.wait_until_no_fresh_requests(tmp_path)
    finally:
        waiter.release()
    assert slept[0] == adm.POLL_SECONDS and set(slept) == {adm.POLL_SECONDS}


def test_the_observation_hint_is_per_dir_never_renewed_and_expires(
        tmp_path, clock, no_hint):
    """Detects: a hint that an empty check renews (so a later ordinary writer
    is retried in short steps forever), one shared across data dirs, and one
    that outlives ``CLAUDE_WAITER_RECENT_SECONDS``."""
    other = tmp_path / "other"
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    assert not adm.claude_waiter_recently_observed(tmp_path)
    waiter = _register(tmp_path, clock)
    try:
        assert adm.fresh_requests(tmp_path) == [adm.CLAUDE_WAITER_SENTINEL]
    finally:
        waiter.release()
    observed = clock.now
    assert adm.claude_waiter_recently_observed(tmp_path)
    assert adm.claude_waiter_recently_observed(alias)       # realpath key
    assert not adm.claude_waiter_recently_observed(other)
    clock.now += SECOND_NS // 2
    assert adm.fresh_requests(tmp_path) == []               # empty check
    clock.now = observed + int(adm.CLAUDE_WAITER_RECENT_SECONDS * SECOND_NS) - 1
    assert adm.claude_waiter_recently_observed(tmp_path)
    clock.now += 1
    assert not adm.claude_waiter_recently_observed(tmp_path)


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_child_starts_without_the_hint(tmp_path, clock, no_hint):
    """Detects: a forked hook child or worker inheriting its parent's recent
    observation and expediting retries it never earned."""
    waiter = _register(tmp_path, clock)
    try:
        assert adm.claude_waiter_fresh(tmp_path)
    finally:
        waiter.release()
    assert adm.claude_waiter_recently_observed(tmp_path)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:                                # pragma: no cover - child
        try:
            os.write(write_fd, b"1" if adm.claude_waiter_recently_observed(
                tmp_path) else b"0")
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        import select
        ready, _, _ = select.select([read_fd], [], [], 10)
        assert ready, "the forked child never answered (deadlocked after fork)"
        assert os.read(read_fd, 1) == b"0"
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)


_CHILD = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
import _lib_codex_drain_admission as adm
app, mode = sys.argv[2], sys.argv[3]
adm.CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS = 1.0
waiter = adm.ClaudeWaiter.register(
    app, deadline_ns=adm._monotonic_ns() + 1_000_000_000)
if waiter is None:
    print("none", flush=True)
    sys.exit(1)
if mode == "holding":
    waiter.mark_holding()
print(open(os.path.join(app, adm.CLAUDE_WAITER_FILE_NAME)).read().split()[-1],
      flush=True)
time.sleep(30)
"""


def _spawn_waiter(app_dir, mode):
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(BIN), str(app_dir), mode],
        stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip()
    assert line.isdigit(), ("the child did not register", line)
    return child, int(line)


def _reap(child) -> None:
    try:
        child.kill()
    except OSError:
        pass
    child.wait(5)
    child.stdout.close()


@pytest.mark.parametrize("mode", ["registered", "holding"])
def test_a_stopped_waiter_idles_drains_only_until_its_record_expires(
        tmp_path, no_hint, mode):
    """Real clock, real process. Detects: a living but stopped hook (after its
    registration, or between its writer unlock and its waiter release) that
    idles every drain while the Claude writer lock is free; the drain must
    resume once the owner-written record expires, whatever the lock says."""
    child, expiry = _spawn_waiter(tmp_path, mode)
    try:
        os.kill(child.pid, signal.SIGSTOP)
        assert adm.CLAUDE_WAITER_SENTINEL in adm.fresh_requests(tmp_path)
        end = time.monotonic() + 5
        while (adm.CLAUDE_WAITER_SENTINEL in adm.fresh_requests(tmp_path)
               and time.monotonic() < end):
            time.sleep(0.01)
        lost = time.monotonic_ns()
        assert adm.CLAUDE_WAITER_SENTINEL not in adm.fresh_requests(tmp_path)
        # Not fresh before its expiry, and dropped well before the 5 s poll
        # limit (1 s of slack for a loaded runner's scheduling).
        assert lost >= expiry and lost <= expiry + 1_000_000_000, (expiry, lost)
        fd = os.open(_waiter_path(tmp_path), os.O_RDONLY)
        try:                                    # still held by the stopped child
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        _reap(child)


def test_a_killed_waiter_is_released_by_the_kernel(tmp_path, no_hint):
    """Real process. Detects: a waiter that only a clean exit releases, so a
    killed hook would idle drains until its record expired."""
    child, _expiry = _spawn_waiter(tmp_path, "registered")
    try:
        assert adm.CLAUDE_WAITER_SENTINEL in adm.fresh_requests(tmp_path)
        child.kill()
        child.wait(5)
        assert adm.fresh_requests(tmp_path) == []
        waiter = adm.ClaudeWaiter.register(
            tmp_path, deadline_ns=adm._monotonic_ns() + SECOND_NS)
        assert waiter is not None
        waiter.release()
    finally:
        _reap(child)
