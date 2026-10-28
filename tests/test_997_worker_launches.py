"""#997 T-B1 — the pure launch-ledger kernel (spec D8).

A detached rebuild worker that dies before it reaches a recorded start left no
trace on 2026-10-22: the log records only returned outcomes. The ledger is the
record of every launch, and `_lib_worker_launches` is the one place that
decides what became of each one. Its central rule: a death is settled only by
evidence — the run state shows the launch finished, or its process is
confirmed gone by identity — never by elapsed time.
"""
from __future__ import annotations

import copy
import importlib

import pytest


def _wl():
    return importlib.import_module("_lib_worker_launches")


NOW = "2026-10-24T12:00:00Z"
BIRTH = "Sat Oct 24 11:00:00 2026"


def _ledger_with(*entries):
    wl = _wl()
    ledger = wl.empty_ledger()
    for launch_id, pid, birth in entries:
        ledger = wl.append_entry(
            ledger, launch_id=launch_id, at="2026-10-24T11:00:00Z",
            pid=pid, birth=birth)
    return ledger


def test_empty_ledger_shape():
    wl = _wl()
    ledger = wl.empty_ledger()
    assert ledger == {
        "schemaVersion": 1, "entries": [], "counts": {},
        "lastDeathAt": None,
    }


def test_append_dedupes_by_launch_id_and_does_not_mutate_input():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    before = copy.deepcopy(ledger)
    again = wl.append_entry(ledger, launch_id="L1", at=NOW, pid=200,
                            birth="other")
    assert ledger == before
    assert len(again["entries"]) == 1
    assert again["entries"][0]["pid"] == 100
    entry = again["entries"][0]
    assert entry == {
        "launchId": "L1", "at": "2026-10-24T11:00:00Z", "pid": 100,
        "birth": BIRTH, "spawn": "ok", "ack": None, "outcome": None,
        "settledAt": None,
    }


def test_spawn_failure_is_settled_at_once():
    wl = _wl()
    ledger = wl.append_entry(wl.empty_ledger(), launch_id="L1", at=NOW,
                             pid=None, birth=None, spawn="failed")
    entry = ledger["entries"][0]
    assert entry["spawn"] == "failed"
    assert entry["outcome"] == "spawn-failed"
    assert entry["settledAt"] == NOW
    assert ledger["counts"] == {"spawn-failed": 1}
    assert ledger["lastDeathAt"] is None


def test_classify_never_settles_by_age():
    """A launch years old whose process is alive, or whose liveness is
    unknown, stays unresolved."""
    wl = _wl()
    ledger = wl.empty_ledger()
    for launch_id, pid, birth in (("alive", 10, BIRTH), ("unknown", 11, BIRTH),
                                  ("null-birth", 12, None),
                                  ("no-probe", 13, BIRTH)):
        ledger = wl.append_entry(ledger, launch_id=launch_id,
                                 at="2001-01-01T00:00:00Z", pid=pid,
                                 birth=birth)
    identities = {
        10: {"status": "alive", "birth": BIRTH},
        11: {"status": "unknown", "birth": None},
        12: {"status": "alive", "birth": BIRTH},
    }
    result = wl.classify(ledger, None, identities, now=NOW)
    assert result["settled"] == []
    assert [e["outcome"] for e in result["ledger"]["entries"]] == [None] * 4


def test_classify_gone_without_ack_is_pre_start_death():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    result = wl.classify(ledger, None, {100: {"status": "gone", "birth": None}},
                         now=NOW)
    (entry,) = result["ledger"]["entries"]
    assert entry["outcome"] == "died-pre-start"
    assert entry["settledAt"] == NOW
    assert [e["launchId"] for e in result["settled"]] == ["L1"]
    assert result["ledger"]["counts"] == {"died-pre-start": 1}
    assert result["ledger"]["lastDeathAt"] == NOW
    # Idempotent: a second classification settles nothing new.
    again = wl.classify(result["ledger"], None,
                        {100: {"status": "gone", "birth": None}}, now=NOW)
    assert again["settled"] == []
    assert again["ledger"]["counts"] == {"died-pre-start": 1}


