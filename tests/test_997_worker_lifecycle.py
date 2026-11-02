"""#997 T-B1 — the detached rebuild workers' lifecycle protocol (spec D8, T8).

On 2026-10-22 the epoch-rebuild worker ran for hours and its only trace was
one `success` line: no pid, no start, no record of whether a worker died and a
replacement re-ran the completion. Each worker now leaves a run state (written
only by its flock holder) and a launch ledger (written only under its
admission flock), and a death is settled only by evidence: a finished run
state, or a process confirmed gone by identity — never elapsed time.

Kill cases run the worker body in a subprocess driver that loads the modules
with `tests/_script_loader.py`, patches the kill point to SIGKILL itself, and
leaves the next worker (in-process) to account for it.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS


REPO = pathlib.Path(__file__).resolve().parents[1]
STATE = {"epoch": "stats-epoch-rebuild.state.json",
         "heal": "stats-heal.state.json"}
LEDGER = {"epoch": "stats-epoch-rebuild.launches.json",
          "heal": "stats-heal.launches.json"}
LOG = {"epoch": "stats-epoch-rebuild.log",
       "heal": "stats-corruption-heal.log"}


@pytest.fixture
def rt(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.delenv("CCTALLY_REBUILD_LAUNCH_ID", raising=False)
    core = sys.modules["_cctally_core"]
    pathlib.Path(core.APP_DIR).mkdir(parents=True, exist_ok=True)
    children: list = []
    yield SimpleNamespace(
        ns=ns, core=core, db=sys.modules["_cctally_db"],
        st=sys.modules["_cctally_store"],
        update=sys.modules["_cctally_update"],
        progress=sys.modules.get("_lib_rebuild_progress")
        or __import__("_lib_rebuild_progress"),
        app=pathlib.Path(core.APP_DIR), tmp=tmp_path, children=children)
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=PRESENCE_BACKSTOP_SECONDS)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
    sys.modules["_lib_rebuild_progress"].uninstall()


def _state(rt, worker="epoch"):
    return json.loads((rt.app / STATE[worker]).read_text())


def _ledger(rt, worker="epoch"):
    return json.loads((rt.app / LEDGER[worker]).read_text())


def _log(rt, worker="epoch"):
    path = pathlib.Path(rt.core.LOG_DIR) / LOG[worker]
    return path.read_text() if path.exists() else ""


def _entry(ledger, launch_id):
    return next(e for e in ledger["entries"] if e["launchId"] == launch_id)


def _child(rt, code: str) -> subprocess.Popen:
    """A child process this test owns (reaped at teardown)."""
    proc = subprocess.Popen([sys.executable, "-c", code])
    rt.children.append(proc)
    return proc


def _wait_gone(proc):
    proc.wait(timeout=PRESENCE_BACKSTOP_SECONDS)


DRIVER = r'''
import json, os, pathlib, signal, sys, time
sys.path.insert(0, {tests!r})
from _script_loader import load_script_module
load_script_module()
import _cctally_core as core
import _cctally_db as db
import _cctally_store as st
import _lib_rebuild_progress as progress
APP = pathlib.Path(core.APP_DIR)
def die():
    os.kill(os.getpid(), signal.SIGKILL)
{body}
'''


def _driver(rt, body: str, *, wait: bool = True, launch_id=None):
    script = DRIVER.format(tests=str(REPO / "tests"),
                           body=textwrap.dedent(body))
    env = dict(os.environ)
    env.update({
        "HOME": str(rt.tmp), "CCTALLY_DATA_DIR": str(rt.app),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "CCTALLY_DISABLE_UPDATE_CHECK": "1",
    })
    env.pop("CCTALLY_REBUILD_LAUNCH_ID", None)
    if launch_id is not None:
        env["CCTALLY_REBUILD_LAUNCH_ID"] = launch_id
    proc = subprocess.Popen([sys.executable, "-c", script], env=env,
                            cwd=str(REPO), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    rt.children.append(proc)
    if not wait:
        return proc
    out, err = proc.communicate(timeout=PRESENCE_BACKSTOP_SECONDS * 4)
    return SimpleNamespace(returncode=proc.returncode, out=out.decode(),
                           err=err.decode())


def _worker(rt, worker="epoch"):
    if worker == "epoch":
        return rt.st._stats_epoch_rebuild_worker(None)
    return rt.st.cmd_stats_corruption_heal_internal(SimpleNamespace())


def _make_stale(rt, worker="epoch"):
    marker = (rt.st._stats_epoch_rebuild_marker_path() if worker == "epoch"
              else rt.st._stats_heal_marker_path())
    if marker.exists():
        stale = time.time() - 3600
        os.utime(marker, (stale, stale))


def _spawn_with(rt, monkeypatch, factory):
    """Route the rebuild spawner through ``factory(command, env)`` -> pid."""
    calls = []

    def spawn(command, *, env=None):
        calls.append((command, (env or {}).get("CCTALLY_REBUILD_LAUNCH_ID")))
        return factory(command, env)

    monkeypatch.setattr(rt.update, "_spawn_detached_with_pid", spawn)
    return calls


def _self_killing_child(rt):
    """A launch whose process dies at import time, before any start."""
    def factory(_command, _env):
        proc = _child(rt, "import os, signal; os.kill(os.getpid(), signal.SIGKILL)")
        _wait_gone(proc)
        return proc.pid
    return factory


def _sleeping_child(rt):
    def factory(_command, _env):
        return _child(rt, "import time; time.sleep(600)").pid
    return factory


def _dead_running_state(rt, worker="epoch", *, launch_id="L-dead",
                        run_id="run-dead", phase="completion:replay-all"):
    """A run state left `running` by a process that is gone."""
    proc = _child(rt, "pass")
    _wait_gone(proc)
    state = {"schemaVersion": 1, "run": {
        "runId": run_id, "launchId": launch_id, "pid": proc.pid,
        "birth": "Thu Jan  1 00:00:00 1970", "status": "running",
        "phase": phase, "progressAt": "2026-10-24T10:00:00Z",
        "startedAt": "2026-10-24T09:00:00Z", "outcome": None},
        "deaths": [], "previousUnreadable": None}
    (rt.app / STATE[worker]).write_text(json.dumps(state))
    ledger = {"schemaVersion": 1, "entries": [{
        "launchId": launch_id, "at": "2026-10-24T09:00:00Z", "pid": proc.pid,
        "birth": "Thu Jan  1 00:00:00 1970", "spawn": "ok",
        "ack": {"kind": "started", "runId": run_id,
                "at": "2026-10-24T09:00:00Z"},
        "outcome": None, "settledAt": None}],
        "counts": {}, "lastDeathAt": None}
    (rt.app / LEDGER[worker]).write_text(json.dumps(ledger))
    return launch_id, run_id


# --------------------------------------------------------------------------
# process identity
# --------------------------------------------------------------------------

def test_process_identity_birth_mismatch_and_zombie(rt, monkeypatch):
    st = rt.st
    proc = _child(rt, "import time; time.sleep(600)")
    alive = st.process_identity(proc.pid)
    assert alive["status"] == "alive" and alive["birth"]
    import _lib_worker_launches as wl
    assert wl.liveness({"pid": proc.pid, "birth": alive["birth"]},
                       {proc.pid: alive}) == "alive"
    assert wl.liveness({"pid": proc.pid, "birth": "Mon Jan  1 00:00:00 2001"},
                       {proc.pid: alive}) == "gone"
    proc.send_signal(signal.SIGKILL)
    deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
    while st.process_identity(proc.pid)["status"] != "gone":
        assert time.monotonic() < deadline, "an unreaped child never read as gone"
        time.sleep(0.05)
    # Still a zombie (not reaped): gone by its `Z` state.
    proc.wait(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert st.process_identity(proc.pid)["status"] == "gone"

    def broken(*_a, **_kw):
        raise OSError("no ps")
    monkeypatch.setattr(subprocess, "run", broken)
    assert st.process_identity(os.getpid()) == {"status": "unknown",
                                                "birth": None}


def test_zombie_child_is_gone(rt, monkeypatch):
    """An unreaped direct child reads as gone, so its launch settles."""
    st = rt.st
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    rt.children.append(proc)
    deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS
    while True:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(proc.pid)],
                             capture_output=True, text=True,
                             env={**os.environ, "LC_ALL": "C"})
        if "Z" in out.stdout:
            break
        assert time.monotonic() < deadline, "the child never became a zombie"
        time.sleep(0.05)
    assert st.process_identity(proc.pid)["status"] == "gone"
    _spawn_with(rt, monkeypatch, lambda _c, _e: proc.pid)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    launch_id = _ledger(rt)["entries"][0]["launchId"]
    assert _worker(rt) == "current"
    assert _entry(_ledger(rt), launch_id)["outcome"] == "died-pre-start"


# --------------------------------------------------------------------------
# launches and pre-start deaths
# --------------------------------------------------------------------------

def test_import_time_kill_is_pre_start_death_once(rt, monkeypatch):
    st = rt.st
    calls = _spawn_with(rt, monkeypatch, _self_killing_child(rt))
    assert st.defer_stats_epoch_rebuild() == "spawned"
    first_pending = st.read_epoch_marker()["firstPendingAt"]
    (launch_id,) = [c[1] for c in calls]
    entry = _entry(_ledger(rt), launch_id)
    assert entry["spawn"] == "ok" and entry["pid"] and entry["ack"] is None
    assert entry["outcome"] is None, "the spawner settled before any probe"

    # The next spawner settles it; the pending time survives the retry.
    _make_stale(rt)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    ledger = _ledger(rt)
    assert _entry(ledger, launch_id)["outcome"] == "died-pre-start"
    assert ledger["counts"]["died-pre-start"] == 1
    assert st.read_epoch_marker()["firstPendingAt"] == first_pending

    # A worker, and another, never count it again.
    assert _worker(rt) == "current"
    assert _worker(rt) == "current"
    ledger = _ledger(rt)
    assert ledger["counts"]["died-pre-start"] == 2  # the second launch too
    assert sum(1 for e in ledger["entries"]
               if e["outcome"] == "died-pre-start") == 2
    assert "phase=pre-start" in _log(rt)


def test_spawn_failure_is_failed_start(rt, monkeypatch):
    st = rt.st
    _spawn_with(rt, monkeypatch, lambda _c, _e: None)
    assert st.defer_stats_epoch_rebuild() == "failed"
    (entry,) = _ledger(rt)["entries"]
    assert entry["spawn"] == "failed" and entry["outcome"] == "spawn-failed"
    assert not st._stats_epoch_rebuild_marker_path().exists()
    # The heal spawner records its failed start the same way.
    assert st.complete_stats_corruption_heal("reserved") == "failed"
    (heal,) = _ledger(rt, "heal")["entries"]
    assert heal["spawn"] == "failed" and heal["outcome"] == "spawn-failed"


def test_spawner_killed_after_reservation_worker_fills(rt, monkeypatch):
    """The reservation is durable before the spawn; a surviving worker
    fills its own identity at acknowledgement."""
    st = rt.st
    now = rt.core.now_utc_iso()
    with_admission = st._heal_flock_bounded(
        st._stats_epoch_rebuild_admission_path(), 5.0)
    try:
        assert st._spawner_reserve("epoch", "L-reserved", now)
    finally:
        st._heal_release_flock(with_admission)
    (entry,) = _ledger(rt)["entries"]
    assert entry["spawn"] == "reserved" and entry["pid"] is None
    monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "L-reserved")
    assert _worker(rt) == "current"
    entry = _entry(_ledger(rt), "L-reserved")
    assert entry["pid"] == os.getpid() and entry["birth"]
    assert entry["spawn"] == "ok"
    assert entry["ack"]["kind"] == "started"
    assert entry["outcome"] == "finished:current"


def test_reserved_entry_child_died_stays_unknown(rt, monkeypatch):
    """Spawner killed after its reservation, child dead before its
    acknowledgement: unresolved with unknown liveness, never dropped and
    never an invented death."""
    st = rt.st
    fd = st._heal_flock_bounded(st._stats_epoch_rebuild_admission_path(), 5.0)
    try:
        st._spawner_reserve("epoch", "L-orphan", rt.core.now_utc_iso())
    finally:
        st._heal_release_flock(fd)
    for _ in range(3):
        assert _worker(rt) == "current"
    _spawn_with(rt, monkeypatch, lambda _c, _e: None)
    _make_stale(rt)
    st.defer_stats_epoch_rebuild()
    entry = _entry(_ledger(rt), "L-orphan")
    assert entry["outcome"] is None and entry["pid"] is None
    assert "died-pre-start" not in _ledger(rt)["counts"]


def _fill_capacity(rt, worker="epoch"):
    """100 interrupted reservations: unresolved, unknown liveness."""
    st = rt.st
    admission = (st._stats_epoch_rebuild_admission_path() if worker == "epoch"
                 else st._stats_heal_admission_path())
    fd = st._heal_flock_bounded(admission, 5.0)
    try:
        for i in range(100):
            assert st._spawner_reserve(worker, f"U{i}",
                                       rt.core.now_utc_iso()) == "reserved"
    finally:
        st._heal_release_flock(fd)


def test_launch_capacity_withholds_spawn(rt, monkeypatch):
    """With 100 unresolved entries a further request neither reserves nor
    spawns; the request stays pending (the epoch marker and its
    firstPendingAt survive) and nothing is dropped."""
    st = rt.st
    _fill_capacity(rt)
    calls = _spawn_with(rt, monkeypatch, _sleeping_child(rt))
    assert st.defer_stats_epoch_rebuild() == "pending"
    assert calls == []
    ledger = _ledger(rt)
    assert len(ledger["entries"]) == 100
    assert all(e["spawn"] == "reserved" for e in ledger["entries"])
    marker = st.read_epoch_marker()
    assert marker["present"] and marker["firstPendingAt"]
    first = marker["firstPendingAt"]
    _make_stale(rt)
    assert st.defer_stats_epoch_rebuild() == "pending"
    assert calls == [] and st.read_epoch_marker()["firstPendingAt"] == first
    # The heal spawner withholds the same way and leaves its request pending.
    _fill_capacity(rt, "heal")
    heal_calls = []
    monkeypatch.setattr(
        st, "_spawn_rebuild_worker",
        lambda command, launch_id: heal_calls.append(launch_id) or None)
    assert st.complete_stats_corruption_heal("reserved") == "pending"
    assert heal_calls == []
    assert len(_ledger(rt, "heal")["entries"]) == 100


def test_late_ack_of_older_reservation_reopens_admission(rt, monkeypatch):
    """An older reservation's late acknowledgement settles it; the next
    request is admitted again."""
    st = rt.st
    _fill_capacity(rt)
    calls = _spawn_with(rt, monkeypatch, _sleeping_child(rt))
    assert st.defer_stats_epoch_rebuild() == "pending" and calls == []
    # The child of reservation U0 finally runs while a worker owns the
    # rebuild: it loses to that owner and settles its own entry as `busy`.
    with _SyntheticOwner("epoch"):
        monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "U0")
        assert _in_thread(rt, "epoch", "U0") == "busy"
    assert _entry(_ledger(rt), "U0")["outcome"] == "busy"
    # The owner finished before releasing, so admission is open again.
    _make_stale(rt)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    assert len(calls) == 1
    # The 100 reservations, the owner's run and the new launch.
    assert len(_ledger(rt)["entries"]) == 102


def test_delayed_live_child_never_dead(rt, monkeypatch):
    st = rt.st
    calls = _spawn_with(rt, monkeypatch, _sleeping_child(rt))
    assert st.defer_stats_epoch_rebuild() == "spawned"
    (launch_id,) = [c[1] for c in calls]
    # Age the entry by years: elapsed time is no evidence.
    ledger = _ledger(rt)
    _entry(ledger, launch_id)["at"] = "2001-01-01T00:00:00Z"
    (rt.app / LEDGER["epoch"]).write_text(json.dumps(ledger))
    for _ in range(3):
        _make_stale(rt)
        st.defer_stats_epoch_rebuild()
        assert _worker(rt) == "current"
    assert _entry(_ledger(rt), launch_id)["outcome"] is None
    # Once it is confirmed gone, it settles.
    sleeper = rt.children[0]
    sleeper.kill()
    _wait_gone(sleeper)
    assert _worker(rt) == "current"
    assert _entry(_ledger(rt), launch_id)["outcome"] == "died-pre-start"


def test_delayed_busy_loser_unresolved_then_busy(rt, monkeypatch):
    st = rt.st
    calls = _spawn_with(rt, monkeypatch, _sleeping_child(rt))
    assert st.defer_stats_epoch_rebuild() == "spawned"
    (launch_id,) = [c[1] for c in calls]
    with _SyntheticOwner("epoch"):
        _make_stale(rt)
        assert st.defer_stats_epoch_rebuild() == "pending"
        assert _entry(_ledger(rt), launch_id)["outcome"] is None
        owner_state = (rt.app / STATE["epoch"]).read_bytes()
        monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", launch_id)
        assert _in_thread(rt, "epoch", "delayed loser") == "busy"
        assert (rt.app / STATE["epoch"]).read_bytes() == owner_state, \
            "a busy loser is not a run"
    entry = _entry(_ledger(rt), launch_id)
    assert entry["ack"]["kind"] == "busy" and entry["outcome"] == "busy"


def test_newer_pre_start_death_survives_older_winner(rt, monkeypatch):
    """Launch A, launch B dies before start, A wins and completes (removing
    the marker): B's death stays recorded."""
    st = rt.st
    pids = iter((os.getpid(),))
    calls = _spawn_with(rt, monkeypatch, lambda _c, _e: next(pids))
    assert st.defer_stats_epoch_rebuild() == "spawned"
    _spawn_with(rt, monkeypatch, _self_killing_child(rt))
    _make_stale(rt)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    ids = [e["launchId"] for e in _ledger(rt)["entries"]]
    a_id, b_id = calls[0][1], ids[1]
    monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", a_id)
    assert _worker(rt) == "current"
    assert not st._stats_epoch_rebuild_marker_path().exists()
    ledger = _ledger(rt)
    assert _entry(ledger, a_id)["outcome"] == "finished:current"
    assert _entry(ledger, b_id)["outcome"] == "died-pre-start"
    assert _worker(rt) == "current"
    assert _entry(_ledger(rt), b_id)["outcome"] == "died-pre-start"


