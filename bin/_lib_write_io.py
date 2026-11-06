"""Process disk-write telemetry for the dashboard (#901 spec §5.5).

Stdlib-only leaf. It reads ONE kernel-accounted process write counter —
macOS `proc_pid_rusage(RUSAGE_INFO_V2).ri_diskio_byteswritten` through a
correctly declared ctypes ABI, Linux `/proc/self/io` `write_bytes` — and never
substitutes zero, file growth or `ru_oublock` for a counter it could not read:
an unreadable value is `None` with a reason from `UNAVAILABLE_REASONS`. These
are process writes, not NAND writes.

One cumulative timeline is sampled at tick, conversation-pass and maintenance
boundaries. An interval's delta includes whatever ran concurrently; it carries
overlap metadata and is never summed across loops. Sustained rates come only
from the timeline, through `_lib_write_budget`.

It also owns the dashboard instance descriptor: an owner-readable file written
once at startup and removed at clean shutdown, through which `cctally doctor`
discovers a running instance. It is endpoint discovery, never a measurement.
"""
from __future__ import annotations

import array
import dataclasses
import datetime as dt
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time

import _lib_write_budget as _budget

UTC = dt.timezone.utc
UNAVAILABLE_REASONS = ("unsupported_platform", "counter_error", "counter_reset")
WRITE_STATUSES = ("ok",) + UNAVAILABLE_REASONS + ("not_sampled",)
SOURCE_DARWIN = "proc_pid_rusage.ri_diskio_byteswritten"
SOURCE_LINUX = "proc.io.write_bytes"
SCOPE = "process"
TIMELINE_CAPACITY = 96
TIMELINE_BUCKET_NS = 5 * 1_000_000_000
EXCLUSION_CAPACITY = 16
#: The hysteresis advances at most this often; far below its 60 s spacing.
HYSTERESIS_EVALUATION_SPACING_NS = 10 * 1_000_000_000
DESCRIPTOR_SCHEMA_VERSION = 1
DESCRIPTOR_DIRNAME = "run"
DESCRIPTOR_PREFIX = "dashboard-"
_INSTANCE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_RUSAGE_INFO_V2 = 2
#: `struct rusage_info_v2` (<sys/resource.h>): a 16-byte uuid then eighteen
#: uint64 fields, in this order.
_RUSAGE_V2_FIELDS = (
    "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups",
    "ri_interrupt_wkups", "ri_pageins", "ri_wired_size", "ri_resident_size",
    "ri_phys_footprint", "ri_proc_start_abstime", "ri_proc_exit_abstime",
    "ri_child_user_time", "ri_child_system_time", "ri_child_pkg_idle_wkups",
    "ri_child_interrupt_wkups", "ri_child_pageins",
    "ri_child_elapsed_abstime", "ri_diskio_bytesread",
    "ri_diskio_byteswritten",
)
_RUSAGE_TYPE = None
_PROC_PID_RUSAGE = None


def _rusage_type():
    """ctypes is imported lazily: this leaf is reachable from hook paths."""
    global _RUSAGE_TYPE
    if _RUSAGE_TYPE is None:
        import ctypes

        class RusageInfoV2(ctypes.Structure):
            _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
                (name, ctypes.c_uint64) for name in _RUSAGE_V2_FIELDS]

        _RUSAGE_TYPE = RusageInfoV2
    return _RUSAGE_TYPE


def _proc_pid_rusage():
    global _PROC_PID_RUSAGE
    if _PROC_PID_RUSAGE is None:
        import ctypes

        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        fn = lib.proc_pid_rusage
        fn.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
        fn.restype = ctypes.c_int
        _PROC_PID_RUSAGE = fn
    return _PROC_PID_RUSAGE


def darwin_rusage(pid):
    import ctypes

    info = _rusage_type()()
    rc = _proc_pid_rusage()(int(pid), _RUSAGE_INFO_V2, ctypes.byref(info))
    if rc != 0:
        raise OSError(ctypes.get_errno(), f"proc_pid_rusage({pid}) failed")
    return info


def darwin_write_bytes(pid=None) -> int:
    target = os.getpid() if pid is None else int(pid)
    return int(darwin_rusage(target).ri_diskio_byteswritten)


def linux_write_bytes(pid=None, *, opener=open) -> int:
    path = "/proc/self/io" if pid is None else f"/proc/{int(pid)}/io"
    with opener(path, "r", encoding="ascii") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            if key.strip() == "write_bytes":
                return int(value.strip())
    raise OSError(f"write_bytes missing from {path}")


@dataclasses.dataclass(frozen=True)
class CounterReading:
    value: "int | None"
    status: str
    source: "str | None"


