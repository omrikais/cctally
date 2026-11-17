"""#620 S2 — `GET /api/diagnosis`, the dashboard half of the diagnosis.

The route publishes exactly what `diagnosis_to_wire` produces, over the same
`build_diagnosis` the CLI calls, so the two surfaces cannot drift. What is
tested here is what the ROUTE adds: registration, selector parsing, the status
mapping, the read-only promise and the header contract.

The status mapping is the part that is easy to get wrong, because a withheld
report is a legitimate answer for every cause except one. A report fully
withheld as `retained_range_mismatch`, `stale_evidence`, `pricing_unavailable`
or `insufficient_population` is 200; a report whose requested provider's store
could not be READ, with no other provider answering, is 503 with the report
still in the body — the same condition the CLI turns into exit 3 while still
printing the report.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import http.client
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace

import pytest

from conftest import load_script, redirect_paths
from test_620_s2_diagnosis_sources import (  # reuse the seeded corpora
    WINDOW_END, WINDOW_START, _seed_claude, _seed_claude_blocks,
)

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop


UTC = dt.timezone.utc
_WINDOW = f"{WINDOW_START.date().isoformat()}..{WINDOW_END.date().isoformat()}"
_WINDOW_QUERY = f"{_WINDOW}&tz=Etc%2FUTC"


def _dash():
    return sys.modules["_cctally_dashboard"]


class _HeldClock:
    """The dashboard module's `time`, with a `monotonic()` that can be held.

    `_DiagnosisAdmission` arms each flight's wall-clock deadline when the flight
    is created, before a test has reached the outcome it asserts. A host stall
    can spend that deadline first and fail the test at random (#962). Holding
    the clock while the outcome is reached leaves the deadline unchanged and
    lets it expire from the hold's start once released. A held clock can also
    be advanced, to expire a deadline at a chosen point; on release it resumes
    from the later of the held value and real time, so it never runs
    backwards. Every other attribute stays real.
    """

    def __init__(self):
        self._held_at = None
        self._offset = 0.0

    @property
    def is_held(self):
        return self._held_at is not None

    def _now(self):
        return time.monotonic() + self._offset

    def monotonic(self):
        held_at = self._held_at
        return self._now() if held_at is None else held_at

    def advance(self, seconds):
        assert self._held_at is not None, "only a held clock is advanced"
        self._held_at += seconds

    @contextlib.contextmanager
    def held(self):
        assert self._held_at is None, "a held clock is not held again"
        self._held_at = self._now()
        try:
            yield
        finally:
            self._offset += max(0.0, self._held_at - self._now())
            self._held_at = None

    def __getattr__(self, name):
        return getattr(time, name)


class _HeldSlot:
    """The admission slot, whose deadline-timed waits pause while held.

    A queued producer waits for the slot for the time its flight has left
    (`acquire(timeout=remaining)`), a real-time wait the held clock cannot
    reach. While the clock is held no time passes, so neither does that wait:
    it retries in short slices until the slot is free or the clock is
    released, then waits as the product asked.
    """

    def __init__(self, slot, clock):
        self._slot = slot
        self._clock = clock

    def acquire(self, blocking=True, timeout=None):
        while blocking and timeout is not None and self._clock.is_held:
            if self._slot.acquire(timeout=0.05):
                return True
        return self._slot.acquire(blocking, timeout)

    def release(self):
        self._slot.release()


def _hold_admission_clock(monkeypatch, dash, admission):
    """Hold ADMISSION's deadline clock: the dashboard's `time` and its slot."""
    clock = _HeldClock()
    monkeypatch.setattr(dash, "time", clock)
    monkeypatch.setattr(admission, "_admission",
                        _HeldSlot(admission._admission, clock))
    return clock


def _run_bounded_in_process(monkeypatch, sources):
    """Keep a parent-process fault injection visible to an HTTP route test."""
    monkeypatch.setattr(
        sources,
        "build_diagnosis_bounded",
        lambda scope, *, measured_at, transcripts_visible, timeout_seconds, retirement=None: (
            sources.build_diagnosis(
                scope,
                measured_at=measured_at,
                transcripts_visible=transcripts_visible,
            )
        ),
    )


def _nonterminating_diagnosis_worker():
    """Pickle-safe stand-in for a diagnosis process that never returns."""
    while True:
        time.sleep(60)


class _Response:
    def __init__(self, status: int, headers, body: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self):
        return json.loads(self.body)


class _Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def get(self, path: str, *, headers: dict | None = None) -> _Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            body = response.read().decode()
            return _Response(response.status, dict(response.getheaders()), body)
        finally:
            conn.close()


def _boot(ns):
    """A real server on an ephemeral loopback port.

    Driven over HTTP rather than by calling the handler method directly, so
    `_dispatch`, `_require_api_auth` and the header write are all exercised —
    a route that is registered but shadowed, or one that sends its body before
    its status, is only visible from outside.
    """
    dash = _dash()
    dash.DashboardHTTPHandler.hub = ns["SSEHub"]()
    dash.DashboardHTTPHandler.snapshot_ref = ns["_SnapshotRef"](
        ns["_empty_dashboard_snapshot"]()
    )
    dash.DashboardHTTPHandler.hub.publish(
        dash.DashboardHTTPHandler.snapshot_ref.get()
    )
    dash.DashboardHTTPHandler.no_sync = True
    # The bind this fixture models. It decides `_transcripts_visible_to_request`
    # now that the route threads that predicate into plan stage 1, and these are
    # CLASS attributes, so a value a neighbouring module left behind would
    # otherwise decide it.
    dash.DashboardHTTPHandler.cctally_host = "127.0.0.1"
    dash.DashboardHTTPHandler.cctally_expose_transcripts = False
    server = dash._QuietThreadingHTTPServer(
        ("127.0.0.1", 0), dash.DashboardHTTPHandler
    )
    thread = start(server)
    return server, thread, _Client(server.server_address[1])


@pytest.fixture
def rich_server(tmp_path, monkeypatch):
    """A store with a dominant model, project, session and block."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    _seed_claude(ns, models=(("claude-opus-4-20250514", 50),
                             ("claude-haiku-4-20250514", 10)))
    _seed_claude_blocks(ns)
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


@pytest.fixture
def empty_server(tmp_path, monkeypatch):
    """Both stores present and empty — a legitimately withheld report."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


@pytest.fixture
def storeless_server(tmp_path, monkeypatch):
    """No store files at all — the one withheld cause that is also a failure."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    server, thread, client = _boot(ns)
    try:
        yield client
    finally:
        stop(server, thread)


# --- registration -------------------------------------------------------

def test_route_is_registered_as_exact():
    """An exact entry, so it cannot shadow the parameterized conversation
    routes and they cannot shadow it."""
    load_script()
    entry = next(e for e in _dash()._GET_ROUTES if e[1] == "/api/diagnosis")
    assert entry[0] == "exact"
    assert entry[2] == "_handle_get_diagnosis"
    assert entry[4] is False


def test_single_flight_key_separates_every_build_and_authorization_axis():
    """No selector or privacy variant may receive another request's report."""
    ns = load_script()
    sources = ns["_load_sibling"]("_cctally_diagnosis_sources")
    base = sources.DiagnosisScope(
        source="codex",
        account_key="account-a",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        effective_speed="standard",
        display_tz="Etc/UTC",
        label="current",
    )
    scopes = (
        base,
        replace(base, source="claude"),
        replace(base, account_key="account-b"),
        replace(
            base,
            window_start=WINDOW_START + dt.timedelta(hours=1),
            window_end=WINDOW_END + dt.timedelta(hours=1),
        ),
        replace(base, effective_speed="fast"),
        replace(base, display_tz="Asia/Jerusalem"),
        replace(base, label="custom"),
    )
    keys = [
        _dash()._diagnosis_flight_key(scope, True, False)
        for scope in scopes
    ]
    keys.append(_dash()._diagnosis_flight_key(base, False, False))
    keys.append(_dash()._diagnosis_flight_key(base, True, True))
    assert len(set(keys)) == len(keys)


