"""#901 spec §5.5 / G4: the process write sampler, timeline and descriptor.

Every reader is injected except one platform-adaptive case that exercises the
real counter of the runner, so the macOS ABI and the Linux parser are both
covered without depending on which runner the wrapper routes to.
"""
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import threading

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_write_io as wio  # noqa: E402
from tests._support_http import PRESENCE_BACKSTOP_SECONDS  # noqa: E402

S = 1_000_000_000
MiB = 1024 * 1024


class _Clock:
    def __init__(self, start_s=1000):
        self.ns = start_s * S

    def __call__(self):
        return self.ns

    def advance(self, seconds):
        self.ns += int(seconds * S)


class _Values:
    """A counter reader returning queued values, then repeating the last."""

    def __init__(self, *values):
        self.values = list(values)
        self.last = None

    def __call__(self, _pid=None):
        if self.values:
            item = self.values.pop(0)
            if isinstance(item, BaseException):
                raise item
            self.last = item
        return self.last


@pytest.fixture(autouse=True)
def _reset():
    wio.reset_for_tests()
    yield
    wio.reset_for_tests()


def test_the_darwin_counter_names_its_source_and_reads_the_reader():
    counter = wio.ProcessWriteCounter(platform="darwin",
                                      darwin_reader=_Values(4096))
    reading = counter.read()
    assert reading == wio.CounterReading(4096, "ok", wio.SOURCE_DARWIN)


def test_the_linux_counter_parses_proc_io():
    text = "rchar: 10\nwchar: 20\nread_bytes: 30\nwrite_bytes: 81920\n"

    def opener(path, mode, encoding):
        assert path == "/proc/self/io"
        return io.StringIO(text)

    counter = wio.ProcessWriteCounter(
        platform="linux",
        linux_reader=lambda pid: wio.linux_write_bytes(pid, opener=opener))
    assert counter.read() == wio.CounterReading(81920, "ok", wio.SOURCE_LINUX)


def test_an_unsupported_platform_is_null_with_a_reason_never_zero():
    reading = wio.ProcessWriteCounter(platform="win32").read()
    assert reading.value is None
    assert reading.status == "unsupported_platform"
    assert reading.source is None


def test_a_reader_error_is_a_counter_error_and_null():
    counter = wio.ProcessWriteCounter(
        platform="darwin", darwin_reader=_Values(OSError(1, "denied")))
    reading = counter.read()
    assert (reading.value, reading.status) == (None, "counter_error")


def test_a_missing_proc_io_is_an_unsupported_platform():
    counter = wio.ProcessWriteCounter(
        platform="linux", linux_reader=_Values(FileNotFoundError("io")))
    assert counter.read().status == "unsupported_platform"


def test_a_decreasing_counter_is_a_reset_and_the_next_read_recovers():
    counter = wio.ProcessWriteCounter(platform="darwin",
                                      darwin_reader=_Values(500, 100, 300))
    assert counter.read().value == 500
    reset = counter.read()
    assert (reset.value, reset.status) == (None, "counter_reset")
    assert counter.read() == wio.CounterReading(300, "ok", wio.SOURCE_DARWIN)


class _SignallingLock:
    """A lock that announces when a second caller has to wait for it."""

    def __init__(self, contended: threading.Event) -> None:
        self._lock = threading.Lock()
        self._contended = contended

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self._contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()


