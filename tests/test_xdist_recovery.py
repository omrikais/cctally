"""A pytest-xdist worker death in a `--dist loadgroup` leg (#1086).

bin/cctally-test-all's bulk leg, the quick gate and the broad run all run
pytest under `-n N --dist loadgroup` (bin/_lib_test_selection.py `_xdist`).
Under pytest-xdist 3.8.0 a worker death in that mode can strand the leg until
the run's deadline (the replacement or a survivor is handed an all-completed
unit, or holds one item for ever), or end it in an `INTERNALERROR` that loses
the rest of the leg (work assigned to a replacement whose collection has not
registered). tests/_xdist_recovery.py replaces the scheduler and adds an
outcome gate, so every death ends in a bounded, classified outcome.

`test_worker_death_subprocess` drives real nested xdist sessions, built with
the same argv `pytest_legs` gives the quick gate, because a worker death and
its teardown are process behaviour. Every death it injects is either a
recorded precondition or a handshake with the controller's own records, never
a sleep, and an unmet precondition fails the case.
"""
from __future__ import annotations

import gzip
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

xdist = pytest.importorskip("xdist")
from xdist.remote import Producer  # noqa: E402
from xdist.scheduler import LoadGroupScheduling  # noqa: E402

from tests import _xdist_recovery as recovery  # noqa: E402

Recovering = recovery.scheduler_class()

# --- in-process cases ------------------------------------------------------
#
# A stand-in config and fake worker nodes drive the scheduler directly. Where a
# case reproduces an upstream defect, the same driver also runs against
# upstream `LoadGroupScheduling` and asserts the defect, so the case is not
# vacuous.


class FakeNode:
    """A worker as the scheduler sees it: it records every index list sent and
    runs them in order (`outstanding`)."""

    def __init__(self, wid, log=None):
        self.gateway = type("Gateway", (), {"id": wid})()
        self.shutting_down = False
        self.sent: list[list[int]] = []
        self.outstanding: list[int] = []
        self._log = log if log is not None else []

    def send_runtest_some(self, indices):
        self.sent.append(list(indices))
        self.outstanding.extend(indices)
        self._log.append(("send", self.gateway.id, list(indices)))

    def shutdown(self):
        self.shutting_down = True
        self._log.append(("shutdown", self.gateway.id))

    def __repr__(self):
        return f"<FakeNode {self.gateway.id}>"


class FakeConfig:
    def __init__(self, numnodes, reorder=True):
        self.option = type("Option", (), {"loadscopereorder": reorder})()
        self._values = {"tx": [f"{numnodes}*popen"], "dist": "loadgroup"}
        self.hook = type("Hook", (), {"pytest_collectreport": staticmethod(lambda report: None)})()

    def getvalue(self, name):
        return self._values[name]


def _singles(n, prefix="s"):
    return [f"tests/test_a.py::test_{prefix}[{i}]" for i in range(n)]


def _group(name, n):
    return [f"tests/test_g.py::test_{name}[{i}]@{name}" for i in range(n)]


def _scheduler(cls, nodes, numnodes=None, reorder=True):
    sched = cls(FakeConfig(numnodes or len(nodes), reorder), Producer("test", enabled=False))
    for node in nodes:
        sched.add_node(node)
    return sched


def _register(sched, nodes, collection):
    for node in nodes:
        sched.add_node_collection(node, collection)


def _give(sched, node, unit):
    """Hand ``node`` a unit as if it had been sent earlier: its pending items
    are outstanding on the node."""
    scope = sched._split_scope(next(iter(unit)))
    sched.assigned_work.setdefault(node, {})[scope] = unit
    collection = sched.registered_collections[node]
    node.outstanding.extend(collection.index(n) for n, done in unit.items() if not done)


def _drain(sched, nodes, limit=10_000):
    """Every live node completes its outstanding items in turn; a node that
    holds nothing after shutdown finishes normally (`worker_workerfinished`)."""
    for _ in range(limit):
        progressed = False
        for node in nodes:
            if node not in sched.assigned_work:
                continue
            if node.outstanding:
                sched.mark_test_complete(node, node.outstanding.pop(0))
                progressed = True
            elif node.shutting_down:
                assert sched.remove_node(node) is None
                progressed = True
        if not progressed:
            return
    raise AssertionError("the simulation did not settle")


def _late_death_state(cls):
    """A dead node that completed ten single units and a group's prefix, died
    running the group's third member with its fourth and a single item
    prefetched; two survivors hold two items each, one unit is queued."""
    singles = _singles(14)
    group = _group("grp", 4)
    extra = "tests/test_extra.py::test_x"
    collection = [*singles, *group, "tests/test_q.py::test_q", extra]
    dead, b, c = FakeNode("gw0"), FakeNode("gw1"), FakeNode("gw2")
    sched = _scheduler(cls, [dead, b, c])
    _register(sched, [dead, b, c], collection)
    sched.collection = list(collection)
    for nid in singles[:10]:
        _give(sched, dead, {nid: True})
    _give(sched, dead, {group[0]: True, group[1]: True, group[2]: False, group[3]: False})
    _give(sched, dead, {singles[10]: False})
    _give(sched, b, {singles[11]: False})
    _give(sched, b, {singles[12]: False})
    _give(sched, c, {singles[13]: False})
    _give(sched, c, {"tests/test_q.py::test_q": False})
    sched.workqueue[extra] = {extra: False}
    dead.outstanding.clear()
    return sched, dead, (b, c), collection, group


