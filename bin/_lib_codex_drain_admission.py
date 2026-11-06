"""Waiting requests and the drain-admission gate (#968 spec 5.7, 5.12).

Drain workers yield to two signals, both reported by ``fresh_requests``:
immutable Codex requests that end only by expiry (below), and one
process-owned Claude waiter whose expiring record bounds it (#1055, after the
Codex request section).

A Codex hook never waits for a lock. When it meets cache contention it
publishes a uniquely named, immutable request record under the data
directory (outside every SQLite store, because a contended hook cannot write
cache.db). Drain workers — the prune executor and the container-verification
executor — admit one identity at a time through ONE global gate and yield
while any request is fresh:

1. check for fresh requests;
2. acquire the drain gate (non-blocking), then check again;
3. try the store's complete lock domain without blocking, then check again;
4. a fresh request at any check releases everything acquired and yields;
5. otherwise the caller executes one identity, commits, releases every store
   lock and the gate (``finish``), and starts again at step 1.

Requests end only by expiry: 60 s after publication on a cross-process
monotonic clock. A worker observing a request, or restarting, never renews it;
a wall-clock jump cannot extend it (the wall age only shortens freshness, and
a NEGATIVE wall age — an mtime ahead of the wall clock after a backward step —
counts as fresh, bounded by the monotonic lifetime); a malformed record or one
from an incompatible clock is expired and removed.

The Claude waiter (#1055 spec 4.1-4.3). A Claude hook whose single attempt on
``cache.db.lock`` is refused while it sees an admitted drain unit (the Codex
provider lock and the gate both busy) registers on ``claude-cache-waiter.lock``:
one ``LOCK_EX | LOCK_NB`` attempt, never retried, then an owner-written
record ``cctally-claude-waiter v1 <pid> <expiry_ns>`` (expiry on this module's
monotonic clock). The record is written at registration (expiry: the hook's
acquisition deadline) and once on acquiring the writer lock (expiry: now plus
the hold allowance); nothing else renews it, least of all an observation.
Drains probe the file read-only with ``LOCK_SH | LOCK_NB``: the waiter is
fresh only while the probe is refused AND the record parses, is unexpired and
is at most ``max(T, hold allowance)`` ahead. A fresh waiter is reported as the
reserved sentinel after the Codex ids, so every admission check, recheck and
backoff yields to it unchanged; with no fresh waiter the value is exactly the
Codex ids. The release truncates the record before unlocking; a killed hook's
lock is released by the kernel; the file is never unlinked. Stdlib only; no
cctally imports.
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import time
import uuid
from typing import Callable, NamedTuple, Optional

REQUEST_DIR_NAME = "codex-drain-requests"
REQUEST_TTL_NS = 60_000_000_000
WALL_GUARD_SECONDS = 120.0
GATE_FILE_NAME = "codex-drain-admission.lock"
POLL_SECONDS = 5.0
_RECORD_VERSION = 1

# #1055: the Claude waiter. Module constants read at call time (monkeypatchable,
# not configuration).
CLAUDE_WAITER_FILE_NAME = "claude-cache-waiter.lock"
#: Reserved id; a generated Codex id is ``<mono>-<pid>-<hex>``, never this.
CLAUDE_WAITER_SENTINEL = "claude-waiter"
#: T: how long a refused Claude hook may wait for ``cache.db.lock``.
CLAUDE_HOOK_DRAIN_WAIT_SECONDS = 5.0
CLAUDE_HOOK_DRAIN_POLL_SECONDS = 0.05
#: How long the waiter stays fresh once its hook holds ``cache.db.lock``.
CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS = 15.0
#: The drain's step while only the Claude waiter is fresh.
SHORT_POLL_SECONDS = 0.05
#: How recent an observed waiter must be for ``_drain_backoff``'s short step.
CLAUDE_WAITER_RECENT_SECONDS = 1.0
_WAITER_MAGIC = "cctally-claude-waiter"
_WAITER_VERSION = "v1"
_WAITER_RECORD_MAX = 128

# Module attributes so tests can drive a deterministic schedule.
_monotonic_ns: Callable[[], int] = time.monotonic_ns
_sleep: Callable[[float], None] = time.sleep
#: Post-release seam: called by ``finish`` after the store locks AND the gate
#: are released. Production leaves it None; the fairness test synchronizes on
#: it instead of sleeping (review P2-7).
_after_finish: Optional[Callable[[], None]] = None
#: Per-process hint: ``os.path.realpath(app_dir)`` -> the ``_monotonic_ns``
#: time of the last probe that found a fresh waiter. It selects a sleep
#: interval only; empty checks never renew it.
_claude_waiter_observed: "dict[str, int]" = {}


def _clear_claude_waiter_hint() -> None:
    _claude_waiter_observed.clear()


os.register_at_fork(after_in_child=_clear_claude_waiter_hint)


def _request_dir(app_dir) -> pathlib.Path:
    return pathlib.Path(app_dir) / REQUEST_DIR_NAME


def publish_waiting_request(app_dir, *, root_keys) -> "str | None":
    """Atomically publish one immutable request; return its id or None.

    ``None`` is a protocol failure the hook records; it is never fairness
    evidence and never raises.
    """
    try:
        directory = _request_dir(app_dir)
        directory.mkdir(parents=True, exist_ok=True)
        mono = int(_monotonic_ns())
        request_id = f"{mono}-{os.getpid()}-{uuid.uuid4().hex}"
        final = directory / f"req-{request_id}.json"
        tmp = directory / f".tmp-{request_id}"
        payload = json.dumps({
            "v": _RECORD_VERSION, "id": request_id,
            "roots": sorted(str(k) for k in root_keys), "mono_ns": mono,
        }, separators=(",", ":"))
        with open(tmp, "x", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, final)
        return request_id
    except OSError:
        return None


def _is_fresh(path: pathlib.Path, now_mono: int, now_wall: float) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        mono = int(record["mono_ns"])
        if record.get("v") != _RECORD_VERSION:
            return False
        wall_age = now_wall - path.stat().st_mtime
    except (OSError, ValueError, TypeError, KeyError):
        return False
    age = now_mono - mono
    # A negative monotonic age is an incompatible clock: expired. A negative
    # WALL age is only an mtime ahead of the wall clock, which is not evidence
    # of staleness: it stays fresh, and the monotonic bound still ends it
    # within the single 60 s lifetime (review P3-5).
    return 0 <= age < REQUEST_TTL_NS and wall_age < WALL_GUARD_SECONDS


def fresh_requests(app_dir) -> "list[str]":
    """Ids of fresh requests; expired or malformed records are removed one by one.

    A fresh Claude waiter follows the Codex ids as ``CLAUDE_WAITER_SENTINEL``;
    it is probed even when the request directory is missing or unreadable.
    """
    fresh = _fresh_codex_requests(app_dir)
    if claude_waiter_fresh(app_dir):
        fresh.append(CLAUDE_WAITER_SENTINEL)
    return fresh


def _fresh_codex_requests(app_dir) -> "list[str]":
    directory = _request_dir(app_dir)
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    now_mono = int(_monotonic_ns())
    now_wall = time.time()
    fresh: list[str] = []
    for path in entries:
        name = path.name
        if name.startswith(".tmp-"):
            try:
                if now_wall - path.stat().st_mtime >= WALL_GUARD_SECONDS:
                    path.unlink()
            except OSError:
                pass
            continue
        if not (name.startswith("req-") and name.endswith(".json")):
            continue
        if _is_fresh(path, now_mono, now_wall):
            fresh.append(name[len("req-"):-len(".json")])
        else:
            try:
                path.unlink()
            except OSError:
                pass
    return fresh


def _waiter_path(app_dir) -> pathlib.Path:
    return pathlib.Path(app_dir) / CLAUDE_WAITER_FILE_NAME


def _hint_key(app_dir) -> str:
    return os.path.realpath(os.fspath(app_dir))


def _waiter_bound_ns() -> int:
    return round(max(CLAUDE_HOOK_DRAIN_WAIT_SECONDS,
                     CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS) * 1e9)


def _parse_waiter_expiry(data: bytes) -> "int | None":
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n"):
        return None
    parts = text[:-1].split(" ")
    if (len(parts) != 4 or parts[0] != _WAITER_MAGIC
            or parts[1] != _WAITER_VERSION
            or not parts[2].isdigit() or not parts[3].isdigit()):
        return None
    return int(parts[3])


class ClaudeWaiter:
    """The one process-owned Claude waiter (#1055 spec 4.1)."""

    def __init__(self, fd: int):
        self._fd: "int | None" = fd

    @classmethod
    def register(cls, app_dir, *, deadline_ns: int) -> "ClaudeWaiter | None":
        """One non-blocking exclusive attempt; None when another waiter or a
        drain probe holds the file at that instant (never retried)."""
        try:
            fd = os.open(_waiter_path(app_dir), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        waiter = cls(fd)
        try:
            waiter._write(deadline_ns)
        except OSError:
            waiter.release()
            return None
        return waiter

    def _write(self, expiry_ns: int) -> None:
        record = (f"{_WAITER_MAGIC} {_WAITER_VERSION} {os.getpid()} "
                  f"{int(expiry_ns)}\n").encode("ascii")
        os.pwrite(self._fd, record, 0)
        os.ftruncate(self._fd, len(record))

    def mark_holding(self) -> None:
        """Rewrite the record once the hook holds ``cache.db.lock``: fresh for
        the hold allowance from now. A failed write leaves the registration
        record, which only ends the yield sooner."""
        if self._fd is None:
            return
        try:
            self._write(int(_monotonic_ns())
                        + round(CLAUDE_WAITER_HOLD_ALLOWANCE_SECONDS * 1e9))
        except OSError:
            pass

    def release(self) -> None:
        """Truncate the record, unlock, close. Idempotent; never raises
        ``OSError``."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        for step in (lambda: os.ftruncate(fd, 0),
                     lambda: fcntl.flock(fd, fcntl.LOCK_UN),
                     lambda: os.close(fd)):
            try:
                step()
            except OSError:
                pass


def claude_waiter_fresh(app_dir) -> bool:
    """The drain-side probe: never creates the file, holds no lock past the
    call, and stamps the observation hint when the waiter is fresh."""
    try:
        fd = os.open(_waiter_path(app_dir), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        except OSError:
            return False
        else:
            return False                # no waiter; close releases the probe
        try:
            data = os.pread(fd, _WAITER_RECORD_MAX, 0)
        except OSError:
            return False
    finally:
        os.close(fd)
    expiry = _parse_waiter_expiry(data)
    if expiry is None:
        return False
    now = int(_monotonic_ns())
    if not (now < expiry and expiry - now <= _waiter_bound_ns()):
        return False
    _claude_waiter_observed[_hint_key(app_dir)] = now
    return True


def claude_waiter_recently_observed(app_dir) -> bool:
    """True when this process found a fresh waiter less than
    ``CLAUDE_WAITER_RECENT_SECONDS`` ago (a sleep-interval hint only)."""
    stamp = _claude_waiter_observed.get(_hint_key(app_dir))
    if stamp is None:
        return False
    age = int(_monotonic_ns()) - stamp
    return 0 <= age < round(CLAUDE_WAITER_RECENT_SECONDS * 1e9)


def lock_busy(path) -> bool:
    """A shared non-blocking probe on its own descriptor, closed at once.
    A missing file or any non-contention error is no evidence (False)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


class DrainGate:
    """The one global, non-blocking drain-admission flock."""

    def __init__(self, app_dir):
        self._path = pathlib.Path(app_dir) / GATE_FILE_NAME
        self._fd: "int | None" = None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class Admission(NamedTuple):
    admitted: bool
    store_locks: object
    gate: "DrainGate | None"


def admit(app_dir, try_store_locks: Callable[[], object],
          release_store_locks: Callable[[object], None], *,
          on_check: Optional[Callable[[str], None]] = None) -> Admission:
    """Steps 1-4 of the protocol. ``try_store_locks`` must never block and
    returns a token, or None on contention."""
    def check(stage: str) -> bool:
        if on_check is not None:
            on_check(stage)
        return bool(fresh_requests(app_dir))

    if check("before-gate"):
        return Admission(False, None, None)
    gate = DrainGate(app_dir)
    if not gate.try_acquire():
        return Admission(False, None, None)
    if check("after-gate"):
        gate.release()
        return Admission(False, None, None)
    token = try_store_locks()
    if token is None:
        gate.release()
        return Admission(False, None, None)
    if check("after-store-locks"):
        release_store_locks(token)
        gate.release()
        return Admission(False, None, None)
    return Admission(True, token, gate)


def finish(admission: Admission,
           release_store_locks: Callable[[object], None]) -> None:
    """Step 5: release every store lock, then the gate."""
    try:
        if admission.store_locks is not None:
            release_store_locks(admission.store_locks)
    finally:
        if admission.gate is not None:
            admission.gate.release()
    if _after_finish is not None:
        _after_finish()


def wait_until_no_fresh_requests(
        app_dir, *, should_continue: Optional[Callable[[], bool]] = None) -> None:
    """Sleep in bounded steps until nothing is fresh (requests expire; the
    Claude waiter's record bounds it). The set is re-classified after every
    sleep: the Claude sentinel alone takes ``SHORT_POLL_SECONDS``, any set
    holding a Codex request ``POLL_SECONDS``."""
    while True:
        fresh = fresh_requests(app_dir)
        if not fresh:
            return
        if should_continue is not None and not should_continue():
            return
        if fresh == [CLAUDE_WAITER_SENTINEL]:
            _sleep(SHORT_POLL_SECONDS)
        else:
            _sleep(POLL_SECONDS)
