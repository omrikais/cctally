"""#313 P2 (F10): dashboard sync-thread work-proportional cooldown.

The automatic sync thread must sleep to a monotonic deadline
``t0 + max(interval, work)`` so its CPU duty is bounded (worst case 50% of one
core when work >= interval), while normal-cadence operation (work < interval)
is byte-for-byte unchanged.

#583 S2 adds the request-driven half. A queued request may start a rebuild
earlier than the automatic deadline, but never before ``t0 + 2*work`` — the
same ``period >= 2*work`` that caps duty at 50% of one core. The loop's
injected collaborator therefore changed from a test-and-clear
``take_sync_request`` to a peek-then-capture pair, and it gained a failure
boundary so an escaped exception still settles the batch it had captured.

Every bound here is asserted algebraically on a virtual clock. No test
measures elapsed wall-clock time.
"""
import importlib
import threading

from conftest import load_script


def _dash():
    load_script()  # sets sys.path so sibling modules import
    return importlib.import_module("_cctally_dashboard")


def test_next_deadline_normal_case_period_is_work_plus_interval():
    dash = _dash()
    # work < interval → period = work + interval (byte-identical to old loop):
    # deadline = (t0 + work) + max(interval, work) = 100 + 2 + 5 = 107.
    assert dash._next_deadline(100.0, 5.0, 2.0) == 107.0


def test_next_deadline_slow_work_period_is_double_work():
    dash = _dash()
    # work >= interval → period = 2*work → CPU duty capped at 50%:
    # deadline = (t0 + work) + max(interval, work) = t0 + 2*work.
    assert dash._next_deadline(100.0, 5.0, 8.0) == 116.0   # 100 + 8 + 8
    assert dash._next_deadline(100.0, 5.0, 5.0) == 110.0   # equal boundary: 100 + 5 + 5


def test_sync_loop_sleeps_to_work_proportional_deadline():
    dash = _dash()
    clock = [0.0]
    seen_sleeps = []

    def monotonic():
        return clock[0]

    def sleep(d):
        assert d >= 0.0, "cooldown must never sleep a negative duration"
        seen_sleeps.append(d)
        clock[0] += d

    stop = threading.Event()
    starts = []
    n = [0]

    def run_iteration():
        starts.append(round(clock[0], 6))  # round off float-accumulated sleep dust
        n[0] += 1
        clock[0] += 3.0  # slow work: 3s > interval 1s
        if n[0] >= 3:
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        take_sync_request=lambda: False, monotonic=monotonic, sleep=sleep,
    )
    # Slow work (3s) > interval (1s): period = 2*work = 6, so each iteration
    # starts 6s after the previous — CPU duty capped at 50%.
    assert starts == [0.0, 6.0, 12.0]
    # A genuine cooldown sleep happened (deadline was beyond the work-end).
    assert seen_sleeps, "slow work must still cool down to cap CPU duty"


def test_sync_loop_normal_cadence_unchanged():
    dash = _dash()
    clock = [0.0]

    def monotonic():
        return clock[0]

    def sleep(d):
        assert d >= 0.0
        clock[0] += d

    stop = threading.Event()
    starts = []
    n = [0]

    def run_iteration():
        starts.append(round(clock[0], 6))
        n[0] += 1
        clock[0] += 0.2  # fast work < interval 1.0
        if n[0] >= 2:
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        take_sync_request=lambda: False, monotonic=monotonic, sleep=sleep,
    )
    assert starts[0] == 0.0
    # Fast work (0.2s) < interval (1.0s): period = work + interval = 1.2 — the
    # same cadence as the prior "rebuild then sleep interval" loop.
    assert starts[1] == 1.2