def test_late_death_requeues_only_unfinished_units():
    """Detects the stall of a late death (#1086, spec 4.3 item 1): re-queued
    completed units reach the survivors as empty sends, which absorb each one
    into idleness until the run's deadline."""
    sched, dead, live, collection, group = _late_death_state(Recovering)
    crash = sched.remove_node(dead)
    assert crash == group[2]
    # Only units with pending items were re-queued; the completed ones never.
    assert all(not all(unit.values()) for unit in sched.workqueue.values())
    _drain(sched, list(live))
    sends = [indices for node in live for indices in node.sent]
    assert [] not in sends
    sent_ids = [collection[i] for indices in sends for i in indices]
    assert group[2] not in sent_ids, "the crash item ran again"
    assert sorted(sent_ids) == sorted(["tests/test_extra.py::test_x", group[3],
                                       "tests/test_a.py::test_s[10]"])
    assert sched.unfinished_count() == 0 and not sched.assigned_work

    upstream, dead, live, _collection, _group_ids = _late_death_state(LoadGroupScheduling)
    upstream.remove_node(dead)
    _drain(upstream, list(live))
    assert [] in [indices for node in live for indices in node.sent], "upstream defect absent"


def test_group_remainder_moves_whole():
    """Detects a group split across workers, or its completed or crashed
    members sent again, after the group's worker dies mid-group."""
    group = _group("grp", 5)
    collection = [*group, *_singles(2)]
    dead, survivor = FakeNode("gw0"), FakeNode("gw1")
    sched = _scheduler(Recovering, [dead, survivor])
    _register(sched, [dead, survivor], collection)
    sched.collection = list(collection)
    _give(sched, dead, {group[0]: True, group[1]: False, group[2]: False, group[3]: False,
                        group[4]: False})
    _give(sched, survivor, {collection[5]: False})
    dead.outstanding.clear()
    assert sched.remove_node(dead) == group[1]
    remainder = [[collection[i] for i in indices] for indices in survivor.sent]
    assert remainder == [group[2:]]


def _registered_run(cls, nodes, collection):
    sched = _scheduler(cls, nodes)
    _register(sched, nodes, collection)
    sched.schedule()
    return sched


def test_replacement_gets_lookahead():
    """Detects a replacement left holding one item while units stay queued:
    a worker starts an item only once it knows the next one, so it waits for
    ever (spec 4.3 item 2)."""
    collection = _singles(12)
    for cls, expected in ((Recovering, 2), (LoadGroupScheduling, 1)):
        a, b = FakeNode("gw0"), FakeNode("gw1")
        sched = _registered_run(cls, [a, b], collection)
        replacement = FakeNode("gw2")
        sched.add_node(replacement)
        sched.add_node_collection(replacement, collection)
        sched.schedule()
        assert sched.workqueue
        assert sum(len(i) for i in replacement.sent) == expected, cls.__name__


def _second_death_state(cls):
    collection = _singles(20)
    a, b = FakeNode("gw0"), FakeNode("gw1")
    sched = _registered_run(cls, [a, b], collection)
    replacement = FakeNode("gw2")
    sched.add_node(replacement)  # its collection has not registered
    return sched, a, b, replacement


def test_unregistered_node_is_never_assigned():
    """Detects work assigned to a replacement whose collection has not
    registered: upstream raises `KeyError`, an `INTERNALERROR` or a lost unit
    and crash report (spec 4.3 item 3)."""
    sched, a, _b, replacement = _second_death_state(Recovering)
    crash = sched.remove_node(a)
    assert crash == "tests/test_a.py::test_s[0]"
    assert sched.assigned_work[replacement] == {} and replacement.sent == []
    # The dead node's prefetched item is queued again, for the replacement to
    # take once its collection registers.
    assert "tests/test_a.py::test_s[2]" in sched.workqueue

    upstream, a, _b, _replacement = _second_death_state(LoadGroupScheduling)
    with pytest.raises(KeyError):
        upstream.remove_node(a)


class GoneNode(FakeNode):
    """A worker whose process has gone while its death event is still queued:
    execnet refuses every send on the closed channel or broken pipe."""

    def send_runtest_some(self, indices):
        self._log.append(("refused", self.gateway.id, list(indices)))
        raise OSError("cannot send (already closed?)")