def test_concurrent_reads_never_reorder_into_a_false_reset():
    """#901 Amendment 19 PR-2. The kernel counter only grows, so two reads
    that observe 100 and then 110 must never report a reset. When the reader
    ran outside the lock, a thread could observe 100, lose the race to store
    it, and find the later 110 already stored: a false ``counter_reset``.

    Deterministic: thread A observes 100 and parks inside its reader; thread B
    then reads 110. The test proceeds only once B has either finished (the old
    form: its reader ran beside A's) or is waiting for the counter's lock (the
    fixed form), so no step depends on timing."""
    first_inside, release_first, progress = (
        threading.Event(), threading.Event(), threading.Event())
    values = iter((100, 110))
    calls = []

    def reader(_pid):
        value = next(values)
        calls.append(value)
        if value == 100:
            first_inside.set()
            release_first.wait(timeout=30)
        return value

    counter = wio.ProcessWriteCounter(platform="darwin", darwin_reader=reader)
    counter._lock = _SignallingLock(progress)
    results = {}

    def read(name):
        results[name] = counter.read()
        if name == "b":
            progress.set()

    first = threading.Thread(target=read, args=("a",))
    first.start()
    assert first_inside.wait(timeout=30)
    second = threading.Thread(target=read, args=("b",))
    second.start()
    assert progress.wait(timeout=30)
    release_first.set()
    first.join(timeout=20)
    second.join(timeout=20)
    assert not first.is_alive() and not second.is_alive()
    assert calls == [100, 110]
    assert results["a"] == wio.CounterReading(100, "ok", wio.SOURCE_DARWIN)
    assert results["b"] == wio.CounterReading(110, "ok", wio.SOURCE_DARWIN)


class _ThreadClock:
    """The shared clock, except for a thread that registered its own reading
    (and an optional hook run before that reading is returned)."""

    def __init__(self, start_s=1000):
        self.ns = start_s * S
        self.overrides = {}

    def __call__(self):
        hook = self.overrides.pop(threading.get_ident(), None)
        if hook is None:
            return self.ns
        value, park = hook
        if park is not None:
            park()
        return value

    def advance(self, seconds):
        self.ns += int(seconds * S)


def _begun_then_ended(interval_box):
    return wio.end_interval(interval_box["iv"])


@pytest.mark.parametrize("entry", [
    "read_counter", "begin_interval", "end_interval", "note_publication"])
def test_a_sample_across_a_bucket_boundary_never_reads_as_a_reset(
        entry, monkeypatch):
    """901-CD-001. A public sampling entry point that takes its timestamp,
    pauses, and reads the counter after another thread sampled across the
    five-second bucket boundary must not file the larger reading under the
    earlier time: sorted by time the timeline would then decrease, and the
    budget statistic would report a false ``counter_reset`` for a counter
    that only grew, hiding the verdict.

    Deterministic: thread A's clock parks before returning 4.999 s into the
    last bucket; thread B then samples at 5.001 s. The test proceeds once B
    has either finished (the old form: A's clock ran outside the timeline
    lock) or waits for that lock (the fixed form)."""
    clock = _ThreadClock()
    value = {"n": 0}

    def reader(_pid):
        value["n"] += 1000
        return value["n"]

    counter = wio.ProcessWriteCounter(platform="darwin", darwin_reader=reader)
    wio.reset_for_tests(counter=counter, clock_ns=clock,
                        wall_s=lambda: 1_700_000_000)
    wio.note_publication(cold=False)
    for _ in range(70):
        clock.advance(5)
        wio.note_publication(cold=False)
    base = clock.ns
    pending = {"iv": wio.begin_interval("tick")} if entry == "end_interval" \
        else None
    action = {
        "read_counter": wio.read_counter,
        "begin_interval": lambda: wio.begin_interval("maintenance"),
        "end_interval": lambda: _begun_then_ended(pending),
        "note_publication": lambda: wio.note_publication(cold=False),
    }[entry]

    a_inside, release_a, progress = (
        threading.Event(), threading.Event(), threading.Event())
    monkeypatch.setattr(wio, "_LOCK", _SignallingLock(progress))

    def park():
        a_inside.set()
        release_a.wait(timeout=30)

    def run_a():
        clock.overrides[threading.get_ident()] = (base + 4_999_000_000, park)
        action()

    def run_b():
        clock.overrides[threading.get_ident()] = (base + 5_001_000_000, None)
        wio.read_counter()
        progress.set()

    first = threading.Thread(target=run_a)
    first.start()
    assert a_inside.wait(timeout=30)
    second = threading.Thread(target=run_b)
    second.start()
    assert progress.wait(timeout=30)
    release_a.set()
    first.join(timeout=20)
    second.join(timeout=20)
    assert not first.is_alive() and not second.is_alive()

    clock.ns = base + 6 * S
    wio.read_counter()
    stat, _ = wio.evaluate()
    assert (stat.status, stat.reasons) == ("qualified", ()), (
        f"a growing counter lost its budget verdict: {stat}")
    t_ns, b = wio.state_for_budget()[0], wio.state_for_budget()[1]
    pairs = sorted(zip(t_ns, b))
    assert all(y[1] >= x[1] for x, y in zip(pairs, pairs[1:])), (
        f"the timeline decreases by time: {pairs[-4:]}")


