"""#583 S4 (F5) — the conversation loop's duty bound, proven algebraically.

The dashboard runs two work loops. The main one has been bounded since #313.
The second one — transcript ingest — computed its interval once and ended every
pass with a fixed ``wait(interval)``, which prevents literal 100% duty for
finite work but provides no scale-independent ceiling below it: a pass costing
30 s ran 30-on/5-off, about 86% duty, and nothing bounded that as
``conversations.db`` grew.

No wall-clock assertion appears anywhere in this file. A virtual monotonic clock
advances only when the fake pass or the fake wait says it does, so every test
measures the scheduling algebra and never the machine (D-1).

``tests/test_dashboard_sync_cooldown.py`` owns the MAIN loop's contract; this
file is deliberately separate rather than an extension of it.
"""
import argparse
import importlib
import sqlite3
import sys
import threading

import pytest

from conftest import load_script, redirect_paths
from tests._support_http import PRESENCE_BACKSTOP_SECONDS


def _dash():
    load_script()  # sets sys.path so sibling modules import
    return importlib.import_module("_cctally_dashboard")


class _VirtualClock:
    """A monotonic clock nothing but this test advances."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Stop:
    """Stops the loop once the scripted passes are exhausted."""

    def __init__(self, remaining):
        self._remaining = remaining

    def is_set(self):
        return not self._remaining


def _run_loop(dash, *, work_costs, interval, record=None):
    clock = _VirtualClock()
    starts, works = [], []
    remaining = list(work_costs)
    stop = _Stop(remaining)

    def run_iteration():
        starts.append(clock.now)
        cost = remaining.pop(0)
        works.append(cost)
        clock.advance(cost)
        return "ok"

    def wait(seconds):
        clock.advance(max(0.0, seconds))
        return False

    dash._conversation_sync_loop(
        stop=stop,
        interval=interval,
        run_iteration=run_iteration,
        monotonic=clock.monotonic,
        thread_time_ns=lambda: 0,
        wait=wait,
        record=record if record is not None else (lambda **kw: None),
    )
    return starts, works


# --- criterion 1: the duty bound -------------------------------------------


def test_period_is_at_least_twice_work_when_work_exceeds_interval():
    dash = _dash()
    starts, works = _run_loop(
        dash, work_costs=[30.0, 30.0, 30.0, 30.0], interval=5.0
    )
    assert len(starts) == 4
    for i in range(len(starts) - 1):
        assert starts[i + 1] - starts[i] >= 2 * works[i], (
            f"pass {i} ran at >50% duty: period "
            f"{starts[i + 1] - starts[i]} < 2*{works[i]}"
        )


def test_the_bound_holds_for_alternating_pass_costs():
    """A uniform script cannot distinguish a per-pass bound from a constant
    one, so the costs alternate."""
    dash = _dash()
    starts, works = _run_loop(
        dash, work_costs=[30.0, 6.0, 90.0, 7.0, 45.0], interval=5.0
    )
    for i in range(len(starts) - 1):
        assert starts[i + 1] - starts[i] >= 2 * works[i]


def test_light_passes_keep_the_existing_work_plus_interval_cadence():
    """Below the interval the loop must behave exactly as it does today, so a
    small install sees no behavioural change at all."""
    dash = _dash()
    starts, works = _run_loop(
        dash, work_costs=[0.5, 0.5, 0.5, 0.5], interval=5.0
    )
    for i in range(len(starts) - 1):
        assert starts[i + 1] - starts[i] == works[i] + 5.0


def test_the_two_deadline_helpers_agree_algebraically():
    """A divergence from the main loop's bound must be a deliberate act."""
    dash = _dash()
    for t0, interval, work in [
        (0.0, 5.0, 0.5), (10.0, 5.0, 5.0), (3.0, 5.0, 30.0), (0.0, 0.0, 0.0),
    ]:
        assert (dash._conversation_next_deadline(t0, interval, work)
                == dash._next_deadline(t0, interval, work))


def test_the_deadline_helper_is_a_separate_function():
    """C-2: sharing one helper would let a later change to the main loop's
    scheduling silently remove this thread's bound."""
    dash = _dash()
    assert dash._conversation_next_deadline is not dash._next_deadline


# --- criterion 2: `work` charges the WHOLE pass ----------------------------


