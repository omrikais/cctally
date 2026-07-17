"""#313 P2 (F10): dashboard sync-thread work-proportional cooldown.

The automatic sync thread must sleep to a monotonic deadline
``t0 + max(interval, work)`` so its CPU duty is bounded (worst case 50% of one
core when work >= interval), while normal-cadence operation (work < interval)
is byte-for-byte unchanged.
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
