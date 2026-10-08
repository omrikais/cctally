"""Writer-owned WAL checkpoints for the sync writers (#901 spec §5.3c W9).

SQLite's automatic checkpoint runs after every commit that leaves 1,000 or more
frames in the WAL. While readers keep the WAL from resetting it runs after
nearly every commit and copies its frames into the database file again: P2
measured 85% of conversations.db's WAL bytes copied a second time. A sync
writer instead sets ``PRAGMA wal_autocheckpoint = 0`` once its opener's
open-time work is done and owns the checkpoint itself (Q13), and revision 13
(Q14) makes that deferral span passes and ticks:

* It counts the WAL frames its own connection appends, from
  ``sqlite3_db_status(SQLITE_DBSTATUS_CACHE_WRITE)``, which in WAL mode counts
  the pages a connection writes to the WAL. The count is read through the
  stdlib extension's own linked SQLite, by the same ctypes layout
  ``_lib_sqlite_close`` uses, and reset on every read, so this module owns the
  counter. It is never the ``-wal`` file's length: a checkpointed WAL restarts
  from its first frame and reuses the file, whose length then stays constant
  while frames are appended (901-SR-024). A test proves the counter on the
  runtime SQLite; where it cannot be read, the connection keeps SQLite's
  default ``wal_autocheckpoint``.
* A per-process, per-database policy keeps a pending flag that a commit sets
  and only a complete checkpoint clears. It runs one
  ``PRAGMA main.wal_checkpoint(PASSIVE)`` when the flag is set and the frames
  appended since its last attempt reach ``SIZE_TRIGGER_BYTES`` (size trigger,
  evaluated after a commit), and whenever ``TIMER_SECONDS`` have passed since
  the process's last attempt on that store, counted from when the process
  armed it if it never attempted, whether or not the process committed
  anything itself (timer). Another process's frames (a hook's,
  ``cache-sync``'s) are otherwise copied by no one; an attempt with nothing
  to copy writes nothing.
* A checkpoint is complete only when the pragma succeeds with ``busy = 0`` and
  valid, non-negative counts with ``log = checkpointed``. Anything else (an
  error, ``SQLITE_BUSY`` from a competing checkpoint, a busy result, ``-1``
  counts, an incomplete copy while a reader still needs frames) keeps the flag
  set, so a later due attempt retries it, never sooner than ``TIMER_SECONDS``
  after the attempt; nothing loops between attempts.
* The keeper (Q14). The first arm of a store in a process also opens one idle
  keeper connection to it, kept until a maintenance yield (below), a store
  replacement or the process's exit. It reads
  the database once, so it holds the store's WAL open, and then holds no
  transaction, so it pins no frame and blocks no checkpoint. While it is open
  a sync connection's close is not the store's last close, so it neither
  checkpoints nor resets the WAL: the dashboard opens and closes its sync
  connections every pass and tick, and with SQLite's close-time checkpoint
  every pass had ended in a checkpoint and a WAL reset (§1.8). Each later arm
  compares the store's file identity with the keeper's and reopens it on a
  replaced file, so no keeper stays on an obsolete inode, and a drain check in
  this process releases it (``release``) so a keeper never blocks this
  process's own store recovery.
* The timer thread (901-SR-033). A dashboard iteration whose ingest frontier
  is caught up skips the sync functions, so boundaries alone cannot bound the
  retained frames. One daemon thread per process, started with the first
  keeper, makes the timed attempt through the keeper on each kept store every
  ``TIMER_INTERVAL_SECONDS``. The keeper is used only by that thread and by
  the finalizer, under one lock.
* The finalizer (901-SR-034). ``finalize`` attempts one PASSIVE on each
  store the process armed and closes its keepers; a keeper close that is the
  store's last also runs SQLite's close-time checkpoint, so a short-lived
  writer that is alone drains the WAL as before, and one that is not still
  hands its frames to a checkpoint attempt. A store whose keeper was released
  or yielded is attempted through a short-lived connection under the same
  rules (#901 Amendment 19 PR-7). It runs at interpreter exit (``atexit``) and is called
  explicitly before the ``os._exit`` of every detached worker that may sync.
  Only abrupt termination drains nothing; the next opener recovers the WAL.
* Fork. A forked child starts with no keeper and no timer thread: the at-fork
  handler drops the parent's keepers in the child without using or closing
  them, and a child that syncs arms its own.
* The yield (Q15). An idle keeper still keeps its shared lock on the database
  file, as every WAL-mode connection does between transactions, so while one
  is open SQLite's exclusive access (``db vacuum``) and every handle-drain
  check fail. A process that needs the family exclusive or drained publishes
  an owner-qualified request, ``<store>.keeper-yield``; a live repair marker
  and a pending-quarantine record fence keepers the same way. The fence check
  (``live_fence``) runs at every ``arm``, at the start of every timer round
  before the due check, and in the finalizer; it reads the owner records
  through the repair marker's reader and liveness check, takes no maintenance
  or provider lock, writes nothing and removes nothing. On a live fence the
  keeper closes with no-checkpoint-on-close and no PASSIVE, the store's policy
  is kept, the store is suspended and one diagnostic line (``YIELD_DIAGNOSTIC``)
  goes to stderr. An admission checks before it opens the keeper and again
  after, while the arming connection is still open, under the module's lock.
  A suspended store resumes at the first ``arm`` (under the module's lock
  alone, because its callers already hold the writer and provider locks) or
  timer round (under the store's maintenance lock taken shared and
  non-blocking for that admission only) that finds no fence. Contention on a
  maintenance flock is never read as a request.
* The close control (Q15). The keeper's no-checkpoint-on-close setting is
  applied and verified before its first database read; where the runtime
  cannot set it the process admits no keeper (remembered per process) and the
  store keeps revision 12's close-time checkpoints. Only the finalizer, for an
  unchanged and unfenced store, restores normal close behaviour.
* The #297 end-of-sync ``TRUNCATE`` shrink stays; a failed shrink is not
  retried for ``SHRINK_RETRY_SECONDS``.

Reclaim's and the deletion connection's settings are not touched here (§5.4).
``CCTALLY_TEST_W9_DISABLE=1`` is a test-only seam that leaves every connection
at SQLite's default and opens no keeper, timer or exit attempt (P3's "W9
disabled" run). Stdlib-only leaf.
"""
from __future__ import annotations

