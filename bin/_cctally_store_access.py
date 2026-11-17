"""#787 / #861 store authority services (spec 5.1, 5.4–5.8, 5.10, 5.13).

Thin typed services over the existing openers and mutation functions:

- `QueryService`: query sessions over ``mode=ro`` connections, governed by a
  composed authorizer, one snapshot transaction and a frozen identity vector.
- `WriterService.request_writer`: schema-current-only connections for the
  restricted request entry points (spec 5.9).
- `StartupService.ensure`: the existing full opener at the caller's authority.

No authority state lives here: it lives in `_cctally_core`, which
`tests/conftest.py` never reloads, so reloading this module mid-scope keeps
the caller's authority.

A query never creates, migrates, recovers, launches or ingests (spec 5.4):

- maintenance lock files are opened ``O_RDONLY`` and never created; a missing
  lock means ``absent``;
- no ``mkdir``, ``chmod``, marker, log, launch record or config write;
- cache.db / conversations.db may gain ``-shm`` and a 0-byte zero-frame
  ``-wal``; stats.db gains no sidecar at all (a WAL-mode header, any WAL/SHM,
  a ``-journal`` or a recovery artifact refuses the stats session);
- the data directory must be 0700 and owned by the user, else
  ``unavailable``.
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import errno
import fcntl
import hashlib
import importlib
import os
import pathlib
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping

import _cctally_core

_STORES = ("stats", "cache", "conversations")
_ATTACH_ALIAS = {"cache": "cache_db"}
_POLICIES = ("interactive", "command", "diagnostic")
#: The temp store every query connection selects (MEMORY: a query writes no
#: temporary file). A module constant, like `_cctally_store.WRITER_TEMP_STORE`,
#: so the #902 plan guard can judge the same statements under a FILE control.
QUERY_TEMP_STORE = "MEMORY"
_POLL_S = 0.02
_INTERACTIVE_ATTEMPTS = 3
_INTERACTIVE_BUSY_CAP_S = 0.25
#: Reasons a diagnostic session admits read-only, handing the readiness to its
#: caller (spec 5.12 "Schema admission (frozen)").
_DIAGNOSTIC_ADMITTED = frozenset({
    "schema_behind", "schema_ahead", "legacy_bridge_pending",
    "recovery_pending",
})
#: The schema-mismatch readiness reasons a query caller may tolerate for one
#: plain SELECT of tables whose shape it checks itself (`tolerate`): a reader
#: base ran through a raw ``mode=ro`` open whatever the schema (R8).
SCHEMA_MISMATCH_REASONS = frozenset({
    "schema_behind", "schema_ahead", "legacy_bridge_pending",
})
#: PRAGMAs that change persistent state when given a value (spec 5.2). Their
#: read forms (``PRAGMA user_version``) stay allowed: the snapshot protocol
#: reads them.
_DENIED_PRAGMA_SETTERS = frozenset({
    "journal_mode", "user_version", "auto_vacuum", "application_id",
    "page_size", "schema_version", "locking_mode",
})
#: PRAGMAs that act whenever they run.
_DENIED_PRAGMA_ACTIONS = frozenset({
    "wal_checkpoint", "incremental_vacuum", "optimize",
})
_WRITE_ACTIONS = frozenset({
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_VTABLE,
})
#: Statements the authorizer never sees (SQLite reports no action for plain
#: VACUUM or REINDEX); ``mode=ro`` refuses their main-file effect, and the
#: connection refuses them outright so a TEMP-only form is not run either.
_REFUSED_LEADING_KEYWORDS = ("VACUUM", "REINDEX")


class _QueryConnection(sqlite3.Connection):
    """Every connection a query session creates (plan D2).

    The Task C recorder checks ``isinstance(conn, _QueryConnection)``; the
    ``session_tag`` names the session in latch records."""

    session_tag: str = ""
    #: The session's transaction is its read snapshot (5.13 step 5): the
    #: connection is ``mode=ro`` under a permanent authorizer, so it can never
    #: hold a write. A kernel that refuses a foreign transaction (it cannot
    #: tell a read snapshot from a write) may borrow this one, and must leave
    #: its end to the session.
    query_session_snapshot: bool = True

    def _refuse_if_maintenance(self, sql) -> None:
        head = str(sql).lstrip().upper()
        for keyword in _REFUSED_LEADING_KEYWORDS:
            if head.startswith(keyword):
                _cctally_core.record_authorizer_denial(self.session_tag,
                                                       keyword)
                raise sqlite3.DatabaseError(
                    f"not authorized: {keyword} in a query session")

    def execute(self, sql, *args, **kwargs):
        self._refuse_if_maintenance(sql)
        return super().execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self._refuse_if_maintenance(sql)
        return super().executemany(sql, *args, **kwargs)

    def executescript(self, sql_script):
        # `executescript` COMMITs first, which would end the session's one
        # snapshot transaction (spec 5.13 step 8).
        _cctally_core.record_authorizer_denial(self.session_tag,
                                               "executescript")
        raise sqlite3.DatabaseError(
            "not authorized: executescript in a query session")


# ── identity (spec 5.13) ─────────────────────────────────────────────────

#: The fields compared per store. A field a store does not carry (stats has
#: no incarnation) is not compared; a compared field that is None makes two
#: identities unequal, so a missing leg disables reuse instead of matching.
_COMPARED_FIELDS = {
    "stats": ("file", "user_version", "protocol", "content_revision"),
    "cache": ("file", "user_version", "incarnation", "migration_state"),
    "conversations": ("file", "user_version", "incarnation",
                      "migration_state", "rollup_epoch"),
}


@dataclasses.dataclass(frozen=True)
class StoreIdentity:
    file: "tuple[int, int] | None"
    user_version: "int | None"
    incarnation: "str | None"
    protocol: "str | None"
    content_revision: "tuple[str, str] | None"
    migration_state: "str | None"
    rollup_epoch: "int | str | None"
    store: str = "unknown"

    def matches(self, other: "StoreIdentity") -> bool:
        if not isinstance(other, StoreIdentity) or self.store != other.store:
            return False
        fields = _COMPARED_FIELDS.get(self.store, ())
        if not fields:
            return False
        for name in fields:
            a, b = getattr(self, name), getattr(other, name)
            if a is None or b is None or a != b:
                return False
        return True


@dataclasses.dataclass(frozen=True, eq=False)
class SessionIdentity:
    """role -> StoreIdentity. Equal only when every role matches and no
    compared field is None (spec 5.13 "Missing state")."""

    stores: Mapping[str, StoreIdentity]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SessionIdentity):
            return NotImplemented
        if set(self.stores) != set(other.stores) or not self.stores:
            return False
        return all(self.stores[r].matches(other.stores[r])
                   for r in self.stores)

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.stores)))


# ── store descriptions ───────────────────────────────────────────────────


def _store_db_path(store: str) -> pathlib.Path:
    return pathlib.Path({
        "stats": _cctally_core.DB_PATH,
        "cache": _cctally_core.CACHE_DB_PATH,
        "conversations": _cctally_core.CONVERSATIONS_DB_PATH,
    }[store])


def _store_lock_path(store: str) -> pathlib.Path:
    return pathlib.Path({
        "stats": _cctally_core.STATS_LOCK_MAINTENANCE_PATH,
        "cache": _cctally_core.CACHE_LOCK_MAINTENANCE_PATH,
        "conversations": _cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH,
    }[store])


def _db():
    return importlib.import_module("_cctally_db")


def _store_mod():
    return importlib.import_module("_cctally_store")


def _cache_mod():
    return importlib.import_module("_cctally_cache")


def _unavailable(store: str, reason: str, message: str = "", *,
                 family: "str | None" = None, sqlite_text: bool = False):
    """The typed refusal for `store`. A conversations session (``family``)
    raises through the `ConversationReaderUnavailable` family so its existing
    handlers see it, with ``store="cache"`` for an attached-cache state.

    The message is the internal detail. It is marked ``query_refusal`` so a
    human-readable surface renders it through
    `_cctally_core.store_refusal_text` (Q-W1); ``sqlite_text`` marks a
    message that is SQLite's own text, which keeps rendering as is."""
    text = message or f"{store}.db is not readable ({reason})"
    if (family or store) == "conversations":
        cache = _cache_mod()
        cls = {
            "maintenance": cache.MaintenanceInProgress,
            "schema_behind": cache.SchemaBehind,
            "schema_ahead": cache.SchemaAhead,
            "legacy_bridge_pending": cache.LegacyBridgePending,
        }.get(reason, cache.ConversationReaderUnavailable)
        exc = cls(text, store=store, reason=reason)
    else:
        exc = _cctally_core.StoreUnavailable(text, store=store, reason=reason)
    exc.query_refusal = True
    exc.sqlite_text = sqlite_text
    return exc


def _private_data_dir_ok() -> bool:
    """The 5.4 prerequisite: a reader-created 0644 sidecar is acceptable only
    inside a data directory an owner established as private: 0700 and
    user-owned, or, where the filesystem did not take the mode, matching the
    owner's access record (AD20, `_cctally_core.data_dir_access_ok`)."""
    return _cctally_core.data_dir_access_ok()


# ── admission (spec 5.3) ─────────────────────────────────────────────────


class _Admission:
    """One store's shared maintenance hold on an ``O_RDONLY`` descriptor."""

    def __init__(self, store: str, fd: int):
        self.store = store
        self.fd = fd

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None