def test_sustained_pre_start_failures_counted(rt, monkeypatch):
    """More than five launches over more than ten minutes, each killed at
    import: all counted, and the newest is the ledger's newest entry."""
    st = rt.st
    clock = {"t": 1_800_000_000}
    real_now = rt.core.now_utc_iso

    def fake_now(now_utc=None):
        if now_utc is not None:
            return real_now(now_utc)
        import datetime as dt
        return real_now(dt.datetime.fromtimestamp(clock["t"], dt.timezone.utc))

    monkeypatch.setattr(rt.core, "now_utc_iso", fake_now)
    _spawn_with(rt, monkeypatch, _self_killing_child(rt))
    launched = []
    for _ in range(7):
        _make_stale(rt)
        assert st.defer_stats_epoch_rebuild() == "spawned"
        launched.append(_ledger(rt)["entries"][-1]["launchId"])
        clock["t"] += 180
    assert _worker(rt) == "current"
    ledger = _ledger(rt)
    assert ledger["counts"]["died-pre-start"] == 7
    pre_start = [e["launchId"] for e in ledger["entries"]
                 if e["outcome"] == "died-pre-start"]
    assert pre_start[-1] == launched[-1]
    assert ledger["lastDeathAt"] is not None


# --------------------------------------------------------------------------
# run records
# --------------------------------------------------------------------------