import atexit
import fcntl
import os
import pathlib
import sqlite3
import sys
import threading
import time
from typing import Callable

#: Frames appended since the last attempt that trigger a checkpoint after a
#: commit, in bytes of WAL frames (page plus the 24-byte frame header).
SIZE_TRIGGER_BYTES = 16 * 1024 * 1024
#: Seconds since the process's last attempt on a store (or since it armed the
#: store) after which a boundary or the timer thread attempts a checkpoint.
TIMER_SECONDS = 60.0
#: Real seconds between two rounds of the timer thread. Read before every
#: wait, so a test that injects it before the first arm gets a fast thread.
TIMER_INTERVAL_SECONDS = 10.0
#: Seconds a failed #297 end-of-sync shrink waits before the next one.
SHRINK_RETRY_SECONDS = 60.0
#: The test-only seam that turns W9 off for a whole process.
DISABLE_ENV = "CCTALLY_TEST_W9_DISABLE"
#: `SQLITE_DBSTATUS_CACHE_WRITE` from sqlite3.h.
SQLITE_DBSTATUS_CACHE_WRITE = 9
#: The WAL frame header that precedes every page image.
WAL_FRAME_HEADER_BYTES = 24
#: The timer thread's name, so a test or a stack dump can find it.
TIMER_THREAD_NAME = "cctally-w9-checkpoint-timer"
#: The stable marker of the one stderr line every keeper yield writes, which
#: P's verdicts look for in the dashboard log (spec §6.3, P after revision 14).
YIELD_DIAGNOSTIC = "[w9] keeper-yield"
#: The fences a keeper yields to (Q15), as the diagnostic line names them.
FENCE_REQUEST = "keeper-yield request"
FENCE_REPAIR = "repair marker"
FENCE_QUARANTINE = "pending quarantine"
FENCE_UNVERIFIABLE = "fence state unverifiable"

#: The clock every policy reads (monotonic seconds); a test injects its own.
CLOCK: "Callable[[], float]" = time.monotonic


def disabled() -> bool:
    """Whether the test-only seam turned W9 off for this process."""
    return os.environ.get(DISABLE_ENV) == "1"


# ── the frame source ─────────────────────────────────────────────────────────

_DB_STATUS = None
_DB_STATUS_RESOLVED = False


def _db_status_function():
    """``sqlite3_db_status`` from the stdlib extension's own SQLite, or None."""
    global _DB_STATUS, _DB_STATUS_RESOLVED
    if _DB_STATUS_RESOLVED:
        return _DB_STATUS
    _DB_STATUS_RESOLVED = True
    try:
        if sys.implementation.name != "cpython":
            return None
        import sysconfig
        if sysconfig.get_config_var("Py_GIL_DISABLED"):
            # A free-threaded object header does not have the layout below.
            return None
        import _sqlite3
        import ctypes

        function = ctypes.CDLL(_sqlite3.__file__).sqlite3_db_status
        function.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.c_int)
        function.restype = ctypes.c_int
        _DB_STATUS = function
    except (ImportError, OSError, AttributeError):
        _DB_STATUS = None
    return _DB_STATUS


def _connection_handle(conn: sqlite3.Connection) -> "int | None":
    """The ``sqlite3 *`` behind a CPython connection (first field after the
    object header, as ``_lib_sqlite_close`` reads it), or None when closed."""
    import ctypes

    if type(conn) is not sqlite3.Connection and not isinstance(
            conn, sqlite3.Connection):
        return None
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    handle = ctypes.c_void_p.from_address(id(conn) + 2 * pointer_size).value
    return int(handle) if handle else None