@pytest.mark.timeout(10, method="thread")
def test_a_refused_send_leaves_the_death_to_its_own_event():
    """Detects a send to a worker that has gone, before its death event is
    handled, raising `OSError` out of the scheduler: an `INTERNALERROR` that
    loses the rest of the leg, as the double-death `KeyError` did. The unit
    must go back to the queue, so the death is attributed by what the worker
    actually held: its running item, or none (an itemless death)."""
    collection = _singles(20)
    for cls in (Recovering, LoadGroupScheduling):
        a, b = FakeNode("gw0"), FakeNode("gw1")
        sched = _registered_run(cls, [a, b], collection)
        held = {nid for unit in sched.assigned_work[b].values() for nid in unit}
        b.__class__ = GoneNode  # gw1 dies; gw0's death is handled first
        if cls is LoadGroupScheduling:
            with pytest.raises(OSError):
                sched.remove_node(a)
            continue
        assert sched.remove_node(a) == "tests/test_a.py::test_s[0]"
        assert {nid for unit in sched.assigned_work[b].values() for nid in unit} == held
        assert sched.remove_node(b) == "tests/test_a.py::test_s[1]"
        # Every item but the two crash items is queued again; none is lost.
        assert sched.unfinished_count() == 18 and not sched.assigned_work

    # A worker that reports its last item and dies idle stays an itemless
    # death: the refused unit is not charged to it as a crash item.
    a, b = FakeNode("gw0"), FakeNode("gw1")
    sched = _registered_run(Recovering, [a, b], collection)
    queued_before = list(sched.workqueue)
    b.__class__ = GoneNode
    for index in list(b.outstanding):
        sched.mark_test_complete(b, index)
    assert list(sched.workqueue) == queued_before
    assert sched.remove_node(b) is None


def _first_schedule_race(cls):
    """Three originals; gw0 registers and dies before the first schedule, and
    its replacement is still collecting when the other two register. Two units
    for three nodes."""
    collection = _singles(2)
    a, b, c = FakeNode("gw0"), FakeNode("gw1"), FakeNode("gw2")
    sched = _scheduler(cls, [a, b, c])
    sched.add_node_collection(a, collection)
    assert sched.remove_node(a) is None
    replacement = FakeNode("gw3")
    sched.add_node(replacement)
    sched.add_node_collection(b, collection)
    sched.add_node_collection(c, collection)
    assert sched.collection_is_completed
    return sched, replacement, collection


def test_first_schedule_keeps_uncollected_replacement():
    """Detects the first schedule retiring an uncollected replacement, whose
    later collection then fails an assertion (spec 4.3 item 5)."""
    sched, replacement, collection = _first_schedule_race(Recovering)
    sched.schedule()
    assert replacement in sched.assigned_work and not replacement.shutting_down
    assert replacement.sent == []
    sched.add_node_collection(replacement, collection)
    sched.schedule()
    assert replacement.shutting_down  # nothing left to give it

    upstream, replacement, collection = _first_schedule_race(LoadGroupScheduling)
    upstream.schedule()
    assert replacement not in upstream.assigned_work and replacement.shutting_down
    with pytest.raises(AssertionError):
        upstream.add_node_collection(replacement, collection)

    # The set-aside node is restored even when the first schedule raises.
    sched, replacement, _collection = _first_schedule_race(Recovering)

    def boom():
        raise RuntimeError("collection check failed")

    sched._check_nodes_have_same_collection = boom
    with pytest.raises(RuntimeError):
        sched.schedule()
    assert replacement in sched.assigned_work


def test_mismatched_replacement_collection_raises():
    """Detects a replacement with a different collection silently stranding
    the queued work it should take over."""
    collection = _singles(6)
    a, b = FakeNode("gw0"), FakeNode("gw1")
    sched = _registered_run(Recovering, [a, b], collection)
    replacement = FakeNode("gw2")
    sched.add_node(replacement)
    with pytest.raises(RuntimeError) as excinfo:
        sched.add_node_collection(replacement, collection[:-1])
    assert "gw2" in str(excinfo.value) and "#1086" in str(excinfo.value)
    assert replacement not in sched.registered_collections


def _healthy_log(cls):
    log: list = []
    nodes = [FakeNode(f"gw{i}", log) for i in range(3)]
    collection = [*_group("ga", 5), *_singles(7), *_group("gb", 3), *_singles(9, "t"),
                  *_group("gc", 1)]
    sched = _registered_run(cls, nodes, collection)
    _drain(sched, nodes)
    return log, sched


def test_healthy_dispatch_matches_upstream():
    """Detects the corrected scheduler changing a healthy leg's dispatch: the
    lookahead and the first-schedule set-aside must be no-ops when no worker
    dies."""
    recovered, sched = _healthy_log(Recovering)
    upstream, _ = _healthy_log(LoadGroupScheduling)
    assert recovered == upstream
    assert sum(1 for event in recovered if event[0] == "shutdown") == 3
    assert sched.unfinished_count() == 0