def test_run_state_records_start_progress_and_finish(rt, monkeypatch):
    st, progress = rt.st, rt.progress
    seen = {}

    def body():
        progress.phase("rebuild:read", lines=0)
        seen["running"] = _state(rt)
        return "success"

    monkeypatch.setattr(st, "_stats_epoch_rebuild_body", body)
    assert _worker(rt) == "success"
    running = seen["running"]["run"]
    assert running["status"] == "running"
    assert running["phase"] == "rebuild:read"
    assert running["counters"] == {"lines": 0}
    assert running["pid"] == os.getpid() and running["birth"]
    assert running["binaryEpoch"] == rt.core.STATS_INDEX_EPOCH
    final = _state(rt)["run"]
    assert final["status"] == "finished" and final["outcome"] == "success"
    assert final["peakResidentBytes"] > 0
    assert final["wallSeconds"] is not None
    assert (rt.app / STATE["epoch"]).stat().st_mode & 0o777 == 0o600
    ledger = _ledger(rt)
    (entry,) = ledger["entries"]
    assert entry["outcome"] == "finished:success"
    assert entry["ack"]["runId"] == final["runId"]


def test_finished_run_not_death_after_next_start(rt):
    assert _worker(rt) == "current"
    first = _state(rt)["run"]
    assert _worker(rt) == "current"
    state, ledger = _state(rt), _ledger(rt)
    assert state["deaths"] == []
    assert _entry(ledger, first["launchId"])["outcome"] == "finished:current"
    assert not any(k.startswith("died") for k in ledger["counts"])


def test_terminal_outcome_transferred_from_state(rt, monkeypatch):
    """The finishing ledger write fails: the next run transfers the outcome
    from the state, with no invented death."""
    st, db = rt.st, rt.db
    real = db._atomic_write_private_json
    fail = {"on": False}

    def flaky(path, payload):
        if fail["on"] and pathlib.Path(path).name == LEDGER["epoch"]:
            raise OSError("ledger write failed")
        return real(path, payload)

    def body():
        fail["on"] = True
        return "success"

    monkeypatch.setattr(db, "_atomic_write_private_json", flaky)
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body", body)
    assert _worker(rt) == "success"
    first = _state(rt)["run"]
    assert _entry(_ledger(rt), first["launchId"])["outcome"] is None
    fail["on"] = False
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body", lambda: "current")
    assert _worker(rt) == "current"
    assert _entry(_ledger(rt), first["launchId"])["outcome"] == \
        "finished:success"
    assert _state(rt)["deaths"] == []


def test_state_write_failure_does_no_work(rt, monkeypatch):
    st, db = rt.st, rt.db
    real = db._atomic_write_private_json

    def failing(path, payload):
        if pathlib.Path(path).name == STATE["epoch"]:
            raise OSError("state write failed")
        return real(path, payload)

    monkeypatch.setattr(db, "_atomic_write_private_json", failing)
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body",
                        lambda: pytest.fail("work ran without a recorded run"))
    rt.st._stats_epoch_rebuild_marker_path().write_text("")
    assert _worker(rt) == "error"
    assert "result=error stage=state" in _log(rt)
    (entry,) = _ledger(rt)["entries"]
    assert entry["outcome"] == "finished:error"
    assert st._stats_epoch_rebuild_marker_path().exists()


def test_ledger_write_failure_does_no_work(rt, monkeypatch):
    st, db = rt.st, rt.db
    real = db._atomic_write_private_json

    def failing(path, payload):
        if pathlib.Path(path).name == LEDGER["epoch"]:
            raise OSError("ledger write failed")
        return real(path, payload)

    monkeypatch.setattr(db, "_atomic_write_private_json", failing)
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body",
                        lambda: pytest.fail("work ran without a recorded run"))
    rt.st._stats_epoch_rebuild_marker_path().write_text("")
    assert _worker(rt) == "error"
    assert "result=error stage=ledger" in _log(rt)
    assert not (rt.app / STATE["epoch"]).exists()
    assert st._stats_epoch_rebuild_marker_path().exists()


# --------------------------------------------------------------------------
# kills
# --------------------------------------------------------------------------

def test_kill_inside_replay_recorded_by_next_worker(rt):
    driver = _driver(rt, """
        def body():
            progress.phase("completion:replay-all", inputs=0)
            time.sleep(600)
        st._stats_epoch_rebuild_body = body
        st._stats_epoch_rebuild_worker(None)
    """, wait=False)
    deadline = time.monotonic() + PRESENCE_BACKSTOP_SECONDS * 4
    while True:
        try:
            run = _state(rt)["run"]
        except (OSError, ValueError):
            run = None
        if run and run.get("phase") == "completion:replay-all":
            break
        assert driver.poll() is None, driver.stderr.read().decode()
        assert time.monotonic() < deadline, "the driver never reached replay"
        time.sleep(0.05)
    driver.send_signal(signal.SIGKILL)
    driver.wait(timeout=PRESENCE_BACKSTOP_SECONDS)
    dead_run = run
    assert _worker(rt) == "current"
    state = _state(rt)
    (death,) = state["deaths"]
    assert death["runId"] == dead_run["runId"]
    assert death["lastPhase"] == "completion:replay-all"
    assert f"result=died run={dead_run['runId']}" in _log(rt)
    assert _entry(_ledger(rt), dead_run["launchId"])["outcome"] == "died-in-run"
    assert _worker(rt) == "current"
    assert len(_state(rt)["deaths"]) == 1
    assert _ledger(rt)["counts"]["died-in-run"] == 1


def test_kill_during_start_write(rt):
    assert _worker(rt) == "current"
    before = (rt.app / STATE["epoch"]).read_bytes()
    result = _driver(rt, """
        real = db.os.replace
        def replace(src, dst):
            if str(dst).endswith("stats-epoch-rebuild.state.json"):
                die()
            return real(src, dst)
        db.os.replace = replace
        st._stats_epoch_rebuild_worker(None)
    """)
    assert result.returncode == -signal.SIGKILL, result.err
    assert (rt.app / STATE["epoch"]).read_bytes() == before
    acked = [e for e in _ledger(rt)["entries"]
             if e["outcome"] is None and e["ack"]]
    assert len(acked) == 1, "the killed worker acknowledged before its start"
    assert _worker(rt) == "current"
    assert _entry(_ledger(rt), acked[0]["launchId"])["outcome"] == \
        "died-in-run"
    assert _state(rt)["deaths"] == []


def test_kill_during_progress_write(rt):
    result = _driver(rt, """
        real = db._atomic_write_private_json
        def write(path, payload):
            run = payload.get("run") if isinstance(payload, dict) else None
            if run and run.get("phase") == "rebuild:read":
                die()
            return real(path, payload)
        db._atomic_write_private_json = write
        def body():
            progress.phase("rebuild:read", lines=0)
            return "success"
        st._stats_epoch_rebuild_body = body
        st._stats_epoch_rebuild_worker(None)
    """)
    assert result.returncode == -signal.SIGKILL, result.err
    killed = _state(rt)["run"]
    assert killed["status"] == "running" and killed["phase"] == "starting"
    assert _worker(rt) == "current"
    (death,) = _state(rt)["deaths"]
    assert death["runId"] == killed["runId"] and death["lastPhase"] == "starting"


def test_kill_during_reconciliation_is_idempotent(rt):
    launch_id, run_id = _dead_running_state(rt)
    # First kill: at the reconciling ledger write (nothing persisted).
    first = _driver(rt, """
        real = db.os.replace
        def replace(src, dst):
            if str(dst).endswith("stats-epoch-rebuild.launches.json"):
                die()
            return real(src, dst)
        db.os.replace = replace
        st._stats_epoch_rebuild_worker(None)
    """)
    assert first.returncode == -signal.SIGKILL, first.err
    # Second kill: after the ledger settled the death, at the state write.
    second = _driver(rt, """
        real = db.os.replace
        def replace(src, dst):
            if str(dst).endswith("stats-epoch-rebuild.state.json"):
                die()
            return real(src, dst)
        db.os.replace = replace
        st._stats_epoch_rebuild_worker(None)
    """)
    assert second.returncode == -signal.SIGKILL, second.err
    assert _entry(_ledger(rt), launch_id)["outcome"] == "died-in-run"
    assert _state(rt)["run"]["runId"] == run_id, "the slot was not replaced"
    assert _worker(rt) == "current"
    state, ledger = _state(rt), _ledger(rt)
    assert [d["launchId"] for d in state["deaths"]] == [launch_id]
    died = [e for e in ledger["entries"] if e["outcome"] == "died-in-run"]
    # The original run, plus the second driver (it acknowledged, then died).
    assert ledger["counts"]["died-in-run"] == len(died) == 2
    assert _log(rt).count(f"result=died run={run_id}") == 1


def test_kill_during_unreadable_archive(rt):
    garbage = b"{not json at all"
    (rt.app / STATE["epoch"]).write_bytes(garbage)
    result = _driver(rt, """
        real = st._archive_unreadable_record
        def archive(path, raw):
            out = real(path, raw)
            die()
        st._archive_unreadable_record = archive
        st._stats_epoch_rebuild_worker(None)
    """)
    assert result.returncode == -signal.SIGKILL, result.err
    assert (rt.app / STATE["epoch"]).read_bytes() == garbage
    archives = sorted(rt.app.glob(STATE["epoch"] + ".unreadable-*"))
    assert len(archives) == 1 and archives[0].read_bytes() == garbage
    assert _worker(rt) == "current"
    state = _state(rt)
    assert state["run"]["status"] == "finished"
    archived = pathlib.Path(state["previousUnreadable"]["archive"])
    assert archived.read_bytes() == garbage
    assert archived.stat().st_mode & 0o777 == 0o600


def test_protected_entry_survives_pruning_death_once(rt, monkeypatch):
    """A worker killed after settling its predecessor's death but before
    publishing its own state, then more than ten settled launches: the
    death is still counted exactly once when the next worker starts."""
    st = rt.st
    launch_id, run_id = _dead_running_state(rt)
    killed = _driver(rt, """
        real = db.os.replace
        def replace(src, dst):
            if str(dst).endswith("stats-epoch-rebuild.state.json"):
                die()
            return real(src, dst)
        db.os.replace = replace
        st._stats_epoch_rebuild_worker(None)
    """)
    assert killed.returncode == -signal.SIGKILL, killed.err
    assert _entry(_ledger(rt), launch_id)["outcome"] == "died-in-run"
    _spawn_with(rt, monkeypatch, lambda _c, _e: None)
    for _ in range(12):
        _make_stale(rt)
        assert st.defer_stats_epoch_rebuild() == "failed"
    assert any(e["launchId"] == launch_id for e in _ledger(rt)["entries"])
    assert _worker(rt) == "current"
    state, ledger = _state(rt), _ledger(rt)
    assert [d["launchId"] for d in state["deaths"]] == [launch_id]
    # The predecessor and the killed reconciler, each once.
    assert ledger["counts"]["died-in-run"] == 2
    assert ledger["counts"]["spawn-failed"] == 12


