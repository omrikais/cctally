"""Runtime arming of the phase trace, and the isolation that makes it safe.

Spec §2 (#583 S1). Three hazards, one file:

1. **A2 thread-local corruption.** `sync_cache` opens its `walk` phase, calls
   the A2 progress callback while that phase is still open, and closes it
   afterwards. The partial build reaches `_tui_build_snapshot_once`'s
   unconditional `reset_thread()`, which rebinds `_tls.stack` while each open
   `Phase` still holds the original list in `Phase._stack` — so later phases
   attach elsewhere and the outer phase closes into a detached root.
   `isolated_thread_state()` is what replaces the blanket suppression.
2. **A lost arm request.** An unsynchronised test-and-clear drops a request
   arriving between the read and the clear.
3. **Cross-thread flips.** `_ENABLED` is process-global and HTTP handlers open
   their own roots concurrently, so a flip mid-request would nest traced phases
   under an untraced root, or truncate a traced one.
"""
import pathlib
import sys
import threading

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


@pytest.fixture
def perf():
    import _lib_perf
    saved = _lib_perf.enabled()
    _lib_perf.reset_thread()
    try:
        yield _lib_perf
    finally:
        _lib_perf.request_enabled(saved)
        _lib_perf.apply_pending()
        _lib_perf.set_enabled(saved)
        _lib_perf.reset_thread()


# ── §2.1 isolated_thread_state() ────────────────────────────────────────────


def test_isolated_thread_state_restores_the_exact_objects(perf):
    """By reference, not by value: `Phase._stack` holds the original list."""
    perf.set_enabled(True)
    perf.reset_thread()
    outer = perf.phase("outer")
    outer.__enter__()
    saved_stack = perf._tls.stack
    saved_root = perf.current_root()

    with perf.isolated_thread_state():
        assert perf._tls.stack is not saved_stack, (
            "the body must not see the caller's stack")
        assert perf._tls.stack == []
        perf.reset_thread()
        with perf.phase("inner-build"):
            pass

    assert perf._tls.stack is saved_stack, "the SAME list object must return"
    assert perf.current_root() is saved_root
    outer.__exit__(None, None, None)
    root = perf.current_root()
    assert root is outer, (
        "the outer phase closed into a detached root: the isolation leaked")
    assert [c.name for c in root.children] == [], (
        "the isolated build's phases attached to the caller's tree")


def test_isolated_thread_state_restores_after_an_exception(perf):
    perf.set_enabled(True)
    perf.reset_thread()
    outer = perf.phase("outer")
    outer.__enter__()
    saved_stack = perf._tls.stack

    with pytest.raises(RuntimeError):
        with perf.isolated_thread_state():
            perf.reset_thread()
            raise RuntimeError("the partial build failed")

    assert perf._tls.stack is saved_stack
    outer.__exit__(None, None, None)
    assert perf.current_root() is outer


def test_isolated_thread_state_restores_the_captured_arm(perf):
    """The captured root arm is thread state too, so it restores with the rest."""
    perf.set_enabled(True)
    perf.reset_thread()
    before = perf.root_armed()
    with perf.isolated_thread_state():
        perf.set_enabled(False)
        perf.reset_thread()
        assert perf.root_armed() is False
    assert perf.root_armed() is before
    perf.set_enabled(True)


# ── §2.2 the arming mailbox ─────────────────────────────────────────────────


def test_a_request_is_not_applied_until_apply_pending_runs(perf):
    perf.set_enabled(False)
    perf.request_enabled(True)
    assert perf.enabled() is False, "the request must not flip anything itself"
    requested, applied = perf.pending_state()
    assert (requested, applied) == (True, False)
    assert perf.apply_pending() is True
    assert perf.enabled() is True
    assert perf.pending_state() == (True, True)


def test_apply_pending_consumes_the_request_exactly_once(perf):
    perf.set_enabled(False)
    perf.request_enabled(True)
    assert perf.apply_pending() is True
    perf.set_enabled(False)          # a flip from somewhere else
    assert perf.apply_pending() is False, (
        "the mailbox still held a consumed request")
    assert perf.enabled() is False


def test_the_latest_request_wins(perf):
    perf.set_enabled(False)
    perf.request_enabled(True)
    perf.request_enabled(False)
    assert perf.pending_state()[0] is False
    assert perf.apply_pending() is False, "no change: False was already applied"
    assert perf.enabled() is False


def test_no_request_racing_apply_pending_is_dropped(perf):
    """An unsynchronised test-and-clear loses a request landing between the
    read and the clear — the race `_SnapshotRef.take_sync_request` documents."""
    perf.set_enabled(False)
    stop = threading.Event()
    last = {"value": False}
    lock = threading.Lock()

    def writer():
        for i in range(4000):
            value = bool(i % 2)
            with lock:
                perf.request_enabled(value)
                last["value"] = value
        stop.set()

    def applier():
        while not stop.is_set():
            perf.apply_pending()

    threads = [threading.Thread(target=writer), threading.Thread(target=applier)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with lock:
        want = last["value"]
        perf.apply_pending()
    assert perf.enabled() is want, (
        "a request was consumed without ever being applied")


# ── §2.2 per-root capture ───────────────────────────────────────────────────


def test_a_root_opened_while_disarmed_stays_wholly_untraced(perf):
    """Arming mid-request must not nest a traced phase under an untraced root."""
    perf.set_enabled(False)
    perf.reset_thread()                       # the root opens, capturing OFF
    with perf.phase("endpoint.assemble"):
        perf.set_enabled(True)                # a flip from another thread
        with perf.phase("assemble") as inner:
            inner.set_meta(k=1)
    assert perf.current_root() is None, (
        "a phase was created beneath an untraced root, making a bogus root")


def test_a_root_opened_while_armed_stays_wholly_traced(perf):
    """Disarming mid-request must not truncate a tree that already started."""
    perf.set_enabled(True)
    perf.reset_thread()                       # the root opens, capturing ON
    with perf.phase("endpoint.assemble"):
        perf.set_enabled(False)               # a flip from another thread
        with perf.phase("assemble"):
            pass
    root = perf.current_root()
    assert root is not None, "the root vanished mid-tree"
    assert [c.name for c in root.children] == ["assemble"], (
        f"the later child was dropped: {[c.name for c in root.children]}")


def test_a_thread_with_no_root_open_reads_the_global(perf):
    """The fallback the spec names: no root, so `_ENABLED` decides."""
    import _lib_perf
    perf.set_enabled(False)

    seen = {}

    def worker():
        # A fresh thread has captured nothing at all.
        seen["armed"] = _lib_perf.root_armed()
        _lib_perf.set_enabled(True)
        seen["phase_is_real"] = type(_lib_perf.phase("x")).__name__ == "Phase"

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["armed"] is None
    assert seen["phase_is_real"] is True