class ProcessWriteCounter:
    """One process's cumulative write counter, with reset detection."""

    def __init__(self, *, platform=None, pid=None, darwin_reader=None,
                 linux_reader=None):
        self._platform = sys.platform if platform is None else platform
        self._pid = pid
        self._darwin = darwin_reader or darwin_write_bytes
        self._linux = linux_reader or linux_write_bytes
        self._last = None
        self._lock = threading.Lock()

    @property
    def source(self) -> "str | None":
        if self._platform == "darwin":
            return SOURCE_DARWIN
        if self._platform.startswith("linux"):
            return SOURCE_LINUX
        return None

    def read(self) -> CounterReading:
        source = self.source
        if source is None:
            return CounterReading(None, "unsupported_platform", None)
        reader = self._darwin if source == SOURCE_DARWIN else self._linux
        # #901 Amendment 19 PR-2: the observation and its comparison with the
        # previous one are ONE step. With the reader outside the lock, a thread
        # could observe 100, lose the race to store it to a thread that then
        # observed and stored 110, and report a false `counter_reset` for a
        # counter that only grew.
        with self._lock:
            try:
                value = int(reader(self._pid))
            except FileNotFoundError:
                return CounterReading(None, "unsupported_platform", source)
            except (OSError, ValueError, TypeError, AttributeError):
                return CounterReading(None, "counter_error", source)
            if value < 0:
                return CounterReading(None, "counter_error", source)
            last, self._last = self._last, value
        if last is not None and value < last:
            return CounterReading(None, "counter_reset", source)
        return CounterReading(value, "ok", source)


def _read_proc_stat(pid) -> str:
    with open(f"/proc/{int(pid)}/stat", "r", encoding="ascii",
              errors="replace") as fh:
        return fh.read()


def process_start_identity(pid, *, platform=None, darwin_rusage_fn=None,
                           proc_stat_reader=None) -> "str | None":
    """A value that changes when `pid` is reused; None when unknowable."""
    platform = sys.platform if platform is None else platform
    try:
        if platform == "darwin":
            info = (darwin_rusage_fn or darwin_rusage)(pid)
            return f"darwin:{int(info.ri_proc_start_abstime)}"
        if platform.startswith("linux"):
            text = (proc_stat_reader or _read_proc_stat)(pid)
            fields = text.rsplit(")", 1)[1].split()
            # Field 22 (starttime) is index 19 after the `(comm)` field.
            return f"linux:{int(fields[19])}"
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


@dataclasses.dataclass(frozen=True)
class Interval:
    token: int
    kind: str
    thread: int
    started_ns: int
    start_bytes: "int | None"
    start_status: str


@dataclasses.dataclass(frozen=True)
class IntervalObservation:
    kind: str
    started_ns: int
    ended_ns: int
    start_bytes: "int | None"
    end_bytes: "int | None"
    process_write_bytes: "int | None"
    status: str
    overlap: bool


class _State:
    def __init__(self, counter=None, clock_ns=None, wall_s=None):
        self.counter = counter or ProcessWriteCounter()
        self.clock_ns = clock_ns or time.monotonic_ns
        self.wall_s = wall_s or time.time
        self.t = array.array("q")
        self.b = array.array("q")
        self.p = array.array("q")
        self.ex = tuple(array.array("q") for _ in range(6))
        self.ledger = array.array("q")
        self.ledger_observed = False
        self.publications = 0
        self.warm_admitted_ns = None
        self.cold_startup_bytes = None
        self.last_status = "not_sampled"
        self.open = {}
        self.next_token = 1
        self.hysteresis = _budget.HysteresisState()
        self.last_hysteresis_ns = None
        self.local_instance = None
        # #901 Q9: the latest reclaim attempt the planner refused, in memory
        # only (a refusal writes nothing durable); None after a chunk runs.
        self.reclaim_refusal = None
        # #939 D5: the startup admission hold. ``drain`` is the evaluator,
        # kept only while the hold is placed (its obligations scale with the
        # backlog, so it is dropped at release and never part of the fixed
        # state budget); the rest is the fixed-size published state.
        self.hold_placed = False
        self.released_by = None
        self.first_non_cold_ns = None
        self.accounting_caught_up = False
        self.residual_counts = None
        self.last_pass = None
        self.drain = None


_LOCK = threading.Lock()
_STATE = _State()


def _counter_lock(state) -> "threading.Lock | None":
    return getattr(state.counter, "_lock", None)


def _before_fork() -> None:
    """PR-6: a fork waits for any sample in flight (`_LOCK`, then the
    counter's lock, the order `_sample_locked` takes them), so neither is
    inherited held by a thread the child does not have."""
    _LOCK.acquire()
    lock = _counter_lock(_STATE)
    if lock is not None:
        lock.acquire()


def _after_fork_in_parent() -> None:
    lock = _counter_lock(_STATE)
    if lock is not None:
        lock.release()
    _LOCK.release()


def _after_fork_in_child() -> None:
    """The child measures its own process: released locks, a fresh state."""
    global _LOCK, _STATE
    old = _STATE
    counter = old.counter
    if type(counter) is ProcessWriteCounter:
        counter = None
    else:
        lock = _counter_lock(old)
        if lock is not None:
            lock.release()
    _LOCK = threading.Lock()
    _STATE = _State(counter, old.clock_ns, old.wall_s)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_before_fork,
                        after_in_parent=_after_fork_in_parent,
                        after_in_child=_after_fork_in_child)


