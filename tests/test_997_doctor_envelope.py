"""#997 T-B3 — the dashboard doctor aggregate carries `rebuild_worker`.

The chip's tooltip and accessible name lead with the rebuild-worker sentence
when `db.rebuild_worker` is not ok. The envelope's small doctor block gains
``rebuild_worker: {severity, summary} | None``: populated from the check
result when it warns, ``None`` when it is ok and in every synthetic fallback
block, on both producers (the TUI precompute and the envelope's inline path).
"""
from __future__ import annotations

import datetime as dt
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
DIED = "the last stats.db upgrade rebuild died without finishing"


def _env(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    importlib.import_module("_lib_snapshot_cache")._DOCTOR_MEMO.clear()
    import _cctally_core
    return ns, pathlib.Path(_cctally_core.APP_DIR)


def _write_dead_epoch_launch(app_dir: pathlib.Path) -> None:
    """A settled pre-start death: no pid probe, deterministic."""
    app_dir.mkdir(parents=True, exist_ok=True)
    ledger = {
        "schemaVersion": 1, "lastDeathAt": "2026-10-24T11:00:00Z",
        "counts": {"died-pre-start": 1},
        "entries": [{
            "launchId": "launch-b", "at": "2026-10-24T10:59:00Z",
            "pid": 4343, "birth": "Sat Oct 24 10:59:00 2026", "spawn": "ok",
            "ack": None, "outcome": "died-pre-start",
            "settledAt": "2026-10-24T11:00:00Z"}],
    }
    (app_dir / "stats-epoch-rebuild.launches.json").write_text(
        json.dumps(ledger))


def test_envelope_carries_rebuild_worker(tmp_path, monkeypatch):
    ns, app_dir = _env(tmp_path, monkeypatch)
    tui = ns["_load_sibling"]("_cctally_tui")
    quiet = tui._tui_precompute_doctor_payload(NOW, None)
    assert "_error" not in quiet, quiet
    assert "rebuild_worker" in quiet and quiet["rebuild_worker"] is None

    _write_dead_epoch_launch(app_dir)
    importlib.import_module("_lib_snapshot_cache")._DOCTOR_MEMO.clear()
    payload = tui._tui_precompute_doctor_payload(NOW, None)
    assert "_error" not in payload, payload
    assert payload["rebuild_worker"] == {"severity": "warn", "summary": DIED}
    # The block's existing keys are unchanged.
    assert {"severity", "counts", "generated_at", "fingerprint"} <= set(payload)


def test_envelope_inline_doctor_path_carries_rebuild_worker(
        tmp_path, monkeypatch):
    """The envelope's inline fallback (a snapshot with no precomputed doctor
    block) builds the same aggregate."""
    ns, app_dir = _env(tmp_path, monkeypatch)
    _write_dead_epoch_launch(app_dir)
    snap = ns["_empty_dashboard_snapshot"]()
    assert getattr(snap, "doctor_payload", None) is None
    envelope = ns["snapshot_to_envelope"](snap, now_utc=NOW,
                                          monotonic_now=0.0,
                                          runtime_bind="127.0.0.1")
    assert envelope["doctor"]["rebuild_worker"] == {
        "severity": "warn", "summary": DIED}


def test_doctor_fail_fallbacks_carry_null_rebuild_worker(tmp_path, monkeypatch):
    ns, _app_dir = _env(tmp_path, monkeypatch)
    tui = ns["_load_sibling"]("_cctally_tui")

    def boom(**_kwargs):
        raise RuntimeError("gather exploded")

    monkeypatch.setattr(sys.modules["cctally"], "doctor_gather_state", boom)
    payload = tui._tui_precompute_doctor_payload(NOW, None)
    assert payload["severity"] == "fail" and "_error" in payload
    assert "rebuild_worker" in payload and payload["rebuild_worker"] is None

    snap = ns["_empty_dashboard_snapshot"]()
    envelope = ns["snapshot_to_envelope"](snap, now_utc=NOW,
                                          monotonic_now=0.0,
                                          runtime_bind="127.0.0.1")
    assert envelope["doctor"]["severity"] == "fail"
    assert "rebuild_worker" in envelope["doctor"]
    assert envelope["doctor"]["rebuild_worker"] is None


# --------------------------------------------------------------------------
# P1-1: the deferred frames carry the rebuild-worker aggregate
# --------------------------------------------------------------------------
#
# While an epoch rebuild is pending or running every live stats open defers,
# so the dashboard serves only the deferred startup frame and deferred ticks.
# Neither runs the doctor gather; both must still carry the `db.rebuild_worker`
# aggregate (spec D10, acceptance A1).

RUNNING = "a stats.db upgrade rebuild has been running for 5 minutes"


def _iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_running_epoch_state(app_dir: pathlib.Path, pid: int,
                               birth) -> None:
    """The run state and ledger a started epoch worker writes, with a
    pending epoch marker."""
    store = sys.modules["_cctally_store"]
    app_dir.mkdir(parents=True, exist_ok=True)
    started = _iso(NOW - dt.timedelta(minutes=5))
    progress = _iso(NOW - dt.timedelta(minutes=1))
    state = {"schemaVersion": 1, "run": {
        "runId": "run-a", "launchId": "launch-a", "pid": pid,
        "birth": birth, "version": "test", "pendingSince": started,
        "launchesBeforeStart": 0, "status": "running",
        "phase": "rebuild:read", "phaseStartedAt": started,
        "progressAt": progress, "counters": {}, "outcome": None,
        "startedAt": started, "finishedAt": None, "wallSeconds": None},
        "deaths": [], "previousUnreadable": None}
    (app_dir / "stats-epoch-rebuild.state.json").write_text(
        json.dumps(state))
    ledger = {"schemaVersion": 1, "counts": {}, "lastDeathAt": None,
              "entries": [{
                  "launchId": "launch-a", "at": started, "pid": pid,
                  "birth": birth, "spawn": "ok",
                  "ack": {"kind": "started", "runId": "run-a",
                          "at": started},
                  "outcome": None, "settledAt": None}]}
    (app_dir / "stats-epoch-rebuild.launches.json").write_text(
        json.dumps(ledger))
    store._write_epoch_marker(started)


def _live_running(app_dir):
    store = sys.modules["_cctally_store"]
    pid = os.getpid()
    _write_running_epoch_state(app_dir, pid,
                               store.process_identity(pid)["birth"])


def _dead_running(app_dir):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=PRESENCE_BACKSTOP_SECONDS)
    _write_running_epoch_state(app_dir, proc.pid,
                               "Thu Jan  1 00:00:00 1970")