def test_process_wide_admission_serializes_distinct_scopes():
    """Different selectors do not coalesce, but still cannot multiply heaps."""
    load_script()
    admission = _dash()._DiagnosisAdmission()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def _first():
        first_entered.set()
        assert release_first.wait(PRESENCE_BACKSTOP_SECONDS)
        return "first"

    def _second():
        second_entered.set()
        return "second"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            admission.run, ("scope-a", True, False), _first,
        )
        assert first_entered.wait(PRESENCE_BACKSTOP_SECONDS)
        second = executor.submit(
            admission.run, ("scope-b", True, False), _second,
        )
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: len(admission._flights) == 2,
                timeout=PRESENCE_BACKSTOP_SECONDS,
            ), "the second diagnosis never registered for admission"
            assert not second_entered.is_set(), (
                "distinct diagnosis scopes ran concurrently"
            )
        release_first.set()
        assert first.result() == "first"
        assert second.result() == "second"


def test_diagnosis_admission_bounds_same_and_cross_key_callers(monkeypatch):
    load_script()
    dash = _dash()
    # A caller cap above the three admitted callers, so the fourth "a" caller
    # meets only the per-key bound and the "c" caller only the key bound.
    admission = dash._DiagnosisAdmission(max_callers=4, max_per_key=2,
                                         max_keys=2, deadline_seconds=1)
    # #962: the bounds and the shared report are asserted, not the deadline,
    # so a host stall must not expire the "a" flight first.
    clock = _hold_admission_clock(monkeypatch, dash, admission)
    entered = threading.Event()
    release = threading.Event()

    def _blocked():
        entered.set()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return "report"

    # The clock is released before the executor joins its callers, so on a
    # failure path every caller still reaches its deadline.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=5,
    ) as executor, clock.held():
        first = executor.submit(admission.run, ("a", True, False), _blocked)
        assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
        follower = executor.submit(admission.run, ("a", True, False), _blocked)
        queued = executor.submit(admission.run, ("b", True, False), lambda: "b")
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: len(admission._flights) == 2
                and admission._flights[("a", True, False)].callers == 2,
                timeout=PRESENCE_BACKSTOP_SECONDS,
            )
        with pytest.raises(dash._DiagnosisOverloaded):
            admission.run(("a", True, False), _blocked)
        with pytest.raises(dash._DiagnosisOverloaded):
            admission.run(("c", True, False), lambda: "c")
        release.set()
        assert first.result(timeout=PRESENCE_BACKSTOP_SECONDS) == "report"
        assert follower.result(timeout=PRESENCE_BACKSTOP_SECONDS) == "report"
        assert queued.result(timeout=PRESENCE_BACKSTOP_SECONDS) == "b"


def test_diagnosis_deadline_retires_stuck_producer_and_recovers(monkeypatch):
    import os

    load_script()
    dash = _dash()
    sources = sys.modules["cctally"]._load_sibling(
        "_cctally_diagnosis_sources"
    )
    deadline_seconds = 0.25
    admission = dash._DiagnosisAdmission(max_callers=3, max_per_key=2,
                                         max_keys=2,
                                         deadline_seconds=deadline_seconds)
    # The test's own probes read the product's slot itself, never through the
    # held clock.
    slot = admission._admission
    clock = _hold_admission_clock(monkeypatch, dash, admission)
    threads_before = set(threading.enumerate())
    real_await = sources._await_retirement
    sessions = []
    at_return = []
    entered = threading.Event()
    retired = threading.Event()
    allow_return = threading.Event()
    late_builds = []

    def recorded_await(process, sid, seconds):
        sessions.append(sid)
        return real_await(process, sid, seconds)

    # #1087: the parent signals nothing; at the deadline it sends the stop
    # frame and confirms the worker's retirement itself. The worker's session
    # id is read where the parent confirms it.
    monkeypatch.setattr(sources, "_await_retirement", recorded_await)

    # #962: the callers stop waiting at the flight deadline, but the producer
    # keeps the single process-wide slot until its process build has been
    # retired and has returned. On a loaded or sandboxed runner that outlasts
    # a whole recovery deadline, so a slow return is modelled
    # deterministically: the producer leaves its bounded build only when the
    # test allows it.
    def _bounded_stuck(remaining, retirement=None):
        entered.set()
        try:
            return sources._run_process_with_deadline(
                _nonterminating_diagnosis_worker, (),
                timeout_seconds=remaining, retirement=retirement,
            )
        except BaseException as exc:
            at_return.append(exc)
            raise
        finally:
            retired.set()
            allow_return.wait(PRESENCE_BACKSTOP_SECONDS)

    def _late_build(remaining, retirement=None):
        late_builds.append(remaining)
        return "late"

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            # Held until the producer has started its process build and the
            # follower has joined, so a host stall cannot refuse that start.
            with clock.held():
                first = executor.submit(
                    admission.run, ("a", True, False),
                    lambda: pytest.fail("unbounded build path ran"),
                    bounded_build=_bounded_stuck,
                )
                follower = executor.submit(
                    admission.run, ("a", True, False),
                    lambda: pytest.fail("coalesced caller built independently"),
                    bounded_build=_bounded_stuck,
                )
                assert entered.wait(PRESENCE_BACKSTOP_SECONDS), (
                    "the stuck producer never started its process build"
                )
                with admission._changed:
                    assert admission._changed.wait_for(
                        lambda: admission._callers == 2,
                        timeout=PRESENCE_BACKSTOP_SECONDS,
                    ), "the follower never joined the stuck flight"
            # Released: both callers give up at the real 0.25 s deadline.
            with pytest.raises(dash._DiagnosisTimedOut):
                first.result(timeout=PRESENCE_BACKSTOP_SECONDS)
            with pytest.raises(dash._DiagnosisTimedOut):
                follower.result(timeout=PRESENCE_BACKSTOP_SECONDS)

            # Retirement: the runner returned with the deadline, either with
            # its worker shown retired or with that retirement pending; the
            # producer still holds the slot until its bounded build returns.
            assert retired.wait(PRESENCE_BACKSTOP_SECONDS), (
                "the timed-out diagnosis process was never retired"
            )
            assert sessions and sessions[0] is not None, (
                "the runner never confirmed its worker's retirement"
            )
            assert isinstance(at_return[0], TimeoutError), at_return
            if getattr(at_return[0], "wait", None) is None:
                # Shown retired at return: the worker's group is gone.
                with pytest.raises(ProcessLookupError):
                    os.killpg(sessions[0], 0)
            assert not slot.acquire(blocking=False), (
                "the slot was released before the producer left its build"
            )

            # A queued flight that expires while the slot is held must never
            # start its build once the slot frees. Its deadline is expired
            # on the held clock, so its producer is still waiting for the
            # slot when the stuck producer returns.
            before = set(threading.enumerate())
            with clock.held():
                expired = executor.submit(
                    admission.run, ("q", True, False),
                    lambda: pytest.fail("unbounded build path ran"),
                    bounded_build=_late_build,
                )
                with admission._changed:
                    assert admission._changed.wait_for(
                        lambda: ("q", True, False) in admission._flights,
                        timeout=PRESENCE_BACKSTOP_SECONDS,
                    ), "the queued diagnosis never registered"
                clock.advance(4 * deadline_seconds)
                with pytest.raises(dash._DiagnosisTimedOut):
                    expired.result(timeout=PRESENCE_BACKSTOP_SECONDS)
                producers = [
                    thread for thread in threading.enumerate()
                    if thread.name == "cctally-diagnosis"
                    and thread not in before
                ]
                assert len(producers) == 1, (
                    "the expired diagnosis's producer was not found waiting"
                )
                allow_return.set()
                # The retired producer frees the slot once it returns
                # (20fc159d5), and only once its worker's retirement is shown
                # (#1087): the worker's group is gone by then.
                assert slot.acquire(timeout=PRESENCE_BACKSTOP_SECONDS), (
                    "the retired producer never released the diagnosis slot"
                )
                slot.release()
                with pytest.raises(ProcessLookupError):
                    os.killpg(sessions[0], 0)
                assert _live_session_members(sessions[0]) == []
                for producer in producers:
                    producer.join(PRESENCE_BACKSTOP_SECONDS)
                    assert not producer.is_alive(), (
                        "the expired diagnosis's producer never finished"
                    )
            assert late_builds == [], "an expired queued diagnosis started"
    finally:
        # Whatever failed, every producer this test started is let go and
        # settled before its patches are undone. The held windows have
        # already released the clock, so each one ends within its own
        # bounded wait.
        allow_return.set()
        for producer in threading.enumerate():
            if (producer.name == "cctally-diagnosis"
                    and producer not in threads_before):
                producer.join(PRESENCE_BACKSTOP_SECONDS)

    # The recovery itself is asserted, not how fast it runs: its flight keeps
    # the same 0.25 s deadline, held so that a host stall between the flight's
    # creation and its producer's last check cannot expire it (#962).
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
    ) as executor, clock.held():
        recovery = executor.submit(
            admission.run, ("b", True, False), lambda: "recovered",
            bounded_build=lambda _remaining, retirement=None: "recovered",
        )
        assert recovery.result(timeout=PRESENCE_BACKSTOP_SECONDS) == (
            "recovered"
        )