class _FakeConn:
    """A conversations connection whose close() charges the shared clock."""

    def __init__(self, clock, cost):
        self._clock = clock
        self._cost = cost
        self.closed = False

    def close(self):
        self.closed = True
        self._clock.advance(self._cost)


def test_work_charges_open_both_syncs_prune_and_close(monkeypatch):
    """Each component charges a DISTINCT cost, so a missed one is identifiable
    from the total rather than merely making it 'too small'.

    Charges: open 1, claude 2, codex 4, prune 8, close 16 = 31.
    """
    dash = _dash()
    clock = _VirtualClock()
    charged = {}

    def _charge(name, cost, result=None):
        def _fn(*a, **k):
            charged[name] = cost
            clock.advance(cost)
            return result
        return _fn

    conn = _FakeConn(clock, 16.0)
    monkeypatch.setattr(
        dash, "open_conversations_db", _charge("open", 1.0, conn)
    )
    monkeypatch.setattr(
        dash, "sync_claude_conversations", _charge("claude", 2.0)
    )
    monkeypatch.setattr(
        dash, "sync_codex_conversations", _charge("codex", 4.0)
    )
    monkeypatch.setattr(
        dash, "_dashboard_maybe_prune_retention", _charge("prune", 8.0)
    )

    recorded = []
    remaining = [None]
    stop = _Stop(remaining)

    def run_iteration():
        remaining.pop()
        return dash._conversation_sync_pass()

    dash._conversation_sync_loop(
        stop=stop,
        interval=5.0,
        run_iteration=run_iteration,
        monotonic=clock.monotonic,
        thread_time_ns=lambda: 0,
        wait=lambda seconds: clock.advance(max(0.0, seconds)),
        record=lambda **kw: recorded.append(kw),
    )

    assert charged == {"open": 1.0, "claude": 2.0, "codex": 4.0, "prune": 8.0}
    assert conn.closed, "the pass must close the connection it opened"
    assert recorded[0]["duration_ns"] == int(31.0 * 1e9), (
        "work must charge open+claude+codex+prune+close = 1+2+4+8+16"
    )
    assert recorded[0]["status"] == "ok"
    assert recorded[0]["claude_mode"] == "full"
    assert recorded[0]["codex_mode"] == "full"


# --- criterion 3: the prune's placement ------------------------------------


def _install_pass_doubles(
    dash, monkeypatch, *, open_ok=True, sync_raises=None, counters=None
):
    counters = counters if counters is not None else {}
    counters.setdefault("opens", 0)
    counters.setdefault("prunes", 0)
    counters.setdefault("claude", 0)

    class _Conn:
        def close(self):
            pass

    def _open(*a, **k):
        counters["opens"] += 1
        if not open_ok:
            raise sqlite3.DatabaseError("unopenable store")
        return _Conn()

    def _claude(conn):
        counters["claude"] += 1
        if sync_raises is not None:
            raise sync_raises

    monkeypatch.setattr(dash, "open_conversations_db", _open)
    monkeypatch.setattr(dash, "sync_claude_conversations", _claude)
    monkeypatch.setattr(dash, "sync_codex_conversations", lambda conn: None)
    monkeypatch.setattr(
        dash,
        "_dashboard_maybe_prune_retention",
        lambda: counters.__setitem__("prunes", counters["prunes"] + 1),
    )
    return counters


def test_a_pass_whose_sync_raises_still_prunes(monkeypatch, capsys):
    """Today the prune is the fourth statement inside the same `try` as both
    syncs, so a raising sync skips it. The documented contract is a throttled
    prune driven by this thread, not a prune conditional on a successful
    ingest."""
    dash = _dash()
    counters = _install_pass_doubles(
        dash, monkeypatch, sync_raises=sqlite3.DatabaseError("boom")
    )
    status = dash._conversation_sync_pass()
    assert counters["prunes"] == 1
    assert status == "store_unavailable"


def test_a_pass_whose_generic_sync_error_raises_still_prunes(monkeypatch):
    dash = _dash()
    counters = _install_pass_doubles(
        dash, monkeypatch, sync_raises=ValueError("malformed provider record")
    )
    status = dash._conversation_sync_pass()
    assert counters["prunes"] == 1
    assert status == "error"