@pytest.mark.parametrize("how", ["killed", "state-write-fails"])
def test_prior_run_entry_protected_until_state_write(rt, monkeypatch, how):
    """Spec D8: until a starting worker's state write lands, the state still
    names the PRIOR run, so the prior run's entry must survive the starting
    worker's own pruning. Twelve settled launches accumulate BEFORE worker B
    writes its ledger (the spawners keep the prior run plus nine of them), and
    a launch that died before starting is left for B to settle, so B's own
    write holds eleven settled entries; B is then killed at, or fails, its
    state write; worker C still counts the prior run's death exactly once."""
    st, db = rt.st, rt.db
    launch_id, _run_id = _dead_running_state(rt)
    _spawn_with(rt, monkeypatch, lambda _c, _e: None)
    for _ in range(12):
        _make_stale(rt)
        assert st.defer_stats_epoch_rebuild() == "failed"
    _spawn_with(rt, monkeypatch, _self_killing_child(rt))
    _make_stale(rt)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    ledger = _ledger(rt)
    assert _entry(ledger, launch_id)["outcome"] == "died-in-run"
    assert ledger["counts"]["died-in-run"] == 1
    assert len([e for e in ledger["entries"] if e["outcome"]]) == 10
    if how == "killed":
        killed = _driver(rt, """
            real = db.os.replace
            def replace(src, dst):
                if str(dst).endswith("stats-epoch-rebuild.state.json"):
                    die()
                return real(src, dst)
            db.os.replace = replace
            st._stats_epoch_rebuild_worker(None)
        """)
        assert killed.returncode == -signal.SIGKILL, killed.err
    else:
        real = db._atomic_write_private_json

        def failing(path, payload):
            if pathlib.Path(path).name == STATE["epoch"]:
                raise OSError("state write failed")
            return real(path, payload)

        monkeypatch.setattr(db, "_atomic_write_private_json", failing)
        monkeypatch.setattr(st, "_stats_epoch_rebuild_body",
                            lambda: pytest.fail("work ran without a run"))
        assert _worker(rt) == "error"
        # Restore by hand: `monkeypatch.undo()` would also undo the fixture's
        # path redirection.
        monkeypatch.setattr(db, "_atomic_write_private_json", real)
    assert _state(rt)["run"]["launchId"] == launch_id, \
        "B never published its state"
    assert any(e["launchId"] == launch_id for e in _ledger(rt)["entries"]), \
        "B's own pruning dropped the entry its state slot still names"
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body", lambda: "current")
    assert _worker(rt) == "current"
    state, ledger = _state(rt), _ledger(rt)
    assert [d["launchId"] for d in state["deaths"]] == [launch_id]
    assert ledger["counts"]["died-pre-start"] == 1
    if how == "killed":
        # The prior run and the killed worker B, each once.
        assert ledger["counts"]["died-in-run"] == 2
    else:
        assert ledger["counts"]["died-in-run"] == 1


@pytest.mark.parametrize("stage", ["admission", "ledger"])
def test_busy_loser_that_cannot_record_is_logged(rt, monkeypatch, stage):
    """A loser to a running worker that cannot take the admission flock, or
    whose ledger write fails, leaves no acknowledgement (its entry will read
    as a pre-start death); the worker log says what really happened."""
    st, db = rt.st, rt.db
    real = db._atomic_write_private_json
    owner = _SyntheticOwner("epoch").start()
    admission = None
    try:
        if stage == "admission":
            monkeypatch.setattr(st, "_WORKER_ADMISSION_WAIT_S", 0.05)
            admission = os.open(st._worker_admission_path("epoch"),
                                os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            def failing(path, payload):
                if pathlib.Path(path).name == LEDGER["epoch"]:
                    raise OSError("ledger write failed")
                return real(path, payload)

            monkeypatch.setattr(db, "_atomic_write_private_json", failing)
        monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "L-busy")
        assert _in_thread(rt, "epoch", "busy loser") == "busy"
    finally:
        if admission is not None:
            fcntl.flock(admission, fcntl.LOCK_UN)
            os.close(admission)
        # Restore by hand so the owner can record its finish.
        monkeypatch.setattr(db, "_atomic_write_private_json", real)
        owner.finish()
    assert (f"result=busy-unrecorded launch=L-busy pid={os.getpid()} "
            f"stage={stage}") in _log(rt)
    ledger = rt.st.read_launch_ledger("epoch")["ledger"]
    assert not any(e["launchId"] == "L-busy"
                   for e in (ledger or {}).get("entries", []))


def test_malformed_death_record_does_not_stop_the_run(rt, monkeypatch):
    """A death record whose ``launchId`` is not a string (corrupt, but the
    state still validates) is skipped for deduplication; the run goes on."""
    state = {"schemaVersion": 1, "run": {
        "runId": "R0", "launchId": "L0", "pid": 1, "birth": None,
        "status": "finished", "outcome": "current",
        "startedAt": "2026-10-24T09:00:00Z"},
        "deaths": [{"runId": "Rx", "launchId": ["L0"],
                    "detectedAt": "2026-10-24T09:30:00Z"}],
        "previousUnreadable": None}
    (rt.app / STATE["epoch"]).write_text(json.dumps(state))
    monkeypatch.setattr(rt.st, "_stats_epoch_rebuild_body", lambda: "current")
    assert _worker(rt) == "current"
    after = _state(rt)
    assert after["run"]["status"] == "finished"
    assert after["run"]["launchId"] != "L0"
    assert after["deaths"] == state["deaths"]


@pytest.mark.parametrize("bad_id", [["L0"], 7], ids=["list", "int"])
def test_non_string_run_launch_id_is_unreadable_state(rt, monkeypatch,
                                                      bad_id):
    """A ``run.launchId`` that is neither a string nor ``None`` makes the
    state unreadable: it is archived, and the run starts and records
    normally. (A list id once wedged every later launch: the prior run's
    entry was appended under it, and ``prune`` raised on the unhashable id
    before the state was ever rewritten; an int id made the next
    ``normalize`` reject the ledger.)"""
    _dead_running_state(rt, launch_id="L-seed")
    state = _state(rt)
    state["run"]["launchId"] = bad_id
    raw = json.dumps(state).encode()
    (rt.app / STATE["epoch"]).write_bytes(raw)
    monkeypatch.setattr(rt.st, "_stats_epoch_rebuild_body", lambda: "current")
    for _ in range(2):
        assert _worker(rt) == "current"
    after = _state(rt)
    assert after["run"]["status"] == "finished"
    assert after["run"]["outcome"] == "current"
    assert isinstance(after["run"]["launchId"], str)
    assert after["deaths"] == []
    archives = sorted(rt.app.glob(STATE["epoch"] + ".unreadable-*"))
    assert len(archives) == 1 and archives[0].read_bytes() == raw
    ledger = rt.st.read_launch_ledger("epoch")
    assert ledger["unreadable"] is False
    assert all(isinstance(e["launchId"], str)
               for e in ledger["ledger"]["entries"])
    (rt.app / STATE["epoch"]).write_bytes(raw)
    assert rt.st.read_worker_state("epoch") == {"state": None,
                                                "unreadable": True}


def test_launches_before_start_compares_instants(rt):
    """``launchesBeforeStart`` compares parsed UTC instants: ``Z`` and
    ``+00:00`` (and fractional seconds) are the same clock, and an
    unparsable value is never "before the start"."""
    count = rt.st._launches_before_start
    entries = [
        {"launchId": "L0", "at": "2026-10-24T10:59:59Z"},
        {"launchId": "L1", "at": "2026-10-24T11:00:00+00:00"},
        {"launchId": "L2", "at": "2026-10-24T11:00:00.500000Z"},
        {"launchId": "L3", "at": "2026-10-24T12:00:00+01:00"},
        {"launchId": "L4", "at": "garbage"},
        {"launchId": "L5", "at": None},
        {"launchId": "ME", "at": "2026-10-24T11:30:00Z"},
    ]
    assert count(entries, "ME", "2026-10-24T11:00:00Z") == 3
    assert count(entries, "ME", "2026-10-24T11:00:00+00:00") == 3
    assert count(entries, "ME", None) == 0
    assert count(entries, "ME", "not a time") == 0


def test_dir_fsync_failure_after_replace_no_invented_death(rt, monkeypatch):
    """The start state's replace took effect but its directory fsync raised:
    the run records `finished:error`; the next worker records no death."""
    st, db = rt.st, rt.db
    real_replace, real_fsync = db.os.replace, db._fsync_directory
    armed = {"state": False}

    def replace(src, dst):
        out = real_replace(src, dst)
        if str(dst).endswith(STATE["epoch"]) and armed.get("once") is None:
            armed["state"] = True
            armed["once"] = True
        return out

    def fsync(path):
        if armed["state"]:
            armed["state"] = False
            raise OSError("directory fsync failed")
        return real_fsync(path)

    monkeypatch.setattr(db.os, "replace", replace)
    monkeypatch.setattr(db, "_fsync_directory", fsync)
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body",
                        lambda: pytest.fail("work ran after a failed start"))
    assert _worker(rt) == "error"
    stuck = _state(rt)["run"]
    assert stuck["status"] == "running", "the replace had taken effect"
    assert _entry(_ledger(rt), stuck["launchId"])["outcome"] == \
        "finished:error"
    # Restore by hand: `monkeypatch.undo()` would also undo the fixture's
    # path redirection.
    monkeypatch.setattr(db.os, "replace", real_replace)
    monkeypatch.setattr(db, "_fsync_directory", real_fsync)
    monkeypatch.setattr(st, "_stats_epoch_rebuild_body", lambda: "current")
    assert _worker(rt) == "current"
    state, ledger = _state(rt), _ledger(rt)
    assert state["deaths"] == []
    assert _entry(ledger, stuck["launchId"])["outcome"] == "finished:error"
    assert not any(k.startswith("died") for k in ledger["counts"])