def test_classify_birth_mismatch_is_gone_and_ack_makes_it_in_run():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    ledger = wl.acknowledge(ledger, "L1", "started", "run-1", at=NOW)
    result = wl.classify(
        ledger, None, {100: {"status": "alive", "birth": "a different one"}},
        now=NOW)
    assert result["ledger"]["entries"][0]["outcome"] == "died-in-run"


def test_classify_transfers_finished_outcome_from_state():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    ledger = wl.acknowledge(ledger, "L1", "started", "run-1", at=NOW)
    state = {"schemaVersion": 1, "run": {
        "runId": "run-1", "launchId": "L1", "status": "finished",
        "outcome": "success"}, "deaths": []}
    # Even with the process gone, the finished run wins over a death.
    result = wl.classify(ledger, state, {100: {"status": "gone"}}, now=NOW)
    assert result["ledger"]["entries"][0]["outcome"] == "finished:success"
    assert result["ledger"]["counts"] == {"finished:success": 1}


def test_classify_reads_a_death_recorded_in_state():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    ledger = wl.acknowledge(ledger, "L1", "started", "run-1", at=NOW)
    state = {"schemaVersion": 1, "run": None,
             "deaths": [{"runId": "run-1", "launchId": "L1"}]}
    result = wl.classify(ledger, state, {}, now=NOW)
    assert result["ledger"]["entries"][0]["outcome"] == "died-in-run"


def test_busy_acknowledgement_settles_busy():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    ledger = wl.acknowledge(ledger, "L1", "busy", at=NOW)
    (entry,) = ledger["entries"]
    assert entry["ack"] == {"kind": "busy", "at": NOW}
    assert entry["outcome"] == "busy"
    assert ledger["counts"] == {"busy": 1}


def test_acknowledge_appends_when_spawner_never_did():
    wl = _wl()
    ledger = wl.acknowledge(wl.empty_ledger(), "L9", "started", "run-9",
                            at=NOW, pid=42, birth=BIRTH)
    (entry,) = ledger["entries"]
    assert entry["launchId"] == "L9"
    assert entry["pid"] == 42 and entry["birth"] == BIRTH
    assert entry["ack"] == {"kind": "started", "runId": "run-9", "at": NOW}
    assert entry["outcome"] is None


def test_settle_is_idempotent_and_first_settlement_wins():
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    ledger = wl.settle(ledger, "L1", "died-in-run", at=NOW)
    ledger = wl.settle(ledger, "L1", "finished:success", at=NOW)
    assert ledger["entries"][0]["outcome"] == "died-in-run"
    assert ledger["counts"] == {"died-in-run": 1}


def test_prune_bounds_settled_never_drops_unresolved_keeping_counts():
    wl = _wl()
    ledger = wl.empty_ledger()
    for i in range(30):
        ledger = wl.append_entry(ledger, launch_id=f"S{i}", at=NOW, pid=i + 1,
                                 birth=BIRTH)
        ledger = wl.settle(ledger, f"S{i}", "died-pre-start", at=NOW)
    for i in range(130):
        ledger = wl.append_entry(ledger, launch_id=f"U{i}", at=NOW,
                                 pid=1000 + i, birth=BIRTH)
    pruned = wl.prune(ledger)
    settled = [e for e in pruned["entries"] if e["outcome"]]
    unresolved = [e for e in pruned["entries"] if not e["outcome"]]
    assert [e["launchId"] for e in settled] == [f"S{i}" for i in range(20, 30)]
    # Unresolved entries are never dropped (launch capacity bounds them).
    assert [e["launchId"] for e in unresolved] == [
        f"U{i}" for i in range(130)]
    assert "unresolvedOverflow" not in pruned
    assert pruned["counts"] == {"died-pre-start": 30}
    # Ledger order (launch order) is preserved.
    ids = [e["launchId"] for e in pruned["entries"]]
    assert ids == [e["launchId"] for e in ledger["entries"] if e["launchId"]
                   in set(ids)]