# --- #1087: the worker retires itself (spec §4.10, test 12; plan §R G1-G2) -------------------

@pytest.fixture(params=[("spawn",), ("forkserver", "spawn")], ids=["spawn", "forkserver"])
def start_methods(request, monkeypatch):
    """The worker's context, forced as the product chooses it: from
    `multiprocessing.get_all_start_methods()`."""
    import multiprocessing

    methods = list(request.param)
    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: list(methods))
    return methods[0]


def _sources():
    load_script()
    return sys.modules["cctally"]._load_sibling("_cctally_diagnosis_sources")


def _parent_signal_spy(monkeypatch):
    """Every real signal the parent sends through `os.killpg` or `os.kill`;
    signal 0 delivers nothing and is passed through unrecorded."""
    sent = []
    real_killpg, real_kill = os.killpg, os.kill

    def killpg(pgid, sig):
        if sig:
            sent.append(("killpg", pgid, sig))
        return real_killpg(pgid, sig)

    def kill(pid, sig):
        if sig:
            sent.append(("kill", pid, sig))
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(os, "kill", kill)
    return sent


def _live_session_members(sid):
    """The live processes whose session id is SID, read independently of the
    product: `/proc/<pid>/stat` on Linux, `ps` and `os.getsid` elsewhere
    (Darwin answers ESRCH for an unreaped zombie)."""
    members = []
    if os.path.isdir("/proc/self/task"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat") as stream:
                    fields = stream.read().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if fields[0] not in ("Z", "X") and int(fields[3]) == sid:
                members.append(int(name))
        return members
    listing = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True,
                             check=True).stdout
    for token in listing.split():
        try:
            if os.getsid(int(token)) == sid:
                members.append(int(token))
        except OSError:
            continue
    return members


def _settle_pending(error, *, within=PRESENCE_BACKSTOP_SECONDS):
    """Call a `_RetirementPending`'s `wait()` on a bounded thread; True once it returned."""
    wait = getattr(error, "wait", None)
    if wait is None:
        return True
    done = threading.Event()

    def run():
        wait()
        done.set()
    threading.Thread(target=run, daemon=True).start()
    return done.wait(within)


def _recording_sleeper(pid_path, seconds):
    """Pickle-safe worker-only task: records the worker's pid (its session and
    group id) and sleeps."""
    with open(pid_path, "w") as stream:
        stream.write(str(os.getpid()))
    time.sleep(seconds)


def test_the_deadline_stops_a_worker_without_a_parent_signal(tmp_path, monkeypatch, start_methods):
    """12(a): the dashboard signalled a process number on a user's machine
    (`killpg` of a group whose leader the forkserver may already have reaped).
    The parent sends no signal; the worker's group is gone at return."""
    sources = _sources()
    sent = _parent_signal_spy(monkeypatch)
    pid_path = tmp_path / "worker.pid"
    with pytest.raises(TimeoutError) as raised:
        sources._run_process_with_deadline(_recording_sleeper, (str(pid_path), 60), timeout_seconds=8.0)
    assert sent == [], f"the parent signalled {sent}"
    sid = int(pid_path.read_text())
    if getattr(raised.value, "wait", None) is None:
        assert _live_session_members(sid) == []
    else:
        # A worker still bootstrapping at the deadline is retired once its
        # watchdog runs the queued stop.
        assert _settle_pending(raised.value), "the pending retirement never settled"
        assert _live_session_members(sid) == []
    assert sent == [], f"the parent signalled {sent}"


_TERM_IGNORING_PROVIDER = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = sys.argv[1]
if sys.argv[2] == 'fork':
    if os.fork():
        os._exit(0)   # the provider exits at once; its child stays in the group
with open(path + '.tmp', 'w') as stream:
    stream.write(str(os.getpid()))
os.rename(path + '.tmp', path)
time.sleep(120)
"""


def _start_provider(path, mode="plain"):
    """In the worker: a provider child that ignores TERM (in the worker's
    group and session), started and recorded before the task goes on."""
    subprocess.Popen([sys.executable, "-c", _TERM_IGNORING_PROVIDER, path, mode])
    limit = time.monotonic() + 60
    while not os.path.exists(path) and time.monotonic() < limit:
        time.sleep(0.02)


def _provider_then_sleep(path, seconds):
    _start_provider(path)
    time.sleep(seconds)


def _provider_then_return(path, value):
    _start_provider(path)
    return value


def _forking_provider_then_crash(path):
    """Leaves a TERM-ignoring grandchild in the group and exits without a result."""
    _start_provider(path, "fork")
    os._exit(1)


def _quick_task(value):
    return value


def _recording_quick_task(pid_path, value):
    with open(pid_path, "w") as stream:
        stream.write(str(os.getpid()))
    return value


def _frame_spy(monkeypatch):
    """Every frame the parent writes on a multiprocessing Connection."""
    import multiprocessing.connection as connection

    frames = []
    real = connection.Connection.send_bytes

    def send_bytes(self, buf, *args, **kwargs):
        # One-byte frames only: a forkserver's own authentication messages
        # travel on a Connection too.
        if len(bytes(buf)) == 1:
            frames.append(bytes(buf))
        return real(self, buf, *args, **kwargs)
    monkeypatch.setattr(connection.Connection, "send_bytes", send_bytes)
    return frames


def _wait_for(predicate, within=PRESENCE_BACKSTOP_SECONDS):
    limit = time.monotonic() + within
    while time.monotonic() < limit:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def _read_pid(path, within=PRESENCE_BACKSTOP_SECONDS):
    def read():
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            return None
    return _wait_for(read, within)


def _pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _group_gone(sid):
    try:
        os.killpg(sid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def test_the_deadline_stops_a_term_ignoring_provider(tmp_path, monkeypatch, start_methods):
    """12(b): a provider child that ignores TERM outlived the diagnosis on a
    user's machine. The worker's group KILL ends it; at return it is gone."""
    sources = _sources()
    sent = _parent_signal_spy(monkeypatch)
    provider_path = tmp_path / "provider.pid"
    provider = None
    try:
        with pytest.raises(TimeoutError) as raised:
            sources._run_process_with_deadline(_provider_then_sleep, (str(provider_path), 60),
                                               timeout_seconds=8.0)
        provider = int(provider_path.read_text())
        if getattr(raised.value, "wait", None) is not None:
            assert _settle_pending(raised.value), "the pending retirement never settled"
        assert _wait_for(lambda: _pid_gone(provider)), "the TERM-ignoring provider outlived the worker's retirement"
        assert sent == [], f"the parent signalled {sent}"
    finally:
        if provider is not None:
            _kill_quietly(provider)


def test_a_stop_queued_before_the_watchdog_starts_is_executed(tmp_path, monkeypatch, start_methods):
    """12(c): a worker held before its watchdog starts. The parent's stop is
    written before the hold ends and never lost; the call raises
    `_RetirementPending`, whose `wait()` returns once the worker stopped its
    group. Through `_DiagnosisAdmission` the slot is held until then and the
    next flight is not admitted before. Detects a second worker tree started
    beside one that may still run, or a queued stop discarded."""
    sources = _sources()
    dash = _dash()
    frames = _frame_spy(monkeypatch)
    sent = _parent_signal_spy(monkeypatch)
    marker = tmp_path / "watchdog"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_WATCHDOG_ENV, str(marker))
    with pytest.raises(sources._RetirementPending) as raised:
        sources._run_process_with_deadline(_quick_task, ("report",), timeout_seconds=1.0)
    assert frames == [b"T"], frames
    sid = _read_pid(tmp_path / "watchdog.reached")
    assert sid is not None, "the worker never reached its hold"
    assert not _group_gone(sid), "the held worker was gone before its hold ended"
    marker.touch()
    assert _settle_pending(raised.value), "wait() never returned after the hold ended"
    assert _group_gone(sid) and _live_session_members(sid) == []

    # Through the admission: the slot stays held until wait() completes.
    frames.clear()
    marker2 = tmp_path / "watchdog-2"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_WATCHDOG_ENV, str(marker2))
    admission = dash._DiagnosisAdmission(deadline_seconds=3.0)
    outcomes = []

    def bounded(remaining, retirement=None):
        try:
            return sources._run_process_with_deadline(_quick_task, ("first",), timeout_seconds=remaining,
                                                      retirement=retirement)
        except BaseException as exc:
            outcomes.append(exc)
            raise

    with pytest.raises(dash._DiagnosisTimedOut):
        admission.run(("a", True, False), lambda: pytest.fail("unbounded build path ran"),
                      bounded_build=bounded)
    assert _wait_for(lambda: outcomes), "the producer never left its bounded build"
    assert isinstance(outcomes[0], sources._RetirementPending), outcomes
    sid = _read_pid(tmp_path / "watchdog-2.reached")
    assert not admission._admission.acquire(blocking=False), "the slot was released with the worker held"
    admission._deadline_seconds = PRESENCE_BACKSTOP_SECONDS
    admitted = []

    def next_build(remaining, retirement=None):
        admitted.append(_live_session_members(sid))
        return "next"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        following = executor.submit(admission.run, ("b", True, False),
                                    lambda: pytest.fail("unbounded build path ran"), bounded_build=next_build)
        time.sleep(1.0)
        assert admitted == [], "the next flight was admitted beside a held worker"
        marker2.touch()
        assert following.result(timeout=PRESENCE_BACKSTOP_SECONDS) == "next"
    assert admitted == [[]], admitted
    assert sent == [], f"the parent signalled {sent}"


def test_a_stop_while_the_result_is_published_ends_the_group(tmp_path, monkeypatch, start_methods):
    """12(d1): the deadline passes while the worker has written its result
    but not yet sent `D`, with a TERM-ignoring provider alive. The parent
    sends `T` and the worker ends through its group's KILL: the call times
    out, the provider is gone, and the admission slot is released only once
    the worker's session is empty. Detects a received stop discarded while
    the worker publishes its result (revision 4's stop lock)."""
    sources = _sources()
    dash = _dash()
    frames = _frame_spy(monkeypatch)
    sent = _parent_signal_spy(monkeypatch)
    marker = tmp_path / "result"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_RESULT_ENV, f"before-done:{marker}")
    provider_path = tmp_path / "provider.pid"
    admission = dash._DiagnosisAdmission(deadline_seconds=6.0)
    provider = None
    try:
        with pytest.raises(dash._DiagnosisTimedOut):
            admission.run(
                ("a", True, False), lambda: pytest.fail("unbounded build path ran"),
                bounded_build=lambda remaining, retirement=None: sources._run_process_with_deadline(
                    _provider_then_return, (str(provider_path), "late"), timeout_seconds=remaining,
                    retirement=retirement))
        sid = _read_pid(tmp_path / "result.reached")
        assert sid is not None, "the worker never reached the result-to-D window"
        provider = int(provider_path.read_text())
        assert admission._admission.acquire(timeout=PRESENCE_BACKSTOP_SECONDS), "the slot was never released"
        admission._admission.release()
        assert _live_session_members(sid) == [] and _group_gone(sid)
        assert _pid_gone(provider), "the provider outlived the stop"
        assert frames == [b"T"], frames
        assert sent == [], f"the parent signalled {sent}"
    finally:
        if provider is not None:
            _kill_quietly(provider)