# --------------------------------------------------------------------------
# heal worker
# --------------------------------------------------------------------------

def test_heal_no_request_reconciles_dead_predecessor(rt):
    launch_id, run_id = _dead_running_state(rt, "heal")
    assert _worker(rt, "heal") == 0
    state = _state(rt, "heal")
    assert state["run"]["outcome"] == "no-request"
    assert state["run"]["finalization"] == "done"
    assert [d["runId"] for d in state["deaths"]] == [run_id]
    assert _entry(_ledger(rt, "heal"), launch_id)["outcome"] == "died-in-run"
    assert f"result=died run={run_id}" in _log(rt, "heal")


def test_heal_finalization_recorded_separately(rt, monkeypatch):
    st = rt.st
    (rt.app / "stats-corruption-heal.pending").write_text(json.dumps(
        {"schemaVersion": 1, "healId": "h1", "probeKind": "readability"}))
    monkeypatch.setattr(st, "_run_stats_corruption_heal",
                        lambda request: "declined-readable")
    monkeypatch.setattr(st, "_record_stats_heal_outcome", lambda *a, **k: None)
    monkeypatch.setattr(st, "_finalize_stats_heal_request", lambda _id: False)
    assert _worker(rt, "heal") == 0
    run = _state(rt, "heal")["run"]
    assert run["outcome"] == "declined-readable"
    assert run["finalization"] == "pending"

    def declined(request):
        st._last_heal_decline_reason = "integrity_check_unavailable"
        return "declined-unconfirmed"

    monkeypatch.setattr(st, "_run_stats_corruption_heal", declined)
    monkeypatch.setattr(st, "_finalize_stats_heal_request", lambda _id: True)
    assert _worker(rt, "heal") == 0
    run = _state(rt, "heal")["run"]
    assert run["outcome"] == "declined-unconfirmed"
    assert run["finalization"] == "done"
    assert run["declineReason"] == "integrity_check_unavailable"


def test_heal_first_unresolved_survives_stale_retries(rt):
    st = rt.st
    first = {"healId": "owner", "probeKind": "readability",
             "detectedAtUtc": "2026-10-24T08:00:00Z"}
    status, elected = st._reserve_stats_corruption_heal(first)
    assert status == "reserved"
    assert elected["firstUnresolvedAt"] == "2026-10-24T08:00:00Z"
    for n in range(3):
        _make_stale(rt, "heal")
        status, elected = st._reserve_stats_corruption_heal({
            "healId": f"retry-{n}", "probeKind": "readability",
            "detectedAtUtc": f"2026-10-24T09:0{n}:00Z"})
        assert status == "reserved", status
        assert elected["healId"] == f"retry-{n}"
        assert elected["firstUnresolvedAt"] == "2026-10-24T08:00:00Z"
    snapshot = st.read_stats_heal_request_snapshot()
    assert snapshot["firstUnresolvedAt"] == "2026-10-24T08:00:00Z"


# --------------------------------------------------------------------------
# markers and liveness corner cases
# --------------------------------------------------------------------------

def test_legacy_empty_marker_first_pending_at(rt, monkeypatch):
    st = rt.st
    marker = st._stats_epoch_rebuild_marker_path()
    marker.write_text("")
    then = 1_790_000_000
    os.utime(marker, (then, then))
    import datetime as dt
    expected = rt.core.now_utc_iso(
        dt.datetime.fromtimestamp(then, dt.timezone.utc))
    assert st.read_epoch_marker() == {"present": True,
                                      "firstPendingAt": expected,
                                      "legacy": True}
    _spawn_with(rt, monkeypatch, lambda _c, _e: os.getpid())
    assert st.defer_stats_epoch_rebuild() == "spawned"
    converted = st.read_epoch_marker()
    assert converted == {"present": True, "firstPendingAt": expected,
                         "legacy": False}
    # The conversion rewrites the marker, so its mtime (the retry throttle)
    # moves past the legacy instant pinned above.
    assert marker.stat().st_mtime > then, "mtime is the throttle"


def test_null_birth_live_pid_unresolved(rt, monkeypatch):
    st = rt.st
    sleeper = _child(rt, "import time; time.sleep(600)")
    real = st.process_identity
    monkeypatch.setattr(
        st, "process_identity",
        lambda pid: ({"status": "alive", "birth": None}
                     if pid == sleeper.pid else real(pid)))
    _spawn_with(rt, monkeypatch, lambda _c, _e: sleeper.pid)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    (entry,) = _ledger(rt)["entries"]
    assert entry["pid"] == sleeper.pid and entry["birth"] is None
    for _ in range(2):
        assert _worker(rt) == "current"
    assert _entry(_ledger(rt), entry["launchId"])["outcome"] is None


# --------------------------------------------------------------------------
# #1012: ownership is decided from records; only workers take the claim lock
# --------------------------------------------------------------------------
#
# Every overlap test below bounds each wait, re-raises its threads'
# exceptions after joining them, releases its barriers in `finally`, and
# cannot hang against a store that refuses at its first try.

WORKER_LOCK = {"epoch": "stats-epoch-rebuild.worker.lock",
               "heal": "stats-corruption-heal.worker.lock"}
CLAIM_LOCK = {"epoch": "stats-epoch-rebuild.claim.lock",
              "heal": "stats-corruption-heal.claim.lock"}
PROBE = {"epoch": "_stats_epoch_rebuild_worker_active",
         "heal": "_stats_heal_worker_active"}
FINISHED = {"epoch": "finished:current", "heal": "finished:no-request"}
REAL_FLOCK = fcntl.flock


def _until(predicate, what, timeout=PRESENCE_BACKSTOP_SECONDS):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


class _Run(threading.Thread):
    """A daemon thread that keeps its target's result or exception."""

    def __init__(self, fn, *, register=None, name=None):
        super().__init__(daemon=True, name=name)
        self._fn, self._register = fn, register
        self.result, self.error = None, None

    def run(self):
        if self._register is not None:
            self._register.append(threading.get_ident())
        try:
            self.result = self._fn()
        except BaseException as exc:  # re-raised by finish()
            self.error = exc

    def finish(self):
        if self.ident is None:
            return
        self.join(PRESENCE_BACKSTOP_SECONDS)
        assert not self.is_alive(), f"{self.name} did not terminate"
        if self.error is not None:
            raise self.error


def _flock_barrier(rt, monkeypatch, worker, *, holder_ids, claimant_ids=None,
                   park=None):
    """Instrument ``fcntl.flock`` on this worker's lock and claim lock
    (matched by inode; the claim path is named, so this works before the
    claim lock exists).

    It counts calls per (thread, lock), records refusals, sets ``tried`` on a
    claimant thread's first attempt on either lock (success or refusal; the
    main thread unless ``claimant_ids`` says otherwise), and parks a thread
    in ``holder_ids`` that has just acquired the worker lock in ``park()``
    (default: until ``tried``)."""
    paths = {"worker": rt.app / WORKER_LOCK[worker],
             "claim": rt.app / CLAIM_LOCK[worker]}
    claimants = ([threading.get_ident()] if claimant_ids is None
                 else claimant_ids)
    bar = SimpleNamespace(holds=threading.Event(), tried=threading.Event(),
                          calls={}, refusals=[])
    guard = threading.Lock()

    def which(fd):
        try:  # fcntl.flock also accepts objects with fileno()
            mine = os.fstat(fd if isinstance(fd, int) else fd.fileno())
        except (OSError, TypeError, ValueError, AttributeError):
            return None
        for name, path in paths.items():
            try:
                found = os.stat(path)
            except OSError:
                continue
            if (found.st_dev, found.st_ino) == (mine.st_dev, mine.st_ino):
                return name
        return None

    def flock(fd, op):
        name = which(fd)
        if name is None:
            return REAL_FLOCK(fd, op)
        me = threading.get_ident()
        taking = bool(op & fcntl.LOCK_EX)
        with guard:
            bar.calls[(me, name)] = bar.calls.get((me, name), 0) + 1
        try:
            out = REAL_FLOCK(fd, op)
        except OSError:
            if taking:
                with guard:
                    bar.refusals.append((me, name))
                if me in claimants:
                    bar.tried.set()
            raise
        if taking:
            if me in claimants:
                bar.tried.set()
            elif name == "worker" and me in holder_ids:
                bar.holds.set()
                (park or (lambda: bar.tried.wait(PRESENCE_BACKSTOP_SECONDS)))()
        return out

    monkeypatch.setattr(fcntl, "flock", flock)
    return bar


def _calls_by(bar, ident) -> dict:
    return {name: n for (tid, name), n in bar.calls.items() if tid == ident}


def _refusals_by(bar, ident, name) -> int:
    return sum(1 for tid, lock in list(bar.refusals)
               if tid == ident and lock == name)


def _run_overlap(rt, monkeypatch, worker, observer_fn):
    """An observer runs ``observer_fn``; if it takes the worker lock it parks
    there until the main thread's claim has been attempted. The main thread
    waits for that hold or the observer's completion, then runs the worker
    (launch ``L-start``)."""
    holder_ids: list = []
    bar = _flock_barrier(rt, monkeypatch, worker, holder_ids=holder_ids)
    observer = _Run(observer_fn, register=holder_ids, name="observer")
    observer.start()
    try:
        _until(lambda: bar.holds.is_set() or not observer.is_alive(),
               "the observer neither held the worker lock nor finished")
        monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "L-start")
        outcome = _worker(rt, worker)
    finally:
        bar.tried.set()
        observer.finish()
    return SimpleNamespace(outcome=outcome, observer_result=observer.result,
                           observer_held=bar.holds.is_set(),
                           calls=_calls_by(bar, holder_ids[0]))


