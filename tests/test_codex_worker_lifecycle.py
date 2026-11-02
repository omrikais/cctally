"""#939 D5: detached Codex worker attempt records.

writeIo's admission hold must not settle while a detached Codex worker is
requested, running, or between stages: such a worker is still doing startup
work, and its gate or lock is momentarily free between identities. Every test
here names the false release (or the permanent hold) it would detect.
"""
import json
import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for extra in (REPO_ROOT / "bin", REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import _lib_codex_worker_lifecycle as lc  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402

CMD = "_codex-replay-drain"


class _Alive:
    def __init__(self, *pids):
        self.pids = set(pids)

    def __call__(self, pid):
        return pid in self.pids


def _records(app_dir, command=CMD):
    return sorted((pathlib.Path(app_dir) / "codex-workers").glob(f"{command}.*.json"))


def test_the_three_worker_commands_are_named():
    assert set(lc.WORKER_COMMANDS) == {
        "_codex-replay-drain", "_codex-prune-drain", "_codex-inventory-bootstrap"}
    assert lc.HANDOFF_SECONDS == 120
    assert lc.ATTEMPT_ENV == "CCTALLY_WORKER_ATTEMPT"


def test_a_requested_worker_is_in_progress_only_through_the_handoff(tmp_path):
    """Detects: a spawned child that has not taken any lock yet read as idle
    (a false release), or a child that never started holding forever."""
    attempt = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    assert attempt
    record = json.loads(_records(tmp_path)[0].read_text())
    assert record["requestedAt"] == 1000.0 and record.get("pid") is None
    alive = _Alive()
    assert lc.in_progress(tmp_path, CMD, now=1119.0, pid_alive=alive)
    assert not lc.in_progress(tmp_path, CMD, now=1120.0, pid_alive=alive)
    assert not lc.in_progress(tmp_path, "_codex-prune-drain", now=1001.0,
                              pid_alive=alive)


def _lock_path(app_dir, attempt, command=CMD):
    return pathlib.Path(app_dir) / "codex-workers" / f"{command}.{attempt}.lock"


def _hold(path):
    """Hold ``path``'s flock through a descriptor of this test's own: flock is
    per open file description, so the probe's separate open still sees it."""
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_started_worker_is_in_progress_between_stages_until_it_ends(tmp_path):
    """Detects: a worker between identities (no store lock held) read as idle,
    or an ended worker still holding the hold."""
    attempt = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    lock = lc.mark_started(tmp_path, CMD, attempt, pid=4242, now=1001.0)
    record = json.loads(_records(tmp_path)[0].read_text())
    assert (record["pid"], record["startedAt"]) == (4242, 1001.0)
    # Liveness is the worker's lifetime lock, not its pid.
    assert lc.in_progress(tmp_path, CMD, now=5000.0, pid_alive=_Alive())
    lc.end_attempt(tmp_path, CMD, attempt, lock)
    assert _records(tmp_path) == []
    assert not _lock_path(tmp_path, attempt).exists()
    assert not lc.in_progress(tmp_path, CMD, now=1002.0, pid_alive=_Alive(4242))


def test_a_started_record_whose_lock_is_free_is_dead_even_with_a_live_pid(tmp_path):
    """Detects: a worker killed without its ``finally`` (SIGKILL, logout,
    reboot) whose recorded pid now belongs to an unrelated long-lived process
    holding writeIo's admission forever."""
    attempt = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    lock = lc.mark_started(tmp_path, CMD, attempt, pid=4242, now=1001.0)
    os.close(lock)                      # the worker died; its lock went with it
    assert _records(tmp_path), "the killed worker's record stays behind"
    assert not lc.in_progress(tmp_path, CMD, now=1002.0, pid_alive=_Alive(4242))
    # A missing lock file is dead too; the probe never creates it.
    _lock_path(tmp_path, attempt).unlink()
    assert not lc.in_progress(tmp_path, CMD, now=1002.0, pid_alive=_Alive(4242))
    assert not _lock_path(tmp_path, attempt).exists()


def test_a_started_record_whose_lock_is_held_is_live(tmp_path):
    """Detects: a running worker read as idle because its pid looks dead to
    this process (another pid namespace, a recycled check)."""
    attempt = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    lock = lc.mark_started(tmp_path, CMD, attempt, pid=4242, now=1001.0)
    os.close(lock)
    held = _hold(_lock_path(tmp_path, attempt))
    try:
        assert lc.in_progress(tmp_path, CMD, now=9999.0, pid_alive=_Alive())
    finally:
        os.close(held)
    assert not lc.in_progress(tmp_path, CMD, now=9999.0, pid_alive=_Alive())


def test_an_unstarted_record_counts_only_while_its_spawned_child_lives(tmp_path):
    """Detects: a child that always dies before marking its start keeping a
    young pid-less record (re-spawned every minute) live forever."""
    attempt = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    lc.note_spawned(tmp_path, CMD, attempt, pid=77)
    record = json.loads(_records(tmp_path)[0].read_text())
    assert record["spawnPid"] == 77 and record.get("startedAt") is None
    assert not lc.in_progress(tmp_path, CMD, now=1001.0, pid_alive=_Alive())
    assert lc.in_progress(tmp_path, CMD, now=1119.0, pid_alive=_Alive(77))
    assert not lc.in_progress(tmp_path, CMD, now=1120.0, pid_alive=_Alive(77))


def _wait_until_exited(proc):
    """Until ``proc`` has exited, without reaping it."""
    import subprocess
    import time
    deadline = time.time() + 20
    while True:
        if hasattr(os, "waitid"):
            if os.waitid(os.P_PID, proc.pid,
                         os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return
        else:
            stat = subprocess.run(["ps", "-o", "stat=", "-p", str(proc.pid)],
                                  capture_output=True, text=True).stdout.strip()
            if not stat or stat.startswith("Z"):
                return
        assert time.time() < deadline, "the child never exited"
        time.sleep(0.05)


@pytest.mark.parametrize("waitid", ["available", "missing"])
def test_an_exited_unreaped_launched_child_is_not_alive(tmp_path, monkeypatch,
                                                         waitid):
    """Detects: ``os.kill(pid, 0)`` answering for the dashboard's own exited,
    not yet reaped child (a zombie), so a child that always dies before
    marking its start keeps admission held, including on a Python without
    ``os.waitid`` (macOS 3.11 and 3.12, #939 R8-04); and a probe that takes
    the child's exit status away from its handle (R7-03)."""
    import subprocess
    import time
    now = time.time()
    attempt = lc.begin_attempt(tmp_path, CMD, now=now)
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    lc.remember_launch(proc, attempt=attempt)
    lc.note_spawned(tmp_path, CMD, attempt, pid=proc.pid)
    _wait_until_exited(proc)
    if waitid == "missing":
        monkeypatch.delattr(os, "waitid", raising=False)
    try:
        assert not lc.in_progress(tmp_path, CMD, now=now + 1)
    finally:
        assert proc.wait() == 3          # the owner still collects its status


def test_probing_a_child_it_did_not_launch_keeps_its_exit_status(tmp_path):
    """Detects: the liveness probe reaping another child of the process that
    reused a recorded pid, taking its exit status from its real owner
    (#939 R7-03)."""
    import subprocess
    import time
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    _wait_until_exited(proc)
    now = time.time()
    attempt = lc.begin_attempt(tmp_path, CMD, now=now)
    lc.note_spawned(tmp_path, CMD, attempt, pid=proc.pid)
    try:
        alive = lc.in_progress(tmp_path, CMD, now=now + 1)
        if hasattr(os, "waitid"):
            assert not alive
    finally:
        assert proc.wait() == 3


def test_a_retained_launch_answers_only_for_its_own_attempt(tmp_path):
    """Detects: a retained handle of this process's exited launch answering
    for another attempt whose child reused its pid (another cctally process's
    worker still in its handoff), so a live handoff stops blocking admission
    (#939 R9-02)."""
    class Exited:
        pid = os.getpid()               # a pid `kill(pid, 0)` answers for

        def poll(self):
            return 3

    now = 1000.0
    ours = lc.begin_attempt(tmp_path, CMD, now=now)
    lc.remember_launch(Exited(), attempt=ours)
    lc.note_spawned(tmp_path, CMD, ours, pid=os.getpid())
    assert not lc.in_progress(tmp_path, CMD, now=now + 1)
    theirs = lc.begin_attempt(tmp_path, CMD, now=now + 2)
    lc.note_spawned(tmp_path, CMD, theirs, pid=os.getpid())
    assert lc.in_progress(tmp_path, CMD, now=now + 3)


def test_noting_the_spawn_never_clobbers_a_started_record(tmp_path):
    """Detects: the spawner's late ``spawnPid`` write erasing the child's
    ``startedAt`` (a lost update), so a running worker expires after the
    handoff."""
    attempt = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    lock = lc.mark_started(tmp_path, CMD, attempt, pid=4242, now=1000.5)
    lc.note_spawned(tmp_path, CMD, attempt, pid=4242)
    record = json.loads(_records(tmp_path)[0].read_text())
    assert record["startedAt"] == 1000.5
    lc.end_attempt(tmp_path, CMD, attempt, lock)
    lc.note_spawned(tmp_path, CMD, attempt, pid=4242)    # after the end
    assert _records(tmp_path) == []
    assert not _lock_path(tmp_path, attempt).exists()


def test_one_attempts_end_never_retires_another(tmp_path):
    """Detects: a second worker that lost the command's lock and exited
    removing the first, still running, worker's record."""
    first = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    first_lock = lc.mark_started(tmp_path, CMD, first, pid=11, now=1000.5)
    second = lc.begin_attempt(tmp_path, CMD, now=1001.0)
    second_lock = lc.mark_started(tmp_path, CMD, second, pid=12, now=1001.5)
    lc.end_attempt(tmp_path, CMD, second, second_lock)
    assert lc.in_progress(tmp_path, CMD, now=5000.0, pid_alive=_Alive())
    assert _lock_path(tmp_path, first).exists()
    lc.end_attempt(tmp_path, CMD, first, first_lock)
    assert not lc.in_progress(tmp_path, CMD, now=5001.0, pid_alive=_Alive(11))


def test_the_next_request_removes_only_stale_records(tmp_path):
    """Detects: records of dead or never-started workers accumulating, or a
    live worker's record removed by the next spawner."""
    dead = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    os.close(lc.mark_started(tmp_path, CMD, dead, pid=20, now=1000.0))
    live = lc.begin_attempt(tmp_path, CMD, now=1000.0)
    live_lock = lc.mark_started(tmp_path, CMD, live, pid=21, now=1000.0)
    lc.begin_attempt(tmp_path, CMD, now=1000.0)           # never starts
    fresh = lc.begin_attempt(tmp_path, CMD, now=1200.0)
    names = {p.name for p in _records(tmp_path)}
    assert names == {f"{CMD}.{live}.json", f"{CMD}.{fresh}.json"}
    assert not _lock_path(tmp_path, dead).exists()
    assert _lock_path(tmp_path, live).exists()
    lc.end_attempt(tmp_path, CMD, live, live_lock)


def test_filesystem_errors_are_never_fatal(tmp_path):
    """Detects: an unwritable data directory crashing a spawner or a worker."""
    blocker = tmp_path / "codex-workers"
    blocker.write_text("not a directory")
    assert lc.begin_attempt(tmp_path, CMD, now=1.0) is None
    lock = lc.mark_started(tmp_path, CMD, "abc", pid=1, now=1.0)
    lc.note_spawned(tmp_path, CMD, "abc", pid=1)
    lc.end_attempt(tmp_path, CMD, "abc", lock)
    assert not lc.in_progress(tmp_path, CMD, now=1.0, pid_alive=_Alive())


# ── the spawn sites and the worker entry points ─────────────────────────────

@pytest.fixture
def ns(tmp_path, monkeypatch):
    namespace = load_script()
    redirect_paths(namespace, monkeypatch, tmp_path)
    return namespace


def _app_dir():
    return pathlib.Path(sys.modules["_cctally_core"].APP_DIR)


def _spawn_spy(monkeypatch, *, ok=True):
    update = sys.modules["_cctally_update"]
    seen = []

    def spawn(command):
        records = [json.loads(p.read_text()) for p in _records(_app_dir(), command)]
        seen.append((command, dict(update.detached_env_current()), records))
        return ok

    monkeypatch.setattr(update, "_spawn_detached", spawn)
    return seen


@pytest.mark.parametrize("command", [
    "_codex-replay-drain", "_codex-prune-drain", "_codex-inventory-bootstrap"])
def test_each_spawn_site_begins_an_attempt_and_passes_its_id(
        ns, monkeypatch, command):
    """Detects: a worker spawned with no attempt record (writeIo settles while
    it runs), or a record whose id the child never receives."""
    cache = sys.modules["_cctally_cache"]
    seen = _spawn_spy(monkeypatch)
    assert cache._request_codex_drain_worker(command) == "spawned"
    ((spawned, env, records),) = seen
    assert spawned == command
    assert [r["attempt"] for r in records] == [env[lc.ATTEMPT_ENV]]
    assert lc.in_progress(_app_dir(), command, now=records[0]["requestedAt"] + 1)
    # A throttled request spawns nothing and writes nothing.
    before = _records(_app_dir(), command)
    assert cache._request_codex_drain_worker(command) == "throttled"
    assert len(seen) == 1 and _records(_app_dir(), command) == before


def test_the_spawner_records_the_childs_pid_with_the_merged_environment(
        ns, monkeypatch):
    """Detects: a spawned child's pid never reaching its attempt record (a
    child that dies before marking its start then counts for the whole
    handoff, on every re-spawn), or a launch that drops the parent's
    environment while passing the attempt id."""
    cache = sys.modules["_cctally_cache"]
    update = sys.modules["_cctally_update"]
    seen = {}

    class FakePopen:
        # A pid that `kill(pid, 0)` answers for, whose handle says it exited:
        # only the retained launch handle can tell (#939 R8-04).
        def __init__(self, argv, **kwargs):
            seen["env"] = kwargs.get("env")
            self.pid = os.getpid()

        def poll(self):
            return 3

    monkeypatch.setattr(update.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("CCTALLY_939_PROBE", "kept")
    assert cache._request_codex_drain_worker("_codex-prune-drain") == "spawned"
    (record,) = [json.loads(p.read_text())
                 for p in _records(_app_dir(), "_codex-prune-drain")]
    assert record["spawnPid"] == os.getpid()
    assert not lc.in_progress(_app_dir(), "_codex-prune-drain",
                              now=record["requestedAt"] + 1)
    assert seen["env"][lc.ATTEMPT_ENV] == record["attempt"]
    assert seen["env"]["CCTALLY_939_PROBE"] == "kept"


def test_a_failed_spawn_ends_its_attempt(ns, monkeypatch):
    """Detects: a spawn that never happened holding admission for the whole
    handoff window."""
    cache = sys.modules["_cctally_cache"]
    _spawn_spy(monkeypatch, ok=False)
    assert cache._request_codex_drain_worker("_codex-prune-drain") == "failed"
    assert _records(_app_dir(), "_codex-prune-drain") == []


@pytest.mark.parametrize("command,entry,body", [
    ("_codex-replay-drain", "cmd_codex_replay_drain_internal",
     "_cmd_codex_replay_drain_body"),
    ("_codex-prune-drain", "cmd_codex_prune_drain_internal",
     "_cmd_codex_prune_drain_body"),
    ("_codex-inventory-bootstrap", "cmd_codex_inventory_bootstrap_internal",
     "_cmd_codex_inventory_bootstrap_body"),
])
@pytest.mark.parametrize("outcome", ["returns", "raises"])
def test_each_worker_marks_started_first_and_ends_its_attempt(
        ns, monkeypatch, command, entry, body, outcome):
    """Detects: a worker that does its work before marking itself started (a
    requested-then-expired record reads idle while it runs), or one that leaves
    its record behind on an exception."""
    cache = sys.modules["_cctally_cache"]
    attempt = lc.begin_attempt(_app_dir(), command, now=1.0)
    monkeypatch.setenv(lc.ATTEMPT_ENV, attempt)
    observed = {}

    def fake_body(_args):
        (record,) = [json.loads(p.read_text())
                     for p in _records(_app_dir(), command)]
        observed["record"] = record
        observed["env"] = os.environ.get(lc.ATTEMPT_ENV)
        observed["locked"] = lc.in_progress(
            _app_dir(), command, now=1e12, pid_alive=_Alive())
        if outcome == "raises":
            raise RuntimeError("injected")
        return 0

    monkeypatch.setattr(cache, body, fake_body)
    if outcome == "raises":
        with pytest.raises(RuntimeError):
            getattr(cache, entry)(None)
    else:
        assert getattr(cache, entry)(None) == 0
    assert observed["record"]["pid"] == os.getpid()
    assert observed["record"]["startedAt"] is not None
    assert observed["env"] is None, "a grandchild must not inherit the attempt"
    assert observed["locked"], "the worker holds its lifetime lock while it runs"
    assert _records(_app_dir(), command) == []
    assert list((_app_dir() / "codex-workers").glob("*.lock")) == []
