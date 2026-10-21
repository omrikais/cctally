"""Waiting requests and the drain-admission gate (#968 spec 5.7, 5.12).

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
from an incompatible clock is expired and removed. Stdlib only; no cctally
imports.
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

# Module attributes so tests can drive a deterministic schedule.
_monotonic_ns: Callable[[], int] = time.monotonic_ns
_sleep: Callable[[float], None] = time.sleep
#: Post-release seam: called by ``finish`` after the store locks AND the gate
#: are released. Production leaves it None; the fairness test synchronizes on
#: it instead of sleeping (review P2-7).
_after_finish: Optional[Callable[[], None]] = None


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
    """Ids of fresh requests; expired or malformed records are removed one by one."""
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
    """Sleep in bounded steps until no request is fresh (requests expire)."""
    while fresh_requests(app_dir):
        if should_continue is not None and not should_continue():
            return
        _sleep(POLL_SECONDS)
