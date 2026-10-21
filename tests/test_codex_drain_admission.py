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