def _no_state(app_dir):
    app_dir.mkdir(parents=True, exist_ok=True)


def _startup_frame(ns):
    import _cctally_dashboard as dash

    db = sys.modules["_cctally_db"]
    return dash._dashboard_stats_deferred_snapshot(
        SimpleNamespace(no_sync=False, host="127.0.0.1"), pinned_now=NOW,
        exc=db.StatsEpochRebuildDeferred("pending"))


def _deferred_tick(ns, monkeypatch, initial):
    db = sys.modules["_cctally_db"]
    ref = ns["_SnapshotRef"](initial)

    class Hub:
        def __init__(self):
            self.published = []

        def publish(self, snapshot):
            self.published.append(snapshot)

    hub = Hub()

    def still_pending(**_kwargs):
        raise db.StatsEpochRebuildDeferred("pending")

    monkeypatch.setitem(ns, "_tui_build_snapshot", still_pending)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=NOW, display_tz_pref_override=None)
    locked(skip_sync=True)
    return hub.published[-1]


def _envelope_rebuild_worker(ns, snap):
    envelope = ns["snapshot_to_envelope"](snap, now_utc=NOW,
                                          monotonic_now=0.0,
                                          runtime_bind="127.0.0.1")
    return envelope["doctor"]["rebuild_worker"]


@pytest.mark.parametrize(("arrange", "expected"), [
    (_live_running, {"severity": "warn", "summary": RUNNING}),
    (_dead_running, {"severity": "warn", "summary": DIED}),
    (_no_state, None),
], ids=["running", "died", "none"])
def test_deferred_frames_carry_rebuild_worker(tmp_path, monkeypatch,
                                              arrange, expected):
    ns, app_dir = _env(tmp_path, monkeypatch)
    arrange(app_dir)
    tui = sys.modules["_cctally_tui"]
    monkeypatch.setattr(
        tui, "_tui_precompute_doctor_payload",
        lambda *_a, **_k: pytest.fail("a deferred frame ran the doctor"))

    startup = _startup_frame(ns)
    assert startup.doctor_payload["severity"] == "warn"
    assert _envelope_rebuild_worker(ns, startup) == expected

    # A tick over the startup frame, and one over a frame with no doctor
    # block at all: both publish the aggregate.
    for initial in (startup, ns["_empty_dashboard_snapshot"]()):
        importlib.import_module("_lib_snapshot_cache").reset_doctor_memo()
        published = _deferred_tick(ns, monkeypatch, initial)
        assert published.hydrating is True
        assert "stats-open:" in published.last_sync_error
        assert published.doctor_payload["severity"] == "warn"
        assert _envelope_rebuild_worker(ns, published) == expected


def test_deferred_ticks_memoize_the_worker_gather(tmp_path, monkeypatch):
    ns, app_dir = _env(tmp_path, monkeypatch)
    _live_running(app_dir)
    doctor = sys.modules["_cctally_doctor"]
    real = doctor._gather_rebuild_workers
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(doctor, "_gather_rebuild_workers", counting)
    initial = ns["_empty_dashboard_snapshot"]()
    first = _deferred_tick(ns, monkeypatch, initial)
    second = _deferred_tick(ns, monkeypatch, first)
    assert len(calls) == 1
    assert _envelope_rebuild_worker(ns, second) == {
        "severity": "warn", "summary": RUNNING}


def test_deferred_frames_survive_a_failing_worker_gather(tmp_path,
                                                         monkeypatch):
    """A helper failure never crashes or blocks a frame: the startup frame
    carries ``None`` and a tick keeps the previous value."""
    ns, app_dir = _env(tmp_path, monkeypatch)
    _live_running(app_dir)
    startup = _startup_frame(ns)
    assert _envelope_rebuild_worker(ns, startup) == {
        "severity": "warn", "summary": RUNNING}

    def boom():
        raise RuntimeError("gather exploded")

    monkeypatch.setattr(sys.modules["_cctally_doctor"],
                        "_gather_rebuild_workers", boom)
    sc = importlib.import_module("_lib_snapshot_cache")
    sc.reset_doctor_memo()
    assert _startup_frame(ns).doctor_payload["rebuild_worker"] is None
    sc.reset_doctor_memo()
    published = _deferred_tick(ns, monkeypatch, startup)
    assert _envelope_rebuild_worker(ns, published) == {
        "severity": "warn", "summary": RUNNING}