def test_a_failed_open_is_not_retried_inside_the_same_pass(monkeypatch):
    """The prune opens its OWN conversations connection and only reaches its
    retention due/throttle check after that open. Pruning after a failed
    primary open would attempt a second open of the same unopenable store on
    every pass, forever, swallowing each failure — the duty bound would cap CPU
    share while doing nothing about the duplicated migration, recovery and I/O
    pressure.

    The invariant proved here is exactly "no SECOND open attempt after a failed
    open", not "at most one open attempt per pass": a pass that opens
    successfully opens the store twice by design, once for the syncs and once
    inside the prune. This test only ever counts one because
    `_install_pass_doubles` replaces the prune wholesale.
    """
    dash = _dash()
    counters = _install_pass_doubles(dash, monkeypatch, open_ok=False)
    status = dash._conversation_sync_pass()
    assert counters["prunes"] == 0
    assert counters["opens"] == 1, "a failed open was retried inside the pass"
    assert counters["claude"] == 0
    assert status == "store_unavailable"


def test_repeated_open_failure_is_never_retried_within_a_pass(monkeypatch):
    """Driven through the real loop, so the per-pass invariant is asserted over
    several consecutive failing passes rather than one. One open per failing
    pass and no prune, however many passes the loop runs."""
    dash = _dash()
    counters = _install_pass_doubles(dash, monkeypatch, open_ok=False)
    clock = _VirtualClock()
    remaining = [None, None, None]
    stop = _Stop(remaining)

    def run_iteration():
        remaining.pop()
        return dash._conversation_sync_pass()

    dash._conversation_sync_loop(
        stop=stop, interval=5.0, run_iteration=run_iteration,
        monotonic=clock.monotonic, thread_time_ns=lambda: 0,
        wait=lambda seconds: clock.advance(max(0.0, seconds)),
        record=lambda **kw: None,
    )
    assert counters["opens"] == 3
    assert counters["prunes"] == 0


def test_a_clean_pass_prunes_and_reports_ok(monkeypatch):
    dash = _dash()
    counters = _install_pass_doubles(dash, monkeypatch)
    assert dash._conversation_sync_pass() == "ok"
    assert counters["prunes"] == 1