def test_the_real_counter_of_this_runner(tmp_path):
    counter = wio.ProcessWriteCounter()
    before = counter.read()
    if counter.source is None:
        assert (before.value, before.status) == (None, "unsupported_platform")
        return
    assert before.status == "ok" and before.value >= 0
    with open(tmp_path / "payload.bin", "wb") as fh:
        fh.write(os.urandom(4 * MiB))
        fh.flush()
        os.fsync(fh.fileno())
    after = counter.read()
    assert after.status == "ok"
    assert after.value - before.value >= MiB, (
        "the kernel counter must see a 4 MiB fsynced write")


def _install(*values, platform="darwin"):
    clock = _Clock()
    counter = wio.ProcessWriteCounter(platform=platform,
                                      darwin_reader=_Values(*values))
    wio.reset_for_tests(counter=counter, clock_ns=clock, wall_s=lambda: 1_700_000_000)
    return clock


def test_an_interval_reports_its_delta_and_no_overlap_alone():
    clock = _install(100, 400)
    interval = wio.begin_interval("tick")
    clock.advance(1)
    obs = wio.end_interval(interval)
    assert (obs.process_write_bytes, obs.status, obs.overlap) == (300, "ok", False)


def test_overlap_is_marked_only_across_threads():
    _install(0, 10, 20, 30, 40, 50)
    outer = wio.begin_interval("conversation")
    nested = wio.begin_interval("maintenance")      # same thread: nested
    assert wio.end_interval(nested).overlap is False
    other = {}

    def worker():
        other["iv"] = wio.begin_interval("tick")
        other["obs"] = wio.end_interval(other["iv"])

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert other["obs"].overlap is True
    assert wio.end_interval(outer).overlap is True


def test_an_unavailable_counter_never_yields_a_number():
    counter = wio.ProcessWriteCounter(platform="win32")
    wio.reset_for_tests(counter=counter)
    obs = wio.end_interval(wio.begin_interval("tick"))
    assert obs.process_write_bytes is None
    assert obs.status == "unsupported_platform"