def test_sync_loop_take_sync_request_breaks_cooldown_early():
    dash = _dash()
    clock = [0.0]

    def monotonic():
        return clock[0]

    def sleep(d):
        clock[0] += max(d, 0.001)

    stop = threading.Event()
    starts = []
    n = [0]
    polls = [False, True]  # second cooldown poll requests a force-refresh

    def take_sync_request():
        return polls.pop(0) if polls else False

    def run_iteration():
        starts.append(clock[0])
        n[0] += 1
        clock[0] += 0.1  # fast work; interval is very long
        if n[0] >= 2:
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=100.0, run_iteration=run_iteration,
        take_sync_request=take_sync_request, monotonic=monotonic, sleep=sleep,
    )
    # A force-refresh request breaks the 100s cooldown almost immediately.
    assert starts[1] < 1.0


def test_sync_loop_stop_exits_promptly():
    dash = _dash()
    clock = [0.0]

    def monotonic():
        return clock[0]

    def sleep(d):
        clock[0] += max(d, 0.001)

    stop = threading.Event()
    n = [0]

    def run_iteration():
        n[0] += 1
        stop.set()  # request stop during the first iteration

    dash._dashboard_sync_loop(
        stop=stop, interval=100.0, run_iteration=run_iteration,
        take_sync_request=lambda: False, monotonic=monotonic, sleep=sleep,
    )
    assert n[0] == 1  # the stop event prevents a second iteration


# ----------------------------------------------------------------------
# #583 S2: the request-driven floor, coalescing, and the failure boundary.
# ----------------------------------------------------------------------
def test_request_driven_start_respects_the_two_times_work_floor():
    """#583 S2 — the #313 duty bound, asserted algebraically.

    Under sustained requests the pre-#583 loop would begin iteration i+1 the
    instant iteration i ended, so the period equalled ``work`` and CPU duty
    approached 100% of one core. The floor puts the earliest request-driven
    start at ``s + 2w``, which is exactly the ``period >= 2*work`` that caps
    duty at 50% of one core, scale-independently.
    """
    dash = _dash()
    clock = [0.0]

    def monotonic():
        return clock[0]

    def sleep(d):
        clock[0] += max(d, 0.001)

    stop = threading.Event()
    starts, works = [], []
    pending = [True, True, False]

    def pending_request():
        return pending[min(len(starts) - 1, len(pending) - 1)] if starts else True

    def capture_batch():
        return (len(starts), False)

    def run_iteration(batch=None):
        starts.append(clock[0])
        clock[0] += 2.0          # slow work; interval is smaller
        works.append(2.0)
        if len(starts) >= 3:
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        monotonic=monotonic, sleep=sleep,
        pending_request=pending_request, capture_batch=capture_batch,
        settle=lambda *a, **k: None,
    )
    assert len(starts) >= 2, "the loop must have serviced more than one batch"
    for i in range(len(starts) - 1):
        assert starts[i + 1] - starts[i] >= 2 * works[i] - 1e-9


def test_request_driven_start_beats_a_long_automatic_deadline():
    """The floor is a floor, not the deadline.

    With a 0.5s rebuild and a 100s interval the automatic deadline is 100.5s;
    a queued request is serviced at 1.0s instead. Without this the floor would
    be indistinguishable from leaving the cooldown alone.
    """
    dash = _dash()
    clock = [0.0]

    def sleep(d):
        clock[0] += max(d, 0.001)

    stop = threading.Event()
    starts = []

    def pending_request():
        # A request arrives during the first cooldown and stays outstanding
        # until the loop captures it.
        return len(starts) == 1

    def run_iteration(batch=None):
        starts.append(clock[0])
        clock[0] += 0.5
        if len(starts) >= 2:
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=100.0, run_iteration=run_iteration,
        monotonic=lambda: clock[0], sleep=sleep,
        pending_request=pending_request,
        capture_batch=lambda: (1, False),
        settle=lambda *a, **k: None,
    )
    assert starts[1] >= 1.0 - 1e-9      # never before s + 2w
    assert starts[1] < 2.0              # and far before the 100.5s deadline