def test_a_result_received_before_the_deadline_lets_the_worker_exit(tmp_path, monkeypatch, start_methods):
    """12(d2): the worker is held after writing its result and released
    before the deadline: `D` arrives, the parent answers `E` and returns the
    result, and the worker exits within the 2 s margin."""
    sources = _sources()
    frames = _frame_spy(monkeypatch)
    sent = _parent_signal_spy(monkeypatch)
    marker = tmp_path / "result"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_RESULT_ENV, f"before-done:{marker}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        call = executor.submit(sources._run_process_with_deadline, _quick_task, ("report",),
                               timeout_seconds=60.0)
        sid = _read_pid(tmp_path / "result.reached")
        assert sid is not None, "the worker never reached the result-to-D window"
        assert frames == [], frames
        marker.touch()
        assert call.result(timeout=PRESENCE_BACKSTOP_SECONDS) == "report"
    assert frames == [b"E"], frames
    assert _pid_gone(sid), "the worker had not exited when the call returned"
    assert sent == [], f"the parent signalled {sent}"


def test_a_provider_left_after_a_delivered_result_keeps_the_slot(tmp_path, monkeypatch, start_methods):
    """Plan §R G1 on the `D` path: a worker that delivered its result and
    exited, leaving a TERM-ignoring provider in its session. The call
    returns the result, and through `_DiagnosisAdmission` the slot stays
    held until that provider exits. Detects a slot released, and a second
    worker tree started, beside a process the first one left running."""
    sources = _sources()
    dash = _dash()
    sent = _parent_signal_spy(monkeypatch)
    provider_path = tmp_path / "provider.pid"
    admission = dash._DiagnosisAdmission(deadline_seconds=PRESENCE_BACKSTOP_SECONDS)
    provider = None
    try:
        assert admission.run(
            ("a", True, False), lambda: pytest.fail("unbounded build path ran"),
            bounded_build=lambda remaining, retirement=None: sources._run_process_with_deadline(
                _provider_then_return, (str(provider_path), "report"), timeout_seconds=remaining,
                retirement=retirement)) == "report"
        provider = int(provider_path.read_text())
        time.sleep(1.0)
        assert not _pid_gone(provider)
        assert not admission._admission.acquire(blocking=False), "the slot was released with the provider alive"
        assert sent == [], f"the parent signalled {sent}"
        os.kill(provider, signal.SIGKILL)
        assert admission._admission.acquire(timeout=PRESENCE_BACKSTOP_SECONDS), "the slot was never released"
        admission._admission.release()
    finally:
        if provider is not None:
            _kill_quietly(provider)


def test_a_worker_without_its_own_group_runs_nothing(tmp_path, monkeypatch):
    """12(e): `os.setsid` failing at the worker's entry. The payload is the
    group failure, the task never ran and no watchdog started, so the worker
    has no group to stop and nothing the parent would have to signal."""
    import multiprocessing
    import pickle

    sources = _sources()
    cmd_r, cmd_w = multiprocessing.Pipe(duplex=False)
    done_r, done_w = multiprocessing.Pipe(duplex=False)

    def refuse():
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "setsid", refuse)
    handler = signal.getsignal(signal.SIGTERM)
    threads = set(threading.enumerate())
    ran = []
    result_path = tmp_path / "result"
    try:
        sources._bounded_process_entry(str(result_path), ran.append, ("ran",), None, (), cmd_r, done_w, {})
        with open(result_path, "rb") as stream:
            payload = pickle.load(stream)
        assert payload[:2] == ("unexpected", "ProcessGroupError"), payload
        assert ran == []
        assert [t for t in threading.enumerate() if t not in threads] == []
        assert signal.getsignal(signal.SIGTERM) is handler
        assert not done_r.poll(0), "a worker without a group announced a result"
    finally:
        for conn in (cmd_r, cmd_w, done_r, done_w):
            conn.close()


def test_a_quick_diagnosis_returns_without_waiting_for_its_deadline(tmp_path, monkeypatch, start_methods):
    """12(f): a successful diagnosis stalled to its deadline, or returned
    before its worker had exited. `D` is answered with `E`; the worker's
    exit is confirmed before the call returns."""
    sources = _sources()
    frames = _frame_spy(monkeypatch)
    sent = _parent_signal_spy(monkeypatch)
    pid_path = tmp_path / "worker.pid"
    began = time.monotonic()
    assert sources._run_process_with_deadline(_recording_quick_task, (str(pid_path), "report"),
                                              timeout_seconds=60.0) == "report"
    elapsed = time.monotonic() - began
    sid = int(pid_path.read_text())
    assert frames == [b"E"], frames
    assert _pid_gone(sid), "the worker had not exited when the call returned"
    assert _live_session_members(sid) == []
    assert sent == [], f"the parent signalled {sent}"
    # timing-budget: one worker start and the 2 s margin; a call that waits
    # out its 60 s deadline exceeds this bound
    assert elapsed < 30, elapsed


