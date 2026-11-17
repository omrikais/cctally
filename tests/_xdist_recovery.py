"""Recover a pytest-xdist worker death in a `--dist loadgroup` leg (#1086).

Loaded with `-p tests._xdist_recovery` on every xdist leg the suite builds
(bin/_lib_test_selection.py `_xdist`), from the repository root, the same way
as tests/_pytest_durations_plugin.py. A direct `pytest -n N --dist loadgroup`
opts in with the same `-p`; tests/conftest.py does not load it.

Under pytest-xdist 3.8.0 `LoadGroupScheduling` (through `LoadScopeScheduling`)
mishandles a worker death:

* `remove_node` re-queues every unit the dead worker was ever given, completed
  ones included. A unit with nothing left to run is sent as an empty index
  list, which the worker ignores, so the node receiving it never reports again
  and the leg stalls until the run's deadline.
* `_reschedule` assigns one unit per call, so a replacement can be left holding
  a single item, which a worker never starts while more work is queued.
* a replacement is in `assigned_work` before its collection registers, and a
  reschedule in that window raises `KeyError` (an `INTERNALERROR`, or a lost
  unit and crash report when `worker_errordown` swallows it).
* the crash item stays pending and runs again, so a deterministic crasher can
  kill one replacement after another.
* the first schedule's `extra_nodes` step can retire an uncollected replacement.
* a send to a worker that has gone while its death event is still queued (the
  second death of a pair, rescheduled from the first) raises execnet's
  `OSError` out of the scheduler, another `INTERNALERROR`.

`RecoveringLoadGroupScheduling` corrects these, and `OutcomeGate` makes every
other worker-death outcome a classified failure: exhausting the restart limit
aborts the leg at once (exit 3) instead of draining the survivors' held work,
and a leg that ends with items unrun, or after a worker died holding no item,
exits 3 too. Healthy legs are unchanged.

The module imports without xdist, and on a worker (which re-applies `-p`
plugins) or in a non-distributed session every hook does nothing. The version
guard confines it to the audited pytest-xdist release.
"""
from __future__ import annotations

from typing import NoReturn

import pytest

# The pytest-xdist release this module's coupling was audited against: the
# scheduler internals it overrides, `DSession.loop_once`'s `getattr` dispatch
# of `worker_*` events, and `worker_errordown`'s restart accounting. The pin in
# tests/requirements-dev.txt changes with it.
SUPPORTED_XDIST = "3.8.0"

_REASON_PREFIX = "xdist recovery (#1086):"
_CLASS: list[type] = []


def scheduler_class() -> type:
    """`RecoveringLoadGroupScheduling`, defined on first use (needs xdist)."""
    if _CLASS:
        return _CLASS[0]
    from xdist.scheduler import LoadGroupScheduling

    class RecoveringLoadGroupScheduling(LoadGroupScheduling):
        """`LoadGroupScheduling` whose worker-death path loses no work."""

        def __init__(self, config, log=None):
            super().__init__(config, log)
            # Nodes that refused a send: gone, their death event still queued.
            self.unreachable: set = set()

        def remove_node(self, node):
            workload = self.assigned_work.pop(node)
            self.unreachable.discard(node)
            if not self._pending_of(workload):
                # A normal finish (`worker_workerfinished`) or a death holding
                # nothing: no crash item. Deaths are attributed by the gate.
                return None
            # The first pending item in assignment order is the one the worker
            # was running, as upstream decides it. It is terminal: DSession
            # reports it once as failed and it never runs again.
            crashitem = next(nodeid for unit in workload.values()
                             for nodeid, completed in unit.items() if not completed)
            for unit in workload.values():
                if crashitem in unit:
                    unit[crashitem] = True
                    break
            # Only units that still hold pending items go back on the queue; a
            # group's pending remainder stays one unit, so it moves whole.
            for scope, unit in workload.items():
                if not all(unit.values()):
                    self.workqueue[scope] = unit
            for other in list(self.assigned_work):
                self._reschedule(other)
            return crashitem

        def _reschedule(self, node):
            if node.shutting_down or node in self.unreachable:
                return
            if not self.workqueue:
                node.shutdown()
                return
            if self._pending_of(self.assigned_work[node]) > 2:
                return
            if node not in self.registered_collections:
                # Scheduled through `schedule()` once its collection registers.
                return
            # A node holding a single item never runs it while work is queued
            # (a worker starts an item only once it knows the next one), so
            # top up to two pending items.
            self._assign_work_unit(node)
            # A refused send marks the node unreachable and assigns nothing,
            # so the top-up stops there.
            while (self.workqueue and node not in self.unreachable
                   and self._pending_of(self.assigned_work[node]) < 2):
                self._assign_work_unit(node)

        def _assign_work_unit(self, node):
            if node not in self.registered_collections or node in self.unreachable:
                return
            scope = next(iter(self.workqueue), None)  # upstream raises on empty
            try:
                super()._assign_work_unit(node)
            except OSError:
                # execnet refuses a send on a closed channel or a broken pipe:
                # the worker has gone and its death event is still queued. The
                # unit goes back to the head of the queue, so that death is
                # attributed by what the worker actually held (its running
                # item, or none), and nothing more is sent to it.
                self.workqueue[scope] = self.assigned_work[node].pop(scope)
                self.workqueue.move_to_end(scope, last=False)
                self.unreachable.add(node)

        def schedule(self):
            if self.collection is not None:
                super().schedule()
                return
            # The first schedule distributes over registered nodes only, so its
            # `extra_nodes` step never retires an uncollected replacement and
            # its first assignment never reaches one.
            aside = {node: work for node, work in self.assigned_work.items()
                     if node not in self.registered_collections}
            for node in aside:
                del self.assigned_work[node]
            try:
                super().schedule()
            finally:
                self.assigned_work.update(aside)

        def add_node_collection(self, node, collection):
            if self.collection is not None and list(collection) != self.collection:
                raise RuntimeError(
                    f"{_REASON_PREFIX} worker {node.gateway.id} registered after the "
                    "first schedule with a different test population than the session, "
                    "so it cannot take over queued work")
            super().add_node_collection(node, collection)

        def unfinished_count(self) -> int:
            """Items still queued or pending on a node."""
            queued = sum(list(unit.values()).count(False) for unit in self.workqueue.values())
            return queued + sum(self._pending_of(work) for work in self.assigned_work.values())

    _CLASS.append(RecoveringLoadGroupScheduling)
    return RecoveringLoadGroupScheduling