class FakeDSession:
    """The parts of xdist's DSession the gate touches; `worker_errordown`
    mirrors 3.8.0's: remove the node, swallow `KeyError`, report a crash item,
    count the death."""

    def __init__(self, sched, max_restart, events=None):
        self.sched = sched
        self.shouldstop = False
        self._failed_nodes_count = 0
        self._max_worker_restart = max_restart
        self.crashes: list[str] = []
        self.events = events if events is not None else []

    def worker_errordown(self, node, error):
        self.events.append(("original", node.gateway.id))
        try:
            crash = self.sched.remove_node(node)
        except KeyError:
            pass
        else:
            if crash:
                self.crashes.append(crash)
        self._failed_nodes_count += 1


class FakeReporter:
    def __init__(self, events):
        self.events = events
        self.lines: list[str] = []

    def short_test_summary(self):
        self.events.append(("summary",))

    def write_line(self, line, **_kw):
        self.lines.append(line)

    def write_sep(self, sep, title="", **_kw):
        self.lines.append(title)


class FakePluginManager:
    def __init__(self, plugins):
        self._plugins = plugins

    def get_plugin(self, name):
        return self._plugins.get(name)


class FakeSessionConfig:
    def __init__(self, plugins, numprocesses=3, dist="loadgroup"):
        self.pluginmanager = FakePluginManager(plugins)
        self.option = type("Option", (), {"numprocesses": numprocesses})()
        self._dist = dist

    def getvalue(self, name):
        assert name == "dist"
        return self._dist


def test_unfinished_count_and_death_attribution():
    """Detects a worker death with no assigned item finishing green (spec 4.3
    item 9), and a normal finish counted as such a death, which would fail
    every healthy leg with exit 3."""
    sched, _dead, _live, _collection, _group_ids = _late_death_state(Recovering)
    # One queued item; pending: the dead node's three, two on each survivor.
    assert sched.unfinished_count() == 1 + 3 + 2 + 2

    # A normal finish: remove_node on an all-completed workload, as
    # worker_workerfinished calls it, records nothing.
    sched, nodes = _finished_state(Recovering)
    gate = recovery.OutcomeGate(config=None)
    dsession = FakeDSession(sched, max_restart=10)
    gate.install(dsession)
    assert sched.remove_node(nodes[0]) is None
    assert gate.itemless_deaths == []
    upstream, nodes = _finished_state(LoadGroupScheduling)
    assert upstream.remove_node(nodes[0]) is None  # the silent green upstream

    # The death wrapper: an empty workload, an all-completed one, an absent
    # node each record the id; a node holding a pending item records nothing.
    sched, nodes = _finished_state(Recovering)
    empty = FakeNode("gw9")
    sched.add_node(empty)
    gate = recovery.OutcomeGate(config=None)
    dsession = FakeDSession(sched, max_restart=10)
    gate.install(dsession)
    dsession.worker_errordown(empty, "crashed")
    dsession.worker_errordown(nodes[0], "crashed")
    dsession.worker_errordown(FakeNode("gw8"), "crashed")  # KeyError swallowed
    assert gate.itemless_deaths == ["gw9", "gw0", "gw8"]
    assert dsession._failed_nodes_count == 3

    sched, dead, _live, _collection, group = _late_death_state(Recovering)
    gate = recovery.OutcomeGate(config=None)
    dsession = FakeDSession(sched, max_restart=10)
    gate.install(dsession)
    dsession.worker_errordown(dead, "crashed")
    assert gate.itemless_deaths == [] and dsession.crashes == [group[2]]


def _finished_state(cls):
    """Two nodes that each completed all they were given."""
    collection = _singles(4)
    nodes = [FakeNode("gw0"), FakeNode("gw1")]
    sched = _scheduler(cls, nodes)
    _register(sched, nodes, collection)
    sched.collection = list(collection)
    _give(sched, nodes[0], {collection[0]: True})
    _give(sched, nodes[0], {collection[1]: True})
    _give(sched, nodes[1], {collection[2]: True})
    return sched, nodes


def test_version_guard(monkeypatch):
    """Detects the plugin running unaudited against another pytest-xdist
    release, whose internals it overrides, instead of failing loudly."""
    monkeypatch.setattr(xdist, "__version__", "3.9.0")
    with pytest.raises(pytest.UsageError) as excinfo:
        recovery.pytest_configure(FakeSessionConfig({}))
    assert "3.9.0" in str(excinfo.value) and recovery.SUPPORTED_XDIST in str(excinfo.value)
    worker = FakeSessionConfig({})
    worker.workerinput = {"workerid": "gw0"}
    for config in (worker, FakeSessionConfig({}, numprocesses=0),
                   FakeSessionConfig({}, dist="load")):
        recovery.pytest_configure(config)
    monkeypatch.setattr(xdist, "__version__", recovery.SUPPORTED_XDIST)
    recovery.pytest_configure(FakeSessionConfig({}))