def test_many_requests_before_the_floor_become_one_batch():
    dash = _dash()          # this file's existing module-loading helper
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    for _ in range(25):
        ref.request_sync()
    ref.request_sync(refresh=True)
    batch_id, refresh = ref.capture_batch()
    assert batch_id == 26
    assert refresh is True
    assert ref.pending_request() is False


def test_escaped_iteration_exception_settles_the_batch_as_failed():
    """An accepted request must always reach a terminal state.

    Parts of the locked rebuild run before the builder's own ``try`` (owner
    marking and tick setup), so an exception can escape the iteration. Before
    #583 that killed the only drainer silently; now a client holding an
    accepted 202 would wait forever for a settlement no surviving thread could
    publish.
    """
    dash = _dash()
    clock = [0.0]
    stop = threading.Event()
    settled = []
    calls = [0]

    def run_iteration(batch=None):
        calls[0] += 1
        clock[0] += 0.5
        if calls[0] == 1:
            raise RuntimeError("builder exploded before its own try")
        stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        monotonic=lambda: clock[0],
        sleep=lambda d: clock.__setitem__(0, clock[0] + max(d, 0.001)),
        pending_request=lambda: calls[0] == 0,
        capture_batch=lambda: (1, False),
        settle=lambda bid, status, warnings=(): settled.append((bid, status)),
    )
    assert settled and settled[0] == (1, "failed")
    assert calls[0] >= 2          # the loop survived and kept draining


def test_a_serviced_batch_settles_ok_and_carries_its_warnings():
    """A queued refresh has no HTTP response, so its warnings ride the
    settlement instead."""
    dash = _dash()
    clock = [0.0]
    stop = threading.Event()
    settled = []
    calls = [0]

    def run_iteration(batch=None):
        calls[0] += 1
        clock[0] += 0.5
        stop.set()
        return {"warnings": [{"code": "rate_limited"}]}

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        monotonic=lambda: clock[0],
        sleep=lambda d: clock.__setitem__(0, clock[0] + max(d, 0.001)),
        pending_request=lambda: calls[0] == 0,
        capture_batch=lambda: (4, True),
        settle=lambda bid, status, warnings=(): settled.append(
            (bid, status, tuple(warnings))),
    )
    assert settled == [(4, "ok", ({"code": "rate_limited"},))]


def test_an_automatic_tick_never_settles_a_batch():
    """Nothing was captured, so nothing may be reported as settled."""
    dash = _dash()
    clock = [0.0]
    stop = threading.Event()
    settled = []

    def run_iteration(batch=None):
        assert batch is None
        clock[0] += 0.2
        stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        monotonic=lambda: clock[0],
        sleep=lambda d: clock.__setitem__(0, clock[0] + max(d, 0.001)),
        pending_request=lambda: False,
        capture_batch=lambda: (1, False),
        settle=lambda *a, **k: settled.append(a),
    )
    assert settled == []


def test_the_loop_peeks_and_never_consumes_the_pending_request():
    """#583 S2 spec 5.2. A poll firing before the floor must not clear the
    request — the old test-and-clear collaborator would discard it."""
    dash = _dash()
    clock = [0.0]
    stop = threading.Event()
    peeks = [0]
    captured = []

    def pending_request():
        peeks[0] += 1
        return True

    def run_iteration(batch=None):
        captured.append(batch)
        clock[0] += 2.0          # work >= interval, so floor == deadline
        if len(captured) >= 2:
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=1.0, run_iteration=run_iteration,
        monotonic=lambda: clock[0],
        sleep=lambda d: clock.__setitem__(0, clock[0] + max(d, 0.001)),
        pending_request=pending_request,
        capture_batch=lambda: (len(captured) + 1, False),
        settle=lambda *a, **k: None,
    )
    # The cooldown polled many times without consuming; both iterations still
    # serviced a batch.
    assert peeks[0] > 2
    assert captured == [(1, False), (2, False)]