def test_caught_up_frontier_skips_both_provider_syncs(monkeypatch):
    """The steady-state pass must not enter either O(files) provider walk."""
    dash = _dash()
    counters = _install_pass_doubles(dash, monkeypatch)

    class _Plan:
        mode = "caught_up"
        paths = frozenset()

    class _FrontierModule:
        @staticmethod
        def conversation_sync_certifiable(mode, stats, **_kwargs):
            return mode == "caught_up" and stats is None

    class _Frontier:
        def commit_provider(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(
        dash,
        "_conversation_frontier_plans",
        lambda _conn: (
            _FrontierModule(), _Frontier(), object(),
            {
                "claude": ((), (), True),
                "codex": ((), (), True),
            },
            {"claude": _Plan(), "codex": _Plan()},
        ),
    )
    status = dash._conversation_sync_pass()
    assert status == "ok"
    assert status.modes == {"claude": "caught_up", "codex": "caught_up"}
    assert status.files == {"claude": 0, "codex": 0}
    assert counters["claude"] == 0
    assert counters["prunes"] == 1


def test_targeted_frontier_passes_only_ticketed_paths(monkeypatch):
    dash = _dash()
    counters = {"prunes": 0}

    class _Conn:
        def close(self):
            pass

    class _Plan:
        def __init__(self, provider):
            self.mode = "targeted"
            self.paths = frozenset({f"/{provider}.jsonl"})

    class _Stats:
        lock_contended = False
        files_failed = 0
        files_deferred_torn = 0
        deferred_reason = None
        prune_refused = False
        budget_exhausted = False
        maintenance_failed = False

    class _FrontierModule:
        @staticmethod
        def conversation_sync_certifiable(_mode, _stats, **_kwargs):
            return True

    committed = []

    class _Frontier:
        def commit_provider(self, plan, *_args, **_kwargs):
            committed.append((plan.mode, plan.paths))

    seen = {}
    def _claude(_conn, **kwargs):
        seen["claude"] = kwargs
        return _Stats()

    def _codex(_conn, **kwargs):
        seen["codex"] = kwargs
        return _Stats()

    monkeypatch.setattr(dash, "open_conversations_db", lambda: _Conn())
    monkeypatch.setattr(dash, "sync_claude_conversations", _claude)
    monkeypatch.setattr(dash, "sync_codex_conversations", _codex)
    monkeypatch.setattr(
        dash, "_dashboard_maybe_prune_retention",
        lambda: counters.__setitem__("prunes", counters["prunes"] + 1),
    )
    monkeypatch.setattr(
        dash,
        "_conversation_frontier_plans",
        lambda _conn: (
            _FrontierModule(), _Frontier(), object(),
            {
                "claude": ((), (), True),
                "codex": ((), (), True),
            },
            {"claude": _Plan("claude"), "codex": _Plan("codex")},
        ),
    )

    assert dash._conversation_sync_pass() == "ok"
    assert seen["claude"]["only_paths"] == {"/claude.jsonl"}
    assert seen["codex"]["only_paths"] == {"/codex.jsonl"}
    assert len(committed) == 2
    assert counters["prunes"] == 1


def test_same_size_replacement_plan_rebuilds_only_affected_provider(monkeypatch):
    """A source-replacement full plan must replay from zero, not size-skip."""
    dash = _dash()

    class _Conn:
        def close(self):
            pass

    class _Plan:
        paths = frozenset()
        marker_end = 0

        def __init__(self, provider, *, reason):
            self.provider = provider
            self.mode = "full"
            self.reason = reason

    class _Stats:
        files_total = 1
        files_processed = 1
        files_skipped_unchanged = 0
        lock_contended = False
        files_failed = 0
        files_deferred_torn = 0
        deferred_reason = None
        prune_refused = False
        budget_exhausted = False
        maintenance_failed = False

    class _FrontierModule:
        @staticmethod
        def conversation_sync_certifiable(_mode, _stats, **_kwargs):
            return True

    class _Frontier:
        def seed_provider(self, *_args, **_kwargs):
            pass

    seen = {}

    def _sync(provider):
        def _run(_conn, **kwargs):
            seen[provider] = kwargs
            return _Stats()
        return _run

    monkeypatch.setattr(dash, "open_conversations_db", lambda: _Conn())
    monkeypatch.setattr(dash, "sync_claude_conversations", _sync("claude"))
    monkeypatch.setattr(dash, "sync_codex_conversations", _sync("codex"))
    monkeypatch.setattr(dash, "_dashboard_maybe_prune_retention", lambda: None)
    monkeypatch.setattr(
        dash,
        "_conversation_frontier_plans",
        lambda _conn: (
            _FrontierModule(), _Frontier(), object(),
            {"claude": ((), (), True), "codex": ((), (), True)},
            {
                "claude": _Plan("claude", reason="source_replaced"),
                "codex": _Plan("codex", reason="unseeded"),
            },
        ),
    )

    assert dash._conversation_sync_pass() == "ok"
    assert seen["claude"] == {"rebuild": True}
    assert seen["codex"] == {}


def test_an_escaping_pass_is_recorded_as_error_and_still_bounded():
    """The loop must outlive one bad pass, charge its cost, and keep waiting."""
    dash = _dash()
    clock = _VirtualClock()
    recorded = []
    remaining = [None, None]
    stop = _Stop(remaining)

    def run_iteration():
        remaining.pop()
        clock.advance(30.0)
        raise RuntimeError("pass exploded")

    def monotonic():
        return clock.now

    def wait(seconds):
        clock.advance(max(0.0, seconds))

    def record(**kw):
        recorded.append(kw)

    dash._conversation_sync_loop(
        stop=stop, interval=5.0, run_iteration=run_iteration,
        monotonic=monotonic, thread_time_ns=lambda: 0, wait=wait,
        record=record,
    )
    assert [r["status"] for r in recorded] == ["error", "error"]
    assert recorded[0]["duration_ns"] == int(30.0 * 1e9)
    starts = [r["started_ns"] for r in recorded]
    assert starts[1] - starts[0] >= int(2 * 30.0 * 1e9)


# --- criterion 3b: production really uses the extracted loop ---------------


class _NoopThread(threading.Thread):
    def __init__(self, *a, **k):
        super().__init__(daemon=True)

    def run(self):
        pass


class _NoopSyncThread:
    """Stand-in for the unrelated main-sync thread in the wiring test."""

    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def test_production_thread_target_is_the_extracted_loop(monkeypatch, tmp_path):
    """Guards the exact vacuity this extraction invites: a green virtual-clock
    suite over a module-level function `cmd_dashboard` does not call.

    Runs the REAL `cmd_dashboard` up to the HTTP bind — the conversation thread
    is started before it — and asserts the thread's target reached
    `_conversation_sync_loop` with a callable pass and a real recorder.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)   # preserve-item 3
    dash = _dash()
    stats = importlib.import_module("_lib_tick_stats")

    seen = {}
    called = threading.Event()

    def spy(**kw):
        seen.update(kw)
        called.set()
        return None

    monkeypatch.setattr(dash, "_conversation_sync_loop", spy)

    # Neutralize the other background threads so nothing else runs or reaches
    # the network; the conversations thread is the one under test.
    monkeypatch.setattr(dash, "_DashboardUpdateCheckThread", _NoopThread)
    monkeypatch.setitem(ns, "_TuiSyncThread", _NoopSyncThread)

    servers = []
    real_server_cls = dash._QuietThreadingHTTPServer

    class _RecordingServer(real_server_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(dash, "_QuietThreadingHTTPServer", _RecordingServer)

    def stop_after_conversation_thread_starts(*_args):
        assert called.wait(timeout=PRESENCE_BACKSTOP_SECONDS), (
            "cmd_dashboard never called _conversation_sync_loop"
        )

    # Let cmd_dashboard enter its real shutdown path after the target is
    # observed. Raising during server construction used to bypass that cleanup
    # and leave daemon threads alive past monkeypatch teardown, where they could
    # write through the restored production paths.
    monkeypatch.setattr(
        dash, "_dashboard_wait_for_signal", stop_after_conversation_thread_starts
    )

    args = argparse.Namespace(host="127.0.0.1", port=0, no_browser=True,
                              no_sync=False, sync_interval=1000, tz=None)
    assert dash.cmd_dashboard(args) == 0
    for server in servers:
        server.server_close()
    assert called.is_set()
    assert callable(seen["run_iteration"])
    assert seen["record"] is stats.record_conversation_pass, (
        "production must pass the real recorder, not None"
    )
    assert seen["interval"] >= 5.0
    assert seen["stop"] is not None


def test_no_sync_never_starts_the_conversation_thread(monkeypatch):
    """`--no-sync` leaves the thread unstarted, which is why an empty
    conversation ring is that mode's correct permanent reading."""
    dash = _dash()
    stop = threading.Event()
    assert dash._make_conversation_sync_thread(
        stop=stop, sync_interval=30.0, no_sync=True
    ) is None
    thread = dash._make_conversation_sync_thread(
        stop=stop, sync_interval=30.0, no_sync=False
    )
    assert thread is not None
    assert thread.name == "dashboard-conversations-sync"
    assert thread.daemon is True


# --- #939 D5: the loop feeds writeIo's startup admission hold ---------------


def _release_spy(dash, monkeypatch, verdicts):
    wio = dash._lib_write_io
    observed, released = [], []

    def observe(info, *, started_ns, seq):
        observed.append((info, seq))
        return verdicts.pop(0) if verdicts else None

    monkeypatch.setattr(wio, "observe_conversation_pass", observe)
    monkeypatch.setattr(wio, "release_admission",
                        lambda reason: released.append(reason) or True)
    return observed, released


def test_the_loop_releases_on_the_evaluators_verdict_and_on_exit(monkeypatch):
    """Detects: a verdict the loop never acts on (a permanent hold), or a loop
    that ends without releasing (a dead loop holding admission)."""
    dash = _dash()
    observed, released = _release_spy(dash, monkeypatch, [None, "settled"])
    drain = {"census_complete": True}
    passes = [dash._ConversationPassStatus("ok", drain=drain)] * 3
    stop = _Stop(passes)
    clock = _VirtualClock()

    def run_iteration():
        return passes.pop(0)

    dash._conversation_sync_loop(
        stop=stop, interval=5.0, run_iteration=run_iteration,
        monotonic=clock.monotonic, thread_time_ns=lambda: 0,
        wait=lambda seconds: clock.advance(max(0.0, seconds)),
        record=lambda **kw: None)
    assert [seq for _info, seq in observed] == [1, 2, 3]
    assert all(info is drain for info, _seq in observed)
    assert released == ["settled", "loopExit"]


def test_a_pass_without_drain_information_is_observed_as_incomplete(
        monkeypatch):
    dash = _dash()
    observed, released = _release_spy(dash, monkeypatch, [])
    _run_loop(dash, work_costs=[1.0], interval=5.0)
    assert observed == [(None, 1)]
    assert released == ["loopExit"]


def test_a_base_exception_in_a_pass_still_releases(monkeypatch):
    dash = _dash()
    _observed, released = _release_spy(dash, monkeypatch, [])

    class _Stopper(BaseException):
        pass

    def run_iteration():
        raise _Stopper()

    stop = threading.Event()
    try:
        dash._conversation_sync_loop(
            stop=stop, interval=5.0, run_iteration=run_iteration,
            thread_time_ns=lambda: 0, record=lambda **kw: None)
    except _Stopper:
        pass
    else:
        raise AssertionError("the BaseException must propagate")
    assert released == ["loopExit"]


def test_a_loop_stopped_before_its_first_pass_releases(monkeypatch):
    dash = _dash()
    _observed, released = _release_spy(dash, monkeypatch, [])
    stop = threading.Event()
    stop.set()
    dash._conversation_sync_loop(stop=stop, interval=5.0,
                                 run_iteration=lambda: "ok")
    assert released == ["loopExit"]


class _D5Stats:
    """A provider result with only the fields the census and residual read."""

    def __init__(self, **fields):
        self.files_total = 1
        self.files_processed = 1
        self.files_skipped_unchanged = 0
        self.__dict__.update(fields)


@pytest.mark.parametrize("worker", [False, True])
def test_a_pass_with_a_worker_in_progress_is_not_census_complete(
        monkeypatch, worker):
    """Detects: a pair of equal passes bracketing a detached Codex worker that
    is still doing startup work (requested, between stages or running)
    settling the hold."""
    dash = _dash()
    _install_pass_doubles(dash, monkeypatch)
    monkeypatch.setattr(dash, "_conversation_frontier_plans", lambda _c: None)
    monkeypatch.setattr(dash, "sync_claude_conversations",
                        lambda _c, **_k: _D5Stats(failed_paths={"/bad"},
                                                  files_failed=1))
    monkeypatch.setattr(dash, "sync_codex_conversations",
                        lambda _c, **_k: _D5Stats())
    lifecycle = importlib.import_module("_lib_codex_worker_lifecycle")
    monkeypatch.setattr(
        lifecycle, "in_progress",
        lambda _app, command, **_k: worker and command == "_codex-prune-drain")
    status = dash._conversation_sync_pass()
    assert status == "ok"
    drain = status.drain
    assert drain["census_complete"] is (not worker)
    assert drain["certifiable"] is False
    assert dict(drain["providers"]["claude"]["residual"]) == {
        "failed_paths": ("/bad",)}
    assert drain["providers"]["codex"]["mode"] == "full"


@pytest.mark.parametrize("failure,status,name", [
    ("sync", "error", "RuntimeError"),
    ("open", "store_unavailable", "DatabaseError"),
])
def test_an_erroring_pass_carries_its_status_and_never_certifies(
        monkeypatch, failure, status, name):
    """Detects: an error that repeats on every pass holding writeIo's
    admission for the dashboard's whole life (its census never complete), or
    an error pass releasing as drained."""
    dash = _dash()
    _install_pass_doubles(dash, monkeypatch, open_ok=failure != "open",
                          sync_raises=RuntimeError("injected"))
    monkeypatch.setattr(dash, "_conversation_frontier_plans", lambda _c: None)
    lifecycle = importlib.import_module("_lib_codex_worker_lifecycle")
    monkeypatch.setattr(lifecycle, "any_in_progress", lambda *_a, **_k: False)
    result = dash._conversation_sync_pass()
    assert result == status
    drain = result.drain
    # A failed open is inconclusive (it may have committed migration work
    # first, #939 R10-01), so only a pass that opened can be census-complete.
    assert drain["census_complete"] is (failure != "open")
    assert drain["certifiable"] is False
    assert set(drain["providers"]) == {"pass"}
    assert drain["providers"]["pass"]["mode"] == "full"
    assert dict(drain["providers"]["pass"]["residual"]) == {
        "status": status, "exception": name}
    monkeypatch.setattr(lifecycle, "any_in_progress", lambda *_a, **_k: True)
    assert dash._conversation_sync_pass().drain["census_complete"] is False


def _loop_over(dash, monkeypatch, outcomes, changed=None):
    """Run the real loop over real passes, each failing or succeeding as
    ``outcomes`` says (an ``error`` pass first changing as many rows on its
    connection as ``changed`` lists), with a placed hold and a
    census-complete accounting tick; return the hold's ``releasedBy`` and the
    passes that ran."""
    wio = dash._lib_write_io
    monkeypatch.setattr(wio, "_STATE", wio._STATE)   # restored at teardown
    wio.reset_for_tests(counter=wio.ProcessWriteCounter(
        platform="darwin", darwin_reader=lambda _pid=None: 0))
    wio.hold_admission()
    wio.observe_accounting_tick(ended_ns=0, census_complete=True,
                                residuals={"claude": (), "codex": ()})
    remaining = list(outcomes)
    rows = list(changed or ())
    ran = []

    def claude(conn, **_k):
        outcome = remaining.pop(0)
        ran.append(outcome)
        if outcome == "error":
            conn.total_changes = (getattr(conn, "total_changes", 0)
                                  + (rows.pop(0) if rows else 0))
            raise RuntimeError("injected")
        return _D5Stats(failed_paths={"/bad"}, files_failed=1)

    _install_pass_doubles(dash, monkeypatch)
    monkeypatch.setattr(dash, "_conversation_frontier_plans", lambda _c: None)
    monkeypatch.setattr(dash, "sync_claude_conversations", claude)
    monkeypatch.setattr(dash, "sync_codex_conversations",
                        lambda _c, **_k: _D5Stats())
    lifecycle = importlib.import_module("_lib_codex_worker_lifecycle")
    monkeypatch.setattr(lifecycle, "any_in_progress", lambda *_a, **_k: False)
    clock = _VirtualClock()
    clock.advance(1.0)

    def run_iteration():
        clock.advance(1.0)
        return dash._conversation_sync_pass()

    released = []
    real_release = wio.release_admission
    monkeypatch.setattr(wio, "release_admission",
                        lambda reason: released.append(reason)
                        or real_release(reason))
    dash._conversation_sync_loop(
        stop=_Stop(remaining), interval=5.0, run_iteration=run_iteration,
        monotonic=clock.monotonic, thread_time_ns=lambda: 0,
        wait=lambda seconds: clock.advance(max(0.0, seconds)),
        record=lambda **kw: None)
    payload = wio.write_io_payload()["coldStartup"]
    return payload["releasedBy"], released, ran


@pytest.mark.parametrize("changed, expected", [
    ([0, 0], "settled"),
    # A failing pass can commit work before the exception propagates (D1's
    # prepared prefix, a Codex publication unit): two such failures of one
    # class are catch-up that may still advance, never a settle (#939 R7-01,
    # R8-01), however their cursors add up.
    ([3, 3], "loopExit"),
    ([3, 0], "loopExit"),
])
def test_two_identical_error_passes_settle_only_without_changed_rows(
        monkeypatch, changed, expected):
    """Detects: a dashboard whose conversation pass fails the same way every
    pass never judging its writes (a permanent hold), and a failure that
    changed rows before raising anchoring a release mid-catch-up."""
    dash = _dash()
    released_by, released, ran = _loop_over(dash, monkeypatch,
                                            ["error", "error"], changed)
    assert ran == ["error", "error"]
    assert released_by == expected
    assert released[0] == expected


_ROW = ("INSERT INTO conversation_source_files (path, size_bytes, mtime_ns,"
        " last_byte_offset, last_ingested_at) VALUES (?,?,?,?,?)")


@pytest.mark.parametrize("work, changed", [
    ("none", False),
    ("pass", True),
    # The store open runs migrations, imports and replays on the pass's own
    # connection, and a resumable migration commits its chunks before it can
    # fail (#939 R9-01).
    ("open", True),
    ("open-then-fail", True),
    # With no connection to ask, a failed open is inconclusive (R10-01).
    ("open-fails", True),
])
def test_a_real_failing_pass_counts_every_row_it_changed(
        monkeypatch, tmp_path, work, changed):
    """Detects: a steady store's open read as progress (a stable failure that
    never settles), or work committed by the pass, by its store open, or by
    an open that then failed, read as no progress."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = _dash()
    cache = importlib.import_module("_cctally_cache")
    cache.open_conversations_db().close()        # the store exists, steady
    lifecycle = importlib.import_module("_lib_codex_worker_lifecycle")
    monkeypatch.setattr(lifecycle, "any_in_progress", lambda *_a, **_k: False)
    monkeypatch.setattr(dash, "_dashboard_maybe_prune_retention", lambda: None)
    seen = []
    real_open = dash.open_conversations_db

    def opening(*a, **k):
        conn = real_open(*a, **k)
        if work in ("open", "open-then-fail"):
            conn.execute(_ROW, (f"/o/{len(seen)}.jsonl", 10, 1, 10, "t"))
            conn.commit()
        if work in ("open-then-fail", "open-fails"):
            conn.close()
            seen.append(1)
            raise sqlite3.OperationalError("database is locked")
        return conn

    def failing(conn, **_k):
        if work == "pass":
            conn.execute(_ROW, (f"/p/{len(seen)}.jsonl", 10, 1, 10, "t"))
            conn.commit()
        seen.append(1)
        raise RuntimeError("injected")

    monkeypatch.setattr(dash, "open_conversations_db", opening)
    monkeypatch.setattr(dash, "sync_claude_conversations", failing)
    monkeypatch.setattr(dash, "sync_codex_conversations", failing)
    for _ in range(2):
        result = dash._conversation_sync_pass()
        assert result == ("store_unavailable"
                          if work in ("open-then-fail", "open-fails")
                          else "error")
        assert result.drain["census_complete"] is (not changed)


def test_an_error_pass_then_an_ok_pass_does_not_settle(monkeypatch):
    """Detects: an ok pass after an error pass read as the same residual (a
    release across the error's recovery)."""
    dash = _dash()
    released_by, released, _ran = _loop_over(dash, monkeypatch,
                                             ["ok", "error", "ok"])
    assert released == ["loopExit"]
    assert released_by == "loopExit"


@pytest.mark.parametrize("raising", ["observe", "release"])
def test_an_evaluator_exception_never_stops_ingest(monkeypatch, capsys,
                                                    raising):
    """Detects: a bug in writeIo's evaluator ending the conversation loop, so
    transcript ingest stops for the dashboard's whole life."""
    dash = _dash()
    wio = dash._lib_write_io
    released = []

    def observe(info, *, started_ns, seq):
        if raising == "observe":
            raise RuntimeError("evaluator bug")
        return "settled"

    def release(reason):
        if raising == "release" and reason != "loopExit":
            raise RuntimeError("release bug")
        released.append(reason)
        return True

    monkeypatch.setattr(wio, "observe_conversation_pass", observe)
    monkeypatch.setattr(wio, "release_admission", release)
    starts, _works = _run_loop(dash, work_costs=[1.0, 1.0, 1.0], interval=5.0)
    assert len(starts) == 3
    assert released == ["loopExit"]
    assert capsys.readouterr().err.count("evaluator bug" if raising == "observe"
                                         else "release bug") == 1


@pytest.mark.parametrize("no_sync", [False, True])
def test_cmd_dashboard_places_the_hold_before_the_main_sync_thread(
        monkeypatch, tmp_path, no_sync):
    """Detects: a hold placed after the main sync thread already published a
    non-cold snapshot (admitting warm too early), or a hold under --no-sync
    that nothing would ever release."""
    dash = _dash()      # loads the script; patch the module it registered
    ns = sys.modules["cctally"].__dict__
    redirect_paths(ns, monkeypatch, tmp_path)
    events = []
    monkeypatch.setattr(dash._lib_write_io, "hold_admission",
                        lambda: events.append("hold"))

    class _OrderedSyncThread(_NoopSyncThread):
        def start(self):
            events.append("sync_start")

    monkeypatch.setattr(dash, "_conversation_sync_loop", lambda **kw: None)
    monkeypatch.setattr(dash, "_DashboardUpdateCheckThread", _NoopThread)
    monkeypatch.setitem(ns, "_TuiSyncThread", _OrderedSyncThread)
    servers = []
    real_server_cls = dash._QuietThreadingHTTPServer

    class _RecordingServer(real_server_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(dash, "_QuietThreadingHTTPServer", _RecordingServer)
    monkeypatch.setattr(dash, "_dashboard_wait_for_signal", lambda *_a: None)
    args = argparse.Namespace(host="127.0.0.1", port=0, no_browser=True,
                              no_sync=no_sync, sync_interval=1000, tz=None)
    assert dash.cmd_dashboard(args) == 0
    for server in servers:
        server.server_close()
    assert events == ([] if no_sync else ["hold", "sync_start"])
