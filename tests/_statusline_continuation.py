"""Test dispatchers for the #825/#832 statusline continuation (spec 4.3
revision 2, "Continuation ownership" and "Dependency waiter").

Production dispatches the continuation worker and each dependency waiter as
detached `cctally _statusline-continue` processes that inherit their
reservation flock. In-process tests replace that one seam,
`_cctally_statusline._spawn_continuation(kind, reservation_fd, argument)`:

* `tests/_script_loader.py` installs the default for every in-process test:
  a worker runs synchronously in the calling thread and a waiter never runs
  (its reservation is released at once), so no test is left with a thread
  blocked on one of its own locks;
* `Dispatch` runs both kinds in threads, records every spawn, can hold
  workers at a gate, and treats a worker that raises as a process that died:
  it closes the reservation descriptor, which is what the operating system
  does for a dead holder.

A test that uses `Dispatch` must release every lock it holds and `join()`
before it ends, so no waiter thread outlives the test's store.
"""
from __future__ import annotations

import os
import threading
import time


def _close(fd) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class Dispatch:
    def __init__(self, monkeypatch, *, gate: "threading.Event | None" = None):
        import _cctally_statusline as sl
        self._sl = sl
        self._gate = gate
        self._lock = threading.Lock()
        self.spawns: list = []
        self.errors: list = []
        self._threads: list = []
        self._kinds: list = []
        monkeypatch.setattr(sl, "_spawn_continuation", self.spawn, raising=False)

    def spawn(self, kind, fd, argument=None):
        with self._lock:
            self.spawns.append((kind, argument))
        thread = threading.Thread(target=self._run, args=(kind, fd, argument),
                                  daemon=True)
        with self._lock:
            self._threads.append(thread)
            self._kinds.append((kind, thread))
        thread.start()
        return True

    def _run(self, kind, fd, argument):
        try:
            if kind == "worker" and self._gate is not None:
                self._gate.wait(30)
            self._sl._run_spawned_continuation(kind, fd, argument)
        except BaseException as exc:  # noqa: BLE001 — a dead process
            _close(fd)
            with self._lock:
                self.errors.append(exc)

    def count(self, kind, argument=None) -> int:
        with self._lock:
            return sum(1 for k, a in self.spawns
                       if k == kind and (argument is None or a == argument))

    def join(self, timeout: float = 60.0) -> None:
        """Wait for every spawned thread, including the ones they spawn."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                alive = [t for t in self._threads if t.is_alive()]
            if not alive:
                return
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"continuation threads still running: {alive}"
            alive[0].join(min(remaining, 1.0))

    def settle(self, kind: str, timeout: float = 60.0) -> None:
        """Wait for every spawned thread of ``kind`` alone: a waiter blocked
        on a lock the test still holds would never let `join()` return."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                alive = [t for k, t in self._kinds if k == kind and t.is_alive()]
            if not alive:
                return
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{kind} threads still running: {alive}"
            alive[0].join(min(remaining, 1.0))