def test_liveness_rules():
    wl = _wl()
    entry = {"pid": 5, "birth": BIRTH}
    assert wl.liveness(entry, {5: {"status": "gone"}}) == "gone"
    assert wl.liveness(entry, {5: {"status": "zombie"}}) == "unknown"
    assert wl.liveness(entry, {5: {"status": "alive", "birth": BIRTH}}) == "alive"
    assert wl.liveness(entry, {5: {"status": "alive", "birth": "x"}}) == "gone"
    assert wl.liveness(entry, {5: {"status": "alive", "birth": None}}) == "unknown"
    assert wl.liveness({"pid": 5, "birth": None},
                       {5: {"status": "alive", "birth": BIRTH}}) == "unknown"
    assert wl.liveness(entry, {}) == "unknown"
    assert wl.liveness({"pid": None, "birth": None}, {}) == "unknown"


def test_normalize_rejects_malformed_ledgers():
    wl = _wl()
    with pytest.raises(ValueError):
        wl.normalize({"schemaVersion": 2, "entries": []})
    with pytest.raises(ValueError):
        wl.normalize({"schemaVersion": 1, "entries": "nope"})
    with pytest.raises(ValueError):
        wl.normalize([])
    ok = wl.normalize({"schemaVersion": 1, "entries": []})
    assert ok == wl.empty_ledger()


def test_reservation_then_fill_and_failed_fill():
    """Revision 6: the spawner persists a reservation before the spawn and
    fills the child's identity after; a failed spawn settles at once."""
    wl = _wl()
    ledger = wl.reserve(wl.empty_ledger(), launch_id="L1", at=NOW)
    (entry,) = ledger["entries"]
    assert entry["spawn"] == "reserved"
    assert entry["pid"] is None and entry["birth"] is None
    filled = wl.fill_spawn(ledger, "L1", pid=77, birth=BIRTH, at=NOW)
    assert filled["entries"][0]["spawn"] == "ok"
    assert filled["entries"][0]["pid"] == 77
    assert filled["entries"][0]["birth"] == BIRTH
    failed = wl.fill_spawn(ledger, "L1", pid=None, birth=None, at=NOW,
                           spawn="failed")
    assert failed["entries"][0]["outcome"] == "spawn-failed"
    assert failed["counts"] == {"spawn-failed": 1}


def test_fill_never_overwrites_a_worker_acknowledged_identity():
    wl = _wl()
    ledger = wl.reserve(wl.empty_ledger(), launch_id="L1", at=NOW)
    ledger = wl.acknowledge(ledger, "L1", "started", "run-1", at=NOW,
                            pid=77, birth=BIRTH)
    assert ledger["entries"][0]["pid"] == 77
    assert ledger["entries"][0]["spawn"] == "ok"
    late = wl.fill_spawn(ledger, "L1", pid=77, birth="other", at=NOW)
    assert late["entries"][0]["birth"] == BIRTH


def test_reserved_entry_without_pid_stays_unresolved():
    wl = _wl()
    ledger = wl.reserve(wl.empty_ledger(), launch_id="L1",
                        at="2001-01-01T00:00:00Z")
    result = wl.classify(ledger, None, {}, now=NOW)
    assert result["settled"] == []
    assert wl.prune(result["ledger"], None)["entries"][0]["outcome"] is None