def test_a_worker_that_dies_leaving_a_forked_provider_keeps_the_slot(tmp_path, monkeypatch, start_methods):
    """12(g), with plan §R G1: a worker that exits on its own without `D`,
    leaving a TERM-ignoring provider's child in its group (the provider
    forked it and exited at once). The parent signals nothing and raises
    `_RetirementPending`; the admission slot stays held until that child
    exits. Detects a slot released by an empty census while a process of
    the worker's group still runs."""
    sources = _sources()
    dash = _dash()
    sent = _parent_signal_spy(monkeypatch)
    child_path = tmp_path / "child.pid"
    admission = dash._DiagnosisAdmission(deadline_seconds=30.0)
    outcomes = []
    child = None

    def bounded(remaining, retirement=None):
        try:
            return sources._run_process_with_deadline(_forking_provider_then_crash, (str(child_path),),
                                                      timeout_seconds=remaining, retirement=retirement)
        except BaseException as exc:
            outcomes.append(exc)
            raise
    try:
        with pytest.raises(dash._DiagnosisTimedOut):
            admission.run(("a", True, False), lambda: pytest.fail("unbounded build path ran"),
                          bounded_build=bounded)
        assert _wait_for(lambda: outcomes)
        assert isinstance(outcomes[0], sources._RetirementPending), outcomes
        child = int(child_path.read_text())
        assert not _pid_gone(child)
        time.sleep(1.0)
        assert not admission._admission.acquire(blocking=False), "the slot was released with the child alive"
        assert sent == [], f"the parent signalled {sent}"
        os.kill(child, signal.SIGKILL)
        assert admission._admission.acquire(timeout=PRESENCE_BACKSTOP_SECONDS), "the slot was never released"
        admission._admission.release()
        assert _pid_gone(child)
    finally:
        if child is not None:
            _kill_quietly(child)


def test_a_worker_held_after_its_result_is_stopped_and_the_slot_waits(tmp_path, monkeypatch, start_methods):
    """12(h), plan §R G2: `D` does not prove the worker exited. A worker held
    after sending `D`: the call returns the result, the parent sent `E` and
    then `T` (the watchdog keeps reading after `E`), the worker's group is
    gone, and through `_DiagnosisAdmission` the slot is released only once
    the predicate holds. Without a sink (the CLI path) the call returns
    after sending `T`."""
    sources = _sources()
    dash = _dash()
    frames = _frame_spy(monkeypatch)
    sent = _parent_signal_spy(monkeypatch)
    marker = tmp_path / "after-done"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_RESULT_ENV, str(marker))
    admission = dash._DiagnosisAdmission(deadline_seconds=PRESENCE_BACKSTOP_SECONDS)
    assert admission.run(
        ("a", True, False), lambda: pytest.fail("unbounded build path ran"),
        bounded_build=lambda remaining, retirement=None: sources._run_process_with_deadline(
            _quick_task, ("report",), timeout_seconds=remaining, retirement=retirement)) == "report"
    sid = _read_pid(tmp_path / "after-done.reached")
    assert frames == [b"E", b"T"], frames
    assert admission._admission.acquire(timeout=PRESENCE_BACKSTOP_SECONDS), "the slot was never released"
    admission._admission.release()
    assert _group_gone(sid) and _live_session_members(sid) == [], "the slot was released before the predicate held"

    frames.clear()
    marker = tmp_path / "after-done-cli"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_RESULT_ENV, str(marker))
    assert sources._run_process_with_deadline(_quick_task, ("cli",), timeout_seconds=60.0) == "cli"
    assert frames == [b"E", b"T"], frames
    sid = _read_pid(tmp_path / "after-done-cli.reached")
    import multiprocessing

    def stopped():
        multiprocessing.active_children()   # reap a spawn worker nothing joined
        return _group_gone(sid)
    assert _wait_for(stopped), "the CLI worker never stopped its group"
    assert sent == [], f"the parent signalled {sent}"


_SELF_ENDING_MEMBER = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = sys.argv[1]
with open(path + '.tmp', 'w') as stream:
    stream.write(str(os.getpid()))
os.rename(path + '.tmp', path)
time.sleep(float(sys.argv[2]))
"""
_MEMBER_BOUND_S = 15.0


def _self_ending_member_then_return(path, value):
    """In the worker: leave a TERM-ignoring member in the worker's group and
    session that ends by itself after `_MEMBER_BOUND_S`, then return."""
    subprocess.Popen([sys.executable, "-c", _SELF_ENDING_MEMBER, path, str(_MEMBER_BOUND_S)])
    limit = time.monotonic() + 60
    while not os.path.exists(path) and time.monotonic() < limit:
        time.sleep(0.02)
    return value


def test_a_stop_begun_after_the_result_runs_its_kill_before_the_worker_exits(
        tmp_path, monkeypatch, start_methods):
    """12(h), #1087 F4: the worker is held after `D` beyond the margin, so the
    parent sends `E` and then `T`; the hold ends as `T` is sent, so the
    worker's main thread finishes during the stop's grace. It used to exit
    there, before the watchdog's KILL, leaving the TERM-ignoring member its
    task left running until it ended by itself (and the admission slot held
    as long). The worker waits for a begun stop, so the group KILL ends the
    member, and the worker, well before the member's own bound; the slot is
    released only once the predicate holds."""
    import multiprocessing.connection as connection

    sources = _sources()
    dash = _dash()
    sent = _parent_signal_spy(monkeypatch)
    marker = tmp_path / "after-done"
    member_path = tmp_path / "member.pid"
    monkeypatch.setenv(sources._DIAGNOSIS_TEST_HOLD_RESULT_ENV, str(marker))
    frames, t_term = [], []
    real = connection.Connection.send_bytes

    def send_bytes(self, buf, *args, **kwargs):
        out = real(self, buf, *args, **kwargs)
        frame = bytes(buf)
        if len(frame) == 1:     # a forkserver's authentication travels on a Connection too
            frames.append(frame)
            if frame == b"T":
                t_term.append(time.monotonic())
                marker.write_text("")   # the worker's main thread goes on, mid-grace
        return out
    monkeypatch.setattr(connection.Connection, "send_bytes", send_bytes)
    admission = dash._DiagnosisAdmission(deadline_seconds=PRESENCE_BACKSTOP_SECONDS)
    assert admission.run(
        ("a", True, False), lambda: pytest.fail("unbounded build path ran"),
        bounded_build=lambda remaining, retirement=None: sources._run_process_with_deadline(
            _self_ending_member_then_return, (str(member_path), "report"),
            timeout_seconds=remaining, retirement=retirement)) == "report"
    assert frames == [b"E", b"T"], frames
    member = int(member_path.read_text())
    sid = _read_pid(tmp_path / "after-done.reached")
    # timing-budget: the stop's 0.5 s grace and a loaded host's start-up; a
    # member left to its own 15 s bound (no KILL) is still alive here.
    until = t_term[0] + 5.0 - time.monotonic()
    assert _wait_for(lambda: _pid_gone(member), within=max(until, 0.1)), (
        "the TERM-ignoring member outlived the stop: the worker exited before its KILL")
    assert admission._admission.acquire(timeout=PRESENCE_BACKSTOP_SECONDS), "the slot was never released"
    admission._admission.release()
    assert _group_gone(sid) and _live_session_members(sid) == [], "the slot was released before the predicate held"
    assert sent == [], f"the parent signalled {sent}"


_FORKED_PROVIDER_PROBE = r"""
import json, os, signal, sys, time
sys.path[:0] = [sys.argv[1], os.path.join(os.path.dirname(sys.argv[1]), "bin")]
from _script_loader import load_script_module
sources = load_script_module()._load_sibling("_cctally_diagnosis_sources")
# The worker's state when it forks the Codex provider: TERM is a no-op, so the
# worker can outlive its group's TERM and send the KILL.
signal.signal(signal.SIGTERM, sources._diagnosis_ignore_term)
sources._build_provider_task = lambda args: time.sleep(args)   # bounded: 8 s
pid, read_fd = sources._start_forked_provider(8)
time.sleep(0.5)
os.kill(pid, signal.SIGTERM)        # its own unreaped child
limit = time.monotonic() + 5
status = None
while status is None and time.monotonic() < limit:
    waited, code = os.waitpid(pid, os.WNOHANG)
    if waited:
        status = code
    else:
        time.sleep(0.02)
within = status is not None
if status is None:
    _waited, status = os.waitpid(pid, 0)   # it ends by itself after its 8 s
os.close(read_fd)
print(json.dumps({"within": within, "signaled": os.WIFSIGNALED(status),
                  "sig": os.WTERMSIG(status) if os.WIFSIGNALED(status) else None}))