def _admit(store: str, policy: str) -> _Admission:
    """Take `store`'s maintenance lock SHARED, non-blocking, within the
    running admission's effective deadline (5.3, AD29: the read unit's
    remaining admission budget or a tighter caller deadline, fixed at the
    admission's entry). Its time is debited once, by the enclosing
    `admission_scope`, never here. Never creates the lock file."""
    lock_path = _store_lock_path(store)
    try:
        fd = os.open(lock_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise _unavailable(store, "absent",
                           f"{store}.db maintenance lock is absent")
    except OSError as exc:
        raise _unavailable(store, "unavailable",
                           f"{store}.db maintenance lock unreadable ({exc})")
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:
        # No owner can take a lock that is not a regular file (they open it
        # for append), so a shared hold on it excludes nothing.
        os.close(fd)
        raise _unavailable(store, "unavailable",
                           f"{store}.db maintenance lock is not a regular file")
    started = time.monotonic()
    attempts = 0
    admitted = False
    try:
        while True:
            attempts += 1
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                admitted = True
                return _Admission(store, fd)
            except (BlockingIOError, InterruptedError):
                # Contention, or a signal: retried within the deadline, as
                # `admission_flock` retries them.
                pass
            except OSError as exc:
                # Any other OS error on the lock (EIO, ENOLCK, ...) is no
                # contention: the typed refusal, so every surface renders
                # the existing sentence (Q-W1) instead of a raw OSError.
                raise _unavailable(
                    store, "unavailable",
                    f"{store}.db maintenance lock could not be acquired "
                    f"({exc})") from exc
            remaining = _cctally_core.remaining_deadline()
            if policy == "interactive" and attempts >= _INTERACTIVE_ATTEMPTS:
                break
            if remaining is None or remaining <= 0:
                break
            time.sleep(min(_POLL_S, remaining))
        raise _unavailable(store, "maintenance",
                           f"{store}.db maintenance is in progress")
    finally:
        if not admitted:
            try:
                os.close(fd)
            except OSError:
                pass
        if policy == "diagnostic":
            allowance = _cctally_core._STATS_DIAGNOSTIC_WAIT_ALLOWANCE.get()
            if allowance is not None:
                allowance.remaining_s = max(
                    0.0, allowance.remaining_s - (time.monotonic() - started))


# ── stats header (5.6, R10) ──────────────────────────────────────────────

_HEADER_CACHE: "dict[tuple, str | None]" = {}
#: AD30 (revision 29): the cached verdicts this process probed itself, by
#: stat identity, so a one-shot diagnosis worker returns them with its
#: result and its parent adds them to its own cache. A verdict seeded from a
#: handoff is never listed here. Bounded and cleared with the cache.
_HEADER_PROBED: "dict[tuple, str]" = {}
_HEADER_CACHE_LOCK = threading.Lock()
_HEADER_CACHE_MAX = 64
_HEADER_PROBE_TIMEOUT_S = 2.0
_USR_BIN_HEAD = "/usr/bin/head"
#: The interpreter fallback's program (AD19): reads exactly 20 bytes.
_HEADER_PROBE_SCRIPT = ("import sys;sys.stdout.buffer.write("
                        "open(sys.argv[1],'rb').read(20))")
#: The probe's program choice, decided once per process (AD19): True for
#: /usr/bin/head, False for the interpreter fallback, None until decided.
_HEAD_ARGV0: "bool | None" = None


def _usr_bin_head_usable() -> bool:
    """Whether /usr/bin/head is an executable regular file (AD19)."""
    try:
        st = os.stat(_USR_BIN_HEAD)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(_USR_BIN_HEAD, os.X_OK)


def _header_probe_argv(path) -> "list[str]":
    """Exactly one of the two AD19 argv shapes; PATH is never consulted."""
    global _HEAD_ARGV0
    if _HEAD_ARGV0 is None:
        _HEAD_ARGV0 = bool(_usr_bin_head_usable())
    if _HEAD_ARGV0:
        return [_USR_BIN_HEAD, "-c", "20", str(path)]
    return [sys.executable, "-I", "-S", "-B", "-c", _HEADER_PROBE_SCRIPT,
            str(path)]


#: AD19 (spec revision 13): the cleanup reserve inside the probe's deadline.
_HEADER_PROBE_CLEANUP_RESERVE_S = 0.05

#: AD19 (revision 14): admission overruns recorded for the session being
#: built. A list while `_session` or the metadata reader admits, else None;
#: copied onto the session (or the refusal) as ``admission_overruns``.
_ADMISSION_OVERRUNS: "contextvars.ContextVar[list | None]" = (
    contextvars.ContextVar("cctally_admission_overruns", default=None))


def _record_admission_overrun(what: str, seconds: float) -> None:
    """Record that `what` held the shared admission `seconds` past its
    deadline (AD19: a child that survived SIGKILL is waited for to exit)."""
    sink = _ADMISSION_OVERRUNS.get()
    if sink is not None:
        sink.append((what, seconds))


def _header_probe_deadline() -> float:
    """One absolute deadline for the whole probe (AD19, revision 13):
    ``min(now + 2.0 s, the running admission's effective deadline)`` (5.3,
    AD29), computed before launch and carried through launch, communication
    and cleanup."""
    now = time.monotonic()
    deadline = now + _HEADER_PROBE_TIMEOUT_S
    remaining = _cctally_core.remaining_deadline()
    if remaining is not None:
        deadline = min(deadline, now + remaining)
    return deadline


def _reap_header_probe(proc, deadline: float) -> None:
    """Kill `proc` and wait for it within the actual remaining deadline, never
    a fresh allowance. The probe never returns with a live child (AD19,
    revision 14): a child that survives SIGKILL past the deadline is waited
    for until it exits, still under the caller's shared admission, and the
    overrun is recorded on the session."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.wait()
        _record_admission_overrun("stats_header_probe",
                                  max(0.0, time.monotonic() - deadline))
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass


def _run_header_probe(path) -> "bytes | None":
    """The header bytes from the AD19 child, or None (fail closed)."""
    deadline = _header_probe_deadline()
    reserve = _HEADER_PROBE_CLEANUP_RESERVE_S
    if deadline - time.monotonic() < reserve:
        return None                 # refuse without spawning
    try:
        proc = subprocess.Popen(
            _header_probe_argv(path), shell=False, env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    try:
        # Recomputed after launch, so process creation is charged.
        out, _err = proc.communicate(
            timeout=max(0.0, deadline - reserve - time.monotonic()))
    except subprocess.TimeoutExpired:
        _reap_header_probe(proc, deadline)
        return None
    except BaseException:
        _reap_header_probe(proc, deadline)
        raise
    if proc.returncode != 0:
        return None
    return out or b""


#: The verdicts `_stats_header_kind` caches (None is never cached).
_HEADER_VERDICTS = frozenset({"wal", "rollback", "invalid"})


def _header_stat_key(st) -> tuple:
    """The stat identity the AD19 cache is keyed on."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def header_verdict_handoff(paths) -> "tuple[tuple[tuple, str], ...]":
    """This process's cached AD19 verdicts for PATHS, for a one-shot worker
    to seed its own cache from (AD30): one ``(stat identity, verdict)``
    entry per path whose CURRENT stat identity has a cached verdict. It
    never probes, so a path without a verdict is simply absent."""
    entries = []
    for raw in paths:
        try:
            st = os.stat(raw)
        except OSError:
            continue
        key = _header_stat_key(st)
        with _HEADER_CACHE_LOCK:
            verdict = _HEADER_CACHE.get(key)
        if verdict in _HEADER_VERDICTS:
            entries.append((key, verdict))
    return tuple(entries)


def probed_header_verdicts() -> "tuple[tuple[tuple, str], ...]":
    """The ``(stat identity, verdict)`` entries this process learned (AD30):
    the ones it probed itself (revision 29) and, revision 31 (R30-3), the
    ones it adopted from a worker or forked provider it started, each under
    its original key. A one-shot worker returns them with its result so its
    parent adopts them (`seed_header_verdicts`), and a verdict crosses any
    number of hops: a later request then reuses a probe any earlier
    descendant paid for. Never an entry a parent handed this process."""
    with _HEADER_CACHE_LOCK:
        return tuple(_HEADER_PROBED.items())


def seed_header_verdicts(entries, *, learned: bool = False) -> None:
    """Seed this process's AD19 cache from a parent's handoff, before its
    first read, or adopt the verdicts a descendant returned (AD30; LEARNED,
    revision 31: they are then also this process's to return). Each entry
    stays keyed by the stat identity it was probed for, so a store whose
    identity moved since misses and re-probes exactly as an empty cache
    would, and a verdict never serves another identity. A malformed entry
    is ignored."""
    for entry in entries or ():
        try:
            key, verdict = entry
        except (TypeError, ValueError):
            continue
        if (verdict not in _HEADER_VERDICTS or not isinstance(key, tuple)
                or len(key) != 5
                or not all(type(part) is int for part in key)):
            continue
        with _HEADER_CACHE_LOCK:
            if len(_HEADER_CACHE) >= _HEADER_CACHE_MAX:
                _HEADER_CACHE.clear()
            _HEADER_CACHE[key] = verdict
            if learned:
                if len(_HEADER_PROBED) >= _HEADER_CACHE_MAX:
                    _HEADER_PROBED.clear()
                _HEADER_PROBED[key] = verdict


def _stats_header_kind(path: pathlib.Path) -> "str | None":
    """Whether stats.db's main header declares WAL, read WITHOUT a descriptor
    in this process (spec 5.6, AD19).

    Closing any descriptor on a file releases every POSIX lock this process
    holds on it, which would drop another connection's SQLite lock; and no
    SQLite open mode reports the header's version bytes without materializing
    sidecars (``immutable=1`` reports ``delete``; ``nolock=1`` closes its
    descriptor immediately). So a short read-only child (``/usr/bin/head``,
    or the interpreter fallback when that is not an executable regular file)
    reads the 20 header bytes with no shell, an empty environment, DEVNULL
    stdin/stderr and ``close_fds``, under one absolute deadline (see
    `_run_header_probe`); the result is cached per (dev, ino, size,
    mtime_ns, ctime_ns). Returns ``"wal"``, ``"rollback"`` (an empty file
    included), ``"invalid"`` (not an SQLite header) or None when it cannot be
    read (a spawn error, a timeout, a non-zero exit or short output: fail
    closed as ``unavailable``)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = _header_stat_key(st)
    with _HEADER_CACHE_LOCK:
        if key in _HEADER_CACHE:
            return _HEADER_CACHE[key]
    out = _run_header_probe(path)
    if out is None:
        return None
    if not st.st_size:
        result = "rollback"
    elif len(out) < min(20, st.st_size):
        return None                 # short read: fail closed
    elif len(out) < 20 or out[:16] != b"SQLite format 3\x00":
        result = "invalid"
    else:
        result = "wal" if (out[18] == 2 or out[19] == 2) else "rollback"
    with _HEADER_CACHE_LOCK:
        if len(_HEADER_CACHE) >= _HEADER_CACHE_MAX:
            _HEADER_CACHE.clear()
        if len(_HEADER_PROBED) >= _HEADER_CACHE_MAX:
            _HEADER_PROBED.clear()
        _HEADER_CACHE[key] = result
        _HEADER_PROBED[key] = result
    return result


# ── pre-connect checks per store ─────────────────────────────────────────


def _file_identity(path) -> "tuple[int, int] | None":
    import _lib_snapshot_cache
    return _lib_snapshot_cache.codex_cache_file_identity(path)


#: The stats `-journal` refusal: the precheck's, and AD23's for a journal
#: that turned hot after it (the same reason and message).
_STATS_JOURNAL_REFUSAL = "stats.db has a -journal side file"
#: SQLite's extended code for a read-only handle that found a hot journal it
#: may not roll back (Python 3.11+ exposes it; the value is SQLite's).
_SQLITE_READONLY_ROLLBACK = getattr(sqlite3, "SQLITE_READONLY_ROLLBACK", 776)


def _hot_journal_refusal(primary: str, exc: sqlite3.DatabaseError) -> bool:
    """AD23: whether `exc` is the ``mode=ro`` stats connection meeting a
    `-journal` that turned hot after the precheck. A read-only handle never
    rolls it back; SQLite reports it as a read-only rollback error."""
    if primary != "stats":
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code == _SQLITE_READONLY_ROLLBACK
    return ("readonly database" in str(exc).lower()
            and pathlib.Path(f"{_store_db_path('stats')}-journal").exists())


def _stats_journal_cleared(path: pathlib.Path, policy: str) -> bool:
    """Whether stats.db has no `-journal`, waiting within the operation
    deadline (bounded like the admission wait) for one to clear.

    stats.db is `journal_mode=DELETE`, so every writer transaction holds a
    `-journal` until it commits: a journal seen here is usually a live
    writer's, gone within milliseconds. Only one that outlives the wait is
    refused (5.4). A journal that appears after this check is the writer's
    concern and SQLite's: the `mode=ro` connection waits on its lock, and
    a hot one left by a crash is never rolled back by a read-only handle."""
    journal = pathlib.Path(f"{path}-journal")
    started = time.monotonic()
    attempts = 0
    try:
        while True:
            attempts += 1
            if not journal.exists():
                return True
            remaining = _cctally_core.remaining_deadline()
            if policy == "interactive" and attempts >= _INTERACTIVE_ATTEMPTS:
                return False
            if remaining is None or remaining <= 0:
                return False
            time.sleep(min(_POLL_S, remaining))
    finally:
        if policy == "diagnostic":
            allowance = _cctally_core._STATS_DIAGNOSTIC_WAIT_ALLOWANCE.get()
            if allowance is not None:
                allowance.remaining_s = max(
                    0.0, allowance.remaining_s - (time.monotonic() - started))


def _cache_repair_owner_alive(db, marker: pathlib.Path) -> bool:
    """R32-1: whether ``marker``'s owner record decodes and names a running
    PID (``os.kill(pid, 0)``). It never reads the owner's process-start
    identity: on macOS that read spawns ``ps``, and a query scope creates no
    process (5.12). A reused PID therefore reads as live here; only the
    owner open, with the full identity and lease check, recovers a marker."""
    owner, _record = db._read_repair_owner_record(marker)
    if owner is None:
        return False
    return db._pid_is_alive(int(owner["pid"]))


def _precheck(store: str, policy: str) -> "str | None":
    """Facts checked under the shared hold BEFORE any connection exists.

    Raises the refusal; returns a readiness reason a diagnostic session admits
    (``recovery_pending`` for stats rebuild artifacts), else None."""
    path = _store_db_path(store)
    db = _db()
    marker = (_store_mod()._stats_repair_marker(path) if store == "stats"
              else db._repair_marker_path(path))
    pending = db._quarantine_pending_path(path)
    if marker.exists() or pending.exists():
        # A destructive recovery that claimed its marker must see no new
        # connection, read-only or not (spec 5.12, N2). A query reports the
        # cache's pending recovery as recovery_pending (5.5); conversations
        # and every diagnostic session as maintenance.
        if store == "cache" and policy != "diagnostic":
            if marker.exists() and _cache_repair_owner_alive(db, marker):
                # R32-1: a live owner's repair is maintenance, as today's
                # owner open reported it, so the facades keep their
                # direct-JSONL fallback.
                raise _unavailable(store, "maintenance",
                                   "cache.db maintenance is in progress")
            raise _unavailable(store, "recovery_pending",
                               "cache.db recovery is pending")
        raise _unavailable(store, "maintenance",
                           f"{store}.db maintenance is in progress")
    if store == "conversations":
        if _cache_mod()._conversation_recovery_state_path().exists():
            raise _unavailable(store, "maintenance",
                               "conversations.db maintenance is in progress")
    if not path.is_file():
        raise _unavailable(store, "absent", f"{store}.db is not present")
    if store != "stats":
        return None
    for suffix in ("-wal", "-shm"):
        if pathlib.Path(f"{path}{suffix}").exists():
            raise _unavailable(store, "unavailable",
                               f"stats.db has a {suffix} side file")
    if not _stats_journal_cleared(path, policy):
        raise _unavailable(store, "unavailable", _STATS_JOURNAL_REFUSAL)
    st = _store_mod()
    artifacts = (st._stats_rebuild_artifact_bases(path)
                 or st._stats_publication_marker(path).exists())
    kind = _stats_header_kind(path)
    if kind == "invalid":
        # SQLite's own text: what today's open reported for this file, so
        # Doctor and `db status` print no new sentence (spec section 12).
        raise _unavailable(store, "corrupt", "file is not a database",
                           sqlite_text=True)
    if kind is None:
        # The probe could not read the header (a spawn error, a timeout
        # under load, a non-zero exit, short output): fail closed, without
        # claiming a header it never read (AD19).
        raise _unavailable(store, "unavailable", "stats.db could not be read")
    if kind != "rollback":
        raise _unavailable(store, "unavailable",
                           "stats.db header is not a rollback-journal index")
    if artifacts:
        if policy != "diagnostic":
            raise _unavailable(store, "recovery_pending",
                               "stats.db recovery is pending")
        return "recovery_pending"
    return None


# ── the composed query authorizer (spec 5.2) ─────────────────────────────


class _AuthorizerState:
    def __init__(self, tag: str):
        self.tag = tag
        self.attach_allowed: "set[str]" = set()
        self.attaching = False
        self.teardown = False


def _make_authorizer(state: _AuthorizerState) -> Callable[..., int]:
    deny = sqlite3.SQLITE_DENY
    ok = sqlite3.SQLITE_OK

    def _denied(what: str) -> int:
        _cctally_core.record_authorizer_denial(state.tag, what)
        return deny

    def authorizer(action, arg1, arg2, dbname, _source):
        if action in _WRITE_ACTIONS:
            if dbname in (None, "temp"):
                return ok
            return _denied(f"write {arg1} in {dbname}")
        if action == sqlite3.SQLITE_PRAGMA:
            name = str(arg1 or "").lower()
            if name in _DENIED_PRAGMA_ACTIONS:
                return _denied(f"PRAGMA {name}")
            if name in _DENIED_PRAGMA_SETTERS and arg2 is not None:
                return _denied(f"PRAGMA {name}={arg2}")
            return ok
        if action == sqlite3.SQLITE_ATTACH:
            if state.attaching and str(arg1) in state.attach_allowed:
                return ok
            return _denied(f"ATTACH {arg1}")
        if action == sqlite3.SQLITE_DETACH:
            if state.teardown:
                return ok
            return _denied(f"DETACH {arg1}")
        if action == sqlite3.SQLITE_ANALYZE:
            return _denied("ANALYZE")
        return ok

    return authorizer


# ── readiness reads (inside the snapshot) ────────────────────────────────


def _migration_state(conn, schema: str) -> "str | None":
    try:
        applied = sorted((str(n), str(t)) for n, t in conn.execute(
            f"SELECT name, applied_at_utc FROM {schema}.schema_migrations"))
        try:
            skipped = sorted(str(r[0]) for r in conn.execute(
                f"SELECT name FROM {schema}.schema_migrations_skipped"))
        except sqlite3.OperationalError:
            skipped = []
    except sqlite3.Error:
        return None
    payload = repr((applied, skipped)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _applied_names(conn, schema: str) -> "set[str] | None":
    try:
        return {str(r[0]) for r in conn.execute(
            f"SELECT name FROM {schema}.schema_migrations")}
    except sqlite3.Error:
        return None


def _migration_error_pending(conn, schema: str, registry, label: str) -> bool:
    """Read-only twin of `_reconcile_durable_applied_migration_errors`: a
    durable sentinel entry names a migration this store already applied."""
    log = pathlib.Path(_cctally_core.MIGRATION_ERROR_LOG_PATH)
    try:
        if not log.exists():
            return False
        content = log.read_text()
    except OSError:
        return False
    applied = _applied_names(conn, schema) or set()
    firsts = [b.splitlines()[0] for b in content.split("\n\n")
              if b.strip() and b.splitlines()]
    for migration in registry:
        if migration.name not in applied:
            continue
        tag = f" {label}:{migration.name}"
        if any(line.endswith(tag) for line in firsts):
            return True
    return False


def _cache_meta_value(conn, schema: str, key: str) -> "str | None":
    try:
        row = conn.execute(
            f"SELECT value FROM {schema}.cache_meta WHERE key=?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None or not row[0] else str(row[0])


def _codex_maintenance_debt(conn, schema: str) -> bool:
    cache = _cache_mod()
    for key in (cache.CODEX_FINALIZATION_DEBT_KEY,
                cache.CODEX_REPLAY_FROM_ZERO_KEY):
        try:
            if conn.execute(
                    f"SELECT 1 FROM {schema}.cache_meta WHERE key=?",
                    (key,)).fetchone():
                return True
        except sqlite3.Error:
            return False
    try:
        if conn.execute(
                f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' "
                "AND name='codex_prune_debt'").fetchone():
            return conn.execute(
                f"SELECT 1 FROM {schema}.codex_prune_debt LIMIT 1"
            ).fetchone() is not None
    except sqlite3.Error:
        return False
    return False


def _read_store(conn, store: str, schema: str, *, attached: "set[str]"):
    """(Readiness, StoreIdentity-without-file) for `store` at `schema`,
    read inside the session transaction. Effect-free."""
    db = _db()
    obligations: "set[str]" = set()
    reason: "str | None" = None
    version = int(conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0])
    incarnation = protocol = content_revision = migration_state = None
    rollup_epoch = None
    if store == "stats":
        epoch = int(_cctally_core.STATS_INDEX_EPOCH)
        legacy = int(_cctally_core.LEGACY_STATS_HEAD)
        if version <= legacy:
            obligations.add("legacy_migration_pending")
        elif version < epoch:
            reason = "schema_behind"
        elif version > epoch:
            reason = "schema_ahead"
        else:
            import _lib_account_protocol
            import _lib_stats_content_revision as rev
            try:
                _store_mod().validate_stats_identity(
                    conn, expected_epoch=epoch,
                    expected_protocol=_lib_account_protocol.PROTOCOL_GENERATION)
                protocol = _lib_account_protocol.PROTOCOL_GENERATION
            except _store_mod().StatsIndexNotCurrent:
                protocol = None
            try:
                current = rev.read_claude_content_revision(conn)
            except rev.StatsContentRevisionUnreadable:
                # Malformed keeps its existing semantics (5.5 "Readiness
                # inside the snapshot"): the owner opener reads such a store
                # and never repairs it, and each reader of the identity gets
                # the unreadable error and treats the revision as untrusted
                # (#1050). No reusable revision, and no obligation, since no
                # owner open drains it.
                current = None
            if current is None:
                pass
            elif current.state != rev.STATE_CURRENT or current.token is None:
                obligations.add("content_revision_missing")
            else:
                content_revision = (current.state, current.token)
            if quota_projection_incomplete(conn):
                obligations.add("quota_projection_incomplete")
            if _migration_error_pending(conn, schema, db._STATS_MIGRATIONS,
                                        "stats.db"):
                obligations.add("migration_error_sentinel")
    elif store == "cache":
        head = len(db._CACHE_MIGRATIONS)
        if version < head:
            reason = "schema_behind"
        elif version > head:
            reason = "schema_ahead"
        elif conn.execute(
                f"SELECT 1 FROM {schema}.sqlite_master "
                "WHERE name='conversation_messages'").fetchone() is None:
            reason = "schema_behind"
        elif db.migration_fence_present(conn, schema):
            reason = "recovery_pending"
        else:
            if _migration_error_pending(conn, schema, db._CACHE_MIGRATIONS,
                                        "cache.db"):
                obligations.add("migration_error_sentinel")
            if _codex_maintenance_debt(conn, schema):
                obligations.add("codex_maintenance_debt")
        incarnation = _cache_meta_value(conn, schema, "cache_incarnation_id")
        migration_state = _migration_state(conn, schema)
    else:
        head = len(db._CONVERSATIONS_MIGRATIONS)
        if version < head:
            reason = "schema_behind"
        elif version > head:
            reason = "schema_ahead"
        elif "cache_db" in attached and (
                _cache_mod().conversation_legacy_bridge_pending(conn)):
            reason = "legacy_bridge_pending"
        incarnation = _cache_meta_value(conn, schema,
                                        "conversations_incarnation_id")
        migration_state = _migration_state(conn, schema)
        if "cache_db" in attached:
            rollup_epoch = _cache_mod()._conversation_rollup_epoch(conn)
    readiness = _cctally_core.Readiness(
        store=store, readable=reason is None, reason=reason,
        obligations=frozenset(obligations) if reason is None else frozenset())
    identity = StoreIdentity(
        file=None, user_version=version, incarnation=incarnation,
        protocol=protocol, content_revision=content_revision,
        migration_state=migration_state, rollup_epoch=rollup_epoch,
        store=store)
    return readiness, identity


def quota_projection_incomplete(conn) -> bool:
    import _cctally_journal
    return _cctally_journal.quota_projection_incomplete_flag(conn)


# ── the session ──────────────────────────────────────────────────────────


class QuerySession:
    """One snapshot over a primary store and its read-only attachments."""

    def __init__(self, conn, identity, readiness, scope, roles):
        self.conn = conn
        self.identity = identity
        self.readiness = readiness
        self.scope = scope
        self._roles = roles
        #: AD19: ``(what, seconds)`` for each hold of the shared admission
        #: past its deadline while this session was admitted.
        self.admission_overruns: "tuple[tuple[str, float], ...]" = ()

    def capture(self, fn: "Callable[[sqlite3.Connection], Any]"):
        """Run ``fn(conn)`` inside the session transaction (spec 5.13)."""
        return fn(self.conn)


_SESSION_SEQ = [0]
_SESSION_SEQ_LOCK = threading.Lock()


def _next_tag(primary: str) -> str:
    with _SESSION_SEQ_LOCK:
        _SESSION_SEQ[0] += 1
        return f"query-session:{primary}:{_SESSION_SEQ[0]}"


def _ro_uri(path: pathlib.Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _connect_ro(store: str, path: pathlib.Path, policy: str, tag: str, *,
                uncached: bool = False):
    # 5.3 / AD29 (R26-2): the busy timeout is fixed here as min(policy cap,
    # max(0, effective admission deadline - now)), from the deadline the
    # running admission fixed at its entry, never from the read unit's
    # not-yet-debited budget. UNCACHED: no statement cache for any store
    # (the AD31 held-session reader), as stats always has (#778).
    remaining = _cctally_core.remaining_deadline()
    if remaining is None:
        remaining = _cctally_core._QUERY_ADMISSION_BUDGET_S[policy]
    busy = (min(_INTERACTIVE_BUSY_CAP_S, remaining)
            if policy == "interactive" else remaining)
    kwargs: "dict[str, Any]" = {
        "uri": True, "timeout": max(0.0, busy),
        "factory": _QueryConnection, "isolation_level": None,
        "check_same_thread": True,
    }
    if store == "stats" or uncached:
        kwargs["cached_statements"] = 0      # #778
    conn = sqlite3.connect(_ro_uri(path), **kwargs)
    conn.session_tag = tag
    trace = getattr(_store_mod(), "_TRACE_HOOK", None)
    if trace is not None:
        conn.set_trace_callback(trace)
    return conn


#: The session-tag prefix of the AD31 held-session reader's connection.
HELD_SESSION_TAG = "held-session"


def held_query_connection(store: str, path, policy: str):
    """The connection of the AD31 held-session reader (revision 29, R28-2):
    `explain`'s conversations reader, which keeps #1000 5.10's maintenance
    lock held for the connection's whole life (`conversation_ro_guarded`)
    and so is not a query session. Only that lifetime differs: the
    connection carries every other 5.2 and 5.4 query protection, from the
    shared query connection factory (``mode=ro``, `_QueryConnection`, no
    statement cache, the 5.3 busy timeout from the running admission's
    deadline, the trace hook), the composed authorizer whose denials the
    violation latch records, ``temp_store=MEMORY`` and its instrumentation
    tag (``held-session:<store>:<n>``). The caller runs it inside one
    admission of its read unit (AD29) and checks the latch at close."""
    with _SESSION_SEQ_LOCK:
        _SESSION_SEQ[0] += 1
        tag = f"{HELD_SESSION_TAG}:{store}:{_SESSION_SEQ[0]}"
    conn = _connect_ro(store, pathlib.Path(path), policy, tag, uncached=True)
    try:
        conn.set_authorizer(_make_authorizer(_AuthorizerState(tag)))
        conn.execute(f"PRAGMA temp_store={QUERY_TEMP_STORE}")
    except BaseException:
        conn.close()
        raise
    return conn


def _build_session(primary: str, attach: "tuple[str, ...]", account,
                   policy: str):
    """Steps 1-8 of the 5.13 snapshot protocol. Returns
    (conn, identity, readiness, auth_state, admissions)."""
    if not _private_data_dir_ok():
        raise _unavailable(primary, "unavailable",
                           "the cctally data directory is not private")
    roles = [("primary", primary)] + [
        (_ATTACH_ALIAS[a], a) for a in attach]
    admissions: "list[_Admission]" = []
    conn = None
    try:
        # 1. Admit each store in the established order (conversations, then
        # cache; spec 5.8): each holds its shared maintenance lock through the
        # checks and the connect/attach, released before reads.
        diag_reasons: "dict[str, str]" = {}
        for _role, store in roles:
            admissions.append(_admit(store, policy))
            r = _precheck(store, policy)
            if r is not None:
                diag_reasons[store] = r
        tag = _next_tag(primary)
        # 2. Pre-open observation of every file.
        observed = {}
        for role, store in roles:
            ident = _file_identity(_store_db_path(store))
            if ident is None:
                raise _unavailable(store, "absent",
                                   f"{store}.db is not present")
            observed[role] = ident
        # 3. Connect mode=ro, then the authorizer and connection-local policy.
        conn = _connect_ro(primary, _store_db_path(primary), policy, tag)
        state = _AuthorizerState(tag)
        conn.set_authorizer(_make_authorizer(state))
        conn.execute(f"PRAGMA temp_store={QUERY_TEMP_STORE}")
        # 4. ATTACH read-only URIs (outside any transaction).
        for role, store in roles[1:]:
            uri = _ro_uri(_store_db_path(store))
            state.attach_allowed.add(uri)
            state.attaching = True
            try:
                # A literal, not a bound parameter: the authorizer sees a
                # bound ATTACH argument only as its placeholder at prepare.
                quoted = uri.replace("'", "''")
                conn.execute(f"ATTACH DATABASE '{quoted}' AS {role}")
            finally:
                state.attaching = False
        for adm in admissions:
            adm.release()
        # 5. BEGIN (deferred).
        conn.execute("BEGIN")
        # 6. Identity and readiness, primary first then attachments by name.
        attached = {role for role, _s in roles[1:]}
        readiness: "dict[str, Any]" = {}
        identities: "dict[str, StoreIdentity]" = {}
        ordered = [roles[0]] + sorted(roles[1:])
        for role, store in ordered:
            schema = "main" if role == "primary" else role
            r, ident = _read_store(conn, store, schema, attached=attached)
            if store in diag_reasons and r.readable:
                r = _cctally_core.Readiness(store, False, diag_reasons[store],
                                            frozenset())
            readiness[role] = r
            identities[role] = ident
        # 7. Post-read binding against the pre-open observation.
        for row in conn.execute("PRAGMA database_list").fetchall():
            role = "primary" if row[1] == "main" else row[1]
            if role not in observed:
                continue
            now = _file_identity(row[2]) if row[2] else None
            if now is None or now != observed[role]:
                raise _BindingFailed(role)
            identities[role] = dataclasses.replace(
                identities[role], file=observed[role])
        return conn, SessionIdentity(identities), readiness, state, roles
    except _cctally_core.StoreUnavailable:
        _cleanup_build(admissions, conn)
        raise
    except sqlite3.DatabaseError as exc:
        _cleanup_build(admissions, conn)
        if _hot_journal_refusal(primary, exc):
            raise _unavailable(primary, "unavailable",
                               _STATS_JOURNAL_REFUSAL) from exc
        text = str(exc).lower()
        if "not authorized" in text:
            raise
        if "locked" in text or "busy" in text:
            raise _unavailable(primary, "busy", str(exc),
                               sqlite_text=True) from exc
        if "not a database" in text or "malformed" in text:
            raise _unavailable(primary, "corrupt", str(exc),
                               sqlite_text=True) from exc
        raise
    except BaseException:
        for adm in admissions:
            adm.release()
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass
        raise


def _cleanup_build(admissions, conn) -> None:
    for adm in admissions:
        adm.release()
    if conn is not None:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        try:
            conn.close()
        except sqlite3.Error:
            pass


class _BindingFailed(Exception):
    """The file a schema reads is not the file observed before the open."""


def _check_admissible(readiness: Mapping, policy: str, primary: str,
                      tolerate: "frozenset[str]" = frozenset(),
                      notify: bool = True) -> None:
    for role, r in readiness.items():
        if r.readable:
            continue
        if policy == "diagnostic" and r.reason in _DIAGNOSTIC_ADMITTED:
            continue
        if notify:
            _cctally_core.notify_owner(r.store, r)
        if r.reason in tolerate:
            # Read as it is (R8), but its owner is still told, as the
            # refusal told it, so the owner phase migrates or heals it.
            continue
        raise _unavailable(r.store, r.reason or "unavailable",
                           family=primary)


def _teardown(conn, state) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    try:
        import _lib_codex_metadata
        _lib_codex_metadata.invalidate_codex_threads_table_resolution(conn)
    except Exception:
        pass
    state.teardown = True
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] not in ("main", "temp"):
                conn.execute(f"DETACH DATABASE {row[1]}")
    except sqlite3.Error:
        pass
    try:
        conn.close()
    except sqlite3.Error:
        pass


@contextlib.contextmanager
def _session(primary: str, *, attach=(), account=None, source=None,
             policy: str = "interactive",
             tolerate: "frozenset[str]" = frozenset(),
             notify: bool = True):
    if primary not in _STORES:
        raise ValueError(f"unknown store {primary!r}")
    tolerate = frozenset(tolerate)
    if not tolerate <= SCHEMA_MISMATCH_REASONS:
        raise ValueError(f"cannot tolerate {sorted(tolerate)!r}")
    attach = tuple(attach)
    for name in attach:
        if name not in _ATTACH_ALIAS or name == primary:
            raise ValueError(f"cannot attach {name!r} to {primary!r}")
    if policy not in _POLICIES:
        raise ValueError(f"unknown query policy {policy!r}")
    if account is not None and primary != "conversations":
        raise ValueError("account scope applies to conversations sessions")
    if account is not None:
        # Inputs are validated before any lock, attach or sidecar (5.8).
        if not str(account).strip():
            raise ValueError("account_key is required")
        if "cache" not in attach:
            raise ValueError("an account-scoped session attaches cache")
    kind = "diagnostic" if policy == "diagnostic" else "query"
    # AD29 (5.3): the outermost session of this thread creates the read
    # unit's admission budget, and a nested session draws on it. Only the
    # admission below debits it (once, at its exit), never the statements
    # this session yields to nor the caller's computation around it.
    with _cctally_core.operation_budget(policy):
        with _cctally_core.authority_scope(kind, policy=policy):
            with _cctally_core.operation_boundary(
                    f"query-session:{primary}") as latch:
                built = None
                overruns: "list[tuple[str, float]]" = []
                sink = _ADMISSION_OVERRUNS.set(overruns)
                try:
                    with _cctally_core.admission_scope(policy):
                        for attempt in range(2):
                            try:
                                built = _build_session(primary, attach,
                                                       account, policy)
                                break
                            except _BindingFailed:
                                if attempt == 1:
                                    raise _unavailable(
                                        primary, "busy",
                                        f"{primary}.db changed during the "
                                        f"open")
                except _cctally_core.StoreUnavailable as exc:
                    # AD19: an overrun is recorded on the refusal too, since
                    # a probe that outlived its deadline refuses the store.
                    exc.admission_overruns = tuple(overruns)
                    raise
                finally:
                    _ADMISSION_OVERRUNS.reset(sink)
                conn, identity, readiness, state, roles = built
                failure: "BaseException | None" = None
                try:
                    _check_admissible(readiness, policy, primary, tolerate,
                                      notify)
                    if primary == "conversations" and "cache" in attach:
                        # The read-only opener's last-good thread views
                        # (#968) and their resolver seed (#854 R2); teardown
                        # evicts the seed on every exit.
                        cache_mod = _cache_mod()
                        if cache_mod._install_codex_last_good_views(conn):
                            import _lib_codex_metadata
                            _lib_codex_metadata.\
                                seed_codex_last_good_threads_resolution(conn)
                    scope = None
                    if account is not None:
                        _cache_mod().scope_conversations_db_to_account(
                            conn, account, in_session=True)
                        scope = str(account).strip()
                    session = QuerySession(conn, identity, readiness, scope,
                                           roles)
                    session.admission_overruns = tuple(overruns)
                    yield session
                except BaseException as exc:
                    failure = exc
                    raise
                finally:
                    _teardown(conn, state)
                    # Every exit: a denial recorded on this session refuses,
                    # even when the caller swallowed the raising statement.
                    if latch.violated and not isinstance(
                            failure, _cctally_core.QueryAuthorityViolation):
                        first = latch.events[0]
                        raise _cctally_core.QueryAuthorityViolation(
                            first.entry, first.capability,
                            first.authority) from failure


class QueryService:
    """Query sessions, readiness and the restricted metadata reader."""

    @staticmethod
    def session(primary: str, *, attach: "tuple[str, ...]" = (),
                account: "str | None" = None, source: "str | None" = None,
                policy: str = "interactive",
                tolerate: "frozenset[str]" = frozenset()):
        """``tolerate`` (a subset of `SCHEMA_MISMATCH_REASONS`) admits a
        store whose schema does not match this binary for a caller that
        reads only tables and columns it checks itself and treats their
        absence as no data; every other refusal, the admission and the
        read-only authorizer are unchanged."""
        return _session(primary, attach=attach, account=account,
                        source=source, policy=policy, tolerate=tolerate)

    # Entry readers (spec 5.11): the facades' parameters minus ``skip_sync``,
    # over the same read kernels, inside one query session. They never sync,
    # open a full opener or fall back to direct JSONL: unavailability raises
    # StoreUnavailable and the caller keeps its existing exception path.

    @staticmethod
    def entries(range_start, range_end, *, project: "str | None" = None,
                account_key: "str | None" = None,
                policy: str = "interactive"):
        cache = importlib.import_module("_cctally_cache")
        with _session("cache", policy=policy) as s:
            return cache.iter_entries(
                s.conn, range_start, range_end, project=project,
                account_key=account_key)

    @staticmethod
    def claude_session_entries(range_start, range_end, *,
                               project: "str | None" = None,
                               account_key: "str | None" = None,
                               policy: str = "interactive"):
        cache = importlib.import_module("_cctally_cache")
        with _session("cache", policy=policy) as s:
            return cache._read_claude_session_entries(
                s.conn, range_start, range_end, project=project,
                account_key=account_key)

    @staticmethod
    def codex_entries(range_start, range_end, *,
                      account_key: "str | None" = None,
                      policy: str = "interactive"):
        cache = importlib.import_module("_cctally_cache")
        # The hook's pinned-snapshot policy (#981 A4, #1076) keeps its own
        # read contract, exactly as in get_codex_entries.
        read_policy = cache._active_codex_read_policy()
        if read_policy is not None:
            return read_policy.read_entries(
                range_start, range_end, account_key=account_key)
        with _session("cache", policy=policy) as s:
            return cache.iter_codex_entries(
                s.conn, range_start, range_end, account_key=account_key,
                current_roots_only=True)

    @staticmethod
    def readiness(store: str, *, policy: str = "interactive",
                  notify: bool = True) -> "_cctally_core.Readiness":
        """The effect-free readiness predicate (spec 5.5, AD2).

        ``notify=False`` reads it without telling the owner about a
        not-readable state: the owner's own outstanding-work probe must
        never wake itself (revision 33, R32-V2)."""
        try:
            with _session(store, policy=policy, notify=notify) as s:
                return s.readiness["primary"]
        except _cctally_core.StoreUnavailable as exc:
            return _cctally_core.Readiness(store, False, exc.reason,
                                           frozenset())

    @staticmethod
    @contextlib.contextmanager
    def metadata(store: str):
        """The restricted diagnostic metadata reader (spec 5.12): ``mode=ro``
        under diagnostic admission with recovery suppressed and no
        current-schema gate. A repair marker or pending quarantine refuses
        before connect."""
        if store not in _STORES:
            raise ValueError(f"unknown store {store!r}")
        with _cctally_core.operation_budget("diagnostic"):
            with _cctally_core.authority_scope("diagnostic",
                                               policy="diagnostic"):
                if not _private_data_dir_ok():
                    raise _unavailable(store, "unavailable",
                                       "the cctally data directory is not "
                                       "private")
                conn = None
                overruns: "list[tuple[str, float]]" = []
                sink = _ADMISSION_OVERRUNS.set(overruns)
                try:
                    # AD29: one admission, charged to the budget on exit.
                    with _cctally_core.admission_scope("diagnostic"):
                        adm = _admit(store, "diagnostic")
                        try:
                            try:
                                _precheck(store, "diagnostic")
                            except _cctally_core.StoreUnavailable as exc:
                                if exc.reason not in ("recovery_pending",):
                                    exc.admission_overruns = tuple(overruns)
                                    raise
                            tag = _next_tag(f"metadata:{store}")
                            conn = _connect_ro(store, _store_db_path(store),
                                               "diagnostic", tag)
                            state = _AuthorizerState(tag)
                            conn.set_authorizer(_make_authorizer(state))
                            conn.execute(
                                f"PRAGMA temp_store={QUERY_TEMP_STORE}")
                            conn.admission_overruns = tuple(overruns)
                        finally:
                            adm.release()
                finally:
                    _ADMISSION_OVERRUNS.reset(sink)
                try:
                    yield conn
                finally:
                    _teardown(conn, state)

    @staticmethod
    def notify_owner(store: str, readiness) -> None:
        _cctally_core.notify_owner(store, readiness)


class WriterService:
    @staticmethod
    @contextlib.contextmanager
    def request_writer(store: str):
        """A connection for the restricted request entry points (5.9, 5.10).

        Admitted only on a readable, schema-current store (the attached cache
        and the Codex contract included); anything else raises
        `StoreUnavailable` before any mutation. It is a plain owner
        connection: the request entries' own `require_capability` calls and
        locks govern what it may write."""
        _cctally_core.require_capability("INGEST_CURRENT",
                                         entry="request_writer")
        if store not in ("cache", "conversations"):
            raise ValueError(f"no request writer for {store!r}")
        attach = ("cache",) if store == "conversations" else ()
        with _session(store, attach=attach, policy="interactive") as s:
            for r in s.readiness.values():
                if not r.readable:
                    raise _unavailable(r.store, r.reason or "unavailable")
        path = _store_db_path(store)
        conn = sqlite3.connect(
            str(path), timeout=_INTERACTIVE_BUSY_CAP_S)
        try:
            # The owner writers' connection settings (synchronous, WAL size
            # cap, in-memory statement journals), never a journal-mode or
            # schema PRAGMA; the busy wait stays the interactive cap.
            _store_mod().apply_connection_policy(conn, store)
            conn.execute(
                f"PRAGMA busy_timeout={int(_INTERACTIVE_BUSY_CAP_S * 1000)}")
            if store == "conversations":
                conn.execute("ATTACH DATABASE ? AS cache_db",
                             (str(_store_db_path("cache")),))
            yield conn
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass


class StartupService:
    @staticmethod
    def ensure(store: str) -> None:
        """The existing full opener at the caller's authority (process),
        then close. Narrows nothing (spec 5.1)."""
        if store == "stats":
            conn = _cctally_core.open_db()
            try:
                _settle_inert_stats_sidecars(conn)
            finally:
                conn.close()
            return
        elif store == "cache":
            conn = _cache_mod().open_cache_db()
        elif store == "conversations":
            conn = _cache_mod().open_conversations_db()
        else:
            raise ValueError(f"unknown store {store!r}")
        conn.close()


#: How long the startup phase waits for SQLite's EXCLUSIVE lock on stats.db
#: to settle its inert sidecars; on a busy store it leaves them (the query
#: then refuses, as before).
_SETTLE_BUSY_TIMEOUT_MS = 250


def _settle_inert_stats_sidecars(conn) -> None:
    """Remove the stats sidecars a rollback-journal store's owner open
    leaves in place although no connection can be using them (I2f item 2).

    The owner open already does what SQLite does: a read rolls a hot
    `-journal` back, and a `-wal` with frames is opened and checkpointed
    away. What SQLite leaves are files it treats as absent: a `-journal`
    that is not hot (a crash before the writer's first sync leaves its
    header zero; a zero-byte file), a zero-byte `-wal` (the unix VFS counts
    a zero-size file as nonexistent) and a `-shm` outside WAL mode. Base
    read the store with them present; the query layer refuses any of them
    (5.4) and never recovers, so the owner, which may, removes them here.

    Only under SQLite's EXCLUSIVE lock on the main file, taken without
    writing: no connection is then reading or writing the rollback-journal
    store, and SQLite has rolled back any journal that was hot before
    granting the lock. Only when the main header is rollback-journal and
    any `-wal` holds no frames, so no WAL-mode connection can exist. On a
    busy store, or in any other state, nothing is touched.

    No descriptor on stats.db is ever opened in this process while the
    lock is held: closing one drops every POSIX lock the process holds on
    the inode, SQLite's EXCLUSIVE lock included (AD19). So the header is
    read before the lock, through the AD19 child, and under the lock the
    state is re-read only through the locked connection: SQLite reads the
    header when it takes the lock and opens WAL mode if it declares WAL."""
    path = _store_db_path("stats")
    sidecars = [pathlib.Path(f"{path}{s}") for s in ("-journal", "-wal",
                                                     "-shm")]
    if not any(p.exists() for p in sidecars) or conn.in_transaction:
        return
    if _stats_header_kind(pathlib.Path(path)) != "rollback":
        return
    try:
        conn.execute(f"PRAGMA busy_timeout={_SETTLE_BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError:
        return
    try:
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            mode = conn.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.Error:
            return
        if not mode or str(mode[0]).lower() == "wal":
            return
        try:
            if os.stat(sidecars[1]).st_size:
                return
        except FileNotFoundError:
            pass
        for sidecar in sidecars:
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        return
    finally:
        conn.rollback()


# ── CLI command phases (spec 5.11, AD2) ──────────────────────────────────

#: The obligations a CLI startup phase drains through the full opener (AD2);
#: `quota_projection_incomplete` stays with armed processes and
#: `codex_maintenance_debt` with the refresh phase (5.5).
CLI_DRAINED_OBLIGATIONS = frozenset({
    "content_revision_missing", "legacy_migration_pending",
    "migration_error_sentinel",
})


def ensure_command_store(store: str) -> None:
    """A CLI command's startup phase for `store` (spec 5.11, AD2).

    Evaluates the effect-free readiness predicate first and runs the
    existing full opener, at the caller's authority (`process`), only when
    the store is not readable or carries a CLI-drained obligation: exactly
    what today's CLI open does (create, migrate, recover, epoch deferral and
    its worker launch). The predicate uses the interactive budget: a
    not-readable answer only hands the decision to the owner opener, which
    keeps its own (AD7-bounded) wait and its existing errors."""
    if _command_store_ready(store):
        return
    StartupService.ensure(store)


def _command_store_ready(store: str) -> bool:
    """AD2's predicate: readable and free of CLI-drained obligations. The
    conversations store is judged with cache attached, so a pending legacy
    bridge (a readable main file whose rows still sit in cache.db) hands the
    open to the owner opener that runs the bridge, as today."""
    attach = ("cache",) if store == "conversations" else ()
    try:
        with _session(store, attach=attach, policy="interactive") as s:
            states = list(s.readiness.values())
    except _cctally_core.StoreUnavailable:
        return False
    return all(r.readable and not (r.obligations & CLI_DRAINED_OBLIGATIONS)
               for r in states)


def _command_unavailable(exc: "_cctally_core.StoreUnavailable"):
    """A query-phase race to unavailability keeps the store's existing CLI
    message and class (spec 5.5): stats maintenance is today's
    `StatsDbMaintenanceError` (exit 3); cache and conversations already
    carry `<store>.db maintenance is in progress`."""
    if exc.store == "stats" and exc.reason == "maintenance":
        store_mod = _store_mod()
        return _db().StatsDbMaintenanceError(
            store_mod._stats_open_maintenance_timeout_message())
    return None


def open_command_session(primary: str, **kwargs):
    """`command_session` entered on an `ExitStack`: returns ``(session,
    stack)`` for a call site whose ``try/finally: conn.close()`` shape closes
    ``stack`` instead. The stack is closed if the entry raises."""
    stack = contextlib.ExitStack()
    try:
        session = stack.enter_context(command_session(primary, **kwargs))
    except BaseException:
        stack.close()
        raise
    return session, stack


#: Refusals a command session reports as they are instead of handing the
#: store to the startup phase: contention (the owner opener would wait the
#: same bounded maintenance again) and corruption (today's open reports it).
_COMMAND_RAISES = frozenset({"maintenance", "busy"})


def _needs_startup(session) -> bool:
    return not all(r.readable and not (r.obligations & CLI_DRAINED_OBLIGATIONS)
                   for r in session.readiness.values())


def _default_read_policy() -> str:
    """The admission policy of a reader that names none (5.3): the current
    authority's policy (a `query(interactive)` build or request reads at
    ``interactive``, a ``diagnostic`` gather at ``diagnostic``), else
    ``command`` under an established authority that names none (CLI query
    phases at ``process``), else ``interactive``."""
    a = _cctally_core.current_authority()
    if a is None:
        return "interactive"
    if a.policy is not None:
        return a.policy
    return "command"


@contextlib.contextmanager
def command_session(primary: str, *, attach: "tuple[str, ...]" = (),
                    account: "str | None" = None, startup: bool = True,
                    row_factory: Any = None, policy: "str | None" = None,
                    tolerate: "frozenset[str]" = frozenset()):
    """One CLI read unit over `primary` (spec 5.11, AD2, AD21).

    The session is the read unit: the caller reads (and captures what it
    needs) inside it and leaves before rendering or computing, so a
    rollback-journal stats session's SHARED lock is held only while the
    unit reads and a `record-usage` commit never waits on a command's
    computation (AD21).

    Startup (AD2) is evaluated from the session itself: a readable store
    with no CLI-drained obligation is read directly. Otherwise the session
    is closed, the existing full owner opener runs for every store the
    session reads, at the caller's `process` authority (create, migrate,
    recover, epoch deferral and its worker launch, as today's CLI open),
    and the session opens again. Maintenance and busy refusals are reported
    as they are (the owner opener would wait the same bounded maintenance
    again); stats maintenance keeps today's `StatsDbMaintenanceError`.

    The session connection gets the owner opener's row factory for
    `primary` (``sqlite3.Row`` for stats, plain tuples for cache and
    conversations), so an existing kernel reads it exactly as it read the
    owner connection; ``row_factory`` overrides it. ``startup=False`` is
    for a caller whose own startup phase already ran, or that must never run
    it. ``policy`` overrides the default, e.g. ``diagnostic`` for a reader
    that reads whatever schema the store has. The default is the current
    authority's own policy when it names one (5.3: ``interactive`` for a
    snapshot build, a TUI render and a dashboard request narrowed to
    ``query(interactive)``), ``command`` under an authority that names none
    (the CLI's ``process`` main thread: its query phases), and
    ``interactive`` with no authority established. ``tolerate`` is
    `QueryService.session`'s."""
    if policy is None:
        policy = _default_read_policy()

    def _open(stack):
        return stack.enter_context(QueryService.session(
            primary, attach=attach, account=account, policy=policy,
            tolerate=tolerate))

    # Decided at the caller's authority, before the session narrows this
    # thread to `query`: inside the open session no thread holds the
    # full-opener capabilities, so a CLI-drained obligation would never be
    # drained (5.11, AD2).
    permitted = _startup_permitted() if startup else False
    try:
        stack = contextlib.ExitStack()
        try:
            session = None
            if startup:
                try:
                    session = _open(stack)
                except _cctally_core.StoreUnavailable as exc:
                    if exc.reason in _COMMAND_RAISES:
                        raise
                    if not permitted:
                        # A request ceiling never runs the startup phase:
                        # the owner is told and the refusal stands (5.5).
                        _cctally_core.notify_owner(exc.store, _cctally_core.
                                                   Readiness(exc.store, False,
                                                             exc.reason,
                                                             frozenset()))
                        raise
                if session is not None and _needs_startup(session):
                    if not permitted:
                        # Readable (an obligation only): read it as it is;
                        # the obligation stays with its owner (5.5).
                        startup = False
                    else:
                        stack.close()
                        session = None
                if session is None and startup:
                    # Attachments first: the conversations owner opener opens
                    # cache.db itself, so cache is established first, as today.
                    for store in tuple(attach) + (primary,):
                        StartupService.ensure(store)
            if session is None:
                session = _open(stack)
        except BaseException:
            stack.close()
            raise
        with stack:
            if row_factory is not None:
                session.conn.row_factory = row_factory
            elif primary == "stats":
                session.conn.row_factory = sqlite3.Row
            yield session
    except _cctally_core.StoreUnavailable as exc:
        mapped = _command_unavailable(exc)
        if mapped is None:
            raise
        raise mapped from exc


def read_unit(primary: str, **kwargs):
    """`command_session` under its AD21 name, for a consumer read function
    or a read group that today runs in one transaction."""
    return command_session(primary, **kwargs)


class HeldSessionConnection:
    """A query session's connection whose ``close()`` ends the session
    (rollback, TEMP dropped, detach, close, authority restored, and the read
    unit's admission budget disposed of when this session created it).

    For a consumer read function whose existing shape is ``conn = open();
    try: ... finally: conn.close()`` (AD21: the function is the read unit).
    Everything else is the session connection's own attribute."""

    def __init__(self, session, stack):
        self._conn = session.conn
        self._stack = stack
        self.session = session

    _OWN = frozenset({"_conn", "_stack", "session"})

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        # A consumer that sets ``row_factory`` (or any connection attribute)
        # sets it on the session's connection, as it did on the owner one.
        if name in HeldSessionConnection._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def close(self) -> None:
        self._stack.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def held_session(primary: str, *, row_factory: Any = None,
                 **kwargs) -> HeldSessionConnection:
    """`QueryService.session(primary, **kwargs)` entered and handed back as a
    `HeldSessionConnection`; the stack is closed if the entry raises. Never
    runs a startup phase (a reader that may convert a store says so with
    `reader_retry`)."""
    stack = contextlib.ExitStack()
    try:
        session = stack.enter_context(QueryService.session(primary, **kwargs))
        if row_factory is not None:
            session.conn.row_factory = row_factory
    except BaseException:
        stack.close()
        raise
    return HeldSessionConnection(session, stack)


def reads_through_query() -> bool:
    """Whether this thread reads as a pure query caller (spec 5.11): an
    established authority without WRITE (a request ceiling, a narrowed query
    or diagnostic scope). An owner phase (`process`) and a thread with no
    authority keep the owner facades and openers, as before."""
    a = _cctally_core.current_authority()
    return a is not None and "WRITE" not in a.capabilities


def _startup_permitted() -> bool:
    """Whether a reader's startup phase may run on this thread: under an
    established authority only with every full-opener capability; with none
    established, as unspecified authority permits (landing parts 1–2)."""
    if _cctally_core.current_authority() is None:
        return all(_cctally_core.has_capability(c)
                   for c in _READER_STARTUP_CAPABILITIES)
    return holds_owner_startup()


def holds_owner_startup() -> bool:
    """Whether the current thread may run a store's startup phase: an
    established authority holding every capability a full opener's branch
    may need. A request ceiling (`query`, `diagnostic`, `request_writer`)
    and a thread with no authority do not."""
    return (_cctally_core.current_authority() is not None
            and all(_cctally_core.has_capability(c)
                    for c in _READER_STARTUP_CAPABILITIES))


class _MaterializedCursor:
    """A cursor-shaped view of rows already read inside a closed session."""

    def __init__(self, rows: list, description):
        self._rows = rows
        self._pos = 0
        self.description = description
        self.rowcount = -1
        self.lastrowid = None

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int = 1):
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        self._rows = []