def test_the_timeline_keeps_one_sample_per_bucket_and_is_bounded():
    values = list(range(0, 1000 * MiB, MiB))
    clock = _install(*values)
    for _ in range(400):
        wio.read_counter()
        wio.note_publication(cold=False)
        clock.advance(2)
    t_ns = wio.state_for_budget()[0]
    assert len(t_ns) == wio.TIMELINE_CAPACITY
    buckets = [t // wio.TIMELINE_BUCKET_NS for t in t_ns]
    assert buckets == sorted(set(buckets)), "one sample per 5 s bucket"


def test_warm_admission_waits_for_a_warm_publication():
    clock = _install(*range(0, 100 * MiB, MiB))
    wio.note_publication(cold=True)
    clock.advance(5)
    stat, _ = wio.evaluate()
    assert stat.reasons == ("warming_up",)
    wio.note_publication(cold=False)
    for _ in range(70):
        clock.advance(5)
        wio.note_publication(cold=False)
    stat, _ = wio.evaluate()
    assert stat.status == "qualified"
    assert stat.publications == 60


def test_a_marked_deletion_is_excluded_from_the_statistic():
    clock = _install(*range(0, 400 * MiB, MiB))
    wio.note_publication(cold=False)
    for step in range(70):
        clock.advance(5)
        if step == 40:
            iv = wio.begin_interval("maintenance")
            clock.advance(1)
            obs = wio.end_interval(iv)
            wio.mark_deletion(obs, rows=250)
        wio.note_publication(cold=False)
    stat, _ = wio.evaluate()
    assert stat.excluded_operations == 1
    assert stat.excluded_rows == 250
    assert stat.excluded_bytes == obs.process_write_bytes


def test_the_exclusion_ring_is_bounded():
    _install(*range(0, 100 * MiB, MiB))
    for _ in range(wio.EXCLUSION_CAPACITY + 5):
        obs = wio.end_interval(wio.begin_interval("maintenance"))
        wio.mark_deletion(obs, rows=1)
    assert len(wio.state_for_budget()[4]) == wio.EXCLUSION_CAPACITY


def test_the_process_start_identity_is_stable_and_absent_for_a_dead_pid():
    identity = wio.process_start_identity(os.getpid())
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert identity is not None
        assert identity == wio.process_start_identity(os.getpid())
    else:
        assert identity is None
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert wio.process_start_identity(child.pid) is None


def test_the_linux_start_identity_reads_field_twenty_two():
    stat_text = "123 (my (odd) name) S " + " ".join(
        str(n) for n in range(4, 30))
    identity = wio.process_start_identity(
        123, platform="linux", proc_stat_reader=lambda pid: stat_text)
    assert identity == "linux:22"


def test_the_descriptor_round_trip_is_owner_readable(tmp_path):
    path = wio.write_instance_descriptor(
        tmp_path, instance_id="0123456789abcdef", host="127.0.0.1",
        port=8789)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    [descriptor] = wio.read_instance_descriptors(tmp_path)
    assert descriptor["instanceId"] == "0123456789abcdef"
    assert descriptor["pid"] == os.getpid()
    assert descriptor["port"] == 8789
    assert descriptor["processStart"] == wio.process_start_identity(os.getpid())
    assert wio.descriptor_is_live(descriptor)
    wio.remove_instance_descriptor(tmp_path, "0123456789abcdef")
    assert wio.read_instance_descriptors(tmp_path) == []


def test_a_dead_or_reused_pid_makes_a_descriptor_stale(tmp_path):
    wio.write_instance_descriptor(tmp_path, instance_id="aaaaaaaaaaaaaaaa",
                                  host="127.0.0.1", port=1)
    [descriptor] = wio.read_instance_descriptors(tmp_path)
    assert not wio.descriptor_is_live(descriptor, alive=lambda pid: False)
    recorded = {**descriptor, "processStart": "darwin:1"}
    assert not wio.descriptor_is_live(
        recorded, identity=lambda pid: "darwin:0")


def test_an_unproven_process_start_identity_is_not_live(tmp_path):
    """#901 Amendment 19 PR-10. A descriptor is live only when the process
    start identity it recorded is readable now and equal: a reused pid (alive,
    another process), an identity unreadable now, and a descriptor that
    recorded none are all stale. The instance that wrote the descriptor is
    still found, and the descriptor files keep their owner-only modes."""
    alive = lambda pid: True  # noqa: E731
    wio.write_instance_descriptor(tmp_path, instance_id="bbbbbbbbbbbbbbbb",
                                  host="127.0.0.1", port=8790)
    [descriptor] = wio.read_instance_descriptors(tmp_path)
    assert isinstance(descriptor["processStart"], str), (
        "non-vacuity: this runner records a start identity")
    assert wio.descriptor_is_live(descriptor)
    assert not wio.descriptor_is_live(
        descriptor, alive=alive, identity=lambda pid: "darwin:999999999")
    assert not wio.descriptor_is_live(
        descriptor, alive=alive, identity=lambda pid: None)
    assert not wio.descriptor_is_live(
        {**descriptor, "processStart": None}, alive=alive,
        identity=lambda pid: descriptor["processStart"])
    # On disk: a reused-pid descriptor (this live pid, a forged start) and one
    # that recorded no identity are removed; the valid instance stays.
    directory = wio.descriptor_dir(tmp_path)
    for instance_id, start in (("cccccccccccccccc", "linux:1"),
                               ("dddddddddddddddd", None)):
        path = wio.write_instance_descriptor(
            tmp_path, instance_id=instance_id, host="127.0.0.1", port=8791)
        payload = json.loads(path.read_text())
        payload["processStart"] = start
        path.write_text(json.dumps(payload))
    assert len(wio.read_instance_descriptors(tmp_path)) == 3
    assert wio.remove_stale_descriptors(tmp_path) == 2
    assert [d["instanceId"] for d in wio.read_instance_descriptors(tmp_path)] \
        == ["bbbbbbbbbbbbbbbb"]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "dashboard-bbbbbbbbbbbbbbbb.json")
                        .stat().st_mode) == 0o600