"""


def test_the_forked_provider_dies_on_term(tmp_path):
    """#1087 F3: the worker ignores TERM so it can send its group's KILL, and
    the Codex provider it forks inherited that no-op handler, so a TERM to
    the provider (the group's TERM, or an operator's) left it running to the
    end of its build. The provider restores the default first and dies on
    TERM, as it did before the worker ignored it."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), CCTALLY_DATA_DIR=str(home / "data"))
    done = subprocess.run([sys.executable, "-c", _FORKED_PROVIDER_PROBE, str(pathlib.Path(__file__).parent)],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report == {"within": True, "signaled": True, "sig": signal.SIGTERM}, report


@pytest.mark.parametrize("exitcode,probe,census,retired", [
    (None, None, False, False),                          # exit not confirmed
    (0, None, False, False),                             # the probe succeeds: a member is left
    (0, ProcessLookupError(3, "x"), False, True),        # group gone, census empty
    (0, PermissionError(1, "x"), False, False),          # a member we may not signal, or a zombie
    (0, OSError(22, "x"), False, False),                 # any other probe error
    (0, ProcessLookupError(3, "x"), None, False),        # unreadable census
    (0, ProcessLookupError(3, "x"), True, False),        # a member that left the group
])
def test_the_release_predicate(monkeypatch, exitcode, probe, census, retired):
    """Plan §R G1: a bare empty census never releases the slot; only a
    confirmed exit, `killpg(S, 0)` raising ESRCH and an empty census do."""
    import types

    sources = _sources()
    probes = []

    def killpg(pgid, sig):
        probes.append((pgid, sig))
        if probe is not None:
            raise probe
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(sources, "_session_members", lambda sid: census)
    assert sources._worker_retired(types.SimpleNamespace(exitcode=exitcode), 4242) is retired
    assert all(sig == 0 for _pgid, sig in probes), probes


def _fake_proc(root, pid, state, session, tasks=None):
    base = root / str(pid)
    (base / "task").mkdir(parents=True)
    (base / "stat").write_text(f"{pid} (a) b) {state} 1 {pid} {session} 0 0\n")
    for task, task_state in (tasks or {pid: state}).items():
        (base / "task" / str(task)).mkdir()
        (base / "task" / str(task) / "stat").write_text(f"{task} (a) {task_state} 1 {pid} {session}\n")


def test_the_linux_census_reads_thread_group_death(tmp_path):
    """Plan §R G3 for the diagnosis census, in-process over a fake `/proc`:
    a zombie leader with a live thread is live, an all-zombie process is
    not, another session is not counted, and an unreadable listing is no
    proof of emptiness."""
    sources = _sources()
    root = tmp_path / "proc"
    root.mkdir()
    _fake_proc(root, 10, "S", 99)
    _fake_proc(root, 20, "Z", 77, tasks={20: "Z", 21: "Z"})
    assert sources._proc_session_members(77, str(root)) is False
    assert sources._proc_session_members(99, str(root)) is True
    _fake_proc(root, 30, "Z", 77, tasks={30: "Z", 31: "S"})
    assert sources._proc_session_members(77, str(root)) is True
    assert sources._proc_session_members(77, str(tmp_path / "missing")) is None


def test_the_linux_census_reads_esrch_as_gone(tmp_path, monkeypatch):
    """A process or task that exits between the `/proc` listing and its stat
    read answers ESRCH (as `_lib_hold`'s census treats it), not only ENOENT.
    Read as unreadable, that kept the admission slot held for a session that
    was already empty, and a task's ESRCH counted a dead process as live."""
    import builtins
    import errno

    sources = _sources()
    root = tmp_path / "proc"
    root.mkdir()
    _fake_proc(root, 20, "Z", 77, tasks={20: "Z", 21: "Z"})
    _fake_proc(root, 40, "S", 77)                               # its stat read: ESRCH
    _fake_proc(root, 50, "Z", 77, tasks={50: "Z", 51: "S"})     # task 51's read: ESRCH
    failing = {str(root / "40" / "stat"): errno.ESRCH,
               str(root / "50" / "task" / "51" / "stat"): errno.ESRCH}
    real_open = builtins.open

    def failing_open(path, *args, **kwargs):
        code = failing.get(str(path))
        if code is not None:
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, *args, **kwargs)
    # The module's own `open`, so nothing else in this process is affected.
    monkeypatch.setattr(sources, "open", failing_open, raising=False)
    assert sources._proc_session_members(77, str(root)) is False
    # An unreadable read other than gone is still no proof of emptiness.
    failing[str(root / "40" / "stat")] = errno.EACCES
    assert sources._proc_session_members(77, str(root)) is None


def test_the_listing_census_counts_only_live_session_members():
    """The census elsewhere (Darwin): a pid answering ESRCH (gone, or an
    unreaped zombie) is not live; an unreadable session read or listing is
    no proof of emptiness."""
    sources = _sources()
    sids = {1: 1, 2: 77, 3: ProcessLookupError(3, "x"), 4: PermissionError(1, "x")}

    def getsid(pid):
        value = sids[pid]
        if isinstance(value, BaseException):
            raise value
        return value
    assert sources._ps_session_members(77, listing="1 3\n2\n", getsid=getsid) is True
    assert sources._ps_session_members(77, listing="1 3\n", getsid=getsid) is False
    assert sources._ps_session_members(77, listing="1 4\n", getsid=getsid) is None
    assert sources._ps_session_members(77, listing="1 x\n", getsid=getsid) is None


def test_failed_diagnosis_wakes_coalesced_callers_and_recovers(monkeypatch):
    load_script()
    dash = _dash()
    admission = dash._DiagnosisAdmission(deadline_seconds=5)
    # #962: the shared failure and the recovery are asserted, not the
    # deadline, so a host stall must not expire either flight first.
    clock = _hold_admission_clock(monkeypatch, dash, admission)
    entered = threading.Event()
    release = threading.Event()
    builds = 0
    failure_backstop = 20.0

    def _fails():
        nonlocal builds
        builds += 1
        entered.set()
        assert release.wait(failure_backstop)
        raise RuntimeError("synthetic producer failure")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=2,
    ) as executor, clock.held():
        first = executor.submit(admission.run, ("a", True, False), _fails)
        assert entered.wait(failure_backstop)
        follower = executor.submit(admission.run, ("a", True, False), _fails)
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: admission._flights[("a", True, False)].callers == 2,
                timeout=failure_backstop,
            )
        release.set()
        with pytest.raises(RuntimeError, match="synthetic producer failure"):
            first.result(timeout=failure_backstop)
        with pytest.raises(RuntimeError, match="synthetic producer failure"):
            follower.result(timeout=failure_backstop)
    assert builds == 1
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
    ) as executor, clock.held():
        recovery = executor.submit(
            admission.run, ("a", True, False), lambda: "recovered",
        )
        assert recovery.result(timeout=failure_backstop) == "recovered"


def test_diagnosis_burst_over_64_caps_admitted_waiters_and_producers(
    monkeypatch,
):
    load_script()
    dash = _dash()
    admission = dash._DiagnosisAdmission(max_callers=24, max_per_key=16,
                                         max_keys=8, deadline_seconds=5)
    # #962: the caps and the single build are asserted, not the deadline, so
    # a host stall must not expire the admitted callers' flight first.
    clock = _hold_admission_clock(monkeypatch, dash, admission)
    release = threading.Event()
    launched = threading.Barrier(81)
    counts = {"builds": 0, "overloaded": 0}
    changed = threading.Condition()

    def _build():
        with changed:
            counts["builds"] += 1
            changed.notify_all()
        assert release.wait(PRESENCE_BACKSTOP_SECONDS)
        return "one report"

    def _request():
        launched.wait(PRESENCE_BACKSTOP_SECONDS)
        try:
            return admission.run(("same", True, False), _build)
        except dash._DiagnosisOverloaded:
            with changed:
                counts["overloaded"] += 1
                changed.notify_all()
            return "overloaded"

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=80,
    ) as executor, clock.held():
        futures = [executor.submit(_request) for _ in range(80)]
        launched.wait(PRESENCE_BACKSTOP_SECONDS)
        with changed:
            assert changed.wait_for(
                lambda: counts["overloaded"] == 64,
                timeout=PRESENCE_BACKSTOP_SECONDS,
            ), "the burst did not deterministically overload excess callers"
            # The producer starts on its own thread, so its build is awaited
            # rather than assumed to have begun.
            assert changed.wait_for(
                lambda: counts["builds"] >= 1,
                timeout=PRESENCE_BACKSTOP_SECONDS,
            ), "the burst's producer never started its build"
            assert counts["builds"] == 1
        with admission._changed:
            assert admission._callers == 16
            assert len(admission._flights) == 1
        release.set()
        results = [future.result(timeout=PRESENCE_BACKSTOP_SECONDS)
                   for future in futures]
    assert results.count("one report") == 16
    assert results.count("overloaded") == 64