def _gate_session(max_restart, sched=None):
    events: list = []
    if sched is None:
        sched, _nodes = _finished_state(Recovering)
    dsession = FakeDSession(sched, max_restart, events)
    reporter = FakeReporter(events)
    config = FakeSessionConfig({"dsession": dsession, "terminalreporter": reporter})
    session = type("Session", (), {"config": config})()
    loop = recovery.pytest_runtestloop(session)
    next(loop)  # the gate is installed; xdist's loop would run now
    return loop, dsession, reporter, events


def test_outcome_gate():
    """Detects a leg that exhausts its restart limit draining the survivors'
    held work and ending exit 1 as if complete, a leg that ends with items
    unrun or after an itemless death ending green, and a failure summary
    that is lost (status 3 suppresses pytest's) or written twice."""
    # A death below the limit returns normally.
    loop, dsession, reporter, events = _gate_session(max_restart=1)
    sched = dsession.sched
    node = next(iter(sched.assigned_work))
    pending = FakeNode("gw5")
    sched.add_node(pending)
    sched.registered_collections[pending] = sched.collection
    _give(sched, pending, {sched.collection[3]: False})
    dsession.worker_errordown(pending, "crashed")
    assert events == [("original", "gw5")]

    # The death that exceeds the limit aborts at once, after the original ran,
    # with nothing unrun: summary once, then Exit(3) carrying the reason.
    with pytest.raises(pytest.exit.Exception) as excinfo:
        dsession.worker_errordown(node, "crashed")
    assert events == [("original", "gw5"), ("original", "gw0"), ("summary",)]
    assert excinfo.value.returncode == 3
    assert sched.unfinished_count() == 0
    reason = str(excinfo.value.msg)
    assert reason.startswith("xdist recovery (#1086):") and "2 crashed" in reason
    assert not any(reason in line for line in reporter.lines)

    # An interrupt is not relabelled as exhaustion: a worker that exits with
    # status 2 reaches `worker_errordown` after xdist set `shouldstop`, and the
    # loop must raise its own `Interrupted` (exit 2), not exit 3.
    loop, dsession, reporter, events = _gate_session(max_restart=0)
    dsession.shouldstop = "gw0 received keyboard-interrupt"
    dsession.worker_errordown(next(iter(dsession.sched.assigned_work)), "keyboard-interrupt")
    assert events == [("original", "gw0")]

    # A normal loop end with unfinished items exits 3 naming them.
    loop, dsession, reporter, events = _gate_session(max_restart=10)
    queued = dsession.sched.collection[3]
    dsession.sched.workqueue[queued] = {queued: False}
    with pytest.raises(pytest.exit.Exception) as excinfo:
        loop.send(True)
    assert excinfo.value.returncode == 3 and " 1 item" in excinfo.value.msg
    assert events == [("summary",)]

    # After an itemless death the leg finishes its work, then exits 3.
    loop, dsession, reporter, events = _gate_session(max_restart=10)
    dsession.worker_errordown(next(iter(dsession.sched.assigned_work)), "crashed")
    with pytest.raises(pytest.exit.Exception) as excinfo:
        loop.send(True)
    assert excinfo.value.returncode == 3 and "gw0" in excinfo.value.msg

    # A normal end with neither returns the loop's result.
    loop, dsession, reporter, events = _gate_session(max_restart=10)
    with pytest.raises(StopIteration) as stop:
        loop.send(True)
    assert stop.value.value is True and events == []

    # A loop that raised passes through untouched.
    loop, dsession, reporter, events = _gate_session(max_restart=10)
    boom = KeyboardInterrupt("interrupted")
    with pytest.raises(KeyboardInterrupt) as raised:
        loop.throw(boom)
    assert raised.value is boom and events == []

    # Elsewhere (a worker, another --dist) the hook only yields.
    for config in (FakeSessionConfig({}), FakeSessionConfig({"dsession": object()},
                                                            dist="load")):
        loop = recovery.pytest_runtestloop(type("Session", (), {"config": config})())
        next(loop)
        with pytest.raises(StopIteration):
            loop.send(True)


# --- subprocess cases ------------------------------------------------------

# Seconds. The nested run's bound, the reap after a kill, and the handshake
# waits inside the nested run.
RUN_BOUND = 60
REAP_BOUND = 10
HANDSHAKE_WAIT = 20

# The exhausted case's large group, and the time it may take after the
# exhausting death: the controller's teardown bound (about 20 s, execnet's
# terminate timeouts) plus 5 s.
BIG_GROUP_ITEMS = 50
EXHAUSTED_END_BOUND = 25