class StatementReader:
    """A read-only, connection-shaped reader whose every statement is one read
    unit (AD21: "today's per-statement reads become sessions of one read
    unit").

    A command that handed an owner `open_db()` connection to a kernel read
    it one autocommit statement at a time. Each `execute` here opens a
    `command_session`, runs the statement, materializes its rows and closes
    the session before returning, so no SHARED lock outlives the statement
    and the kernel's computation between statements never blocks a stats
    writer.

    A kernel that brackets several statements in an explicit ``BEGIN`` ...
    ``ROLLBACK``/``COMMIT`` to read one consistent snapshot already names a
    read unit: the ``BEGIN`` opens one session (whose own deferred
    transaction is the snapshot), the bracketed statements read it, and the
    closing statement ends the session. ``in_transaction`` reports whether
    such a unit is open, as on a connection, and ``commit()`` /
    ``rollback()`` end it as they end a connection's read transaction.

    Construction runs the startup phase (AD2) once, so an open failure is
    reported where today's `open_db()` reported it; later statements read
    the established store. A write, DDL or denied PRAGMA is refused by the
    session's authorizer as for any query. `close()` ends an open unit.

    ``on_session(conn)``, when given, runs on every session's connection
    right after it opens, before the statement: per-connection TEMP state a
    kernel reads through (a TEMP view naming an attached table), which a
    per-statement session would otherwise lose between statements. The
    session's authorizer governs it like any other statement."""

    def __init__(self, primary: str, *, attach: "tuple[str, ...]" = (),
                 row_factory: Any = None, startup: bool = True,
                 policy: "str | None" = None,
                 on_session: "Callable[[sqlite3.Connection], Any] | None"
                 = None):
        self._primary = primary
        self._attach = tuple(attach)
        self._policy = policy
        self._on_session = on_session
        self.row_factory = (row_factory if row_factory is not None
                            else (sqlite3.Row if primary == "stats"
                                  else None))
        self._unit: "tuple[Any, contextlib.ExitStack] | None" = None
        self._trace = None
        if startup:
            with command_session(primary, attach=self._attach,
                                 policy=policy):
                pass

    @property
    def in_transaction(self) -> bool:
        return self._unit is not None

    def _session(self, stack: contextlib.ExitStack):
        session = stack.enter_context(command_session(
            self._primary, attach=self._attach, startup=False,
            row_factory=self.row_factory, policy=self._policy))
        if self.row_factory is None:
            session.conn.row_factory = None
        if self._trace is not None:
            session.conn.set_trace_callback(self._trace)
        if self._on_session is not None:
            self._on_session(session.conn)
        return session

    def set_trace_callback(self, callback) -> None:
        """Traces every later statement, in whichever session runs it."""
        self._trace = callback
        if self._unit is not None:
            self._unit[0].conn.set_trace_callback(callback)

    @staticmethod
    def _verb(sql: str) -> str:
        head = sql.lstrip().split(None, 1)
        return head[0].rstrip(";").upper() if head else ""

    def execute(self, sql, parameters=()):
        verb = self._verb(sql)
        if verb == "BEGIN":
            if self._unit is not None:
                raise sqlite3.OperationalError(
                    "cannot start a transaction within a transaction")
            stack = contextlib.ExitStack()
            try:
                session = self._session(stack)
            except BaseException:
                stack.close()
                raise
            self._unit = (session, stack)
            return _MaterializedCursor([], None)
        if verb in ("ROLLBACK", "COMMIT", "END"):
            if self._unit is None:
                raise sqlite3.OperationalError(
                    "cannot %s - no transaction is active" % verb.lower())
            self._end_unit()
            return _MaterializedCursor([], None)
        if self._unit is not None:
            conn = self._unit[0].conn
            conn.row_factory = self.row_factory
            cur = conn.execute(sql, parameters)
            return _MaterializedCursor(cur.fetchall(), cur.description)
        with contextlib.ExitStack() as stack:
            cur = self._session(stack).conn.execute(sql, parameters)
            rows = cur.fetchall()
            description = cur.description
        return _MaterializedCursor(rows, description)

    def _end_unit(self) -> None:
        unit, self._unit = self._unit, None
        if unit is not None:
            unit[1].close()

    def commit(self) -> None:
        """Ends an open read unit, as a connection's ``commit()`` ends its
        read transaction; nothing is written."""
        self._end_unit()

    def rollback(self) -> None:
        self._end_unit()

    def close(self) -> None:
        self._end_unit()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