def test_diagnosis_route_maps_saturation_and_timeout_to_safe_503(
    empty_server, monkeypatch,
):
    dash = _dash()
    admission = dash._DiagnosisAdmission(max_callers=1, max_per_key=1,
                                         max_keys=1, deadline_seconds=0.15)
    monkeypatch.setattr(dash, "_DIAGNOSIS_ADMISSION", admission)
    sources = sys.modules["cctally"]._load_sibling(
        "_cctally_diagnosis_sources"
    )
    entered = threading.Event()
    release = threading.Event()
    real_build = sources.build_diagnosis

    def _stuck(*args, **kwargs):
        entered.set()
        release.wait(PRESENCE_BACKSTOP_SECONDS)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(sources, "build_diagnosis", _stuck)
    _run_bounded_in_process(monkeypatch, sources)
    path = f"/api/diagnosis?window={_WINDOW_QUERY}"
    # #962: the busy answer needs the first caller still admitted when the
    # second request arrives, so the clock is held until that answer; the
    # first request's 0.15 s deadline then expires from the hold's start.
    clock = _hold_admission_clock(monkeypatch, dash, admission)
    threads_before = set(threading.enumerate())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            with clock.held():
                first = executor.submit(empty_server.get, path)
                assert entered.wait(PRESENCE_BACKSTOP_SECONDS)
                overloaded = empty_server.get(path)
            assert overloaded.status == 503
            assert overloaded.json == {
                "error": "diagnosis busy; retry shortly",
                "code": "diagnosis_overloaded",
            }
            timed_out = first.result(timeout=PRESENCE_BACKSTOP_SECONDS)
            assert timed_out.status == 503
            assert timed_out.json == {
                "error": "diagnosis timed out; retry shortly",
                "code": "diagnosis_timeout",
            }
    finally:
        # Whatever failed, the stuck build is let go and its producer settled
        # before the server's paths and patches are undone.
        release.set()
        for producer in threading.enumerate():
            if (producer.name == "cctally-diagnosis"
                    and producer not in threads_before):
                producer.join(PRESENCE_BACKSTOP_SECONDS)


def test_the_diagnosis_is_not_an_envelope_key(tmp_path, monkeypatch):
    """B8: the diagnosis is on-demand, never a per-tick envelope cost."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    envelope = ns["snapshot_to_envelope"](ns["_empty_dashboard_snapshot"](),
                                          now_utc=WINDOW_END)
    assert "diagnosis" not in envelope
    assert "explain" not in envelope


# --- the status mapping -------------------------------------------------

def test_a_ranked_report_is_200(rich_server):
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body
    payload = response.json
    assert payload["schemaVersion"] == 1
    assert payload["overallVerdict"] == "contributor_detected"
    assert payload["results"][0]["contributors"]


def test_valid_report_is_200_even_when_every_class_is_withheld(empty_server):
    """A withheld answer is a correct answer about what the store holds."""
    response = empty_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body
    assert response.json["overallVerdict"] == "withheld"


def test_malformed_window_is_400(rich_server):
    response = rich_server.get("/api/diagnosis?window=nonsense")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_an_unresolvable_account_is_400(rich_server):
    response = rich_server.get(
        f"/api/diagnosis?window={_WINDOW_QUERY}&account=no-such-account")
    assert response.status == 400, response.body
    assert response.json["code"] == "account_unresolved"


def test_account_with_source_all_is_400(rich_server):
    """Account keys are provider-scoped; one selector cannot address both.
    The CLI exits 2 on the same condition."""
    response = rich_server.get(
        f"/api/diagnosis?window={_WINDOW_QUERY}&source=all&account=x")
    assert response.status == 400, response.body


def test_an_unreadable_store_publishes_the_report_with_503(storeless_server):
    """The same condition the CLI turns into exit 3, and for the same reason
    the CLI still prints: a person needs to see the typed cause."""
    response = storeless_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 503, response.body
    payload = response.json
    assert payload["overallCode"] == "provider_unavailable"
    assert payload["results"][0]["denominator"]["usd"]["state"] == "withheld"


def test_a_store_failure_resolving_the_window_is_503(rich_server, monkeypatch):
    """The route's `store_unavailable` arm, reached.

    `build_diagnosis` converts every store failure it meets into a withheld
    provider result, so the only `store_unavailable` that escapes as an
    `EstablishmentFailure` comes from selector resolution — the week anchor,
    which reads stats.db through the ordinary opener. Before that read was
    classified, a malformed store here surfaced as an unexplained 500.
    """
    import sqlite3

    dk = load_script()["_load_sibling"]("_lib_diff_kernel")

    def _raise(*_a, **_kw):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(dk, "_diff_resolve_anchor", _raise)
    response = rich_server.get("/api/diagnosis?window=this-week")
    assert response.status == 503, response.body
    assert response.json["code"] == "store_unavailable"


def test_an_unresolvable_week_anchor_is_400_not_500(rich_server, monkeypatch):
    """A token the grammar cannot resolve without an anchor the machine does
    not hold is an unresolved RANGE, which is the 400 arm. It reached the
    outer handler as a bare `RuntimeError` and was served as 500."""
    dk = load_script()["_load_sibling"]("_lib_diff_kernel")
    monkeypatch.setattr(dk, "_diff_resolve_anchor", lambda *_a, **_kw: (None, None))
    response = rich_server.get("/api/diagnosis?window=this-week")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


@pytest.mark.parametrize("state", ["schema_ahead", "missing_lock",
                                   "recovery_pending"])
def test_a_refused_stats_store_resolving_the_window_is_503_not_no_anchor(
        tmp_path, monkeypatch, state):
    """R32-3 / R32-V4 (#787 / #861 revisions 32-33, browser finding B3): a
    stats.db that exists but is refused (version ahead, a missing
    maintenance lock beside it, an interrupted rebuild) is unavailable, not
    a store with no subscription-week anchor. The anchor helper turned the
    refusal into "no anchor", so Explain answered 400 "cannot resolve week
    token 'this-week'" and told the user to record usage while the owner
    resolved the store. Detects that 400, and a route that never tells the
    owner about an owner-resolved refusal."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_AS_OF",
                       WINDOW_END.isoformat().replace("+00:00", "Z"))
    core = sys.modules["_cctally_core"]
    ns["open_cache_db"]().close()
    ns["open_db"]().close()
    stats = pathlib.Path(core.DB_PATH)
    if state == "schema_ahead":
        import sqlite3
        conn = sqlite3.connect(stats)
        conn.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH + 1}")
        conn.commit()
        conn.close()
    elif state == "missing_lock":
        pathlib.Path(core.STATS_LOCK_MAINTENANCE_PATH).unlink()
    else:
        stats.with_name(stats.name + ".rebuilding-20260101T000000_000000"
                        ).write_text("")
    heard: list = []
    monkeypatch.setattr(core, "_OWNER_ATTENTION_COALESCE_S", 0.0)
    monkeypatch.setattr(core, "_OWNER_ATTENTION_CALLBACKS", [
        lambda store, r: heard.append((store, r.readable, r.reason))])
    server, thread, client = _boot(ns)
    try:
        response = client.get("/api/diagnosis?window=this-week")
    finally:
        stop(server, thread)
    assert response.status == 503, response.body
    assert response.json["code"] == "store_unavailable", response.json
    # Q-W1: the `error` field renders the refusal through the existing
    # sentence; the query layer's internal detail (`stats.db is not readable
    # (schema_ahead)`, `maintenance lock is absent`, `recovery is pending`)
    # is the leak this detects.
    error = response.json["error"]
    assert "stats.db could not be read" in error, error
    for leak in ("not readable", state, "lock is absent", "is pending"):
        assert leak not in error, error
    assert ("stats", False, state if state != "missing_lock" else "absent"
            ) in heard, heard