# Both sides of the nested run: the injector and recorder. One conftest, keyed
# by REC_CASE. Worker side: one line per started item in a per-worker file,
# line-buffered so a killed worker's last start survives; deaths by
# os._exit(1). Controller side: JSON lines with a wall-clock timestamp for each
# node ready, collection finished and node down, read from the scheduler.
RECORDER = r'''
import json
import os
import pathlib
import time

import pytest

DIR = pathlib.Path(os.environ["REC_DIR"])
CASE = os.environ["REC_CASE"]
WID = os.environ.get("PYTEST_XDIST_WORKER")  # None on the controller
WAIT = float(os.environ["REC_WAIT"])
_CONFIG = []
_STATE = {"started": 0, "plain": 0, "starts": None}


def _append(name, record):
    record["at"] = time.time()
    with open(DIR / name, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _await(marker):
    end = time.monotonic() + WAIT
    while time.monotonic() < end:
        if (DIR / marker).exists():
            return True
        time.sleep(0.01)
    _append("unmet.jsonl", {"worker": WID, "waited_for": marker})
    return False


def _number(wid):
    return int(wid[2:])


def pytest_configure(config):
    _CONFIG.append(config)


# --- worker side ---

def pytest_collection_modifyitems(config, items):
    if WID is None:
        return
    if CASE == "collection-death" and WID == "gw1":
        os._exit(1)
    if CASE == "double":
        # The first replacement finishes collecting only once the second one
        # is in the scheduler; the second waits for the first's registration.
        if WID == "gw3":
            _await("ready-gw4")
        elif WID == "gw4":
            _await("collected-gw3")


def pytest_runtest_logstart(nodeid, location):
    if WID is None:
        return
    if _STATE["starts"] is None:
        _STATE["starts"] = open(DIR / ("starts-" + WID), "a", encoding="utf-8", buffering=1)
    _STATE["starts"].write(nodeid + "\n")


def pytest_runtest_setup(item):
    if WID is None:
        return
    _STATE["started"] += 1
    grouped = item.get_closest_marker("xdist_group") is not None
    if not grouped:
        _STATE["plain"] += 1
    if CASE == "late" and (DIR / ("kill-" + WID)).exists():
        os._exit(1)
    if CASE == "double" and WID in ("gw0", "gw1") and _STATE["started"] == 3:
        os._exit(1)
    if CASE == "exhausted" and not grouped:
        if _STATE["plain"] == (2 if _number(WID) < 3 else 1):
            os._exit(1)


# --- controller side ---

def _sched():
    dsession = _CONFIG[0].pluginmanager.getplugin("dsession")
    return dsession, dsession.sched


def _pending(workload):
    return sum(list(unit.values()).count(False) for unit in workload.values())


def _grouped_pending(workload):
    return sum(1 for unit in workload.values() for nid, done in unit.items()
               if not done and nid.rfind("@") > nid.rfind("]"))


def _completed_units(workload):
    return sum(1 for unit in workload.values() if unit and all(unit.values()))


def _live(sched, node):
    return {other.gateway.id: work for other, work in sched.assigned_work.items()
            if other is not node}


def pytest_testnodeready(node):
    if WID is not None:
        return
    _append("evidence.jsonl", {"event": "ready", "worker": node.gateway.id})
    (DIR / ("ready-" + node.gateway.id)).touch()


def pytest_xdist_node_collection_finished(node, ids):
    if WID is not None:
        return
    _dsession, sched = _sched()
    waiting = sorted(other.gateway.id for other in sched.assigned_work
                     if other is not node and other not in sched.registered_collections)
    _append("evidence.jsonl", {"event": "collected", "worker": node.gateway.id,
                               "count": len(ids), "assigned_unregistered": waiting})
    population = DIR / "collection.json"
    if not population.exists():
        population.write_text(json.dumps(list(ids)), encoding="utf-8")
    (DIR / ("collected-" + node.gateway.id)).touch()


def pytest_testnodedown(node, error):
    if WID is not None:
        return
    dsession, sched = _sched()
    workload = sched.assigned_work.get(node)
    live = _live(sched, node)
    _append("evidence.jsonl", {
        "event": "down", "worker": node.gateway.id,
        "death": error is not None,
        "failed_before": dsession._failed_nodes_count,
        "in_assigned": workload is not None,
        "completed_units": _completed_units(workload or {}),
        "pending": _pending(workload or {}),
        "queue": len(sched.workqueue),
        "live_pending": {wid: _pending(work) for wid, work in live.items()},
        "live_grouped_pending": {wid: _grouped_pending(work) for wid, work in live.items()},
    })


def pytest_runtest_logreport(report):
    # The late case's trigger: an original worker that has completed at least
    # 10 units while units are still queued is told to die at its next item.
    if WID is not None or CASE != "late" or report.when != "teardown":
        return
    node = getattr(report, "node", None)
    if node is None or node.gateway.id != "gw0" or (DIR / "kill-gw0").exists():
        return
    _dsession, sched = _sched()
    workload = sched.assigned_work.get(node, {})
    if _completed_units(workload) >= 10 and sched.workqueue:
        _append("evidence.jsonl", {"event": "trigger", "worker": "gw0",
                                   "completed_units": _completed_units(workload),
                                   "queue": len(sched.workqueue)})
        (DIR / "kill-gw0").touch()
'''