def reset_for_tests(*, counter=None, clock_ns=None, wall_s=None) -> None:
    global _STATE
    with _LOCK:
        _STATE = _State(counter, clock_ns, wall_s)


def _append_sample_locked(now_ns: int, reading: CounterReading) -> None:
    state = _STATE
    state.last_status = reading.status
    value = -1 if reading.value is None else int(reading.value)
    bucket = now_ns // TIMELINE_BUCKET_NS
    if len(state.t) and state.t[-1] // TIMELINE_BUCKET_NS == bucket:
        state.t[-1] = now_ns
        state.b[-1] = value
        state.p[-1] = state.publications
        return
    state.t.append(now_ns)
    state.b.append(value)
    state.p.append(state.publications)
    while len(state.t) > TIMELINE_CAPACITY:
        del state.t[0]
        del state.b[0]
        del state.p[0]


def _sample_locked(state) -> "tuple[int, CounterReading]":
    """Take the timestamp, read the counter and append the sample as ONE
    step under `_LOCK` (901-CD-001). With the clock and the reader outside
    the lock, a thread could stamp 4.999 s, pause while another sampled at
    5.001 s, then file its larger reading under the earlier bucket: sorted by
    time the timeline decreased, and the statistic reported a false
    `counter_reset`. The counter's own lock never takes `_LOCK`."""
    now = state.clock_ns()
    reading = state.counter.read()
    _append_sample_locked(now, reading)
    return now, reading


def read_counter() -> CounterReading:
    """Read and record one sample (a loop wakeup or a phase boundary)."""
    state = _STATE
    with _LOCK:
        _, reading = _sample_locked(state)
    return reading


def begin_interval(kind: str) -> Interval:
    state = _STATE
    thread = threading.get_ident()
    with _LOCK:
        now, reading = _sample_locked(state)
        token = state.next_token
        state.next_token += 1
        overlapped = False
        for entry in state.open.values():
            if entry[0] != thread:
                entry[1] = True
                overlapped = True
        state.open[token] = [thread, overlapped]
    return Interval(token, str(kind), thread, now, reading.value,
                    reading.status)


def end_interval(interval: Interval) -> IntervalObservation:
    state = _STATE
    with _LOCK:
        now, reading = _sample_locked(state)
        entry = state.open.pop(interval.token, None)
    overlap = bool(entry[1]) if entry is not None else False
    if (reading.status == "ok" and interval.start_status == "ok"
            and interval.start_bytes is not None
            and reading.value >= interval.start_bytes):
        delta, status = reading.value - interval.start_bytes, "ok"
    elif reading.status != "ok":
        delta, status = None, reading.status
    elif interval.start_status != "ok":
        delta, status = None, interval.start_status
    else:
        delta, status = None, "counter_reset"
    return IntervalObservation(
        kind=interval.kind, started_ns=interval.started_ns, ended_ns=now,
        start_bytes=interval.start_bytes, end_bytes=reading.value,
        process_write_bytes=delta, status=status, overlap=overlap)


def mark_deletion(observation: IntervalObservation, *, rows: int) -> None:
    """Record one deletion operation's BEGIN-to-COMMIT interval."""
    state = _STATE
    wall = int(state.wall_s())
    with _LOCK:
        columns = state.ex
        for column, value in zip(columns, (
                observation.started_ns, observation.ended_ns,
                -1 if observation.start_bytes is None else observation.start_bytes,
                -1 if observation.end_bytes is None else observation.end_bytes,
                max(0, int(rows)), wall)):
            column.append(int(value))
        while len(columns[0]) > EXCLUSION_CAPACITY:
            for column in columns:
                del column[0]


def _samples_locked():
    state = _STATE
    return tuple(
        _budget.CounterSample(t, None if b < 0 else b, p)
        for t, b, p in zip(state.t, state.b, state.p))


def _deletions_locked():
    s, e, sb, eb, rows, wall = _STATE.ex
    return tuple(
        _budget.DeletionInterval(a, b, None if c < 0 else c,
                                 None if d < 0 else d, r, w)
        for a, b, c, d, r, w in zip(s, e, sb, eb, rows, wall))


def _steady_locked(now_ns):
    state = _STATE
    return _budget.steady_statistic(
        _samples_locked(), _deletions_locked(), now_ns=now_ns,
        warm_admitted_ns=state.warm_admitted_ns,
        counter_status=state.last_status)


def note_publication(*, cold: bool) -> None:
    """One completed main publication: count it, sample, admit, evaluate.

    #939 D5: while the startup admission hold is placed, a non-cold
    publication is recorded but does not admit warm; `release_admission`
    admits at release when one was already seen."""
    state = _STATE
    with _LOCK:
        state.publications += 1
        now, reading = _sample_locked(state)
        if state.warm_admitted_ns is None and not cold:
            if state.first_non_cold_ns is None:
                state.first_non_cold_ns = now
            if not state.hold_placed or state.released_by is not None:
                state.warm_admitted_ns = now
                state.cold_startup_bytes = reading.value
        if (state.last_hysteresis_ns is None
                or now - state.last_hysteresis_ns
                >= HYSTERESIS_EVALUATION_SPACING_NS):
            state.hysteresis = _budget.advance(
                state.hysteresis, _steady_locked(now), _budget.LIMITS, now)
            state.last_hysteresis_ns = now


