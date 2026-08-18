"""#583 S4 (F5) — the conversation loop's duty bound, proven algebraically.

The dashboard runs two work loops. The main one has been bounded since #313.
The second one — transcript ingest — computed its interval once and ended every
pass with a fixed ``wait(interval)``, which prevents literal 100% duty for
finite work but provides no scale-independent ceiling below it: a pass costing
30 s ran 30-on/5-off, about 86% duty, and nothing bounded that as
``conversations.db`` grew.

No wall-clock assertion appears anywhere in this file. A virtual monotonic clock
advances only when the fake pass or the fake wait says it does, so every test
measures the scheduling algebra and never the machine (D-1 /
``tests/test_timing_budget_guard.py``).

``tests/test_dashboard_sync_cooldown.py`` owns the MAIN loop's contract; this
file is deliberately separate rather than an extension of it.
"""
import argparse
import importlib
import sqlite3
import threading

from conftest import load_script, redirect_paths


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
        assert called.wait(timeout=5), (
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
