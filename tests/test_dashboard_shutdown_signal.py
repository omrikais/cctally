"""Regression tests for the dashboard's lost-wakeup-proof shutdown wait (#154).

`cmd_dashboard` used to block its main thread on `threading.Event.wait()`,
woken only by a SIGINT/SIGTERM handler calling `stop.set()`. CPython can
lose a *single* signal that races the entry into `Event.wait()` — the
Python-level handler never runs, or `set()`'s `notify_all()` fires before
the waiter registers — so ~0.04-0.07% of single SIGTERMs failed to wake the
loop and recovery needed a *second* signal (proven during #153 triage).

The fix replaces the Event with a self-pipe wakeup fd (`signal.set_wakeup_fd`):
CPython's C-level signal trampoline writes the signum to the pipe on EVERY
delivery, *before* (and independent of) the Python-level handler running, so
a `select` on the read end unblocks on the very first signal.

These tests drive the real `_dashboard_wait_for_signal` helper directly. The
load-bearing one — `test_wakeup_does_not_depend_on_handler_body` — passes
`on_signal=None` so the Python-level handler does nothing useful: it
DETERMINISTICALLY proves the wakeup survives a handler that never sets a
flag, which is exactly the worst case of the race. Against any
handler-dependent mechanism (the old `Event`-based wait) that assertion
times out → `False` → fails; against the self-pipe it returns `True`.

A finite `timeout` is passed everywhere so a regressed mechanism FAILS LOUDLY
instead of hanging the suite.
"""
import signal
import sys
import threading
import time

import pytest

from conftest import load_script


def _wait_fn():
    """Fetch the real helper after loading the script (sibling needs `cctally`)."""
    load_script()
    return sys.modules["_cctally_dashboard"]._dashboard_wait_for_signal


def _fire_signal_after(sig, delay):
    """Background thread: deliver `sig` to this process after `delay` seconds.

    The main thread arms the wakeup fd + handler synchronously at the top of
    `_dashboard_wait_for_signal` (microseconds), well before this delay, so
    the signal always lands AFTER the fd is armed — even if it arrives before
    `select`, the buffered byte still wakes it.
    """
    def _run():
        time.sleep(delay)
        signal.raise_signal(sig)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="set_wakeup_fd / signal.signal require the main thread",
)
def test_wakeup_does_not_depend_on_handler_body():
    """The load-bearing non-vacuity test (#154 acceptance criterion 1).

    `on_signal=None` means the Python-level handler does nothing toward
    waking the loop — modelling 'the handler never ran' / 'set() lost the
    race'. The self-pipe wakeup must STILL unblock from the C-level byte.
    An Event-based wait would block until the timeout here → return False.
    """
    wait = _wait_fn()
    saved_term = signal.getsignal(signal.SIGTERM)
    try:
        _fire_signal_after(signal.SIGTERM, 0.05)
        woke = wait((signal.SIGTERM,), on_signal=None, timeout=5.0)
        assert woke is True
    finally:
        signal.signal(signal.SIGTERM, saved_term)


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="set_wakeup_fd / signal.signal require the main thread",
)
def test_secondary_on_signal_callback_still_fires():
    """`on_signal` (belt-and-suspenders) is invoked when a signal arrives."""
    wait = _wait_fn()
    saved_term = signal.getsignal(signal.SIGTERM)
    fired = threading.Event()
    try:
        _fire_signal_after(signal.SIGTERM, 0.05)
        woke = wait((signal.SIGTERM,), on_signal=fired.set, timeout=5.0)
        assert woke is True
        # The Python-level handler ran in addition to the C-level wakeup.
        assert fired.wait(1.0) is True
    finally:
        signal.signal(signal.SIGTERM, saved_term)


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="set_wakeup_fd / signal.signal require the main thread",
)
def test_returns_false_on_timeout_when_no_signal():
    """No signal → the wait times out and reports it (proves True is meaningful)."""
    wait = _wait_fn()
    saved_int = signal.getsignal(signal.SIGINT)
    saved_term = signal.getsignal(signal.SIGTERM)
    try:
        woke = wait((signal.SIGINT, signal.SIGTERM), on_signal=None, timeout=0.2)
        assert woke is False
    finally:
        signal.signal(signal.SIGINT, saved_int)
        signal.signal(signal.SIGTERM, saved_term)


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="set_wakeup_fd / signal.signal require the main thread",
)
def test_restores_prior_handlers_and_wakeup_fd():
    """The helper must leave global signal state exactly as it found it.

    Otherwise it would clobber pytest's own SIGINT handling and leak a
    dangling wakeup fd into every later test on this worker.
    """
    wait = _wait_fn()
    sentinel_int = signal.getsignal(signal.SIGINT)
    sentinel_term = signal.getsignal(signal.SIGTERM)
    # set_wakeup_fd returns the prior fd; -1 means "none". Round-trip to read
    # the current value without disturbing it.
    prior_wakeup = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(prior_wakeup)
    try:
        woke = wait((signal.SIGINT, signal.SIGTERM), on_signal=None, timeout=0.1)
        assert woke is False
        assert signal.getsignal(signal.SIGINT) is sentinel_int
        assert signal.getsignal(signal.SIGTERM) is sentinel_term
        now_wakeup = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(prior_wakeup if prior_wakeup != -1 else -1)
        assert now_wakeup == prior_wakeup
    finally:
        signal.signal(signal.SIGINT, sentinel_int)
        signal.signal(signal.SIGTERM, sentinel_term)
        signal.set_wakeup_fd(prior_wakeup)


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="set_wakeup_fd / signal.signal require the main thread",
)
def test_single_sigterm_stress():
    """Realistic regression: many single-SIGTERM cycles, each must wake once.

    Cheap with the self-pipe (the buffered byte makes every cycle
    deterministic); the modest count keeps suite cost ~1s while exercising
    the end-to-end arm → fire → select → drain → restore loop repeatedly.
    """
    wait = _wait_fn()
    saved_term = signal.getsignal(signal.SIGTERM)
    try:
        for i in range(20):
            _fire_signal_after(signal.SIGTERM, 0.01)
            assert wait((signal.SIGTERM,), on_signal=None, timeout=5.0) is True, (
                f"single SIGTERM lost on iteration {i}"
            )
    finally:
        signal.signal(signal.SIGTERM, saved_term)
