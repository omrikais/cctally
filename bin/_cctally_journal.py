"""Journal I/O glue — append surface (spec §4.3).

The pure line codec / identity / segment rules live in ``_lib_journal``; this
module owns the *durable* side: appending a fully-encoded line to the current
month's segment under the leaf flock, with torn-tail repair and fsync
discipline, plus the high-water snapshot and segment listing the single-flight
ingester (Task 4+) consumes.

Lock discipline (spec §4.3 / §5.2 lock-order law):

- ``journal.lock`` is a **leaf** blocking exclusive flock, held for
  microseconds. No other lock, flock, or SQLite transaction is ever acquired
  while it is held — it may therefore be taken from inside any context
  (including under a provider flock) without ordering hazards.
- The appender never reads, seeks, or rewrites the segment beyond the
  bounded torn-tail repair: read the final byte; if it is not ``\\n`` the
  previous appender crashed mid-write, so scan back within a 64 KiB window to
  the last complete line and ``ftruncate`` to it before appending. A crash can
  therefore only ever leave a torn *final* line, which the next append heals.

Path constants (``JOURNAL_DIR``, ``JOURNAL_LOCK_PATH``) are read from
``_cctally_core`` at call time so dev/data-dir redirection and test isolation
apply. Permissions match the hardened DB sidecars: ``0o700`` dir, ``0o600``
segment files.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field, replace as _dc_replace

import _cctally_core
import _lib_accounts
import _lib_journal
import _lib_record


# Torn-tail scan window (spec §4.3). A single line must fit inside it — a
# longer line is a hard error by construction, which is what keeps torn-tail
# repair bounded (the previous newline is guaranteed to fall inside the window).
_TAIL_WINDOW = 64 * 1024
_MAX_LINE_BYTES = _TAIL_WINDOW

# Process-local acceleration for the durable Codex-quota dedupe index. The
# compact index is re-derivable from the journal and lets short-lived hook
# processes load ~20-byte natural-key digests instead of rescanning every full
# JSONL segment.
_QUOTA_DEDUP_INDEX_NAME = ".quota-observation-keys"
_QUOTA_DEDUP_DIR: str | None = None
_QUOTA_DEDUP_KEYS: set[str] = set()
_QUOTA_DEDUP_LOADED = False
_HIGH_WATER_UNSET = object()


class JournalError(Exception):
    """A structural journal-append failure (line too long, unrepairable tail)."""


class CorrectionRebuildRequired(JournalError):
    """A completed correction cannot be applied incrementally to a live index.

    The recovery boundary needs more than a message: it must rebuild through
    the exact completed-batch commit that triggered the mismatch, then
    revalidate the exact effective metadata under exclusive ownership.
    """

    def __init__(
        self,
        message,
        *,
        batch_id=None,
        event_id=None,
        high_water=None,
        expected_metadata=None,
        recovery_eligible=False,
    ):
        super().__init__(message)
        self.batch_id = batch_id
        self.event_id = event_id
        self.high_water = high_water
        self.expected_metadata = expected_metadata
        self.recovery_eligible = recovery_eligible


class CorrectionRecoveryError(JournalError):
    """Bounded correction recovery could not safely replace the live index."""


# --------------------------------------------------------------------------
# leaf lock
# --------------------------------------------------------------------------

def _acquire_leaf_lock() -> int:
    """Open + blocking-EX-flock ``journal.lock``; return the held fd.

    LEAF: the caller must acquire no other lock while this is held."""
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_cctally_core.JOURNAL_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_leaf_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# low-level helpers
# --------------------------------------------------------------------------

def _write_all(fd: int, data: bytes) -> None:
    """Write ``data`` fully, looping on partial writes and EINTR."""
    view = memoryview(data)
    total = 0
    n = len(data)
    while total < n:
        try:
            written = os.write(fd, view[total:])
        except InterruptedError:  # pragma: no cover — PEP 475 retries most EINTR
            continue
        total += written


def _fsync_dir(path) -> None:
    """fsync a directory so a newly-created child entry is durable."""
    dfd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _repair_torn_tail(fd: int) -> None:
    """Heal a torn final line under the lock (spec §4.3).

    Fast path (spec §4.3): ``pread`` just the file's FINAL byte first. On the
    hot append path the segment is already newline-terminated, so this avoids
    the 64 KiB window read on every single append — the common case is one
    1-byte read, not a 64 KiB read.

    Only when that final byte is not ``\\n`` did the previous appender crash
    mid-write: read the final ≤64 KiB window and ``ftruncate`` back to the last
    complete line. A window with no newline at all means either an empty file
    with a single incomplete first line (truncate to 0) or — if the window is a
    full 64 KiB — a line longer than the scan window, which is a hard error."""
    size = os.fstat(fd).st_size
    if size == 0:
        return
    if os.pread(fd, 1, size - 1) == b"\n":
        return
    window = min(size, _TAIL_WINDOW)
    chunk_start = size - window
    chunk = os.pread(fd, window, chunk_start)
    valid = _lib_journal.valid_tail_offset(chunk, chunk_start)
    if valid <= chunk_start and chunk_start > 0:
        raise JournalError(
            "torn journal tail exceeds the 64 KiB scan window "
            "(a single line must fit the window, spec §4.3)")
    os.ftruncate(fd, valid)


# --------------------------------------------------------------------------
# public append surface
# --------------------------------------------------------------------------

def _is_codex_quota_obs(record: dict) -> bool:
    return (
        record.get("t") == "obs"
        and record.get("provider") == "codex"
        and (record.get("payload") or {}).get("kind") == "quota_window_snapshot"
    )


def _codex_quota_natural_key(record: dict) -> str | None:
    """Digest the cache table's stable UNIQUE key for one quota observation."""
    if not _is_codex_quota_obs(record):
        return None
    payload = record.get("payload") or {}
    source = payload.get("source")
    source_path = payload.get("source_path")
    line_offset = payload.get("line_offset")
    logical_limit_key = payload.get("logical_limit_key")
    if (
        not isinstance(source, str)
        or not isinstance(source_path, str)
        or not isinstance(line_offset, int)
        or not isinstance(logical_limit_key, str)
    ):
        return None
    return _lib_journal.content_id({
        "t": "quota-natural-key",
        "payload": {
            "source": source,
            "source_path": source_path,
            "line_offset": line_offset,
            "logical_limit_key": logical_limit_key,
        },
    })


def _load_quota_dedup_keys() -> None:
    """Load or atomically rebuild the re-derivable quota natural-key index.

    Caller owns journal.lock, so an initial full scan observes one stable
    journal prefix and only one process can publish the compact index. Normal
    quota appends fsync the journal first and this index second; a crash in
    between can cause at most one harmless duplicate on retry, never a skipped
    durable observation.
    """
    global _QUOTA_DEDUP_DIR, _QUOTA_DEDUP_LOADED
    journal_dir = _cctally_core.JOURNAL_DIR
    dir_key = str(journal_dir)
    if _QUOTA_DEDUP_DIR != dir_key:
        _QUOTA_DEDUP_DIR = dir_key
        _QUOTA_DEDUP_KEYS.clear()
        _QUOTA_DEDUP_LOADED = False
    if _QUOTA_DEDUP_LOADED:
        return

    index_path = journal_dir / _QUOTA_DEDUP_INDEX_NAME
    try:
        raw_keys = index_path.read_text(encoding="ascii").splitlines()
        if any(not item.startswith("o:") or len(item) != 18 for item in raw_keys):
            raise ValueError("invalid quota dedupe index")
        _QUOTA_DEDUP_KEYS.update(raw_keys)
        _QUOTA_DEDUP_LOADED = True
        return
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        _QUOTA_DEDUP_KEYS.clear()

    for name in list_segments():
        with (journal_dir / name).open("rb") as fh:
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break
                decoded = _lib_journal.decode_line(raw)
                if decoded is not None:
                    natural_key = _codex_quota_natural_key(decoded)
                    if natural_key is not None:
                        _QUOTA_DEDUP_KEYS.add(natural_key)

    tmp = index_path.with_name(
        f"{index_path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    )
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = "".join(
                f"{natural_key}\n" for natural_key in sorted(_QUOTA_DEDUP_KEYS)
            ).encode("ascii")
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(index_path))
        _fsync_dir(journal_dir)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    _QUOTA_DEDUP_LOADED = True