def take_frames(conn: sqlite3.Connection, *,
                reset: bool = True) -> "int | None":
    """WAL frames this connection appended since the previous call.

    ``SQLITE_DBSTATUS_CACHE_WRITE`` read with its reset flag, so each call
    returns the frames written since the last one and this module owns the
    counter. In WAL mode it counts the pages the connection writes to the WAL
    (summed over its attached databases, of which a sync writer only writes
    its main one). None when the count cannot be read on this runtime.
    ``reset=False`` reads without consuming.
    """
    function = _db_status_function()
    if function is None:
        return None
    import ctypes

    try:
        handle = _connection_handle(conn)
    except (ValueError, OverflowError):
        return None
    if handle is None:
        return None
    current, highwater = ctypes.c_int(), ctypes.c_int()
    rc = function(ctypes.c_void_p(handle), SQLITE_DBSTATUS_CACHE_WRITE,
                  ctypes.byref(current), ctypes.byref(highwater),
                  1 if reset else 0)
    if rc != 0 or current.value < 0:
        return None
    return int(current.value)


def frame_source_available(conn: sqlite3.Connection) -> bool:
    """Whether ``take_frames`` can read this connection's frame count.

    The probe does not consume the count, so frames written before arming
    (the opener's open-time work) still reach the policy at the next read."""
    return take_frames(conn, reset=False) is not None


# ── the per-process, per-database policy ─────────────────────────────────────


class WalCheckpointPolicy:
    """Checkpoint state for one database in this process (W9)."""

    def __init__(self, label: str, *,
                 clock: "Callable[[], float] | None" = None) -> None:
        self.label = label
        self._clock = clock
        self._lock = threading.Lock()
        self.pending = False
        self.pending_since: "float | None" = None
        self.frames_since_attempt = 0
        self.last_attempt: "float | None" = None
        #: Whether the last attempt was incomplete or failed: until a
        #: complete checkpoint clears it, the size trigger waits
        #: ``TIMER_SECONDS`` from that attempt (W9: nothing retries sooner
        #: than 60 s; 901-RW-002).
        self.last_attempt_unsuccessful = False
        #: When this process first armed the store; the timer counts from it
        #: until the first attempt.
        self.armed_at: "float | None" = None
        self.last_failed_shrink: "float | None" = None
        self.page_size = 4096
        self.attempts = 0
        self.completions = 0
        #: The last attempt's result row, for tests and telemetry.
        self.last_result: "tuple | None" = None

    def now(self) -> float:
        return float((self._clock or CLOCK)())

    @property
    def frame_bytes(self) -> int:
        return self.page_size + WAL_FRAME_HEADER_BYTES

    # -- accounting --------------------------------------------------------

    def note_armed(self) -> None:
        """Start the timer's count the first time the process arms the store."""
        with self._lock:
            if self.armed_at is None:
                self.armed_at = self.now()

    def record_frames(self, frames: int) -> None:
        """Add frames this process appended; a commit sets the pending flag."""
        if frames <= 0:
            return
        with self._lock:
            if not self.pending:
                self.pending = True
                self.pending_since = self.now()
            self.frames_since_attempt += int(frames)

    def size_due(self) -> bool:
        """16 MiB of this process's frames since its last attempt; after an
        unsuccessful attempt, also ``TIMER_SECONDS`` since that attempt, so a
        pinned reader or a competing checkpoint is not retried sooner."""
        with self._lock:
            if not (self.pending and self.frames_since_attempt
                    * self.frame_bytes >= SIZE_TRIGGER_BYTES):
                return False
            return (not self.last_attempt_unsuccessful
                    or self.last_attempt is None
                    or self.now() - self.last_attempt >= TIMER_SECONDS)

    def timer_due(self) -> bool:
        """``TIMER_SECONDS`` since the last attempt, or since the arm when
        there was none, whoever wrote the frames (Q14). A policy that was
        never armed (a test standing for another process) counts from its
        own first commit, as revision 12 did."""
        with self._lock:
            reference = self.last_attempt
            if reference is None:
                reference = (self.armed_at if self.armed_at is not None
                             else self.pending_since)
            return reference is not None and (
                self.now() - reference >= TIMER_SECONDS)

    def checkpoint_completed(self) -> None:
        """Clear the flag: every committed frame reached the database file."""
        with self._lock:
            self.pending = False
            self.pending_since = None
            self.last_attempt_unsuccessful = False
            self.completions += 1

    # -- the attempt -------------------------------------------------------

    def attempt(self, conn: sqlite3.Connection) -> bool:
        """One ``PRAGMA main.wal_checkpoint(PASSIVE)``; True when complete."""
        if conn.in_transaction:
            return False
        with self._lock:
            self.last_attempt = self.now()
            self.frames_since_attempt = 0
            self.attempts += 1
        try:
            row = run_passive_checkpoint(conn)
        except sqlite3.DatabaseError:
            row = None
        self.last_result = row
        if checkpoint_complete(row):
            self.checkpoint_completed()
            return True
        with self._lock:
            self.last_attempt_unsuccessful = True
        return False

    # -- the shrink's rate limit ---------------------------------------------

    def shrink_allowed(self) -> bool:
        with self._lock:
            return (self.last_failed_shrink is None
                    or self.now() - self.last_failed_shrink
                    >= SHRINK_RETRY_SECONDS)

    def record_shrink(self, truncated: bool) -> None:
        if truncated:
            with self._lock:
                self.last_failed_shrink = None
            self.checkpoint_completed()
        else:
            with self._lock:
                self.last_failed_shrink = self.now()