def evaluate(now_ns=None, limits=None):
    """`(SteadyStatistic, HysteresisState)` now; never moves the hysteresis."""
    state = _STATE
    now = state.clock_ns() if now_ns is None else int(now_ns)
    with _LOCK:
        return _steady_locked(now), state.hysteresis


def set_maintenance_record(record) -> None:
    """Hold the latest durable charge ledger, compactly (hour, charged,
    largest) — the source of `writeIo.maintenance` in this process."""
    buckets = _budget.parse_ledger(
        record.get("ledger") if isinstance(record, dict) else None)
    flat = array.array("q")
    for bucket in buckets:
        flat.extend((int(bucket.hour.timestamp()), bucket.charged,
                     bucket.largest))
    with _LOCK:
        _STATE.ledger = flat
        _STATE.ledger_observed = True


def maintenance_ledger() -> "list[dict]":
    with _LOCK:
        flat = list(_STATE.ledger)
    out = []
    for i in range(0, len(flat), 3):
        hour = dt.datetime.fromtimestamp(flat[i], UTC)
        out.append({"hour": _budget.iso_z(hour), "charged": flat[i + 1],
                     "largest": flat[i + 2]})
    return out


def register_local_instance(*, instance_id, host, port, pid=None) -> None:
    with _LOCK:
        _STATE.local_instance = {
            "instanceId": str(instance_id), "host": str(host),
            "port": int(port), "pid": os.getpid() if pid is None else int(pid)}


def clear_local_instance() -> None:
    with _LOCK:
        _STATE.local_instance = None


def local_instance() -> "dict | None":
    with _LOCK:
        local = _STATE.local_instance
    return None if local is None else dict(local)


def state_for_budget() -> tuple:
    """Every object this module retains, for the tick-stats state budget."""
    state = _STATE
    return (state.t, state.b, state.p, state.ledger, state.ex[0],
            state.ex[1:], state.open, state.hysteresis, state.local_instance,
            state.cold_startup_bytes, state.last_status, state.reclaim_refusal,
            state.released_by, state.residual_counts, state.last_pass)


#: The reason strings a reclaim refusal may publish; anything else is
#: published as "unknown" so no free text reaches `writeIo`.
RECLAIM_REFUSAL_REASON_MAX_CHARS = 48


def set_reclaim_refusal(reason, at_utc) -> None:
    """Publish the latest planner refusal of this process's reclaim (spec
    §5.4 "Refusal": the in-memory pass record, never a durable write)."""
    text = reason if (isinstance(reason, str) and reason.isascii()
                      and 0 < len(reason) <= RECLAIM_REFUSAL_REASON_MAX_CHARS
                      and all(c.islower() or c == "_" for c in reason)
                      ) else "unknown"
    with _LOCK:
        _STATE.reclaim_refusal = (text, _budget.iso_z(at_utc))


def clear_reclaim_refusal() -> None:
    with _LOCK:
        _STATE.reclaim_refusal = None


def reclaim_refusal() -> "dict | None":
    with _LOCK:
        refusal = _STATE.reclaim_refusal
    return None if refusal is None else {"reason": refusal[0],
                                         "at": refusal[1]}


# ── instance descriptor (spec §5.6) ───────────────────────────────────────

def new_instance_id() -> str:
    return secrets.token_hex(8)


def descriptor_dir(app_dir) -> pathlib.Path:
    return pathlib.Path(app_dir) / DESCRIPTOR_DIRNAME