@pytest.mark.parametrize("worker", ["epoch", "heal"])
def test_probe_overlap_never_makes_a_starting_worker_busy(rt, monkeypatch,
                                                          worker):
    """#1012 T1. Detects the status-line activity probe making a starting
    rebuild worker exit `busy` (and record `busy` in the ledger that doctor
    and db status read), which delayed the pending rebuild. The probe now
    decides from records and takes neither lock."""
    probe = getattr(rt.st, PROBE[worker])
    run = _run_overlap(rt, monkeypatch, worker, probe)
    entry = _entry(_ledger(rt, worker), "L-start")
    assert run.outcome != "busy", "the probe made the starting worker busy"
    assert entry["outcome"] == FINISHED[worker], entry
    assert run.observer_result is False
    assert run.calls == {}, f"the probe took a rebuild lock: {run.calls}"


class _FakeStoreTime:
    """The store's ``time`` for one test: every ``sleep`` advances
    ``monotonic`` by 60 s (and yields briefly), so any deadline or retry
    bound an implementation hid would fire within the test."""

    STEP = 60.0

    def __init__(self):
        self.offset = 0.0
        self._guard = threading.Lock()

    def monotonic(self):
        return time.monotonic() + self.offset

    def sleep(self, _seconds):
        with self._guard:
            self.offset += self.STEP
        time.sleep(0.001)

    def __getattr__(self, name):
        return getattr(time, name)


def _record_launch_ids(rt, monkeypatch) -> dict:
    """Thread ident -> the launch id that thread's worker consumed (each
    worker mints its own: the launch environment variable is unset)."""
    ids: dict = {}
    real = rt.st._take_launch_id

    def take():
        launch_id = real()
        ids[threading.get_ident()] = launch_id
        return launch_id

    monkeypatch.setattr(rt.st, "_take_launch_id", take)
    return ids


def _parked_body(rt, monkeypatch, worker):
    """Replace the worker body (entered after ``begin()`` made the run's
    records durable) with one that parks until ``release``."""
    body = SimpleNamespace(entered=threading.Event(),
                           release=threading.Event(), runs=[])

    def park():
        body.runs.append(threading.get_ident())
        body.entered.set()
        assert body.release.wait(PRESENCE_BACKSTOP_SECONDS * 4)

    if worker == "epoch":
        def epoch_body():
            park()
            return "current"
        monkeypatch.setattr(rt.st, "_stats_epoch_rebuild_body", epoch_body)
    else:
        def heal_body(result):
            park()
            result.update(outcome="no-request", finalization="done")
            return False
        monkeypatch.setattr(rt.st, "_stats_heal_worker_body", heal_body)
    return body