def run_passive_checkpoint(conn: sqlite3.Connection) -> "tuple | None":
    """The checkpoint itself, a seam for tests: ``(busy, log, checkpointed)``.

    ``main.`` matters: an unqualified pragma checkpoints every attached
    database, and the conversations writer attaches cache.db read-only."""
    row = conn.execute("PRAGMA main.wal_checkpoint(PASSIVE)").fetchone()
    return tuple(row) if row is not None else None


def checkpoint_complete(row) -> bool:
    """``busy = 0`` and valid, non-negative counts with ``log = checkpointed``."""
    if row is None or len(row) != 3:
        return False
    try:
        busy, log, checkpointed = (int(value) for value in row)
    except (TypeError, ValueError):
        return False
    return busy == 0 and log >= 0 and checkpointed >= 0 and log == checkpointed


_POLICIES: "dict[str, WalCheckpointPolicy]" = {}
_POLICIES_LOCK = threading.Lock()


def _key(db_path) -> str:
    return os.path.realpath(os.fspath(db_path))


def policy_for_path(db_path) -> WalCheckpointPolicy:
    """This process's policy for the database at ``db_path``."""
    key = _key(db_path)
    with _POLICIES_LOCK:
        policy = _POLICIES.get(key)
        if policy is None:
            policy = WalCheckpointPolicy(key)
            _POLICIES[key] = policy
        return policy


def _main_path(conn: sqlite3.Connection) -> "str | None":
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return path or None
    return None


def policy_for(conn: sqlite3.Connection) -> "WalCheckpointPolicy | None":
    path = _main_path(conn)
    return policy_for_path(path) if path else None


# ── the keeper registry, the timer thread and the finalizer (Q14) ────────────


class _Keeper:
    """One idle connection holding a store's WAL open for this process."""

    __slots__ = ("path", "conn", "identity")

    def __init__(self, path: str, conn: sqlite3.Connection,
                 identity: "tuple[int, int]") -> None:
        self.path = path
        self.conn = conn
        self.identity = identity


_KEEPERS: "dict[str, _Keeper]" = {}
#: The one lock under which a keeper is opened, used, closed or dropped, and
#: under which a store is suspended or resumed.
_KEEPER_LOCK = threading.Lock()
#: Stores whose keeper yielded to a fence (Q15) and has not been reopened.
_SUSPENDED: "set[str]" = set()
#: Every spelling of each store's main path this process armed it under, so
#: the fence files next to it are found whichever directory names it.
_ALIASES: "dict[str, set[str]]" = {}
#: Every store this process armed, with the file identity it last armed
#: (None when the file could not be stat'ed then). The finalizer attempts each
#: one, kept or not (#901 Amendment 19 PR-7).
_ARMED: "dict[str, tuple[int, int] | None]" = {}
#: Whether this runtime can set a keeper's no-checkpoint-on-close control;
#: None until the first keeper probes it (Q15). Remembered per process.
_CLOSE_CONTROL: "bool | None" = None
_TIMER: "threading.Thread | None" = None
_TIMER_STOP = threading.Event()
_ATEXIT_REGISTERED = False
#: Keepers a forked child inherited: never used, never closed, kept alive so
#: their deallocation never runs SQLite's close in the child.
_INHERITED: list = []


def _identity(path: str) -> "tuple[int, int] | None":
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


# -- the fences (Q15) ---------------------------------------------------------


def _fence_at(path: str) -> "str | None":
    """The first live fence next to the store at ``path``, or None.

    Reads the owner records through the repair marker's reader and liveness
    check (`_cctally_db`, imported lazily, as `_cctally_db` imports this
    module lazily in the other direction): a dead or reused-pid owner is not a
    fence. Takes no lock, writes nothing and removes nothing; only the owner
    of a request or marker ever removes it."""
    import _cctally_db

    store = pathlib.Path(path)
    request = _cctally_db._keeper_yield_request_path(store)
    if os.path.lexists(request) and _cctally_db._repair_marker_is_live(
            request)[0]:
        return FENCE_REQUEST
    marker = _cctally_db._repair_marker_path(store)
    if os.path.lexists(marker) and _cctally_db._repair_marker_is_live(
            marker)[0]:
        return FENCE_REPAIR
    if os.path.lexists(_cctally_db._quarantine_pending_path(store)):
        return FENCE_QUARANTINE
    return None