GROUPED = '''
import time

import pytest

''' + "".join(f'''
@pytest.mark.xdist_group("g{g}")
@pytest.mark.parametrize("i", range(5))
def test_g{g}(i):
    time.sleep(0.01)
''' for g in "abc")

PLAIN = '''
import time

import pytest


@pytest.mark.parametrize("i", range(120))
def test_plain(i):
    time.sleep(0.02)
'''

BIG = f'''
import time

import pytest


@pytest.mark.xdist_group("big")
@pytest.mark.parametrize("i", range({BIG_GROUP_ITEMS}))
def test_big(i):
    time.sleep(1)
'''

# Named to sort last, so its unit is the last one queued: in the tail case it
# is the last item its worker runs.
LONG = '''
import os
import time

import pytest


@pytest.mark.timeout(3, method="thread")
def test_long():
    if os.environ["REC_CASE"] == "tail":
        time.sleep(10)
'''

CASES = ("tail", "late", "double", "collection-death", "healthy", "exhausted")


def _build_suite(suite: Path, case: str) -> None:
    tests = suite / "tests"
    tests.mkdir(parents=True)
    (suite / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tests / "conftest.py").write_text(RECORDER, encoding="utf-8")
    (tests / "test_grouped.py").write_text(GROUPED, encoding="utf-8")
    (tests / "test_plain.py").write_text(PLAIN, encoding="utf-8")
    (tests / "test_zz_long.py").write_text(LONG, encoding="utf-8")
    if case == "exhausted":
        (tests / "test_big.py").write_text(BIG, encoding="utf-8")


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _starts(evidence: Path) -> dict[str, list[str]]:
    """node id -> the workers that started it, in start order per worker."""
    started: dict[str, list[str]] = {}
    for path in sorted(evidence.glob("starts-gw*")):
        wid = path.name[len("starts-"):]
        for nodeid in path.read_text(encoding="utf-8").splitlines():
            started.setdefault(nodeid, []).append(wid)
    return started


def _last_start(evidence: Path, wid: str) -> str | None:
    path = evidence / f"starts-{wid}"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return lines[-1] if lines else None