def test_malformed_descriptors_are_ignored(tmp_path):
    directory = wio.descriptor_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "dashboard-bad.json").write_text("{not json")
    (directory / "dashboard-wrong.json").write_text('{"schemaVersion": 9}')
    assert wio.read_instance_descriptors(tmp_path) == []


def test_only_stale_descriptors_are_removed(tmp_path):
    wio.write_instance_descriptor(tmp_path, instance_id="1111111111111111",
                                  host="127.0.0.1", port=1)
    wio.write_instance_descriptor(tmp_path, instance_id="2222222222222222",
                                  host="127.0.0.1", port=2, pid=2 ** 22 + 7)
    assert wio.remove_stale_descriptors(tmp_path) == 1
    assert [d["instanceId"] for d in wio.read_instance_descriptors(tmp_path)] \
        == ["1111111111111111"]


@pytest.mark.parametrize("bind, target", [
    ("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"),
    ("localhost", "127.0.0.1"), ("::", "::1"), ("::1", "::1"),
    ("192.168.1.5", None), ("example.com", None)])
def test_the_loopback_target_of_a_bind(bind, target):
    assert wio.loopback_target(bind) == target


def test_the_local_instance_is_registered_and_cleared():
    assert wio.local_instance() is None
    wio.register_local_instance(instance_id="abcdabcdabcdabcd",
                                host="127.0.0.1", port=8789)
    assert wio.local_instance() == {
        "instanceId": "abcdabcdabcdabcd", "host": "127.0.0.1",
        "port": 8789, "pid": os.getpid()}
    wio.clear_local_instance()
    assert wio.local_instance() is None


def test_the_maintenance_record_is_held_as_a_compact_ledger():
    wio.set_maintenance_record({"ledger": [
        {"hour": "2026-10-03T09:00:00Z", "charged": 300, "largest": 200}]})
    assert wio.maintenance_ledger() == [
        {"hour": "2026-10-03T09:00:00Z", "charged": 300, "largest": 200}]
    wio.set_maintenance_record(None)
    assert wio.maintenance_ledger() == []


def test_a_fork_while_another_thread_samples_leaves_the_child_able_to_sample():
    """PR-6 / OV-7. `_LOCK` is held across the counter syscall, and neither
    it nor the counter's own lock was reset at fork: a child forked (without
    exec) while another thread sampled inherited both locks held, so its
    first sample or phase boundary blocked forever. The fork now waits for
    the sampler and the child starts with released locks and its own
    timeline."""
    wio.reset_for_tests()
    holding = threading.Event()
    done = threading.Event()

    def sampler():
        with wio._LOCK:
            with wio._STATE.counter._lock:
                holding.set()
                done.wait(0.3)

    thread = threading.Thread(target=sampler)
    thread.start()
    assert holding.wait(PRESENCE_BACKSTOP_SECONDS)
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            if not wio._LOCK.acquire(timeout=PRESENCE_BACKSTOP_SECONDS):
                code = 7
            elif not wio._STATE.counter._lock.acquire(
                    timeout=PRESENCE_BACKSTOP_SECONDS):
                code = 8
            elif len(wio._STATE.t):
                code = 9
        except BaseException:
            code = 10
        os._exit(code)
    done.set()
    thread.join()
    _pid, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    assert code == 0, {
        7: "the child inherited a held write-io lock",
        8: "the child inherited a held counter lock",
        9: "the child inherited the parent's timeline",
    }.get(code, code)
    wio.reset_for_tests()
