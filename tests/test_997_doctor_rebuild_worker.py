"""#997 T-B2 — `doctor db.rebuild_worker` and `db status` (spec D10, T8, T10).

The October 22 worker ran 4.4 hours and nothing could say whether it was
running, stalled or dead. `doctor` and `db status` now classify each detached
rebuild worker from a read-only snapshot (no worker or admission flock, no
write), using the operator-confirmed sentences, and never infer a death from
elapsed time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS


NOW = dt.datetime(2026, 10, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
BIRTH = "Sat Oct 24 10:00:00 2026"
NOUN = {"epoch": "stats.db upgrade rebuild", "heal": "stats.db auto-heal"}
STATE = {"epoch": "stats-epoch-rebuild.state.json",
         "heal": "stats-heal.state.json"}
LEDGER = {"epoch": "stats-epoch-rebuild.launches.json",
          "heal": "stats-heal.launches.json"}


def _doctor():
    return importlib.import_module("_lib_doctor")


def _at(minutes_ago: float) -> str:
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat().replace(
        "+00:00", "Z")


def _run(status="running", *, launch_id="L1", run_id="R1", pid=100,
         started=25, progress=1, outcome=None, **extra):
    run = {"runId": run_id, "launchId": launch_id, "pid": pid,
           "birth": BIRTH, "status": status, "phase": "rebuild:read",
           "startedAt": _at(started), "progressAt": _at(progress),
           "outcome": outcome, "peakResidentBytes": 300 * 1024 ** 2}
    run.update(extra)
    return run


def _entry(launch_id="L1", *, pid=100, ack=True, outcome=None, at=30):
    return {"launchId": launch_id, "at": _at(at), "pid": pid, "birth": BIRTH,
            "spawn": "ok",
            "ack": ({"kind": "started", "runId": "R-" + launch_id,
                     "at": _at(at)} if ack else None),
            "outcome": outcome, "settledAt": _at(at) if outcome else None}


def _snap(*, run=None, deaths=(), entries=(), identities=None, pending=None,
          unreadable=False):
    state = None
    if run is not None or deaths:
        state = {"schemaVersion": 1, "run": run, "deaths": list(deaths)}
    counts: dict = {}
    for e in entries:
        if e["outcome"]:
            counts[e["outcome"]] = counts.get(e["outcome"], 0) + 1
    return {
        "state": None if unreadable else state,
        "stateUnreadable": unreadable,
        "ledger": {"schemaVersion": 1, "entries": list(entries),
                   "counts": counts, "lastDeathAt": None},
        "ledgerUnreadable": False,
        "identities": identities or {},
        "pending": pending or {"present": False, "since": None,
                               "indexPending": False},
    }


def _assess(worker, snap):
    return _doctor().assess_rebuild_worker(worker, snap, NOW)


def _check(**snaps):
    return _doctor()._check_db_rebuild_worker(
        SimpleNamespace(rebuild_workers=snaps, now_utc=NOW))


ALIVE = {100: {"status": "alive", "birth": BIRTH}}
GONE = {100: {"status": "gone", "birth": None}}
WORKERS = pytest.mark.parametrize("worker", ["epoch", "heal"])


# --------------------------------------------------------------------------
# one test per D10 row, for each worker
# --------------------------------------------------------------------------

@WORKERS
def test_row_running(worker):
    got = _assess(worker, _snap(run=_run(), entries=[_entry()],
                                identities=ALIVE))
    assert got["severity"] == "warn"
    assert got["warnings"] == [
        f"a {NOUN[worker]} has been running for 25 minutes"]
    assert got["details"]["liveness"] == "alive"


@WORKERS
def test_row_running_no_progress(worker):
    got = _assess(worker, _snap(run=_run(progress=12), entries=[_entry()],
                                identities=ALIVE))
    assert got["warnings"] == [
        f"a {NOUN[worker]} has been running for 25 minutes with no progress "
        f"for 12 minutes"]


@WORKERS
def test_row_died_in_run(worker):
    got = _assess(worker, _snap(run=_run(), entries=[_entry()],
                                identities=GONE))
    assert got["warnings"] == [f"the last {NOUN[worker]} died without finishing"]
    assert got["details"]["preStart"] is False
    assert got["details"]["launchId"] == "L1"


@WORKERS
def test_row_died_pre_start(worker):
    finished = _run("finished", launch_id="L0", outcome="success")
    got = _assess(worker, _snap(
        run=finished,
        entries=[_entry("L0", outcome="finished:success", at=60),
                 _entry("L1", ack=False, at=5)],
        identities=GONE))
    assert got["warnings"] == [f"the last {NOUN[worker]} died without finishing"]
    assert got["details"]["preStart"] is True


@WORKERS
def test_row_spawn_failed(worker):
    failed = dict(_entry("L2", ack=False, outcome="spawn-failed", at=2),
                  spawn="failed", pid=None)
    got = _assess(worker, _snap(entries=[failed]))
    assert got["warnings"] == [
        f"the last {NOUN[worker]} failed (spawn-failed)"]


@pytest.mark.parametrize(("worker", "outcome"), [
    ("epoch", "error"), ("epoch", "success-residual-not-durable"),
    ("heal", "failed"), ("heal", "rebuilt-residual-not-durable"),
    ("heal", "request-persist-failed"), ("heal", "declined-no-journal"),
    ("heal", "prod-refused"), ("epoch", "some-future-outcome"),
])
def test_row_failed_outcome(worker, outcome):
    got = _assess(worker, _snap(
        run=_run("finished", outcome=outcome),
        entries=[_entry(outcome=f"finished:{outcome}")]))
    assert got["warnings"] == [f"the last {NOUN[worker]} failed ({outcome})"]


@WORKERS
def test_row_memory_above_bound(worker):
    got = _assess(worker, _snap(
        run=_run("finished", outcome="success",
                 peakResidentBytes=int(3 * 1024 ** 3)),
        entries=[_entry(outcome="finished:success")]))
    assert got["warnings"] == [
        f"the last {NOUN[worker]} used 3.0 GiB of memory, above its 2 GiB "
        f"bound"]


@WORKERS
def test_row_pending_no_worker(worker):
    got = _assess(worker, _snap(pending={"present": True, "since": _at(15),
                                         "indexPending": True}))
    assert got["warnings"] == [
        f"a {NOUN[worker]} has been pending for 15 minutes with no worker "
        f"running"]
    young = _assess(worker, _snap(pending={"present": True, "since": _at(9),
                                           "indexPending": True}))
    assert young["severity"] == "ok"


@WORKERS
def test_pending_row_reports_launch_capacity(worker):
    """Rev 7: at 100 unresolved entries the spawner withholds the launch; the
    request stays pending and the existing pending row carries
    ``details.admission = "launch-capacity"`` and the unresolved count, with
    no new sentence."""
    reserved = [dict(_entry(f"U{i}", pid=None, ack=False), birth=None,
                     spawn="reserved") for i in range(100)]
    pending = {"present": True, "since": _at(15), "indexPending": True}
    got = _assess(worker, _snap(entries=reserved, pending=pending))
    assert got["warnings"] == [
        f"a {NOUN[worker]} has been pending for 15 minutes with no worker "
        f"running"]
    assert got["details"]["admission"] == "launch-capacity"
    assert got["details"]["unresolvedCount"] == 100
    below = _assess(worker, _snap(entries=reserved[:99], pending=pending))
    assert below["warnings"] == got["warnings"]
    assert "admission" not in below["details"]
    assert "unresolvedOverflow" not in got["details"]["ledger"]


def test_epoch_marker_without_index_mismatch_is_not_pending():
    got = _assess("epoch", _snap(pending={"present": True, "since": _at(60),
                                          "indexPending": False}))
    assert got["severity"] == "ok"


@WORKERS
def test_row_state_unreadable(worker):
    got = _assess(worker, _snap(unreadable=True))
    assert got["warnings"] == [f"the {NOUN[worker]} state is unreadable"]


@WORKERS
def test_row_recovered_deaths_ok(worker):
    death = {"runId": "R0", "launchId": "L0", "pid": 9,
             "detectedAt": _at(30), "lastPhase": "completion:replay-all"}
    old = dict(death, runId="Rold", detectedAt=_at(60 * 24 * 8))
    got = _assess(worker, _snap(
        run=_run("finished", outcome="success"), deaths=[old, death],
        entries=[_entry("L0", outcome="died-in-run", at=90),
                 _entry(outcome="finished:success")]))
    assert got["severity"] == "ok"
    assert got["summary"] == f"the last {NOUN[worker]} finished (success)"
    assert [d["runId"] for d in got["details"]["recoveredDeaths"]] == ["R0"]


@WORKERS
def test_row_no_state(worker):
    got = _assess(worker, _snap())
    assert got["severity"] == "ok"
    assert got["summary"] == f"no {NOUN[worker]} has run"
    assert got["row"] == "idle"


# --------------------------------------------------------------------------
# outcome classes
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("worker", "outcome", "expected"), [
    ("epoch", "success", "clean"), ("epoch", "current", "clean"),
    ("epoch", "error", "failing"),
    ("epoch", "success-residual-not-durable", "failing"),
    ("heal", "success", "clean"), ("heal", "no-request", "clean"),
    ("heal", "declined-readable", "clean"),
    ("heal", "maintenance-busy", "retryable"),
    ("heal", "ingest-busy", "retryable"),
    ("heal", "failed", "failing"),
    ("heal", "rebuilt-residual-not-durable", "failing"),
    ("heal", "request-persist-failed", "failing"),
    ("heal", "declined-no-journal", "failing"),
    ("heal", "prod-refused", "failing"),
    ("heal", "never-heard-of-it", "failing"),
])
def test_outcome_class_members(worker, outcome, expected):
    assert _doctor().rebuild_outcome_class(worker, outcome) == expected


@pytest.mark.parametrize(("reason", "expected"), [
    ("integrity_check_ok", "clean"),
    ("integrity_check_inconclusive", "failing"),
    ("integrity_check_unavailable", "failing"),
    ("bundle_write_failed", "failing"),
    (None, "failing"),
])
def test_declined_unconfirmed_reason_classes(reason, expected):
    doctor = _doctor()
    assert doctor.rebuild_outcome_class(
        "heal", "declined-unconfirmed", decline_reason=reason) == expected
    got = _assess("heal", _snap(
        run=_run("finished", outcome="declined-unconfirmed",
                 declineReason=reason, finalization="done"),
        entries=[_entry(outcome="finished:declined-unconfirmed")]))
    assert got["severity"] == ("ok" if expected == "clean" else "warn")


def test_failing_outcome_with_pending_finalization():
    doctor = _doctor()
    assert doctor.rebuild_outcome_class(
        "heal", "request-persist-failed", finalization="pending") == "failing"
    assert doctor.rebuild_outcome_class(
        "heal", "declined-readable", finalization="pending") == "retryable"
    got = _assess("heal", _snap(
        run=_run("finished", outcome="request-persist-failed",
                 finalization="pending"),
        entries=[_entry(outcome="finished:request-persist-failed")]))
    assert got["warnings"] == [
        "the last stats.db auto-heal failed (request-persist-failed)"]
    retry = _assess("heal", _snap(
        run=_run("finished", outcome="declined-readable",
                 finalization="pending"),
        entries=[_entry(outcome="finished:declined-readable")]))
    assert retry["severity"] == "ok"
    assert retry["details"]["outcomeClass"] == "retryable"


# --------------------------------------------------------------------------
# the aggregate check
# --------------------------------------------------------------------------

def test_check_carries_every_warn_sentence_epoch_first():
    result = _check(
        epoch=_snap(run=_run(), entries=[_entry()], identities=ALIVE),
        heal=_snap(run=_run("finished", outcome="failed"),
                   entries=[_entry(outcome="finished:failed")]))
    assert result.id == "db.rebuild_worker"
    assert result.severity == "warn"
    assert result.summary == (
        "a stats.db upgrade rebuild has been running for 25 minutes; "
        "the last stats.db auto-heal failed (failed)")
    assert set(result.details) == {"epoch", "heal"}


def test_check_ok_when_nothing_ran():
    result = _check(epoch=_snap(), heal=_snap())
    assert result.severity == "ok"
    assert result.summary == ("no stats.db upgrade rebuild has run; "
                              "no stats.db auto-heal has run")
    assert _doctor()._check_db_rebuild_worker(SimpleNamespace(
        rebuild_workers=None, now_utc=NOW)).severity == "ok"


def test_check_is_registered_after_version_ahead():
    defs = dict((cat, checks) for cat, _title, checks
                in _doctor()._CATEGORY_DEFINITIONS)
    ids = [check_id for check_id, _fn in defs["db"]]
    assert ids[ids.index("db.version_ahead") + 1] == "db.rebuild_worker"


def test_doctor_alive_unacknowledged_not_dead():
    """A delayed child (alive, no acknowledgement) is never a death, however
    old its entry."""
    got = _assess("epoch", _snap(
        run=_run("finished", launch_id="L0", outcome="success"),
        entries=[_entry("L0", outcome="finished:success", at=600),
                 _entry("L1", ack=False, at=60 * 24 * 365)],
        identities=ALIVE))
    assert got["severity"] == "ok"
    assert got["details"]["ledger"]["unresolved"][0]["launchId"] == "L1"


def test_doctor_spawn_failed_newest():
    entries = [_entry("L0", outcome="finished:success", at=120),
               dict(_entry("L1", ack=False, outcome="spawn-failed", at=3),
                    spawn="failed", pid=None)]
    got = _assess("epoch", _snap(
        run=_run("finished", launch_id="L0", outcome="success"),
        entries=entries))
    assert got["warnings"] == [
        "the last stats.db upgrade rebuild failed (spawn-failed)"]


def test_doctor_liveness_unknown_reads_running():
    got = _assess("epoch", _snap(run=_run(), entries=[_entry()],
                                 identities={100: {"status": "unknown",
                                                   "birth": None}}))
    assert got["warnings"] == [
        "a stats.db upgrade rebuild has been running for 25 minutes"]
    assert got["details"]["liveness"] == "unknown"


# --------------------------------------------------------------------------
# the gather (real files, real processes)
# --------------------------------------------------------------------------

@pytest.fixture
def rt(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.delenv("CCTALLY_REBUILD_LAUNCH_ID", raising=False)
    core = sys.modules["_cctally_core"]
    pathlib.Path(core.APP_DIR).mkdir(parents=True, exist_ok=True)
    yield SimpleNamespace(ns=ns, core=core, st=sys.modules["_cctally_store"],
                          db=sys.modules["_cctally_db"],
                          doctor_io=sys.modules.get("_cctally_doctor")
                          or importlib.import_module("_cctally_doctor"),
                          update=sys.modules["_cctally_update"],
                          app=pathlib.Path(core.APP_DIR))
    sys.modules["_lib_rebuild_progress"].uninstall()


def _write(rt, worker, state=None, ledger=None):
    if state is not None:
        (rt.app / STATE[worker]).write_text(json.dumps(state))
    if ledger is not None:
        (rt.app / LEDGER[worker]).write_text(json.dumps(ledger))


def test_doctor_rereads_before_reporting_death(rt, monkeypatch):
    """The run finishes between the state read and the process probe: the
    re-read shows it finished, so no death is reported."""
    st = rt.st
    running = {"schemaVersion": 1, "run": _run(pid=424242), "deaths": []}
    ledger = {"schemaVersion": 1, "entries": [_entry(pid=424242)],
              "counts": {}, "lastDeathAt": None}
    _write(rt, "epoch", running, ledger)

    def probe(pid):
        finished = dict(running, run=_run("finished", pid=424242,
                                          outcome="success"))
        _write(rt, "epoch", finished)
        return {"status": "gone", "birth": None}

    monkeypatch.setattr(st, "process_identity", probe)
    snap = st.rebuild_worker_snapshot("epoch")
    assert snap["reread"] is True
    got = _doctor().assess_rebuild_worker("epoch", snap, NOW)
    assert got["severity"] == "ok"
    assert got["summary"] == "the last stats.db upgrade rebuild finished (success)"


def test_doctor_reports_confirmed_pre_start_death(rt, monkeypatch):
    st = rt.st
    children = []

    def spawn(_command, *, env=None):
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import os, signal; os.kill(os.getpid(), 9)"])
        proc.wait(timeout=PRESENCE_BACKSTOP_SECONDS)
        children.append(proc)
        return proc.pid

    monkeypatch.setattr(rt.update, "_spawn_detached_with_pid", spawn)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    before = (rt.app / LEDGER["epoch"]).read_bytes()
    result = rt.doctor_io._gather_rebuild_workers()
    got = _doctor().assess_rebuild_worker("epoch", result["epoch"],
                                          dt.datetime.now(dt.timezone.utc))
    assert got["warnings"][0] == \
        "the last stats.db upgrade rebuild died without finishing"
    assert got["details"]["preStart"] is True
    assert (rt.app / LEDGER["epoch"]).read_bytes() == before, \
        "doctor computes settlements without writing"


def test_doctor_gather_takes_no_worker_lock(rt, monkeypatch):
    """A probe of the worker flock would make a starting worker exit `busy`;
    the gather opens no lock file and never calls flock."""
    st = rt.st
    _write(rt, "epoch", {"schemaVersion": 1, "run": _run(pid=os.getpid()),
                         "deaths": []},
           {"schemaVersion": 1, "entries": [_entry(pid=os.getpid())],
            "counts": {}, "lastDeathAt": None})
    (rt.app / "stats-epoch-rebuild.pending").write_text("")
    opened = []
    real_open = os.open

    def tracking_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    def no_flock(*_a, **_kw):
        raise AssertionError("the doctor gather took a flock")

    monkeypatch.setattr(fcntl, "flock", no_flock)
    monkeypatch.setattr(os, "open", tracking_open)
    result = rt.doctor_io._gather_rebuild_workers()
    monkeypatch.setattr(os, "open", real_open)
    assert set(result) == {"epoch", "heal"}
    assert not [p for p in opened if p.endswith(".lock")]
    assert result["epoch"]["state"]["run"]["status"] == "running"


def test_doctor_gather_failure_reads_as_unreadable(rt, monkeypatch):
    """A worker whose snapshot cannot be gathered is reported with the
    existing unreadable sentence and the error in `details`, never as "no
    ... has run"; the other worker is still gathered."""
    st = rt.st
    real = st.rebuild_worker_snapshot

    def snapshot(worker):
        if worker == "epoch":
            raise OSError(5, "Input/output error")
        return real(worker)

    monkeypatch.setattr(st, "rebuild_worker_snapshot", snapshot)
    result = rt.doctor_io._gather_rebuild_workers()
    got = _doctor().assess_rebuild_worker("epoch", result["epoch"], NOW)
    assert got["warnings"] == [f"the {NOUN['epoch']} state is unreadable"]
    assert got["row"] == "unreadable"
    assert "OSError" in got["details"]["gatherError"]
    heal = _doctor().assess_rebuild_worker("heal", result["heal"], NOW)
    assert heal["summary"] == f"no {NOUN['heal']} has run"
    assert heal["details"].get("gatherError") is None