def test_prune_exempts_the_protected_entry_from_both_bounds():
    wl = _wl()
    ledger = wl.empty_ledger()
    ledger = wl.append_entry(ledger, launch_id="P", at=NOW, pid=1, birth=BIRTH)
    ledger = wl.settle(ledger, "P", "died-in-run", at=NOW)
    for i in range(20):
        ledger = wl.append_entry(ledger, launch_id=f"S{i}", at=NOW,
                                 pid=10 + i, birth=BIRTH)
        ledger = wl.settle(ledger, f"S{i}", "busy", at=NOW)
    pruned = wl.prune(ledger, "P")
    ids = [e["launchId"] for e in pruned["entries"]]
    assert ids == ["P"] + [f"S{i}" for i in range(11, 20)]
    # Unresolved: nothing is dropped, protected or not.
    ledger = wl.append_entry(wl.empty_ledger(), launch_id="Q", at=NOW, pid=1,
                             birth=BIRTH)
    for i in range(105):
        ledger = wl.append_entry(ledger, launch_id=f"U{i}", at=NOW,
                                 pid=100 + i, birth=BIRTH)
    pruned = wl.prune(ledger, "Q")
    ids = [e["launchId"] for e in pruned["entries"]]
    assert ids == ["Q"] + [f"U{i}" for i in range(105)]


def test_prune_exempts_every_protected_entry():
    """A starting worker protects both the run its state still names and its
    own launch until its state write lands (spec D8): each is exempt, and the
    settled bound counts both plus ``SETTLED_KEEP - 2`` others."""
    wl = _wl()
    ledger = wl.empty_ledger()
    ledger = wl.append_entry(ledger, launch_id="A", at=NOW, pid=1, birth=BIRTH)
    ledger = wl.settle(ledger, "A", "died-in-run", at=NOW)
    for i in range(20):
        ledger = wl.append_entry(ledger, launch_id=f"S{i}", at=NOW,
                                 pid=10 + i, birth=BIRTH)
        ledger = wl.settle(ledger, f"S{i}", "busy", at=NOW)
    ledger = wl.append_entry(ledger, launch_id="B", at=NOW, pid=2, birth=BIRTH)
    ledger = wl.settle(ledger, "B", "finished:error", at=NOW)
    for protected in ({"A", "B"}, ["B", "A"], ("A", "B", None)):
        pruned = wl.prune(ledger, protected)
        ids = [e["launchId"] for e in pruned["entries"]]
        assert ids == ["A"] + [f"S{i}" for i in range(12, 20)] + ["B"]
    # A single id still works, and a non-string id protects nothing.
    ids = [e["launchId"] for e in wl.prune(ledger, "A")["entries"]]
    assert ids == ["A"] + [f"S{i}" for i in range(12, 20)] + ["B"]
    ids = [e["launchId"] for e in wl.prune(ledger, [["A"], 7])["entries"]]
    assert ids == [f"S{i}" for i in range(11, 20)] + ["B"]


def test_classify_skips_a_death_whose_launch_id_is_not_a_string():
    """A corrupt but schema-valid death record (its ``launchId`` a list)
    neither crashes classification nor settles anything by itself."""
    wl = _wl()
    ledger = _ledger_with(("L1", 100, BIRTH))
    ledger = wl.acknowledge(ledger, "L1", "started", "run-1", at=NOW)
    state = {"schemaVersion": 1, "run": None,
             "deaths": [{"runId": "run-0", "launchId": ["L1"]},
                        {"runId": "run-x", "launchId": {"a": 1}}]}
    result = wl.classify(ledger, state, {}, now=NOW)
    assert result["settled"] == []
    assert result["ledger"]["entries"][0]["outcome"] is None


def test_launch_capacity_is_fewer_than_100_unresolved():
    wl = _wl()
    assert wl.LAUNCH_CAPACITY == 100
    ledger = wl.empty_ledger()
    assert wl.has_launch_capacity(ledger)
    for i in range(99):
        ledger = wl.reserve(ledger, launch_id=f"U{i}", at=NOW)
    assert wl.has_launch_capacity(ledger)
    ledger = wl.reserve(ledger, launch_id="U99", at=NOW)
    assert not wl.has_launch_capacity(ledger)
    # Settled entries never count against capacity.
    ledger = wl.acknowledge(ledger, "U0", "busy", at=NOW, pid=5, birth=BIRTH)
    assert wl.has_launch_capacity(ledger)