def write_instance_descriptor(app_dir, *, instance_id, host, port, pid=None,
                              now_utc=None) -> pathlib.Path:
    """Write once at startup: owner-readable directory 0700, file 0600."""
    pid = os.getpid() if pid is None else int(pid)
    directory = descriptor_dir(app_dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    payload = {
        "schemaVersion": DESCRIPTOR_SCHEMA_VERSION,
        "instanceId": str(instance_id),
        "pid": pid,
        "processStart": process_start_identity(pid),
        "host": str(host),
        "port": int(port),
        "startedAt": _budget.iso_z(now_utc or dt.datetime.now(UTC)),
    }
    target = directory / f"{DESCRIPTOR_PREFIX}{instance_id}.json"
    tmp = directory / f".{target.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, target)
    return target


def remove_instance_descriptor(app_dir, instance_id) -> None:
    try:
        (descriptor_dir(app_dir)
         / f"{DESCRIPTOR_PREFIX}{instance_id}.json").unlink()
    except FileNotFoundError:
        pass


def _valid_descriptor(data) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("schemaVersion") != DESCRIPTOR_SCHEMA_VERSION:
        return False
    instance_id = data.get("instanceId")
    pid = data.get("pid")
    port = data.get("port")
    start = data.get("processStart")
    return (isinstance(instance_id, str) and bool(_INSTANCE_ID_RE.match(instance_id))
            and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and isinstance(port, int) and not isinstance(port, bool)
            and 1 <= port <= 65535
            and isinstance(data.get("host"), str)
            and (start is None or isinstance(start, str)))


def read_instance_descriptors(app_dir) -> "list[dict]":
    try:
        paths = sorted(descriptor_dir(app_dir).glob(f"{DESCRIPTOR_PREFIX}*.json"))
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _valid_descriptor(data):
            out.append(data)
    return out


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def descriptor_is_live(descriptor, *, alive=None, identity=None) -> bool:
    """The descriptor's pid is alive AND is still the process that wrote it.

    #901 Amendment 19 PR-10: "still the process" must be PROVEN. A descriptor
    that recorded no start identity, or whose pid's identity cannot be read
    now, cannot tell its writer from a later process that reused the pid, so
    it is not live; only a readable identity equal to the recorded one is."""
    pid = descriptor["pid"]
    if not (alive or _pid_alive)(pid):
        return False
    recorded = descriptor.get("processStart")
    if not isinstance(recorded, str) or not recorded:
        return False
    current = (identity or process_start_identity)(pid)
    return current is not None and current == recorded


def remove_stale_descriptors(app_dir) -> int:
    """Dashboard startup only: remove descriptors of dead or reused pids."""
    removed = 0
    for descriptor in read_instance_descriptors(app_dir):
        if not descriptor_is_live(descriptor):
            remove_instance_descriptor(app_dir, descriptor["instanceId"])
            removed += 1
    return removed


def loopback_target(bind_host) -> "str | None":
    """The IP-literal loopback address that reaches a dashboard bound to
    `bind_host`, or None when no loopback address does."""
    host = str(bind_host or "").strip()
    if host in ("", "0.0.0.0", "127.0.0.1", "localhost", "loopback"):
        return "127.0.0.1"
    if host in ("::", "::1"):
        return "::1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    return host if address.is_loopback else None


# ── #939 D5: the startup admission hold and its evaluator ──────────────────
#
# `cmd_dashboard` places the hold before the main sync thread starts (not
# under --no-sync). While it is placed, `note_publication` does not admit warm
# samples, so the trailing statistic stays `warming_up` and doctor and
# dashboard-perf keep printing "no samples yet (needs 5 minutes)". There is no
# deadline: a time bound would judge a long catch-up as steady state. Only
# `StartupDrain`'s evidence ("drained", "settled") or the end of the
# conversation loop ("loopExit") releases it.

RELEASE_REASONS = ("drained", "settled", "loopExit")
RESIDUAL_COUNT_KEYS = ("failed", "unpublishedDeferred", "obligations",
                       "containerPending", "keysDeferred")
#: The Codex conversation counts that are part of a pass's residual.
_RESIDUAL_COUNT_FIELDS = ("container_pending", "container_reads_held",
                          "barrier_keys_deferred", "barrier_keys_terminal",
                          "files_deferred_torn", "prune_refused")


def conversation_residual(stats) -> tuple:
    """One provider pass's residual as canonical ``(name, value)`` pairs.

    The failed paths, the deferrals no unit of the same pass published (with
    the keys each named), the causes of ``barrier_retry``, the terminal
    deferrals (each terminal file with its keys, #1052), the existing
    leftover counts, and the pass's deferred reason and maintenance failure
    (a refusal that repeats identically settles; one that changes restarts the
    chain). ``()`` when the pass left nothing unfinished."""
    if stats is None:
        return ()
    reason = getattr(stats, "deferred_reason", None)
    failed = tuple(sorted(str(path) for path in
                          (getattr(stats, "failed_paths", None) or ())))
    deferred = getattr(stats, "barrier_deferred_keys", None) or {}
    published = {str(path) for path in
                 (getattr(stats, "barrier_published_paths", None) or ())}
    unpublished = tuple(sorted(
        (str(path), tuple(sorted(keys))) for path, keys in deferred.items()
        if str(path) not in published))
    causes = tuple(sorted(getattr(stats, "barrier_retry_causes", None) or ()))
    # #1052: each terminal file with the keys it holds back. No certificate
    # revision: a re-seal that leaves the same file terminal for the same keys
    # is the same residual, and a changed key is a different one.
    terminal = tuple(sorted(
        (str(path), tuple(sorted(keys))) for path, keys in
        (getattr(stats, "barrier_terminal_keys", None) or {}).items()))
    items = [("failed_paths", failed), ("unpublished_deferred", unpublished),
             ("barrier_retry_causes", causes), ("barrier_terminal", terminal)]
    items += [(name, getattr(stats, name, 0) or 0)
              for name in _RESIDUAL_COUNT_FIELDS]
    items += [("deferred_reason", str(reason) if reason else None),
              ("maintenance_failed",
               bool(getattr(stats, "maintenance_failed", False)))]
    return tuple((name, value) for name, value in items if value)


def unpublished_deferrals(stats) -> dict:
    """``{path: (file_identity, covered, keys)}`` for every Codex deferral of
    the pass that no unit of the same pass published."""
    if stats is None:
        return {}
    deferred = getattr(stats, "barrier_deferred_keys", None) or {}
    targets = getattr(stats, "barrier_deferred_targets", None) or {}
    published = {str(path) for path in
                 (getattr(stats, "barrier_published_paths", None) or ())}
    out = {}
    for path, keys in deferred.items():
        if str(path) in published:
            continue
        identity, covered = targets.get(path, (None, False))
        out[str(path)] = (identity, bool(covered), frozenset(keys))
    return out


def residual_digest(provider_results: dict, deferral_facts: dict) -> tuple:
    """Canonical, hashable: equal iff every residual component is equal.

    ``provider_results`` maps a component name (a provider, or "accounting")
    to its ``(name, value)`` pairs; ``deferral_facts`` maps each unpublished
    deferral's path to its clipped facts. Never carries the global seal
    token. ``()`` means empty."""
    parts = tuple((name, tuple(value))
                  for name, value in sorted(provider_results.items()) if value)
    facts = tuple(sorted(deferral_facts.items()))
    if not parts and not facts:
        return ()
    return (parts, facts)


def digest_hash(digest) -> "str | None":
    """A short stable hash of a residual digest; ``None`` for the empty one."""
    if not digest:
        return None
    return hashlib.sha256(repr(digest).encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class _Anchor:
    """A deferral's startup work, bound when its anchor resolved."""
    generation: object
    end: int
    required: tuple          # sorted ((key, required_revision), ...)


def _resolve_anchor(keys, raw) -> "_Anchor | None":
    """Resolved only with a complete line end at the target's generation,
    membership proven there and a population row for every key."""
    target = (raw or {}).get("target")
    if target is None:
        return None
    generation = target.get("generation")
    end = target.get("complete_line_end")
    if end is None or target.get("accepted_generation") != generation:
        return None
    facts = (raw or {}).get("keys") or {}
    required = []
    for key in sorted(set(keys) | set((raw or {}).get("membership") or ())):
        revision = (facts.get(key) or {}).get("required")
        if revision is None:
            return None
        required.append((key, int(revision)))
    return _Anchor(generation, int(end), tuple(required))


def _cap(value, limit):
    return None if value is None else min(int(value), int(limit))


def _clipped_facts(keys, anchor, raw) -> tuple:
    """A deferral's facts, scoped to its startup work: progress on its own
    keys and the resolution of its anchor change them, input beyond the
    anchored end and other conversations' seals do not."""
    target = (raw or {}).get("target")
    if anchor is not None:
        same = (target is not None
                and target.get("generation") == anchor.generation)
        accepted = None
        if (same and target.get("accepted_generation") == anchor.generation
                and target.get("accepted_end") is not None):
            accepted = min(int(target["accepted_end"]), anchor.end)
        facts = (raw or {}).get("keys") or {}
        per_key = tuple(
            (key, _cap((facts.get(key) or {}).get("certificate"), required),
             _cap((facts.get(key) or {}).get("published"), required),
             _cap((facts.get(key) or {}).get("repair"), required))
            for key, required in anchor.required)
        return ("resolved", same,
                None if target is None else target.get("state"),
                None if target is None else target.get("reason"),
                accepted, per_key)
    if target is None:
        return ("unresolved", None, None, None, False, None,
                tuple(sorted(keys)))
    return ("unresolved", target.get("generation"), target.get("state"),
            target.get("reason"), target.get("complete_line_end") is not None,
            target.get("accepted_generation"),
            tuple(sorted(set(keys) | set((raw or {}).get("membership") or ()))))


def _discharged(anchor: "_Anchor | None", raw) -> bool:
    """Every anchored key's publication reached its anchored requirement.
    Accounting acceptance never counts: a contributor can be fully accepted
    while its publication group still waits."""
    if anchor is None or not anchor.required or raw is None:
        return False
    facts = raw.get("keys") or {}
    for key, required in anchor.required:
        published = (facts.get(key) or {}).get("published")
        if published is None or int(published) < required:
            return False
    return True


class StartupDrain:
    """The pure evaluator behind writeIo's admission hold (#939 D5).

    ``observe_accounting`` records each accounting tick; ``observe`` receives
    every completed conversation pass and returns ``"drained"``,
    ``"settled"`` or ``None``. ``pass_info`` carries ``counted`` (the pass
    started after the first accounting-census-complete tick ended; only a
    counted pass resolves anchors or discharges obligations),
    ``census_complete`` (every provider's census complete, or a pass that
    ended in an error or an unavailable store, and no detached Codex worker
    in progress at the pass's end), ``certifiable`` (both providers; never an
    error pass), per-provider ``mode`` and ``residual`` (``None`` or a
    ``caught_up`` mode carries the previous one forward; the ``"pass"`` entry
    is the pass's own status, empty for ``ok`` and ``((status, exception
    class),)`` otherwise, so identical error passes settle and a recovery
    restarts the chain), ``deferrals``
    (``{path: (identity, covered, keys)}`` of the unpublished Codex deferrals,
    ``None`` when Codex did not run) and ``facts`` (the stored facts of every
    tracked deferral, read at the pass's end).
    """

    def __init__(self):
        self.first_complete_end_ns = None
        self.accounting: dict = {}
        self._accounting_seen = False
        self._chain_broken = False
        self.components: dict = {}
        self.deferrals: dict = {}
        self.tracked: dict = {}
        self.anchors: dict = {}
        self.obligations: set = set()
        self.previous_digest = None
        self.digest: tuple = ()
        self.counts = None

    def observe_accounting(self, *, ended_ns, census_complete, residuals):
        merged = dict(self.accounting)
        for provider, residual in (residuals or {}).items():
            if residual is not None:            # caught_up carries forward
                merged[provider] = tuple(residual)
        if self._accounting_seen and (
                not census_complete or merged != self.accounting):
            self._chain_broken = True
        self._accounting_seen = True
        self.accounting = merged
        if census_complete and self.first_complete_end_ns is None:
            self.first_complete_end_ns = int(ended_ns)

    def watch(self) -> dict:
        out = {}
        for path, (identity, keys) in self.tracked.items():
            anchor = self.anchors.get(path)
            anchored = (frozenset(key for key, _ in anchor.required)
                        if anchor is not None else frozenset())
            out[path] = (identity, frozenset(keys) | anchored)
        return out

    def _update_deferrals(self, deferrals) -> None:
        self.deferrals = {str(path): (identity, bool(covered), frozenset(keys))
                          for path, (identity, covered, keys)
                          in deferrals.items()}
        for path, (identity, covered, keys) in self.deferrals.items():
            prior = self.tracked.get(path)
            self.tracked[path] = (
                identity, keys | (prior[1] if prior else frozenset()))
            if covered and keys:
                self.obligations.add(path)
        for path in list(self.tracked):
            if path not in self.deferrals and path not in self.obligations:
                self.tracked.pop(path)
                self.anchors.pop(path, None)

    def _residual_counts(self, unpublished) -> tuple:
        failed = sum(len(dict(component).get("failed_paths", ()))
                     for component in self.components.values())
        codex = dict(self.components.get("codex", ()))
        return (failed, len(unpublished), len(self.obligations),
                int(codex.get("container_pending", 0) or 0),
                int(codex.get("barrier_keys_deferred", 0) or 0))

    def observe(self, pass_info) -> "str | None":
        info = pass_info or {}
        for name, entry in (info.get("providers") or {}).items():
            residual = entry.get("residual")
            if entry.get("mode") == "caught_up" or residual is None:
                continue
            self.components[name] = tuple(residual)
        if info.get("deferrals") is not None:
            self._update_deferrals(info["deferrals"])
        facts = info.get("facts") or {}
        if info.get("counted"):
            # Only a counted pass anchors or discharges: one that started
            # before accounting caught up could freeze an anchor at
            # accounting's partial progress.
            for path, (_identity, keys) in self.tracked.items():
                if path not in self.anchors and path in facts:
                    anchor = _resolve_anchor(keys, facts[path])
                    if anchor is not None:
                        self.anchors[path] = anchor
            for path in list(self.obligations):
                if _discharged(self.anchors.get(path), facts.get(path)):
                    self.obligations.discard(path)
        unpublished = set(self.deferrals) | self.obligations
        clipped = {
            path: _clipped_facts(self.tracked.get(path, (None, frozenset()))[1],
                                 self.anchors.get(path), facts.get(path))
            for path in unpublished}
        results = dict(self.components)
        results["accounting"] = tuple(sorted(
            (provider, residual)
            for provider, residual in self.accounting.items() if residual))
        self.digest = residual_digest(results, clipped)
        self.counts = self._residual_counts(unpublished)
        if self._chain_broken:
            self.previous_digest = None
            self._chain_broken = False
        if not info.get("counted") or not info.get("census_complete"):
            self.previous_digest = None
            return None
        if (info.get("certifiable") and not self.obligations
                and not any(self.accounting.values())):
            return "drained"
        if self.digest and self.digest == self.previous_digest:
            return "settled"
        self.previous_digest = self.digest
        return None


def hold_admission() -> None:
    """Place the startup admission hold (before the main sync thread)."""
    state = _STATE
    with _LOCK:
        state.hold_placed = True
        state.released_by = None
        state.drain = StartupDrain()


def admission_held() -> bool:
    state = _STATE
    return state.hold_placed and state.released_by is None


def release_admission(reason: str) -> bool:
    """Release the hold once; admits warm at once when a non-cold publication
    was already seen, otherwise at the next one. False when no hold was
    placed or it was already released."""
    if reason not in RELEASE_REASONS:
        raise ValueError(f"unknown release reason: {reason!r}")
    state = _STATE
    with _LOCK:
        if not state.hold_placed or state.released_by is not None:
            return False
        state.released_by = reason
        state.drain = None
        if (state.warm_admitted_ns is None
                and state.first_non_cold_ns is not None):
            now, reading = _sample_locked(state)
            state.warm_admitted_ns = now
            state.cold_startup_bytes = reading.value
        return True


def observe_accounting_tick(*, ended_ns, census_complete, residuals) -> None:
    """One accounting tick of the main sync thread (any process may call
    this; only a placed hold evaluates it)."""
    state = _STATE
    with _LOCK:
        if census_complete:
            state.accounting_caught_up = True
        if state.drain is not None:
            state.drain.observe_accounting(
                ended_ns=ended_ns, census_complete=bool(census_complete),
                residuals=residuals)


def observe_conversation_pass(pass_info, *, started_ns, seq) -> "str | None":
    """Feed one completed conversation pass to the evaluator; returns its
    release verdict. A pass counts only if it started after the end of the
    first accounting-census-complete tick (same monotonic clock)."""
    state = _STATE
    with _LOCK:
        drain = state.drain
        if drain is None or state.released_by is not None:
            return None
        first = drain.first_complete_end_ns
        counted = first is not None and int(started_ns) > first
        census = bool(pass_info and pass_info.get("census_complete"))
        info = dict(pass_info or {})
        info["counted"] = counted
        info["census_complete"] = census
        try:
            verdict = drain.observe(info)
        except Exception:
            # A pass the evaluator could not judge breaks the comparison
            # chain: the next pass must not settle against the one before
            # it, across an observation that never completed.
            drain.previous_digest = None
            raise
        state.last_pass = (int(seq), census, counted, digest_hash(drain.digest))
        if counted:
            state.residual_counts = drain.counts
        return verdict


def startup_watch() -> dict:
    """``{path: (file_identity, keys)}`` of every deferral the evaluator
    tracks; empty once released or when no hold was placed."""
    with _LOCK:
        drain = _STATE.drain
        return {} if drain is None else drain.watch()


# ── the `/api/debug/backend` `writeIo` object (spec §5.5) ─────────────────

def write_io_payload(*, now_ns=None, now_utc=None, limits=None) -> dict:
    """The camelCase `writeIo` object: the trailing five-minute statistic,
    its budget verdict (the hysteresis state, never advanced here), the
    excluded deletion intervals, cold startup and the maintenance ledger.
    Numbers are `None`, never zero, whenever the statistic cannot stand."""
    state = _STATE
    limits = limits or _budget.LIMITS
    if state.last_status == "not_sampled":
        read_counter()
    now = state.clock_ns() if now_ns is None else int(now_ns)
    wall = (dt.datetime.fromtimestamp(state.wall_s(), UTC)
            if now_utc is None else now_utc.astimezone(UTC))
    with _LOCK:
        stat = _steady_locked(now)
        hysteresis = state.hysteresis
        last_status = state.last_status
        cold = state.cold_startup_bytes
        admitted = state.warm_admitted_ns is not None
        released_by = state.released_by
        held = state.hold_placed and released_by is None
        caught_up = state.accounting_caught_up
        counts = state.residual_counts
        last_pass = state.last_pass
        local = None if state.local_instance is None else dict(state.local_instance)
        observed = state.ledger_observed
    verdict, reasons = _budget.steady_verdict(stat, hysteresis, limits)
    maintenance = (_budget.maintenance_statistic(maintenance_ledger(), wall)
                   .as_wire() if observed else None)
    last_wall = stat.last_excluded_wall_s
    return {
        "status": "ok" if last_status == "ok" else "unavailable",
        "reason": None if last_status == "ok" else last_status,
        "source": state.counter.source,
        "scope": SCOPE,
        "instanceId": None if local is None else local["instanceId"],
        "sampledAt": _budget.iso_z(wall),
        "windowSeconds": (None if stat.window_seconds is None
                          else round(stat.window_seconds, 3)),
        "bytesWritten": stat.bytes_written,
        "bytesPerMinute": stat.bytes_per_minute,
        "tickCount": stat.publications,
        "meanBytesPerTick": stat.mean_bytes_per_publication,
        "excludedDeletions": {
            "operations": stat.excluded_operations,
            "rows": stat.excluded_rows,
            "bytes": stat.excluded_bytes,
            "lastEndedAt": (None if last_wall is None else _budget.iso_z(
                dt.datetime.fromtimestamp(last_wall, UTC))),
        },
        "coldStartup": {
            "bytes": cold, "admitted": admitted,
            # #939 D5: the startup admission hold (additive keys).
            "releasedBy": released_by, "held": held,
            "accountingCaughtUp": caught_up,
            "residual": (None if counts is None
                         else dict(zip(RESIDUAL_COUNT_KEYS, counts))),
            "lastPass": (None if last_pass is None else {
                "seq": last_pass[0], "censusComplete": last_pass[1],
                "counted": last_pass[2], "residualDigest": last_pass[3]}),
        },
        "maintenance": maintenance,
        "reclaimRefusal": reclaim_refusal(),
        "budget": {
            "policyVersion": limits.policy_version,
            "verdict": verdict,
            "reasons": list(reasons),
            "bytesPerMinuteLimit": limits.bytes_per_minute,
            "meanBytesPerTickLimit": limits.bytes_per_publication,
        },
    }