def live_fence(db_path) -> "str | None":
    """The kind of the first live fence on the store, or None (W9, Q15).

    A fence that cannot be verified counts as live: a keeper that yields
    wrongly costs one reopen, while one that stays open wrongly makes a
    requester wait out its deadline and refuse."""
    key = _key(db_path)
    paths = {key, os.fspath(db_path)} | _ALIASES.get(key, set())
    try:
        for path in sorted(paths):
            fence = _fence_at(path)
            if fence is not None:
                return fence
    except Exception:  # noqa: BLE001 — unknown is not unfenced
        return FENCE_UNVERIFIABLE
    return None


def _diagnose(key: str, fence: str) -> None:
    """The one stderr line a keeper yield writes; nothing on the ordinary
    path."""
    try:
        sys.stderr.write(
            f"cctally: {YIELD_DIAGNOSTIC}: {os.path.basename(key)} "
            f"released its checkpoint keeper ({fence})\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — a diagnostic must never fail a sync
        pass


def _yield_locked(key: str, fence: str) -> None:
    """Close the store's keeper silently and suspend the store. Caller holds
    the lock. The store's policy (pending flag, frame accounting, attempt
    times) is kept and its sidecars stay in place: recovery must find the
    family as it was, so no PASSIVE runs and the close does not checkpoint."""
    keeper = _KEEPERS.pop(key, None)
    _SUSPENDED.add(key)
    if keeper is not None:
        _close_quietly(keeper.conn)
        _diagnose(key, fence)
    if not disabled():
        # A suspended store is resumed by the timer too, so a process whose
        # every later iteration is caught up still reopens its keeper.
        _register_atexit()
        _start_timer_locked()


# -- the close control (Q15) --------------------------------------------------


def _apply_close_control(conn: sqlite3.Connection) -> None:
    """Turn SQLite's checkpoint at the last close off and verify it; raise
    when the runtime cannot. Runs before the keeper's first read."""
    import _lib_sqlite_close

    _lib_sqlite_close.set_no_checkpoint_on_close(
        conn, True, purpose="W9 keeper")
    if _lib_sqlite_close.no_checkpoint_on_close_enabled(conn) is False:
        raise sqlite3.NotSupportedError(
            "W9 keeper could not verify SQLite close checkpointing")


def _connect_keeper(path: str) -> sqlite3.Connection:
    """Connect without creating the file and without reading it."""
    uri = pathlib.Path(path).as_uri() + "?mode=rw"
    return sqlite3.connect(uri, uri=True, check_same_thread=False,
                           isolation_level=None)


def _open_keeper(path: str, identity: "tuple[int, int]") -> "_Keeper | None":
    """Connect, apply the close control, read once, hold no transaction.

    No keeper where the close control cannot be set: that is remembered for
    the process, and the never-read connection closes without touching the
    store (no WAL was opened, so there is nothing to checkpoint)."""
    global _CLOSE_CONTROL
    if _CLOSE_CONTROL is False:
        return None
    conn = None
    try:
        conn = _connect_keeper(path)
        try:
            _apply_close_control(conn)
        except Exception:  # noqa: BLE001 — any failure means no control
            _CLOSE_CONTROL = False
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            return None
        _CLOSE_CONTROL = True
        # Fully consumed, so the statement is reset and no read transaction
        # stays open: the keeper pins no frame and blocks no checkpoint.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchall()
        if conn.in_transaction or _identity(path) != identity:
            # Replaced between the stat and the connect: no keeper this time.
            _close_quietly(conn)
            return None
        return _Keeper(path, conn, identity)
    except (sqlite3.Error, OSError, ValueError):
        if conn is not None:
            _close_quietly(conn)
        return None


def _close_quietly(conn: sqlite3.Connection) -> None:
    """Close with SQLite's close-time checkpoint turned off.

    A keeper dropped for a fence, a replaced store, a drain check or a test
    reset must neither copy frames into a file that is no longer the store
    (quarantine evidence) or that recovery must find as it was, nor delete the
    WAL by its name, which may now be the replacement's live WAL: without the
    close-time checkpoint SQLite does neither. Every keeper carries the
    setting from its admission (Q15); it is applied again here because the
    finalizer may have turned it back on."""
    try:
        import _lib_sqlite_close

        _lib_sqlite_close.set_no_checkpoint_on_close(
            conn, True, purpose="W9 keeper")
    except Exception:  # noqa: BLE001 — never fail a close over this
        pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _admit_locked(key: str) -> None:
    """Open the store's keeper, or reopen it on a new file, unless a fence is
    live. Caller holds the lock and, for `arm`, the arming connection is still
    open, so a request published between the check before the open and the
    check after it is never missed. Takes no maintenance or provider lock."""
    fence = live_fence(key)
    if fence is not None:
        _yield_locked(key, fence)
        return
    identity = _identity(key)
    if identity is None:
        return
    current = _KEEPERS.get(key)
    if current is not None and current.identity == identity:
        _SUSPENDED.discard(key)
        return
    if current is not None:
        del _KEEPERS[key]
        _close_quietly(current.conn)
    keeper = _open_keeper(key, identity)
    if keeper is None:
        return
    fence = live_fence(key)
    if fence is not None:
        _close_quietly(keeper.conn)
        _SUSPENDED.add(key)
        _diagnose(key, fence)
        return
    _KEEPERS[key] = keeper
    _SUSPENDED.discard(key)
    _register_atexit()
    _start_timer_locked()


def _ensure_keeper(path: str) -> None:
    """Open this process's keeper for ``path``, reopen it on a new file, or
    resume a suspended store, unless a fence is live (`arm`'s admission)."""
    key = _key(path)
    with _KEEPER_LOCK:
        aliases = _ALIASES.setdefault(key, set())
        aliases.add(os.fspath(path))
        _ARMED[key] = _identity(key)
        _admit_locked(key)
        # The finalizer attempts every armed store, kept or not (PR-7).
        _register_atexit()


def maintenance_lock_path(db_path) -> str:
    """The store's maintenance flock: ``_cctally_core`` names it
    ``APP_DIR / "<store>.maintenance.lock"`` for both kept stores
    (``CACHE_LOCK_MAINTENANCE_PATH``, ``CONVERSATIONS_LOCK_MAINTENANCE_PATH``),
    so it is derived from the store's own path."""
    return f"{_key(db_path)}.maintenance.lock"


def _resume_from_timer_locked(key: str) -> None:
    """Reopen a suspended store's keeper from the timer (901-SR-036).

    The store's maintenance lock is taken shared and non-blocking for the
    admission only, never for the keeper's life: while a requester (or this
    process's own maintenance on another descriptor) holds it exclusively,
    the store stays suspended until the next round. Under it the fences and
    the file identity are checked again before SQLite is opened, and the
    fences once more after, as at any admission. Caller holds the lock."""
    if live_fence(key) is not None or _identity(key) is None:
        return
    try:
        lock_fh = open(maintenance_lock_path(key), "a+")
    except OSError:
        return
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            if key in _KEEPERS:
                _SUSPENDED.discard(key)
                return
            _admit_locked(key)
        finally:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        lock_fh.close()


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    if not _ATEXIT_REGISTERED:
        atexit.register(finalize)
        _ATEXIT_REGISTERED = True


def _start_timer_locked() -> None:
    """Start the process's timer thread unless it runs. Caller holds the lock."""
    global _TIMER, _TIMER_STOP
    if (_TIMER is not None and _TIMER.is_alive()
            and not _TIMER_STOP.is_set()):
        return
    _TIMER_STOP = threading.Event()
    _TIMER = threading.Thread(target=_timer_main, args=(_TIMER_STOP,),
                              name=TIMER_THREAD_NAME, daemon=True)
    _TIMER.start()


def _timer_main(stop: threading.Event) -> None:
    while not stop.wait(max(0.001, float(TIMER_INTERVAL_SECONDS))):
        try:
            _timer_round(stop)
        except Exception:  # noqa: BLE001 — the thread must outlive any error
            pass


def _timer_round(stop: "threading.Event | None" = None) -> None:
    """One round: the fence check on every kept store before the due check,
    resumption of suspended stores no longer fenced, then the timed attempt
    through each kept store's keeper."""
    with _KEEPER_LOCK:
        if stop is not None and stop.is_set():
            return
        for key in list(_KEEPERS):
            fence = live_fence(key)
            if fence is not None:
                _yield_locked(key, fence)
        for key in sorted(_SUSPENDED):
            _resume_from_timer_locked(key)
        for key, keeper in list(_KEEPERS.items()):
            policy = policy_for_path(key)
            if not policy.timer_due():
                continue
            if _identity(key) != keeper.identity:
                # The store was replaced underneath this process; the next arm
                # opens a keeper on the new file.
                del _KEEPERS[key]
                _close_quietly(keeper.conn)
                continue
            try:
                policy.attempt(keeper.conn)
            except sqlite3.Error:
                pass


def finalize() -> None:
    """At orderly exit: one PASSIVE per armed store, then close the keepers.

    Idempotent. A keeper whose store is unchanged and unfenced gets its normal
    close behaviour back, so when it is the store's last connection SQLite's
    close-time checkpoint drains the WAL; one whose store was replaced or is
    fenced closes without touching either file, and a fenced store gets no
    PASSIVE either.

    #901 Amendment 19 PR-7: a store this process armed whose keeper is gone —
    released for a drain check, or yielded to a fence that has since cleared —
    is attempted too, through a short-lived connection with the same rules
    (`_finalize_unkept_locked`). Only the kept stores were attempted before,
    so such a store left its frames for whoever opened it next."""
    _TIMER_STOP.set()
    with _KEEPER_LOCK:
        keepers = list(_KEEPERS.items())
        _KEEPERS.clear()
        _SUSPENDED.clear()
        kept = {key for key, _keeper in keepers}
        unkept = sorted(key for key in _ARMED if key not in kept)
        armed_identity = dict(_ARMED)
        _ARMED.clear()
        for key in unkept:
            _finalize_unkept_locked(key, armed_identity.get(key))
        for key, keeper in keepers:
            same = _identity(key) == keeper.identity
            fence = live_fence(key) if same else None
            if same and fence is None:
                if not disabled():
                    try:
                        policy_for_path(key).attempt(keeper.conn)
                    except sqlite3.Error:
                        pass
                try:
                    import _lib_sqlite_close

                    _lib_sqlite_close.set_no_checkpoint_on_close(
                        keeper.conn, False, purpose="W9 keeper")
                except Exception:  # noqa: BLE001 — keep it silent instead
                    _close_quietly(keeper.conn)
                    continue
                try:
                    keeper.conn.close()
                except Exception:  # noqa: BLE001
                    pass
            else:
                _close_quietly(keeper.conn)
                if fence is not None:
                    _diagnose(key, fence)


def _finalize_unkept_locked(key: str,
                            armed_identity: "tuple[int, int] | None") -> None:
    """The finalizer's PASSIVE for an armed store with no keeper (PR-7).

    The kept-store rules, on a connection opened for the purpose: no attempt
    when the store is gone, was replaced since it was armed, or is fenced.
    As at a keeper admission, the identity and the fences are checked before
    the connection opens and again after, before any read (901-RW-003), and
    the connection carries no-checkpoint-on-close until the store is found
    unchanged and unfenced once more after the PASSIVE: a fence or a
    replacement that lands meanwhile gets a silent close, which touches
    neither file. Where that setting cannot be established and verified
    (OV-4), it makes no attempt at all, exactly as keeper admission refuses.
    Otherwise it closes normally, so when it is the store's last connection
    SQLite's close-time checkpoint drains the WAL.

    Like the kept-store exit path, and unlike a timer round's admission, it
    takes no maintenance lock: the finalizer may run while the process still
    holds its writer and provider locks (a detached worker's explicit call
    before `os._exit`), and the lock-order law forbids taking the earlier
    maintenance lock after them, even non-blocking (W9, Q15)."""
    if disabled():
        return
    identity = _identity(key)
    if identity is None or (armed_identity is not None
                            and identity != armed_identity):
        return
    if live_fence(key) is not None or _CLOSE_CONTROL is False:
        return
    try:
        conn = _connect_keeper(key)
    except (sqlite3.Error, OSError, ValueError):
        return
    try:
        _apply_close_control(conn)
    except Exception:  # noqa: BLE001 — any failure means no control
        # As at keeper admission: without the control a fence landing
        # during the attempt could not be honoured at the close, so there is
        # no attempt at all. Never read, the connection closes untouched.
        _close_quietly(conn)
        return
    # Never read yet: a connection that opened no WAL closes untouched.
    if _identity(key) != identity or live_fence(key) is not None:
        _close_quietly(conn)
        return
    try:
        policy_for_path(key).attempt(conn)
    except sqlite3.Error:
        pass
    if _identity(key) != identity or live_fence(key) is not None:
        _close_quietly(conn)
        return
    try:
        import _lib_sqlite_close

        _lib_sqlite_close.set_no_checkpoint_on_close(
            conn, False, purpose="W9 keeper")
    except Exception:  # noqa: BLE001 — keep it silent instead
        _close_quietly(conn)
        return
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def release(db_path) -> None:
    """Drop this process's keeper for ``db_path`` without a checkpoint.

    Called by a requester before it waits for the family (W9, Q15) and before
    every drain check (`_cctally_db._db_family_open_pids`): a keeper exists
    only to defer checkpoints and must never be the open handle that refuses
    this process's own maintenance or store recovery. The store is suspended,
    so the next arm, or a timer round once no fence is live, reopens it."""
    key = _key(db_path)
    if key not in _KEEPERS:
        return
    with _KEEPER_LOCK:
        keeper = _KEEPERS.pop(key, None)
        if keeper is not None:
            _close_quietly(keeper.conn)
            _SUSPENDED.add(key)


def kept_stores() -> "dict[str, tuple[int, int]]":
    """The stores this process keeps, with the file identity of each keeper."""
    with _KEEPER_LOCK:
        return {key: keeper.identity for key, keeper in _KEEPERS.items()}


def suspended_stores() -> "frozenset[str]":
    """The stores whose keeper yielded and has not been reopened (Q15)."""
    with _KEEPER_LOCK:
        return frozenset(_SUSPENDED)


def timer_thread() -> "threading.Thread | None":
    """This process's timer thread, or None when it never started one."""
    return _TIMER


def reset_policies() -> None:
    """Forget this process's W9 state (tests: a fresh process).

    The policies, and with them every keeper (closed without a checkpoint),
    the suspended stores, the close-control probe and the timer thread."""
    global _TIMER, _CLOSE_CONTROL
    _TIMER_STOP.set()
    thread = _TIMER
    with _KEEPER_LOCK:
        keepers = list(_KEEPERS.values())
        _KEEPERS.clear()
        _SUSPENDED.clear()
        _ALIASES.clear()
        _ARMED.clear()
        _CLOSE_CONTROL = None
        for keeper in keepers:
            _close_quietly(keeper.conn)
        _TIMER = None
    if (thread is not None and thread is not threading.current_thread()
            and thread.is_alive()):
        thread.join(timeout=5.0)
    with _POLICIES_LOCK:
        _POLICIES.clear()


def _after_fork_in_child() -> None:
    """A forked child starts with no keeper, no timer thread and fresh locks.

    The inherited keepers are neither used nor closed: they are parked in
    ``_INHERITED`` (with an extra reference, so the child's teardown never
    deallocates them), because closing a copy of the parent's connection in
    the child could run SQLite's close path against the parent's files."""
    global _KEEPERS, _KEEPER_LOCK, _TIMER, _TIMER_STOP, _POLICIES
    global _POLICIES_LOCK, _SUSPENDED, _ALIASES, _ARMED
    inherited = [keeper.conn for keeper in _KEEPERS.values()]
    _INHERITED.extend(inherited)
    try:
        import ctypes

        for conn in inherited:
            ctypes.pythonapi.Py_IncRef(ctypes.py_object(conn))
    except Exception:  # noqa: BLE001 — the list reference still holds them
        pass
    _KEEPERS = {}
    _KEEPER_LOCK = threading.Lock()
    _SUSPENDED = set()
    _ALIASES = {}
    _ARMED = {}
    _TIMER = None
    _TIMER_STOP = threading.Event()
    _POLICIES = {}
    _POLICIES_LOCK = threading.Lock()


def _before_fork() -> None:
    """PR-6: no keeper connection crosses a fork.

    A child forked without `exec` (the transcript rebuild worker, a detached
    hook or provider worker) that opens a store the parent keeps would share
    SQLite's per-process inode lock bookkeeping with the parent's keeper while
    the fcntl locks behind it are not inherited, so it could believe it holds
    locks the kernel never granted it. Every keeper is released first, as a
    requester releases it (no checkpoint, the store suspended and reopened by
    the next arm or timer round), and the keeper lock is held across the fork
    so nothing is reopened before it. `subprocess` without `preexec_fn` runs
    no fork hook, and the steady-state dashboard does not fork."""
    _KEEPER_LOCK.acquire()
    for key, keeper in list(_KEEPERS.items()):
        _close_quietly(keeper.conn)
        _SUSPENDED.add(key)
    _KEEPERS.clear()


def _after_fork_in_parent() -> None:
    _KEEPER_LOCK.release()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_before_fork,
                        after_in_parent=_after_fork_in_parent,
                        after_in_child=_after_fork_in_child)