#: Refusals a reader's base opener (`stats_open_guarded`, recovering, then
#: ``mode=ro``) read through and that the owner opener resolves: a
#: not-yet-converted WAL header or other unreadable file state, an
#: interrupted rebuild, a missing maintenance lock (item 6, AD2).
_READER_STARTUP_REASONS = frozenset({"unavailable", "recovery_pending",
                                     "absent"})
#: Refusals base's ``mode=ro`` open read as they were; the diagnostic policy
#: admits them read-only (5.12).
_READER_DIAGNOSTIC_REASONS = frozenset({"schema_behind", "schema_ahead"})
_READER_STARTUP_CAPABILITIES = ("WRITE", "STARTUP", "MAINTENANCE", "LAUNCH")


def reader_startup(store: str) -> "str | None":
    """The startup phase of a reader that runs no command startup of its own
    (`alerts`' label and test-alert account, `config`'s account-budget refs;
    spec AD2, Task 2 item 6; CLI `explain` before its query phase, revision
    31). Returns the policy to read `store` with.

    Effect-free first: a readable store is read as it is (obligations stay
    with the command openers, as before). Any refusal is then handed to
    `reader_retry`, whose policy is returned; when it declines, the reader
    reads with its default policy and keeps its existing handling."""
    try:
        with _session(store, policy="interactive"):
            return None
    except _cctally_core.StoreUnavailable as exc:
        reason = exc.reason
    return reader_retry(store, reason)[1]


