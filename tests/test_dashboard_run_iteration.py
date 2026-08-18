"""#583 S2 — the sync loop's one iteration, including its queued-refresh leg.

Spec section 4: when the loop services a batch whose refresh bit is set, it
performs one OAuth refresh immediately before one rebuild, holding `sync_lock`
across both. That is where Preserve 9's rule now lives — moving it out of the
handler is what stops the periodic thread firing a redundant rebuild between
the two steps — and it was the only part of the refresh contract no test
reached, so acceptance criterion 2's core mechanism rested on inspection alone.

The iteration body was a closure inside `cmd_dashboard`. It is now built by a
module-level factory, mirroring `_make_run_sync_now_locked`, so these
assertions are made against the code the dashboard runs.
"""
import importlib
import threading

import pytest

from conftest import load_script, redirect_paths


class _RecordingLock:
    """A real lock that records its own acquire/release ordering.

    The point of the queued-refresh leg is that the lock is held ACROSS both
    steps with no release between them, so the assertion has to be about
    ordering rather than about `locked()` at two sampled instants.
    """

    def __init__(self, events):
        self._lock = threading.Lock()
        self._events = events

    def __enter__(self):
        self._lock.acquire()
        self._events.append("acquire")
        return self

    def __exit__(self, *exc):
        self._events.append("release")
        self._lock.release()
        return False

    def locked(self):
        return self._lock.locked()


@pytest.fixture
def mods(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = importlib.import_module("_cctally_dashboard")
    return ns, dash


def _wire(dash, monkeypatch, *, skip_sync=False, refresh_status="ok"):
    """Build the iteration with spies for both legs. Returns (fn, events)."""
    events = []
    lock = _RecordingLock(events)

    class _Result:
        status = refresh_status

    def _refresh(*a, **kw):
        events.append(("refresh", lock.locked()))
        return _Result()

    def _locked(skip_sync=False):
        events.append(("rebuild_locked", lock.locked()))

    def _public(skip_sync=False):
        events.append(("rebuild_public", lock.locked()))

    monkeypatch.setattr(dash, "_refresh_usage_inproc", _refresh)
    monkeypatch.setattr(dash, "_dashboard_self_heal_orphans",
                        lambda **kw: events.append("heal"))
    fn = dash._make_dashboard_run_iteration(
        sync_lock=lock,
        run_sync_now=_public,
        run_sync_now_locked=_locked,
        skip_sync=skip_sync,
        monotonic=lambda: 0.0,          # never reaches the 60s heal cadence
    )
    return fn, events


def test_a_queued_refresh_runs_one_oauth_call_then_one_rebuild_under_one_lock(
        mods, monkeypatch):
    ns, dash = mods
    fn, events = _wire(dash, monkeypatch)

    out = fn(batch=(7, True))

    assert events == [
        "acquire",
        ("refresh", True),
        ("rebuild_locked", True),
        "release",
    ], events
    assert out == {"warnings": []}


def test_a_degraded_queued_refresh_carries_its_warning_to_the_settlement(
        mods, monkeypatch):
    """A queued request has no HTTP response, so the warning rides the
    settlement frame instead (spec section 4, deferred warnings)."""
    ns, dash = mods
    fn, events = _wire(dash, monkeypatch, refresh_status="rate_limited")

    out = fn(batch=(7, True))

    assert out == {"warnings": [{"code": "rate_limited"}]}


def test_a_batch_without_the_refresh_bit_never_calls_oauth(mods, monkeypatch):
    ns, dash = mods
    fn, events = _wire(dash, monkeypatch)

    fn(batch=(7, False))

    assert events == [("rebuild_public", False)]


def test_an_automatic_tick_never_calls_oauth(mods, monkeypatch):
    ns, dash = mods
    fn, events = _wire(dash, monkeypatch)

    fn()

    assert events == [("rebuild_public", False)]


def test_skip_sync_suppresses_the_refresh_leg_of_a_queued_batch(
        mods, monkeypatch):
    """--no-sync freezes data, so a refresh bit that reached the loop anyway
    must not perform a network call."""
    ns, dash = mods
    fn, events = _wire(dash, monkeypatch, skip_sync=True)

    fn(batch=(7, True))

    assert events == [("rebuild_public", False)]


def test_the_orphan_self_heal_runs_on_its_own_cadence_inside_the_iteration(
        mods, monkeypatch):
    """It is inside the MEASURED iteration so its cost counts toward the
    cooldown deadline (#313 P2 / F10)."""
    ns, dash = mods
    events = []
    clock = [0.0]
    monkeypatch.setattr(dash, "_dashboard_self_heal_orphans",
                        lambda **kw: events.append("heal"))
    fn = dash._make_dashboard_run_iteration(
        sync_lock=threading.Lock(),
        run_sync_now=lambda skip_sync=False: events.append("rebuild"),
        run_sync_now_locked=lambda skip_sync=False: None,
        skip_sync=False,
        monotonic=lambda: clock[0],
    )

    fn()
    assert events == ["rebuild"]        # far short of the 60s cadence
    clock[0] = 61.0
    fn()
    assert events == ["rebuild", "rebuild", "heal"]
    clock[0] = 62.0
    fn()
    assert events == ["rebuild", "rebuild", "heal", "rebuild"]


def test_skip_sync_suppresses_the_orphan_self_heal(mods, monkeypatch):
    ns, dash = mods
    events = []
    monkeypatch.setattr(dash, "_dashboard_self_heal_orphans",
                        lambda **kw: events.append("heal"))
    fn = dash._make_dashboard_run_iteration(
        sync_lock=threading.Lock(),
        run_sync_now=lambda skip_sync=False: events.append("rebuild"),
        run_sync_now_locked=lambda skip_sync=False: None,
        skip_sync=True,
        monotonic=lambda: 1000.0,
    )

    fn()

    assert events == ["rebuild"]