def _append_quota_dedup_key(natural_key: str) -> None:
    """Journal-first second leg: append+fsync one key to the compact index."""
    index_path = _cctally_core.JOURNAL_DIR / _QUOTA_DEDUP_INDEX_NAME
    fd = os.open(str(index_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _write_all(fd, f"{natural_key}\n".encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _QUOTA_DEDUP_KEYS.add(natural_key)


def append_record(
    record: dict,
    *,
    now_utc: dt.datetime | None = None,
    dedupe_codex_quota: bool = False,
) -> tuple[str, int] | None:
    """Append one encoded line to the current UTC month's segment.

    Implements spec §4.3 exactly: ``O_RDWR|O_APPEND|O_CREAT`` (0o600) → blocking
    leaf flock → torn-tail repair → loop-write the full line → ``fsync(fd)`` →
    parent-dir fsync on first segment/dir creation → unlock.

    ``now_utc`` selects the segment (defaults to the current UTC time).
    ``dedupe_codex_quota`` skips a retained Codex quota obs whose table natural
    key already exists in any segment; this is the cache-recovery replay path
    and returns ``None`` on a skip. Otherwise returns ``(segment_basename,
    end_offset)`` where ``end_offset`` is the file size just past the appended
    line — the byte position the ingest cursor advances to when it consumes
    this line."""
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    data = _lib_journal.encode_line(record)
    if len(data) > _MAX_LINE_BYTES:
        raise JournalError(
            f"journal line is {len(data)} bytes, exceeds the "
            f"{_MAX_LINE_BYTES}-byte limit (spec §4.3)")

    journal_dir = _cctally_core.JOURNAL_DIR
    seg_name = _lib_journal.segment_name(now_utc)
    seg_path = journal_dir / seg_name

    dir_created = not journal_dir.exists()
    journal_dir.mkdir(parents=True, exist_ok=True)
    if dir_created:
        try:
            os.chmod(journal_dir, 0o700)
        except OSError:
            pass

    lock_fd = _acquire_leaf_lock()
    try:
        if dedupe_codex_quota:
            if not _is_codex_quota_obs(record):
                raise ValueError(
                    "dedupe_codex_quota is valid only for Codex quota obs"
                )
            _load_quota_dedup_keys()
            natural_key = _codex_quota_natural_key(record)
            if natural_key is None:
                raise ValueError(
                    "dedupe_codex_quota requires a complete quota natural key"
                )
            if natural_key in _QUOTA_DEDUP_KEYS:
                return None

        seg_created = not seg_path.exists()
        fd = os.open(str(seg_path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if seg_created:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            _repair_torn_tail(fd)
            _write_all(fd, data)
            os.fsync(fd)
            end_offset = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if dedupe_codex_quota:
            _append_quota_dedup_key(natural_key)
        # Durably record new directory entries (spec §4.3: fsync the parent
        # directory on first creation of a segment or the journal dir).
        if seg_created:
            _fsync_dir(journal_dir)
        if dir_created:
            _fsync_dir(journal_dir.parent)
        return (seg_name, end_offset)
    finally:
        _release_leaf_lock(lock_fd)


def append_records(
    records: list[dict],
    *,
    now_utc: dt.datetime | None = None,
    expected_high_water=_HIGH_WATER_UNSET,
    line_hook=None,
) -> tuple[str, int]:
    """Append one ordered record group under a single leaf-lock hold.

    The group is not transactionally atomic across a power loss: a crash can
    leave complete prefix lines plus one torn final line, exactly like the
    single-record appender. It *is* non-interleavable with other appenders, so a
    correction batch remains physically ordered. ``expected_high_water`` is
    checked while holding the same leaf lock that performs the append, closing
    the plan/revalidate/append race.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("journal record group must be a non-empty list")
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    encoded = []
    for record in records:
        data = _lib_journal.encode_line(record)
        if len(data) > _MAX_LINE_BYTES:
            raise JournalError(
                f"journal line is {len(data)} bytes, exceeds the "
                f"{_MAX_LINE_BYTES}-byte limit (spec §4.3)"
            )
        encoded.append(data)

    journal_dir = _cctally_core.JOURNAL_DIR
    seg_name = _lib_journal.segment_name(now_utc)
    seg_path = journal_dir / seg_name
    dir_created = not journal_dir.exists()
    journal_dir.mkdir(parents=True, exist_ok=True)
    if dir_created:
        try:
            os.chmod(journal_dir, 0o700)
        except OSError:
            pass

    lock_fd = _acquire_leaf_lock()
    try:
        segments = list_segments()
        actual_high_water = None
        if segments:
            latest = segments[-1]
            actual_high_water = (
                latest,
                os.stat(journal_dir / latest).st_size,
            )
        if (
            expected_high_water is not _HIGH_WATER_UNSET
            and actual_high_water != expected_high_water
        ):
            raise JournalError(
                "journal high-water changed before correction append "
                f"(expected {expected_high_water!r}, found {actual_high_water!r})"
            )

        seg_created = not seg_path.exists()
        fd = os.open(str(seg_path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if seg_created:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            _repair_torn_tail(fd)
            for index, data in enumerate(encoded, start=1):
                _write_all(fd, data)
                if line_hook is not None:
                    line_hook(index)
            os.fsync(fd)
            end_offset = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if seg_created:
            _fsync_dir(journal_dir)
        if dir_created:
            _fsync_dir(journal_dir.parent)
        return (seg_name, end_offset)
    finally:
        _release_leaf_lock(lock_fd)


def list_segments() -> list[str]:
    """Journal segment basenames in canonical order (spec §4.1): bootstrap
    segments first, then observation segments, each class lexicographic.
    Excludes ``.partial`` cutover files and any non-segment entries."""
    journal_dir = _cctally_core.JOURNAL_DIR
    if not journal_dir.exists():
        return []
    names = []
    for entry in journal_dir.iterdir():
        name = entry.name
        if not name.endswith(".jsonl"):
            continue
        if not (name.startswith(_lib_journal.BOOTSTRAP_PREFIX)
                or name.startswith(_lib_journal.SEGMENT_PREFIX)):
            continue
        if entry.is_file():
            names.append(name)
    return sorted(names, key=_lib_journal.segment_sort_key)


def _has_retained_journal_bytes(segment_sizes) -> bool:
    """Whether any canonical journal segment retains replayable bytes."""
    return any(int(size) > 0 for size in segment_sizes)


def _journal_rebuild_snapshot() -> tuple[tuple[str, int] | None, bool]:
    """Snapshot the canonical high-water and retained-byte truth together.

    A freshly created newest segment can legitimately be empty while older
    immutable segments still contain the durable rebuild source.  Holding the
    leaf lock across both facts keeps the epoch resolver from making its
    fail-closed decision against two different journal states.
    """
    lock_fd = _acquire_leaf_lock()
    try:
        segments = list_segments()
        if not segments:
            return None, False
        sizes = [
            os.stat(_cctally_core.JOURNAL_DIR / segment).st_size
            for segment in segments
        ]
        return (
            (segments[-1], sizes[-1]),
            _has_retained_journal_bytes(sizes),
        )
    finally:
        _release_leaf_lock(lock_fd)


def journal_high_water() -> tuple[str, int] | None:
    """Snapshot ``(latest segment basename, size)`` under a µs leaf-lock hold.

    "Latest" is the canonically-last segment (spec §4.1 order). The ingest
    cycle takes this snapshot and consumes only ``cursor → HW`` so a line
    appended after the snapshot belongs to the next cycle (spec §5.2.1).
    Returns ``None`` when no segment exists yet."""
    high_water, _has_bytes = _journal_rebuild_snapshot()
    return high_water


# ==========================================================================
# Single-flight ingest cycle (spec §5.1 / §5.2, revision 3)
# ==========================================================================
#
# `run_stats_ingest` is the sole stats.db writer (spec §5.1). One cycle
# consumes `cursor -> HW` in canonical `(segment, offset)` order. The rev-3
# structure runs derivation INSIDE the index transaction (the reused Task-5
# chokepoints write rows directly on the connection), then journals the derived
# facts inside the same transaction (`journal.lock` is a leaf, legal here —
# every evt append is fsync'd BEFORE the commit that indexes it). Per cycle,
# under the ingest lock (spec §5.2):
#
#   1. HW snapshot (leaf lock, µs).                         journal_high_water()
#   2. read+decode cursor -> HW, counting malformed.        _read_range()
#   3. cache leg (Codex quota + attribution) before stats.  CACHE_APPLIER seam
#   4. ONE `BEGIN IMMEDIATE`:
#        a. replay journal evt lines (apply-only, NO alerts).  _apply_evt()
#        b. per-record sequential PIPELINE over obs/op.        PIPELINE hooks
#        c. journal derived facts: Model-A emission + harvest. emit_model_a() /
#                                                              _harvest()
#        d. advance the cursor.                                _write_cursor()
#   5. COMMIT (journal-first: every evt fsync'd before this).
#   6. post-commit alert dispatch from the step-4b sink.    ALERT_DISPATCHER
#
# Seams later implementors (6b / Task 7) wire on top of this machinery:
#   PIPELINE         list of (ctx, record) -> None hooks; sequential, in-txn.
#                    6b appends the obs-derivation hooks (snapshot_accept,
#                    milestones, resets/credits, cost snapshots, budgets); the
#                    built-in `_pipeline_op_weekly_credit_floor` op fold ships
#                    here (spec §5.3 "fold op").
#   CACHE_APPLIER    the composite Codex cache leg (Task 7; #416 widened it from
#                    quota-only to quota + `codex_file_account` attribution ops,
#                    wired to `_cache_applier` below; `QUOTA_APPLIER` remains as
#                    its back-compat alias). Contract: (decoded) -> stop_index |
#                    None. `decoded` is the ordered list of (record, segment,
#                    offset); a non-None int is a prefix-stop boundary (busy
#                    global or Codex cache writer flock, or an incomplete cache
#                    write): the cycle processes decoded[:stop] and advances the
#                    cursor to decoded[stop]'s offset (spec §5.2 step 3). ONE
#                    applier, ONE `BEGIN IMMEDIATE`, ONE stop across BOTH
#                    families — see the #416 review-F1 note at `_cache_applier`
#                    for why a second independent applier is unsafe here.
#                    Always-on: a Claude-only batch scans + returns None.
#   codex_apply      per-cycle `(ctx) -> None` closure (Task 7, a `run_stats_
#                    ingest` arg, not a module global) run in step 4b'' on
#                    ctx.conn — the seam every Codex on-demand stats.db writer
#                    routes through: the quota projection re-materializer
#                    (`reconcile_codex_quota_projection`) and the on-demand codex
#                    budget/projected firings. Its harvest-family crossings are
#                    journaled by step 4c; its alerts ride ctx.pending_alerts.
#   ALERT_DISPATCHER post-commit dispatch override (None -> _dispatch_pending
#                    _alerts). Consumes ctx.pending_alerts, populated by step-4b
#                    pipeline hooks AND the step-4b'' codex_apply leg — step-4a
#                    replay has no sink access.
#
# Lock-order law (spec §5.2): the ingest lock is acquired BEFORE any SQLite
# transaction and BEFORE the leaf `journal.lock`; it is never taken while
# holding a provider flock. The quota cache leg + its provider flock run BEFORE
# the stats `BEGIN IMMEDIATE`. Inside the txn the only leaf-lock holds are the
# discrete evt appends (emit_model_a / _harvest -> append_record); `journal.lock`
# is a leaf and may be taken inside a transaction — it never spans the commit.

PIPELINE: list = []
QUOTA_APPLIER = None
ALERT_DISPATCHER = None
FOLD_APPLIERS: dict = {}


@dataclass
class IngestResult:
    """Outcome of one `run_stats_ingest` call."""

    ran: bool                 # False when the lock was busy (opportunistic)
    consumed: int             # decoded records processed this cycle (obs/op/evt)
    malformed: int            # lines in range that failed to decode (spec §4.4)
    events_emitted: int       # evt lines emitted this cycle (Model-A + harvest)
    alerts: list              # alert payloads dispatched post-commit (step 6)
    # #374: same-revision divergences handled this cycle — emissions withheld at
    # the write boundary plus journal evts the preflight reader quarantined.
    conflicts_dropped: int = 0
    # Exception discipline (6b-gate P2): the exception that aborted the cycle on
    # an OPPORTUNISTIC ingest — the txn rolled back, the cursor did NOT advance
    # (invariant ii), and `run_stats_ingest` logged it loudly and returned
    # `ran=True, error=<exc>` rather than break a statusline/hook tick. `None`
    # on a clean cycle. Authoritative callers never see this — their cycle
    # exception propagates.
    error: object = None


@dataclass
class IngestContext:
    """Per-cycle context handed to every PIPELINE hook (spec §5.2 step 4b).

    `as_of_for(record)` is the capture-time-pure clock: a hook injects the
    record's own `at` wherever the live code would consult wall time, so replay
    is deterministic. `config` is read ONCE per cycle (only when the batch is
    non-empty). `pending_alerts` is the post-commit dispatch SINK: a hook that
    fires an alert appends its payload here, and step 6 dispatches them after the
    commit. Replay (step 4a) folds evt lines with NO ctx, so it is structurally
    unable to add to the sink. `events_emitted` counts the evt lines this cycle
    journaled (Model-A `emit_model_a` + harvest).
    """

    conn: sqlite3.Connection
    batch: list                     # decoded obs/op records this cycle
    config: object = None
    pending_alerts: list = field(default_factory=list)
    events_emitted: int = 0
    # Design B (DB journal redesign §5.3 event+effects): the per-cycle
    # suppression map a reset/credit pipeline hook populates BEFORE it runs its
    # stale-replica DELETE, keyed on the harvest natural-key parts of the reset
    # it just inserted — `(old_week_end_at, new_week_end_at)` for a
    # `week_reset_events` row, `(five_hour_window_key, effective_reset_at_utc)`
    # for a `five_hour_reset_events` row. The value is the list of logical
    # `journal_id`s the DELETE will hit. `_build_harvest_evt` reads it back and
    # attaches the list to the reset's harvest evt payload, so the destructive
    # effect replays deterministically (idempotent against absent ids). The
    # hook captures BEFORE deleting and ONLY on the genuine-new-reset winner
    # (reset INSERT OR IGNORE rowcount == 1), so a crash-replayed reset never
    # re-suppresses with a divergent list.
    suppression_map: dict = field(default_factory=dict)
    # Task B rederive seam. Normal ingest leaves both defaults unchanged.
    # A scratch planner supplies an in-memory sink so derived events are captured
    # instead of appended to the durable journal, and disables projection-file
    # writes while still exercising the same SQLite derivation/fold code.
    event_sink: "list | None" = None
    projection_writes: bool = True
    # Scratch replay reconstructs ephemeral marker state in memory. A planner
    # shares this dict across its per-record contexts.
    projection_state: dict = field(default_factory=dict)
    # #374 write boundary: emissions WITHHELD this cycle because they would have
    # violated the same-revision rule. Each entry is a `DroppedConflict`; the row
    # was converged from the already-journaled effective event instead.
    conflicts_dropped: list = field(default_factory=list)

    def as_of_for(self, record: dict) -> str:
        return record["at"]


@dataclass(frozen=True)
class DroppedConflict:
    """One live emission withheld at the write boundary (#374 §6).

    The journal is append-only, so a divergent line can never be un-written —
    the only durable defence is to never append it. The row is converged from
    the effective event instead, and the rejected content is reported here.
    """

    event_id: str
    rev: int
    rejected_hash: str


@dataclass(frozen=True)
class _EvtSpec:
    """How to fold one evt `kind` into its target table (step 4a replay + the
    Model-A `emit_model_a` apply path — one applier, two callers, so live-emit
    and crash-replay converge by construction).

    `fk_refs` maps a payload key carrying a *logical* id to `(column,
    ref_table)`: the fold resolves the logical id to the rebuilt DB's actual
    rowid via `_resolve_ref` (spec §4.2 FK rule). Everything else in the payload
    maps mechanically to a same-named column. `order` sequences folds so a
    referenced family (snapshots, resets, blocks) folds before a referencing one
    (milestones) — the FK-resolution dependency order (spec Appendix B I4 P2-8).
    `applier`, when set, overrides the generic column-map fold (weekly credit
    effects + block-close children need bespoke logic).
    """

    table: "str | None"
    fk_refs: dict = field(default_factory=dict)
    order: int = 60
    applier: object = None
    # A FK column that is NOT journaled as a logical id but RE-DERIVED at fold
    # time from another (journaled) natural-key column, keyed `column ->
    # (ref_table, lookup_column)`: `column = SELECT id FROM ref_table WHERE
    # lookup_column = payload[lookup_column]`. Used for a FK into a MUTABLE
    # PROJECTION that may have no `journal_id` (spec §5.3): `five_hour_milestones.
    # block_id` points at the OPEN five_hour_blocks row (a projection,
    # re-materialized on rebuild, never journaled), so it cannot carry a stable
    # logical id — but it is recoverable from the milestone's own
    # `five_hour_window_key`, which IS journaled.
    derived_fk: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _HarvestSpec:
    """How to harvest one natural-keyed family (spec §5.3): after the pipeline,
    every row `WHERE journal_id IS NULL` is a this-cycle insert — build its evt
    with logical-FK refs (reverse lookup rowid -> journal_id), append+fsync, and
    stamp `journal_id`.

    `id_prefix` is the opaque evt-id prefix (`pm`, `wr`, …) — deliberately NOT
    the fold `kind` (`percent_milestone`, `week_reset`, …); the id is an opaque
    token, never parsed (spec §5.3 / Appendix B I4 P3-11). `id_parts` is the
    ordered list of columns whose values follow the prefix; a column that is
    also an FK ref contributes its *logical* id. `fk_refs` maps an FK column to
    `(ref_table, payload_ref_key)` — the reverse of `_EvtSpec.fk_refs`.
    `at_column` supplies the evt `at`. `order` harvests referenced families
    before referencing ones. `closed_only` scopes the scan to `is_closed = 1`
    (five_hour_blocks). `children` embeds rollup children into the payload
    (five_hour_blocks' `_models`/`_projects`).
    """

    table: str
    kind: str
    id_prefix: str
    id_parts: tuple
    fk_refs: dict = field(default_factory=dict)
    at_column: "str | None" = None
    order: int = 60
    closed_only: bool = False
    children: tuple = ()
    # A FK column re-derived at fold from a journaled natural-key column instead
    # of a logical id, keyed `column -> (ref_table, lookup_column)` (see
    # `_EvtSpec.derived_fk`). The harvest EXCLUDES these columns from the evt
    # payload (the raw rowid is not stable) — the lookup column carries the info.
    derived_fk: dict = field(default_factory=dict)
    # Design B (event+effects): when True this family's harvest evt carries a
    # `suppression` list (logical ids of `weekly_usage_snapshots` rows the reset
    # deleted) that the fold applier replays. `_build_harvest_evt` sources the
    # list from `ctx.suppression_map` keyed on this spec's `id_parts` values.
    suppression: bool = False


# --------------------------------------------------------------------------
# ingest lock (spec §5.1: opportunistic NB / authoritative bounded-blocking)
# --------------------------------------------------------------------------

def _acquire_ingest_lock(mode: str, timeout_s: float) -> int | None:
    """Acquire `journal.ingest.lock`; return the held fd or None (busy).

    Opportunistic → single non-blocking attempt (busy = None). Authoritative →
    poll LOCK_NB up to `timeout_s` (a bounded blocking wait; None on timeout).
    """
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_cctally_core.JOURNAL_INGEST_LOCK_PATH),
                 os.O_RDWR | os.O_CREAT, 0o600)
    if mode == "opportunistic":
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
    deadline = time.monotonic() + timeout_s
    # Wrap the whole poll body so the lock fd cannot leak if the wait is
    # interrupted mid-sleep (KeyboardInterrupt / any non-BlockingIOError raised
    # by flock or time.sleep): close the fd on any escaping exception path. The
    # busy-timeout branch closes + returns before this handler can fire, and a
    # successful acquire returns fd, so the fd is closed exactly once.
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


def _release_ingest_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _acquire_maintenance_shared(mode: str, timeout_s: float) -> int | None:
    """Acquire the stats maintenance lock shared, before the ingest lock."""
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    if mode == "opportunistic":
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            _cctally_core.note_stats_maintenance_acquired()
            return fd
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                _cctally_core.note_stats_maintenance_acquired()
                return fd
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


def _acquire_maintenance_exclusive(mode: str, timeout_s: float) -> int | None:
    """Acquire the stats maintenance lock exclusively for legacy cutover."""
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    if mode == "opportunistic":
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _cctally_core.note_stats_maintenance_acquired()
            return fd
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                _cctally_core.note_stats_maintenance_acquired()
                return fd
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise


def _release_maintenance_shared(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        # #386: paired with the note in both acquire helpers. `open_db()` takes
        # this same lock SHARED on a fresh fd, and flock conflicts are
        # process-wide across descriptions, so the legacy/fresh ingest branch
        # (which holds it EXCLUSIVE across its `open_db()`) would self-deadlock
        # without the re-entrancy signal.
        _cctally_core.note_stats_maintenance_released()
        os.close(fd)


def _downgrade_maintenance_shared(fd: int) -> None:
    """Atomically downgrade a held maintenance lock from EX to SH."""
    fcntl.flock(fd, fcntl.LOCK_SH)


def _stats_db_identity():
    """Return the current stats main-file identity, or ``None`` if absent."""
    try:
        stat = os.stat(_cctally_core.DB_PATH)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _stats_db_user_version() -> int | None:
    """Read the main file's raw epoch without invoking schema or heal paths."""
    try:
        conn = sqlite3.connect(
            f"file:{_cctally_core.DB_PATH}?mode=ro",
            uri=True,
        )
    except sqlite3.Error:
        return None
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# --------------------------------------------------------------------------
# cursor (spec §5.2.2: segment-aware, prior-month tails covered)
# --------------------------------------------------------------------------

def _read_cursor(conn: sqlite3.Connection) -> tuple[str, int] | None:
    """Return `(segment_basename, offset)` from `journal_cursor`, or None when
    nothing has been consumed yet (start of the first segment).

    ``applied_segment`` / ``applied_offset`` are the trusted duplicate written
    in the same stats transaction as every materialized row (#410 Task B). A
    cursor-only hand edit can therefore no longer skip durable events and make
    their natural keys appear new: on disagreement, resume from the last
    atomically applied prefix and let the normal replay heal both pairs."""
    row = conn.execute(
        "SELECT segment, offset, applied_segment, applied_offset "
        "FROM journal_cursor WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    public = (str(row[0]), int(row[1]))
    if row[2] is None or row[3] is None:
        raise JournalError(
            "journal cursor applied-prefix guard is incomplete; "
            "run cctally db rebuild --db stats"
        )
    applied = (str(row[2]), int(row[3]))
    if public != applied:
        print(
            f"[journal] cursor-only advancement detected: public={public!r}, "
            f"applied={applied!r}; replaying from the applied prefix",
            file=sys.stderr,
        )
    return applied


def _write_cursor(conn: sqlite3.Connection, segment: str, offset: int) -> None:
    conn.execute(
        "INSERT INTO journal_cursor "
        "(id, segment, offset, applied_segment, applied_offset) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET segment = excluded.segment, "
        "offset = excluded.offset, "
        "applied_segment = excluded.applied_segment, "
        "applied_offset = excluded.applied_offset",
        (segment, offset, segment, offset),
    )


_SEGMENT_READ_CHUNK = 256 * 1024


def _iter_segment_lines(seg_path, lo: int, hi: int):
    """Stream `(basename, absolute-offset, raw-line-without-newline)` for every
    complete line in `[lo, hi)`, holding at most one chunk plus one partial line
    in memory. `hi` is a line boundary (a HW snapshot size or an immutable prior
    segment's full size), so no partial trailing line appears."""
    name = seg_path.name
    with open(seg_path, "rb") as fh:
        fh.seek(lo)
        pos = lo
        buf = b""
        buf_at = lo
        while pos < hi:
            data = fh.read(min(_SEGMENT_READ_CHUNK, hi - pos))
            if not data:
                break
            pos += len(data)
            buf = buf + data if buf else data
            start = 0
            while True:
                nl = buf.find(b"\n", start)
                if nl == -1:
                    break
                yield (name, buf_at + start, buf[start:nl])
                start = nl + 1
            if start:
                buf = buf[start:]
                buf_at += start


def _read_segment_lines(seg_path, lo: int, hi: int) -> list[tuple[str, int, bytes]]:
    """Materialized form of :func:`_iter_segment_lines` (see it for the
    contract). Callers that walk a whole range at once should prefer
    :func:`iter_range`; this list form is retained for the ingest cycle, which
    needs the batch as an indexable sequence."""
    return list(_iter_segment_lines(seg_path, lo, hi))


def iter_range(cursor, hw):
    """Stream `cursor -> HW` across segments in canonical order (spec §5.2.2).

    Prior segments (before HW's) are immutable and read to their full size;
    HW's segment is read only up to the snapshot size, so appends past HW
    belong to the next cycle.

    Streaming, not list-building: a caller that only needs to fold each record
    into a table (the Codex attribution rehydration) must not put a transient
    the size of the whole journal on the hot path. `_read_range` remains the
    materialized form for the ingest cycle, which genuinely needs the batch as
    an indexable sequence (prefix-stop indices address into it).
    """
    hw_seg, hw_size = hw
    segments = list_segments()
    if hw_seg not in segments:
        return
    hw_idx = segments.index(hw_seg)
    if cursor is None:
        start_idx, start_off = 0, 0
    else:
        cur_seg, cur_off = cursor
        if cur_seg in segments:
            start_idx, start_off = segments.index(cur_seg), cur_off
        else:
            start_idx, start_off = 0, 0
    for idx in range(start_idx, hw_idx + 1):
        seg = segments[idx]
        seg_path = _cctally_core.JOURNAL_DIR / seg
        lo = start_off if idx == start_idx else 0
        hi = hw_size if idx == hw_idx else os.path.getsize(seg_path)
        if lo >= hi:
            continue
        yield from _iter_segment_lines(seg_path, lo, hi)


def _read_range(cursor, hw) -> list[tuple[str, int, bytes]]:
    """Materialized `cursor -> HW` (see :func:`iter_range`)."""
    return list(iter_range(cursor, hw))


def journal_prefix_hash(high_water) -> "str | None":
    """Hash exact raw segment bytes through one canonical high-water."""
    if high_water is None:
        return None
    digest = hashlib.sha256()
    found = False
    for segment in list_segments():
        path = _cctally_core.JOURNAL_DIR / segment
        size = high_water[1] if segment == high_water[0] else path.stat().st_size
        data = path.read_bytes()[:size]
        if len(data) != size:
            raise OSError(f"journal segment changed while reading: {segment}")
        name = segment.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        if segment == high_water[0]:
            found = True
            break
    if not found:
        raise OSError(
            f"journal high-water segment is unavailable: {high_water[0]}"
        )
    return "sha256:" + digest.hexdigest()


def _capture_protocol_prefix_evidence(record, prior_high_water, evidence) -> None:
    """Capture the actual raw prefix immediately preceding one audit record."""
    if (
        record.get("t") == "op"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("kind")
        == _lib_journal._PROTOCOL_RESOLUTION_KIND
    ):
        evidence.append(
            (
                prior_high_water,
                journal_prefix_hash(prior_high_water),
            )
        )


# --------------------------------------------------------------------------
# cache leg — Codex quota obs -> cache.db quota_window_snapshots (spec §5.2
# step 3, Task 7 Item 2). Runs BEFORE the stats BEGIN IMMEDIATE, under the
# global cache writer flock followed by `cache.db.codex.lock` (lock-order law:
# flocks precede SQLite write transactions). The journal Codex quota obs are the
# DURABLE truth (§1 latent data-loss hole — the source rollout JSONL
# evaporates); this leg re-materializes the disposable cache.db index from them,
# idempotently
# (INSERT OR IGNORE on the natural key). Distinct from the direct cache write in
# `sync_codex_cache._write_codex_file_batch` (kept byte-identical, Item 1) — the
# two converge on the same rows.
# --------------------------------------------------------------------------

_QUOTA_OBS_KIND = "quota_window_snapshot"

_QUOTA_SNAPSHOT_INSERT_LEGACY = (
    "INSERT OR IGNORE INTO quota_window_snapshots "
    "(source, source_root_key, source_path, line_offset, captured_at_utc, "
    " observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
    " used_percent, resets_at_utc, plan_type, individual_limit_json, "
    " reached_type, observed_model, account_key) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_QUOTA_SNAPSHOT_INSERT = (
    "INSERT OR IGNORE INTO quota_window_snapshots "
    "(source, source_root_key, source_path, line_offset, captured_at_utc, "
    " observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
    " used_percent, resets_at_utc, plan_type, individual_limit_json, "
    " reached_type, observed_model, account_key, canonical_resets_at_utc) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_QUOTA_SNAPSHOT_COLS = (
    "source", "source_root_key", "source_path", "line_offset", "captured_at_utc",
    "observed_slot", "logical_limit_key", "limit_id", "limit_name",
    "window_minutes", "used_percent", "resets_at_utc", "plan_type",
    "individual_limit_json", "reached_type", "observed_model",
)


def _quota_snapshot_values(rec: dict, anchor: "str | None" = None) -> tuple:
    """Build the INSERT values tuple for one Codex quota obs. account_key (#341)
    rides the obs TOP-LEVEL ``account`` field (obs stamp shape), not the payload,
    so an unstamped/sentinel obs re-materializes cache.db with NULL account_key
    (``NULL ≡ unattributed`` on the read path). first-stamp-wins via the
    ``INSERT OR IGNORE`` natural key — account_key is a stamped attribute, never
    part of the identity.

    ``anchor`` is the #416 §4.2 canonical reset. It is NOT journaled: it is a
    property of the observation's POPULATION, not of the observation, so it is
    re-resolved by whichever writer materializes the row. NULL leaves every
    reader on the raw-reset fallback, i.e. exactly today's behaviour."""
    p = rec.get("payload") or {}
    return (
        tuple(p.get(col) for col in _QUOTA_SNAPSHOT_COLS)
        + (rec.get("account"), anchor)
    )


def _is_codex_quota_obs(rec: dict) -> bool:
    return (
        rec.get("t") == "obs"
        and rec.get("provider") == "codex"
        and (rec.get("payload") or {}).get("kind") == _QUOTA_OBS_KIND
    )


# --------------------------------------------------------------------------
# #416: the second family this leg carries — the durable Codex attribution
# decision (`codex_file_account` op, spec §3.3). It shares the leg rather than
# getting its own applier because `run_stats_ingest` invokes exactly ONE applier
# and truncates the batch afterwards, while an applier commits everything it
# handled BEFORE returning a stop index. Two independent prefix-stopping
# appliers could therefore commit past each other's stop, exposing suffix
# effects beyond the retained prefix and violating the scalar-cursor rule
# (docs/journal-gotchas.md; #416 review F1).
# --------------------------------------------------------------------------

_FILE_ACCOUNT_OP_KIND = "codex_file_account"
# Byte prefilter for the streamed replay: the canonical encoder is
# `json.dumps(..., ensure_ascii=False)`, which never escapes an ASCII token, so
# every genuine op carries the kind verbatim.
_FILE_ACCOUNT_KIND_MARKER = f'"{_FILE_ACCOUNT_OP_KIND}"'.encode("ascii")

# FIRST-WINS at a contended primary key (Slice 1 closeout review C1). Spec §3.3
# — "a mid-file account change appends a second range-qualified op; the first is
# never rewritten" — and §3.5 — "a genuine correction is expressed as an explicit
# new range decision, not by mutating history". Ops apply in journal order, so
# `DO NOTHING` retains the FIRST decision at that key.
#
# This reverses the fix-round's last-op-wins. The #374 concern that motivated it
# (a fold applier is an inserter, not a convergence operator) does not apply
# here, because the path that must converge — `cache-sync --rebuild` — runs
# `rehydrate_codex_file_accounts(authoritative=True)`, which DELETEs the table
# before replaying. After an authoritative clear, `DO NOTHING` is first-WINS on
# an empty table, not a no-op, so clear-then-replay still repairs a drifted row
# (pinned by `test_authoritative_replay_still_repairs_a_drifted_row`).
#
# Last-op-wins was actively wrong on the one path where duplicates are
# reachable: a failed rehydration lets the walk re-decide from the live
# `auth.json` and mint a second op at the same key (plan candidate 10). Under
# last-op-wins the documented remedy would then CEMENT that newer
# live-auth-derived value rather than restore the original — the inverse of
# acceptance criterion 4. The disagreement is REPORTED (see
# `_apply_file_account_records`) rather than applied silently.
#
# `OR IGNORE` is deliberate, exactly as on `_QUOTA_SNAPSHOT_UPSERT`: SQLite
# gives the named upsert clause precedence for the conflict it names, so the
# first-wins policy is unaffected, while a record violating some OTHER
# constraint is dropped instead of raising — an `IntegrityError` here would
# prefix-stop `_cache_applier`, and the scalar cursor could never advance past
# that record, wedging the whole journal ingest cycle for every provider.
_FILE_ACCOUNT_INSERT = (
    "INSERT OR IGNORE INTO codex_file_accounts "
    "(file_identity, incarnation, from_offset, root_scope, account_key, "
    " decided_at_utc) "
    "VALUES (?,?,?,?,?,?) "
    "ON CONFLICT(file_identity, incarnation, from_offset) DO NOTHING"
)

# The incarnation high-water rides the same replay. MAX-set, never an increment,
# so replaying the same op converges instead of drifting.
#
# `OR IGNORE` for the same reason its sibling carries it (closeout review C2):
# an `IntegrityError` raised HERE prefix-stops `_cache_applier` just as surely,
# and the scalar cursor then never advances past the record. The named upsert
# clause still takes precedence for the primary-key conflict, so the MAX-set
# convergence is unaffected. `_apply_file_account_records` additionally refuses
# to reach this statement for a record the map insert dropped, so the two guards
# are belt-and-suspenders over disjoint failure modes: this one covers any
# constraint the incarnation table might gain that the map table lacks.
_FILE_INCARNATION_INSERT = (
    "INSERT OR IGNORE INTO codex_file_incarnations "
    "(file_identity, incarnation, updated_at_utc) "
    "VALUES (?,?,?) "
    "ON CONFLICT(file_identity) DO UPDATE SET "
    "  incarnation = MAX(codex_file_incarnations.incarnation, excluded.incarnation), "
    "  updated_at_utc = excluded.updated_at_utc"
)


def _is_codex_file_account_op(rec: dict) -> bool:
    return (
        rec.get("t") == "op"
        and (rec.get("payload") or {}).get("kind") == _FILE_ACCOUNT_OP_KIND
    )


def _file_account_values(rec: dict) -> tuple:
    """INSERT values for one attribution decision.

    ``account_key`` is read with ``.get`` because the stably-absent sentinel
    OMITS the field (two-shaped stamp), and the absence must materialize as SQL
    NULL — the literal string is never stored.
    """
    p = rec.get("payload") or {}
    return (
        p.get("file_identity"), p.get("incarnation"), p.get("from_offset"),
        p.get("root_scope"), p.get("account_key"), rec.get("at"),
    )


def _apply_file_account_records(cache, records) -> "tuple[int, int]":
    """Materialize the given ``codex_file_account`` ops into an OPEN cache.db
    transaction; return ``(restored, conflicts)`` — how many rows were ABSENT
    before and actually landed (a genuine restore) and how many CONTRADICTED a
    different account already recorded at the same primary key (and were
    therefore declined, first-wins). Never opens or commits a transaction itself
    — every call site owns the flocks and the single ``BEGIN IMMEDIATE`` so the
    two families stay atomic.

    ``restored`` deliberately excludes a no-op replay of a row that is already
    present and already says the same thing. The rehydration re-reads the ops
    its OWN previous sync appended (the cursor is snapshotted before the walk,
    which is exactly what recovers a failed write), so counting those would
    make every second sync claim to have rehydrated something — and that claim
    is printed on stderr, where several golden harnesses read it.

    The ``prior is None`` probe is NOT sufficient on its own to call a record
    restored (closeout review C3): ``_FILE_ACCOUNT_INSERT`` carries ``OR
    IGNORE``, so a record violating some OTHER constraint is silently dropped
    and nothing was restored at all. ``total_changes`` after the statement is
    the only honest witness. A dropped record must also NOT raise the
    incarnation high-water — an inflated counter is the DANGEROUS direction,
    because ranges resolve at exactly the walk's current incarnation, so the
    range list comes back empty, ``covered`` is False, and a plain sync falls
    straight through to the live ``auth.json``.

    Both counts are returned rather than printed here because this is called
    once per record by the streamed rehydration; the caller collapses a run
    into one line AFTER its commit (closeout review C5) — a rollback must not
    leave the operator told about a decline that never happened."""
    restored = conflicts = 0
    for rec in records:
        values = _file_account_values(rec)
        prior = cache.execute(
            "SELECT account_key FROM codex_file_accounts "
            "WHERE file_identity = ? AND incarnation = ? AND from_offset = ?",
            values[:3],
        ).fetchone()
        before = cache.total_changes
        cache.execute(_FILE_ACCOUNT_INSERT, values)
        landed = cache.total_changes > before
        if prior is None:
            if not landed:
                # Dropped by a constraint the map table carries and the
                # incarnation table does not (`root_scope`, `decided_at_utc`).
                # Nothing was restored and nothing may be advanced.
                continue
            restored += 1
        elif prior[0] != values[4]:
            conflicts += 1
        cache.execute(_FILE_INCARNATION_INSERT, (values[0], values[1], values[5]))
    return restored, conflicts


def _report_file_account_conflicts(conflicts: int) -> None:
    """One stderr line for a run of replayed decisions that contradicted a
    different account already recorded at the same
    ``(file_identity, incarnation, from_offset)`` and were therefore DECLINED
    (first-wins, spec §3.3). Two ops at one primary key means one of them was
    minted without seeing the other, which is a real (if rare) condition with a
    real remedy, so it is reported rather than applied silently — the #374 rule.

    Every call site must invoke this AFTER its commit (closeout review C5): a
    rolled-back transaction applied nothing, so reporting from inside it would
    tell the operator about a decline that did not happen."""
    if conflicts > 0:
        print(
            f"[ingest] codex attribution replay declined {conflicts} "
            "contradicting decision(s); the first journalled decision for each "
            "byte range is retained; run "
            "`cctally cache-sync --source codex --rebuild` if the "
            "attribution still looks wrong",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------
# #416 spec §3.5 (review F5): which record is AUTHORITATIVE.
#
# "The journal always wins" is the wrong authority. Journaled quota obs are
# deduplicated on a natural key that EXCLUDES the account
# (`_codex_quota_natural_key`) and later records for that key are discarded by
# `append_record`, so the retained observation is FIRST-STAMP-WINS and may
# preserve the known late-ingest guess — bytes written under one login but
# ingested after a switch (spec §1.7). An unconditional upsert from it would
# overwrite a corrected file-range decision.
#
# Precedence: the durable file/range DECISION is authoritative; the observation
# stamp is used only where no range decision covers those bytes. Where the two
# disagree the row keeps the decision and the disagreement is REPORTED rather
# than silently applied — a genuine correction is expressed as an explicit new
# range decision, never by mutating history.
# --------------------------------------------------------------------------

# Converging form, used ONLY for a row whose bytes a decision covers. Repeating
# a deterministic DO UPDATE is idempotent, which is what preserves crash-replay;
# an uncovered obs keeps the first-write-wins INSERT OR IGNORE above.
#
# `INSERT OR IGNORE` is RETAINED here, not replaced by a plain `INSERT`. SQLite
# gives the upsert clause precedence for the conflict it names, so the targeted
# `DO UPDATE` still fires and convergence is unaffected — while every OTHER
# constraint on `quota_window_snapshots` (four CHECKs and several NOT NULLs)
# keeps the silent-drop tolerance the uncovered path has. Without it a violating
# record raises `IntegrityError`, `_cache_applier` catches it as `sqlite3.Error`
# and prefix-stops, and the scalar cursor can never advance past that record —
# so ONE permanently-violating row would wedge the whole journal ingest cycle
# forever, for every provider, not just Codex.
_QUOTA_SNAPSHOT_UPSERT_CLAUSE = (
    " ON CONFLICT(source, source_path, line_offset, logical_limit_key) "
    "DO UPDATE SET account_key = excluded.account_key "
    "WHERE quota_window_snapshots.account_key IS NOT excluded.account_key"
)

_QUOTA_SNAPSHOT_UPSERT = _QUOTA_SNAPSHOT_INSERT + _QUOTA_SNAPSHOT_UPSERT_CLAUSE
_QUOTA_SNAPSHOT_UPSERT_LEGACY = (
    _QUOTA_SNAPSHOT_INSERT_LEGACY + _QUOTA_SNAPSHOT_UPSERT_CLAUSE
)


class _CodexAttributionOracle:
    """Resolve ``(root_scope, source_path, line_offset)`` to the authoritative
    decision, memoised per file for one transaction.

    The map is keyed on the durable file identity, not on the path, so the path
    is canonicalized through the SAME helper the ingest used
    (``_cctally_cache._canonical_codex_path``) to reach it. When that lookup
    cannot be made — the sibling is unavailable, or the map holds MORE THAN ONE
    incarnation for the file — the oracle DECLINES. Declining matters most for
    the multi-incarnation case: a truncation reuses offsets from zero, so an
    observation's byte offset no longer identifies which incarnation it belongs
    to, and guessing would attribute pre-truncation bytes to the replacement
    file's account. Declining falls back to the observation stamp, which is
    exactly the documented "no range decision covers those bytes" branch.
    """

    def __init__(self, cache):
        self._cache = cache
        self._cache_by_path: dict = {}
        self._canonicalize = None
        self._available = None

    def _ensure_loaded(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            probe = self._cache.execute(
                "SELECT 1 FROM codex_file_accounts LIMIT 1").fetchone()
        except sqlite3.Error:
            self._available = False
            return False
        if probe is None:
            self._available = False
            return False
        try:
            import _cctally_cache as _cc
            from _lib_source_identity import codex_file_key
        except Exception:  # pragma: no cover — sibling unavailable
            self._available = False
            return False
        self._canonicalize = (_cc._canonical_codex_path, codex_file_key)
        self._available = True
        return True

    def _ranges_for(self, root_scope, source_path):
        key = (root_scope, source_path)
        if key in self._cache_by_path:
            return self._cache_by_path[key]
        ranges: list = []
        canonical, file_key = self._canonicalize
        try:
            identity = file_key(root_scope, str(canonical(pathlib.Path(source_path))))
        except Exception:
            self._cache_by_path[key] = ranges
            return ranges
        rows = self._cache.execute(
            "SELECT DISTINCT incarnation FROM codex_file_accounts "
            "WHERE file_identity = ?", (identity,)).fetchall()
        if len(rows) == 1:
            ranges = [
                (int(r[0]), r[1]) for r in self._cache.execute(
                    "SELECT from_offset, account_key FROM codex_file_accounts "
                    "WHERE file_identity = ? AND incarnation = ? "
                    "ORDER BY from_offset ASC", (identity, int(rows[0][0])))
            ]
        self._cache_by_path[key] = ranges
        return ranges

    def resolve(self, rec) -> "tuple[bool, str | None]":
        """``(covered, account_key)`` for one quota obs."""
        if not self._ensure_loaded():
            return False, None
        payload = rec.get("payload") or {}
        root_scope = payload.get("source_root_key")
        source_path = payload.get("source_path")
        offset = payload.get("line_offset")
        if not root_scope or not source_path or offset is None:
            return False, None
        covered, account_key = False, None
        for from_offset, decided in self._ranges_for(root_scope, source_path):
            if from_offset > offset:
                break
            covered, account_key = True, decided
        return covered, account_key


def _cache_has_anchor_column(cache) -> bool:
    """Whether this cache.db carries ``quota_window_snapshots.canonical_resets_at_utc``.

    Probed rather than assumed. This leg opens cache.db RAW (no dispatcher, no
    schema apply), so it can meet a cache that has not yet gained the column. A
    column-count mismatch would raise ``sqlite3.OperationalError``, which
    ``_cache_applier`` catches as a write failure and turns into a PREFIX-STOP —
    and the scalar cursor could then never advance past that record, wedging the
    journal ingest cycle for every provider. Same reasoning as the ``OR IGNORE``
    on ``_FILE_ACCOUNT_INSERT``; the walk's own writer needs no such guard
    because it only ever runs on a dispatcher-opened connection.
    """
    try:
        return "canonical_resets_at_utc" in {
            str(row[1]) for row in cache.execute(
                "PRAGMA table_info(quota_window_snapshots)")
        }
    except sqlite3.Error:  # pragma: no cover — unreadable schema
        return False


def _codex_anchor_resolver(cache):
    """A ``CodexResetAnchorResolver`` over this connection, or ``None`` when the
    sibling is unavailable. Degrading to ``None`` leaves the anchor column NULL,
    which every reader treats as "use the raw reset" — today's behaviour."""
    try:
        import _cctally_cache as _cc
    except Exception:  # pragma: no cover — sibling unavailable
        return None
    try:
        return _cc.CodexResetAnchorResolver(cache)
    except Exception:  # pragma: no cover — older sibling without the resolver
        return None


def _resolve_obs_anchor(resolver, rec: dict) -> "str | None":
    if resolver is None:
        return None
    p = rec.get("payload") or {}
    root = p.get("source_root_key")
    slot = p.get("observed_slot")
    key = p.get("logical_limit_key")
    if not isinstance(root, str) or not isinstance(slot, str) or not isinstance(key, str):
        return None
    try:
        return resolver.resolve(
            source_root_key=root, observed_slot=slot, logical_limit_key=key,
            window_minutes=p.get("window_minutes"),
            resets_at_utc=p.get("resets_at_utc"),
            source_path=p.get("source_path"),
            line_offset=p.get("line_offset"),
        )
    except Exception:  # pragma: no cover — never fail an ingest over a label
        return None


def _apply_quota_records(cache, records) -> None:
    """Materialize Codex quota obs into an OPEN cache.db transaction, applying
    the §3.5 precedence rule. Callers must apply the batch's file-account
    decisions FIRST, so a decision arriving in the same batch already governs
    the observations it covers."""
    oracle = _CodexAttributionOracle(cache)
    # One line per FILE PER BATCH, not per record. A mid-file account switch
    # legitimately produces a run of observations whose first-stamp-wins account
    # disagrees with the range decision now governing those bytes, and an
    # unthrottled warning would emit one line per row for the whole run. The set
    # is deliberately local, so a file whose conflicting run SPANS several
    # ingest batches reports once per batch — a per-cycle or per-process set
    # would have to outlive the transaction that may roll back, and repeating a
    # standing condition a handful of times is the cheaper error. The condition
    # is worth reporting at all because a genuine correction is expressed as an
    # explicit new range decision, never by mutating history.
    reported_conflicts: set = set()
    # #416 spec §4.2: this leg is a genuine INGEST into cache.db (it materializes
    # observations whose source rollout may have evaporated), so it must resolve
    # the canonical anchor too. Without it, a journal-replayed row lands with a
    # NULL anchor, a later walk's `INSERT OR IGNORE` cannot correct it, and that
    # window stays fragmented forever.
    has_anchor = _cache_has_anchor_column(cache)
    anchors = _codex_anchor_resolver(cache) if has_anchor else None
    insert_sql = _QUOTA_SNAPSHOT_INSERT if has_anchor else _QUOTA_SNAPSHOT_INSERT_LEGACY
    upsert_sql = _QUOTA_SNAPSHOT_UPSERT if has_anchor else _QUOTA_SNAPSHOT_UPSERT_LEGACY
    for rec in records:
        covered, decided = oracle.resolve(rec)
        anchor = _resolve_obs_anchor(anchors, rec)
        if anchors is not None:
            anchors.apply_pending_merges()
            anchors.mark_file_committed()
        row_values = _quota_snapshot_values(rec, anchor)
        if not has_anchor:
            row_values = row_values[:-1]
        if not covered:
            cache.execute(insert_sql, row_values)
            continue
        observed = rec.get("account")
        payload = rec.get("payload") or {}
        conflict_key = (payload.get("source_root_key"), payload.get("source_path"))
        if (observed is not None and observed != decided
                and conflict_key not in reported_conflicts):
            reported_conflicts.add(conflict_key)
            print(
                "[ingest] codex attribution conflict: "
                f"{payload.get('source_path')}@{payload.get('line_offset')} "
                f"observation stamped {observed} but the durable decision says "
                f"{decided if decided is not None else 'unattributed'}; "
                "keeping the decision",
                file=sys.stderr,
            )
        values = list(row_values)
        values[16] = decided
        cache.execute(upsert_sql, tuple(values))


def rehydrate_codex_file_accounts(
    cache_conn, *, authoritative: bool = False, since=None,
) -> "tuple[int, tuple[str, int] | None, int]":
    """Replay journaled ``codex_file_account`` ops into an open cache.db
    connection; return ``(applied_count, high_water, declined_conflicts)``
    (#416 spec §3.4).

    The conflict count is RETURNED rather than reported here (closeout review
    C5). This function runs inside the caller's transaction, and that caller
    rolls back on failure — reporting from in here would tell the operator about
    a decline that was undone. The two ``_apply_file_account_records`` call
    sites in the appliers already report post-commit; this one now matches.

    ``since`` is the ``(segment, offset)`` journal cursor the caller last
    replayed, or ``None`` for "from the beginning". The returned high-water is
    what the caller must persist so the NEXT call replays only the delta — the
    two together are what make this affordable on the hot path AND what recovers
    a decision that was journaled but never materialized (spec §3.6: "a crash
    after append but before the cache-map commit is recovered by replaying
    pending journal state under the same locked operation BEFORE ``auth.json``
    is consulted on retry"). A one-shot "already rehydrated" marker cannot do
    that: the failing sync's own op lands AFTER the marker was written, so the
    retry would never replay it and would re-decide from a possibly-changed
    identity instead.

    ``authoritative`` ignores ``since`` — a clear-then-replay is only correct
    from the beginning of the journal.

    The caller owns the flocks, the transaction and the commit — this function
    only executes the idempotent upserts, so it can run inside
    ``sync_codex_cache``'s already-locked phases without violating the
    lock-order law.

    Why an explicit phase exists at all: the ordinary journal-to-cache replay
    runs only inside ``rebuild_stats_index``, whereas ``cache-sync`` clears (on
    ``--rebuild``) and begins the rollout walk with NO applier in front of it. A
    recreated cache.db (corruption recovery, a manual ``rm cache.db``) therefore
    starts with an empty map, and the walk would fall straight back to the live
    ``auth.json`` for every file — which is the defect (review F2). Note this is
    NOT rebuild-only: every production Codex call site syncs with
    ``rebuild=False``, and the corruption auto-heal recreates the cache.db family
    and then re-runs the ORDINARY sync, so a rebuild-only wiring leaves the
    defect reachable by a shorter road.

    ``authoritative=True`` makes the replay a CONVERGENCE operator rather than an
    inserter: it clears ``codex_file_accounts`` first, so a row that has drifted
    away from the journal is corrected instead of being silently preserved by the
    ``DO NOTHING`` conflict clause (the #374 fold-applier defect class — see
    ``docs/journal-gotchas.md``). This is lossless because the ingest's
    fail-closed append journals the decision BEFORE any accounting DML or map
    write for that file, so every map row has a journal op behind it, and the
    journal is append-only with no segment pruning. It is the documented remedy
    (``cache-sync --rebuild``), so it must actually be able to repair.

    ``codex_file_incarnations`` is deliberately NOT cleared even under
    ``authoritative``, and the reason is NOT that a clear would be conservative.
    The op is journaled BEFORE the batch that persists the incarnation, so every
    committed incarnation is ``<=`` the highest incarnation any op carries — a
    re-derivation from ops can therefore never LOWER the counter, only raise it
    above what any committed batch used. And too high is the DANGEROUS
    direction, not the safe one: ranges are resolved at exactly the walk's
    current incarnation, so an inflated counter loads an EMPTY range list,
    ``covered`` is False, and a plain sync falls straight through to the live
    ``auth.json`` branch and re-decides — the original defect. Since the MAX-set
    upsert already converges the counter, a clear has no upside and that
    downside.
    """
    hw = journal_high_water()
    if hw is None:
        if authoritative:
            # No journal at all: an authoritative pass still says "the journal
            # is the truth", and the truth is that there are no decisions.
            cache_conn.execute("DELETE FROM codex_file_accounts")
        return 0, None, 0
    if authoritative:
        cache_conn.execute("DELETE FROM codex_file_accounts")
        since = None
    applied = 0
    conflicts = 0
    # Streamed, never materialized: this runs on the FIRST ordinary sync of
    # every cache.db (hook-tick, the dashboard, the corruption auto-heal's
    # re-sync) while both cache flocks are held, so a whole-journal transient
    # here is a multi-second global cache-writer stall — itself a
    # `database is locked` trigger. The cheap byte prefilter skips the JSON
    # decode for every non-decision line; the canonical encoder is
    # `json.dumps(..., ensure_ascii=False)`, which never escapes an ASCII kind
    # token, so a genuine op always carries this substring verbatim. A false
    # positive is harmless — it is decoded and rejected by the real predicate.
    for _seg, _off, raw in iter_range(since, hw):
        if _FILE_ACCOUNT_KIND_MARKER not in raw:
            continue
        rec = _lib_journal.decode_line(raw)
        if rec is not None and _is_codex_file_account_op(rec):
            _restored, _conflicts = _apply_file_account_records(cache_conn, (rec,))
            applied += _restored
            conflicts += _conflicts
    return applied, hw, conflicts


def _cache_applier(decoded) -> int | None:
    """Composite cache leg (spec §5.2 step 3 + #416 spec §3.4): materialize this
    batch's Codex quota obs into `quota_window_snapshots` AND its
    `codex_file_account` ops into the attribution map, under the NON-BLOCKING
    global cache writer lock followed by `cache.db.codex.lock`, in ONE
    `BEGIN IMMEDIATE`. Contract (journal seam): `(decoded) -> stop | None`,
    `decoded = [(record, segment, offset), ...]` in canonical order.

    - Neither family present in the batch → return None (no flock taken).
    - Busy global/provider flock, OR a cache write it cannot complete → PREFIX-STOP:
      return the EARLIEST index across BOTH families having committed NEITHER, so
      the cycle processes only `decoded[:stop]` and advances the cursor to
      `decoded[stop]`'s offset, retrying the remainder next cycle (the scalar
      cursor never advances past an unmaterialized record — spec §5.2 step 3).
    - Flock acquired + everything upserted → return None (full consumption).
      A quota-row change advances ``codex_physical_mutation_seq`` in the same
      transaction; an idempotent replay leaves the sequence unchanged.
    """
    quota_idx = [i for i, (rec, _s, _o) in enumerate(decoded)
                 if _is_codex_quota_obs(rec)]
    file_idx = [i for i, (rec, _s, _o) in enumerate(decoded)
                if _is_codex_file_account_op(rec)]
    if not quota_idx and not file_idx:
        return None
    # All-or-nothing across the two families: one stop, the earliest of either.
    stop_idx = min(quota_idx[0] if quota_idx else file_idx[0],
                   file_idx[0] if file_idx else quota_idx[0])
    from _lib_cache_writer_lock import (
        acquire_cache_writer_flocks,
        release_cache_writer_flocks,
    )

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        held = acquire_cache_writer_flocks(
            _cctally_core.CACHE_LOCK_PATH,
            _cctally_core.CACHE_LOCK_CODEX_PATH,
        )
    except OSError:
        return stop_idx
    if held is None:
        return stop_idx
    try:
        try:
            cache = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH), timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover — cache.db unopenable
            print(f"[ingest] cache leg connect failed: {exc}", file=sys.stderr)
            return stop_idx
        try:
            cache.execute("PRAGMA busy_timeout=15000")
            cache.execute("BEGIN IMMEDIATE")
            # Decisions FIRST: §3.5 makes the file/range decision authoritative
            # over the observation stamp, so a decision arriving in this batch
            # must already govern the observations it covers.
            _, _file_conflicts = _apply_file_account_records(
                cache, [decoded[i][0] for i in file_idx])
            quota_changes_before = cache.total_changes
            _apply_quota_records(cache, [decoded[i][0] for i in quota_idx])
            if cache.total_changes != quota_changes_before:
                # #457: this path is independent of the fused rollout writer,
                # but its quota rows feed the same certificate and dashboard
                # signatures.  Keep the token atomic with the materialization.
                import _cctally_cache
                _cctally_cache._bump_codex_physical_mutation_seq(cache)
            cache.commit()
            _report_file_account_conflicts(_file_conflicts)
        except sqlite3.Error as exc:
            try:
                cache.rollback()
            except sqlite3.Error:
                pass
            # Could not materialize -> prefix-stop so the cursor holds and the
            # next cycle retries (the records stay durable in the journal
            # regardless). NEITHER family is committed.
            print(f"[ingest] cache leg write failed: {exc}", file=sys.stderr)
            return stop_idx
        finally:
            cache.close()
        return None
    finally:
        release_cache_writer_flocks(held)


# Back-compat alias: the leg was Codex-quota-only until #416 widened it.
_quota_applier = _cache_applier

# Wire the seam (declared None near the top as the contract stub). Always-on:
# a Claude-only cycle's scan finds neither family and returns None before any
# flock/DB touch, so the cost is two list comprehensions over the batch.
CACHE_APPLIER = _cache_applier
QUOTA_APPLIER = _cache_applier


# --------------------------------------------------------------------------
# fold appliers (spec §5.3)
# --------------------------------------------------------------------------

def _resolve_ref(conn: sqlite3.Connection, table: str, logical_id) -> int | None:
    """Resolve a logical journal id to its rebuilt rowid in `table` via the
    `journal_id` column (spec §4.2 FK rule).

    A falsy logical id (0 / "0" / None / "") is the "no FK" sentinel — e.g.
    `reset_event_id` defaults to 0 — and resolves to 0 without a lookup. An
    unresolved id returns None so the caller can decide (Tasks 6-7).
    """
    if logical_id in (0, "0", None, ""):
        return 0
    row = conn.execute(
        f"SELECT id FROM {table} WHERE journal_id = ?", (logical_id,)
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def _insert_or_ignore(
    conn: sqlite3.Connection, table: str, cols: dict, *, strict: bool = False
):
    keys = list(cols.keys())
    colnames = ", ".join(keys)
    placeholders = ", ".join("?" for _ in keys)
    statement = (
        f"INSERT OR IGNORE INTO {table} ({colnames}) VALUES ({placeholders})"
    )
    if strict:
        statement = statement.replace("INSERT OR IGNORE", "INSERT", 1)
    return conn.execute(statement, tuple(cols[k] for k in keys))


def _reverse_ref(conn: sqlite3.Connection, ref_table: str, rowid) -> "str | None":
    """Reverse of `_resolve_ref` — a FK rowid to the referenced row's *logical*
    id (its `journal_id`), for building a harvest evt's logical-FK ref (spec
    §4.2). The falsy sentinel (0 / None) — e.g. `reset_event_id`'s no-event
    default — maps to the literal ``"0"`` so the id/payload stay stable across
    replay. A referenced row whose `journal_id` is still NULL returns None — a
    harvest-order violation (the referenced family must harvest first); the
    caller degrades loudly rather than journaling an unresolvable ref.
    """
    if rowid in (0, None):
        return "0"
    row = conn.execute(
        f"SELECT journal_id FROM {ref_table} WHERE id = ?", (rowid,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


def _now_iso() -> str:
    """Fallback capture time for a derived evt with no natural `at` column
    (UTC, seconds, ``Z``). Live emission passes the triggering record's `at`."""
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _usage_snapshot_fold_decision(conn, payload) -> tuple[bool, object]:
    """The apply-time dedup for a Claude rate-limit obs — the exact predicate
    ported from `cmd_record_usage`'s insert guard (bin/_cctally_record.py), now
    at fold time (spec §4.5 / §5.3).

    Returns `(skip, adjusted_five_hour_percent)`:
      - reset-aware 7d HWM clamp (`_reset_aware_floor` + reset-aware MAX +
        `hwm_clamp_applies`) → skip when a lower 7d % would be clamped;
      - the 5h clamp adjusts `five_hour_percent` UP to the in-window MAX but
        never gates (mirrors the nested-else in the live code);
      - dedup vs the latest snapshot in the week: both percents unchanged → skip.
    """
    week_start_date = payload["week_start_date"]
    week_start_at = payload.get("week_start_at")
    week_end_at = payload.get("week_end_at")
    weekly_percent = float(payload["weekly_percent"])
    five_hour_percent = payload.get("five_hour_percent")
    five_hour_window_key = payload.get("five_hour_window_key")
    # Account dimension (#341, review finding 11): every clamp/dedup query below
    # is scoped to the account being processed so two accounts writing into the
    # same week / same physical 5h window never clamp or dedup against each other.
    # Defaults to the reserved sentinel when the caller omits it (byte-stable on a
    # single-account install where every row shares one key).
    account_key = payload.get("account_key") or _lib_accounts.UNATTRIBUTED

    clamp_floor_iso = _cctally_core._reset_aware_floor(
        conn, week_start_date, week_start_at, week_end_at,
        account_key=account_key,
    ) or "1970-01-01T00:00:00Z"
    max_row = conn.execute(
        "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
        "WHERE week_start_date = ? AND account_key = ? "
        "  AND unixepoch(captured_at_utc) >= unixepoch(?)",
        (week_start_date, account_key, clamp_floor_iso),
    ).fetchone()
    max_v = max_row[0] if max_row else None
    if _lib_record.hwm_clamp_applies(weekly_percent, max_v):
        return True, five_hour_percent

    adjusted_5h = five_hour_percent
    if five_hour_percent is not None and five_hour_window_key is not None:
        max_5h_row = conn.execute(
            "SELECT MAX(five_hour_percent) FROM weekly_usage_snapshots "
            "WHERE five_hour_window_key = ? AND account_key = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(COALESCE("
            "        (SELECT effective_reset_at_utc FROM five_hour_reset_events "
            "          WHERE five_hour_window_key = ? AND account_key = ? "
            "          ORDER BY id DESC LIMIT 1),"
            "        '1970-01-01T00:00:00Z'))",
            (int(five_hour_window_key), account_key,
             int(five_hour_window_key), account_key),
        ).fetchone()
        max_5h = max_5h_row[0] if max_5h_row else None
        if _lib_record.hwm_clamp_applies(float(five_hour_percent), max_5h):
            adjusted_5h = float(max_5h)

    last = conn.execute(
        "SELECT weekly_percent, five_hour_percent FROM weekly_usage_snapshots "
        "WHERE week_start_date = ? AND account_key = ? "
        "ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
        (week_start_date, account_key),
    ).fetchone()
    if last is not None and float(last[0]) == weekly_percent:
        last_5h = last[1]
        if adjusted_5h is None or (
            last_5h is not None and float(last_5h) == float(adjusted_5h)
        ):
            return True, adjusted_5h
    return False, adjusted_5h


# NOTE (rev 3): the direct obs -> weekly_usage_snapshots fold is GONE. That
# table is now written ONLY via `snapshot_accept` Model-A evts (spec §5.3); the
# accept/skip DECISION above (`_usage_snapshot_fold_decision`) is 6b's
# snapshot_accept deriver's to make, ONCE, at capture time — the decision itself
# is journaled, so replay never re-derives it.


def _apply_op_weekly_credit_floor(conn, record) -> None:
    """Fold a `record-credit` `op` into `weekly_credit_floors` (spec §5.3
    "fold op"). `INSERT OR IGNORE` dedups on both `journal_id` and the table's
    own `UNIQUE(week_start_date, effective_at_utc)`."""
    payload = record["payload"]
    _insert_or_ignore(conn, "weekly_credit_floors", {
        "journal_id": record["id"],
        "week_start_date": payload["week_start_date"],
        "effective_at_utc": payload["effective_at_utc"],
        "observed_pre_credit_pct": float(payload["observed_pre_credit_pct"]),
        "applied_at_utc": payload.get("applied_at_utc", record["at"]),
        # Two-shaped stamp (#341 rev 4.1): evt/op carry account_key in the
        # payload. Default to the sentinel for legacy ops written pre-#341.
        "account_key": payload.get("account_key") or _lib_accounts.UNATTRIBUTED,
    })


# --------------------------------------------------------------------------
# accounts registry fold (#341, spec §1/§2). `account_observe` / `account_label`
# op lines fold into the `accounts` registry. Registered here so BOTH the live
# ingest (`_pipeline_op_fold`) AND `rebuild_stats_index` (its op-fold stream)
# derive the registry deterministically. `last_seen_utc` is NOT set here — it
# derives from the max `at` of any account-stamped line via
# `_derive_account_last_seen`, run after the fold in both paths.
# --------------------------------------------------------------------------

_LABEL_RANK = {"auto": 0, "switcher": 1, "user": 2}


def _label_rank(source: str | None) -> int:
    return _LABEL_RANK.get(source or "auto", 0)


def _apply_op_account_observe(conn, record) -> None:
    """Fold an `account_observe` op into the `accounts` registry. Idempotent:
    INSERT OR IGNORE creates the row on first sight, then the identity fields
    (provider/natural_id/email/plan_type) take the latest chronological value
    (canonical fold order = chronological), `first_seen_utc` keeps the MIN `at`,
    and an optional label is applied only when its provenance rank is >= the
    stored one (user > switcher > auto — never override a user label)."""
    p = record.get("payload") or {}
    key = p.get("account_key")
    at = record.get("at")
    if not key:
        return
    conn.execute(
        "INSERT OR IGNORE INTO accounts "
        "(account_key, provider, label_source, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, 'auto', ?, ?)",
        (key, p.get("provider"), at, at),
    )
    conn.execute(
        "UPDATE accounts SET provider = COALESCE(?, provider), "
        "  natural_id = COALESCE(?, natural_id), email = COALESCE(?, email), "
        "  plan_type = COALESCE(?, plan_type) WHERE account_key = ?",
        (p.get("provider"), p.get("natural_id"), p.get("email"),
         p.get("plan_type"), key),
    )
    if at is not None:
        conn.execute(
            "UPDATE accounts SET first_seen_utc = ? WHERE account_key = ? "
            "AND (first_seen_utc IS NULL OR ? < first_seen_utc)",
            (at, key, at),
        )
        conn.execute(
            "UPDATE accounts SET last_seen_utc = ? WHERE account_key = ? "
            "AND (last_seen_utc IS NULL OR ? > last_seen_utc)",
            (at, key, at),
        )
    inc_label = p.get("label")
    if inc_label is not None:
        inc_src = p.get("label_source") or "auto"
        row = conn.execute(
            "SELECT label_source FROM accounts WHERE account_key = ?", (key,)
        ).fetchone()
        cur_src = row[0] if row is not None else "auto"
        if _label_rank(inc_src) >= _label_rank(cur_src):
            conn.execute(
                "UPDATE accounts SET label = ?, label_source = ? "
                "WHERE account_key = ?",
                (inc_label, inc_src, key),
            )


def _apply_op_account_label(conn, record) -> None:
    """Fold an `account_label` op (a user rename) — always authoritative
    (label_source='user', the top of the precedence order)."""
    p = record.get("payload") or {}
    key = p.get("account_key")
    if not key:
        return
    at = record.get("at")
    conn.execute(
        "INSERT OR IGNORE INTO accounts "
        "(account_key, provider, label_source, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, 'auto', ?, ?)",
        (key, p.get("provider"), at, at),
    )
    conn.execute(
        "UPDATE accounts SET label = ?, label_source = 'user' "
        "WHERE account_key = ?",
        (p.get("label"), key),
    )


def _account_of(record) -> str | None:
    """The account_key a record contributes to `last_seen_utc`: the top-level
    `account` stamp on a data-bearing line, or an `account_observe`'s own key."""
    acct = record.get("account")
    if isinstance(acct, str) and acct:
        return acct
    if record.get("t") == "op":
        p = record.get("payload") or {}
        if p.get("kind") == "account_observe":
            k = p.get("account_key")
            if isinstance(k, str) and k:
                return k
    return None


def _derive_account_last_seen(conn, records) -> None:
    """Fold-time `last_seen_utc` derivation (spec §1): the MAX `at` of any
    account-stamped line advances an account's last-seen, so a stable account's
    last-seen keeps moving without extra observe records. Idempotent MAX update;
    runs after the fold in both the live cycle and rebuild. Only touches rows a
    prior observe already created (never invents an account row)."""
    latest: dict = {}
    for rec in records:
        key = _account_of(rec)
        at = rec.get("at")
        if not key or not at:
            continue
        prev = latest.get(key)
        if prev is None or at > prev:
            latest[key] = at
    for key, at in latest.items():
        conn.execute(
            "UPDATE accounts SET last_seen_utc = ? WHERE account_key = ? "
            "AND (last_seen_utc IS NULL OR ? > last_seen_utc)",
            (at, key, at),
        )


# --------------------------------------------------------------------------
# legacy classifier (#341, spec §2). A DATA-BEARING journal line that lacks an
# `account` field is "legacy" (pre-cutover). Accounts-machinery records
# (account_observe / account_label ops, the cutover op) are recognised by their
# registered kinds and are NEITHER legacy NOR account-stamped data. This pure
# classifier maps a legacy line to its provider, then `legacy_account_key` maps
# that provider to the cutover mapping (Claude legacy -> the op's value; Codex
# legacy -> unattributed). Used at rebuild + by the cache backfill migration to
# normalise the missing account_key BEFORE insertion.
# --------------------------------------------------------------------------

# Old evt lines carry no top-level provider, so evt kind -> provider is a fixed
# table. `budget` is vendor-dependent (its payload `vendor` names the provider).
_EVT_KIND_PROVIDER = {
    "snapshot_accept": "claude",
    "weekly_cost_snapshot": "claude",
    "week_reset": "claude",
    "five_hour_credit": "claude",
    "five_hour_block_close": "claude",
    "percent_milestone": "claude",
    "five_hour_milestone": "claude",
    "projected": "claude",
    "project_budget": "claude",
    "quota_alert_arming": "codex",
    "quota_threshold_event": "codex",
}

# Op kinds that are accounts-machinery (recognised, never classified as legacy).
# `codex_file_account` (#416 spec §3.3) joins them: it is the durable Codex
# attribution DECISION, and its sentinel form deliberately OMITS `account_key`
# — exactly the shape the legacy classifier keys on — so registration here is
# what keeps `_normalize_legacy_account_stamp` from ever retro-stamping it.
# Registering a kind here also feeds `_cctally_rederive.plan_claude_usage`'s
# `op_kinds` set, so the kind MUST additionally carry a
# `_lib_rederive._OP_CLASSIFICATIONS` entry or the re-derive planner raises
# `RederiveConflict` on every run.
_ACCOUNTS_MACHINERY_KINDS = frozenset(
    ("account_observe", "account_label", "accounts_cutover",
     "codex_file_account"))

# Legacy-classifier exhaustiveness guard (#341, review finding P2-1). EVERY evt
# kind in `_EVT_SPECS` and every harvest kind in `_HARVEST_SPECS` must carry a
# classifier disposition: a provider verdict (a data-bearing real-account or
# `*`-family kind, via `_EVT_KIND_PROVIDER`), the vendor-tagged special case
# (`budget`, provider read from the payload `vendor`), or an explicit EXEMPTION.
# `weekly_credit_effects` is exempt because it is effects-only
# (`_EvtSpec.table is None`): it inserts NO target row, so nothing carries
# `account_key` to normalise — it only deletes stale-replica snapshots by their
# globally-unique `journal_id` (an account-agnostic key) and force-writes the
# account-agnostic hwm-7d statusline file. The exhaustiveness is asserted
# STRUCTURALLY by tests/test_accounts_journal.py (iterating both spec registries),
# so a future data-bearing kind cannot silently escape classification.
_CLASSIFIER_VENDOR_TAGGED_KINDS = frozenset(("budget",))
_CLASSIFIER_EXEMPT_KINDS = frozenset(("weekly_credit_effects",))


def classify_legacy_provider(record) -> str | None:
    """Return the provider ('claude'|'codex') of a DATA-BEARING legacy record
    (obs/op/evt lacking an account stamp), or None when the record is not
    legacy data — an already-account-stamped line, an accounts-machinery record,
    an effects-only exempt kind, or an unknown kind (additive-evolution
    tolerance).

    Two-shaped already-stamped guard (#341 rev 4.1): obs carry the account on the
    top-level ``account`` field; evt/op carry it inside ``payload.account_key``.
    EITHER shape means the line is already account-stamped and is NOT legacy — a
    single-shape check would mis-classify a freshly account-stamped evt as legacy
    and re-normalise it."""
    if not isinstance(record, dict):
        return None
    payload = record.get("payload") or {}
    kind = payload.get("kind")
    # A line already carrying an account stamp (either shape) is not legacy.
    if isinstance(record.get("account"), str) and record.get("account"):
        return None
    if isinstance(payload.get("account_key"), str) and payload.get("account_key"):
        return None
    # Accounts-machinery records + effects-only exempt kinds carry no target row
    # to stamp — recognised by registration, never legacy data (review P3-D).
    if kind in _ACCOUNTS_MACHINERY_KINDS or kind in _CLASSIFIER_EXEMPT_KINDS:
        return None
    t = record.get("t")
    if t == "obs":
        prov = record.get("provider")
        return prov if prov in ("claude", "codex") else None
    if t == "op":
        # weekly_credit_floor is the only legacy op family; it is Claude.
        return "claude" if kind == "weekly_credit_floor" else None
    if t == "evt":
        if kind in _CLASSIFIER_VENDOR_TAGGED_KINDS:
            vendor = payload.get("vendor")
            return vendor if vendor in ("claude", "codex") else None
        return _EVT_KIND_PROVIDER.get(kind)
    return None


def legacy_account_key(record, claude_legacy_account: str) -> str | None:
    """Map a legacy record to the account_key to stamp: the cutover op's
    recorded Claude account for a Claude legacy line, `unattributed` for a Codex
    legacy line. Returns None when the record is not legacy data (caller leaves
    it untouched)."""
    prov = classify_legacy_provider(record)
    if prov is None:
        return None
    if prov == "claude":
        return claude_legacy_account
    return _lib_accounts.UNATTRIBUTED


# The REAL-account evt/op kinds whose missing account_key is normalised to the
# cutover mapping at rebuild (#341, handoff item 2). The `*`-families (`budget`,
# `projected`, `project_budget`) are DELIBERATELY excluded — they take the
# schema DEFAULT `'*'`, never the cutover account (spec §2 / scope matrix).
_REAL_ACCOUNT_EVT_OP_KINDS = frozenset((
    "snapshot_accept", "weekly_cost_snapshot", "week_reset", "five_hour_credit",
    "five_hour_block_close", "percent_milestone", "five_hour_milestone",
    "weekly_credit_floor",
))


def _normalize_legacy_account_stamp(record, claude_legacy_account: str) -> None:
    """In-place two-shaped account normalisation for a legacy (pre-#341) record
    at rebuild (spec §2, handoff item 2). obs get a top-level ``account``; a
    REAL-account evt/op gets ``payload.account_key``. Already-stamped records,
    ``*``-families, and unknown/machinery kinds are left untouched — so a rebuild
    over a cutover-op'd journal reproduces pre-feature Claude data under the op's
    account and pre-feature Codex data under ``unattributed`` (acceptance 4)."""
    if not isinstance(record, dict):
        return
    t = record.get("t")
    if t == "obs":
        if isinstance(record.get("account"), str) and record.get("account"):
            return
        key = legacy_account_key(record, claude_legacy_account)
        if key is not None:
            record["account"] = key
        return
    if t in ("evt", "op"):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("kind") not in _REAL_ACCOUNT_EVT_OP_KINDS:
            return
        if isinstance(payload.get("account_key"), str) and payload.get("account_key"):
            return
        key = legacy_account_key(record, claude_legacy_account)
        if key is not None:
            payload["account_key"] = key


# Obs/op fold registry (spec §5.3 "fold op"). Keyed by `payload.kind`; the
# built-in `_pipeline_op_fold` pipeline hook dispatches through it. 6b may
# register more op folds; obs no longer fold directly (see the NOTE above).
FOLD_APPLIERS = {
    "weekly_credit_floor": _apply_op_weekly_credit_floor,
    "account_observe": _apply_op_account_observe,
    "account_label": _apply_op_account_label,
}


# --------------------------------------------------------------------------
# evt fold appliers (step 4a replay + the emit_model_a apply path)
# --------------------------------------------------------------------------

_BLOCK_CHILDREN = (
    ("_models", "five_hour_block_models"),
    ("_projects", "five_hour_block_projects"),
)
_BLOCK_CHILD_KEYS = frozenset(k for k, _t in _BLOCK_CHILDREN)


def _replace_block_children(
    conn, block_id, parent_account, parent_window, children
) -> None:
    """Materialize one frozen block's child sets exactly.

    A close event owns the complete model/project membership, so replay and
    convergence replace both sets rather than relying on natural-key
    ``INSERT OR IGNORE``. This removes stale children and restores missing
    children while preserving the parent rowid used by milestone FKs.
    """
    for payload_key, child_table in _BLOCK_CHILDREN:
        if parent_account is None:
            predicate = "five_hour_window_key = ?"
            params = (int(parent_window),)
        else:
            predicate = "account_key = ? AND five_hour_window_key = ?"
            params = (parent_account, int(parent_window))
        conn.execute(
            f"DELETE FROM {child_table} WHERE {predicate}", params
        )
        for child in children.get(payload_key, []):
            cols = dict(child)
            cols["block_id"] = int(block_id)
            if parent_account is not None:
                cols["account_key"] = parent_account
            _insert_or_ignore(conn, child_table, cols, strict=True)


def _apply_generic_evt(conn, evt):
    """Fold an evt line into its target table (spec §5.3), returning the sqlite
    cursor of the `INSERT OR IGNORE`.

    Table + logical-FK spec come from `_EVT_SPECS[payload['kind']]`. Non-FK
    payload keys map to same-named columns; FK-ref keys resolve logical ids to
    rowids via `_resolve_ref`. `INSERT OR IGNORE` keyed on `journal_id` (and the
    table's natural-key UNIQUE) makes replay idempotent. An unknown kind, or a
    spec with no `table`, is a no-op (additive-evolution tolerance, spec §4.2).
    """
    payload = evt.get("payload") or {}
    spec = _EVT_SPECS.get(payload.get("kind"))
    if spec is None or spec.table is None:
        return None
    cols = {"journal_id": evt["id"]}
    for key, value in payload.items():
        if key == "kind":
            continue
        if key in spec.fk_refs:
            column, ref_table = spec.fk_refs[key]
            cols[column] = _resolve_ref(conn, ref_table, value)
        else:
            cols[key] = value
    # Re-derive any projection-FK columns from a journaled natural-key column
    # (spec §5.3 — e.g. five_hour_milestones.block_id from five_hour_window_key,
    # since the open block is a projection with no logical id).
    acct = cols.get("account_key")
    for column, (ref_table, lookup_col) in spec.derived_fk.items():
        cols[column] = _derived_fk_value(
            conn, ref_table, lookup_col, cols.get(lookup_col), acct)
    return _insert_or_ignore(conn, spec.table, cols)


def _derived_fk_value(conn, ref_table, lookup_col, lookup_value, account_key):
    """Resolve one derived (re-derived-at-fold) FK column (spec §5.3).

    Composite `(account_key, <lookup_col>)` when the row carries an account
    (#341, review finding 3): a shared physical 5h window resolves THIS
    account's block, so a milestone never attaches to another account's block.
    0 when unresolvable. The SINGLE home of this rule — the fold applier and the
    #374 duplicate-path validation must agree by construction, not by copy."""
    if account_key is not None:
        row = conn.execute(
            f"SELECT id FROM {ref_table} "
            f"WHERE {lookup_col} = ? AND account_key = ?",
            (lookup_value, account_key),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT id FROM {ref_table} WHERE {lookup_col} = ?",
            (lookup_value,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


def _apply_weekly_credit_effects(conn, evt, *, projection_writes=True):
    """Apply a `weekly_credit_effects` evt (spec §5.3 event+effects). The
    same-window sub-25pp credit writes NO reset row, so its DESTRUCTIVE effects
    ride this vehicle: delete the stale-replica snapshots by their logical
    `journal_id` (idempotent — deleting an already-absent id is a clean no-op),
    then force the HWM floor file down (mirrors `_apply_credit` step 4b; an
    idempotent overwrite). The synthetic post-credit snapshots ride their own
    `snapshot_accept` evts. Effects-only — no target-table row, so no journal_id
    of its own; convergence is the natural idempotence of DELETE + overwrite.

    A ``--force`` re-record's destructive clear (the ingest-path replacement for
    ``_force_clear_credit``) rides the SAME evt: ``suppression`` also carries the
    OLD command-owned synthetic snapshots' `journal_id`s (deleted from the same
    ``weekly_usage_snapshots`` table), and ``floor_suppression`` carries the OLD
    ``weekly_credit_floors`` rows' `journal_id`s (the prior credit's floor,
    NEVER the new op's own floor — the op fold owns that). Both delete by logical
    id, so replay reproduces the clear deterministically and idempotently; the
    NEW floor + NEW synthetic are keyed by the current op's id and never appear
    in either list, so this effect is order-independent w.r.t. them (spec §5.3)."""
    payload = evt.get("payload") or {}
    table = payload.get("suppression_table", "weekly_usage_snapshots")
    for logical_id in (payload.get("suppression") or []):
        conn.execute(f"DELETE FROM {table} WHERE journal_id = ?", (logical_id,))
    for logical_id in (payload.get("floor_suppression") or []):
        conn.execute(
            "DELETE FROM weekly_credit_floors WHERE journal_id = ?", (logical_id,))
    floor = payload.get("hwm_floor")
    if floor and projection_writes:
        try:
            (_cctally_core.APP_DIR / "hwm-7d").write_text(
                f"{floor['week_start_date']} {floor['weekly_percent']}\n"
            )
        except OSError:
            pass
    return None


def _apply_quota_alert_arming(conn, evt):
    """Fold a `quota_alert_arming` evt (spec §5.3 "state", Task 7 Item 5). The
    quota-alert arming boundary is journaled state — its `activated_at_utc` is a
    forward-only alert boundary that MUST survive a stats.db rebuild so the
    reconcile honors it (no historical re-fires). Activation records UPSERT the
    natural key; explicit disarm records DELETE that same account-qualified key.
    Canonical replay therefore leaves the latest retained state in force, and
    re-applying either transition is a clean no-op. `quota_alert_arming` has no
    `journal_id` column (it is not in the Task-4 additive list); idempotence is
    the natural-key upsert/delete, not a journal_id INSERT OR IGNORE."""
    p = evt.get("payload") or {}
    # account_key (#341) is part of the arming identity/UNIQUE. A live-emitted evt
    # carries payload.account_key; a legacy (pre-#341) cutover-exported arming has
    # none -> normalise to the sentinel (Codex legacy -> unattributed) so the
    # NOT NULL column always receives a value.
    account_key = p.get("account_key") or _lib_accounts.UNATTRIBUTED
    if p.get("state") == "disarmed":
        conn.execute(
            "DELETE FROM quota_alert_arming "
            "WHERE source=? AND source_root_key=? AND account_key=? "
            "AND logical_limit_key=? AND observed_slot=? AND window_minutes=?",
            (
                p.get("source"), p.get("source_root_key"), account_key,
                p.get("logical_limit_key"), p.get("observed_slot"),
                p.get("window_minutes"),
            ),
        )
        return None
    conn.execute(
        "INSERT INTO quota_alert_arming "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        " window_minutes, rule_fingerprint, activated_at_utc, account_key) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, source_root_key, account_key, logical_limit_key, "
        "            observed_slot, window_minutes) DO UPDATE SET "
        "  rule_fingerprint=excluded.rule_fingerprint, "
        "  activated_at_utc=excluded.activated_at_utc",
        (p.get("source"), p.get("source_root_key"), p.get("logical_limit_key"),
         p.get("observed_slot"), p.get("window_minutes"),
         p.get("rule_fingerprint"), p.get("activated_at_utc"), account_key),
    )
    return None


def _apply_quota_threshold_event(conn, evt):
    """Fold a `quota_threshold_event` evt (#416 spec §7.2, review F13).

    `quota_threshold_events` is TERMINAL alert evidence: each row records that a
    threshold was crossed and either alerted or was suppressed as backfill, with
    the exact moment it happened. It is in NEITHER `_HARVEST_SPECS` (eight
    families, none of them this one) nor — before #416 — `_CUTOVER_SPECS`, and
    `rematerialize_quota_projection_for_rebuild` runs with
    `alert_eligible_roots=frozenset()`, so a rebuild could not recreate an
    `alerted` row at all. Every rebuild silently discarded the evidence that an
    alert had already fired, which is what would let it fire again.

    Convergence is a natural-key UPSERT, exactly like `_apply_quota_alert_arming`
    — the table has no `journal_id` column, so idempotence cannot ride an
    `INSERT OR IGNORE` on the journal id. The upsert restores `disposition`,
    `alerted_at` and `suppressed_at` VERBATIM, which matters because the rebuild's
    re-materialization pass can legitimately re-derive the same crossing as a
    fresh `suppressed_backfill` row: whichever of the two runs second, the
    journaled terminal fact is what stands.

    `orphaned_at` is deliberately NOT journaled and NOT touched here. It marks a
    window whose evidence has since vanished, and `_orphan_unseen` re-derives it
    from the current projection on every pass — replaying a stale value would
    fight that.
    """
    p = evt.get("payload") or {}
    account_key = p.get("account_key") or _lib_accounts.UNATTRIBUTED
    disposition = p.get("disposition")
    alerted_at = p.get("alerted_at")
    suppressed_at = p.get("suppressed_at")
    # The table's CHECK pairs disposition with exactly one timestamp. Normalize
    # rather than trust the payload, so a malformed record cannot raise here and
    # prefix-stop the whole fold.
    if disposition == "alerted":
        suppressed_at = None
        alerted_at = alerted_at or p.get("created_at_utc")
    elif disposition == "suppressed_backfill":
        alerted_at = None
        suppressed_at = suppressed_at or p.get("created_at_utc")
    else:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO quota_threshold_events "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        " window_minutes, resets_at_utc, threshold, qualifying_kind, "
        " qualifying_percent, projected_percent, severity, created_at_utc, "
        " disposition, alerted_at, suppressed_at, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source, source_root_key, account_key, logical_limit_key, "
        "            observed_slot, window_minutes, resets_at_utc, threshold) "
        "DO UPDATE SET "
        "  qualifying_kind=excluded.qualifying_kind, "
        "  qualifying_percent=excluded.qualifying_percent, "
        "  projected_percent=excluded.projected_percent, "
        "  severity=excluded.severity, "
        "  created_at_utc=excluded.created_at_utc, "
        "  disposition=excluded.disposition, "
        "  alerted_at=excluded.alerted_at, "
        "  suppressed_at=excluded.suppressed_at",
        (p.get("source"), p.get("source_root_key"), p.get("logical_limit_key"),
         p.get("observed_slot"), p.get("window_minutes"), p.get("resets_at_utc"),
         p.get("threshold"), p.get("qualifying_kind"),
         p.get("qualifying_percent"), p.get("projected_percent"),
         p.get("severity"), p.get("created_at_utc"), disposition,
         alerted_at, suppressed_at, account_key),
    )
    return None


def _apply_block_close(conn, evt):
    """Fold one authoritative frozen-block fact.

    A replay may meet an existing open projection under the same
    ``(account_key, five_hour_window_key)`` natural key. ``INSERT OR IGNORE``
    alone would silently leave that mutable row and its children in place, so
    the event now converges the existing parent in place and replaces both
    child sets exactly. The parent rowid is preserved for milestone FKs.
    """
    payload = evt.get("payload") or {}
    parent = {"journal_id": evt["id"]}
    children = {}
    for key, value in payload.items():
        if key == "kind":
            continue
        if key in _BLOCK_CHILD_KEYS:
            children[key] = value or []
            continue
        parent[key] = value
    _insert_or_ignore(conn, "five_hour_blocks", parent)

    p_acct = parent.get("account_key")
    if p_acct is not None:
        prow = conn.execute(
            "SELECT id, journal_id, account_key, five_hour_window_key "
            "FROM five_hour_blocks "
            "WHERE five_hour_window_key = ? AND account_key = ?",
            (parent.get("five_hour_window_key"), p_acct),
        ).fetchone()
    else:
        prow = conn.execute(
            "SELECT id, journal_id, account_key, five_hour_window_key "
            "FROM five_hour_blocks "
            "WHERE five_hour_window_key = ?",
            (parent.get("five_hour_window_key"),),
        ).fetchone()
    if prow is None:
        raise JournalError(
            f"five_hour_block_close {evt['id']} did not materialize its parent"
        )
    block_id = int(prow[0])
    existing_journal_id = prow[1]
    if existing_journal_id not in (None, evt["id"]):
        raise JournalError(
            f"five_hour_block_close {evt['id']} collided with "
            f"{existing_journal_id} on its parent natural key"
        )
    assignments = ", ".join(f"{name} = ?" for name in parent)
    conn.execute(
        f"UPDATE five_hour_blocks SET {assignments} WHERE id = ?",
        (*parent.values(), block_id),
    )
    _replace_block_children(conn, block_id, prow[2], prow[3], children)
    return None


def _apply_reset_with_suppression(conn, evt):
    """Fold a reset/credit evt (`week_reset`/`five_hour_credit`) that carries a
    `suppression` list (spec §5.3 event+effects, Design B). Insert the reset row
    (idempotent on natural key / journal_id via the generic column map — the
    `suppression`/`suppression_table` keys are effects, NOT columns, so they are
    stripped), THEN apply the destructive stale-replica DELETE by logical
    `journal_id` (idempotent — deleting an already-absent id is a clean no-op,
    mirroring `_apply_weekly_credit_effects`). The synthetic post-credit
    snapshots ride their own `snapshot_accept` evts; this vehicle only inserts
    the reset row and replays its suppression."""
    payload = evt.get("payload") or {}
    spec = _EVT_SPECS.get(payload.get("kind"))
    if spec is None or spec.table is None:
        return None
    cols = {"journal_id": evt["id"]}
    for key, value in payload.items():
        if key in ("kind", "suppression", "suppression_table"):
            continue
        if key in spec.fk_refs:
            column, ref_table = spec.fk_refs[key]
            cols[column] = _resolve_ref(conn, ref_table, value)
        else:
            cols[key] = value
    _insert_or_ignore(conn, spec.table, cols)
    supp_table = payload.get("suppression_table", "weekly_usage_snapshots")
    for logical_id in (payload.get("suppression") or []):
        conn.execute(f"DELETE FROM {supp_table} WHERE journal_id = ?", (logical_id,))
    return None


def _apply_evt(conn, evt, *, projection_writes=True):
    """Dispatch one evt line to its fold applier by `payload.kind` (step 4a
    replay + the emit_model_a apply path). A kind with a bespoke `applier`
    (weekly_credit_effects, five_hour_block_close) uses it; everything else
    goes through the generic column-map fold. Apply-only: NO alert dispatch,
    NO ctx — replay is structurally unable to fire alerts (spec §5.2 step 4a)."""
    spec = _EVT_SPECS.get((evt.get("payload") or {}).get("kind"))
    if spec is not None and spec.applier is _apply_weekly_credit_effects:
        return spec.applier(
            conn, evt, projection_writes=projection_writes)
    if spec is not None and spec.applier is not None:
        return spec.applier(conn, evt)
    return _apply_generic_evt(conn, evt)


# --------------------------------------------------------------------------
# harvest registry (natural-keyed families, spec §5.3)
# --------------------------------------------------------------------------

# Every harvest family's natural key now leads with `account_key` (#341): the
# account is part of each table's extended UNIQUE, so the opaque evt id must
# include it to stay a bijection with the row (two accounts sharing a physical
# window / week / threshold produce DISTINCT evt ids). account_key is also a
# plain payload column, so the generic fold round-trips it back onto the row.
_HARVEST_SPECS = [
    _HarvestSpec(
        "week_reset_events", "week_reset", "wr",
        id_parts=("account_key", "old_week_end_at", "new_week_end_at"),
        at_column="detected_at_utc", order=30, suppression=True,
    ),
    _HarvestSpec(
        "five_hour_reset_events", "five_hour_credit", "fhc",
        id_parts=("account_key", "five_hour_window_key", "effective_reset_at_utc"),
        at_column="detected_at_utc", order=30, suppression=True,
    ),
    _HarvestSpec(
        "five_hour_blocks", "five_hour_block_close", "fhbc",
        id_parts=("account_key", "five_hour_window_key"),
        at_column="last_updated_at_utc", order=40, closed_only=True,
        children=_BLOCK_CHILDREN,
    ),
    _HarvestSpec(
        "percent_milestones", "percent_milestone", "pm",
        id_parts=("account_key", "week_start_date", "reset_event_id",
                  "percent_threshold"),
        fk_refs={
            "usage_snapshot_id": ("weekly_usage_snapshots", "usage_snapshot_ref"),
            "cost_snapshot_id": ("weekly_cost_snapshots", "cost_snapshot_ref"),
            "reset_event_id": ("week_reset_events", "reset_event_ref"),
        },
        at_column="captured_at_utc", order=60,
    ),
    _HarvestSpec(
        "five_hour_milestones", "five_hour_milestone", "fhm",
        id_parts=("account_key", "five_hour_window_key", "reset_event_id",
                  "percent_threshold"),
        fk_refs={
            "usage_snapshot_id": ("weekly_usage_snapshots", "usage_snapshot_ref"),
            "reset_event_id": ("five_hour_reset_events", "reset_event_ref"),
        },
        # block_id points at the OPEN five_hour_blocks row (a projection with no
        # journal_id) — re-derive it at fold from the journaled (account_key,
        # window key) composite (#341) instead of a broken logical FK.
        derived_fk={"block_id": ("five_hour_blocks", "five_hour_window_key")},
        at_column="captured_at_utc", order=60,
    ),
    _HarvestSpec(
        "budget_milestones", "budget", "bm",
        id_parts=("account_key", "vendor", "period_start_at", "period",
                  "threshold"),
        at_column="crossed_at_utc", order=60,
    ),
    _HarvestSpec(
        "projected_milestones", "projected", "pjm",
        id_parts=("account_key", "week_start_at", "period", "metric",
                  "threshold"),
        at_column="crossed_at_utc", order=60,
    ),
    _HarvestSpec(
        "project_budget_milestones", "project_budget", "pbm",
        id_parts=("account_key", "week_start_at", "project_key", "threshold"),
        at_column="crossed_at_utc", order=60,
    ),
]


# Evt fold specs, keyed by `payload.kind`. The Model-A families are declared
# here (snapshot_accept + weekly_cost_snapshot are generic column-map folds;
# weekly_credit_effects is effects-only). The natural-keyed harvest families
# contribute the INVERSE of their `fk_refs` so one registry drives both harvest
# (rowid -> logical) and fold (logical -> rowid) without drift. `order` is the
# FK-dependency fold order (referenced families before referencing ones).
_EVT_SPECS = {
    "snapshot_accept": _EvtSpec("weekly_usage_snapshots", order=10),
    "weekly_cost_snapshot": _EvtSpec("weekly_cost_snapshots", order=20),
    "weekly_credit_effects": _EvtSpec(
        None, order=50, applier=_apply_weekly_credit_effects),
    # Quota-alert arming state (Task 7 Item 5): an independent stats.db table
    # with no FK into the journal-covered families and its own natural-key
    # upsert applier. order is arbitrary among evts (no cross-family FK).
    "quota_alert_arming": _EvtSpec(
        None, order=45, applier=_apply_quota_alert_arming),
    # Terminal quota alert evidence (#416 spec §7.2). Order 44 — BEFORE
    # `quota_alert_arming` (45) and before the quota projection
    # re-materialization, so the journaled terminal fact is already in place
    # when the rebuild's re-derivation runs. Both directions are safe anyway:
    # this applier converges by natural-key upsert and the re-derivation's own
    # insert is `INSERT OR IGNORE`, so neither can clobber the other's row.
    "quota_threshold_event": _EvtSpec(
        None, order=44, applier=_apply_quota_threshold_event),
}
for _hs in _HARVEST_SPECS:
    if _hs.children:
        _applier = _apply_block_close
    elif _hs.suppression:
        _applier = _apply_reset_with_suppression
    else:
        _applier = None
    _EVT_SPECS[_hs.kind] = _EvtSpec(
        _hs.table,
        fk_refs={ref_key: (col, ref_table)
                 for col, (ref_table, ref_key) in _hs.fk_refs.items()},
        order=_hs.order,
        applier=_applier,
        derived_fk=dict(_hs.derived_fk),
    )


# --------------------------------------------------------------------------
# Model-A emission + harvest (spec §5.3, step 4c)
# --------------------------------------------------------------------------

def emit_model_a(ctx, *, kind, evt_id, table, columns, refs=None, at=None):
    """Emit one Model-A evt (spec §5.3): append the evt line FIRST (leaf lock,
    inside the txn — legal per the lock-order law; fsync'd before the commit
    that indexes it), then apply it through the SAME fold applier replay uses,
    so live emission and crash-replay converge by construction. Used by 6b's
    obs-derivation hooks for the no-natural-key families — `snapshot_accept`
    (the ONLY writer of weekly_usage_snapshots now), `weekly_cost_snapshot`,
    and `weekly_credit_effects` (effects-only, `table=None`).

    `columns` are the target row's canonical column values; `refs` (optional)
    are logical-FK ref payload keys the fold resolves to rowids. `at` should be
    the triggering record's capture time (`ctx.as_of_for(record)`) for replay
    determinism. Returns the target row's rowid (freshly inserted OR a converged
    crash-replay — 6b callers need it for FK linkage regardless of which cycle
    inserted it), or None for an effects-only family (`table=None`).
    """
    payload = dict(columns)
    if refs:
        payload.update(refs)
    evt = _lib_journal.make_evt(kind=kind, id=evt_id, at=(at or _now_iso()),
                                payload=payload)
    if ctx.event_sink is not None:
        # SCRATCH planning (`db rederive`, spec §6): capture EVERY derived
        # candidate — never classify, never drop, or the very divergence the
        # planner exists to correct would be filtered out of `desired_events`
        # and the diff would report a false no-op. Model-A events are still
        # APPLIED to the private scratch projection because callers consume the
        # returned rowid immediately (`snapshot_accept` stores it before
        # milestone/block derivation). Live effective metadata is never read or
        # written. Discriminated on `is not None` — the sink is `list | None`
        # and an EMPTY sink list is falsy.
        ctx.event_sink.append(evt)
        ctx.events_emitted += 1
        _apply_evt(ctx.conn, evt, projection_writes=ctx.projection_writes)
    else:
        decision = _classify_live_effective_event(ctx.conn, evt)
        if decision == CLASSIFY_CONFLICT:
            # No append, no metadata mutation — converge the row instead.
            _record_dropped_conflict(ctx, evt)
            _converge_row_from_effective(ctx.conn, evt_id, table=table)
        else:
            # `new` AND `duplicate` both still append: crash-replay convergence
            # and two-bootstrap idempotency are built on that.
            append_record(evt)
            ctx.events_emitted += 1
            if decision == CLASSIFY_NEW:
                _record_new_effective_event(ctx.conn, evt)
                _apply_evt(
                    ctx.conn, evt, projection_writes=ctx.projection_writes)
    if table is None:
        return None
    row = ctx.conn.execute(
        f"SELECT id FROM {table} WHERE journal_id = ?", (evt_id,)
    ).fetchone()
    return int(row[0]) if row is not None else None


def _build_harvest_evt(ctx, spec, row):
    """Build the evt for one harvested natural-keyed row (spec §5.3): map the
    plain columns, replace FK rowids with their referenced row's logical id
    (reverse lookup), embed rollup children, and assemble the opaque natural-key
    id from `spec.id_prefix` + `spec.id_parts` (FK parts contribute their
    logical id).

    For a suppression family (`week_reset`/`five_hour_credit`, Design B) the
    reset's destructive effects also ride the evt: the list of logical
    `journal_id`s the live pipeline hook captured (in `ctx.suppression_map`,
    keyed on this row's `id_parts` values) is attached as `payload["suppression"]`
    so the effects replay deterministically. The id stays the pure natural key —
    `suppression` is an effect, never an id component."""
    conn = ctx.conn
    fk_cols = set(spec.fk_refs.keys())
    # derived-FK columns (e.g. block_id) are NOT journaled — the raw rowid is
    # not stable and the fold re-derives them from a journaled natural key.
    skip_cols = fk_cols | set(spec.derived_fk.keys())
    payload = {}
    for key in row.keys():
        if key in ("id", "journal_id") or key in skip_cols:
            continue
        payload[key] = row[key]
    refs = {}
    for col, (ref_table, ref_key) in spec.fk_refs.items():
        logical = _reverse_ref(conn, ref_table, row[col])
        if logical is None:
            raise JournalError(
                f"harvest {spec.kind}: unresolved FK {col} -> {ref_table} "
                "(referenced row has no journal_id — harvest-order violation)")
        payload[ref_key] = logical
        refs[col] = logical
    for payload_key, child_table in spec.children:
        child_rows = conn.execute(
            f"SELECT * FROM {child_table} WHERE block_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        payload[payload_key] = [
            {k: cr[k] for k in cr.keys() if k not in ("id", "block_id")}
            for cr in child_rows
        ]
    parts = [refs[name] if name in fk_cols else row[name]
             for name in spec.id_parts]
    if spec.suppression:
        supp = ctx.suppression_map.get(tuple(row[name] for name in spec.id_parts))
        if supp:
            payload["suppression"] = list(supp)
    eid = _lib_journal.evt_id(spec.id_prefix, *parts)
    at = row[spec.at_column] if spec.at_column else _now_iso()
    return _lib_journal.make_evt(kind=spec.kind, id=eid, at=at, payload=payload)


def _emit_harvest_row(ctx, spec, row):
    """Journal and stamp one already-selected natural-keyed row."""
    conn = ctx.conn
    evt = _build_harvest_evt(ctx, spec, row)
    if ctx.event_sink is not None:
        # Scratch planning: capture + stamp the PRIVATE projection so a later
        # raw record does not re-harvest the same row and its downstream FKs
        # still resolve. Never classify, drop, or touch live metadata.
        ctx.event_sink.append(evt)
        ctx.events_emitted += 1
        conn.execute(
            f"UPDATE {spec.table} SET journal_id = ? WHERE id = ?",
            (evt["id"], row["id"]),
        )
        return evt

    decision = _classify_live_effective_event(conn, evt)
    if decision == CLASSIFY_CONFLICT:
        _record_dropped_conflict(ctx, evt)
        _converge_row_from_effective(
            conn, evt["id"], table=spec.table, rowid=row["id"]
        )
        return evt

    append_record(evt)
    ctx.events_emitted += 1
    if decision == CLASSIFY_NEW:
        _record_new_effective_event(conn, evt)
    else:
        # An exact crash-retry duplicate still validates excluded derived FKs
        # before it stamps the physical row.
        _validate_excluded_derived_fks(conn, spec, row)
    conn.execute(
        f"UPDATE {spec.table} SET journal_id = ? WHERE id = ?",
        (evt["id"], row["id"]),
    )
    return evt


def freeze_five_hour_block_close(ctx, block_id: int):
    """Freeze one closed block immediately as a complete replayable fact.

    Unlike the end-of-cycle table scan, this surface also accepts an already
    stamped row so a lost-commit retry can re-emit the exact duplicate selected
    by its retained closure trigger. It never derives from cache state itself;
    the parent and both child sets present at this call are the durable boundary.
    """
    spec = next(
        item for item in _HARVEST_SPECS
        if item.kind == "five_hour_block_close"
    )
    row = ctx.conn.execute(
        "SELECT * FROM five_hour_blocks WHERE id = ?", (int(block_id),)
    ).fetchone()
    if row is None:
        raise JournalError(
            f"cannot freeze five_hour_block_close: missing block {block_id}"
        )
    if int(row["is_closed"]) != 1:
        raise JournalError(
            f"cannot freeze five_hour_block_close: block {block_id} is open"
        )
    return _emit_harvest_row(ctx, spec, row)


def _harvest(ctx) -> None:
    """Step 4c: journal + stamp every natural-keyed row inserted this cycle
    (`journal_id IS NULL`). Families harvest in dependency order so a referenced
    family (resets, blocks) stamps its journal_id before a referencing family
    (milestones) reverse-looks-it-up (spec §5.3 / Appendix B I4 P2-8). Each evt
    is appended+fsync'd before its row is stamped, inside the cycle's txn."""
    conn = ctx.conn
    for spec in sorted(_HARVEST_SPECS, key=lambda s: s.order):
        where = "journal_id IS NULL"
        if spec.closed_only:
            where += " AND is_closed = 1"
        rows = conn.execute(
            f"SELECT * FROM {spec.table} WHERE {where} ORDER BY id"
        ).fetchall()
        for row in rows:
            _emit_harvest_row(ctx, spec, row)


# --------------------------------------------------------------------------
# pipeline (step 4b) + post-commit alert dispatch (step 6)
# --------------------------------------------------------------------------

def _pipeline_op_fold(ctx, record) -> None:
    """Built-in pipeline hook: fold an obs/op record whose `payload.kind` has a
    registered `FOLD_APPLIERS` entry (spec §5.3 "fold op" — the
    weekly_credit_floor op ships here). No-op for every other record."""
    applier = FOLD_APPLIERS.get((record.get("payload") or {}).get("kind"))
    if applier is not None:
        applier(ctx.conn, record)


PIPELINE.append(_pipeline_op_fold)


def _dispatch_pending_alerts(alerts: list) -> None:
    """Default post-commit dispatch (spec §5.2 step 6): fire each queued alert
    payload through the cctally dispatch glue (bin/_lib_alert_dispatch via
    `_dispatch_alert_notification`). Failures are logged, never raised — a bad
    payload can't suppress healthy ones, and a dispatch failure never rolls back
    a committed milestone (set-then-dispatch, docs/alerts-gotchas.md)."""
    cctally = sys.modules.get("cctally")
    dispatch = getattr(cctally, "_dispatch_alert_notification", None) if cctally else None
    if dispatch is None:
        return
    for payload in alerts:
        try:
            dispatch(payload, mode="real")
        except Exception as exc:  # pragma: no cover — best-effort dispatch
            print(f"[alerts] dispatch failed: {exc}", file=sys.stderr)


def _load_config_once() -> dict:
    """Read config once per cycle for the pipeline hooks (spec §5.2 step 4b).
    Config-at-ingest is acceptable because derived records are journaled — replay
    never re-derives, so a config change between capture and ingest only shifts
    which cycle derived the event. Only called for a non-empty batch, so an empty
    cycle never touches config."""
    cctally = sys.modules.get("cctally")
    if cctally is not None and hasattr(cctally, "load_config"):
        try:
            return cctally.load_config()
        except Exception:
            return {}
    return {}


def _run_config_reconcile(ctx, reconcile_config) -> None:
    """Design C (DB journal redesign §5.3): run the three Task-5 budget-reconcile
    chokepoints INSIDE the cycle transaction, on `ctx.conn`, after the batch
    pipeline and BEFORE harvest — so any newly-latched crossing row (journal_id
    NULL, `commit=False`) is picked up by the natural-keyed budget harvest and
    journaled as a `budget`/`projected`/`project_budget` evt.

    `reconcile_config` is `{"budget": <validated_budget>, "touched_projects":
    set | None, "axes": set | None}` (6c widening + 6f axes gate).
    `touched_projects` threads into the per-project reconcile: a SCOPED `budget
    set/unset --project` write (6e/6f) passes `{root}` so touching project A never
    latches a sibling project B's crossed-but-not-yet-dispatched threshold — which
    would permanently suppress B's real alert (memory: the per-project reconcile's
    `touched_projects` contract). `None` reconciles every configured project (the
    config-set / dashboard-toggle / wholesale `budget.projects` "suppress the
    retroactive storm for all" case).

    `axes` ⊆ {"budget", "codex_budget", "project_budget"} names which reconcile
    axes to run — the per-call-site touched-leaf mapping (6f writer reroute), so
    a `budget set` write reconciles ONLY the global axis and never latches a
    Codex/project crossing that its config write didn't touch. `axes = None`
    runs ALL three axes (the pre-6f behavior; kept so a caller that doesn't scope
    still reconciles everything).

    There is NO journaled op line for a config write; this is a LIVE-only entry
    (never seen at rebuild — replay of the harvested budget evts reproduces the
    latched rows). The reconcile family is stamp-no-dispatch by construction
    (retroactive-storm suppression), so it never pushes to `ctx.pending_alerts`;
    passing the sink is vacuous for this path — the latch is recorded and
    journaled, never popped. `as_of=None` lets each reconcile stamp at its own
    (live) moment, which the harvest then freezes into the evt.
    """
    cctally = sys.modules.get("cctally")
    if cctally is None or not reconcile_config:
        return
    validated_budget = reconcile_config.get("budget")
    touched_projects = reconcile_config.get("touched_projects")
    axes = reconcile_config.get("axes")  # None => run all (pre-6f behavior)
    if not validated_budget:
        return

    def _wants(axis: str) -> bool:
        return axes is None or axis in axes

    # The per-call guard is NOT the redundant belt the 6c order flagged for
    # removal: each reconcile self-guards best-effort AND never re-raises (the
    # reconcile family is stamp-no-dispatch and MUST NOT break the cycle, unlike
    # the milestone chokepoints that re-raise on a passed conn). Keeping the
    # guard here is deliberate defense-in-depth for that "never break the cycle
    # over a reconcile" contract — it holds even if a future reconcile forgets
    # its own self-guard. (P3 disposition: justified-in-comment, not dropped.)
    for axis, name in (
        ("budget", "_reconcile_budget_on_config_write"),
        ("codex_budget", "_reconcile_codex_budget_on_config_write"),
    ):
        if not _wants(axis):
            continue
        fn = getattr(cctally, name, None)
        if fn is None:
            continue
        try:
            fn(validated_budget, conn=ctx.conn)
        except Exception as exc:  # best-effort; never break the cycle over a reconcile
            print(f"[budget-reconcile] {name} failed: {exc}", file=sys.stderr)
    # Per-project reconcile takes `touched_projects` as its 2nd positional
    # (scoped-vs-wholesale, above); split out from the loop for that one extra arg.
    if _wants("project_budget"):
        proj_fn = getattr(cctally, "_reconcile_project_budget_milestones_on_write", None)
        if proj_fn is not None:
            try:
                proj_fn(validated_budget, touched_projects, conn=ctx.conn)
            except Exception as exc:  # best-effort; never break the cycle over a reconcile
                print(
                    "[budget-reconcile] _reconcile_project_budget_milestones_on_write "
                    f"failed: {exc}",
                    file=sys.stderr,
                )


def reconcile_budget_config(validated_budget, *, axes, touched_projects=None):
    """Route a budget-config-write forward-only reconcile THROUGH the ingest
    cycle (spec §5.3 Design C / Appendix A "dashboard config-change / forward-
    only budget reconciliations → opportunistic ingest cycle").

    The single chokepoint the 6f writer reroute points every budget-config write
    site at (`budget set/set-codex/set-project/unset-project`, `config set
    budget.*`, dashboard POST /api/settings): instead of opening its own stats.db
    connection and writing the latched crossings directly (the last remaining
    direct-writer class), the site names the axes its touched config leaves feed
    and this runs those reconciles INSIDE `run_stats_ingest` on the cycle
    connection — so the latched crossing rows are journaled by the budget harvest
    and become rebuild-replayable.

    Mode is OPPORTUNISTIC and the whole thing is exception-wrapped: a config
    write must NEVER fail (or block on a busy ingest lock) because a forward-only
    reconcile could not run — it is a best-effort retroactive-storm suppression,
    identical to today's fire-and-forget semantics. `axes` ⊆ {"budget",
    "codex_budget", "project_budget"}; empty `axes` (or a falsy budget) is a
    no-op. `touched_projects` scopes the per-project reconcile (spec §5.3)."""
    if not validated_budget or not axes:
        return
    try:
        run_stats_ingest(
            mode="opportunistic",
            reconcile_config={
                "budget": validated_budget,
                "touched_projects": touched_projects,
                "axes": set(axes),
            },
        )
    except Exception as exc:  # best-effort; a config write must never fail here
        print(f"[budget-reconcile] ingest reconcile failed: {exc}", file=sys.stderr)


# Fold order for an evt whose kind is unknown to this binary — sorts LAST so a
# future kind never wedges before a known referenced family (additive tolerance).
_UNKNOWN_EVT_SPEC = _EvtSpec(None, order=999)


def _fold_order(evt) -> int:
    kind = (evt.get("payload") or {}).get("kind")
    return (_EVT_SPECS.get(kind) or _UNKNOWN_EVT_SPEC).order


def _write_effective_metadata(conn, selection) -> None:
    """Replace the disposable effective-event summary from a pure selection."""
    conn.execute("DELETE FROM journal_effective_events")
    for event_id, selected in selection.by_id.items():
        event_json = None
        if selected.record is not None:
            event_json = (
                _lib_journal.encode_line(selected.record)
                .decode("utf-8")
                .rstrip("\n")
            )
        conn.execute(
            "INSERT INTO journal_effective_events "
            "(event_id, rev, status, content_hash, batch_id, event_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_id,
                selected.rev,
                selected.status,
                selected.content_hash,
                selected.batch_id,
                event_json,
            ),
        )
    _write_protocol_violations(
        conn,
        selection.protocol_violations,
        selection.acknowledged_protocol_violations,
    )


def _write_protocol_violations(conn, violations, acknowledged=()) -> None:
    """Replace the disposable structural-violation summary."""
    conn.execute("DELETE FROM journal_protocol_violations")
    rows = [*violations, *acknowledged]
    rows.sort(
        key=lambda violation: (
            violation.batch_id,
            violation.kind,
            violation.fingerprint,
        )
    )
    for violation in rows:
        conn.execute(
            "INSERT INTO journal_protocol_violations "
            "(fingerprint, batch_id, kind, violation_json) "
            "VALUES (?, ?, ?, ?)",
            (
                violation.fingerprint,
                violation.batch_id,
                violation.kind,
                json.dumps(
                    violation.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )


def _metadata_row(conn, event_id):
    return conn.execute(
        "SELECT rev, status, content_hash, batch_id "
        "FROM journal_effective_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()


def _metadata_event_record(conn, event_id):
    row = conn.execute(
        "SELECT event_json FROM journal_effective_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _lib_journal.decode_line(row[0].encode("utf-8"))


def _legacy_qaa_can_advance(conn, selected) -> bool:
    return (
        _lib_journal.is_legacy_quota_arming_record(selected.record)
        and _lib_journal.is_legacy_quota_arming_record(
            _metadata_event_record(conn, selected.event_id)
        )
    )


def _insert_effective_metadata(conn, selected) -> None:
    event_json = None
    if selected.record is not None:
        event_json = (
            _lib_journal.encode_line(selected.record).decode("utf-8").rstrip("\n")
        )
    conn.execute(
        "INSERT INTO journal_effective_events "
        "(event_id, rev, status, content_hash, batch_id, event_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            selected.event_id,
            selected.rev,
            selected.status,
            selected.content_hash,
            selected.batch_id,
            event_json,
        ),
    )


CLASSIFY_NEW = "new"
CLASSIFY_DUPLICATE = "duplicate"
CLASSIFY_CONFLICT = "conflict"


def _classify_live_effective_event(conn, evt) -> str:
    """Decide what a freshly derived evt means against the live effective
    metadata — WITHOUT mutating anything (#374 §6).

    The whole point of the split is ordering: both emit paths call this BEFORE
    `append_record`, so a conflicting emission is never written. Previously the
    append ran first and the check raised afterwards, so the divergent line
    landed in the append-only journal and the rollback could not take it back —
    poisoning every subsequent rebuild.

    Returns `CLASSIFY_NEW` (no prior effective event, or a legacy `qaa` state
    stream that may advance), `CLASSIFY_DUPLICATE` (byte-identical to the prior
    effective event) or `CLASSIFY_CONFLICT` (same revision, different content).
    Raises `CorrectionRebuildRequired` on a revision mismatch — a completed
    correction batch outranks any live emission and stays FATAL.
    """
    selected = _lib_journal.resolve_effective_events([evt]).by_id[evt["id"]]
    prior = _metadata_row(conn, selected.event_id)
    if prior is None:
        return CLASSIFY_NEW
    prior_rev, prior_status, prior_hash, prior_batch = prior
    if int(prior_rev) == selected.rev:
        if prior_status != selected.status or prior_hash != selected.content_hash:
            if _legacy_qaa_can_advance(conn, selected):
                return CLASSIFY_NEW
            return CLASSIFY_CONFLICT
        return CLASSIFY_DUPLICATE
    raise CorrectionRebuildRequired(
        f"event {selected.event_id} rev {selected.rev} conflicts with effective "
        f"rev {prior_rev} from {prior_batch or 'base journal'}",
        batch_id=prior_batch,
        event_id=selected.event_id,
        high_water=_correction_commit_high_water(prior_batch),
        expected_metadata=(
            int(prior_rev),
            prior_status,
            prior_hash,
            prior_batch,
        ),
    )


def _record_new_effective_event(conn, evt) -> None:
    """Write the effective-metadata row for a `CLASSIFY_NEW` emission. The
    DELETE covers the legacy `qaa` advance (the only case where a prior row is
    replaced rather than absent)."""
    selected = _lib_journal.resolve_effective_events([evt]).by_id[evt["id"]]
    if _metadata_row(conn, selected.event_id) is not None:
        conn.execute(
            "DELETE FROM journal_effective_events WHERE event_id = ?",
            (selected.event_id,),
        )
    _insert_effective_metadata(conn, selected)


def _record_live_effective_event(conn, evt) -> bool:
    """Record a newly folded base event; return False when the caller must NOT
    apply it — an exact duplicate, or a quarantined same-revision conflict.

    Retained for the step-4a replay site, whose conflicts the preflight reader
    has already dropped. The emit paths use the classifier directly so they can
    withhold the append and converge the row."""
    decision = _classify_live_effective_event(conn, evt)
    if decision == CLASSIFY_NEW:
        _record_new_effective_event(conn, evt)
        return True
    return False


def _record_dropped_conflict(ctx, evt) -> None:
    """Count + report one withheld emission (spec §8: a one-line stderr note per
    dropped emission, and a count on the cycle summary)."""
    selected = _lib_journal.resolve_effective_events([evt]).by_id[evt["id"]]
    ctx.conflicts_dropped.append(
        DroppedConflict(
            event_id=selected.event_id,
            rev=selected.rev,
            rejected_hash=selected.content_hash,
        )
    )
    print(
        f"[journal] withheld a divergent emission for {selected.event_id} "
        f"rev {selected.rev}; converged the row from the journaled event",
        file=sys.stderr,
    )


def _effective_event_for_convergence(conn, event_id) -> dict:
    """The ACTIVE, validated journal record the live row must converge to.

    Fails closed (#374 §6): `decode_line` only checks that the value is an object
    with a string `t`, and a tombstoned selection deliberately stores
    `event_json` as NULL — so a same-revision active-vs-tombstone conflict has no
    record to converge from. Missing, tombstoned, or hash-mismatched metadata
    raises here and the caller therefore NEVER stamps."""
    row = conn.execute(
        "SELECT rev, status, content_hash, event_json "
        "FROM journal_effective_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise _lib_journal.JournalProtocolError(
            f"cannot converge {event_id}: no effective metadata")
    _rev, status, content_hash, event_json = row
    if status != "active" or event_json is None:
        raise _lib_journal.JournalProtocolError(
            f"cannot converge {event_id}: effective selection is {status!r} "
            "with no retained record")
    record = _lib_journal.decode_line(event_json.encode("utf-8"))
    if (
        record is None
        or record.get("t") != "evt"
        or record.get("id") != event_id
        or not isinstance(record.get("payload"), dict)
    ):
        raise _lib_journal.JournalProtocolError(
            f"cannot converge {event_id}: retained record is not a matching evt")
    if _lib_journal._sha256_canonical(record) != content_hash:
        raise _lib_journal.JournalProtocolError(
            f"cannot converge {event_id}: retained record hash mismatch")
    return record


# Effect keys that ride an evt payload but are NOT target-table columns.
_EVT_EFFECT_KEYS = frozenset(
    {"kind", "suppression", "suppression_table", "floor_suppression", "hwm_floor"}
)


def _evt_target_columns(conn, evt, spec) -> tuple:
    """Decode one evt payload into `(columns, children)` for its target row —
    the same mapping `_apply_generic_evt` performs, but WITHOUT inserting, so a
    convergence can UPDATE an existing physical row."""
    payload = evt.get("payload") or {}
    cols = {"journal_id": evt["id"]}
    children: dict = {}
    for key, value in payload.items():
        if key in _EVT_EFFECT_KEYS:
            continue
        if key in _BLOCK_CHILD_KEYS:
            children[key] = value or []
            continue
        if key in spec.fk_refs:
            column, ref_table = spec.fk_refs[key]
            cols[column] = _resolve_ref(conn, ref_table, value)
        else:
            cols[key] = value
    acct = cols.get("account_key")
    for column, (ref_table, lookup_col) in spec.derived_fk.items():
        cols[column] = _derived_fk_value(
            conn, ref_table, lookup_col, cols.get(lookup_col), acct)
    return cols, children


CONVERGE_DROPPED = "dropped"
CONVERGE_APPLIED = "converged"


def _converge_row_from_effective(conn, event_id, *, table=None, rowid=None) -> str:
    """Bring the live physical row into agreement with the already-journaled
    effective event, and stamp `journal_id` in the SAME operation (#374 §6).

    This is an EXPLICIT row-convergence operation, deliberately NOT a generic
    re-run of an arbitrary fold applier: `_apply_generic_evt` ends in
    `INSERT OR IGNORE`, and effect-bearing appliers cannot safely run out of
    canonical order. `five_hour_block_close` is the strengthened exception:
    its ordinary fold also converges the parent and exact child sets so orphan
    replay freezes an existing projection before raw derivation. Keeping the
    explicit convergence path still avoids invoking destructive effects and
    supports row-targeted conflict repair.

    Effect-bearing families are NOT converged. `event_json` is authoritative row
    *data*, never permission to invoke every applier: `_apply_weekly_credit_
    effects` performs destructive deletes and writes a non-transactional HWM
    projection, and the reset appliers replay suppression deletes. Replaying an
    older effective event at the CURRENT execution point is not equivalent to
    folding it at its canonical journal position. So an effects-only family
    (`spec.table is None`) returns `CONVERGE_DROPPED` and nothing is replayed.

    A family WITH a table but no physical row carrying the event id is also
    `CONVERGE_DROPPED`: convergence updates what exists, it never materialises a
    row (see the `rowid is None` branch).
    """
    record = _effective_event_for_convergence(conn, event_id)
    spec = _EVT_SPECS.get((record.get("payload") or {}).get("kind"))
    if spec is None or spec.table is None:
        return CONVERGE_DROPPED
    target = spec.table
    if table is not None and table != target:
        raise JournalError(
            f"convergence target mismatch for {event_id}: {table} != {target}")
    cols, children = _evt_target_columns(conn, record, spec)
    if rowid is None:
        row = conn.execute(
            f"SELECT id FROM {target} WHERE journal_id = ?", (event_id,)
        ).fetchone()
        rowid = int(row[0]) if row is not None else None
    if rowid is None:
        # NOTHING to converge — and materializing a row here would be wrong twice
        # over (#374 review). A row absent because a suppression effect
        # deliberately DELETED it would be resurrected, so the live index and a
        # rebuild would diverge — the very contract convergence exists to hold.
        # And an insert swallowed by a natural-key UNIQUE would leave the
        # follow-up lookup empty and abort the whole cycle on a `JournalError`.
        # Drop and report; the emission was already withheld by the caller.
        print(
            f"[journal] no live row for {event_id} in {target}; "
            "dropped the divergent emission without materializing one",
            file=sys.stderr,
        )
        return CONVERGE_DROPPED
    assignments = ", ".join(f"{name} = ?" for name in cols)
    conn.execute(
        f"UPDATE {target} SET {assignments} WHERE id = ?",
        (*cols.values(), int(rowid)),
    )
    if spec.applier is _apply_block_close:
        identity = conn.execute(
            "SELECT account_key, five_hour_window_key "
            "FROM five_hour_blocks WHERE id = ?",
            (int(rowid),),
        ).fetchone()
        if identity is None:
            raise JournalError(
                f"convergence target vanished for {event_id}"
            )
        _replace_block_children(
            conn, int(rowid), identity[0], identity[1], children
        )
    return CONVERGE_APPLIED


def _validate_excluded_derived_fks(conn, spec, row) -> None:
    """Validate every column the harvest EXCLUDES from the evt, before the
    duplicate path stamps the row (#374 §6 / acceptance 8).

    Byte identity of the emitted event proves the JOURNALED columns match. It
    proves nothing about the excluded ones: `_build_harvest_evt` omits
    `journal_id`, physical ids and derived-FK columns, and
    `five_hour_milestones.block_id` is deliberately derived rather than
    journaled — so a milestone pointing at the WRONG block can emit an otherwise
    byte-identical event. The canonical logical dump also excludes `block_id`
    and would not catch it.

    An unresolvable reference is NOT an error on its own. `_derived_fk_value`
    returns 0 as the "no such parent" sentinel and the fold appliers store that
    same 0, so a legitimately parentless row — e.g. a `five_hour_milestones` row
    whose `five_hour_blocks` replica the 5h-credit stale-replica DELETE removed —
    carries `actual == expected == 0` and is in agreement. Raising on that shape
    escaped `_harvest`, rolled the cycle back, left the row unstamped and made
    every later cycle repeat it (#374 review). ONLY disagreement is fatal."""
    if not spec.derived_fk:
        return
    keys = set(row.keys())
    account_key = row["account_key"] if "account_key" in keys else None
    for column, (ref_table, lookup_col) in spec.derived_fk.items():
        expected = _derived_fk_value(
            conn, ref_table, lookup_col, row[lookup_col], account_key)
        actual = row[column]
        if int(actual) != expected:
            raise JournalError(
                f"harvest {spec.kind}: derived FK {column}={actual!r} does not "
                f"resolve to {ref_table}.{lookup_col}={row[lookup_col]!r} "
                f"(re-derived {expected})")


def _full_effective_selection(hw):
    records = []
    evidence = []
    prior_high_water = None
    if hw is not None:
        for segment, offset, raw in _read_range(None, hw):
            record = _lib_journal.decode_line(raw)
            if record is not None:
                _capture_protocol_prefix_evidence(
                    record,
                    prior_high_water,
                    evidence,
                )
                records.append(record)
            prior_high_water = (segment, offset + len(raw) + 1)
    cutover_claude = resolve_cutover_claude_account()
    for record in records:
        _normalize_legacy_account_stamp(record, cutover_claude)
    return _lib_journal.resolve_effective_events(
        records,
        protocol_prefix_evidence=evidence,
    )


def _correction_commit_high_water(batch_id, hw=None):
    """Return the exact end offset of one completed-batch commit marker.

    The batch was already structurally validated either by the full effective
    selector or by the live metadata row that names it. The earliest matching
    commit is the narrowest complete prefix and remains stable even when later
    journal bytes or crash-replayed duplicate markers exist.
    """
    if not batch_id:
        return None
    if hw is None:
        hw = journal_high_water()
    if hw is None:
        return None
    for segment, offset, raw in _read_range(None, hw):
        record = _lib_journal.decode_line(raw)
        if (
            record is not None
            and record.get("t") == "correction_batch"
            and record.get("phase") == "commit"
            and record.get("id") == batch_id
        ):
            return (segment, offset + len(raw) + 1)
    return None


def _preflight_live_events(
    conn, records, hw, conflicts=None, protocol_scan=None
):
    """Validate unread evt/correction records before the stats transaction.

    A READER (#374 §6): the evt records it inspects are already durably in the
    journal, so same-revision divergence must NOT raise here — that raise wedged
    every cycle over an already-poisoned journal, exactly like the rebuild. The
    divergent evt is DROPPED from the apply set, the prior effective event
    stands, and the group is appended to `conflicts` when a sink is supplied.
    `CorrectionRebuildRequired` stays fatal."""
    event_records = [record for record in records if record.get("t") == "evt"]
    selected_new = _lib_journal.resolve_effective_events(event_records)
    if conflicts is not None:
        conflicts.extend(selected_new.conflicts)
    to_apply = []
    for evt in selected_new.active:
        selected = selected_new.by_id[evt["id"]]
        prior = _metadata_row(conn, selected.event_id)
        if prior is None:
            to_apply.append(evt)
            continue
        prior_rev, prior_status, prior_hash, prior_batch = prior
        if int(prior_rev) == selected.rev:
            if prior_status != selected.status or prior_hash != selected.content_hash:
                if _legacy_qaa_can_advance(conn, selected):
                    to_apply.append(evt)
                    continue
                if conflicts is not None:
                    conflicts.append(
                        _lib_journal.EventConflict(
                            event_id=selected.event_id,
                            rev=selected.rev,
                            content_hashes=tuple(
                                sorted({prior_hash, selected.content_hash})),
                            selected_hash=prior_hash,
                        )
                    )
                print(
                    f"[journal] quarantined a divergent journal event for "
                    f"{selected.event_id} rev {selected.rev}; the prior "
                    "effective event stands",
                    file=sys.stderr,
                )
                continue
            continue
        raise CorrectionRebuildRequired(
            f"event {selected.event_id} rev {selected.rev} conflicts with "
            f"effective rev {prior_rev} from {prior_batch or 'base journal'}",
            batch_id=prior_batch,
            event_id=selected.event_id,
            high_water=_correction_commit_high_water(prior_batch, hw),
            expected_metadata=(
                int(prior_rev),
                prior_status,
                prior_hash,
                prior_batch,
            ),
        )

    if any(
        record.get("t") in {"correction", "correction_batch"}
        or (
            record.get("t") == "op"
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("kind")
            == _lib_journal._PROTOCOL_RESOLUTION_KIND
        )
        for record in records
    ):
        full = _full_effective_selection(hw)
        if protocol_scan is not None:
            protocol_scan["scanned"] = True
            protocol_scan["violations"] = full.protocol_violations
            protocol_scan["acknowledged"] = (
                full.acknowledged_protocol_violations
            )
        for selected in full.by_id.values():
            if selected.batch_id is None:
                continue
            prior = _metadata_row(conn, selected.event_id)
            if prior is not None:
                prior_tuple = (int(prior[0]), prior[1], prior[2], prior[3])
                selected_tuple = (
                    selected.rev,
                    selected.status,
                    selected.content_hash,
                    selected.batch_id,
                )
                if prior_tuple == selected_tuple:
                    continue
            raise CorrectionRebuildRequired(
                f"completed correction batch {selected.batch_id} requires "
                "stats index rebuild",
                batch_id=selected.batch_id,
                event_id=selected.event_id,
                high_water=_correction_commit_high_water(selected.batch_id, hw),
                expected_metadata=(
                    selected.rev,
                    selected.status,
                    selected.content_hash,
                    selected.batch_id,
                ),
                recovery_eligible=True,
            )
    return to_apply


# --------------------------------------------------------------------------
# the cycle (spec §5.2, revision 3)
# --------------------------------------------------------------------------

def _run_cycle(conn: sqlite3.Connection, *, reconcile_config=None,
               codex_apply=None, post_commit=None) -> IngestResult:
    # Step 1: HW snapshot (leaf lock, µs). Lines appended after this — by other
    # processes OR by this cycle's own evt emission — are past HW and belong to
    # the next cycle (§5.2.1, closes the skipped-append race).
    hw = journal_high_water()
    # An empty journal (no segments yet) has nothing to consume. Normally that is
    # a no-op cycle — BUT a LIVE-only entry that appends no journal line of its
    # own must still run even on a still-empty journal: the Design-C budget-config
    # reconcile (§5.3, 6f) AND the Codex `codex_apply` leg (Task 7 — the quota
    # projection re-materializer + on-demand codex budget/projected firings; a
    # user may run `cctally budget` or a Codex hook-tick before any Claude usage
    # is recorded). In that case fall through with an empty batch and no cursor to
    # advance — any harvested budget evt lands in the freshly-created first
    # segment past the (absent) HW and replays idempotently on the next cycle.
    decoded: list = []  # (record, segment, offset)
    malformed = 0
    cursor_target = None
    if hw is None:
        if reconcile_config is None and codex_apply is None:
            return IngestResult(ran=True, consumed=0, malformed=0,
                                events_emitted=0, alerts=[])
    else:
        hw_seg, hw_size = hw

        # Step 2: read cursor -> HW in canonical order; decode, counting
        # malformed. Keep each record's (segment, offset) so the cache leg can
        # truncate the cursor on a prefix-stop.
        cursor = _read_cursor(conn)
        for seg, off, raw in _read_range(cursor, hw):
            rec = _lib_journal.decode_line(raw)
            if rec is None:
                malformed += 1
                continue
            decoded.append((rec, seg, off))

        # Step 3: cache leg (Codex quota) BEFORE the stats txn (lock-order law).
        # QUOTA_APPLIER attempts the global-then-Codex cache flock NB upsert; on
        # a busy flock it returns a prefix-stop index — the cycle processes
        # decoded[:stop], sets the cursor to decoded[stop]'s offset, and retries
        # the remainder next cycle (§5.2 step 3; prefix consumption keeps the
        # scalar cursor sound).
        cursor_target = (hw_seg, hw_size)
        if QUOTA_APPLIER is not None:
            stop = QUOTA_APPLIER(decoded)
            if stop is not None:
                _rec, stop_seg, stop_off = decoded[stop]
                cursor_target = (stop_seg, stop_off)
                decoded = decoded[:stop]

    records = [r for (r, _s, _o) in decoded]
    batch = [r for r in records if r.get("t") in ("obs", "op")]
    # #374: the preflight reader quarantines same-revision divergence instead of
    # raising; the groups it drops are counted on the cycle summary.
    preflight_conflicts: list = []
    protocol_scan: dict = {}
    journal_evts = _preflight_live_events(
        conn,
        records,
        cursor_target,
        conflicts=preflight_conflicts,
        protocol_scan=protocol_scan,
    )

    # Step 4: ONE BEGIN IMMEDIATE — replay + pipeline + derived-fact journaling +
    # cursor advance, atomic (§5.2 crash boundary). A crash before COMMIT rolls
    # back rows + cursor together; the fsync'd evt lines replay idempotently in
    # the next cycle's step 4a.
    ctx = IngestContext(conn=conn, batch=batch,
                        config=(_load_config_once() if batch else None))
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 4a. Replay journal evt lines (a prior cycle's emission that landed past
        # its own HW, or a crashed cycle's orphans). Apply-only, sorted by fold
        # order so a referenced family (snapshots, resets, blocks) resolves
        # before a referencing one (milestones); NO ctx, so replay is
        # structurally unable to fire an alert (§5.2 step 4a).
        for evt in sorted(journal_evts, key=_fold_order):
            if _record_live_effective_event(conn, evt):
                _apply_evt(conn, evt)
        # 4b. Per-record sequential pipeline over obs/op in canonical order —
        # sequential is REQUIRED (reset/credit detection precedes the same
        # record's snapshot-accept; a reset-spanning batch needs prior records'
        # effects already applied). Hooks emit Model-A evts and push alert
        # payloads to ctx.pending_alerts.
        pipeline_changes_before = conn.total_changes
        for rec in batch:
            for hook in PIPELINE:
                hook(ctx, rec)
        # 4b'. Design C (§5.3): run the live-only budget-config reconcile INSIDE
        # the txn, after the pipeline and BEFORE harvest, so any newly-latched
        # crossing row is journaled by the budget harvest below. No op line is
        # journaled for it — it is never seen at rebuild.
        if reconcile_config is not None:
            _run_config_reconcile(ctx, reconcile_config)
        # 4b''. Codex leg (Task 7): the quota projection re-materializer +
        # on-demand codex budget/projected alert firings, run on ctx.conn inside
        # the txn, AFTER the pipeline and BEFORE harvest so any newly-latched
        # budget/projected crossing (a natural-keyed harvest family) is journaled
        # below. The quota projection tables + arming are written by the closure
        # itself (arming via its own `quota_alert_arming` evt). Alerts land in
        # ctx.pending_alerts for the post-commit dispatch. A `_before_stats_commit`
        # hook (used by the reconcile's crash-consistency callers) fires at the
        # end of the closure — inside this txn, before COMMIT — so a raise rolls
        # the whole cycle back (invariant ii).
        if codex_apply is not None:
            codex_apply(ctx)
        # 4c. Journal + stamp the natural-keyed rows the pipeline inserted.
        # Early-out (Task 6 gate P2): the ONLY source of `journal_id IS NULL`
        # rows is a Task-5 chokepoint called from a step-4b pipeline hook —
        # step-4a replay and step-4b Model-A emit both set journal_id. So when
        # the pipeline wrote nothing (empty batch, or an all-replay cycle, or a
        # hook that short-circuited before any write), the 8 harvest scans have
        # nothing to find; skip them. When it DID write, we harvest (the
        # per-table partial `WHERE journal_id IS NULL` index keeps each scan
        # O(this-cycle inserts) even on the accept-only common tick).
        if conn.total_changes != pipeline_changes_before:
            _harvest(ctx)
        # 4c'. Fold-time `last_seen_utc` derivation (#341): advance each account's
        # last-seen from the max `at` of any account-stamped line this cycle. A
        # no-op when the batch carries no account stamps (byte-stable on a
        # pre-multi-account single-account install).
        _derive_account_last_seen(conn, records)
        # A full correction-prefix preflight is authoritative for the
        # disposable protocol summary. Replace it in the same transaction as
        # the cursor so shallow Doctor paths observe either the old complete
        # result or the new complete result, never an in-between state.
        if protocol_scan.get("scanned"):
            _write_protocol_violations(
                conn,
                protocol_scan.get("violations", ()),
                protocol_scan.get("acknowledged", ()),
            )
        # 4d. Advance the cursor (to HW, or to the cache-leg prefix boundary).
        # `cursor_target is None` ONLY on a reconcile-only cycle over a still-
        # empty journal (§5.2 above): there are no consumed lines to advance
        # past, and the harvest's budget evts land in the freshly-created first
        # segment past the (absent) HW — replayed idempotently next cycle. So do
        # not touch the cursor here.
        if cursor_target is not None:
            _write_cursor(conn, cursor_target[0], cursor_target[1])
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    # `post_commit` (the reconcile's `_after_stats_commit` seam) fires AFTER the
    # commit and BEFORE alert dispatch — the committed state stands, and a raise
    # propagates (authoritative re-raises), skipping dispatch exactly as the
    # legacy reconcile's after-commit-then-cert-then-dispatch order did.
    if post_commit is not None:
        post_commit()

    # Step 6: dispatch alerts post-commit, from the step-4b sink ONLY (never from
    # step-4a replay). A crash between 5 and 6 loses at most one dispatch — the
    # set-then-dispatch trade (§5.2 step 6; docs/alerts-gotchas.md).
    alerts = list(ctx.pending_alerts)
    if alerts:
        (ALERT_DISPATCHER or _dispatch_pending_alerts)(alerts)

    return IngestResult(ran=True, consumed=len(records), malformed=malformed,
                        events_emitted=ctx.events_emitted, alerts=alerts,
                        conflicts_dropped=(len(ctx.conflicts_dropped)
                                           + len(preflight_conflicts)))


def _run_stats_ingest_once(
    *,
    mode: str = "opportunistic",
    timeout_s: float = 10.0,
    conn: sqlite3.Connection | None = None,
    reconcile_config=None,
    codex_apply=None,
    post_commit=None,
) -> IngestResult:
    """Run one single-flight attempt, without correction-recovery orchestration.

    `mode="opportunistic"` takes the ingest lock non-blocking (busy → `ran=False`;
    the current holder consumes the lines). `mode="authoritative"` waits up to
    `timeout_s` for the lock so a caller observes its own appended line
    synchronously. Pass `conn` to run the cycle on an existing stats.db
    connection; otherwise a fresh `open_db()` connection is opened and closed.

    `reconcile_config` (Design C, §5.3): `{"budget": <validated_budget>,
    "touched_projects": set | None, "axes": set | None}` to reconcile LIVE inside
    this cycle (never journaled as an op — the latched crossings ride the budget
    harvest); `touched_projects` scopes the per-project reconcile (6c widening),
    `axes` ⊆ {"budget","codex_budget","project_budget"} names which axes run
    (6f writer reroute; `None` runs all). Prefer `reconcile_budget_config(...)`
    as the opportunistic+wrapped entry point from config-write sites. `None`
    skips reconcile entirely.

    `codex_apply` (Task 7): a `(ctx) -> None` closure run on `ctx.conn` inside the
    txn (step 4b'', after the pipeline, before harvest). It is the seam every
    Codex on-demand stats.db writer routes through — the quota projection
    re-materializer (`reconcile_codex_quota_projection`) and the on-demand codex
    budget/projected alert firings — so those writers become single-flight instead
    of opening their own stats connections. `post_commit` (`() -> None`) fires
    AFTER the commit, before dispatch (the reconcile's `_after_stats_commit` seam).

    Exception discipline (6b-gate P2): a pipeline-hook/chokepoint exception aborts
    the cycle — `_run_cycle` rolls back the txn and re-raises, so no cursor
    advance and no partial commit survive (invariant ii). `run_stats_ingest`
    catches at this boundary: an OPPORTUNISTIC ingest logs the failure loudly and
    returns `IngestResult(ran=True, error=<exc>)` so a statusline/hook tick is
    never broken; an AUTHORITATIVE ingest re-raises so its caller (record-usage,
    record-credit, sync-week, statusline publication) sees the failure.
    """
    own_conn = conn is None
    maintenance_fd = None
    lock_fd = None
    try:
        # Let open_db resolve an epoch mismatch or classified corruption before
        # this caller owns any lock. It can therefore take maintenance EX ->
        # ingest in the required order. A fresh/legacy DB is different: its
        # open runs the one-time schema/cutover path, so serialize that whole
        # path under maintenance EX and downgrade to SH before taking ingest.
        # For a current/mismatched epoch, open first, then take maintenance SH
        # and verify the main-file identity did not change across the open; if
        # a sibling rebuilt in that gap, discard the stale handle and retry.
        if own_conn:
            while True:
                raw_epoch = _stats_db_user_version()
                if (
                    raw_epoch is None
                    or raw_epoch <= _cctally_core.LEGACY_STATS_HEAD
                ):
                    maintenance_fd = _acquire_maintenance_exclusive(
                        mode, timeout_s
                    )
                    if maintenance_fd is None:
                        return IngestResult(
                            ran=False,
                            consumed=0,
                            malformed=0,
                            events_emitted=0,
                            alerts=[],
                    )
                    identity_before = _stats_db_identity()
                    conn = _cctally_core.open_db()
                    _downgrade_maintenance_shared(maintenance_fd)
                else:
                    identity_before = _stats_db_identity()
                    conn = _cctally_core.open_db()
                    maintenance_fd = _acquire_maintenance_shared(
                        mode, timeout_s
                    )
                if maintenance_fd is None:
                    if conn is not None:
                        conn.close()
                        conn = None
                    return IngestResult(
                        ran=False,
                        consumed=0,
                        malformed=0,
                        events_emitted=0,
                        alerts=[],
                    )
                identity_after = _stats_db_identity()
                opened_epoch = conn.execute("PRAGMA user_version").fetchone()[0]
                epoch_ok = (
                    opened_epoch <= _cctally_core.LEGACY_STATS_HEAD
                    or opened_epoch == _cctally_core.STATS_INDEX_EPOCH
                )
                if (
                    identity_before == identity_after
                    and epoch_ok
                ):
                    break
                _release_maintenance_shared(maintenance_fd)
                maintenance_fd = None
                conn.close()
                conn = None
        else:
            maintenance_fd = _acquire_maintenance_shared(mode, timeout_s)
            if maintenance_fd is None:
                return IngestResult(
                    ran=False,
                    consumed=0,
                    malformed=0,
                    events_emitted=0,
                    alerts=[],
                )

        lock_fd = _acquire_ingest_lock(mode, timeout_s)
        if lock_fd is None:
            if own_conn and conn is not None:
                conn.close()
                conn = None
            return IngestResult(
                ran=False,
                consumed=0,
                malformed=0,
                events_emitted=0,
                alerts=[],
            )
        try:
            # #386: declare the sanctioned steady-state write regime for the
            # duration of the cycle. Two consumers: the Stage 3 authorizer, and
            # `holds_ingest_lock()` — a corruption surfacing from INSIDE the
            # cycle (via a nested `open_db()`, e.g. the cross-DB stats read on
            # the quota leg) reaches the heal hook while this process already
            # owns journal.ingest.lock, and the heal must recognise itself as
            # the serialized writer rather than wait 5s for a lock it holds.
            import _cctally_store
            with _cctally_store.stats_write_scope("ingest", ingest_lock=True):
                return _run_cycle(conn, reconcile_config=reconcile_config,
                                  codex_apply=codex_apply,
                                  post_commit=post_commit)
        except CorrectionRebuildRequired:
            # The public boundary must unwind its transaction, internally owned
            # connection, ingest lock, and maintenance-shared lock before it can
            # seek maintenance EXCLUSIVE in total order.
            raise
        except Exception as exc:
            if mode == "authoritative":
                raise
            print(f"[ingest] opportunistic cycle aborted, cursor unmoved: {exc}",
                  file=sys.stderr)
            return IngestResult(ran=True, consumed=0, malformed=0,
                                events_emitted=0, alerts=[], error=exc)
        finally:
            # Guard on ``conn is not None`` so a failing ``open_db()`` (e.g.
            # StatsDbCorruptError) surfaces its real error instead of an
            # AttributeError from ``None.close()`` masking it.
            if own_conn and conn is not None:
                conn.close()
    finally:
        if lock_fd is not None:
            _release_ingest_lock(lock_fd)
        if maintenance_fd is not None:
            _release_maintenance_shared(maintenance_fd)


def _correction_recovery_guidance(cause) -> str:
    detail = str(cause)
    lower = detail.lower()
    holder = (
        "open handle" in lower
        or "family is still open" in lower
        or "open in process" in lower
    )
    prefix = ""
    if holder:
        prefix = (
            "stop the dashboard or other process holding stats.db open, then "
        )
    return (
        f"{detail}; {prefix}run `cctally db rebuild --db stats` and retry"
    )


def _correction_error_result(error) -> IngestResult:
    print(
        f"[ingest] correction recovery declined, cursor unmoved: {error}",
        file=sys.stderr,
    )
    return IngestResult(
        ran=True,
        consumed=0,
        malformed=0,
        events_emitted=0,
        alerts=[],
        error=error,
    )


def _correction_index_converged(signal: CorrectionRebuildRequired) -> bool:
    """Revalidate the triggering effective row without open-time mutation."""
    if signal.event_id is None or signal.expected_metadata is None:
        return False
    path = pathlib.Path(_cctally_core.DB_PATH)
    if not path.exists():
        return False
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT rev, status, content_hash, batch_id "
            "FROM journal_effective_events WHERE event_id = ?",
            (signal.event_id,),
        ).fetchone()
        if row is None:
            return False
        return (
            int(row[0]),
            row[1],
            row[2],
            row[3],
        ) == tuple(signal.expected_metadata)
    finally:
        conn.close()


def _correction_scratch_mains() -> set[pathlib.Path]:
    path = pathlib.Path(_cctally_core.DB_PATH)
    prefix = path.name + ".rebuilding-"
    return {
        member
        for member in path.parent.glob(prefix + "*")
        if not member.name.endswith(("-wal", "-shm"))
    }


def _cleanup_new_correction_scratches(before: set[pathlib.Path]) -> None:
    for scratch in _correction_scratch_mains() - before:
        try:
            _remove_db_family(scratch)
        except OSError:
            pass


def _recover_completed_correction(
    signal: CorrectionRebuildRequired,
    *,
    mode: str,
    timeout_s: float,
) -> None:
    """Revalidate and, when still needed, replace through the trigger prefix."""
    if (
        signal.batch_id is None
        or signal.event_id is None
        or signal.high_water is None
        or signal.expected_metadata is None
    ):
        raise CorrectionRecoveryError(
            _correction_recovery_guidance(
                "completed correction lacks an exact validated commit high-water"
            )
        )

    maintenance_fd = _acquire_maintenance_exclusive(mode, timeout_s)
    if maintenance_fd is None:
        raise CorrectionRecoveryError(
            _correction_recovery_guidance(
                "stats maintenance lock is busy"
            )
        )
    ingest_fd = None
    try:
        ingest_fd = _acquire_ingest_lock(mode, timeout_s)
        if ingest_fd is None:
            raise CorrectionRecoveryError(
                _correction_recovery_guidance(
                    "another ingest holds journal.ingest.lock"
                )
            )

        # A sibling may have rebuilt after the original attempt unwound. The
        # locked re-check prevents redundant preservation/publication.
        try:
            converged = _correction_index_converged(signal)
        except Exception as exc:
            raise CorrectionRecoveryError(
                _correction_recovery_guidance(
                    f"correction revalidation failed: {exc}"
                )
            ) from exc
        if converged:
            return

        import _cctally_db
        if _cctally_db._would_block_prod_stats(_cctally_core.DB_PATH):
            raise CorrectionRecoveryError(
                _correction_recovery_guidance(
                    "refusing to rebuild the prod stats.db from a dev checkout"
                )
            )

        import _cctally_store
        scratches_before = _correction_scratch_mains()
        try:
            with _cctally_store.stats_write_scope(
                "maintenance-correction-rebuild",
                ingest_lock=True,
            ):
                rebuild_stats_index(
                    context=RebuildContext(
                        trigger="correction-recovery-in-band"
                    ),
                    high_water=signal.high_water,
                )
        except BaseException as exc:
            _cleanup_new_correction_scratches(scratches_before)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise CorrectionRecoveryError(
                _correction_recovery_guidance(exc)
            ) from exc
    finally:
        if ingest_fd is not None:
            _release_ingest_lock(ingest_fd)
        _release_maintenance_shared(maintenance_fd)


def run_stats_ingest(
    *,
    mode: str = "opportunistic",
    timeout_s: float = 10.0,
    conn: sqlite3.Connection | None = None,
    reconcile_config=None,
    codex_apply=None,
    post_commit=None,
) -> IngestResult:
    """Run one cycle, healing one completed-correction mismatch when safe.

    The initial attempt fully unwinds before recovery seeks maintenance
    EXCLUSIVE then ingest. Recovery revalidates, rebuilds through the exact
    triggering commit, releases both locks, and retries once on a freshly opened
    current-family connection. Caller-owned connections are never closed or
    replaced. A second correction signal is surfaced with the manual remedy.
    """
    kwargs = {
        "mode": mode,
        "timeout_s": timeout_s,
        "conn": conn,
        "reconcile_config": reconcile_config,
        "codex_apply": codex_apply,
        "post_commit": post_commit,
    }
    try:
        return _run_stats_ingest_once(**kwargs)
    except CorrectionRebuildRequired as signal:
        if not signal.recovery_eligible:
            raise
        if conn is not None:
            raise CorrectionRebuildRequired(
                _correction_recovery_guidance(
                    "automatic correction recovery cannot replace a "
                    "caller-owned stats.db connection"
                ),
                batch_id=signal.batch_id,
                event_id=signal.event_id,
                high_water=signal.high_water,
                expected_metadata=signal.expected_metadata,
                recovery_eligible=True,
            ) from signal

        try:
            _recover_completed_correction(
                signal,
                mode=mode,
                timeout_s=timeout_s,
            )
        except CorrectionRecoveryError as exc:
            if mode == "authoritative":
                raise
            return _correction_error_result(exc)

        retry_kwargs = dict(kwargs)
        retry_kwargs["conn"] = None
        try:
            result = _run_stats_ingest_once(**retry_kwargs)
        except Exception as exc:
            wrapped = CorrectionRecoveryError(
                _correction_recovery_guidance(
                    f"single correction-recovery retry failed: {exc}"
                )
            )
            if mode == "authoritative":
                raise wrapped from exc
            return _correction_error_result(wrapped)
        if result.error is not None:
            wrapped = CorrectionRecoveryError(
                _correction_recovery_guidance(
                    f"single correction-recovery retry failed: {result.error}"
                )
            )
            return _correction_error_result(wrapped)
        return result


# ==========================================================================
# Rebuild — a FRESH stats index from the journal alone (spec §5.4, Task 8 Item 1)
# ==========================================================================
#
# `rebuild_stats_index` makes stats.db DISPOSABLE: it replays the whole journal
# in canonical `(segment, offset)` order into a fresh schema'd index (bootstrap
# segments before observation segments, per list_segments()), NEVER running the
# live PIPELINE — no Model-A emission, no harvest, no alert dispatch, no
# `reconcile_config`; every fold is apply-only. The accept/skip DECISIONS were
# journaled at capture (`snapshot_accept` evts), so rebuild replays decisions and
# NEVER re-derives reset-aware clamps — this is what closes the spanning-reset
# non-determinism (spec §5.3, Appendix B I4 P1-3).
#
# Fold order = `_fold_order` (referenced families before referencing ones) within
# the canonical stream, exactly as the live replay path (§5.2 step 4a) does —
# generalized to the whole journal. Two projection passes sit between the
# structural folds (< milestone order) and the milestone/budget folds (>=
# milestone order):
#   * the OPEN 5h block re-materialization (block-only), so a five_hour_milestone's
#     `block_id` derived_fk resolves against a real block row (§5.3 / Appendix B
#     I4 P2-8); and
#   * the quota `quota_*` projection re-materialization over the (journal-sourced)
#     cache.db `quota_window_snapshots`, run AFTER the `quota_alert_arming` evts
#     fold (order 45) so `honor-no-refire` holds.
#
# Duplicate evt lines with byte-identical payloads are LEGAL (crash-replay appends
# duplicates; the 6g/Task-7 purity fixes guarantee byte-identity) — every fold is
# idempotent (`INSERT OR IGNORE` on journal_id / natural key, DELETE-by-id,
# natural-key UPSERT), so rebuild is idempotent over them.
#
# The hwm-7d statusline file is NOT re-materialized by a dedicated pass — the SQL
# 7d-HWM clamp re-establishes the floor on the next statusline tick, so a stale/
# absent hwm-7d file self-heals (the only hwm-7d write during a rebuild is the
# incidental one inside a `weekly_credit_effects` credit-effect replay, harmless
# last-write-wins). Post-rebuild the cursor equals the journal high-water, so the
# next ingest is a no-op over the already-folded lines.

# op-fold order: floors (`weekly_credit_floor`) fold BEFORE snapshot_accept (10)
# and BEFORE any `weekly_credit_effects` (50) that deletes a PRIOR credit's floor.
_OP_FOLD_ORDER = 5
# fold-order threshold: milestone/budget folds (order 60) run in the second
# phase, after the open-block + quota projection re-materialization passes.
_REBUILD_MILESTONE_ORDER = 60

# Journal-covered families counted in the RebuildResult report (+ the two
# re-materialized quota projection families, useful for the operator command).
_REBUILD_COUNT_TABLES = (
    "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
    "five_hour_reset_events", "five_hour_blocks", "five_hour_block_models",
    "five_hour_block_projects", "weekly_credit_floors", "percent_milestones",
    "five_hour_milestones", "budget_milestones", "projected_milestones",
    "project_budget_milestones", "quota_alert_arming", "quota_window_blocks",
    "quota_percent_milestones", "quota_threshold_events", "accounts",
)


#: Every production path that reaches `rebuild_stats_index` against the live
#: destination, plus one test-only identity. Closed by construction: a value
#: outside this set is rejected by `RebuildContext.validate` (#496 S1 F3).
#: `test-fixture` is for harnesses only, and
#: `tests/test_stats_incident_identity.py` asserts no shipped call site emits it.
REBUILD_TRIGGERS = frozenset({
    "corruption-heal",
    "interrupted-rebuild-recovery",
    "db-rebuild",
    "journal-repair-acknowledge",
    "journal-repair-recovery",
    "rederive-apply",
    "rederive-recovery",
    "correction-recovery-in-band",
    "epoch-transition",
    "test-fixture",
})


@dataclass(frozen=True)
class RebuildContext:
    """Why this rebuild ran, and what evidence preceded it (#496 S1 F3).

    A bare identifier would not be enough: `trigger_error` and `forensics_path`
    cannot be derived from it, and both are what tie a quarantine incident to
    the forensics bundle written moments earlier.

    `record_path` is resolved by `rebuild_stats_index` itself, never by a
    caller, so preservation and the rebuild record name the same file.
    """

    trigger: str
    trigger_error: "str | None" = None
    forensics_path: "str | None" = None
    record_path: "str | None" = None

    def validate(self) -> "RebuildContext":
        if self.trigger not in REBUILD_TRIGGERS:
            raise ValueError(f"unknown rebuild trigger: {self.trigger!r}")
        if self.record_path is not None:
            # `rebuild_stats_index` overwrites this field unconditionally, so a
            # caller-supplied value would be silently discarded.
            raise ValueError(
                "record_path is resolved by rebuild_stats_index; callers must "
                "leave it unset"
            )
        return self


@dataclass
class RebuildResult:
    """Outcome of a `rebuild_stats_index` call (spec §5.4)."""

    rows_by_table: dict       # journal-covered table -> row count in the rebuild
    malformed: int            # journal lines that failed to decode (spec §4.4)
    duration_s: float         # wall time of the whole rebuild
    segments_read: int        # journal segments folded
    lines_folded: int         # op + evt lines applied (obs are rederive input)
    # #374: divergent same-revision groups quarantined behind a lowest-sequence
    # provisional winner. The rebuild COMPLETES and exits 0 — reporting them is
    # how we refuse to assert that a guessed winner is authoritative.
    conflicts: tuple = ()
    # #402 Task A: whole correction batches omitted after one of the seven
    # enumerated structural violations. The usable index still publishes, but
    # every operator surface must report that intended corrections were omitted.
    protocol_violations: tuple = ()
    # #402 Task B: exact violations the operator acknowledged as omitted. The
    # batches remain tainted; this is diagnostic/audit state, never validity.
    acknowledged_protocol_violations: tuple = ()
    quarantine_dir: "pathlib.Path | None" = None


def _remove_db_sidecars_strict(path) -> None:
    """Remove both sidecars or fail before publishing a replacement main file."""
    for suffix in ("-wal", "-shm"):
        candidate = pathlib.Path(str(path) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    _fsync_dir(pathlib.Path(path).parent)


def _remove_db_family(path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            pathlib.Path(str(path) + suffix).unlink()
        except OSError:
            pass


def _stats_rebuild_test_pause(point: str) -> None:
    """Private process-control seam for the #388 interrupted-rebuild tests."""
    if os.environ.get("CCTALLY_TEST_STATS_REBUILD_PAUSE_AT") != point:
        return
    marker = os.environ.get("CCTALLY_TEST_STATS_REBUILD_MARKER")
    if not marker:
        return
    pathlib.Path(marker).write_text(f"{os.getpid()}\n")
    os.kill(os.getpid(), signal.SIGSTOP)


_REBUILD_REQUIRED_TABLES = frozenset(
    {
        "accounts",
        "budget_milestones",
        "five_hour_block_models",
        "five_hour_block_projects",
        "five_hour_blocks",
        "five_hour_milestones",
        "five_hour_reset_events",
        "journal_cursor",
        "journal_effective_events",
        "journal_protocol_violations",
        "percent_milestones",
        "project_budget_milestones",
        "projected_milestones",
        "quota_alert_arming",
        "quota_percent_milestones",
        "quota_projection_ledger_state",
        "quota_projection_state",
        "quota_threshold_events",
        "quota_window_blocks",
        "schema_migrations",
        "schema_migrations_skipped",
        "stats_open_fixups",
        "stats_publication_stamp",
        "week_reset_events",
        "weekly_cost_snapshots",
        "weekly_credit_floors",
        "weekly_usage_snapshots",
    }
)
_REBUILD_REQUIRED_INDEXES = frozenset(
    {
        "idx_budget_milestones_journal_id",
        "idx_budget_milestones_journal_id_null",
        "idx_cost_week_start_at_time",
        "idx_cost_week_time",
        "idx_five_hour_block_models_block",
        "idx_five_hour_block_models_window",
        "idx_five_hour_block_projects_block",
        "idx_five_hour_block_projects_window",
        "idx_five_hour_blocks_block_start",
        "idx_five_hour_blocks_journal_id",
        "idx_five_hour_blocks_journal_id_null",
        "idx_five_hour_milestones_block",
        "idx_five_hour_milestones_journal_id",
        "idx_five_hour_milestones_journal_id_null",
        "idx_five_hour_reset_events_journal_id",
        "idx_five_hour_reset_events_journal_id_null",
        "idx_percent_milestones_journal_id",
        "idx_percent_milestones_journal_id_null",
        "idx_project_budget_milestones_journal_id",
        "idx_project_budget_milestones_journal_id_null",
        "idx_projected_milestones_journal_id",
        "idx_projected_milestones_journal_id_null",
        "idx_quota_blocks_active",
        "idx_quota_milestones_active",
        "idx_quota_threshold_events_active",
        "idx_usage_week_start_at_time",
        "idx_usage_week_time",
        "idx_week_reset_events_journal_id",
        "idx_week_reset_events_journal_id_null",
        "idx_weekly_cost_snapshots_journal_id",
        "idx_weekly_credit_floors_journal_id",
        "idx_weekly_usage_snapshots_5h_window_key",
        "idx_weekly_usage_snapshots_journal_id",
    }
)
# SHA-256 of the current epoch's non-internal table/index sqlite_schema rows,
# ordered by (type, name).  Unlike table-name checks, this catches a silently
# omitted column, constraint, partial predicate, or index definition.  An epoch
# schema change must update this contract alongside STATS_INDEX_EPOCH.
_REBUILD_SCHEMA_FINGERPRINT = (
    "7dde5a7995f441558d08b0204136824d6ff7208b221e576c79a76854b76aa178"
)


def _stats_schema_fingerprint(conn: sqlite3.Connection) -> str:
    rows = [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE type IN ('table', 'index') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]
    payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_rebuilt_stats_index(
    conn: sqlite3.Connection, high_water: "tuple[str, int] | None"
) -> None:
    """Validate the scratch index before it is eligible for publication."""
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise JournalError(
            "rebuilt stats index failed integrity_check: " + "; ".join(integrity)
        )

    epoch = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if epoch != _cctally_core.STATS_INDEX_EPOCH:
        raise JournalError(
            f"rebuilt stats index has epoch {epoch}, expected "
            f"{_cctally_core.STATS_INDEX_EPOCH}"
        )

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_tables = sorted(_REBUILD_REQUIRED_TABLES - tables)
    unexpected_tables = sorted(tables - _REBUILD_REQUIRED_TABLES)
    if missing_tables or unexpected_tables:
        raise JournalError(
            "rebuilt stats index table contract mismatch"
            f"; missing={missing_tables!r}; unexpected={unexpected_tables!r}"
        )

    indexes = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    missing_indexes = sorted(_REBUILD_REQUIRED_INDEXES - indexes)
    unexpected_indexes = sorted(indexes - _REBUILD_REQUIRED_INDEXES)
    if missing_indexes or unexpected_indexes:
        raise JournalError(
            "rebuilt stats index index contract mismatch"
            f"; missing={missing_indexes!r}; unexpected={unexpected_indexes!r}"
        )

    schema_fingerprint = _stats_schema_fingerprint(conn)
    if schema_fingerprint != _REBUILD_SCHEMA_FINGERPRINT:
        raise JournalError(
            "rebuilt stats index schema definition mismatch: "
            f"{schema_fingerprint}, expected {_REBUILD_SCHEMA_FINGERPRINT}"
        )

    # Force representative table and cursor B-tree reads.  Header readability
    # and a constant-only SELECT do not establish that the index is usable.
    conn.execute(
        "SELECT id, journal_id FROM weekly_usage_snapshots "
        "ORDER BY id DESC LIMIT 1"
    ).fetchall()
    cursor_row = conn.execute(
        "SELECT segment, offset, applied_segment, applied_offset "
        "FROM journal_cursor WHERE id = 1"
    ).fetchone()
    actual_cursor = (
        (str(cursor_row[0]), int(cursor_row[1]))
        if cursor_row is not None
        else None
    )
    applied_cursor = (
        (str(cursor_row[2]), int(cursor_row[3]))
        if cursor_row is not None
        and cursor_row[2] is not None
        and cursor_row[3] is not None
        else None
    )
    if actual_cursor != high_water or applied_cursor != high_water:
        raise JournalError(
            "rebuilt stats index cursor contract "
            f"(public={actual_cursor!r}, applied={applied_cursor!r}) "
            f"does not match pinned journal high-water {high_water!r}"
        )


def stats_index_matches_journal_prefix(
    path: pathlib.Path, high_water: "tuple[str, int] | None"
) -> bool:
    """Whether ``path`` is a fully valid materialization of ``high_water``.

    This is intentionally stronger than "the index has rows": it validates the
    full Task A publication contract and compares the disposable effective-event
    summary with the canonical journal selection.  A legitimate empty index
    therefore matches an empty selection, while a valid-looking empty/partial
    index over data-bearing journal events does not.
    """
    if not pathlib.Path(path).exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            _validate_rebuilt_stats_index(conn, high_water)
            decoded: list[dict] = []
            protocol_evidence = []
            prior_high_water = None
            if high_water is not None:
                for segment, offset, raw in _read_range(None, high_water):
                    record = _lib_journal.decode_line(raw)
                    if record is not None:
                        _capture_protocol_prefix_evidence(
                            record,
                            prior_high_water,
                            protocol_evidence,
                        )
                        decoded.append(record)
                    prior_high_water = (
                        segment,
                        offset + len(raw) + 1,
                    )
            cutover_claude = resolve_cutover_claude_account()
            for record in decoded:
                _normalize_legacy_account_stamp(record, cutover_claude)
            selection = _lib_journal.resolve_effective_events(
                decoded,
                protocol_prefix_evidence=protocol_evidence,
            )
            expected = []
            for event_id, selected in selection.by_id.items():
                event_json = None
                if selected.record is not None:
                    event_json = (
                        _lib_journal.encode_line(selected.record)
                        .decode("utf-8")
                        .rstrip("\n")
                    )
                expected.append(
                    (
                        event_id,
                        selected.rev,
                        selected.status,
                        selected.content_hash,
                        selected.batch_id,
                        event_json,
                    )
                )
            expected.sort(key=lambda row: row[0])
            actual = [
                tuple(row)
                for row in conn.execute(
                    "SELECT event_id, rev, status, content_hash, batch_id, "
                    "event_json FROM journal_effective_events ORDER BY event_id"
                )
            ]
            if actual != expected:
                return False
            expected_violation_rows = [
                *selection.protocol_violations,
                *selection.acknowledged_protocol_violations,
            ]
            expected_violation_rows.sort(
                key=lambda violation: (
                    violation.batch_id,
                    violation.kind,
                    violation.fingerprint,
                )
            )
            expected_violations = [
                json.dumps(
                    violation.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for violation in expected_violation_rows
            ]
            actual_violations = [
                str(row[0])
                for row in conn.execute(
                    "SELECT violation_json FROM journal_protocol_violations "
                    "ORDER BY batch_id, kind, fingerprint"
                )
            ]
            if actual_violations != expected_violations:
                return False
            for record in selection.active:
                if record.get("t") != "evt":
                    continue
                spec = _EVT_SPECS.get((record.get("payload") or {}).get("kind"))
                if spec is None or spec.table is None:
                    continue
                row = conn.execute(
                    f"SELECT id FROM {spec.table} WHERE journal_id = ?",
                    (record["id"],),
                ).fetchone()
                if row is None:
                    return False
                if spec.applier is _apply_block_close:
                    block_id = int(row[0])
                    payload = record.get("payload") or {}
                    for payload_key, child_table in _BLOCK_CHILDREN:
                        child_count = conn.execute(
                            f"SELECT COUNT(*) FROM {child_table} WHERE block_id = ?",
                            (block_id,),
                        ).fetchone()[0]
                        if int(child_count) != len(payload.get(payload_key) or ()):
                            return False
            return True
        finally:
            conn.close()
    except (
        OSError,
        sqlite3.DatabaseError,
        JournalError,
        _lib_journal.JournalProtocolError,
    ):
        return False


def _prepare_existing_stats_for_cutover(path: pathlib.Path) -> str:
    """Checkpoint a readable old index so removing its sidecars is kill-safe.

    Returns what it actually did, so the incident manifest can say whether the
    explicit checkpoint ran (#496 S1 F8). Failure still RAISES rather than
    returning an outcome — the caller records `failed` and re-raises, because
    proceeding past an undrained WAL would pair stale sidecars with the
    replacement main file.
    """
    import _cctally_db

    try:
        conn = sqlite3.connect(str(path), timeout=15.0)
        try:
            conn.execute("PRAGMA schema_version").fetchone()
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise JournalError(
                    "old stats index WAL could not be drained before cutover"
                )
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        # Auto-heal necessarily starts from an unreadable family. Preserve its
        # exact bytes below, then publish the already-validated replacement.
        if _cctally_db._is_sqlite_corruption_error(exc):
            return "skipped_corrupt"
        raise
    return "checkpointed"


def _utc_iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def read_publication_stamp(path):
    """Read `stats_publication_stamp` from ``path`` on a fresh read-only conn.

    Never raises. Returns the input `_lib_stats_publish.resolve_stamp` expects:

    - the exception that prevented the read, which resolves INDETERMINATE;
    - `None` when the read succeeded and named no publication;
    - the list of row mappings the table held.

    **A destination whose `user_version` is not this binary's
    `STATS_INDEX_EPOCH` returns `None`, and that is a proof rather than a
    convenience.** Every scratch eligible for publication has already been
    validated at `STATS_INDEX_EPOCH`, and the publication transaction stamps
    that epoch onto the destination in the same commit as the stamp row, so a
    committed publication always leaves the destination at this epoch. A
    destination at any other epoch therefore proves this publication did not
    commit — which is exactly what makes an interrupted upgrade rebuild
    recoverable: the epoch-1007 index it was publishing into has no stamp
    table at all, and reading that absence as INDETERMINATE would condemn a
    perfectly healthy index instead of discarding a marker that never became
    live. When the epochs differ in the other direction, a newer binary reading
    an older destination, the same conclusion holds and the epoch gate refuses
    the destination anyway.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            epoch = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if epoch != _cctally_core.STATS_INDEX_EPOCH:
                return None
            rows = conn.execute(
                "SELECT record_path FROM stats_publication_stamp"
            ).fetchall()
        finally:
            conn.close()
    except BaseException as exc:
        return exc
    if not rows:
        return None
    return [{"record_path": row[0]} for row in rows]


def in_place_publication_proven_predecessor(destination, state) -> bool:
    """Whether a PENDING in-place marker's publication provably never committed.

    True only on `PROVEN_PREDECESSOR`: the stamp was read and does not name
    this marker's record, so the live bytes are the untouched predecessor and
    the marker may be discarded. `MATCH` means the verdict is still owed, and
    `INDETERMINATE` fails closed — it must never collapse into either of the
    other two states, because discarding on an unreadable stamp is exactly the
    silent-acceptance class the publication transaction exists to close.

    Public because both discriminator sites consume it: the opener's
    `_cctally_store._pending_stats_publication_never_replaced` and this
    module's `_settle_prior_publication_verdict`.
    """
    import _lib_stats_publish as sp

    record_path = state.get("recordPath")
    verdict = sp.resolve_stamp(
        read_publication_stamp(destination),
        record_path if isinstance(record_path, str) else None,
    )
    return verdict == sp.STAMP_PROVEN_PREDECESSOR


def _stamp_identity_error(path, expected_record_path: str) -> "str | None":
    import _lib_stats_publish as sp

    verdict = sp.resolve_stamp(
        read_publication_stamp(path), expected_record_path
    )
    if verdict == sp.STAMP_MATCH:
        return None
    return (
        "published stats index does not carry this publication's stamp "
        f"({verdict}); expected {expected_record_path}"
    )


def validate_published_stats_index(
    path, high_water: "tuple[str, int] | None", *,
    expected_record_path: "str | None" = None,
) -> "str | None":
    """Validate an index on a FRESH read-only connection (#496 S1 F1).

    Returns None on success, or a short failure reason. The building
    connection wrote the pages it then validated, so it is not an independent
    witness to what reached the disk; this reopens the file instead. The
    mechanism is already proven by `stats_index_matches_journal_prefix`, which
    runs the same check on the same kind of connection.

    ``expected_record_path`` names the publication whose bytes these are meant
    to be, and the stamp row is verified against it (#496 S3 §5): the
    high-water alone answers "is this index a correct materialization of the
    journal prefix", not "is this index the generation THIS publication just
    installed". The in-place publisher passes it because it has just written
    that identity inside the publication transaction. The opener deliberately
    does NOT: it has already resolved the stamp as a three-state question, and
    folding that into a boolean validation error would turn an INDETERMINATE
    read into a settled `failed` verdict.

    Public because `stats_open_guarded` runs exactly this check when it
    resolves a pending publication marker.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            _validate_rebuilt_stats_index(conn, high_water)
        finally:
            conn.close()
    except BaseException as exc:
        return f"{type(exc).__name__}: {exc}"[:500]
    if expected_record_path is not None:
        return _stamp_identity_error(path, expected_record_path)
    return None


def _publication_marker_path(destination) -> pathlib.Path:
    return pathlib.Path(str(destination) + ".publication")


def _write_publication_marker(
    destination, record_path, *, started_at: str, scratch_path,
    status: str = "pending", error: "str | None" = None,
    prior: "dict | None" = None, mechanism: str = "replace",
) -> None:
    """Publish the durable marker a later opener honours (#496 S1 F1).

    Mirrors the existing `cache.db.repairing` marker idiom.
    `_atomic_write_private_json` writes at mode 0600 and fsyncs both the file
    and its parent directory.

    `scratchPath` records the exact index this publication was about to install.
    Across processes `os.replace` is the only thing that consumes a scratch, so
    its presence or absence on disk is an exact answer to "did this run replace
    the destination?" — which is what interrupted-rebuild recovery needs in
    order to tell a marker it supersedes from one whose verdict is still owed.
    (Within one process the claim is weaker; see
    `_cctally_store._pending_stats_publication_never_replaced`.)

    `mechanism` states which publication protocol this marker belongs to, so
    the opener SELECTS its discriminator instead of inferring one (#496 S3 §5).
    `replace` keeps the `scratchPath` proxy above, which remains exactly
    correct there. `in_place` attaches the scratch read-only and leaves it on
    disk whether the transaction committed or rolled back, so the proxy
    inverts and the publication's own `stats_publication_stamp` row answers
    instead.

    `priorFailure` carries a settled verdict this publication is about to
    overwrite, so a crash before `os.replace` cannot discard it — see
    `_settle_prior_publication_verdict`.
    """
    import _cctally_db

    payload = {
        "schemaVersion": 1,
        "status": status,
        "recordPath": str(record_path),
        "startedAtUtc": started_at,
        "scratchPath": str(scratch_path),
        "mechanism": mechanism,
    }
    if error is not None:
        payload["error"] = error
    if prior:
        payload["priorFailure"] = prior
    _cctally_db._atomic_write_private_json(
        _publication_marker_path(destination), payload
    )
    _fsync_dir(pathlib.Path(destination).parent)


def _read_publication_marker(destination) -> "dict | None":
    """The marker's state as a MAPPING, or None when no marker exists.

    Present-but-unusable bytes read as an empty mapping, for the reason given
    in `_cctally_store._read_stats_publication_marker`.
    """
    try:
        state = json.loads(_publication_marker_path(destination).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _pending_publication_owes_nothing(destination, state) -> bool:
    """Whether a PENDING marker's own publication never reached the live bytes.

    The marker STATES its mechanism, so the discriminator is selected rather
    than inferred (#496 S3 §5). `replace` keeps the `scratchPath` proxy, which
    is exactly correct there because across processes `os.replace` is the only
    thing that consumes a scratch. `in_place` attaches its scratch read-only
    and leaves it on disk whether the transaction committed or rolled back, so
    that proxy INVERTS and the publication's own stamp answers instead. Neither
    is generalized over the other.

    A marker written before the mechanism field existed reads as `replace`,
    which is what those binaries did.
    """
    if str(state.get("mechanism") or "replace") == "in_place":
        return in_place_publication_proven_predecessor(destination, state)
    scratch = state.get("scratchPath")
    return (
        isinstance(scratch, str)
        and bool(scratch)
        and pathlib.Path(scratch).exists()
    )


def _settle_prior_publication_verdict(destination) -> "dict | None":
    """Settle the verdict a PREVIOUS publication still owes, before this one
    overwrites the single marker slot (#496 S1 F1).

    `<db>.publication` is one file, so Phase 1 of a new publication destroys
    whatever the last one left there. `cmd_db_rebuild` takes maintenance
    EXCLUSIVE without opening the live database through `stats_open_guarded`,
    so a rebuild can legitimately begin while a pending marker still owes a
    verdict on an already-published, never-validated index. If this run then
    dies between its own marker write and its `os.replace`, the next opener
    sees a pending marker beside this run's own scratch, discards it as
    never-replaced, and accepts the earlier index having never validated it.

    Resolving is preferred over refusing to overwrite, because a refusal would
    wedge the one operation that repairs a bad index behind the marker that
    reports it. Returns the verdict to CARRY into this run's marker, or None
    when nothing is owed:

    - a `failed` marker is already settled and is carried verbatim;
    - a `pending` marker whose own publication provably never became live owes
      nothing about the live bytes of its own — but it may still be CARRYING an
      older run's verdict, which is passed through so a third consecutive
      crashed run cannot drop it;
    - any other `pending` marker is settled HERE, by validating the destination
      against its record's pinned high-water — the same check the opener would
      have run. Success clears it; failure is written to both the marker and
      the record before this run touches the destination, and is then carried
      forward.

    A marker that cannot be judged (no record, or no pinned high-water) is left
    to the opener's existing discard policy rather than wedging the rebuild.
    """
    import _cctally_db

    state = _read_publication_marker(destination)
    if not state:
        return None
    status = str(state.get("status") or "")
    if status == "failed":
        return state
    if status != "pending":
        return None
    if _pending_publication_owes_nothing(destination, state):
        # This marker owes nothing itself, but dropping what it carries would
        # lose an older run's verdict once a third run crashes the same way.
        carried = state.get("priorFailure")
        return carried if isinstance(carried, dict) and carried else None
    record_path = state.get("recordPath")
    record = None
    if isinstance(record_path, str):
        try:
            record = json.loads(pathlib.Path(record_path).read_text())
        except (OSError, ValueError):
            record = None
    if not isinstance(record, dict) or "highWater" not in record:
        return None
    raw = record.get("highWater")
    high_water = (
        (str(raw[0]), int(raw[1]))
        if isinstance(raw, (list, tuple)) and len(raw) == 2
        else None
    )
    error = validate_published_stats_index(destination, high_water)
    if error is None:
        _remove_publication_marker(destination)
        return None
    settled = dict(state)
    settled.update({"status": "failed", "error": error})
    # Durable BEFORE this run touches the destination: a crash between here and
    # the marker this run is about to write must still leave the verdict.
    _cctally_db._atomic_write_private_json(
        _publication_marker_path(destination), settled
    )
    _fsync_dir(pathlib.Path(destination).parent)
    record.update({
        "status": "failed",
        "postPublicationValidation": {"ok": False, "error": error},
    })
    try:
        _write_rebuild_record(record_path, record)
    except OSError:
        pass
    # #496 exists partly because corruption gets MISATTRIBUTED, so the wording
    # must not accuse an earlier publication of failing a check that never ran.
    # A destination that cannot be read fails this validation for a reason that
    # is not evidence about the publication: the stamp read is INDETERMINATE
    # and the high-water check then trips on the unrelated damage.
    unreadable = _cctally_db._is_sqlite_corruption_error(error) or error.startswith(
        ("DatabaseError:", "OperationalError:", "InterfaceError:", "sqlite3.")
    )
    if unreadable:
        print(
            "[stats] an earlier stats.db publication was interrupted before it "
            "could validate what it published, and that check could not be "
            f"completed because the index could not be read: {error}. The read "
            "failure is not evidence about that publication. Rebuild record: "
            f"{record_path}.",
            file=sys.stderr,
        )
    else:
        print(
            "[stats] an earlier stats.db publication was interrupted before it "
            f"could validate what it published, and it FAILED that check: "
            f"{error}. Rebuild record: {record_path}.",
            file=sys.stderr,
        )
    return settled


def _remove_publication_marker(destination) -> None:
    try:
        _publication_marker_path(destination).unlink()
    except FileNotFoundError:
        pass
    _fsync_dir(pathlib.Path(destination).parent)


def _write_rebuild_record(path, payload: dict) -> None:
    import _cctally_db

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _cctally_db._atomic_write_private_json(path, payload)


def _forensics_shape_token(forensics_path) -> "str | None":
    if not forensics_path:
        return None
    try:
        bundle = json.loads(pathlib.Path(forensics_path).read_text())
    except (OSError, ValueError):
        return None
    damage = bundle.get("damage")
    return damage.get("shapeToken") if isinstance(damage, dict) else None


def _scan_stats_damage(path) -> dict:
    """Describe one stats family member by reading its bytes. Never raises."""
    try:
        import _lib_stats_damage

        return _lib_stats_damage.describe_damage(integrity_rows=None, path=path)
    except Exception as exc:  # noqa: BLE001 — enrichment never breaks a rebuild
        return {
            "schemaVersion": 1,
            "method": "unavailable",
            "findings": [],
            "shapeToken": "none",
            "reason": f"{type(exc).__name__}: {exc}"[:200],
        }


def _record_post_checkpoint_damage(
    incident: pathlib.Path, destination: pathlib.Path, outcome: str,
) -> "dict | None":
    """Add the post-checkpoint scan to an already-written incident manifest.

    A second `_atomic_write_private_json` to the same path is safe: the write
    is atomic and nothing references the incident yet. It has to be a second
    write because preservation runs BEFORE the explicit checkpoint, so the
    outcome this records does not exist when the manifest is first written.
    """
    import _cctally_db

    try:
        manifest_path = incident / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        damage = manifest.get("damage") or {}
        damage["postCheckpoint"] = _scan_stats_damage(destination)
        damage["checkpointOutcome"] = outcome
        manifest["damage"] = damage
        _cctally_db._atomic_write_private_json(manifest_path, manifest)
        return damage
    except Exception as exc:  # noqa: BLE001 — enrichment never breaks a rebuild
        print(
            f"[rebuild] post-checkpoint damage scan failed: {exc}",
            file=sys.stderr,
        )
        return None


def _binary_version() -> "str | None":
    """The running binary's released version, or None when it cannot be read."""
    try:
        import _lib_changelog

        value = _lib_changelog._read_latest_changelog_version()
    except Exception:  # pragma: no cover — a missing CHANGELOG is not fatal
        return None
    return value[0] if value else None


def _preserve_stats_family_for_cutover(
    path: pathlib.Path, *, context: RebuildContext,
) -> pathlib.Path:
    """Durably copy the old family into quarantine without removing the main."""
    import _cctally_db

    root = _cctally_core.APP_DIR / "quarantine"
    root.mkdir(parents=True, exist_ok=True)
    # The quarantine entry itself must survive power loss before any old
    # sidecar can be removed. fsyncing only the new root/incident cannot make
    # the root's directory entry durable in APP_DIR.
    _fsync_dir(_cctally_core.APP_DIR)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    incident = root / f"{path.name}-{stamp}"
    incident.mkdir(mode=0o700)
    destination = incident / path.name
    members = [
        pathlib.Path(str(path) + suffix).name
        for suffix in ("", "-wal", "-shm")
        if pathlib.Path(str(path) + suffix).exists()
    ]
    if not members:
        raise OSError(f"no database family exists to preserve at {path}")
    # Observed sizes are read BEFORE the copy, so the empty-WAL case in the
    # routine corruption heal is evidenced rather than assumed (#496 S1 F2).
    family_sizes = {}
    for name in members:
        try:
            family_sizes[name] = path.with_name(name).stat().st_size
        except OSError:
            family_sizes[name] = None
    # Read from the raw header rather than by opening the file: the file this
    # is asked about is typically one SQLite refuses to open, which is exactly
    # when the epoch it carried is worth recording (#496 S1).
    preserved_user_version = _cctally_db._read_user_version_header(path)
    _cctally_db._copy_db_family(path, destination)
    manifest = {
        "schemaVersion": 2,
        "quarantinedAtUtc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "originalPath": str(path),
        "movedFiles": members,
        "complete": True,
        "cutoverProtocol": "preserve-then-atomic-replace-v1",
        # #496 S1 additive fields. Every key above keeps its v1 name and
        # meaning, so a v1 reader is unaffected by the bump.
        "trigger": context.trigger,
        "triggerError": context.trigger_error,
        "forensicsPath": context.forensics_path,
        "rebuildRecordPath": context.record_path,
        "binaryVersion": _binary_version(),
        "binaryEpoch": _cctally_core.STATS_INDEX_EPOCH,
        "preservedUserVersion": preserved_user_version,
        "familySizes": family_sizes,
        # The retained COPY is described, not the live file, because the copy
        # is the artifact that actually survives. `postCheckpoint` and
        # `checkpointOutcome` are filled in by the caller once the explicit
        # checkpoint has run (#496 S1 F8 section 6.3).
        "damage": {
            "preserved": _scan_stats_damage(destination),
            "postCheckpoint": None,
            "checkpointOutcome": None,
        },
    }
    _cctally_db._atomic_write_private_json(incident / "manifest.json", manifest)
    _fsync_dir(incident)
    _fsync_dir(root)
    return incident


#: Set on the exception a failed in-place publication raises, so the caller can
#: read the phase it had reached. "The transaction raised" is not a safe
#: discriminator: a failure before the commit can roll back while a failure
#: after it cannot, and a commit-time I/O error leaves the outcome unknown.
_PUBLICATION_PHASE_ATTR = "_cctally_publication_phase"


def publication_phase_of(exc) -> "str | None":
    """The publication phase ``exc`` was raised in, or None when unrecorded."""
    return getattr(exc, _PUBLICATION_PHASE_ATTR, None)


def _open_publication_connection(destination) -> sqlite3.Connection:
    """Open the LIVE destination for an in-place publish.

    `stats_open_guarded` skips its own flock when the caller already holds
    stats maintenance, which every production trigger does. Interrupted-rebuild
    recovery is suppressed because this run's own `.rebuilding-*` scratch is on
    disk right now and is not an interruption to recover from.

    `stats_open_guarded` does NOT apply connection policy — `open_db` does that
    separately — so the busy timeout, journal mode and WAL size limit are
    applied here rather than assumed.

    The connection is opened with `uri=True` because the publisher ATTACHes the
    scratch through a `file:...?mode=ro` URI. SQLite honours a URI filename in
    `ATTACH` only when the main connection carries `SQLITE_OPEN_URI`, or when
    the library happens to be built with `SQLITE_USE_URI`. Relying on the
    latter would make the read-only attach ambient rather than guaranteed.
    """
    import _cctally_store

    conn = _cctally_store.stats_open_guarded(
        pathlib.Path(destination),
        connect=lambda path: sqlite3.connect(
            pathlib.Path(path).resolve().as_uri(), uri=True
        ),
        recover_interruptions=False,
    )
    try:
        _cctally_store.apply_policy(conn, "stats")
    except BaseException:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def _carry_sqlite_sequence(conn: sqlite3.Connection) -> None:
    """Install the scratch's AUTOINCREMENT watermarks, not the copy's.

    A table-by-table row copy sets each counter to `max(rowid)`, whereas
    `os.replace` publishes the scratch's `sqlite_sequence` verbatim. Measured on
    SQLite 3.53.4: a scratch with `max(id)=6` and a counter of 10 — the shape
    produced whenever the fold inserts rows and later deletes them, as the
    `five_hour_block_close` fold's exact-child DELETE/INSERT does — published a
    counter of 6, and the next insert took id 7, an id a deleted row had
    already used.

    Delete-then-insert rather than update, because `DROP TABLE` removes the
    table's `sqlite_sequence` row and `CREATE TABLE` does not put one back, so
    an UPDATE has nothing of its own to act on. Measured on SQLite 3.53.4, a
    zero-row `INSERT ... SELECT` does create the row with seq 0 — so on that
    version an UPDATE would in fact land — but that is an undocumented
    implementation detail rather than a contract, and silently losing an
    AUTOINCREMENT watermark hands out an id a deleted row already used.
    """
    present = conn.execute(
        "SELECT (SELECT 1 FROM src.sqlite_schema WHERE type = 'table' "
        "AND name = 'sqlite_sequence'), "
        "(SELECT 1 FROM main.sqlite_schema WHERE type = 'table' "
        "AND name = 'sqlite_sequence')"
    ).fetchone()
    if present is None or present[0] is None or present[1] is None:
        return
    rows = conn.execute("SELECT name, seq FROM src.sqlite_sequence").fetchall()
    for name, seq in rows:
        conn.execute("DELETE FROM main.sqlite_sequence WHERE name = ?", (name,))
        conn.execute(
            "INSERT INTO main.sqlite_sequence (name, seq) VALUES (?, ?)",
            (name, seq),
        )


def _publish_generation_in_place(
    conn: sqlite3.Connection, scratch, *, record_path, started_at: str
) -> str:
    """Install the validated scratch's generation into the LIVE database.

    Returns the terminating phase. The whole swap is ONE `BEGIN IMMEDIATE`, so
    a reader inside a transaction keeps the generation it opened on, an
    abandoned attempt leaves the prior generation live and sound, and
    `PRAGMA user_version` flips atomically at the commit.
    """
    import _lib_stats_publish as sp

    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        # `apply_policy` never enables foreign keys and the schema documents
        # them as enforcement-off, so the derived-FK seam for
        # `five_hour_milestones.block_id` is a fold ordering contract rather
        # than an enforced constraint. Assert that rather than depend on it
        # silently, so a future change that turns them on fails loudly here.
        raise JournalError(
            "stats publication requires foreign_keys=0; the schema's derived-FK "
            "seam is a fold ordering contract, not an enforced constraint"
        )
    resolved = pathlib.Path(scratch).resolve()
    conn.execute("ATTACH DATABASE ? AS src", (resolved.as_uri() + "?mode=ro",))
    attached = conn.execute(
        "SELECT file FROM pragma_database_list WHERE name = 'src'"
    ).fetchone()
    if attached is None or pathlib.Path(str(attached[0])) != resolved:
        # A connection without SQLITE_OPEN_URI treats the URI as a literal
        # filename and silently attaches a new, EMPTY database under that name.
        # Publishing from it would install an empty generation, so the identity
        # of what was attached is checked rather than assumed.
        try:
            conn.execute("DETACH DATABASE src")
        except Exception:
            pass
        raise JournalError(
            "stats publication attached the wrong file as its scratch: "
            f"expected {resolved}, got {attached[0] if attached else '<none>'}"
        )
    phase = sp.PRE_COMMIT
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Both schemas are read INSIDE the transaction, so the drop list
            # describes the generation actually being retired.
            src_objects = conn.execute(
                "SELECT type, name, sql FROM src.sqlite_schema"
            ).fetchall()
            dest_objects = conn.execute(
                "SELECT type, name, sql FROM main.sqlite_schema"
            ).fetchall()
            plan = sp.plan_generation_swap(dest_objects, src_objects)
            if plan.rejected:
                raise JournalError(
                    "stats publication cannot copy unsupported object(s): "
                    + ", ".join(plan.rejected)
                )
            for statement in plan.drop_statements:
                conn.execute(statement)
            for statement in plan.create_table_statements:
                conn.execute(statement)
            for name in plan.copy_tables:
                conn.execute(
                    f'INSERT INTO main."{name}" SELECT * FROM src."{name}"'
                )
            for statement in plan.create_index_statements:
                conn.execute(statement)
            _carry_sqlite_sequence(conn)
            epoch = int(conn.execute("PRAGMA src.user_version").fetchone()[0])
            if epoch != _cctally_core.STATS_INDEX_EPOCH:
                # `read_publication_stamp`'s entire short-circuit rests on the
                # claim that a committed publication always leaves the
                # destination at THIS binary's epoch. Upstream validation
                # already guarantees the scratch carries it; asserting it here
                # costs nothing and turns the argument into an invariant.
                raise JournalError(
                    "stats publication refuses to stamp a scratch at index "
                    f"epoch {epoch}; this binary builds "
                    f"{_cctally_core.STATS_INDEX_EPOCH}"
                )
            conn.execute(f"PRAGMA main.user_version={epoch:d}")
            # The publication's own identity, committed atomically with the
            # content and the epoch it describes (#496 S3 §5).
            conn.execute("DELETE FROM main.stats_publication_stamp")
            conn.execute(
                "INSERT INTO main.stats_publication_stamp "
                "(record_path, started_at_utc, stamped_at_utc) VALUES (?, ?, ?)",
                (str(record_path), started_at, _utc_iso_now()),
            )
            phase = sp.COMMIT_UNKNOWN
            conn.commit()
            phase = sp.COMMITTED
            _stats_rebuild_test_pause("publication_after_commit_before_detach")
        except BaseException as exc:
            if phase == sp.PRE_COMMIT:
                try:
                    conn.rollback()
                except Exception:
                    pass
            try:
                setattr(exc, _PUBLICATION_PHASE_ATTR, phase)
            except Exception:  # pragma: no cover — some exceptions are frozen
                pass
            raise
    finally:
        # DETACH cannot run inside a transaction, so this is best-effort: a
        # failure that left one open is already being raised.
        try:
            conn.execute("DETACH DATABASE src")
        except Exception:
            pass
    return phase


def _checkpoint_after_publication(conn: sqlite3.Connection) -> str:
    """Drain the WAL after a committed in-place publish — BEST EFFORT.

    `wal_checkpoint(TRUNCATE)` returns a busy ROW rather than raising, and this
    repository has measured it taking about 16 seconds against a 15-second
    `busy_timeout` under a pinned reader. Its result is recorded and never
    interpreted as a transaction failure, and it is never a reason to fall back
    after a commit.
    """
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        return f"error:{type(exc).__name__}"
    if row is None:
        return "unknown"
    return "checkpointed" if int(row[0]) == 0 else "busy"


def _remove_empty_db_sidecars(path) -> None:
    """Remove the sidecars a read-only validation open leaves behind.

    Unlike the physical path, an in-place publish leaves a LEGITIMATE WAL: it
    belongs to the live database, and deleting a non-empty one would discard
    committed frames. Only a zero-length WAL is removed — the state a
    successful TRUNCATE checkpoint and a clean last close leave — so the
    documented sidecar-free end state still holds without ever risking data.
    """
    path = pathlib.Path(path)
    wal = pathlib.Path(str(path) + "-wal")
    try:
        if wal.stat().st_size:
            return
    except OSError:
        pass
    for suffix in ("-wal", "-shm"):
        try:
            pathlib.Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return
    _fsync_dir(path.parent)


#: Probe 2 measured the live WAL peaking at 1.01x the main file across five
#: successive in-place publishes, so the projection is that plus a margin.
_PUBLICATION_WAL_PROJECTION = 1.05
#: Headroom for the rollback journal and freelist churn of an abandoned attempt.
_PUBLICATION_ROLLBACK_MARGIN = 0.25


def _free_disk_bytes(directory) -> int:
    """Free bytes on the filesystem holding ``directory`` (mockable in tests)."""
    import _cctally_db

    return _cctally_db._free_disk_bytes(directory)


def _db_family_bytes(path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += pathlib.Path(str(path) + suffix).stat().st_size
        except OSError:
            pass
    return total


def _publication_required_free_bytes(scratch, destination) -> int:
    """Conservative free-space floor for publishing ``scratch``.

    The live attempt adds a database-sized WAL while the scratch still exists,
    and a physical fallback would then add a full quarantine copy of the old
    family on top of that. All three are counted, because the run cannot know
    at this point which mechanism it will end up using.
    """
    scratch_bytes = _db_family_bytes(scratch)
    projected = _PUBLICATION_WAL_PROJECTION + _PUBLICATION_ROLLBACK_MARGIN
    return int(scratch_bytes * projected) + _db_family_bytes(destination)


def _preflight_publication_space(scratch, destination) -> None:
    """Abort before any live mutation when the disk cannot hold the publish.

    Aborting is deliberately NOT a reason to fall back to replacement: a full
    disk leaves a perfectly good generation live, and physically replacing it
    is exactly the outcome §12 refuses.
    """
    parent = pathlib.Path(destination).parent
    needed = _publication_required_free_bytes(scratch, destination)
    try:
        free = _free_disk_bytes(parent)
    except OSError as exc:  # pragma: no cover — statvfs failing is exotic
        raise JournalError(
            f"could not determine free space on {parent} before publishing "
            f"the rebuilt stats index: {exc}"
        ) from exc
    if free < needed:
        raise JournalError(
            "publishing the rebuilt stats index needs about "
            f"{needed / (1024 * 1024):.1f} MB free on {parent}, but only "
            f"{free / (1024 * 1024):.1f} MB is available. The existing index "
            "is untouched; free space and retry."
        )


#: Returned by the in-place publisher when the destination cannot be operated
#: on structurally and physical replacement is the sanctioned fallback.
_FALL_BACK = object()


def _publish_stats_index_in_place(
    *, scratch, destination, context, high_water, record, fire_before_swap,
    prior,
):
    """Publish transactionally into the live file (#496 S3 §4).

    Returns `None` on success (an in-place publish never preserves, so there is
    no incident directory), or `_FALL_BACK` when physical replacement is the
    sanctioned response.
    """
    import _lib_stats_publish as sp

    try:
        conn = _open_publication_connection(destination)
    except BaseException as exc:
        if sp.may_fall_back_to_replacement(exc):
            print(
                "[rebuild] the live stats index cannot be opened "
                f"({exc}); publishing by replacement instead",
                file=sys.stderr,
            )
            return _FALL_BACK
        raise

    started_at = _utc_iso_now()
    record_path = pathlib.Path(context.record_path)
    live = dict(record)
    live.update({
        "status": "pending",
        "startedAtUtc": started_at,
        "completedAtUtc": None,
        # An in-place publish never preserves. Preservation is a consequence of
        # destroying a file; `db backup --db stats` is the supported snapshot.
        "incidentPath": None,
        "damageShapeTokens": None,
        "postPublicationValidation": None,
        "publicationMechanism": "in_place",
    })
    try:
        fire_before_swap()
        # Phase 1 of the publication transaction: the record and then the
        # marker, each fsynced, BEFORE any live byte changes.
        _write_rebuild_record(record_path, live)
        _stats_rebuild_test_pause("publication_before_marker")
        _write_publication_marker(
            destination, record_path, started_at=started_at,
            scratch_path=scratch, prior=prior, mechanism="in_place",
        )
        _stats_rebuild_test_pause("rebuild_before_cutover")
        _publish_generation_in_place(
            conn, scratch, record_path=record_path, started_at=started_at,
        )
    except BaseException as exc:
        # Rollback, detach and CLOSE are all mandatory before any fallback: the
        # drain gate is a whole-system handle scan, and this connection would
        # either fail it or hollow out the invariant it exists to enforce.
        try:
            conn.close()
        except Exception:
            pass
        phase = publication_phase_of(exc)
        if phase == sp.PRE_COMMIT and sp.may_fall_back_to_replacement(exc):
            print(
                "[rebuild] the in-place stats publication rolled back "
                f"({exc}); publishing by replacement instead",
                file=sys.stderr,
            )
            record["inPlaceAttempt"] = {
                "phase": phase, "error": f"{type(exc).__name__}: {exc}"[:500],
            }
            return _FALL_BACK
        raise

    checkpoint_outcome = _checkpoint_after_publication(conn)
    try:
        conn.close()
    except Exception:
        pass
    _stats_rebuild_test_pause("rebuild_after_publication_replace")

    # Phase 2: validate the bytes that are now live, on a connection that never
    # saw them being written. The expected publication identity goes with it:
    # the high-water alone cannot distinguish this generation from an equally
    # journal-consistent one some other run installed.
    post_error = validate_published_stats_index(
        destination, high_water, expected_record_path=str(record_path),
    )
    _remove_empty_db_sidecars(destination)

    live["publicationCheckpoint"] = checkpoint_outcome
    live["postPublicationValidation"] = {
        "ok": post_error is None, "error": post_error,
    }
    live["completedAtUtc"] = _utc_iso_now()
    live["status"] = "ok" if post_error is None else "failed"
    _write_rebuild_record(record_path, live)
    if post_error is not None:
        # The scratch is deliberately NOT removed here: it is the last
        # independently validated copy of this generation, and the live bytes
        # just failed.
        _write_publication_marker(
            destination, record_path, started_at=started_at,
            scratch_path=scratch, status="failed", error=post_error,
            mechanism="in_place",
        )
        raise JournalError(
            "published stats index failed post-publication validation: "
            f"{post_error}; rebuild record: {record_path}"
        )
    _stats_rebuild_test_pause("publication_after_verdict_before_marker_removal")
    _remove_publication_marker(destination)
    # Removal follows verdict settlement. A surviving `.rebuilding-*` family is
    # classified FIRST by the next opener and would route this healthy index
    # through interrupted-rebuild recovery.
    _stats_rebuild_test_pause("publication_before_scratch_removal")
    _remove_db_family(scratch)
    _fsync_dir(pathlib.Path(destination).parent)
    return None


def _publish_rebuilt_stats_index(
    *,
    scratch: pathlib.Path,
    destination: pathlib.Path,
    preserve_existing: bool,
    context: RebuildContext,
    high_water: "tuple[str, int] | None",
    record: dict,
    before_swap=None,
) -> "pathlib.Path | None":
    """Publish one validated, closed, sidecar-free scratch index.

    In-place transactional publication is the mechanism (#496 S3). Physical
    replacement is the fallback, taken when the destination cannot be operated
    on structurally. The mechanism is chosen against the destination in front
    of this run, not by the trigger that reached it: corruption is not uniform,
    and a readable-but-damaged destination publishes in place like any other.

    Publication is a two-phase durable transaction (#496 S1 F1) under either
    mechanism. A published file carries the current epoch, so `open_db`'s
    zero-DDL fast path returns it with no validation and a post-publication
    failure that only RAISED would leave a known-bad index accepted by every
    later command. The record and the marker are what make the verdict outlive
    this process.
    """
    import _cctally_store

    # Read-only, and first: a short disk must abort while the destination is
    # still untouched, not part-way through a publication.
    _preflight_publication_space(scratch, destination)

    # A marker already beside the destination may still owe a verdict on bytes
    # that are live right now. Settle it BEFORE Phase 1 overwrites the only
    # marker slot, while the destination is still exactly what that publication
    # left there.
    prior = _settle_prior_publication_verdict(destination)

    # The `before_swap` seam fires ONCE, for either mechanism: it is the
    # `db rederive` crash seam, and a fallback must not re-enter it.
    fired = []

    def fire_before_swap() -> None:
        if before_swap is not None and not fired:
            fired.append(True)
            before_swap()

    if pathlib.Path(destination).exists():
        published = _publish_stats_index_in_place(
            scratch=scratch, destination=destination, context=context,
            high_water=high_water, record=record,
            fire_before_swap=fire_before_swap, prior=prior,
        )
        if published is not _FALL_BACK:
            return published

    family_exists = any(
        pathlib.Path(str(destination) + suffix).exists()
        for suffix in ("", "-wal", "-shm")
    )
    incident = None
    damage_tokens = None
    if family_exists:
        blocked = _cctally_store._stats_family_drained(destination)
        if blocked is not None:
            raise JournalError(f"stats.db cutover declined: {blocked}")
        _cctally_store._stats_storm_test_pause("stats_replace_drained")
        if preserve_existing:
            # Preserve the exact pre-cutover family, including a committed WAL
            # and SHM, before checkpointing mutates or removes those sidecars.
            incident = _preserve_stats_family_for_cutover(
                destination, context=context
            )
        checkpoint_outcome = "skipped_absent"
        if destination.exists():
            try:
                checkpoint_outcome = _prepare_existing_stats_for_cutover(
                    destination
                )
            except BaseException:
                if incident is not None:
                    _record_post_checkpoint_damage(
                        incident, destination, "failed"
                    )
                raise
        if incident is not None:
            # Scanned BEFORE the sidecars are removed, so this and the
            # preserved scan bracket the explicit checkpoint.
            damage = _record_post_checkpoint_damage(
                incident, destination, checkpoint_outcome
            )
            if damage:
                damage_tokens = {
                    "forensics": _forensics_shape_token(context.forensics_path),
                    "preserved": (damage.get("preserved") or {}).get(
                        "shapeToken"
                    ),
                    "postCheckpoint": (damage.get("postCheckpoint") or {}).get(
                        "shapeToken"
                    ),
                    "checkpointOutcome": damage.get("checkpointOutcome"),
                }
        # The old main stays present and, when it was readable, fully
        # checkpointed. A kill from here until os.replace therefore still
        # leaves a usable old destination while preventing stale sidecars from
        # being paired with the replacement main.
        _remove_db_sidecars_strict(destination)
        _cctally_store._stats_storm_test_pause("stats_replace_sidecars_removed")

    fire_before_swap()

    # Phase 1 of the publication transaction: the record and then the marker,
    # each fsynced, BEFORE the replacement becomes visible.
    started_at = _utc_iso_now()
    record = dict(record)
    record.update({
        "status": "pending",
        "startedAtUtc": started_at,
        "completedAtUtc": None,
        "incidentPath": str(incident) if incident is not None else None,
        "damageShapeTokens": damage_tokens,
        "postPublicationValidation": None,
        "publicationMechanism": "replace",
    })
    record_path = pathlib.Path(context.record_path)
    _write_rebuild_record(record_path, record)
    _write_publication_marker(
        destination, record_path, started_at=started_at, scratch_path=scratch,
        prior=prior, mechanism="replace",
    )

    _stats_rebuild_test_pause("rebuild_before_cutover")
    os.replace(str(scratch), str(destination))
    _fsync_dir(destination.parent)
    _stats_rebuild_test_pause("rebuild_after_publication_replace")

    # Phase 2: validate the bytes that are now live, on a connection that never
    # saw them being written.
    post_error = validate_published_stats_index(destination, high_water)
    # The read-only open above creates a zero-byte WAL and a 32 KiB SHM.
    # Remove them so the documented no-post-publication-stale-sidecar end state
    # still holds; an empty WAL is consistent with the freshly published main,
    # so a crash between validation and removal is harmless.
    _remove_db_sidecars_strict(destination)

    record["postPublicationValidation"] = {
        "ok": post_error is None,
        "error": post_error,
    }
    record["completedAtUtc"] = _utc_iso_now()
    record["status"] = "ok" if post_error is None else "failed"
    _write_rebuild_record(record_path, record)
    if post_error is not None:
        # No rollback is possible: the old family is already quarantined, and
        # restoring it would republish a file known to be corrupt. Any carried
        # prior verdict is dropped here on purpose: `os.replace` succeeded, so
        # the bytes it judged are gone and THIS failure is the live one.
        _write_publication_marker(
            destination, record_path, started_at=started_at,
            scratch_path=scratch, status="failed", error=post_error,
            mechanism="replace",
        )
        raise JournalError(
            "published stats index failed post-publication validation: "
            f"{post_error}; rebuild record: {record_path}"
        )
    _remove_publication_marker(destination)
    return incident


def _rebuild_quota_cache_leg(records) -> None:
    """Re-materialize cache.db `quota_window_snapshots` AND the #416 Codex
    attribution map from the journal (spec §5.4 + #416 spec §3.4).

    The journal records are the DURABLE source (§1 latent data-loss hole — the
    rollout JSONL evaporates); this INSERT-OR-IGNOREs the quota obs on their
    natural key and the attribution decisions on theirs, mirroring
    `_cache_applier` family for family. The map half is not optional: without it
    a rebuild would leave the map empty and the following rollout walk would
    have nothing to replay, sending every file back to the live `auth.json` —
    the exact defect this mechanism exists to prevent.

    Runs BEFORE any stats transaction, under the global cache writer lock
    followed by the `cache.db.codex.lock` provider flock (lock-order law).
    Best-effort: a missing/busy cache.db is a clean skip (the records stay
    durable in the journal; the stats quota projection pass then degrades
    cleanly)."""
    quota_obs = [r for r in records if _is_codex_quota_obs(r)]
    file_accounts = [r for r in records if _is_codex_file_account_op(r)]
    if not quota_obs and not file_accounts:
        return
    cache_path = _cctally_core.CACHE_DB_PATH
    if not cache_path.exists():
        return
    from _lib_cache_writer_lock import (
        acquire_cache_writer_flocks,
        release_cache_writer_flocks,
    )

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        held = acquire_cache_writer_flocks(
            _cctally_core.CACHE_LOCK_PATH,
            _cctally_core.CACHE_LOCK_CODEX_PATH,
            timeout=15.0,
        )
    except OSError as exc:
        print(f"[rebuild] quota cache leg lock failed: {exc}", file=sys.stderr)
        return
    if held is None:
        print("[rebuild] quota cache leg locks busy; skipping", file=sys.stderr)
        return
    try:
        try:
            cache = sqlite3.connect(str(cache_path), timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover — cache.db unopenable
            print(f"[rebuild] quota cache leg connect failed: {exc}", file=sys.stderr)
            return
        try:
            cache.execute("PRAGMA busy_timeout=15000")
            cache.execute("BEGIN IMMEDIATE")
            # Decisions FIRST — same §3.5 precedence ordering as `_cache_applier`.
            _, _file_conflicts = _apply_file_account_records(cache, file_accounts)
            _apply_quota_records(cache, quota_obs)
            cache.commit()
            _report_file_account_conflicts(_file_conflicts)
        except sqlite3.Error as exc:
            try:
                cache.rollback()
            except sqlite3.Error:
                pass
            print(f"[rebuild] quota cache leg write failed: {exc}", file=sys.stderr)
        finally:
            cache.close()
    finally:
        release_cache_writer_flocks(held)


def rebuild_stats_index(
    *,
    context: RebuildContext,
    target_path=None,
    high_water: "tuple[str, int] | None" = None,
    update_quota_cache: bool = True,
    before_swap=None,
) -> RebuildResult:
    """Build a FRESH stats index from the journal alone (spec §5.4).

    Replays every segment in canonical `(segment, offset)` order into a fresh
    schema'd DB at a scratch sibling of the destination, then ATOMICALLY swaps it
    in — crash-safe (a mid-fold crash leaves only a discardable scratch). Folds
    are apply-only: the PIPELINE never runs, so no Model-A emission, no harvest,
    no alerts, no `reconcile_config` (see the module note above). Post-rebuild the
    cursor equals the journal high-water.

    `context` states WHY this rebuild ran (#496 S1 F3). It is keyword-only with
    no default, so a future call site cannot silently produce an unattributed
    quarantine incident; omitting it raises `TypeError` at the call.

    `target_path` selects the destination (default `DB_PATH`). `high_water`
    optionally pins the exact inclusive journal prefix; later bytes stay beyond
    the rebuilt cursor. `update_quota_cache=False` is the Task-C Claude-only
    path whose caller already holds a stable cache exclusion. The common
    cutover keeps a live destination in place until its validated replacement
    is ready, preserves the old family, detaches old sidecars, and atomically
    replaces the main file. A `target_path` build uses the same atomic
    publication but does not create a live-family quarantine incident.
    """
    start = time.monotonic()
    context = context.validate()
    # Resolve the rebuild record's path ONCE, here, because preservation runs
    # long before the record is written and both must name the same file
    # (#496 S1). Callers never supply it.
    record_stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    context = _dc_replace(
        context,
        record_path=str(
            _cctally_core.LOG_DIR / f"stats-rebuild-{record_stamp}.json"
        ),
    )
    dest = (pathlib.Path(target_path) if target_path is not None
            else pathlib.Path(_cctally_core.DB_PATH))

    # HW snapshot at the START — lines appended during the rebuild are past HW
    # and belong to the next ingest cycle (they replay idempotently); mirrors the
    # live cycle's §5.2.1 HW-prefix rule.
    hw = high_water if high_water is not None else journal_high_water()
    segments = list_segments()
    if hw is not None:
        if hw[0] not in segments:
            raise JournalError(
                f"rebuild high-water segment is missing: {hw[0]}"
            )
        current_size = os.path.getsize(_cctally_core.JOURNAL_DIR / hw[0])
        if hw[1] < 0 or hw[1] > current_size:
            raise JournalError(
                f"rebuild high-water offset is invalid for {hw[0]}: {hw[1]}"
            )
        segments = segments[:segments.index(hw[0]) + 1]

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    scratch = dest.with_name(dest.name + f".rebuilding-{stamp}")
    _remove_db_family(scratch)

    # Build a fresh schema'd empty index at the scratch path. `_target_path`
    # DISARMS open_db's auto-heal (no recursion) and yields the current schema
    # (migrations stamped, gated backfills no-op on empty, fixups marked).
    conn = _cctally_core.open_db(_target_path=str(scratch))
    malformed = 0
    lines_folded = 0
    try:
        decoded: list = []
        protocol_evidence = []
        prior_high_water = None
        if hw is not None:
            for segment, offset, raw in _read_range(None, hw):
                rec = _lib_journal.decode_line(raw)
                if rec is None:
                    malformed += 1
                    prior_high_water = (
                        segment,
                        offset + len(raw) + 1,
                    )
                    continue
                _capture_protocol_prefix_evidence(
                    rec,
                    prior_high_water,
                    protocol_evidence,
                )
                decoded.append(rec)
                prior_high_water = (
                    segment,
                    offset + len(raw) + 1,
                )

        # Legacy account normalisation (#341, spec §2 / handoff item 2): a
        # pre-#341 real-account line lacks an account stamp — inject the cutover
        # mapping BEFORE the fold (Claude legacy -> the cutover op's account;
        # Codex legacy -> unattributed). `*`-families + already-stamped lines are
        # untouched. Resolved once from the journal's own cutover op (falls back
        # to `unattributed` when none is present), so a fresh single-account
        # rebuild is byte-neutral (everything is already `unattributed`).
        cutover_claude = resolve_cutover_claude_account()
        for rec in decoded:
            _normalize_legacy_account_stamp(rec, cutover_claude)

        # Resolve corrections BEFORE either disposable index is mutated. A
        # malformed revision, divergent same-revision candidate, or invalid
        # committed manifest leaves the existing destination untouched.
        effective = _lib_journal.resolve_effective_events(
            decoded,
            protocol_prefix_evidence=protocol_evidence,
        )

        # Cache leg BEFORE any stats txn (provider-flock lock-order): journal
        # Codex quota obs -> cache.db quota_window_snapshots.
        if update_quota_cache:
            _rebuild_quota_cache_leg(decoded)

        # One ordered fold stream: op-folds (order 5) + evts, keyed by
        # (fold_order, canonical seq) so referenced families resolve before
        # referencing ones and crash-replay duplicates fold idempotently.
        stream: list = []
        for seq, rec in enumerate(decoded):
            t = rec.get("t")
            kind = (rec.get("payload") or {}).get("kind")
            if t == "op" and kind in FOLD_APPLIERS:
                stream.append((_OP_FOLD_ORDER, seq, "op", rec))
        for seq, rec in enumerate(effective.active):
            stream.append((_fold_order(rec), seq, "evt", rec))
        stream.sort(key=lambda x: (x[0], x[1]))
        structural = [s for s in stream if s[0] < _REBUILD_MILESTONE_ORDER]
        tail = [s for s in stream if s[0] >= _REBUILD_MILESTONE_ORDER]

        _stats_rebuild_test_pause("rebuild_fold_started")

        # Phase 1 (txn A) — structural folds: op floors, snapshot_accept, cost
        # snapshots, resets+suppression, block_close, arming, credit effects.
        conn.execute("BEGIN IMMEDIATE")
        try:
            _write_effective_metadata(conn, effective)
            for _order, _seq, kind, rec in structural:
                if kind == "op":
                    FOLD_APPLIERS[(rec.get("payload") or {}).get("kind")](conn, rec)
                else:
                    _apply_evt(conn, rec)
                lines_folded += 1
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

        # Phase 2a — OPEN 5h block projection (own txn; block-only). Closed blocks
        # came from block_close evts; this materializes the never-closed window(s)
        # so the five_hour_milestone block_id derived_fk resolves. Best-effort
        # (the open block is a projection, §5.3).
        try:
            cctally = sys.modules.get("cctally")
            bf = getattr(cctally, "_backfill_five_hour_blocks", None)
            if bf is not None:
                bf(conn, only_missing=True)
        except Exception as exc:  # pragma: no cover — projection is best-effort
            print(f"[rebuild] open 5h block re-materialization failed: {exc}",
                  file=sys.stderr)

        # Phase 2b + 3 (txn B) — quota projection re-materialization (after the
        # order-45 arming folds) + milestone/budget folds + cursor advance.
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                import _cctally_quota as _q
                _q.rematerialize_quota_projection_for_rebuild(conn)
            except Exception as exc:  # pragma: no cover — projection best-effort
                print(f"[rebuild] quota projection re-materialization failed: {exc}",
                      file=sys.stderr)
            for _order, _seq, _kind, rec in tail:
                _apply_evt(conn, rec)
                lines_folded += 1
            # Fold-time `last_seen_utc` derivation (#341): re-derive each
            # account's last-seen from the whole journal (the observe ops folded
            # in the structural phase already created the rows).
            _derive_account_last_seen(conn, decoded)
            if hw is not None:
                _write_cursor(conn, hw[0], hw[1])
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

        rows_by_table = {}
        for tbl in _REBUILD_COUNT_TABLES:
            try:
                rows_by_table[tbl] = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.Error:
                rows_by_table[tbl] = 0
        # Drain the WAL into the main file so the atomic rename carries all data.
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise JournalError("rebuilt stats index WAL could not be drained")
        _validate_rebuilt_stats_index(conn, hw)
        _stats_rebuild_test_pause("rebuild_scratch_complete")
    finally:
        conn.close()

    # Closed, drained, validated, and durable before the old family is touched.
    _remove_db_sidecars_strict(scratch)
    with scratch.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_dir(scratch.parent)

    # Extract the compact result data and RELEASE the replay structures before
    # publication begins (#496 S3 §4.2). The in-place attempt adds a
    # database-sized WAL while the scratch still exists, so the measured
    # multi-gigabyte replay peak must not still be resident on top of it.
    segments_read = len(segments)
    conflicts = effective.conflicts
    protocol_violations = effective.protocol_violations
    acknowledged = effective.acknowledged_protocol_violations
    decoded = effective = stream = structural = tail = None
    segments = protocol_evidence = None

    # First fresh-connection validation (#496 S1 F1). A failure here raises
    # BEFORE any preservation, so no incident is created and the old family
    # stays live — the existing contract is preserved exactly.
    pre_error = validate_published_stats_index(scratch, hw)
    if pre_error is not None:
        raise JournalError(
            f"rebuilt stats index failed pre-publication validation: {pre_error}"
        )
    # That read-only open recreated the scratch sidecars; publication requires
    # a sidecar-free scratch, and a leftover pair would also survive the
    # `os.replace` as a stray artifact.
    _remove_db_sidecars_strict(scratch)

    incident = _publish_rebuilt_stats_index(
        scratch=scratch,
        destination=dest,
        preserve_existing=target_path is None,
        before_swap=before_swap,
        context=context,
        high_water=hw,
        record={
            "schemaVersion": 1,
            "trigger": context.trigger,
            "triggerError": context.trigger_error,
            "forensicsPath": context.forensics_path,
            "binaryVersion": _binary_version(),
            "binaryEpoch": _cctally_core.STATS_INDEX_EPOCH,
            "highWater": [hw[0], hw[1]] if hw is not None else None,
            "destination": str(dest),
            "targetPath": str(target_path) if target_path is not None else None,
            "segmentsRead": segments_read,
            "linesFolded": lines_folded,
            "malformed": malformed,
            "rowsByTable": rows_by_table,
            "buildSeconds": round(time.monotonic() - start, 3),
            "prePublicationValidation": {"ok": True, "error": None},
        },
    )

    return RebuildResult(
        rows_by_table=rows_by_table, malformed=malformed,
        duration_s=time.monotonic() - start, segments_read=segments_read,
        lines_folded=lines_folded, conflicts=conflicts,
        protocol_violations=protocol_violations,
        acknowledged_protocol_violations=acknowledged,
        quarantine_dir=incident,
    )


# ==========================================================================
# Cutover — one-time in-place upgrade of a pre-journal install (spec §8, Task 9)
# ==========================================================================
#
# `run_cutover(conn)` exports every journal-covered row of a legacy stats.db
# (already at migration head 13, schema applied) into a NEW `bootstrap-<ts>.jsonl`
# segment, stamps `journal_id = b:<table>:<rowid>` back onto every exported row,
# advances the ingest cursor past the bootstrap, and stamps
# `user_version = STATS_INDEX_EPOCH` — the last three ALL inside ONE
# `BEGIN IMMEDIATE` transaction, so a crash before the commit rolls the DB back
# to the fully-functional legacy shape (PRAGMA user_version is transactional in
# WAL). `open_db` calls this once per legacy open (version-only trigger, §8).
#
# Re-classification (§5.3 / §8): weekly_usage_snapshots rows export as
# `snapshot_accept` evts (verbatim decisions — replay never re-derives clamps);
# weekly_credit_floors as `op` lines (the ONLY op family); every harvest-family
# row as its evt kind with logical-FK refs (`b:<ref_table>:<fk_rowid>`), so
# `rebuild_stats_index` over the bootstrap alone reproduces the exported DB.
# quota_window_snapshots (cache.db) export as `obs` lines (the §1 latent
# data-loss hole: the rollout JSONL evaporates, so the journal becomes their
# durable home). OPEN five_hour_blocks are re-materialized projections — they are
# NOT exported and keep NULL journal_id, so a later close is still harvested.
#
# Retry safety (§8 / P1.9): rename-then-stamp with STABLE bootstrap ids. The
# segment is built at a `.partial` name, fsync'd, then renamed into place before
# the stamping runs; a re-run after any crash re-exports byte-identical lines
# (ids are `b:<table>:<rowid>`, independent of the retry's timestamp), so a
# duplicate/leftover bootstrap folds idempotently (`INSERT OR IGNORE`). The
# cutover does NOT take the ingest lock. **The conclusion is right; the reason
# once written here was false and is corrected (#386).** It was: "open_db
# reaches it from INSIDE run_stats_ingest's own ingest-lock hold — re-acquiring
# would self-deadlock." It does not: `run_stats_ingest` takes maintenance
# EXCLUSIVE *before* `open_db()` on the legacy/fresh branch and only acquires
# `journal.ingest.lock` after `open_db` has returned, so cutover never runs
# under an ingest hold from that path.
#
# The real reason is that cutover runs under maintenance-EXCLUSIVE — since #386,
# unconditionally, via `_cctally_store.stats_open_time_guard()` around
# `open_db`'s whole open-time mutation region, whichever command reached it.
# Maintenance-exclusive already serializes it against ingest, so the ingest lock
# would add nothing; single-flight of the STAMP is provided by `BEGIN
# IMMEDIATE`, and concurrent cutovers converge by id.
#
# DO NOT "fix" this by making cutover take the ingest lock. `holds_ingest_lock()`
# now exists, and the deleted sentence reads like an invitation to add the
# acquire with a re-entrancy check. Taking maintenance then ingest here would be
# in lock order and would not deadlock — it would simply be a second, redundant
# lock on a path that already holds the stronger one.


def _cutover_iso(dt_utc: dt.datetime) -> str:
    return (dt_utc.astimezone(dt.timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _cutover_ref(ref_table: str, fk_value) -> str:
    """The logical FK ref for a legacy integer FK: `b:<ref_table>:<rowid>`, or
    the ``"0"`` no-FK sentinel (spec §4.2). Every exported row's journal_id is
    `b:<table>:<its rowid>`, so a FK pointing at rowid N is exactly
    `b:<ref_table>:N` — no lookup needed; an orphan FK folds to a dropped row on
    rebuild (INSERT OR IGNORE), same as today."""
    if fk_value in (0, None, "0", ""):
        return "0"
    return _lib_journal.bootstrap_id(ref_table, fk_value)


@dataclass(frozen=True)
class _CutoverSpec:
    """How to export one legacy stats table at cutover (spec §8 / §5.3)."""

    table: str
    kind: str
    line: str                       # "evt" | "op" | "obs"
    at_col: str                     # column supplying the line `at`
    fk_refs: dict = field(default_factory=dict)   # column -> (ref_table, ref_key)
    exclude: tuple = ()             # extra payload columns to drop (derived_fk)
    closed_only: bool = False       # five_hour_blocks: closed rows only
    children: tuple = ()            # (payload_key, child_table)
    # `stamp=False` for a §5.3 "state" family with NO journal_id column
    # (quota_alert_arming): its fold applier converges by NATURAL-KEY upsert, so
    # there is nothing to stamp back and it is excluded from the no-NULL-survivors
    # invariant (§8). When `natural_key_id` is set, the exported evt id is the
    # state-instance form (`<natural_key_prefix>:<col>:<col>…`) matching the LIVE
    # emission, instead of the `b:<table>:<rowid>` bootstrap id.
    stamp: bool = True
    natural_key_prefix: str = ""    # evt_id kind prefix (e.g. "qaa")
    natural_key_id: tuple = ()      # columns forming the natural-key evt id


# Order is cosmetic — the fold sorts by dependency (`_fold_order`); the file
# order does not affect correctness. Kept referenced-before-referencing for
# readability.
_CUTOVER_SPECS = (
    _CutoverSpec("weekly_credit_floors", "weekly_credit_floor", "op",
                 "applied_at_utc"),
    _CutoverSpec("weekly_usage_snapshots", "snapshot_accept", "evt",
                 "captured_at_utc"),
    _CutoverSpec("weekly_cost_snapshots", "weekly_cost_snapshot", "evt",
                 "captured_at_utc"),
    _CutoverSpec("week_reset_events", "week_reset", "evt", "detected_at_utc"),
    _CutoverSpec("five_hour_reset_events", "five_hour_credit", "evt",
                 "detected_at_utc"),
    _CutoverSpec("five_hour_blocks", "five_hour_block_close", "evt",
                 "last_updated_at_utc", closed_only=True,
                 children=_BLOCK_CHILDREN),
    _CutoverSpec(
        "percent_milestones", "percent_milestone", "evt", "captured_at_utc",
        fk_refs={
            "usage_snapshot_id": ("weekly_usage_snapshots", "usage_snapshot_ref"),
            "cost_snapshot_id": ("weekly_cost_snapshots", "cost_snapshot_ref"),
            "reset_event_id": ("week_reset_events", "reset_event_ref"),
        }),
    _CutoverSpec(
        "five_hour_milestones", "five_hour_milestone", "evt", "captured_at_utc",
        fk_refs={
            "usage_snapshot_id": ("weekly_usage_snapshots", "usage_snapshot_ref"),
            "reset_event_id": ("five_hour_reset_events", "reset_event_ref"),
        },
        exclude=("block_id",)),   # derived_fk: re-derived from five_hour_window_key
    _CutoverSpec("budget_milestones", "budget", "evt", "crossed_at_utc"),
    _CutoverSpec("projected_milestones", "projected", "evt", "crossed_at_utc"),
    _CutoverSpec("project_budget_milestones", "project_budget", "evt",
                 "crossed_at_utc"),
    # quota_alert_arming (§5.3 "state") — its activation boundary is a
    # forward-only alert clock (`activated_at_utc`) that must survive rebuild so
    # the reconcile honors it (no historical re-fires). No journal_id column →
    # NOT stamped; the fold applier upserts by natural key. The evt id is the
    # `qaa:` state-instance form (matching the live emission in
    # `_cctally_quota._codex_leg._emit_arming`): the natural row key is followed
    # by fingerprint + activation boundary so distinct state transitions never
    # collide at rev 0, while exact re-emission of one state converges.
    _CutoverSpec("quota_alert_arming", "quota_alert_arming", "evt",
                 "activated_at_utc", stamp=False, natural_key_prefix="qaa",
                 natural_key_id=("source", "source_root_key", "account_key",
                                 "logical_limit_key", "observed_slot",
                                 "window_minutes", "rule_fingerprint",
                                 "activated_at_utc")),
    _CutoverSpec("quota_threshold_events", "quota_threshold_event", "evt",
                 "created_at_utc", stamp=False, natural_key_prefix="qte",
                 natural_key_id=("source", "source_root_key", "account_key",
                                 "logical_limit_key", "observed_slot",
                                 "window_minutes", "resets_at_utc",
                                 "threshold")),
    # quota_threshold_events (#416 spec §7.2, review F13) — TERMINAL alert
    # evidence, modelled exactly on the quota_alert_arming precedent above: no
    # `journal_id` column -> NOT stamped, and the fold applier converges by
    # natural key. The `qte:` id mirrors the table's own UNIQUE key so one
    # crossing is one event forever, and it MUST match the live emitter in
    # `_cctally_quota._codex_leg._emit_terminal_event`.
    #
    # This spec covers a legacy install that has not yet cut over. An install
    # already past the cutover carries no history here — which is exactly why
    # the live emitter exists: without it, only pre-cutover rows would ever be
    # replayable, and every row written since would still be lost on a rebuild.
)

def _export_stats_table(conn, spec) -> list:
    """Return `[(line_record, rowid), ...]` for every row of `spec.table`
    (closed rows only when `spec.closed_only`). Bootstrap id = b:<table>:<rowid>;
    FK columns become logical refs; block children embed under `_models`/
    `_projects` (spec §8)."""
    where = " WHERE is_closed = 1" if spec.closed_only else ""
    rows = conn.execute(f"SELECT * FROM {spec.table}{where}").fetchall()
    out = []
    for row in rows:
        rowid = row["id"]
        payload = {}
        for key in row.keys():
            # account_key (#341) is NEVER exported — a legacy stats.db row carries
            # only the schema DEFAULT ('unattributed'/'*'), so exporting it would
            # make the bootstrap evt look already-stamped and defeat the rebuild's
            # legacy normalisation. Dropping it lets the fold re-derive the right
            # account (Claude legacy -> cutover op's account; Codex legacy ->
            # unattributed; `*`-families -> schema DEFAULT '*').
            if key in ("id", "journal_id", "account_key") \
                    or key in spec.fk_refs or key in spec.exclude:
                continue
            payload[key] = row[key]
        for col, (ref_table, ref_key) in spec.fk_refs.items():
            payload[ref_key] = _cutover_ref(ref_table, row[col])
        for payload_key, child_table in spec.children:
            child_rows = conn.execute(
                f"SELECT * FROM {child_table} WHERE block_id = ? ORDER BY id",
                (rowid,)).fetchall()
            payload[payload_key] = [
                {k: cr[k] for k in cr.keys() if k not in ("id", "block_id")}
                for cr in child_rows]
        if spec.kind in ("quota_alert_arming", "quota_threshold_event"):
            payload["journal_identity_version"] = 2
        if spec.natural_key_id:
            # §5.3 "state" family: the evt id is the state-instance form (matching
            # the live emission), NOT the b:<table>:<rowid> bootstrap id. A
            # legacy (pre-#341) stats.db has no `account_key` column, so that
            # missing component is the sentinel (#341).
            row_cols = set(row.keys())
            bid = _lib_journal.evt_id(
                spec.natural_key_prefix,
                *(row[c] if c in row_cols else _lib_accounts.UNATTRIBUTED
                  for c in spec.natural_key_id))
        else:
            bid = _lib_journal.bootstrap_id(spec.table, rowid)
        at = row[spec.at_col]
        if spec.line == "op":
            rec = _lib_journal.make_op(
                at=at, src="bootstrap", payload={**payload, "kind": spec.kind})
            rec["id"] = bid
        else:  # evt
            rec = _lib_journal.make_evt(kind=spec.kind, id=bid, at=at,
                                        payload=payload)
        out.append((rec, rowid))
    return out


def _export_quota_obs() -> list:
    """Export cache.db `quota_window_snapshots` as `obs` lines (spec §8/§5.3).
    Read-only, best-effort: a missing table / cache.db is a clean empty result
    (the durable obs simply have nothing to carry). id = b:quota_window_
    snapshots:<rowid>; NOT stamped in cache.db (that table has no journal_id —
    it re-materializes from the journal)."""
    cache_path = _cctally_core.CACHE_DB_PATH
    if not cache_path.exists():
        return []
    try:
        cache = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        # account_key (#341) rides the obs top-level ``account`` field, NOT the
        # payload — so a NULL/unattributed cache row exports an obs that OMITS the
        # field (byte-stable, invariant #1). Selected after the payload cols when
        # present; a pre-#341 cache lacking the column exports NULL (never loses
        # the durable quota obs).
        has_account = any(
            str(r[1]) == "account_key"
            for r in cache.execute("PRAGMA table_info(quota_window_snapshots)")
        )
        acct_sel = ", account_key" if has_account else ", NULL AS account_key"
        cols = ", ".join(_QUOTA_SNAPSHOT_COLS)
        rows = cache.execute(
            f"SELECT id, {cols}{acct_sel} FROM quota_window_snapshots"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        cache.close()
    out = []
    for row in rows:
        rowid = row[0]
        payload = {"kind": _QUOTA_OBS_KIND}
        for i, col in enumerate(_QUOTA_SNAPSHOT_COLS):
            payload[col] = row[1 + i]
        account = row[1 + len(_QUOTA_SNAPSHOT_COLS)]
        at = payload.get("captured_at_utc") or _now_iso()
        rec = _lib_journal.make_obs(at=at, src="bootstrap", provider="codex",
                                    account=account, payload=payload)
        rec["id"] = _lib_journal.bootstrap_id("quota_window_snapshots", rowid)
        out.append(rec)
    return out


def _cutover_segment_name(now_utc: dt.datetime) -> str:
    ts = now_utc.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    return f"{_lib_journal.BOOTSTRAP_PREFIX}{ts}.jsonl"


def _write_bootstrap_segment(seg_name: str, lines: list) -> int:
    """Materialize the bootstrap segment atomically (spec §8 rename-then-stamp):
    encode all lines, write to a `.partial` sibling, fsync file + dir, verify the
    line count, then `os.replace` into `seg_name`. Returns the final byte size.
    Every line must fit the torn-tail window (append discipline)."""
    journal_dir = _cctally_core.JOURNAL_DIR
    dir_created = not journal_dir.exists()
    journal_dir.mkdir(parents=True, exist_ok=True)
    if dir_created:
        try:
            os.chmod(journal_dir, 0o700)
        except OSError:
            pass
    encoded = []
    for rec in lines:
        data = _lib_journal.encode_line(rec)
        if len(data) > _MAX_LINE_BYTES:
            raise JournalError(
                f"cutover line is {len(data)} bytes, exceeds the "
                f"{_MAX_LINE_BYTES}-byte limit (spec §4.3)")
        encoded.append(data)
    blob = b"".join(encoded)
    if blob.count(b"\n") != len(lines):
        raise JournalError(
            "cutover export line count mismatch (spec §8 verify step)")
    partial = journal_dir / (seg_name + ".partial")
    fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(journal_dir)
    seg_path = journal_dir / seg_name
    os.replace(str(partial), str(seg_path))
    _fsync_dir(journal_dir)
    if dir_created:
        _fsync_dir(journal_dir.parent)
    return os.path.getsize(seg_path)


def run_cutover(conn, *, now_utc: dt.datetime | None = None) -> "str | None":
    """Export a legacy stats.db to a bootstrap journal segment and stamp the
    epoch (spec §8). `conn` is an open stats.db at head 13 with the full schema
    (journal_id columns + journal_cursor) already applied by `open_db`.

    ONE `BEGIN IMMEDIATE`: read+export every journal-covered row (§5.3
    re-classification), write the bootstrap segment (rename-then-stamp), stamp
    `journal_id` on every exported row, advance the cursor past the bootstrap,
    and stamp `user_version = STATS_INDEX_EPOCH`, then commit. A crash before the
    commit rolls the whole thing back (the legacy DB stays fully usable); the
    next open retries idempotently (stable bootstrap ids). A truly empty install
    (nothing to export) just stamps the epoch — no bootstrap file. Returns the
    bootstrap segment basename, or None when nothing was exported."""
    if now_utc is None:
        now_utc = dt.datetime.now(dt.timezone.utc)
    epoch = _cctally_core.STATS_INDEX_EPOCH

    conn.execute("BEGIN IMMEDIATE")
    try:
        lines = []
        stamp = []  # (table, rowid)
        for spec in _CUTOVER_SPECS:
            for rec, rowid in _export_stats_table(conn, spec):
                lines.append(rec)
                if spec.stamp:
                    stamp.append((spec.table, rowid))
        lines.extend(_export_quota_obs())

        if not lines:
            # Fresh/empty install — no history to journal; just stamp the epoch.
            conn.execute(f"PRAGMA user_version = {epoch}")
            conn.commit()
            return None

        seg_name = _cutover_segment_name(now_utc)
        seg_size = _write_bootstrap_segment(seg_name, lines)

        for table, rowid in stamp:
            conn.execute(
                f"UPDATE {table} SET journal_id = ? WHERE id = ?",
                (_lib_journal.bootstrap_id(table, rowid), rowid))
        _write_cursor(conn, seg_name, seg_size)
        conn.execute(f"PRAGMA user_version = {epoch}")
        conn.commit()
        return seg_name
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


# ==========================================================================
# Account epoch-transition coordinator (#341, spec §2)
# ==========================================================================
#
# The epoch-transition coordinator was introduced when 1000 -> 1001 added the
# account dimension. Later disposable-index epoch bumps reuse it idempotently:
# `resolve_stats_epoch_mismatch` runs this coordinator BEFORE the rebuild, in
# exact order (spec §2, review finding 1):
#   (1) resolve the cutover identity WITHOUT opening stats.db — a stable-read of
#       ~/.claude.json; stably-absent / torn -> `unattributed` (never a guess);
#   (2) atomically check/append the canonical cutover op (stable semantic id
#       `accounts-cutover-v1`, timestamp-independent, so a retry cannot duplicate
#       it and replay is deterministic forever);
#   (3) only THEN capture the journal HW and rebuild — so the op is always inside
#       the rebuild's input and the legacy classifier can consume its recorded
#       Claude account.
# The cache backfill migration (Task 2) consumes the SAME op value; it never
# re-reads auth. The coordinator opens no stats.db until the rebuild's own
# scratch, so no ordering circularity exists.

CUTOVER_OP_ID = "accounts-cutover-v1"      # stable semantic id (timestamp-free)
_CUTOVER_OP_KIND = "accounts_cutover"
_CUTOVER_OP_SRC = "accounts-cutover"


def _resolve_claude_cutover_identity(claude_json_path=None) -> str:
    """Resolve the single legacy Claude account_key from ~/.claude.json without
    opening stats.db (spec §2 step 1). Identified -> account_key; stably-absent
    (missing file / no oauthAccount) or torn (unparseable mid-write) ->
    ``unattributed`` (the op is always appended with whatever was resolvable, so
    replay is deterministic forever after)."""
    path = str(claude_json_path) if claude_json_path is not None \
        else str(_cctally_core.CLAUDE_JSON_PATH)

    def _reader(data: bytes):
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            raise _lib_accounts.TornRead()
        if not isinstance(obj, dict):
            raise _lib_accounts.TornRead()
        return _lib_accounts.claude_natural_id(obj.get("oauthAccount"))

    result = _lib_accounts.stable_read_identity(path, _reader)
    if result.status == "identified":
        return _lib_accounts.account_key("claude", result.value)
    return _lib_accounts.UNATTRIBUTED


def find_accounts_cutover_op():
    """Scan the journal for the canonical cutover op; return its recorded
    ``claude_legacy_account`` (spec §2 payload), or None when it has not been
    appended yet. Cheap enough for the one-time transition + the retry check."""
    for seg in list_segments():
        seg_path = _cctally_core.JOURNAL_DIR / seg
        try:
            size = os.path.getsize(seg_path)
        except OSError:
            continue
        for _name, _off, raw in _read_segment_lines(seg_path, 0, size):
            rec = _lib_journal.decode_line(raw)
            if rec is not None and rec.get("id") == CUTOVER_OP_ID:
                return (rec.get("payload") or {}).get("claude_legacy_account")
    return None


def append_accounts_cutover_op(claude_legacy_account: str, *, at=None) -> str:
    """Check-and-append the canonical cutover op (spec §2 step 2). Idempotent on
    the stable semantic id: if the op is already present, return its recorded
    value unchanged (a retry appends nothing). The real transition holds the
    maintenance lock, which serialises concurrent transitions; the stable id +
    identical payload make any residual race converge."""
    existing = find_accounts_cutover_op()
    if existing is not None:
        return existing
    rec = _lib_journal.make_op(
        at=(at or _now_iso()), src=_CUTOVER_OP_SRC,
        payload={"kind": _CUTOVER_OP_KIND,
                 "claude_legacy_account": claude_legacy_account})
    rec["id"] = CUTOVER_OP_ID   # override the content id with the stable token
    append_record(rec)
    return claude_legacy_account


def resolve_cutover_claude_account() -> str:
    """The single legacy Claude account the cutover op recorded, for the legacy
    classifier + cache backfill. Falls back to ``unattributed`` when no op is
    present (a fresh install with no legacy Claude history)."""
    value = find_accounts_cutover_op()
    return value if value is not None else _lib_accounts.UNATTRIBUTED


def run_epoch_transition(*, claude_json_path=None) -> str:
    """The account epoch-transition coordinator (spec §2). Resolve the cutover
    identity, check/append the canonical cutover op, THEN rebuild — in that exact
    order, so the op is inside the rebuild's input. Returns the resolved
    ``claude_legacy_account``. Exposed for tests; the epoch-mismatch path calls
    it under the maintenance + ingest locks, and the rebuild's common cutover
    preserves the old index only after the replacement is validated."""
    claude_key = _resolve_claude_cutover_identity(claude_json_path)
    recorded = append_accounts_cutover_op(claude_key)
    rebuild_stats_index(context=RebuildContext(trigger="epoch-transition"))
    return recorded