@pytest.mark.parametrize("observer", ["raw", "worker-blocks"])
@pytest.mark.parametrize("worker", ["epoch", "heal"])
def test_observer_overlap_has_no_deadline_and_hands_off(rt, monkeypatch,
                                                        worker, observer):
    """#1012 T2. Detects a non-owner hold of the worker lock (the statusline's
    lock observers, `stats_rebuild_worker_blocks`, an older binary's probe)
    defeating the worker that claimed the rebuild, which then exits `busy`
    and leaves the pending rebuild unowned. Worker A must outwait the hold
    with no deadline (the store's clock runs past an hour of sleeps), and a
    second worker B that loses the claim must hand off, also with no
    deadline (another hour of sleeps): not run, not return, until A has
    visibly started, then record `busy`."""
    st = rt.st
    clock = _FakeStoreTime()
    monkeypatch.setattr(st, "time", clock)
    ids = _record_launch_ids(rt, monkeypatch)
    body = _parked_body(rt, monkeypatch, worker)
    holder_ids: list = []
    claimant_ids: list = []
    observer_release = threading.Event()
    bar = _flock_barrier(
        rt, monkeypatch, worker, holder_ids=holder_ids,
        claimant_ids=claimant_ids,
        park=lambda: observer_release.wait(PRESENCE_BACKSTOP_SECONDS * 4))
    a = _Run(lambda: _worker(rt, worker), register=claimant_ids, name="A")
    b = _Run(lambda: _worker(rt, worker), register=claimant_ids, name="B")
    watcher = None
    held = None
    try:
        if observer == "raw":
            held = os.open(rt.app / WORKER_LOCK[worker],
                           os.O_WRONLY | os.O_CREAT, 0o600)
            REAL_FLOCK(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            (rt.app / WORKER_LOCK[worker]).touch()
            watcher = _Run(st.stats_rebuild_worker_blocks,
                           register=holder_ids, name="observer")
            watcher.start()
            _until(bar.holds.is_set, "the observer never held the lock")
        a.start()
        _until(lambda: clock.offset > 3600 or not a.is_alive(),
               "worker A neither waited an hour of store sleeps nor returned")

        def a_entry_outcome():
            launch = ids.get(a.ident)
            ledger = st.read_launch_ledger(worker)["ledger"] or {}
            entry = _rt_find(ledger, launch)
            return (entry or {}).get("outcome")

        assert a.is_alive(), (
            f"worker A gave up while a non-owner held the worker lock "
            f"(returned {a.result!r}, ledger {a_entry_outcome()!r})")
        b.start()
        _until(lambda: _refusals_by(bar, b.ident, "claim") >= 2
               or not b.is_alive(),
               "worker B never retried the claim")
        assert b.is_alive(), (
            f"worker B returned {b.result!r} before A had started")
        # B's hand-off has no deadline either: it outwaits another hour of
        # store sleeps (B's own retries included) without returning or
        # recording `busy`.
        handoff_from = clock.offset
        _until(lambda: clock.offset > handoff_from + 3600
               or not b.is_alive(),
               "worker B neither waited an hour of store sleeps nor returned")
        retried = _refusals_by(bar, b.ident, "claim")
        _until(lambda: _refusals_by(bar, b.ident, "claim") >= retried + 2
               or not b.is_alive(),
               "worker B stopped retrying the claim")
        assert b.is_alive(), (
            f"worker B gave up its hand-off after an hour of store sleeps "
            f"(returned {b.result!r})")
        ledger_now = st.read_launch_ledger(worker)["ledger"] or {}
        assert not [e for e in ledger_now.get("entries", ())
                    if e.get("outcome") == "busy"], ledger_now
        assert a.is_alive() and body.runs == []
        if held is not None:
            REAL_FLOCK(held, fcntl.LOCK_UN)
            os.close(held)
            held = None
        else:
            observer_release.set()
            watcher.finish()
        _until(lambda: body.entered.is_set() or not a.is_alive(),
               "worker A never entered its body after the release")
        assert body.entered.is_set(), f"worker A returned {a.result!r}"
        _until(lambda: not b.is_alive(),
               "worker B never acknowledged A's started run")
        b.finish()
        assert b.result == ("busy" if worker == "epoch" else 0)
        body.release.set()
        a.finish()
    finally:
        observer_release.set()
        body.release.set()
        if held is not None:
            REAL_FLOCK(held, fcntl.LOCK_UN)
            os.close(held)
        for thread in (a, b, watcher):
            if thread is not None:
                thread.finish()
    assert a.result == ("current" if worker == "epoch" else 0)
    assert body.runs == [a.ident], "the body ran other than exactly once"
    ledger = _ledger(rt, worker)
    a_entry, b_entry = _entry(ledger, ids[a.ident]), _entry(ledger, ids[b.ident])
    assert a_entry["outcome"] == FINISHED[worker], a_entry
    assert a_entry["ack"]["kind"] == "started"
    assert b_entry["outcome"] == "busy" and b_entry["ack"]["kind"] == "busy"
    if watcher is not None:
        assert watcher.result == ()


def _rt_find(ledger, launch_id):
    return next((e for e in ledger.get("entries", ())
                 if e.get("launchId") == launch_id), None)


_PROBED_BIRTH = object()


class _SyntheticOwner:
    """#1012: a running worker, modelled. It takes the claim lock and the
    worker lock with its own descriptors (or only the worker lock, as an
    older binary's worker would), appends a ``started`` entry with fresh ids
    to the existing ledger and writes a running state, through the store's
    own writers under the worker's admission flock. ``finish()`` writes the
    run finished and settles the entry before releasing (worker lock, then
    claim lock), so a later probe sees no owner. Also a context manager."""

    def __init__(self, worker, *, pid=None, birth=_PROBED_BIRTH,
                 hold_claim=True, hold_worker=True):
        import _cctally_store
        self.st = _cctally_store
        self.worker = worker
        self.pid = os.getpid() if pid is None else pid
        self.birth = (self.st.process_identity(self.pid).get("birth")
                      if birth is _PROBED_BIRTH else birth)
        self.hold_claim, self.hold_worker = hold_claim, hold_worker
        self.launch_id = f"L-owner-{os.urandom(4).hex()}"
        self.run_id = f"run-owner-{os.urandom(4).hex()}"
        self.state = None
        self._fds: list = []

    def _lock(self, name):
        path = pathlib.Path(self.st._cctally_core.APP_DIR) / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            REAL_FLOCK(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise
        self._fds.append(fd)

    def _under_admission(self, action):
        st = self.st
        fd = st._heal_flock_bounded(st._worker_admission_path(self.worker),
                                    PRESENCE_BACKSTOP_SECONDS)
        assert fd is not None, "the synthetic owner never got admission"
        try:
            action()
        finally:
            st._heal_release_flock(fd)

    def start(self):
        import _lib_worker_launches as wl

        st, worker = self.st, self.worker
        if self.hold_claim:
            self._lock(CLAIM_LOCK[worker])
        if self.hold_worker:
            self._lock(WORKER_LOCK[worker])
        now = st._cctally_core.now_utc_iso()

        def write():
            ledger = wl.acknowledge(
                st._load_ledger_for_update(worker), self.launch_id,
                "started", self.run_id, at=now, pid=self.pid,
                birth=self.birth)
            st._write_ledger(worker, ledger)
            prior = st.read_worker_state(worker)["state"] or {}
            run = {"runId": self.run_id, "launchId": self.launch_id,
                   "pid": self.pid, "birth": self.birth,
                   "status": "running", "phase": "starting",
                   "phaseStartedAt": now, "progressAt": now, "counters": {},
                   "outcome": None, "startedAt": now, "finishedAt": None}
            if worker == "heal":
                run["finalization"] = None
            self.state = {"schemaVersion": 1, "run": run,
                          "deaths": list(prior.get("deaths") or []),
                          "previousUnreadable": None}
            st._cctally_db._atomic_write_private_json(
                st._worker_state_path(worker), self.state)

        try:
            self._under_admission(write)
        except BaseException:
            self.release()
            raise
        return self

    def finish(self, outcome="current"):
        import _lib_worker_launches as wl

        st, worker = self.st, self.worker
        if self.state is not None:
            now = st._cctally_core.now_utc_iso()

            def write():
                self.state["run"].update(status="finished", outcome=outcome,
                                         finishedAt=now)
                if worker == "heal":
                    self.state["run"]["finalization"] = "done"
                st._cctally_db._atomic_write_private_json(
                    st._worker_state_path(worker), self.state)
                ledger = wl.settle(st._load_ledger_for_update(worker),
                                   self.launch_id, f"finished:{outcome}",
                                   at=now)
                st._write_ledger(worker, ledger)

            try:
                self._under_admission(write)
            finally:
                self.state = None
        self.release()

    def release(self):
        while self._fds:
            fd = self._fds.pop()  # worker lock first, then the claim lock
            try:
                REAL_FLOCK(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.finish()


def _running_records(rt, worker="epoch", *, pid, birth, run_id="run-own",
                     launch_id="L-own", ack_run_id=None, outcome=None,
                     with_entry=True):
    """A running run state and (optionally) its ledger entry, as fixtures."""
    at = "2026-10-31T06:00:00Z"
    state = {"schemaVersion": 1, "run": {
        "runId": run_id, "launchId": launch_id, "pid": pid, "birth": birth,
        "status": "running", "phase": "starting", "phaseStartedAt": at,
        "progressAt": at, "counters": {}, "residentBytes": 1,
        "peakResidentBytes": 1, "startedAt": at, "outcome": None},
        "deaths": [], "previousUnreadable": None}
    (rt.app / STATE[worker]).write_text(json.dumps(state))
    entries = []
    if with_entry:
        entries.append({
            "launchId": launch_id, "at": at, "pid": pid, "birth": birth,
            "spawn": "ok", "ack": {"kind": "started",
                                   "runId": ack_run_id or run_id, "at": at},
            "outcome": outcome, "settledAt": at if outcome else None})
    (rt.app / LEDGER[worker]).write_text(json.dumps(
        {"schemaVersion": 1, "entries": entries, "counts": {},
         "lastDeathAt": None}))


def _ps_times_out_for(monkeypatch, pid):
    """``ps`` for ``pid`` times out; returns the timeouts it was asked for."""
    real_run = subprocess.run
    timeouts: list = []

    def run(cmd, *args, **kwargs):
        if list(cmd[:1]) == ["ps"] and str(pid) in cmd:
            timeouts.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return timeouts


def _own(rt, monkeypatch, **kw):
    pid = os.getpid()
    _running_records(rt, pid=pid, birth=rt.st.process_identity(pid)["birth"],
                     **kw)


def _ownership_case(case, rt, monkeypatch):
    """Arrange one ownership case; returns its expected classification."""
    st = rt.st
    if case == "no-state":
        return "absent"
    if case == "run-null":
        (rt.app / STATE["epoch"]).write_text(json.dumps(
            {"schemaVersion": 1, "run": None, "deaths": []}))
        return "absent"
    if case == "finished":
        _own(rt, monkeypatch)
        state = _state(rt)
        state["run"].update(status="finished", outcome="current")
        (rt.app / STATE["epoch"]).write_text(json.dumps(state))
        return "absent"
    if case == "dead-pid":
        _dead_running_state(rt)
        return "absent"
    if case == "birth-differs":
        sleeper = _child(rt, "import time; time.sleep(600)")
        _running_records(rt, pid=sleeper.pid,
                         birth="Mon Jan  1 00:00:00 2001")
        return "absent"
    if case == "entry-terminal":
        _own(rt, monkeypatch, outcome="finished:error")
        return "absent"
    if case == "unreadable-state":
        (rt.app / STATE["epoch"]).write_bytes(b"{not json")
        return "uncertain"
    if case == "unreadable-ledger":
        _own(rt, monkeypatch)
        (rt.app / LEDGER["epoch"]).write_bytes(b"{not json")
        return "uncertain"
    if case == "entry-missing":
        _own(rt, monkeypatch, with_entry=False)
        return "uncertain"
    if case == "ack-mismatch":
        _own(rt, monkeypatch, ack_run_id="run-other")
        return "uncertain"
    if case == "null-birth":
        _running_records(rt, pid=os.getpid(), birth=None)
        return "uncertain"
    if case == "identity-timeout":
        _own(rt, monkeypatch)
        _ps_times_out_for(monkeypatch, os.getpid())
        return "uncertain"
    assert case == "owned"
    _own(rt, monkeypatch)
    return "owned"


@pytest.mark.parametrize("case", [
    "no-state", "run-null", "finished", "dead-pid", "birth-differs",
    "entry-terminal", "unreadable-state", "unreadable-ledger",
    "entry-missing", "ack-mismatch", "null-birth", "identity-timeout",
    "owned"])
def test_ownership_is_decided_from_records_and_identity(rt, monkeypatch,
                                                         case):
    """#1012 T4. Detects a wrong ownership answer: one that calls a dead or
    unconfirmed worker the owner suppresses the pending rebuild (the epoch
    admission refreshes its marker and launches nothing), and one that calls
    a live owner absent replaces its work. Only positive evidence (a running
    run, its unresolved started entry, a live pid of the recorded birth) is
    ownership, and the epoch admission launches in every other case."""
    st = rt.st
    expected = _ownership_case(case, rt, monkeypatch)
    assert st._rebuild_worker_ownership("epoch") == expected
    calls = _spawn_with(rt, monkeypatch, lambda _c, _e: None)
    st.defer_stats_epoch_rebuild()
    assert len(calls) == (0 if expected == "owned" else 1)


def test_ownership_identity_probe_is_bounded_below_the_diagnostic(
        rt, monkeypatch):
    """#1012 T4. Detects an ownership check that can stall an admission for
    the diagnostics' full `ps` timeout: a timed-out probe is `unknown`."""
    st = rt.st
    _own(rt, monkeypatch)
    timeouts = _ps_times_out_for(monkeypatch, os.getpid())
    assert st._rebuild_worker_ownership("epoch") == "uncertain"
    assert timeouts and max(timeouts) <= st._OWNERSHIP_IDENTITY_TIMEOUT_S < 10
    assert st.process_identity(os.getpid()) == {"status": "unknown",
                                                "birth": None}


@pytest.mark.parametrize("change,expected", [
    ("new-run", "uncertain"), ("finished", "absent"),
    ("settled", "absent"), ("progress", "owned")])
def test_ownership_rereads_records_after_the_identity_probe(
        rt, monkeypatch, change, expected):
    """#1012 T4 reread oracle. Detects an ownership answer built on records
    that changed while `ps` ran: a run that finished (or whose entry
    settled) meanwhile is not an owner, a run that replaced it is not
    confirmed, and progress updates alone change nothing."""
    st = rt.st
    pid = os.getpid()
    _own(rt, monkeypatch)
    real = st.process_identity
    fired: list = []

    def probe(p, **kwargs):
        out = real(p, **kwargs)
        if p == pid and not fired:
            fired.append(p)
            state, ledger = _state(rt), _ledger(rt)
            run = state["run"]
            if change == "new-run":
                run["runId"] = "run-new"
            elif change == "finished":
                run.update(status="finished", outcome="current")
            elif change == "settled":
                ledger["entries"][0].update(outcome="finished:current",
                                            settledAt=run["startedAt"])
            else:
                run.update(phase="rebuild:read", phaseStartedAt="x",
                           progressAt="y", counters={"lines": 9},
                           residentBytes=7, peakResidentBytes=8)
            (rt.app / STATE["epoch"]).write_text(json.dumps(state))
            (rt.app / LEDGER["epoch"]).write_text(json.dumps(ledger))
        return out

    monkeypatch.setattr(st, "process_identity", probe)
    assert st._rebuild_worker_ownership("epoch") == expected
    assert fired == [pid]


def _ps_missing(monkeypatch):
    """``ps`` is not installed: every identity probe is ``unknown``."""
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if list(cmd[:1]) == ["ps"]:
            raise FileNotFoundError(2, "No such file or directory", "ps")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def _owner_with_waiting_contender(rt, monkeypatch, worker, owner, age_s):
    """A recorded owner (``owner`` says how certain it is) plus one launch
    that has not started, reserved ``age_s`` seconds ago. Returns the
    owner's launch id."""
    st = rt.st
    pid = os.getpid()
    birth = st.process_identity(pid)["birth"]
    owner_launch = "L-own"
    if owner == "null-birth":
        _running_records(rt, worker, pid=pid, birth=None)
    elif owner in ("ps-missing", "finished"):
        _running_records(rt, worker, pid=pid, birth=birth)
        if owner == "finished":
            state = _state(rt, worker)
            state["run"].update(status="finished", outcome="current")
            (rt.app / STATE[worker]).write_text(json.dumps(state))
    elif owner == "entry-terminal":
        _running_records(rt, worker, pid=pid, birth=birth,
                         outcome="finished:error")
    else:
        assert owner == "dead-pid"
        owner_launch, _run = _dead_running_state(rt, worker)
    ledger = _ledger(rt, worker)
    at = rt.core.now_utc_iso(dt.datetime.now(dt.timezone.utc)
                             - dt.timedelta(seconds=age_s))
    ledger["entries"].append({
        "launchId": "L-waiting", "at": at, "pid": pid, "birth": birth,
        "spawn": "ok", "ack": None, "outcome": None, "settledAt": None})
    (rt.app / LEDGER[worker]).write_text(json.dumps(ledger))
    if owner == "ps-missing":
        _ps_missing(monkeypatch)
    return owner_launch


_UNCERTAIN = [(owner, age, path)
              for path in ("epoch", "heal-retry", "heal-new")
              for owner in ("null-birth", "ps-missing")
              for age in ("young", "old")]
_SETTLED = [(owner, "young", path)
            for path in ("epoch", "heal-retry")
            for owner in ("finished", "entry-terminal", "dead-pid")]


@pytest.mark.parametrize("owner,age,path", _UNCERTAIN + _SETTLED)
def test_an_uncertain_owner_gets_at_most_one_waiting_contender(
        rt, monkeypatch, owner, age, path):
    """#1012 C1. Detects an uncertain owner (a null recorded birth, `ps`
    missing or timing out) piling up contenders: each admission past the
    marker interval launched another, and every one waited in hand-off for
    the owner's whole run, up to the launch capacity at ~69 MB each. While
    one launch that has not started is younger than the standby limit, a
    further admission spawns nothing and keeps its request pending (epoch,
    the heal's `retry-elected` and its no-marker `reserved` path). An older
    one, or an owner that is finished or proven gone, does not withhold."""
    st = rt.st
    worker = "epoch" if path == "epoch" else "heal"
    if path == "heal-retry":
        assert st.reserve_stats_corruption_heal(
            {"healId": "A", "probeKind": "readability"}) == "reserved"
        _make_stale(rt, "heal")
    age_s = {"young": 60, "old": st._CONTENDER_STANDBY_S + 60}[age]
    owner_launch = _owner_with_waiting_contender(rt, monkeypatch, worker,
                                                 owner, age_s)
    calls = _spawn_with(rt, monkeypatch, lambda _c, _e: os.getpid())
    if worker == "epoch":
        status = st.defer_stats_epoch_rebuild()
    else:
        status = st.defer_stats_corruption_heal(
            {"healId": "B", "probeKind": "readability"})
    withheld = owner in ("null-birth", "ps-missing") and age == "young"
    if not withheld:
        assert (status, len(calls)) == ("spawned", 1)
        return
    assert (status, calls) == ("pending", []), "a second contender spawned"
    ids = {e["launchId"] for e in _ledger(rt, worker)["entries"]}
    assert ids == {owner_launch, "L-waiting"}, "a reservation was written"
    if worker == "epoch":
        assert st.read_epoch_marker()["present"]
    else:
        assert st._read_stats_heal_request()["healId"] == (
            "A" if path == "heal-retry" else "B")


def _in_thread(rt, worker, what):
    """Run a worker in a thread, bounded: a worker that never returns fails
    the test instead of hanging it."""
    run = _Run(lambda: _worker(rt, worker), name=what)
    run.start()
    _until(lambda: not run.is_alive(), f"{what} never returned")
    run.finish()
    return run.result


def _pending_request(rt, worker):
    """An admitted request (before its worker starts), its marker aged past
    the retry interval."""
    st = rt.st
    if worker == "epoch":
        st._stats_epoch_rebuild_marker_path().write_text("")
    else:
        assert st.reserve_stats_corruption_heal(
            {"healId": "held", "probeKind": "readability"}) == "reserved"
    _make_stale(rt, worker)


def _admit(rt, monkeypatch, worker):
    """The worker's admission of a further request; returns (status, spawn
    calls)."""
    st = rt.st
    calls = _spawn_with(rt, monkeypatch, lambda _c, _e: None)
    if worker == "epoch":
        return st.defer_stats_epoch_rebuild(), calls
    return st.defer_stats_corruption_heal(
        {"healId": "late", "probeKind": "readability"}), calls


@pytest.mark.parametrize("hold", ["claim-and-worker", "worker-only"])
@pytest.mark.parametrize("worker", ["epoch", "heal"])
def test_a_real_owner_still_wins(rt, monkeypatch, worker, hold):
    """#1012 T3. Detects the fix losing the single-flight guarantee: a live
    owner (a current worker holding both locks, or an older binary's worker
    holding only the worker lock) must still be seen by the probe and the
    admission, and a new worker must record `busy` against it promptly,
    leaving the owner's records untouched."""
    st = rt.st
    probe = getattr(st, PROBE[worker])
    _pending_request(rt, worker)
    with _SyntheticOwner(worker, hold_claim=(hold != "worker-only")) as owner:
        assert probe() is True
        status, calls = _admit(rt, monkeypatch, worker)
        assert status == "pending" and calls == []
        state_before = (rt.app / STATE[worker]).read_bytes()
        owner_entry = _entry(_ledger(rt, worker), owner.launch_id)
        monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "L-late")
        result = _in_thread(rt, worker, "late worker")
        assert result == ("busy" if worker == "epoch" else 0)
        assert (rt.app / STATE[worker]).read_bytes() == state_before
        ledger = _ledger(rt, worker)
        assert _entry(ledger, owner.launch_id) == owner_entry
        assert _entry(ledger, "L-late")["outcome"] == "busy"
    assert probe() is False


@pytest.mark.parametrize("worker", ["epoch", "heal"])
def test_a_claim_lock_error_is_not_busy(rt, monkeypatch, worker):
    """#1012 T7. Detects an I/O failure on the claim lock being recorded as
    `busy` (a terminal that tells doctor another worker owns the rebuild):
    it is an error, logged structurally, with no acknowledgement."""
    import errno

    st = rt.st
    claim = rt.app / CLAIM_LOCK[worker]

    def flock(fd, op):
        try:
            mine, found = os.fstat(fd), os.stat(claim)
        except OSError:
            return REAL_FLOCK(fd, op)
        if (mine.st_dev, mine.st_ino) == (found.st_dev, found.st_ino) \
                and op & fcntl.LOCK_EX:
            raise OSError(errno.EIO, "claim lock I/O error")
        return REAL_FLOCK(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "L-claim-error")
    result = _in_thread(rt, worker, "worker")
    assert result == ("error" if worker == "epoch" else 0)
    assert "result=error stage=claim" in _log(rt, worker)
    ledger = st.read_launch_ledger(worker)["ledger"] or {"entries": []}
    assert not [e for e in ledger["entries"] if e["outcome"] == "busy"]
    assert st.read_worker_state(worker)["state"] is None, "no run began"


@pytest.mark.parametrize("seen", ["no-records", "stale-dead", "stale-unknown"])
@pytest.mark.parametrize("worker", ["epoch", "heal"])
def test_a_claim_holder_that_dies_before_starting_is_taken_over(
        rt, monkeypatch, worker, seen):
    """#1012 T9. Detects a claim loser exiting `busy` without a live started
    worker to hand the rebuild to: a holder that dies before starting (or a
    stale record, or an unconfirmable identity) would leave the pending work
    with nobody. The loser waits, even through worker-lock contention plus a
    running record, and takes the claim when the holder's descriptor closes."""
    st = rt.st
    holder = os.open(rt.app / CLAIM_LOCK[worker], os.O_WRONLY | os.O_CREAT,
                     0o600)
    REAL_FLOCK(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    observer = None
    if seen != "no-records":
        _dead_running_state(rt, worker)
        if seen == "stale-unknown":
            _ps_times_out_for(monkeypatch, _state(rt, worker)["run"]["pid"])
        observer = os.open(rt.app / WORKER_LOCK[worker],
                           os.O_WRONLY | os.O_CREAT, 0o600)
        REAL_FLOCK(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    answers: list = []
    real_ownership = getattr(st, "_rebuild_worker_ownership", None)
    monkeypatch.setattr(st, "_rebuild_worker_ownership",
                        lambda w: answers.append(real_ownership(w))
                        or answers[-1], raising=False)
    b_ids: list = []
    bar = _flock_barrier(rt, monkeypatch, worker, holder_ids=[],
                         claimant_ids=b_ids)
    monkeypatch.setenv("CCTALLY_REBUILD_LAUNCH_ID", "L-B")
    b = _Run(lambda: _worker(rt, worker), register=b_ids, name="B")
    b.start()
    try:
        _until(lambda: _refusals_by(bar, b.ident, "claim") >= 2
               and (observer is None or answers) or not b.is_alive(),
               "B never retried the claim")
        assert b.is_alive(), f"B returned {b.result!r} while A held the claim"
        if observer is not None:
            assert _refusals_by(bar, b.ident, "worker") >= 1
            REAL_FLOCK(observer, fcntl.LOCK_UN)
            os.close(observer)
            observer = None
            seen_claims = _refusals_by(bar, b.ident, "claim")
            _until(lambda: _refusals_by(bar, b.ident, "claim")
                   >= seen_claims + 2 or not b.is_alive(),
                   "B stopped retrying after the observer released")
            assert b.is_alive(), f"B returned {b.result!r}"
        assert "owned" not in answers
        REAL_FLOCK(holder, fcntl.LOCK_UN)
        os.close(holder)
        holder = None
        _until(lambda: not b.is_alive(), "B never took over the claim")
    finally:
        for fd in (observer, holder):
            if fd is not None:
                REAL_FLOCK(fd, fcntl.LOCK_UN)
                os.close(fd)
        b.finish()
    assert b.result == ("current" if worker == "epoch" else 0)
    ledger = _ledger(rt, worker)
    assert _entry(ledger, "L-B")["outcome"] == FINISHED[worker]
    assert not [e for e in ledger["entries"] if e["outcome"] == "busy"]


@pytest.mark.parametrize("worker", ["epoch", "heal"])
def test_doctor_and_db_status_are_truthful_after_a_probe_overlap(
        rt, monkeypatch, worker):
    """#1012 T6. Detects doctor and `db status` reporting a false `busy`
    after a probe overlapped a worker's start: they classify the ledger, so
    they must now show the run's finished outcome as the newest launch."""
    import _lib_doctor

    _run_overlap(rt, monkeypatch, worker, getattr(rt.st, PROBE[worker]))
    outcome = FINISHED[worker].split(":", 1)[1]
    doctor = _lib_doctor.assess_rebuild_worker(
        worker, rt.st.rebuild_worker_snapshot(worker),
        rt.core._command_as_of())["details"]
    status = rt.db._db_status_rebuild_workers()[worker]
    for report in (doctor, status):
        run = report["state"]["run"]
        assert (run["status"], run["outcome"]) == ("finished", outcome)
        assert "busy" not in report["ledger"]["counts"]
        newest = report["ledger"]["newest"]
        assert (newest["launchId"], newest["outcome"]) == (
            "L-start", FINISHED[worker])