# ── the writer's three calls ─────────────────────────────────────────────────


def armed(conn: sqlite3.Connection) -> bool:
    """Whether ``conn`` is a writer this module owns checkpoints for."""
    if disabled():
        return False
    try:
        return int(conn.execute(
            "PRAGMA wal_autocheckpoint").fetchone()[0]) == 0
    except sqlite3.Error:
        return False


def arm(conn: sqlite3.Connection) -> bool:
    """Make ``conn`` a writer-owned-checkpoint sync writer (W9).

    Called by the sync functions after the opener's open-time work; outside
    any transaction, holding the writer and provider flocks. Opens (or, on a
    replaced file, reopens, or after a yield resumes) the process's keeper for
    the store unless a fence is live, taking no maintenance or provider lock
    (Q15), and sets ``wal_autocheckpoint = 0`` only when the frame count can
    be read on this runtime and the database is in WAL mode; otherwise the
    connection keeps SQLite's default (the keeper still applies). Idempotent.
    Returns whether the connection is armed.
    """
    try:
        if disabled() or conn.in_transaction:
            return False
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode != "wal":
            return False
        path = _main_path(conn)
        if not path:
            return False
        policy = policy_for_path(path)
        policy.note_armed()
        _ensure_keeper(path)
        if not frame_source_available(conn):
            return False
        policy.page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        return True
    except sqlite3.Error:
        return False


def after_commit(conn: sqlite3.Connection, *,
                 policy: "WalCheckpointPolicy | None" = None) -> None:
    """Account for this connection's new frames; size-trigger a checkpoint.

    ``policy`` defaults to this process's policy for the database; a test
    passes its own to stand for another process."""
    try:
        if not armed(conn):
            return
        policy = policy or policy_for(conn)
        frames = take_frames(conn)
        if policy is None or frames is None:
            return
        policy.record_frames(frames)
        if policy.size_due():
            policy.attempt(conn)
    except sqlite3.Error:
        pass  # checkpoint policy must never fail a sync


def at_boundary(conn: sqlite3.Connection, *,
                policy: "WalCheckpointPolicy | None" = None) -> None:
    """End of a sync: account for new frames; timer-trigger a checkpoint."""
    try:
        if not armed(conn) or conn.in_transaction:
            return
        policy = policy or policy_for(conn)
        frames = take_frames(conn)
        if policy is None or frames is None:
            return
        policy.record_frames(frames)
        if policy.size_due() or policy.timer_due():
            policy.attempt(conn)
    except sqlite3.Error:
        pass  # checkpoint policy must never fail a sync