@pytest.mark.parametrize("fixture", ["storeless_server", "empty_server"])
def test_a_missing_or_snapshotless_stats_store_keeps_the_no_anchor_400(
        request, fixture):
    """R32-V4: only a stats.db that does not exist, or a successful read
    that finds no usage snapshot, is "no subscription-week anchor" (400
    `range_unresolved`). Detects the R32-3 propagation reporting a store
    with no snapshots, or no store at all, as unavailable."""
    client = request.getfixturevalue(fixture)
    response = client.get("/api/diagnosis?window=this-week")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved", response.json


def test_generation_incoherence_is_retried_before_the_route_answers(
        rich_server, monkeypatch):
    sources = load_script()["_load_sibling"]("_cctally_diagnosis_sources")
    real_probe = sources._probe_component
    stats_probes = []

    def _probe(component, bundle):
        if component == "stats":
            stats_probes.append(component)
            if len(stats_probes) <= 4:
                return f"changing-{len(stats_probes)}"
        return real_probe(component, bundle)

    monkeypatch.setattr(sources, "_probe_component", _probe)
    _run_bounded_in_process(monkeypatch, sources)
    response = rich_server.get(
        f"/api/diagnosis?source=codex&window={_WINDOW_QUERY}"
    )

    assert response.status == 200, response.body
    assert len(stats_probes) >= 6
    assert response.json["results"][0]["source"] == "codex"


def test_generation_incoherent_is_503(rich_server, monkeypatch):
    sources = load_script()["_load_sibling"]("_cctally_diagnosis_sources")
    probes = iter(["a", "b", "b", "c"] * 40)
    monkeypatch.setattr(sources, "_probe_component",
                        lambda *_a, **_kw: next(probes))
    _run_bounded_in_process(monkeypatch, sources)
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 503, response.body
    assert response.json["code"] == "generation_incoherent"


def test_an_unexpected_exception_is_500_and_never_a_healthy_200(
    rich_server, monkeypatch,
):
    sources = load_script()["_load_sibling"]("_cctally_diagnosis_sources")

    def _raises(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sources, "build_diagnosis", _raises)
    _run_bounded_in_process(monkeypatch, sources)
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 500, response.body
    assert "no_contributor_detected" not in response.body
    assert "contributor_detected" not in response.body


# --- gating and headers -------------------------------------------------

def test_no_csrf_required_for_this_read_only_route(rich_server):
    """`_check_origin_csrf` is opt-in for mutating routes. This one mutates
    nothing, so a request carrying no Origin is served."""
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body


def test_response_sets_no_cache(rich_server):
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.headers["Cache-Control"] == "no-cache"
    assert response.headers["Content-Type"].startswith("application/json")


def test_api_auth_applies_to_the_route(rich_server, monkeypatch):
    """`_require_api_auth` runs before dispatch, so the route inherits it."""
    handler = _dash().DashboardHTTPHandler
    monkeypatch.setattr(handler, "cctally_api_token", "sekret")
    assert rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}").status == 401
    ok = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}",
                         headers={"Authorization": "Bearer sekret"})
    assert ok.status == 200, ok.body


# --- read-only ----------------------------------------------------------

def test_the_route_never_opens_a_writable_store(rich_server, monkeypatch):
    """Read-only is structural: the route must not migrate, repair or write.

    The writable openers are the ones that perform schema work, migration,
    legacy import, contract repair and replay. Reaching either from this
    request is the failure, and it is observable without trusting a grep.
    """
    ns = load_script()
    opened: list[str] = []
    for name in ("open_cache_db", "open_db"):
        real = ns[name]

        def _record(*a, _name=name, _real=real, **k):
            opened.append(_name)
            return _real(*a, **k)

        monkeypatch.setitem(ns, name, _record)
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}")
    assert response.status == 200, response.body
    assert opened == []


# --- selectors ----------------------------------------------------------

def test_reveal_projects_widens_only_the_label(rich_server):
    anonymized = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}").json
    revealed = rich_server.get(
        f"/api/diagnosis?window={_WINDOW_QUERY}&reveal_projects=1").json

    def _projects(payload):
        return [row for result in payload["results"]
                for row in result["contributors"]
                if row["subjectKind"] == "project"]

    assert _projects(anonymized), "non-vacuity: a project row must be reported"
    for row in _projects(anonymized):
        assert row["subjectLabel"].startswith("project-")
    for row in _projects(revealed):
        assert not row["subjectLabel"].startswith("project-")
        # `--reveal-projects` widens the LABEL only; the published key stays
        # the response-local alias in every mode.
        assert row["subjectKey"].startswith("project-")
    for row in _projects(anonymized) + _projects(revealed):
        assert "/" not in row["subjectLabel"], "a filesystem path escaped"


def test_explicit_half_open_bounds_are_honoured(rich_server):
    """The alert-follow path carries instants, not dates: a five-hour block
    start is not a calendar day, so the route must accept the bounds the
    warning actually recorded."""
    start = WINDOW_START.isoformat().replace("+00:00", "Z")
    end = WINDOW_END.isoformat().replace("+00:00", "Z")
    response = rich_server.get(
        f"/api/diagnosis?start_at={start}&end_at={end}")
    assert response.status == 200, response.body
    assert response.json["window"]["startAt"] == start
    assert response.json["window"]["endAt"] == end


def test_half_open_bounds_must_be_ordered(rich_server):
    start = WINDOW_END.isoformat().replace("+00:00", "Z")
    end = WINDOW_START.isoformat().replace("+00:00", "Z")
    response = rich_server.get(
        f"/api/diagnosis?start_at={start}&end_at={end}")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_a_malformed_bound_is_400(rich_server):
    response = rich_server.get(
        "/api/diagnosis?start_at=not-a-time&end_at=also-not")
    assert response.status == 400, response.body
    assert response.json["code"] == "range_unresolved"


def test_source_all_publishes_one_result_per_provider(rich_server):
    response = rich_server.get(f"/api/diagnosis?window={_WINDOW_QUERY}&source=all")
    assert response.status == 200, response.body
    assert [r["source"] for r in response.json["results"]] == ["claude",
                                                               "codex"]


def test_concurrent_identical_diagnoses_share_one_process_wide_build(
    empty_server, monkeypatch,
):
    """Concurrent tabs cannot multiply the largest diagnosis process tree.

    The real HTTP server supplies one request thread per client.  Holding the
    first build open makes a second entry observable without relying on route
    timing, while the ``source=all`` selector keeps the production worker
    shape in scope.  Identical callers must join that in-flight result: one
    admitted build means one isolated provider worker and one report heap.
    """
    sources = sys.modules["cctally"]._load_sibling(
        "_cctally_diagnosis_sources"
    )
    real_build = sources.build_diagnosis
    lock = threading.Lock()
    first_entered = threading.Event()
    another_entered = threading.Event()
    release = threading.Event()
    admission = _dash()._DiagnosisAdmission()
    builds = 0
    active = 0
    peak_active = 0

    def _blocked_build(*args, **kwargs):
        nonlocal builds, active, peak_active
        with lock:
            builds += 1
            active += 1
            peak_active = max(peak_active, active)
            if builds == 1:
                first_entered.set()
            else:
                another_entered.set()
        try:
            assert release.wait(PRESENCE_BACKSTOP_SECONDS), (
                "the concurrent-route test did not release"
            )
            return real_build(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sources, "build_diagnosis", _blocked_build)
    _run_bounded_in_process(monkeypatch, sources)
    monkeypatch.setattr(_dash(), "_DIAGNOSIS_ADMISSION", admission)
    path = f"/api/diagnosis?window={_WINDOW_QUERY}&source=all"
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(empty_server.get, path)
        assert first_entered.wait(PRESENCE_BACKSTOP_SECONDS), (
            "the first diagnosis never started"
        )
        followers = [executor.submit(empty_server.get, path) for _ in range(2)]
        with admission._changed:
            assert admission._changed.wait_for(
                lambda: any(
                    flight.callers == 3
                    for flight in admission._flights.values()
                ),
                timeout=PRESENCE_BACKSTOP_SECONDS,
            ), "the concurrent diagnosis callers did not all register"
            assert not another_entered.is_set(), (
                "more than one diagnosis build was admitted"
            )
        release.set()
        responses = [first.result(), *(future.result() for future in followers)]

    assert builds == 1, "identical concurrent requests were not single-flight"
    assert peak_active == 1
    assert [response.status for response in responses] == [200, 200, 200]