class OutcomeGate:
    """Turns an unrecoverable worker-death outcome into a classified exit 3."""

    def __init__(self, config) -> None:
        self.config = config
        self.itemless_deaths: list[str] = []

    def install(self, dsession) -> None:
        """Wrap the instance's `worker_errordown`, the one entry point of a
        death (`loop_once` dispatches through `getattr(self, "worker_...")`).
        A normal finish arrives as `worker_workerfinished` and never passes
        through here."""
        original = dsession.worker_errordown

        def worker_errordown(node, error):
            # `sched` is read now: xdist creates it inside its own loop.
            sched = dsession.sched
            workload = sched.assigned_work.get(node) if sched is not None else None
            if workload is None or not sched._pending_of(workload):
                self.itemless_deaths.append(node.gateway.id)
            original(node, error)
            if dsession.shouldstop:
                # An interrupt (a worker that exited with status 2 reaches here
                # after xdist set `shouldstop`) or a --maxfail stop: the loop
                # raises its own `Interrupted`, which must not be relabelled
                # as worker exhaustion.
                return
            limit = dsession._max_worker_restart
            if limit is not None and dsession._failed_nodes_count > limit:
                # Abort now: xdist's own shutdown would let every survivor run
                # what it already holds before the loop returns.
                self.abort(self.config, self._reason(dsession))

        dsession.worker_errordown = worker_errordown

    def check_end(self, dsession) -> None:
        """After a normal loop end: items left unrun, or a death with no item,
        is not a complete leg."""
        if _unfinished(dsession) > 0 or self.itemless_deaths:
            self.abort(self.config, self._reason(dsession))

    def _reason(self, dsession) -> str:
        itemless = ", ".join(self.itemless_deaths) or "none"
        return (f"{_REASON_PREFIX} {dsession._failed_nodes_count} crashed worker(s), "
                f"restart limit {dsession._max_worker_restart}; "
                f"{_unfinished(dsession)} item(s) not run; "
                f"worker(s) that died holding no item: {itemless}")

    @staticmethod
    def abort(config, reason: str) -> NoReturn:
        """Write the failure summary, then end the session with status 3.

        pytest prints its short test summary only for statuses other than 3,
        so this is the leg's only copy; its `FAILED` lines are what
        bin/cctally-test-all reads the crash identities from. The reason
        travels in the `Exit` alone, which pytest prints once as its exit
        banner."""
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.short_test_summary()
        pytest.exit(reason, returncode=pytest.ExitCode.INTERNAL_ERROR)


def _unfinished(dsession) -> int:
    counter = getattr(dsession.sched, "unfinished_count", None)
    return counter() if counter is not None else 0


def _loadgroup_controller(config) -> bool:
    if hasattr(config, "workerinput"):
        return False
    if not getattr(config.option, "numprocesses", None):
        return False
    return config.getvalue("dist") == "loadgroup"


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_make_scheduler(config, log):
    if config.getvalue("dist") != "loadgroup":
        return None
    return scheduler_class()(config, log)


def pytest_configure(config):
    if not _loadgroup_controller(config):
        return
    import xdist

    if xdist.__version__ != SUPPORTED_XDIST:
        raise pytest.UsageError(
            f"tests/_xdist_recovery.py supports pytest-xdist {SUPPORTED_XDIST}, but "
            f"{xdist.__version__} is installed. Changing the pin needs a source audit "
            "of the worker-crash path, of DSession.loop_once's dispatch the "
            "exhaustion abort relies on, and of this module's tests (#1086).")


@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    config = session.config
    dsession = config.pluginmanager.get_plugin("dsession")
    if dsession is None or not _loadgroup_controller(config):
        return (yield)
    gate = OutcomeGate(config)
    gate.install(dsession)
    # A loop that raised (an interrupt, --maxfail, an internal error, the
    # exhaustion abort's own Exit) propagates from here untouched.
    result = yield
    gate.check_end(dsession)
    return result