def reader_retry(store: str, reason: str) -> "tuple[bool, str | None]":
    """What a reader does about a query refusal for `reason` that base's
    reader (a raw or guarded ``mode=ro`` open) read through (spec AD2; Task 2
    item 6, I2f item 1). Returns ``(retry, policy)``: whether to open the
    session again, and with which policy (``None``: the default).

    A schema-behind or -ahead store is read read-only under the diagnostic
    policy, as base's ``mode=ro`` open read it: running the owner opener
    there would start the epoch rebuild and refuse the read. It needs the
    diagnostic capabilities, which a `query`-ceiling caller (an HTTP read
    route) or a thread with no authority lacks; such a caller keeps the
    refusal. Any other refusal the
    owner opener resolves (a WAL-header or otherwise unreadable stats file,
    an interrupted rebuild, a missing lock beside an existing store; never
    a missing store, which a reader does not create) runs the existing full
    opener
    when the caller holds the authority to (the `process` capabilities),
    then reads the established store. Corruption, maintenance and busy are
    the reader's existing handling, as is a deferral the opener raises (the
    read is then retried and refused again)."""
    def holds(*capabilities):
        # An established authority only: a thread or worker with none (a
        # dashboard diagnosis worker) never converts a store or widens.
        return (_cctally_core.current_authority() is not None
                and all(_cctally_core.has_capability(c) for c in capabilities))

    if reason in _READER_DIAGNOSTIC_REASONS:
        if holds("METADATA"):
            return True, "diagnostic"
        return False, None
    if reason == "absent" and not _store_db_path(store).is_file():
        # A missing lock beside an existing store is the owner's to create;
        # a missing store is not created by a reader (base reported it).
        return False, None
    if reason in _READER_STARTUP_REASONS and holds(
            *_READER_STARTUP_CAPABILITIES):
        try:
            StartupService.ensure(store)
        except _db().StatsRebuildDeferred:
            pass
        return True, None
    return False, None


def statement_reader(primary: str, **kwargs) -> StatementReader:
    """A `StatementReader` over `primary` (spec AD21)."""
    return StatementReader(primary, **kwargs)