def _durations_rows(durations: Path) -> list[dict]:
    if not durations.exists():
        return []
    with gzip.open(durations, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _footer_exit(durations: Path):
    footers = [row for row in _durations_rows(durations) if row.get("footer")]
    return footers[-1]["exitStatus"] if footers else "no footer"


def _run_nested(case: str, tmp_path: Path):
    import _lib_test_selection as selection

    suite = tmp_path / "suite"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _build_suite(suite, case)
    durations = tmp_path / "durations.jsonl.gz"
    bulk, _serial = selection.pytest_legs(REPO, [], jobs=3, targets=[str(suite / "tests")])
    argv = [sys.executable, "-m", "pytest", *bulk, "--rootdir", str(suite),
            "-p", "tests._pytest_durations_plugin", "--basetemp", str(tmp_path / "bt")]
    if case == "exhausted":
        argv.append("--max-worker-restart=1")
    env = {"HOME": str(tmp_path / "home"), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "PYTHONPATH": str(REPO), "REC_DIR": str(evidence), "REC_CASE": case,
           "REC_WAIT": str(HANDSHAKE_WAIT),
           "CCTALLY_DURATIONS_PATH": str(durations), "CCTALLY_DURATIONS_LEG": "pytest"}
    started = time.monotonic()
    proc = subprocess.Popen(argv, cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    killed = False
    try:
        out, _ = proc.communicate(timeout=RUN_BOUND)
    except subprocess.TimeoutExpired:
        killed = True
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate(timeout=REAP_BOUND)
    ended_at = time.time()
    text = out.decode("utf-8", errors="replace")
    result = {
        "rc": proc.returncode, "killed": killed, "elapsed": time.monotonic() - started,
        "ended_at": ended_at, "text": text, "evidence": evidence, "durations": durations,
        "failed": [line for line in text.splitlines() if line.startswith("FAILED ")],
    }
    lines = text.splitlines()
    result["diag"] = (f"case={case} rc={proc.returncode} killed={killed} "
                      f"elapsed={result['elapsed']:.1f}s internalerror={'INTERNALERROR' in text}\n"
                      + "\n".join(lines[-40:]))
    return result


def _assert_population_started_once(run, *, skip=()):
    population = json.loads((run["evidence"] / "collection.json").read_text(encoding="utf-8"))
    started = _starts(run["evidence"])
    wrong = {nid: started.get(nid, []) for nid in population
             if nid not in skip and len(started.get(nid, [])) != 1}
    assert not wrong, f"items not started exactly once: {wrong}\n{run['diag']}"
    return population, started


@pytest.mark.parametrize("case", CASES)
def test_worker_death_subprocess(case, tmp_path):
    """A worker death in a real `-n 3 --dist loadgroup` session ends in its
    bounded, classified outcome (#1086).

    Detects, per case: tail, late and double, a leg that stalls to the run's
    deadline or ends in `INTERNALERROR` after a death (the crash item lost,
    other items never run); exhausted, a leg that exhausts the restart limit
    and drains a survivor's held group, or ends exit 1 as if complete;
    collection-death, a worker death with no item that finishes green;
    healthy, a gate that fails a leg whose workers all finished normally. The
    argv is the one `pytest_legs` builds, so the plugin's wiring is checked
    too.
    """
    run = _run_nested(case, tmp_path)
    diag = run["diag"]
    evidence = run["evidence"]
    unmet = _jsonl(evidence / "unmet.jsonl")
    assert not unmet, f"unmet precondition: {unmet}\n{diag}"
    assert not run["killed"], f"the nested run did not end inside {RUN_BOUND} s\n{diag}"
    records = _jsonl(evidence / "evidence.jsonl")
    deaths = [r for r in records if r["event"] == "down" and r["death"]]
    text = run["text"]
    banner = [line for line in text.splitlines() if "xdist recovery (#1086):" in line]
    assert "INTERNALERROR" not in text, diag

    if case == "tail":
        assert run["rc"] == 1, diag
        assert len(run["failed"]) == 1 and "test_zz_long.py::test_long" in run["failed"][0], diag
        assert len(deaths) == 1, (deaths, diag)
        # The recorded precondition: the long test was its worker's last item.
        assert _last_start(evidence, deaths[0]["worker"]) == "tests/test_zz_long.py::test_long", diag
        _assert_population_started_once(run)
        assert _footer_exit(run["durations"]) == 1, diag
    elif case == "late":
        trigger = [r for r in records if r["event"] == "trigger"]
        assert trigger, f"the trigger never fired\n{diag}"
        assert len(deaths) == 1, (deaths, diag)
        death = deaths[0]
        assert death["completed_units"] >= 10 and death["pending"] >= 1 and death["queue"] > 0, (
            death, diag)
        assert run["rc"] == 1, diag
        assert len(run["failed"]) == 1 and _last_start(evidence, "gw0") in run["failed"][0], diag
        _assert_population_started_once(run)
    elif case == "double":
        collected = [r for r in records if r["event"] == "collected" and r["worker"] == "gw3"]
        assert collected and "gw4" in collected[0]["assigned_unregistered"], (collected, diag)
        assert sorted(d["worker"] for d in deaths) == ["gw0", "gw1"], (deaths, diag)
        assert run["rc"] == 1, diag
        assert len(run["failed"]) == 2, diag
        for wid in ("gw0", "gw1"):
            assert any(_last_start(evidence, wid) in line for line in run["failed"]), (wid, diag)
        _assert_population_started_once(run)
    elif case == "collection-death":
        assert [d["worker"] for d in deaths] == ["gw1"], (deaths, diag)
        assert run["rc"] == 3, diag
        assert not run["failed"], diag
        assert banner and "gw1" in banner[0], diag
        population, _started = _assert_population_started_once(run)
        calls = {row["nodeId"]: row["outcome"] for row in _durations_rows(run["durations"])
                 if row.get("phase") == "call"}
        assert len(calls) == len(population) and set(calls.values()) == {"passed"}, diag
        assert _footer_exit(run["durations"]) == 3, diag
    elif case == "healthy":
        assert run["rc"] == 0, diag
        assert not deaths and not banner, diag
        population, started = _assert_population_started_once(run)
        for group in ("ga", "gb", "gc"):
            workers = {w for nid in population if nid.endswith("@" + group)
                       for w in started[nid]}
            assert len(workers) == 1, (group, workers, diag)
    elif case == "exhausted":
        exhausting = [d for d in deaths if d["failed_before"] == 1]
        assert exhausting, (deaths, diag)
        held = max(exhausting[0]["live_grouped_pending"].values(), default=0)
        assert held >= BIG_GROUP_ITEMS // 2, (exhausting[0], diag)
        assert run["rc"] == 3, diag
        assert run["ended_at"] - exhausting[0]["at"] <= EXHAUSTED_END_BOUND, (
            run["ended_at"] - exhausting[0]["at"], diag)
        started = _starts(evidence)
        big = [nid for nid in started if "test_big.py::" in nid]
        assert len(big) < BIG_GROUP_ITEMS // 2, (len(big), diag)
        assert all(len(workers) == 1 for workers in started.values()), diag
        # The summary `_abort` writes names each crash item once: the item each
        # worker that died up to the exhausting death last started.
        crash_items = [_last_start(evidence, d["worker"]) for d in deaths
                       if d["at"] <= exhausting[0]["at"]]
        assert len(run["failed"]) == len(crash_items), (crash_items, diag)
        for item in crash_items:
            assert sum(item in line for line in run["failed"]) == 1, (item, diag)
        assert banner, diag
        assert _footer_exit(run["durations"]) == 3, diag