def test_doctor_whole_gather_failure_reads_as_unreadable(rt, monkeypatch):
    def boom():
        raise RuntimeError("gather exploded")

    monkeypatch.setattr(rt.doctor_io, "_gather_rebuild_workers", boom)
    state = rt.doctor_io.doctor_gather_state(now_utc=NOW)
    check = _doctor()._check_db_rebuild_worker(state)
    assert check.severity == "warn"
    assert check.summary == (f"the {NOUN['epoch']} state is unreadable; "
                             f"the {NOUN['heal']} state is unreadable")
    for worker in ("epoch", "heal"):
        assert "RuntimeError" in check.details[worker]["gatherError"]


def test_db_status_rebuild_workers_json(rt, capsys):
    _write(rt, "heal", {"schemaVersion": 1, "run": _run(
        "finished", outcome="failed", finalization=None), "deaths": []},
        {"schemaVersion": 1, "entries": [_entry(outcome="finished:failed")],
         "counts": {"finished:failed": 1}, "lastDeathAt": None})
    assert rt.db.cmd_db_status(argparse.Namespace(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    workers = payload["rebuildWorkers"]
    assert set(workers) == {"epoch", "heal"}
    assert workers["epoch"]["state"] is None
    assert workers["epoch"]["classification"]["row"] == "idle"
    assert workers["heal"]["state"]["run"]["outcome"] == "failed"
    assert workers["heal"]["classification"] == {
        "severity": "warn", "row": "failed",
        "summary": "the last stats.db auto-heal failed (failed)",
        "warnings": ["the last stats.db auto-heal failed (failed)"],
    }
    assert rt.db.cmd_db_status(argparse.Namespace(json=False)) == 0
    text = capsys.readouterr().out
    assert "the last stats.db auto-heal failed (failed)" in text
    assert "stats.db upgrade rebuild" not in text, "an idle worker prints nothing"


# --------------------------------------------------------------------------
# the dashboard aggregate helpers (T-B3 shares them with the envelope)
# --------------------------------------------------------------------------

_DIED_EPOCH = "the last stats.db upgrade rebuild died without finishing"


def test_aggregate_helper_maps_check_severity():
    doctor = importlib.import_module("_lib_doctor")
    check = doctor.CheckResult(
        id="db.rebuild_worker", title="Rebuild worker", severity="warn",
        summary=_DIED_EPOCH, remediation=None, details={})
    assert doctor.rebuild_worker_aggregate([check]) == {
        "severity": "warn", "summary": _DIED_EPOCH}
    ok = doctor.CheckResult(
        id="db.rebuild_worker", title="Rebuild worker", severity="ok",
        summary="no stats.db upgrade rebuild has run", remediation=None,
        details={})
    assert doctor.rebuild_worker_aggregate([ok]) is None
    assert doctor.rebuild_worker_aggregate([]) is None
    # The envelope block helper both producers share.
    report = doctor.DoctorReport(
        schema_version=1, generated_at=NOW, cctally_version="0",
        overall_severity="warn", counts={"ok": 0, "warn": 1, "fail": 0},
        categories=(doctor.CategoryResult(
            id="db", title="DB", severity="warn", checks=(check,)),))
    block = doctor.envelope_block(report)
    assert block == {
        "severity": "warn", "counts": {"ok": 0, "warn": 1, "fail": 0},
        "generated_at": "2026-10-24T12:00:00Z",
        "fingerprint": doctor.fingerprint(report),
        "rebuild_worker": {"severity": "warn", "summary": _DIED_EPOCH}}
