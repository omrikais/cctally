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

import collections
import contextlib
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
import _lib_cache_coverage
import _lib_journal
import _lib_journal_router
import _lib_record
import _lib_segment_summary
import _lib_selector_state


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


#: A newly completed correction batch whose effect the live index lacks. The
#: commit marker IS the narrowest complete prefix, and convergence is decided by
#: the exact `(rev, status, content_hash, batch_id)` the signal carries.
CORRECTION_KIND_NEWLY_COMPLETED = "newly_completed"

#: A batch whose durable status was `completed` and which a later record
#: tainted. Its recovery coordinate is the END OFFSET OF THAT RECORD, never the
#: batch's earliest commit: a rebuild bounded at the commit excludes the
#: tainting record, faithfully reproduces the completed correction, and meets
#: the same taint on the next tick — signalling forever instead of converging
#: (#496 S5b §3.7).
CORRECTION_KIND_COMPLETED_TO_TAINTED = "completed_to_tainted"


class CorrectionRebuildRequired(JournalError):
    """A completed correction cannot be applied incrementally to a live index.

    The recovery boundary needs more than a message: it must rebuild through
    the exact prefix that triggered the mismatch, then revalidate under
    exclusive ownership.

    `kind` selects WHICH prefix and WHICH revalidation. The two kinds do not
    share a convergence predicate: after a taint withdraws a completed batch the
    post-rebuild winner may be an older candidate that durable selector state
    deliberately does not store, so an exact expected metadata tuple is not
    computable for it.
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
        kind=CORRECTION_KIND_NEWLY_COMPLETED,
    ):
        super().__init__(message)
        self.batch_id = batch_id
        self.event_id = event_id
        self.high_water = high_water
        self.expected_metadata = expected_metadata
        self.recovery_eligible = recovery_eligible
        self.kind = kind


class CorrectionRecoveryError(JournalError):
    """Bounded correction recovery could not safely replace the live index."""


class JournalAppendTargetStale(JournalError):
    """The resolved append target is no longer the canonically-last segment.

    Raised when a writer resolved its month segment before taking the leaf lock
    and the journal moved on underneath it (#511, #496 S5b §2.4). Retryable:
    the caller re-resolves and appends again. It is a DISTINCT class precisely
    so `_cctally_cache._append_codex_quota_obs` can re-raise it while still
    swallowing genuine errors — swallowing this one would advance a file offset
    past bytes whose observation was never journaled, and the rollout JSONL
    those bytes came from evaporates.
    """


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

# NOTE: `_is_codex_quota_obs` is defined ONCE, further down beside
# `_QUOTA_OBS_KIND`. A duplicate definition used to sit here and was shadowed by
# that one at import time, so it was dead code an edit here would silently not
# reach (#496 S4). Do not reintroduce a second definition.


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
        with _open_segment_for_read(journal_dir / name) as fh:
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


def _utc_now() -> dt.datetime:
    """The append path's clock, named so both reads are the same call.

    An appender resolves its month segment once before the leaf lock and once
    again after taking it (#511). Reading the clock through one helper keeps the
    two reads identical in everything but time.
    """
    return dt.datetime.now(dt.timezone.utc)


def _validate_append_target(journal_dir, seg_name: str) -> None:
    """Refuse an append whose target is not the canonically-last segment.

    MUST be called with the leaf lock held: the whole point is that the
    canonical order cannot move between this check and the write. Both
    appenders route through it rather than duplicating the comparison, because
    fixing only one leaves the claimed immutability false for exactly the
    correction and audit group appends the durable selector depends on.

    An EXISTING target must equal the canonically-last segment. An ABSENT
    target must sort last when provisionally added to the segment list, which
    is the ordinary month rollover: the new month's file does not exist yet.

    It refuses rather than redirects. Refusal preserves physical order and lets
    a writer retry against a freshly resolved target, where silently
    redirecting a planned correction group would move it out from under a
    caller that had already reasoned about its placement (#496 S5b §2.4).
    """
    segments = list_segments()
    if not segments:
        return
    if seg_name == segments[-1]:
        return
    if seg_name not in segments:
        provisional = sorted(
            [*segments, seg_name], key=_lib_journal.segment_sort_key)
        if provisional[-1] == seg_name:
            return
    raise JournalAppendTargetStale(
        f"append target {seg_name} is not the canonically-last segment "
        f"({segments[-1]}) in {journal_dir}; re-resolve and retry"
    )


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
    this line.

    An explicitly supplied ``now_utc`` is honoured verbatim and NOT validated
    against canonical order: no production caller supplies one, and roughly a
    dozen tests pass a fixed timestamp precisely to pin segment placement. A
    deliberate placement choice is not a stall (#496 S5b §2.4)."""
    explicit_now = now_utc is not None
    if now_utc is None:
        now_utc = _utc_now()
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

        # ORDER MATTERS (#496 S5b §2.4). The dedupe no-write return above runs
        # FIRST, because a skipped append writes nothing and so has no target to
        # validate. Re-resolution and validation then run BEFORE the file is
        # opened or torn-tail-repaired, so the repair sequence is untouched.
        if not explicit_now:
            seg_name = _lib_journal.segment_name(_utc_now())
            seg_path = journal_dir / seg_name
            _validate_append_target(journal_dir, seg_name)

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

    That check is NOT a substitute for target validation: it proves nothing
    about canonical order when ``expected_high_water`` is unset, which is the
    default. A defaulted ``now_utc`` is therefore re-resolved and validated
    under the same lock, exactly as in ``append_record`` (#511, #496 S5b §2.4).
    """
    if not isinstance(records, list) or not records:
        raise ValueError("journal record group must be a non-empty list")
    explicit_now = now_utc is not None
    if now_utc is None:
        now_utc = _utc_now()
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

        # After the caller's own precondition, and before the file is opened or
        # torn-tail-repaired (#496 S5b §2.4).
        if not explicit_now:
            seg_name = _lib_journal.segment_name(_utc_now())
            seg_path = journal_dir / seg_name
            _validate_append_target(journal_dir, seg_name)

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


#: How far back `_complete_line_offset` will look for the last newline when a
#: segment does not end on one. A torn tail is one interrupted `write`, so it is
#: bounded by one record; a megabyte is orders of magnitude beyond any record
#: this journal produces, and reading the whole segment to answer a question
#: about its last byte is exactly the cost the coverage certificate exists to
#: remove.
_COMPLETE_LINE_TAIL_WINDOW = 1 << 20


def _complete_line_offset(path, size: int) -> int:
    """The offset after the last COMPLETE line in ``path``, given its ``size``.

    The coverage certificate promises decoded records, not bytes, so its covered
    boundary must be a verified newline boundary — covering a raw torn-tail
    extent would let `_repair_torn_tail` truncate that suffix and append a
    complete record ending at the same size, leaving `(segment, size)` identical
    while the covered contribution changed (spec §4.2).

    Answering costs one `read` of the segment's last byte in the healthy case,
    because every appender writes a trailing newline. Only a torn tail pays for
    the bounded backward scan, and a tail longer than
    `_COMPLETE_LINE_TAIL_WINDOW` answers 0 rather than reading the whole
    segment: 0 is a valid boundary, it is the conservative direction, and it is
    computed identically by every caller, so two callers cannot disagree.
    """
    if size <= 0:
        return 0
    try:
        with open(path, "rb") as handle:
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return size
            window = min(size, _COMPLETE_LINE_TAIL_WINDOW)
            handle.seek(size - window)
            tail = handle.read(window)
    except OSError:
        return 0
    index = tail.rfind(b"\n")
    if index < 0:
        return 0
    return size - window + index + 1


def segment_summary_sidecar_path():
    """The journal-side segment-summary sidecar (#496 S5b section 5.3).

    Beside the segments it describes rather than in `stats.db`, because a
    rebuild frequently runs precisely when `stats.db` is absent or unreadable —
    the one case a summary stored there could never serve.
    """
    return _cctally_core.JOURNAL_DIR / _lib_segment_summary.SIDECAR_NAME


def read_segment_summaries():
    """``{segment_name: SegmentSummary}`` from the sidecar, or ``{}``.

    A missing, torn, version-mismatched or checksum-mismatched sidecar answers
    an EMPTY map rather than raising, which is exactly a pass that elides
    nothing (spec section 6.3's silent full-replay fallback).
    """
    return _lib_segment_summary.read_sidecar(segment_summary_sidecar_path()) or {}


class SegmentElisionPlan:
    """The one elision planner, shared by both whole-prefix readers.

    `rebuild_stats_index` and `stats_index_matches_journal_prefix` construct it
    the same way from the same inputs, so an eliding rebuild and a prefix
    validation cannot reach different answers about the same journal (spec
    §5.6).

    **Constructed BEFORE the pass, from a short-lived read of the coverage
    certificate.** The certificate has to be consulted before the first segment
    is reached, and holding a `cache.db` read transaction open across a whole
    journal traversal would block WAL checkpointing for the length of the
    rebuild — the bloat issue #297 is about. The leg re-resolves coverage under
    its own snapshot afterwards and may reach a different verdict, because an
    ordinary Codex batch landing mid-pass advances the certificate over a
    journal this pass did not pin. `_refill_elided_quota_raw` is what makes that
    race cost a read rather than a wrong answer.
    """

    __slots__ = ("summaries", "covered", "verdict", "resolution_seen",
                 "elided", "elided_lines", "elided_bytes", "scanned",
                 "reasons", "quota_gaps")

    def __init__(self, *, summaries, covered, verdict) -> None:
        self.summaries = summaries
        #: The certificate's `coveredHighWater`, or None when it is unusable.
        self.covered = covered
        self.verdict = verdict
        #: Set once a `journal_protocol_resolution` op is decoded. From then on
        #: nothing further is elided and the prefix digest is recomputed from
        #: disk, because `PrefixHashAccumulator` cannot compose over a gap.
        self.resolution_seen = False
        self.elided = 0
        self.elided_lines = 0
        self.elided_bytes = 0
        self.scanned = 0
        self.reasons: dict = {}
        #: `(segment, index into quota_raw, summarized_size, summarized line
        #: count)` per elided segment, so the leg can re-read exactly what
        #: elision skipped if its own coverage verdict turns out not to be `ok`.
        #: The line count is carried because `_iter_segment_lines` stops
        #: SILENTLY at EOF, so a short read is invisible to the re-read's
        #: `except` clause and can only be caught by counting what came back.
        self.quota_gaps: list = []

    def decide(self, name, hi, stat_result):
        """The summary to elide ``name`` with, or None to read it.

        ``hi`` is this pass's pinned raw extent for the segment, which is its
        `st_size` for every segment the planner is allowed to consider — the
        canonically-last one is refused by the caller before this runs.
        """
        summary = self.summaries.get(name)
        if summary is None:
            self.scanned += 1
            self.reasons.setdefault(name, "summaryAbsent")
            return None
        ok, reason = _lib_segment_summary.summary_is_elidable(
            summary,
            pinned_raw_extent=hi,
            is_last=False,
            certificate_covers=self._covers(name, summary),
            resolution_seen=self.resolution_seen,
            stat_identity=(stat_result.st_dev, stat_result.st_ino),
        )
        if not ok:
            self.scanned += 1
            self.reasons.setdefault(name, reason)
            return None
        self.elided += 1
        self.elided_lines += summary.lines
        self.elided_bytes += summary.bytes
        self.reasons.setdefault(name, _lib_segment_summary.REASON_OK)
        return summary

    def _covers(self, name, summary) -> bool:
        if self.covered is None:
            return False
        return _coordinate_covers(
            self.covered, (name, int(summary.summarized_size)))

    def counters(self) -> dict:
        """The additive rebuild-record block (§6.3, "recorded, not silent")."""
        return {
            "elidedSegments": self.elided,
            "elidedLines": self.elided_lines,
            "elidedBytes": self.elided_bytes,
            "scannedSegments": self.scanned,
            "coverage": self.verdict,
            "resolutionSeen": self.resolution_seen,
            "refusals": dict(self.reasons),
        }


def plan_segment_elision(segments, high_water):
    """One `SegmentElisionPlan` for the prefix ``segments`` up to ``high_water``.

    Every failure — no journal, no sidecar, no readable cache, an invalid
    certificate — answers a plan that elides nothing, which is exactly today's
    behaviour. Spec §6.3 makes every one of those a SILENT full replay.

    The prefix's own pinned vector is deliberately NOT computed here. Only the
    WHOLE journal's vector validates a certificate (see below), and the per-
    segment extents this plan checks arrive from `_iter_range_with_segments`'s
    own `stat`. Computing a second vector restricted to ``segments`` would cost
    one `os.stat` plus one `open` per segment — through `_complete_line_offset`'s
    tail probe — on every plan construction, including inside
    `stats_index_matches_journal_prefix`, and nothing would read it.
    """
    summaries = read_segment_summaries()
    if not summaries or high_water is None:
        return SegmentElisionPlan(
            summaries={}, covered=None,
            verdict=_lib_cache_coverage.REASON_ABSENT)
    snapshot = _read_coverage_snapshot(_cctally_core.CACHE_DB_PATH)
    if snapshot is None:
        return SegmentElisionPlan(
            summaries=summaries, covered=None,
            verdict=_lib_cache_coverage.REASON_ABSENT)
    try:
        # The vector the certificate is validated against is the WHOLE journal's,
        # exactly as `_resolve_quota_cache_coverage` computes it: a root over the
        # pinned prefix alone would never match one a writer stored over every
        # segment.
        full_vector = coverage_pinned_vector()
        ok, reason = _lib_cache_coverage.certificate_is_valid(
            snapshot.certificate,
            pinned_vector=full_vector,
            physical_seq=snapshot.physical_seq,
        )
        covered = (
            snapshot.certificate.get("coveredHighWater") if ok else None)
    finally:
        _close_coverage_snapshot(snapshot)
    return SegmentElisionPlan(
        summaries=summaries, covered=covered, verdict=reason)


def _refill_elided_quota_raw(quota_raw, gaps):
    """``(stream, complete)`` — ``quota_raw`` with the elided observations back.

    Reached only when the pass elided and the cache leg's own coverage verdict
    then came back something other than `ok` — an ordinary Codex batch landing
    mid-pass is enough. The leg is about to REPLAY, and replaying a stream that
    is missing the elided segments would leave those observations unmaterialized
    while the mint asserted coverage over them.

    **Rebuilt in one ascending sweep rather than spliced in place.** An elided
    segment appends nothing to ``quota_raw``, so every segment of a contiguous
    elided run records the SAME insertion index, and inserting each one at that
    shared index pushes its predecessor later — which replays the run backwards.
    Six of the maintainer's seven bootstrap segments are quota-only and
    adjacent, so on that journal the whole run reverses. Order is not cosmetic:
    `_QUOTA_SNAPSHOT_INSERT` is `INSERT OR IGNORE` and resolves first-wins on
    the natural key, and `CodexResetAnchorResolver` decides per record in stream
    order.

    ``complete`` is False when any elided segment could not be re-read IN FULL.
    The caller must then drop its covered boundary: the observations this stream
    is missing are exactly the ones a minted certificate would claim.

    **A short read is detected by counting, not by catching.**
    `_iter_segment_lines` reads until its own `read()` returns nothing, so a
    segment that yields fewer lines than it held ends the loop with no exception
    at all. Each gap therefore carries the line count its summary recorded, and
    a re-read that returns a different number reports the shortfall rather than
    leaving the leg to certify a stream it never saw.
    """
    by_index: dict = {}
    complete = True
    for name, index, extent, expected_lines in gaps:
        recovered = []
        seen = 0
        try:
            for _seg, _off, raw in _iter_segment_lines(
                    _cctally_core.JOURNAL_DIR / name, 0, int(extent)):
                seen += 1
                record = _lib_journal.decode_line(raw)
                if record is not None and _is_codex_quota_obs(record):
                    recovered.append(raw)
        except Exception:
            # A vanished segment self-heals, because the pinned vector no longer
            # matches the journal and the certificate is invalid the moment it
            # is read. A transient read error on an UNCHANGED file does not: the
            # vector still matches, so a certificate minted over this shortened
            # stream would be valid and would claim observations nobody applied.
            # Reporting the shortfall is what stops the mint.
            #
            # EVERY exception, not just `OSError`: spec §6.3 makes every
            # degraded state here a silent full-replay fallback, and neither the
            # call site nor its caller catches anything, so one that escaped
            # would abort the whole rebuild over a re-derivable optimization.
            complete = False
            continue
        if seen != int(expected_lines):
            complete = False
        by_index.setdefault(int(index), []).extend(recovered)
    out: list = []
    cursor = 0
    for index in sorted(by_index):
        bounded = min(int(index), len(quota_raw))
        out.extend(quota_raw[cursor:bounded])
        out.extend(by_index[index])
        cursor = max(cursor, bounded)
    out.extend(quota_raw[cursor:])
    return out, complete


class _SegmentSummaryCollector:
    """Per-segment traversal facts, accumulated as the pass streams.

    The counters are per segment rather than per pass because a later pass has
    to contribute an ELIDED segment's share of them without reading it, and a
    whole-pass total cannot be decomposed after the fact.

    The last-seen fold is deliberately accumulated per segment and merged into
    the caller's running accumulator at each boundary, rather than folded into
    both. `LastSeenAccumulator.observe` runs once per record on a 1.95-million
    line journal, and folding twice would double that; merging a small map once
    per segment does not.
    """

    __slots__ = ("summaries", "last_seen", "_name", "_lo", "_stat",
                 "_lines", "_bytes", "_decodes", "_malformed", "_retained",
                 "_line_end")

    def __init__(self) -> None:
        self.summaries: dict = {}
        #: The open segment's own accumulator, or None between segments.
        self.last_seen = None
        self._name = None
        self._reset()

    def _reset(self) -> None:
        self._lo = 0
        self._stat = None
        self._lines = 0
        self._bytes = 0
        self._decodes = 0
        self._malformed = 0
        self._retained = 0
        self._line_end = 0

    def begin(self, name, lo, hi, stat_result, running) -> None:
        """Open ``name``, closing whatever segment was open before it.

        ``hi`` is this pass's pinned READ extent, which for the high-water
        segment is the pinned high-water rather than the file's size. The
        summary records `st_size` instead (spec §5.3), so ``hi`` is accepted for
        the shared `on_extent` signature and deliberately not stored.
        """
        self.close(running)
        self._name = name
        self._reset()
        self._lo = int(lo)
        self._stat = stat_result
        self._line_end = int(lo)
        self.last_seen = _lib_journal_router.LastSeenAccumulator()

    def line(self, raw, end_offset) -> None:
        self._lines += 1
        self._bytes += len(raw) + 1
        self._line_end = int(end_offset)

    def decoded(self, retained: bool) -> None:
        """One decoded record: one traversal decode AND one `decoded` element.

        ONE counter, because the two are the same number by construction — the
        rebuild appends exactly one element to `decoded` per successfully
        decoded record, the record itself when retained and a `None` placeholder
        otherwise. The summary stores it under two names because they answer
        different questions: `decodes` is the traversal counter the rebuild
        record reports, and `decoded_entry_count` is the elision contract, which
        carries a `None` sentinel a counter cannot.
        """
        self._decodes += 1
        if retained:
            self._retained += 1

    def malformed_line(self) -> None:
        self._malformed += 1

    def close(self, running) -> None:
        """Finalize the open segment and merge its last-seen into ``running``."""
        if self.last_seen is not None and running is not None:
            running.merge(
                self.last_seen.stamped,
                self.last_seen.legacy_claude_at,
                self.last_seen.legacy_codex_at,
            )
        if self._name is not None and self._lo == 0 and self._stat is not None:
            partial = self.last_seen
            self.summaries[self._name] = _lib_segment_summary.SegmentSummary(
                segment_name=self._name,
                st_dev=int(self._stat.st_dev),
                st_ino=int(self._stat.st_ino),
                # The raw `st_size` observed when this summary was written (spec
                # §5.3), NOT this pass's read extent. They differ only for the
                # high-water segment, whose extent is the pinned high-water; a
                # summary claiming that extent as the file's size would let a
                # later pass elide a segment this one did not read to the end.
                summarized_size=int(self._stat.st_size),
                complete_line_covered_offset=self._line_end,
                lines=self._lines,
                bytes=self._bytes,
                decodes=self._decodes,
                malformed=self._malformed,
                quota_only=self._retained == 0,
                decoded_entry_count=self._decodes,
                last_seen_stamped=(
                    {} if partial is None else dict(partial.stamped)),
                last_seen_legacy_claude_at=(
                    None if partial is None else partial.legacy_claude_at),
                last_seen_legacy_codex_at=(
                    None if partial is None else partial.legacy_codex_at),
            )
        self._name = None
        self.last_seen = None

    def adopt(self, summary) -> None:
        """Carry an ELIDED segment's stored summary forward unchanged.

        The pass did not re-derive it, so re-deriving it here would be inventing
        it. Carrying it forward is what keeps the sidecar complete across a run
        of consecutive eliding passes.
        """
        self.summaries[summary.segment_name] = summary


def coverage_pinned_vector():
    """The ordered ``(name, raw st_size, complete-line offset)`` triple per segment.

    This is the vector the #496 S5b coverage certificate's identity root binds
    (spec §4.2), and it is deliberately ONE function rather than two: the writer
    that advances a certificate and the rebuild that validates it must compute
    the same triples from the same rules, or the root would differ for reasons
    that have nothing to do with coverage.

    Both operands are carried because they are independent. `_repair_torn_tail`
    truncates a partial trailing line before appending, so a segment's size can
    decrease or return to a previous value — "an append strictly increases
    `st_size`" is false. A segment whose last newline sits at 100 and whose size
    is 120 is a different physical state from one where both are 100, and
    collapsing them would let a permanently torn segment certify as covered.

    It takes no segment list. Every caller wants the WHOLE journal's vector,
    because that is the only one a stored certificate's identity root can be
    validated against: a root computed over a prefix would never match one a
    writer stored over every segment.
    """
    vector = []
    for name in list_segments():
        path = _cctally_core.JOURNAL_DIR / name
        try:
            size = os.stat(path).st_size
        except OSError:
            # A segment that vanished between the listing and the stat leaves a
            # vector nothing can match, which falls back to a full replay. That
            # is the right answer: the journal shape moved under this pass.
            size = -1
        vector.append(
            (name, size, 0 if size < 0 else _complete_line_offset(path, size)))
    return tuple(vector)


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


def _open_segment_for_read(seg_path):
    """The single physical read boundary for a journal segment.

    Both read routes go through here — the streaming line reader and the
    prefix hasher — so a test can observe the exact sequence of segment opens
    a pass performs. A per-pass line or byte counter cannot: it stays correct
    while an implementation reopens a segment behind it, which is precisely the
    hidden re-read this session removes (#496 S5 §4.2).

    The cutover's bootstrap-reuse digest and the quota dedupe-index rebuild are
    the module's other two read-only segment scans, and they come through here
    as well, so "every physical read of a segment" is a property a test can
    check rather than a claim in prose. The append path is deliberately NOT
    routed here: it holds a read-write handle of its own for the torn-tail scan.
    """
    return open(seg_path, "rb")


def _iter_segment_lines(seg_path, lo: int, hi: int, *, on_bytes=None):
    """Stream `(basename, absolute-offset, raw-line-without-newline)` for every
    complete line in `[lo, hi)`, holding at most one chunk plus one partial line
    in memory. `hi` is a line boundary (a HW snapshot size or an immutable prior
    segment's full size), so no partial trailing line appears.

    `on_bytes` receives each chunk exactly as it is read, before any line
    splitting. It exists so a caller can reproduce `journal_prefix_hash` from
    the bytes this pass is already reading (#496 S4 §5.2) rather than re-reading
    the segment; it must therefore see the raw `[lo, hi)` range verbatim,
    including a torn trailing partial line that is never yielded.
    """
    name = seg_path.name
    with _open_segment_for_read(seg_path) as fh:
        fh.seek(lo)
        pos = lo
        buf = b""
        buf_at = lo
        while pos < hi:
            data = fh.read(min(_SEGMENT_READ_CHUNK, hi - pos))
            if not data:
                break
            pos += len(data)
            if on_bytes is not None:
                on_bytes(data)
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
    yield from _iter_range_with_segments(cursor, hw, list_segments())


def _iter_range_with_segments(cursor, hw, segments, *, on_segment=None,
                              on_bytes=None, on_extent=None, elide=None):
    """`iter_range` over a segment list the CALLER snapshotted (#496 S4 §4).

    `list_segments()` enumerates the journal directory at call time and orders
    bootstrap segments before observation segments, so a bootstrap segment
    appearing mid-rebuild would insert ahead of the high-water segment and shift
    the indices this function addresses by. A rebuild takes ONE snapshot at its
    pinned high-water and drives every pass from it, so two passes of the same
    rebuild cannot disagree about the journal's shape.

    `on_segment` is called for EVERY segment in the range, including one this
    function then skips because it holds no bytes in range: `journal_prefix_hash`
    frames a zero-byte segment, so a hash accumulator has to be told it exists.
    `on_bytes` is forwarded to `_iter_segment_lines`.

    `on_extent(seg, lo, hi, stat_result)` is called after this pass has pinned
    the range it will read from that segment, so a caller building a durable
    summary records the extent the pass actually covered rather than one it
    re-stats afterwards (#496 S5b section 5.3).

    `elide(seg, lo, hi, stat_result)` decides whether to SKIP the segment
    entirely — no `_open_segment_for_read`, no bytes, no lines. It runs after
    `on_extent` and instead of `on_segment`, because a skipped segment
    contributes nothing to a hash accumulator that could not compose over it
    anyway (spec section 5.1). Only #496 S5b Stage 4's elision planner supplies
    it; every other caller reads every segment exactly as before.
    """
    hw_seg, hw_size = hw
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
        stat_result = None
        if idx == hw_idx:
            hi = hw_size
        else:
            stat_result = os.stat(seg_path)
            hi = stat_result.st_size
        if on_extent is not None or elide is not None:
            if stat_result is None:
                stat_result = os.stat(seg_path)
        if elide is not None and lo == 0 and idx != hw_idx and elide(
                seg, lo, hi, stat_result):
            continue
        if on_extent is not None:
            on_extent(seg, lo, hi, stat_result)
        if on_segment is not None:
            on_segment(seg)
        if lo >= hi:
            continue
        yield from _iter_segment_lines(seg_path, lo, hi, on_bytes=on_bytes)


def _read_range(cursor, hw) -> list[tuple[str, int, bytes]]:
    """Materialized `cursor -> HW` (see :func:`iter_range`)."""
    return list(iter_range(cursor, hw))


def journal_prefix_hash(high_water) -> "str | None":
    """Hash exact raw segment bytes through one canonical high-water.

    The framing is durable: per segment, the 4-byte big-endian name length, the
    name, the 8-byte big-endian data length, then the data. These digests are
    recorded inside `journal_protocol_resolution` payloads, so a change to the
    framing invalidates every acknowledgement already written.
    """
    if high_water is None:
        return None
    digest = hashlib.sha256()
    found = False
    for segment in list_segments():
        path = _cctally_core.JOURNAL_DIR / segment
        size = high_water[1] if segment == high_water[0] else path.stat().st_size
        with _open_segment_for_read(path) as handle:
            data = handle.read(size)
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


def _capture_protocol_prefix_evidence(
    record, prior_high_water, evidence, hasher=None
) -> None:
    """Capture the actual raw prefix immediately preceding one audit record.

    `hasher` is a `_lib_journal_router.PrefixHashAccumulator` fed by the caller's
    streaming pass. When supplied, the digest comes from bytes that pass has
    already read; otherwise `journal_prefix_hash` re-reads the whole prefix from
    disk, which is what the streaming callers exist to avoid (#496 S4 §5.2). The
    two produce the identical durable digest.
    """
    if (
        record.get("t") == "op"
        and isinstance(record.get("payload"), dict)
        and record["payload"].get("kind")
        == _lib_journal._PROTOCOL_RESOLUTION_KIND
    ):
        digest = (
            hasher.digest_at(prior_high_water) if hasher is not None
            else journal_prefix_hash(prior_high_water)
        )
        evidence.append((prior_high_water, digest))


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


def _report_file_account_conflicts(conflicts: int, *, quiet: bool = False) -> None:
    """One stderr line for a run of replayed decisions that contradicted a
    different account already recorded at the same
    ``(file_identity, incarnation, from_offset)`` and were therefore DECLINED
    (first-wins, spec §3.3). Two ops at one primary key means one of them was
    minted without seeing the other, which is a real (if rare) condition with a
    real remedy, so it is reported rather than applied silently — the #374 rule.

    Every call site must invoke this AFTER its commit (closeout review C5): a
    rolled-back transaction applied nothing, so reporting from inside it would
    tell the operator about a decline that did not happen.

    ``quiet`` is the reconciliation's caller. That path is reachable from an
    ordinary command, where acceptance criterion 10 requires no new stderr
    line; the same decline is still reported by the ingest and rebuild paths
    that own the remedy this line names."""
    if conflicts > 0 and not quiet:
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


def _apply_quota_records(
    cache, records, *, reported_conflicts=None, quiet=False,
) -> None:
    """Materialize Codex quota obs into an OPEN cache.db transaction, applying
    the §3.5 precedence rule. Callers must apply the batch's file-account
    decisions FIRST, so a decision arriving in the same batch already governs
    the observations it covers.

    ``reported_conflicts`` lets ONE logical pass thread its conflict-report set
    across several calls (spec §4.6). The rebuild's recovery pass is chunked into
    many transactions, and without threading it would emit one line per chunk for
    a condition a single unchunked call reports once. Every other caller passes
    None and gets today's per-call set.

    ``quiet`` suppresses the conflict line, for the same reason
    `_report_file_account_conflicts` takes it: routing
    `recover_quota_cache_from_journal` through the shared leg made this line
    reachable from the open-time reconciliation, so `cache-sync` and the
    dashboard would emit a line they never emitted before, and acceptance
    criterion 10 requires no new stderr on an ordinary command. Its sibling was
    guarded and this one was not."""
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
    if reported_conflicts is None:
        reported_conflicts = set()
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
            # The key is recorded even when quiet, so a later non-quiet call
            # threading the same set still reports each file at most once.
            reported_conflicts.add(conflict_key)
            if not quiet:
                print(
                    "[ingest] codex attribution conflict: "
                    f"{payload.get('source_path')}"
                    f"@{payload.get('line_offset')} "
                    "observation stamped "
                    f"{observed} but the durable decision says "
                    f"{decided if decided is not None else 'unattributed'}; "
                    "keeping the decision",
                    file=sys.stderr,
                )
        values = list(row_values)
        values[16] = decided
        cache.execute(upsert_sql, tuple(values))


class CoverageInvariantViolation(RuntimeError):
    """A caller reached a covered-family delete without invalidating coverage.

    Raised rather than asserted, because `python -O` strips `assert` and this is
    the only guard standing between a new `authoritative=True` caller and a
    certificate left standing over a table that was just emptied.
    """


def _assert_coverage_already_invalidated(cache_conn) -> None:
    """Refuse an authoritative replay while a coverage certificate stands.

    `rehydrate_codex_file_accounts(authoritative=True)` empties
    `codex_file_accounts`, a member of `COVERAGE_CACHE_FAMILIES`, before
    replaying, yet its inventory entry is `preserve`. That holds only because
    its one caller — `sync_codex_cache` under `--rebuild` — already ran
    `_clear_codex_derived_rows` and committed, which deleted both the
    certificate and any recovery progress. A second caller, or a reordering
    inside `sync_codex_cache`, would break it silently and the writer-surface
    scanner could not see it, because the key is already in the inventory with a
    green label. This turns that ordering into a checked precondition.

    **Where to look when it fires.** No legitimate ordering reaches it today,
    and six were checked. If a future caller does, `sync_codex_cache`'s
    `except Exception` catches `CoverageInvariantViolation`, rolls back, sets
    `deferred_reason = "attribution_rehydration"`, prints one line and skips the
    whole Codex walk — so the symptom is a permanently deferred Codex sync
    rather than a loud failure. That containment is the right production trade
    and is not changed here; the note exists so the next debugger looks for the
    new caller rather than for a broken walk.
    """
    try:
        row = cache_conn.execute(
            "SELECT key FROM cache_meta WHERE key IN (?, ?) LIMIT 1",
            (_lib_cache_coverage.CERTIFICATE_KEY,
             _lib_cache_coverage.PROGRESS_KEY),
        ).fetchone()
    except sqlite3.Error:
        # An unreadable `cache_meta` cannot witness the invariant either way,
        # and raising here would turn a degraded cache into a failed sync.
        return
    if row is not None:
        raise CoverageInvariantViolation(
            "rehydrate_codex_file_accounts(authoritative=True) clears a covered "
            f"family while {row[0]!r} is still stored; the caller must "
            "invalidate coverage in the transaction that clears"
        )


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
    if authoritative:
        _assert_coverage_already_invalidated(cache_conn)
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


def _bounded_covered_offset(segment, raw_offset, covered_offset, decoded_end):
    """The covered boundary for ``segment``, bounded by what a pass DECODED.

    Three independent upper bounds, and the smallest wins:

    - ``raw_offset`` — the pass's own high water; nothing past it was read.
    - ``covered_offset`` — the segment's complete-line offset, so a torn
      trailing line is never inside the claim.
    - ``decoded_end`` — the end of the last line the pass CONSUMED, never past
      it. The two callers supply that operand differently and both are safe.
      `_coverage_advance_plan` (the ingest cycle) passes the end of the last
      record it DECODED, so a malformed line the batch skipped is excluded.
      `rebuild_stats_index` passes `prior_high_water`, which the streaming pass
      advances for a malformed line too — consumed but not decoded — and that is
      still an upper bound on what was read, so the claim stays true. Reading
      the operand as "the last DECODED record" is literally true only of the
      first caller.

    The third is not redundant with the second, and leaving it out was a real
    defect. Both other operands are read from the file, and the file is re-stat'd
    AFTER the pass finished reading: a segment torn at the pinned high water and
    repaired by a later append has a current complete-line offset at or past that
    high water, so `min(raw, covered)` returns the raw high water — a boundary
    whose trailing bytes the traversal never decoded, because the partial line
    was skipped. A newline-terminated MALFORMED trailing line produces the same
    divergence without any repair at all. ``decoded_end`` is the only operand
    the pass observed rather than inferred.

    A ``decoded_end`` in an EARLIER segment answers 0: the pass covered
    everything before this segment and none of it, which is exactly expressible
    and stays true.
    """
    bound = min(int(raw_offset), int(covered_offset))
    if decoded_end is None:
        return bound
    if str(decoded_end[0]) != str(segment):
        return 0
    return min(bound, int(decoded_end[1]))


def _coverage_advance_plan(cursor, covered_to, decoded_end=None):
    """``(pinned_vector, covered, applied_through)`` for an advance, or None.

    Captured BEFORE the cache flocks are acquired, and that ordering is the
    safety property. A vector captured before a concurrent append describes a
    SMALLER journal than the one that exists when the certificate is stored, so
    the stored root stops matching and the next rebuild replays. A vector
    captured after such an append would match the current journal while a record
    nobody applied sat inside it — coverage asserted over an unapplied record,
    which is the one failure the certificate must not produce.

    ``covered`` is bounded by `_bounded_covered_offset`; ``applied_through`` is
    the raw coordinate the cycle advances its cursor to. The two are returned
    separately because the certificate stores both: the next writer's contiguity
    check compares its starting cursor against `appliedThrough`, and comparing
    it against the clamped boundary instead made a single torn or malformed
    trailing line freeze the certificate permanently.
    """
    if cursor is None or covered_to is None:
        return None
    vector = coverage_pinned_vector()
    segment, offset = str(covered_to[0]), int(covered_to[1])
    for name, _raw_extent, covered_offset in vector:
        if name == segment:
            bounded = _bounded_covered_offset(
                segment, offset, covered_offset, decoded_end)
            return vector, (segment, bounded), (segment, offset)
    return None


def _cache_applier(decoded, *, cursor=None, covered_to=None,
                   decoded_end=None) -> int | None:
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

    ``cursor``, ``covered_to`` and ``decoded_end`` carry the cycle's journal
    range so this leg can ADVANCE the #496 S5b coverage certificate (spec §4.3).
    It is the writer that can do so soundly, because it owns a CONTIGUOUS batch:
    it consumed every record in `[cursor, covered_to]` and applied every
    cache-relevant one, so a predecessor applied through exactly `cursor`
    extends to `covered_to`. ``decoded_end`` is the end coordinate of the last
    record the cycle decoded, and it bounds the covered CLAIM below the raw
    cursor target whenever the two differ. All three default to None, which
    advances nothing — the ingest cycle is the only caller that knows the range,
    and a test calling this directly must not mint coverage it cannot justify.
    """
    quota_idx = [i for i, (rec, _s, _o) in enumerate(decoded)
                 if _is_codex_quota_obs(rec)]
    file_idx = [i for i, (rec, _s, _o) in enumerate(decoded)
                if _is_codex_file_account_op(rec)]
    if not quota_idx and not file_idx:
        return None
    # BEFORE the flocks, for the reason `_coverage_advance_plan` states, and
    # before them for a second reason too: it is journal file I/O, and the leg's
    # whole purpose is to hold the global cache writer lock as briefly as it can.
    plan = _coverage_advance_plan(cursor, covered_to, decoded_end)
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
            import _cctally_cache
            cache.execute("PRAGMA busy_timeout=15000")
            # Read and check the predecessor BEFORE the transaction opens
            # (spec §4.3, as corrected). `prior_is_extendable` checks contiguity
            # against this cycle's STARTING cursor — a predecessor applied
            # through less leaves a gap nobody applied, and one applied through
            # more was written by a pass that saw records this batch does not
            # carry — AND the two version fields, because `advance` re-stamps
            # the current module constants and discards `prior`, so extending a
            # certificate written under an older `interpretationVersion` would
            # launder it into a current-version one. It deliberately does not
            # run the full `certificate_is_valid`: an advance's predecessor
            # necessarily describes an older, smaller journal.
            prior = _cctally_cache.load_codex_journal_coverage_certificate(cache)
            if plan is not None:
                extendable, _why = _lib_cache_coverage.prior_is_extendable(
                    prior, applied_through=(str(cursor[0]), int(cursor[1])))
                if not extendable:
                    prior = None
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
                _cctally_cache._bump_codex_physical_mutation_seq(cache)
            if plan is not None:
                # AFTER the bump, so the certificate carries the post-bump
                # sequence, and inside this transaction so a rollback leaves the
                # predecessor standing even when the journal appends survived.
                vector, covered, applied_through = plan
                _cctally_cache._advance_codex_journal_coverage(
                    cache, prior=prior, covered=covered,
                    applied_through=applied_through, pinned_vector=vector)
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
        if rec is None:
            continue
        key = _account_of(rec)
        at = rec.get("at")
        if not key or not at:
            continue
        prev = latest.get(key)
        if prev is None or at > prev:
            latest[key] = at
    _apply_account_last_seen(conn, latest)


def _apply_account_last_seen(conn, latest) -> None:
    """Apply a precomputed `{account_key: max_at}` map.

    Split out so the rebuild can accumulate the map during its single streaming
    pass (#496 S4 §4.2) instead of walking every record again inside the
    publication transaction. The rebuild's retained list no longer contains
    observations at all, so calling `_derive_account_last_seen` over it would
    silently drop every observation's contribution."""
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


def _replace_protocol_violations(conn, rows) -> None:
    """Replace the disposable structural-violation summary from kernel rows.

    ONE writer, shared by the rebuild's whole-generation write and the live
    path's full-prefix fallback, because the two must produce IDENTICAL rows.
    They did not: the fallback wrote four columns and left `available_after`
    NULL, and it serialized with `ensure_ascii=True` while the kernel uses
    `ensure_ascii=False`. A fallback tick therefore left rows a fresh derivation
    would not match — always on `available_after`, and on the JSON bytes for any
    non-ASCII character in a violation payload — so
    `stats_index_matches_journal_prefix` answered False afterwards and the
    incremental path carried the wrong evidence forward verbatim.
    """
    conn.execute("DELETE FROM journal_protocol_violations")
    for row in rows:
        conn.execute(
            "INSERT INTO journal_protocol_violations "
            "(fingerprint, batch_id, kind, violation_json, available_after) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row.fingerprint,
                row.batch_id,
                row.kind,
                row.violation_json,
                row.available_after,
            ),
        )


def _write_selector_state(conn, rows) -> None:
    """Replace the whole durable selector generation from pure-kernel rows.

    This took over from `_write_effective_metadata`, which it superseded and
    which is now deleted: it writes the same `journal_effective_events` and
    `journal_protocol_violations` content plus the two added columns, the
    retained violation evidence, and the three selector tables. Everything lands
    in ONE transaction with the index content, so a generation never publishes
    selector state describing a different fold.
    """
    conn.execute("DELETE FROM journal_effective_events")
    for row in rows.effective:
        conn.execute(
            "INSERT INTO journal_effective_events "
            "(event_id, rev, status, content_hash, batch_id, event_json, "
            " winning_sequence, conflict_hashes_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.event_id,
                row.rev,
                row.status,
                row.content_hash,
                row.batch_id,
                row.event_json,
                row.winning_sequence,
                row.conflict_hashes_json,
            ),
        )
    _replace_protocol_violations(conn, rows.violations)
    conn.execute("DELETE FROM journal_selector_batch_records")
    for row in rows.batch_records:
        conn.execute(
            "INSERT INTO journal_selector_batch_records "
            "(batch_id, kind, key, record_digest, identity_digest, sequence, "
            " action_core_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row.batch_id,
                row.kind,
                row.key,
                row.record_digest,
                row.identity_digest,
                row.sequence,
                row.action_core_json,
            ),
        )
    conn.execute("DELETE FROM journal_selector_batches")
    for row in rows.batches:
        conn.execute(
            "INSERT INTO journal_selector_batches "
            "(batch_id, status, action_count, action_set_hash, begin_segment, "
            " begin_offset, earliest_commit_segment, earliest_commit_offset) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.batch_id,
                row.status,
                row.action_count,
                row.action_set_hash,
                row.begin_segment,
                row.begin_offset,
                row.earliest_commit_segment,
                row.earliest_commit_offset,
            ),
        )
    conn.execute("DELETE FROM journal_selector_state")
    state = rows.state
    conn.execute(
        "INSERT INTO journal_selector_state "
        "(id, generation_record_path, generation_stamped_at_utc, "
        " covered_segment, covered_offset, next_sequence, selector_version, "
        " cutover_seen, cutover_account_key) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            state.generation_record_path,
            state.generation_stamped_at_utc,
            state.covered_segment,
            state.covered_offset,
            state.next_sequence,
            state.selector_version,
            1 if state.cutover_seen else 0,
            state.cutover_account_key,
        ),
    )


def _write_selector_delta(conn, before, after) -> None:
    """Advance durable selector state from ``before`` to ``after`` in place.

    A whole-generation replace is what the rebuild does, and it is the wrong
    shape for an ingest tick: `journal_effective_events` holds one row per event
    id — 34,644 on the maintainer's production journal — and rewriting all of
    every status-line tick would trade the read this session removes for a write
    of the same size. So only the rows that actually changed are written.

    Three of the four groups express upserts only, and the reason is that **the
    fold emits no removals** for them: `after ⊇ before` in every one. It is NOT
    that a key missing from ``after`` lies outside the delta's scope — ``after``
    is computed FROM ``before``, so every key in ``before`` is by construction
    inside the read scope, and a key the kernel dropped would be a genuine
    removal. A merged generation keeps every winner and every batch, and a
    completed batch's action rows keep their keys with a NULL core rather than
    disappearing.

    Violations are the exception, so that group DOES express removals. A phase-2
    verdict is re-derived on every pass and a later record can withdraw one — an
    incomplete action set completed by a late action stops producing
    `manifest_action_sequence_mismatch` — and `_check_journal_protocol` reads
    that table, so a stale row makes `doctor` exit 2 and names a fingerprint no
    fresh derivation reproduces. The removal is safe precisely because it is
    scoped: ``before.violations`` is the delta's own scoped read, so the
    difference can only name rows this delta looked at.
    """
    state = after.state
    conn.execute(
        "INSERT INTO journal_selector_state "
        "(id, generation_record_path, generation_stamped_at_utc, "
        " covered_segment, covered_offset, next_sequence, selector_version, "
        " cutover_seen, cutover_account_key) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "generation_record_path = excluded.generation_record_path, "
        "generation_stamped_at_utc = excluded.generation_stamped_at_utc, "
        "covered_segment = excluded.covered_segment, "
        "covered_offset = excluded.covered_offset, "
        "next_sequence = excluded.next_sequence, "
        "selector_version = excluded.selector_version, "
        "cutover_seen = excluded.cutover_seen, "
        "cutover_account_key = excluded.cutover_account_key",
        (
            state.generation_record_path,
            state.generation_stamped_at_utc,
            state.covered_segment,
            state.covered_offset,
            state.next_sequence,
            state.selector_version,
            1 if state.cutover_seen else 0,
            state.cutover_account_key,
        ),
    )

    # Identity, not equality: `advance_counter` returns the row tuples BY
    # REFERENCE for a delta the fold does not consume, so a tick that only
    # moved the counters skips these diffs entirely rather than walking every
    # durable row to conclude that nothing changed.
    if before.batches is not after.batches:
        prior_batches = {row.batch_id: row for row in before.batches}
        for row in after.batches:
            if prior_batches.get(row.batch_id) == row:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO journal_selector_batches "
                "(batch_id, status, action_count, action_set_hash, begin_segment, "
                " begin_offset, earliest_commit_segment, earliest_commit_offset) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.batch_id,
                    row.status,
                    row.action_count,
                    row.action_set_hash,
                    row.begin_segment,
                    row.begin_offset,
                    row.earliest_commit_segment,
                    row.earliest_commit_offset,
                ),
            )

    if before.batch_records is not after.batch_records:
        prior_records = {
            (row.batch_id, row.kind, row.key): row for row in before.batch_records
        }
        for row in after.batch_records:
            key = (row.batch_id, row.kind, row.key)
            if prior_records.get(key) == row:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO journal_selector_batch_records "
                "(batch_id, kind, key, record_digest, identity_digest, sequence, "
                " action_core_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.batch_id,
                    row.kind,
                    row.key,
                    row.record_digest,
                    row.identity_digest,
                    row.sequence,
                    row.action_core_json,
                ),
            )

    if before.effective is not after.effective:
        prior_effective = {row.event_id: row for row in before.effective}
        for row in after.effective:
            if prior_effective.get(row.event_id) == row:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO journal_effective_events "
                "(event_id, rev, status, content_hash, batch_id, event_json, "
                " winning_sequence, conflict_hashes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.event_id,
                    row.rev,
                    row.status,
                    row.content_hash,
                    row.batch_id,
                    row.event_json,
                    row.winning_sequence,
                    row.conflict_hashes_json,
                ),
            )

    if before.violations is not after.violations:
        prior_violations = {row.fingerprint: row for row in before.violations}
        withdrawn = set(prior_violations) - {
            row.fingerprint for row in after.violations
        }
        for fingerprint in sorted(withdrawn):
            conn.execute(
                "DELETE FROM journal_protocol_violations WHERE fingerprint = ?",
                (fingerprint,),
            )
        for row in after.violations:
            if prior_violations.get(row.fingerprint) == row:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO journal_protocol_violations "
                "(fingerprint, batch_id, kind, violation_json, available_after) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    row.fingerprint,
                    row.batch_id,
                    row.kind,
                    row.violation_json,
                    row.available_after,
                ),
            )


def _read_selector_state(conn):
    """`journal_selector_state`'s single row as a kernel row, or None.

    Split out of `_read_selector_rows` because the three cheap validity checks —
    `selector_version`, cursor agreement and generation identity — are decided
    from this row alone. Materializing the four row groups ahead of them made a
    degraded tick and a counters-only tick read roughly 100,000 rows to conclude
    that nothing was needed, on the status-line path.

    Returns None when the table does not hold exactly one row, or is unreadable
    at all: a stats index predating epoch 1009 has no selector tables, and
    `open_db` returns a legacy one unchanged until its migration or epoch path
    runs. Both are degraded states that fall back to full selection, and neither
    may be mistaken for an empty-but-valid generation.
    """
    try:
        state_rows = conn.execute(
            "SELECT generation_record_path, generation_stamped_at_utc, "
            "covered_segment, covered_offset, next_sequence, selector_version, "
            "cutover_seen, cutover_account_key FROM journal_selector_state"
        ).fetchall()
    except sqlite3.Error:
        return None
    if len(state_rows) != 1:
        return None
    row = state_rows[0]
    return _lib_selector_state.SelectorStateRow(
        next_sequence=int(row[4]),
        selector_version=int(row[5]),
        covered_segment=row[2],
        covered_offset=None if row[3] is None else int(row[3]),
        generation_record_path=row[0],
        generation_stamped_at_utc=row[1],
        cutover_seen=bool(row[6]),
        cutover_account_key=row[7],
    )


def _read_selector_rows(conn, *, batch_ids=None, event_ids=None):
    """Read one generation's durable selector state back as kernel rows.

    Returns `None` when `journal_selector_state` does not hold exactly one row.
    A zero-row or unreadable state is one of the degraded cases that falls back
    to full selection, and it must never be mistaken for an empty-but-valid
    generation.

    ``batch_ids`` and ``event_ids`` SCOPE the four row groups to what one delta
    can reach. Both default to None, which reads the whole generation — the
    shape validation and the rebuild need. The live path always scopes, because
    the unscoped read materializes 34,644 `journal_effective_events` rows (mean
    825 bytes of retained JSON) and 64,248 `journal_selector_batch_records` rows
    on a production journal, on every status-line tick.

    Under a scope the returned rows are a SUBSET, so the caller must not treat a
    key absent from them as a key that was removed — `_write_selector_delta`
    documents that consequence at the write end.

    The five reads run inside ONE deferred read transaction, so they share a
    snapshot. Without it each takes its own, and a publication landing between
    two of them would return rows from two generations under one state row's
    identity. Every current caller holds the ingest or maintenance lock, so that
    interleaving is not reachable today; the transaction is what keeps it
    unreachable if a future caller does not.
    """
    _ks = _lib_selector_state

    with _deferred_read(conn):
        state = _read_selector_state(conn)
        if state is None:
            return None
        return _ks.SelectorRows(
            state=state,
            batches=_read_selector_batch_rows(conn, batch_ids),
            batch_records=_read_selector_batch_record_rows(conn, batch_ids),
            effective=_read_selector_effective_rows(conn, event_ids),
            violations=_read_selector_violation_rows(conn, batch_ids),
        )


@contextlib.contextmanager
def _deferred_read(conn):
    """One snapshot across several SELECTs, without disturbing an open txn.

    A caller already inside a transaction keeps it — beginning a second one
    raises — and the snapshot it holds is the one this block wanted anyway.
    """
    if conn.in_transaction:
        yield
        return
    try:
        conn.execute("BEGIN")
    except sqlite3.Error:
        yield
        return
    try:
        yield
    finally:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


#: SQLite's default parameter ceiling is 999, so a scope wider than this is read
#: in chunks rather than in one statement.
_SCOPE_CHUNK = 500


def _scoped_query(conn, sql, order_by, column, scope):
    """Run ``sql`` unscoped, or once per chunk of ``scope`` on ``column``."""
    if scope is None:
        yield from conn.execute(f"{sql} ORDER BY {order_by}")
        return
    keys = sorted(scope)
    for start in range(0, len(keys), _SCOPE_CHUNK):
        chunk = keys[start:start + _SCOPE_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        yield from conn.execute(
            f"{sql} WHERE {column} IN ({placeholders}) ORDER BY {order_by}",
            chunk,
        )


def _read_selector_batch_rows(conn, scope):
    _ks = _lib_selector_state
    return tuple(
        _ks.SelectorBatchRow(
            batch_id=item[0],
            status=item[1],
            action_count=None if item[2] is None else int(item[2]),
            action_set_hash=item[3],
            begin_segment=item[4],
            begin_offset=None if item[5] is None else int(item[5]),
            earliest_commit_segment=item[6],
            earliest_commit_offset=None if item[7] is None else int(item[7]),
        )
        for item in _scoped_query(
            conn,
            "SELECT batch_id, status, action_count, action_set_hash, "
            "begin_segment, begin_offset, earliest_commit_segment, "
            "earliest_commit_offset FROM journal_selector_batches",
            "batch_id", "batch_id", scope,
        )
    )


def _read_selector_batch_record_rows(conn, scope):
    _ks = _lib_selector_state
    return tuple(
        _ks.SelectorBatchRecordRow(
            batch_id=item[0],
            kind=item[1],
            key=item[2],
            record_digest=item[3],
            sequence=int(item[5]),
            identity_digest=item[4],
            action_core_json=item[6],
        )
        for item in _scoped_query(
            conn,
            "SELECT batch_id, kind, key, record_digest, identity_digest, "
            "sequence, action_core_json FROM journal_selector_batch_records",
            "batch_id, kind, key", "batch_id", scope,
        )
    )


def _read_selector_effective_rows(conn, scope):
    _ks = _lib_selector_state
    return tuple(
        _ks.SelectorEffectiveRow(
            event_id=item[0],
            rev=int(item[1]),
            status=item[2],
            content_hash=item[3],
            batch_id=item[4],
            event_json=item[5],
            winning_sequence=None if item[6] is None else int(item[6]),
            conflict_hashes_json=item[7],
        )
        for item in _scoped_query(
            conn,
            "SELECT event_id, rev, status, content_hash, batch_id, event_json, "
            "winning_sequence, conflict_hashes_json "
            "FROM journal_effective_events",
            "event_id", "event_id", scope,
        )
    )


def _read_selector_violation_rows(conn, scope):
    _ks = _lib_selector_state
    return tuple(
        _ks.SelectorViolationRow(
            fingerprint=item[0],
            batch_id=item[1],
            kind=item[2],
            violation_json=item[3],
            available_after=None if item[4] is None else int(item[4]),
        )
        for item in _scoped_query(
            conn,
            "SELECT fingerprint, batch_id, kind, violation_json, "
            "available_after FROM journal_protocol_violations",
            "batch_id, kind, fingerprint", "batch_id", scope,
        )
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


def _full_effective_selection(hw, accumulators=None):
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
        accumulators=accumulators,
    )


def _selector_generation_matches(conn, state) -> bool:
    """Whether ``state`` names the generation this connection is reading.

    `stats_publication_stamp` carries no counter, so this is an IDENTITY
    comparison rather than an ordering one, and the stamp is re-read on a FRESH
    read-only connection: `conn` may be sitting on a superseded generation
    precisely because in-place publication keeps an open reader alive on the one
    it started with, which is the case this check exists to catch.

    Absent, duplicated, unreadable or mismatched all answer False, which is the
    fall-back-to-full-selection direction (spec §3.4). A stamp read from a
    destination at any other epoch also answers False, for the same reason
    `read_publication_stamp` returns None there.
    """
    if state.generation_record_path is None or (
        state.generation_stamped_at_utc is None
    ):
        return False
    try:
        row = conn.execute(
            "SELECT file FROM pragma_database_list WHERE name = 'main'"
        ).fetchone()
    except sqlite3.Error:
        return False
    path = None if row is None else row[0]
    if not path:
        return False
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            epoch = int(probe.execute("PRAGMA user_version").fetchone()[0])
            if epoch != _cctally_core.STATS_INDEX_EPOCH:
                return False
            rows = probe.execute(
                "SELECT record_path, stamped_at_utc FROM stats_publication_stamp"
            ).fetchall()
        finally:
            probe.close()
    except BaseException:
        return False
    if len(rows) != 1:
        return False
    return (str(rows[0][0]), str(rows[0][1])) == (
        state.generation_record_path,
        state.generation_stamped_at_utc,
    )


def _cutover_for_selection(seen: bool, account_key, hw):
    """The cutover account a selection over this prefix must normalize with.

    When durable state SAW the op, the recorded value is exact and no scan
    happens — which is what removes `resolve_cutover_claude_account()`'s
    whole-journal traversal from the live path (spec §3.6). When it did not, the
    answer is `_resolve_cutover_for_rebuild`'s, so the live path and a rebuild
    pinned at the same high-water cannot disagree; on the live path the pinned
    high-water IS the current one, so that call returns immediately without
    opening a segment.
    """
    if seen:
        return (
            account_key if account_key is not None
            else _lib_accounts.UNATTRIBUTED
        )
    return _resolve_cutover_for_rebuild(_CUTOVER_UNSEEN, hw, list_segments())


def _causal_offset_of(sequence, coordinates):
    """The journal coordinate of the record at ``sequence``, or None.

    A separate function so the fail-closed path has something to exercise. The
    coordinate is MANDATORY: substituting the pinned high-water is unsafe,
    because it is `st_size`, `_iter_segment_lines` omits an incomplete trailing
    line, and `_repair_torn_tail` can truncate below it — so a cursor written
    there can sit beyond unread data (spec §3.7).
    """
    return coordinates.get(sequence)


def _prefix_position(coordinate):
    """A comparable position for a journal prefix; None is the very start.

    Ordered through `segment_sort_key`, because bootstrap segments sort before
    observation segments and a plain string comparison would get the order
    wrong across that boundary.
    """
    if not coordinate or coordinate[0] is None or coordinate[1] is None:
        return None
    return (_lib_journal.segment_sort_key(coordinate[0]), int(coordinate[1]))


def _coordinate_covers(covered, target) -> bool:
    """Whether ``covered`` reaches at least as far as ``target``."""
    left = _prefix_position(covered)
    right = _prefix_position(target)
    if left is None or right is None:
        return False
    return left >= right


#: The most journal bytes one live tick may re-fold to realign a durable
#: selector prefix that fell behind the caller's cursor.
#:
#: The gap is normally one tick wide and closes on that tick, because the common
#: transient — a `database is locked` swallowed by `_selector_generation_matches`
#: — is decided BEFORE the gap read and the next successful tick writes the
#: realigned prefix. One refusal is not the shape this cap exists for.
#:
#: The shape it exists for is a refusal that repeats. `merge_delta`'s
#: durably-completed-batch shape refusal falls back to
#: `_full_effective_selection`, whose loop acts only on entries whose `batch_id`
#: is not `None` — and after a taint the winner reverts to the base journal
#: event, whose `batch_id` IS `None`. That is issue #510, so no
#: `CorrectionRebuildRequired` is raised and no rebuild follows to realign the
#: durable prefix. Every later tick would then re-read and re-decode a
#: monotonically growing range, which is the whole-prefix read spec §3.3 exists
#: to prevent under another name.
#:
#: Past the cap the tick degrades to full selection like every other degraded
#: case, and `_observe_selector_desynchronization` reports the gap and this cap
#: in the structured rebuild record, so the state is observable rather than
#: silent. Four mebibytes is roughly 4,600 records at the maintainer's measured
#: mean journal line length; a genuine one-tick gap is a handful.
_GAP_REFOLD_BYTE_CAP = 4 * 1024 * 1024


def _selector_gap_bytes(durable, cursor) -> "int | None":
    """Journal bytes in ``[durable, cursor)``, from `stat` alone.

    Mirrors `_iter_range_with_segments`' bounds arithmetic without opening a
    single segment, which is what lets the cap refuse BEFORE the read it caps.

    Returns `None` when the distance cannot be determined — an unreadable
    segment, or a cursor naming a segment the journal no longer lists. The
    caller treats that as over the cap, which is the fall-back direction.
    """
    if cursor is None or cursor[0] is None or cursor[1] is None:
        return None
    segments = list_segments()
    if cursor[0] not in segments:
        return None
    end_idx = segments.index(cursor[0])
    if (
        durable is None
        or durable[0] is None
        or durable[1] is None
        or durable[0] not in segments
    ):
        start_idx, start_off = 0, 0
    else:
        start_idx, start_off = segments.index(durable[0]), int(durable[1])
    total = 0
    for idx in range(start_idx, end_idx + 1):
        lo = start_off if idx == start_idx else 0
        if idx == end_idx:
            hi = int(cursor[1])
        else:
            try:
                hi = os.path.getsize(_cctally_core.JOURNAL_DIR / segments[idx])
            except OSError:
                return None
        if hi > lo:
            total += hi - lo
    return total


def _selector_gap_entries(durable, cursor):
    """Decoded records in ``[durable, cursor)`` with each one's end offset.

    This is non-empty only after a tick that could not validate the durable
    generation. `_write_cursor` runs whether or not a selector delta was
    written, so such a tick advances `journal_cursor` and leaves
    `journal_selector_state` where the last rebuild put it — and nothing on the
    live path ever rewrites that table, so an EQUALITY comparison between the
    two could never hold again and F20 stayed off until a full rebuild.

    Re-folding the gap is what brings them back together. It is correct because
    the selector fold is independent of application, so folding records the
    cycle already applied changes only the selector's own accumulators, and the
    read is bounded by the gap rather than by the whole prefix.
    """
    entries = []
    start = None if _prefix_position(durable) is None else (
        durable[0], int(durable[1]))
    for segment, offset, raw in iter_range(start, cursor):
        record = _lib_journal.decode_line(raw)
        if record is None:
            # A malformed line consumes no sequence number, exactly as the
            # cycle's own read loop and `decoded_entry_count` treat it.
            continue
        entries.append((record, segment, offset + len(raw) + 1))
    return entries


def _normalized_for_selection(records, cutover_claude):
    """Copies of ``records`` with the legacy account stamp applied.

    COPIES, because `_normalize_legacy_account_stamp` mutates in place and these
    same dicts are what the cycle's pipeline folds; injecting a stamp into them
    would change what step 4b sees. The payload is copied too, since the evt/op
    branch writes into it.
    """
    copies = []
    for record in records:
        if not isinstance(record, dict):
            copies.append(record)
            continue
        copy = dict(record)
        payload = record.get("payload")
        if isinstance(payload, dict):
            copy["payload"] = dict(payload)
        _normalize_legacy_account_stamp(copy, cutover_claude)
        copies.append(copy)
    return copies


def _incremental_selection(conn, records, entries, cursor, covered):
    """Continue the durable fold over one cycle's delta, or return None.

    ``entries`` is positionally parallel to ``records``: each element is the
    ``(segment, end offset)`` of the record that consumed one sequence number.
    The map from ABSOLUTE sequence to coordinate is built HERE rather than by the
    caller, because a durable prefix behind the cursor prepends its unfolded gap
    and shifts every delta record's number.

    None means every degraded case: absent or unreadable selector state, a
    `selector_version` mismatch, a durable prefix AHEAD of the caller's cursor, a
    gap wider than `_GAP_REFOLD_BYTE_CAP`, a generation identity that does not
    match a freshly read publication stamp, a generation that moved while the
    gap was being read, a cutover operation the durable prefix has not folded,
    and anything the pure kernel refuses. Every one of them takes the existing
    full-prefix path unchanged.

    Read order is load-bearing. The three cheap checks are decided from ONE row
    and run BEFORE any row group is materialized, a delta the fold does not
    consume reads no group at all, and a delta that does is scoped to the
    batches and event ids it names. This function is on `cmd_record_usage`'s
    path, which runs on every Claude Code status-line tick, and an unscoped read
    materializes 34,644 effective rows and 64,248 batch-record rows on a
    production journal.

    There are TWO read snapshots rather than one, and the gap re-fold sits
    between them deliberately: it is arbitrary journal file I/O, and holding a
    stats.db read snapshot open across it pins the WAL against checkpointing for
    the whole read, which is the bloat #297 documents. The second snapshot
    re-reads the one state row and refuses on any difference, which is what
    keeps the row groups and the state row from coming out of two generations.

    That re-read covers in-place publication and any live delta writer, because
    both mutate the state row this connection can see. It does NOT cover
    PHYSICAL replacement: an `os.replace` between the two blocks leaves this
    connection on the unlinked old inode, whose state row and row groups are
    mutually consistent but stale. Physical replacement is excluded by the ingest
    lock the caller already holds, not by this check.
    """
    _ks = _lib_selector_state
    with _deferred_read(conn):
        state = _read_selector_state(conn)
        if state is None:
            return None
        if state.selector_version != _ks.SELECTOR_VERSION:
            return None
        durable = (state.covered_segment, state.covered_offset)
        durable_at = _prefix_position(durable)
        cursor_at = _prefix_position(cursor)
        if durable_at is not None and (
            cursor_at is None or durable_at > cursor_at
        ):
            # The durable prefix has folded records the caller has not applied,
            # so numbering the delta from `next_sequence` would leave a hole the
            # size of the overlap. The other direction is recoverable and is
            # recovered below.
            return None
        if not _selector_generation_matches(conn, state):
            return None

    if durable_at == cursor_at:
        gap = ()
    else:
        gap_bytes = _selector_gap_bytes(durable, cursor)
        if gap_bytes is None or gap_bytes > _GAP_REFOLD_BYTE_CAP:
            # Bounded rather than unbounded: a refusal that repeats leaves the
            # durable prefix behind forever, and an uncapped re-fold would grow
            # the per-tick read without limit. See `_GAP_REFOLD_BYTE_CAP`.
            return None
        gap = _selector_gap_entries(durable, cursor)

    stream = [item[0] for item in gap] + list(records)
    coordinates = {}
    for index, item in enumerate(gap):
        coordinates[state.next_sequence + index] = (item[1], item[2])
    for index, coordinate in enumerate(entries or ()):
        coordinates[state.next_sequence + len(gap) + index] = coordinate

    for record in stream:
        if isinstance(record, dict) and record.get("id") == CUTOVER_OP_ID:
            if not state.cutover_seen:
                # A cutover the durable prefix has not folded re-normalizes
                # every legacy Claude line in that prefix, which changes those
                # events' `content_hash` and `event_json`. The durable winners
                # were folded WITHOUT it, and this path normalizes only the
                # delta, so carrying them forward would diverge from a full
                # pass. Falling back is provably equivalent to one.
                return None
            break

    if not any(
        isinstance(record, dict)
        and (
            record.get("t") in _ks.FOLD_RECORD_TYPES
            or (
                record.get("t") == "op"
                and isinstance(record.get("payload"), dict)
                and record["payload"].get("kind")
                == _lib_journal._PROTOCOL_RESOLUTION_KIND
            )
        )
        for record in stream
    ):
        # Nothing here changes the fold, so only the counters move — and the
        # four row groups are not read at all. `advance_counter` returns them
        # by reference, so an empty placeholder makes the delta writer skip
        # every group by identity, exactly as a full read would have.
        placeholder = _ks.SelectorRows(state=state)
        return {
            "before": placeholder,
            "after": _ks.advance_counter(
                placeholder,
                consumed=state.next_sequence + len(stream),
                covered=covered,
            ),
            "transitions": [],
            "coordinates": coordinates,
        }

    batch_ids = _ks.delta_batch_scope(stream)
    with _deferred_read(conn):
        if _read_selector_state(conn) != state:
            return None
        batches = _read_selector_batch_rows(conn, batch_ids)
        batch_records = _read_selector_batch_record_rows(conn, batch_ids)
        effective = _read_selector_effective_rows(
            conn, _ks.delta_event_scope(stream, batch_records))
        violations = _read_selector_violation_rows(conn, batch_ids)
        # No coordinate widening beyond `batch_ids`, and the reason is a
        # reachability argument rather than an economy. `_preflight_live_events`
        # consults a batch row only for a winner whose four-tuple DIFFERS from
        # `journal_effective_events`. `_read_selector_effective_rows` reads that
        # same table, so a winner the merge passed through compares equal and is
        # skipped; and a winner the merge re-decided took a correction candidate
        # from a batch the delta named, so its batch row is already in `batches`.
        # A read over `{winner batches} - batch_ids` therefore returned rows the
        # caller could not reach, on every tick that named any correction batch.
    rows = _ks.SelectorRows(
        state=state,
        batches=batches,
        batch_records=batch_records,
        effective=effective,
        violations=violations,
    )

    cutover_claude = _cutover_for_selection(
        state.cutover_seen, state.cutover_account_key, covered)

    try:
        merged, transitions = _ks.merge_delta(
            rows,
            _normalized_for_selection(stream, cutover_claude),
            next_sequence=state.next_sequence,
            coordinates=coordinates,
            covered=covered,
        )
    except _ks.IncrementalSelectionUnavailable:
        return None
    return {
        "before": rows,
        "after": merged,
        "transitions": transitions,
        "coordinates": coordinates,
    }


def _raise_taint_transition(transition, coordinates) -> None:
    """Turn one completed-to-tainted transition into its recovery signal."""
    coordinate = _causal_offset_of(transition.causal_sequence, coordinates)
    if coordinate is None:
        raise JournalError(
            f"correction batch {transition.batch_id} moved completed -> "
            "tainted but its causal offset could not be resolved; refusing to "
            "substitute the pinned high-water"
        )
    raise CorrectionRebuildRequired(
        f"completed correction batch {transition.batch_id} was tainted by a "
        "later record and requires a stats index rebuild through it",
        batch_id=transition.batch_id,
        high_water=coordinate,
        recovery_eligible=True,
        kind=CORRECTION_KIND_COMPLETED_TO_TAINTED,
    )


def _correction_commit_high_water(batch_id, hw=None):
    """Return the exact end offset of one completed-batch commit marker.

    The batch was already structurally validated either by the full effective
    selector or by the live metadata row that names it. The earliest matching
    commit is the narrowest complete prefix and remains stable even when later
    journal bytes or crash-replayed duplicate markers exist.

    Streams rather than materializing (#496 S4): the previous form built the
    whole prefix through `_read_range` before its first-match return, so a
    marker in the first segment still paid for every later one.
    """
    if not batch_id:
        return None
    if hw is None:
        hw = journal_high_water()
    if hw is None:
        return None
    for segment, offset, raw in iter_range(None, hw):
        record = _lib_journal.decode_line(raw)
        if (
            record is not None
            and record.get("t") == "correction_batch"
            and record.get("phase") == "commit"
            and record.get("id") == batch_id
        ):
            return (segment, offset + len(raw) + 1)
    return None


def _has_correction_records(records) -> bool:
    return any(
        record.get("t") in {"correction", "correction_batch"}
        or (
            record.get("t") == "op"
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("kind")
            == _lib_journal._PROTOCOL_RESOLUTION_KIND
        )
        for record in records
    )


def _preflight_live_events(
    conn, records, hw, conflicts=None, protocol_scan=None,
    selector=None, cursor=None, entries=None,
):
    """Validate unread evt/correction records before the stats transaction.

    A READER (#374 §6): the evt records it inspects are already durably in the
    journal, so same-revision divergence must NOT raise here — that raise wedged
    every cycle over an already-poisoned journal, exactly like the rebuild. The
    divergent evt is DROPPED from the apply set, the prior effective event
    stands, and the group is appended to `conflicts` when a sink is supplied.
    `CorrectionRebuildRequired` stays fatal.

    `selector` is an out-dict (#496 S5b §3.3). When supplied and the durable
    generation validates, it receives the merged selector rows so the caller can
    advance them inside its own transaction — the delta and the cursor it
    describes then commit together. When the generation does not validate the
    dict is left empty and durable state stays where the last rebuild put it,
    which is the conservative direction: `stats_index_matches_journal_prefix`
    then answers False and its one caller rebuilds.

    `cursor` is the caller's applied journal cursor. The durable covered prefix
    must not be ahead of it; a prefix BEHIND it is re-folded from the journal
    over exactly that gap. `entries` is positionally parallel to `records` and
    carries each record's `(segment, end offset)`, from which the incremental
    step derives the absolute-sequence coordinate map.
    """
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

    has_corrections = _has_correction_records(records)
    incremental = None
    if selector is not None or has_corrections:
        incremental = _incremental_selection(conn, records, entries, cursor, hw)
    if selector is not None and incremental is not None:
        selector["before"] = incremental["before"]
        selector["after"] = incremental["after"]

    if incremental is not None:
        # The incremental path already knows every batch's verdict, so the
        # whole-prefix read is not performed at all. `protocol_scan` stays unset
        # deliberately: the merged violation rows are advanced by the selector
        # delta the caller writes, and running `_write_protocol_violations` on
        # top of that would replace them with a set derived from a scan that did
        # not happen.
        #
        # This runs even when the DELTA carries no correction record, because a
        # re-folded gap can: the merged rows are about to be written into
        # `journal_effective_events`, and installing a corrected winner without
        # the rebuild the correction demands is exactly what the loop refuses.
        # Both loops are bounded by the delta's own scope, so the added work on
        # an ordinary tick is zero rows.
        for transition in incremental["transitions"]:
            _raise_taint_transition(transition, incremental["coordinates"])
        batches = {row.batch_id: row for row in incremental["after"].batches}
        for row in incremental["after"].effective:
            if row.batch_id is None:
                continue
            prior = _metadata_row(conn, row.event_id)
            if prior is not None and (
                int(prior[0]), prior[1], prior[2], prior[3]
            ) == (row.rev, row.status, row.content_hash, row.batch_id):
                continue
            batch = batches.get(row.batch_id)
            commit = None
            if batch is not None and batch.earliest_commit_segment is not None:
                # The durable coordinate, which is what removes
                # `_correction_commit_high_water`'s traversal from this path.
                commit = (
                    batch.earliest_commit_segment,
                    int(batch.earliest_commit_offset),
                )
            if commit is None:
                # A whole-prefix stream, and a correctness fallback rather than
                # a fast path: acceptance criteria 6 and 17 ("no whole-journal
                # read on the interactive ingest path") do NOT hold here.
                #
                # It is reached only when no durable batch row carries an
                # `earliest_commit_*` for this winner, which `marker_coordinates`
                # and `_carry_coordinates` normally prevent. It is NOT reached
                # for a winner the delta merely passed through: `_metadata_row`
                # and `_read_selector_effective_rows` read the same table, so a
                # passed-through winner compares equal to its stored metadata
                # above and is skipped before ever arriving here. A winner the
                # delta RE-DECIDED names a batch the delta itself carried, whose
                # row this pass is about to write.
                commit = _correction_commit_high_water(row.batch_id, hw)
            raise CorrectionRebuildRequired(
                f"completed correction batch {row.batch_id} requires "
                "stats index rebuild",
                batch_id=row.batch_id,
                event_id=row.event_id,
                high_water=commit,
                expected_metadata=(
                    row.rev, row.status, row.content_hash, row.batch_id),
                recovery_eligible=True,
                kind=CORRECTION_KIND_NEWLY_COMPLETED,
            )
        return to_apply

    if not has_corrections:
        return to_apply

    accumulators: dict = {}
    full = _full_effective_selection(hw, accumulators)
    if protocol_scan is not None:
        protocol_scan["scanned"] = True
        # The KERNEL's rows, not a second derivation: the rebuild writes these
        # exact rows, and a fallback that wrote a different shape left the index
        # unable to match its own journal prefix afterwards.
        protocol_scan["rows"] = _lib_selector_state.violation_rows(
            full, accumulators["fold"])
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
            kind=CORRECTION_KIND_NEWLY_COMPLETED,
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
    # End offset per decoded entry, positionally parallel to `decoded`. Kept
    # separate rather than widened into those tuples because `QUOTA_APPLIER`
    # consumes them by shape (#496 S5b): the selector needs each record's end
    # coordinate, and re-encoding a record to recover its length would only be
    # right for lines this binary wrote.
    end_offsets: list = []
    malformed = 0
    cursor = None
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
            end_offsets.append(off + len(raw) + 1)

        # Step 3: cache leg (Codex quota) BEFORE the stats txn (lock-order law).
        # QUOTA_APPLIER attempts the global-then-Codex cache flock NB upsert; on
        # a busy flock it returns a prefix-stop index — the cycle processes
        # decoded[:stop], sets the cursor to decoded[stop]'s offset, and retries
        # the remainder next cycle (§5.2 step 3; prefix consumption keeps the
        # scalar cursor sound).
        cursor_target = (hw_seg, hw_size)
        if QUOTA_APPLIER is not None:
            # The cycle's own range, so the leg can advance the #496 S5b
            # coverage certificate over a batch it can prove contiguous. A
            # prefix-stop advances nothing, because the leg then committed
            # neither family and `cursor_target` moves to the stop coordinate.
            # `decoded_end` is the last DECODED record's end coordinate, which
            # bounds the covered claim below the raw cursor target whenever the
            # traversal stopped short of it — a torn or malformed trailing line.
            stop = QUOTA_APPLIER(
                decoded, cursor=cursor, covered_to=cursor_target,
                decoded_end=(
                    None if not decoded
                    else (decoded[-1][1], end_offsets[-1])))
            if stop is not None:
                _rec, stop_seg, stop_off = decoded[stop]
                cursor_target = (stop_seg, stop_off)
                decoded = decoded[:stop]
                end_offsets = end_offsets[:stop]

    records = [r for (r, _s, _o) in decoded]
    batch = [r for r in records if r.get("t") in ("obs", "op")]
    # #374: the preflight reader quarantines same-revision divergence instead of
    # raising; the groups it drops are counted on the cycle summary.
    preflight_conflicts: list = []
    protocol_scan: dict = {}
    # #496 S5b §3.3: the delta's own sequence numbering starts where the durable
    # prefix stopped, and every decoded entry consumes one number — the same
    # numbering the rebuild produces by appending a placeholder for each valid
    # non-retained record. `_incremental_selection` owns that arithmetic: it
    # rejects a durable prefix AHEAD of `cursor`, and re-folds the gap when the
    # prefix is behind it, so an absent or stale state cannot make these numbers
    # mean something else.
    selector_state: dict = {}
    selector_entries = [
        (segment, end_offsets[index])
        for index, (_rec, segment, _off) in enumerate(decoded)
    ]
    journal_evts = _preflight_live_events(
        conn,
        records,
        cursor_target,
        conflicts=preflight_conflicts,
        protocol_scan=protocol_scan,
        selector=selector_state,
        cursor=cursor,
        entries=selector_entries,
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
            _replace_protocol_violations(conn, protocol_scan.get("rows", ()))
        # 4c''. Advance durable selector state (#496 S5b §3.3), in the SAME
        # transaction as the cursor it describes, so the durable prefix equals
        # the applied journal cursor at every commit and a crash rolls both back
        # together. It runs AFTER step 4a: that step inserts the plain
        # six-column effective row for a newly applied evt, and the delta
        # replaces it with the eight-column row carrying the winning sequence.
        # Absent when the generation did not validate, which leaves durable
        # state where the last rebuild put it.
        if selector_state.get("after") is not None:
            _write_selector_delta(
                conn, selector_state["before"], selector_state["after"])
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
        # §9.2 (#496 S6 F23): the routine stats write. The `-wal`/`-shm`
        # sidecars are materialized by the FIRST write, not by the connect, so
        # hardening at open time alone would leave a 0644 WAL behind every
        # ingest cycle — the exact shape of the cache.db defect #150 fixed.
        # This runs while the ingest lock is still held, so the sidecars it
        # inspects are the ones this cycle produced.
        #
        # Guarded because it is the FIRST statement of this block and the two
        # lock releases are the last two: anything raised here — including the
        # import — skips both, and the flocks are fd-scoped, so a long-lived
        # dashboard process would hold them until it exited. Hardening is
        # best-effort; releasing the locks is not.
        try:
            import _cctally_store
            _cctally_store._harden_stats_family(_cctally_core.DB_PATH)
        except Exception as exc:  # noqa: BLE001 — never above a lock release
            print(
                f"[ingest] could not harden the stats family ({exc}); "
                "continuing",
                file=sys.stderr,
            )
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


def _tainted_batch_converged(signal: CorrectionRebuildRequired) -> bool:
    """Convergence for a completed-to-tainted signal (#496 S5b §3.7).

    `_recover_completed_correction`'s existing predicate needs an exact
    `(rev, status, content_hash, batch_id)`, and after a taint withdraws a
    completed batch the post-rebuild winner may be an OLDER candidate that
    durable selector state deliberately does not store — §3.2 keeps no losing
    candidates. So this kind converges on state that is available: the selector
    batch is tainted, selector coverage includes the causal offset, and no
    effective winner names that batch.
    """
    if signal.batch_id is None or signal.high_water is None:
        return False
    path = pathlib.Path(_cctally_core.DB_PATH)
    if not path.exists():
        return False
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    try:
        rows = _read_selector_rows(conn)
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    if rows is None:
        return False
    batch = {row.batch_id: row for row in rows.batches}.get(signal.batch_id)
    if batch is None or batch.status != "tainted":
        return False
    if not _coordinate_covers(
        (rows.state.covered_segment, rows.state.covered_offset),
        signal.high_water,
    ):
        return False
    return not any(
        row.batch_id == signal.batch_id for row in rows.effective
    )


def _correction_index_converged(signal: CorrectionRebuildRequired) -> bool:
    """Revalidate the triggering effective row without open-time mutation."""
    if signal.kind == CORRECTION_KIND_COMPLETED_TO_TAINTED:
        return _tainted_batch_converged(signal)
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
    if signal.kind == CORRECTION_KIND_COMPLETED_TO_TAINTED:
        # A different contract, deliberately: this kind carries no `event_id`
        # and no expected metadata tuple, because the post-taint winner may be a
        # candidate the durable state does not store. The causal offset is
        # mandatory and there is nothing to substitute for it, so an absent one
        # is refused rather than widened to the pinned high-water.
        if signal.batch_id is None or signal.high_water is None:
            raise CorrectionRecoveryError(
                _correction_recovery_guidance(
                    "completed-to-tainted correction lacks the causal record "
                    "offset its rebuild must include"
                )
            )
    elif (
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
                kind=signal.kind,
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
    # Segments in the pinned prefix, which is NOT the same as segments this
    # pass opened: #496 S5b's elision skips some of them and contributes their
    # stored summary instead. `traversal["elision"]["scannedSegments"]` is the
    # opened count. The value here is unchanged from before elision existed, so
    # the public `segmentsRead` key keeps meaning what it always meant.
    segments_read: int        # journal segments in the pinned prefix
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
    # #496 S4 §8.7 — ADDITIVE instrumentation. Names, units and pass boundaries
    # are fixed by the spec so the gate's assertions are unambiguous; adding
    # them does not bump the rebuild record's `schemaVersion` and no existing
    # field changes meaning.
    #: float seconds per phase. Keys: journal_read_decode, cutover_suffix,
    #: protocol_evidence, effective_selection, quota_cache_leg, stats_fold,
    #: scratch_validate, publication. The phases are DISJOINT — evidence hashing
    #: happens inside the read loop and is subtracted from journal_read_decode.
    phase_seconds: dict = field(default_factory=dict)
    #: per named pass, `{lines, bytes, decodes}`. Passes: stats_prefix (the
    #: router), cutover_suffix (zero unless the §5.1 fallback ran),
    #: protocol_evidence (`bytes` hashed and `lines` digests computed, both zero
    #: on a journal with no resolution op), quota_replay (the in-leg decode,
    #: where `bytes` is the retained byte total and `lines` equals `decodes`).
    traversal: dict = field(default_factory=dict)
    #: `tracemalloc` peak over the pre-publication window; 0 when not tracing.
    peak_heap_bytes: int = 0
    #: cache writer flock acquisition to release, in seconds.
    quota_lock_hold_seconds: float = 0.0
    #: #496 S5b — the durable selector prefix of the index this rebuild is about
    #: to REPLACE, when that prefix was behind the index's own applied journal
    #: cursor. `None` when the two agreed, when the destination did not exist, or
    #: when either could not be read. A tick that cannot validate the durable
    #: generation advances the cursor without advancing the selector, and the
    #: next tick re-folds the gap silently (§6.3); this is the only surface on
    #: which a persistent desynchronization is reported.
    selector_desynchronized: "dict | None" = None
    #: #496 S5b F11 — what the quota cache leg did: `status` in
    #: `{skipped, covered, recovered, failed}`, `reason` naming the coverage
    #: verdict, `coveredHighWater` and `replayedObservations`. `covered` is the
    #: intact path, where the leg takes no cache writer flock and replays
    #: nothing. Additive to the `schemaVersion: 1` rebuild record.
    quota_cache_coverage: dict = field(default_factory=dict)
    #: #496 S5b §4.7 — TRUE when this generation's stats quota projection was
    #: materialized from a cache whose recovery left an uncovered remainder.
    #:
    #: It is a separate field from `quota_cache_coverage` because the two answer
    #: different questions and a consumer must not read one as the other:
    #: coverage describes the CACHE, this describes the PUBLISHED INDEX. The
    #: quota projection is materialized FROM `cache.db`, so a partial cache
    #: produces a semantically partial projection inside the generation being
    #: published, and completing cache recovery later does not by itself
    #: reconcile that projection. `RebuildResult` still has no success or
    #: failure boolean, and this is not one — publication proceeds either way.
    stats_quota_projection_incomplete: bool = False


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
        "journal_selector_batch_records",
        "journal_selector_batches",
        "journal_selector_state",
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
        "stats_quota_projection_state",
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
        "idx_journal_protocol_violations_batch",
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
    "2b378fc3be1c7bb249bb0c3ddd2111a802f689cf30b3fd42806a116611c799e6"
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


def _validate_selector_state(conn, expected) -> None:
    """Refuse an index whose durable selector state is not what the journal says.

    ``expected`` is the kernel rows a full selection over the same pinned
    traversal produced. Raises `JournalError`; `stats_index_matches_journal_
    prefix` catches that and answers False, while the rebuild lets it abort the
    publication.

    Single-row cardinality is part of the contract, so a zero-row state fails
    here rather than reading as an empty-but-valid generation.
    """
    stored = _read_selector_rows(conn)
    if stored is None:
        raise JournalError(
            "durable selector state is absent or not a single row"
        )
    if _lib_selector_state.comparable(stored) != _lib_selector_state.comparable(
        expected
    ):
        raise JournalError(
            "durable selector state does not match the journal selection "
            "derived from the same pinned prefix"
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
        all_segments = list_segments()
        segments = all_segments
        elision = None
        if high_water is not None:
            if high_water[0] in segments:
                segments = segments[:segments.index(high_water[0]) + 1]
            # THE SAME planner the rebuild uses, constructed the same way from
            # the same inputs (#496 S5b §5.6). An eliding rebuild and a prefix
            # validation that disagreed about the same journal would make the
            # validation meaningless.
            #
            # Constructed BEFORE the `stats.db` connection, deliberately: the
            # planner reads `cache.db`, and the lock-order law runs cache before
            # stats. Building it inside the stats connection's lifetime would
            # open the cache underneath an already-open stats reader. Neither
            # read takes a flock and Python's sqlite3 holds no implicit read
            # transaction across `fetchall()`, so nothing deadlocks today — but
            # the law is a TOTAL order, and the cost of keeping it is one short
            # cache read on a path that was about to fail structurally.
            elision = plan_segment_elision(segments, high_water)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            _validate_rebuilt_stats_index(conn, high_water)
            # Same streaming router as the rebuild (#496 S4 §7). This function's
            # only output is a selection compared against
            # `journal_effective_events`, so it needs the decision records and
            # nothing else: no observation retention, no quota bytes, no second
            # from-byte-zero cutover scan. The `None` placeholders keep every
            # `enumerate` sequence — and therefore every violation fingerprint —
            # identical to what the rebuild wrote.
            decoded: list = []
            protocol_evidence = []
            prior_high_water = None
            cutover_captured = _CUTOVER_UNSEEN
            marker_coordinates: dict = {}
            hasher = _lib_journal_router.PrefixHashAccumulator()
            if high_water is not None:
                def _elide_segment(name, lo, hi, stat_result) -> bool:
                    nonlocal prior_high_water
                    summary = elision.decide(name, hi, stat_result)
                    if summary is None:
                        return False
                    # Only the placeholders and the boundary: this pass builds
                    # no counters, retains no observation bytes and folds no
                    # last-seen map, so the `decoded`-entry count is the whole
                    # contribution.
                    decoded.extend([None] * int(summary.decoded_entry_count))
                    prior_high_water = (name, int(summary.summarized_size))
                    return True

                for segment, offset, raw in _iter_range_with_segments(
                    None, high_water, segments,
                    on_segment=lambda name: hasher.begin_segment(
                        name, prior_high_water),
                    on_bytes=hasher.extend,
                    elide=_elide_segment,
                ):
                    record = _lib_journal.decode_line(raw)
                    if record is not None:
                        _capture_protocol_prefix_evidence(
                            record,
                            prior_high_water,
                            protocol_evidence,
                            # See the rebuild's twin: a digest cannot be composed
                            # over an elided prefix, so it is recomputed from
                            # disk instead (§5.1).
                            hasher=None if elision.elided else hasher,
                        )
                        if (isinstance(record.get("payload"), dict)
                                and record["payload"].get("kind")
                                == _lib_journal._PROTOCOL_RESOLUTION_KIND):
                            elision.resolution_seen = True
                        if (cutover_captured is _CUTOVER_UNSEEN
                                and record.get("id") == CUTOVER_OP_ID):
                            cutover_captured = _cutover_value_of(record)
                        if record.get("t") == "correction_batch":
                            marker_coordinates[len(decoded)] = (
                                segment,
                                offset + len(raw) + 1,
                            )
                        decoded.append(
                            record
                            if record.get("t")
                            in _lib_journal_router.RETAINED_RECORD_TYPES
                            else None
                        )
                    prior_high_water = (
                        segment,
                        offset + len(raw) + 1,
                    )
            hasher = None
            cutover_claude = _resolve_cutover_for_rebuild(
                cutover_captured, high_water, all_segments)
            for record in decoded:
                if record is not None:
                    _normalize_legacy_account_stamp(record, cutover_claude)
            selector_accumulators: dict = {}
            selection = _lib_journal.resolve_effective_events(
                decoded,
                protocol_prefix_evidence=protocol_evidence,
                accumulators=selector_accumulators,
            )
            # The same semantic half the rebuild's oracle runs (#496 S5b §6.2).
            # Both planners derive it from THEIR OWN pinned traversal, so an
            # eliding rebuild and a prefix validation cannot reach different
            # answers about the same journal.
            _validate_selector_state(
                conn,
                _lib_selector_state.rows_from_selection(
                    selection,
                    accumulators=selector_accumulators,
                    next_sequence=len(decoded),
                    coordinates=marker_coordinates,
                    covered=high_water,
                    cutover_seen=cutover_captured is not _CUTOVER_UNSEEN,
                    cutover_account_key=(
                        None if cutover_captured is _CUTOVER_UNSEEN
                        else cutover_captured
                    ),
                ),
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
    import _lib_stats_wal

    wal_index = _lib_stats_wal.inspect_wal_index_family(path)
    wal_verdict = wal_index.get("verdict")
    if _lib_stats_wal.is_incoherent_wal_index(wal_index):
        # The caller has already proved whole-family drain and, for a heal,
        # preserved the complete pre-checkpoint family. Opening SQLite here
        # would let a stale aPgno[] map direct valid WAL frames to wrong main
        # pages before quarantine records the original bytes.
        return "skipped_incoherent_wal_index"
    if wal_verdict in {"capture_raced", "analysis_truncated"}:
        raise JournalError(
            "old stats index WAL/SHM coherence could not be established "
            f"before cutover ({wal_verdict})"
        )
    if wal_verdict not in {"coherent", "wal_absent", "wal_empty"}:
        # A malformed/non-empty WAL, missing SHM, or another unrecognized raw
        # shape is not permission to let SQLite reconstruct or checkpoint it.
        # The caller has already preserved the complete family and can publish
        # the independently rebuilt index without mutating these old bytes.
        return "skipped_unproven_wal_index"

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
    """The running binary's released version, or None when it cannot be read.

    One implementation, in `_cctally_db`, shared with the incident manifests
    the quarantine path writes (#496 S6 §4.2).
    """
    import _cctally_db

    return _cctally_db._binary_version()


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
        "sqliteRuntimeVersion": sqlite3.sqlite_version,
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
            stamped_at = _utc_iso_now()
            conn.execute("DELETE FROM main.stats_publication_stamp")
            conn.execute(
                "INSERT INTO main.stats_publication_stamp "
                "(record_path, started_at_utc, stamped_at_utc) VALUES (?, ?, ?)",
                (str(record_path), started_at, stamped_at),
            )
            # Spec §7 case 5 asks for a failure BETWEEN the publication-stamp
            # write and the selector-identity write, so the seam sits here
            # rather than after both. An injection after both still established
            # the property, but it exercised a different interleaving than the
            # one the spec names.
            _stats_rebuild_test_pause("publication_before_commit")
            # The SAME identity onto the durable selector row, in the SAME
            # transaction (#496 S5b §3.4). The scratch was built before this
            # stamp existed, so a row populated there cannot carry the identity
            # it will publish under; writing it here is what lets a live tick
            # prove its durable state belongs to the generation it is reading.
            #
            # The existence probe is not tolerance for a missing table on a real
            # generation — `_REBUILD_REQUIRED_TABLES` makes validation refuse a
            # scratch that lacks it. This function publishes whatever schema its
            # scratch carries, and the publication-protocol tests build minimal
            # synthetic generations that legitimately have no selector state.
            if conn.execute(
                "SELECT 1 FROM main.sqlite_schema WHERE type='table' "
                "AND name='journal_selector_state'"
            ).fetchone() is not None:
                conn.execute(
                    "UPDATE main.journal_selector_state "
                    "SET generation_record_path = ?, "
                    "generation_stamped_at_utc = ?",
                    (str(record_path), stamped_at),
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


# An in-place publication used to unlink the live `-wal` and `-shm` here once
# the TRUNCATE checkpoint had emptied the WAL, to reach a sidecar-free end
# state. That is issue #516 and the call is gone: unlinking the sidecars of a
# database other connections still hold open is outside SQLite's contract, and
# what it cost is a data-correctness fault and a crash, NOT a cosmetic end
# state. Two conditions have to combine, and an earlier thirteen-arrangement
# run never combined them: something must WRITE after the unlink, and the
# reader must already have READ before it. Re-measured on both LAN runners
# (macOS, Python 3.13.14, SQLite 3.53.4, byte-identical on each):
#
#   - later writer in the SAME process — a reader that had read before the
#     unlink, read-write or `mode=ro`, raises `OperationalError: disk I/O
#     error`; and a connection that keeps writing through the unlinked inodes
#     breaks the NEXT connection opened in that process the same way;
#   - later writer in a CHILD process — that reader silently reads a STALE
#     generation, and so does a freshly opened connection in the parent;
#   - a reader that had NOT read before the unlink reads the current value,
#     which is why the earlier run observed nothing;
#   - a reader holding a pinned read transaction makes the checkpoint busy, so
#     the unlink is refused before it can happen.
#
# One mechanism seen from three ends: whichever connection holds an fd on the
# stale `-wal` inode while the shared wal-index describes frames in the other
# `-wal` inode takes the short read. A clean last close removes both sidecars
# by itself, so nothing is leaked; only the case where a handle is open, which
# is exactly the case that must not be unlinked, now leaves a zero-length WAL
# behind.


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
    import _cctally_store
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

    # Readability is not structural health. An integrity failure may consist
    # only of pages which no sqlite_schema object and no freelist entry names.
    # The table-by-table in-place swap cannot discover or reclaim such pages,
    # so publishing into that file would preserve the damage and fail its
    # post-publication verdict forever. Use the independently validated scratch
    # as a physical replacement before any live mutation instead.
    try:
        destination_integrity = [
            str(row[0]) for row in conn.execute("PRAGMA integrity_check")
        ]
    except BaseException as exc:
        try:
            conn.close()
        except Exception:
            pass
        if sp.may_fall_back_to_replacement(exc):
            print(
                "[rebuild] the live stats index failed its integrity probe "
                f"({exc}); publishing by replacement instead",
                file=sys.stderr,
            )
            record["inPlaceAttempt"] = {
                "phase": sp.PRE_COMMIT,
                "stage": "destination_integrity",
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
            return _FALL_BACK
        raise
    if destination_integrity != ["ok"]:
        try:
            conn.close()
        except Exception:
            pass
        print(
            "[rebuild] the live stats index failed integrity_check; "
            "publishing by replacement instead",
            file=sys.stderr,
        )
        record["inPlaceAttempt"] = {
            "phase": sp.PRE_COMMIT,
            "stage": "destination_integrity",
            "error": "destination failed integrity_check",
        }
        return _FALL_BACK

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
    # §9.2 (#496 S6 F23): the in-place publisher never touches the destination
    # file's mode — it mutates objects inside it — so a family that was 0644
    # before the publication is still 0644 after it. The checkpoint above is
    # also the last thing that can re-materialize a sidecar.
    _cctally_store._harden_stats_family(destination)
    _stats_rebuild_test_pause("rebuild_after_publication_replace")

    # Phase 2: validate the bytes that are now live, on a connection that never
    # saw them being written. The expected publication identity goes with it:
    # the high-water alone cannot distinguish this generation from an equally
    # journal-consistent one some other run installed.
    post_error = validate_published_stats_index(
        destination, high_water, expected_record_path=str(record_path),
    )

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
    on structurally or fails an integrity check. The mechanism is chosen
    against the destination in front of this run, not by the trigger that
    reached it: readability alone does not prove that an object-level swap can
    reclaim every damaged page.

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
    # §9.2 (#496 S6 F23): the scratch is closed and about to BECOME the
    # destination, so hardening it here is what makes the replacement private
    # from the instant it is visible under the live name. `os.replace` carries
    # the source inode's mode across; a chmod after the rename would leave a
    # window in which the live index was world-readable.
    _cctally_store._harden_stats_family(scratch)
    os.replace(str(scratch), str(destination))
    _fsync_dir(destination.parent)
    _stats_rebuild_test_pause("rebuild_after_publication_replace")

    # Phase 2: validate the bytes that are now live, on a connection that never
    # saw them being written.
    post_error = validate_published_stats_index(destination, high_water)
    # The validation opened the family read-only, which materializes sidecars
    # the removal below deletes; harden the destination itself while they are
    # still present so neither the main nor a surviving sidecar stays 0644.
    _cctally_store._harden_stats_family(destination)
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


def _decoded_quota_stream(quota_raw, cutover_claude, counters=None):
    """Decode and normalize retained observation bytes ONE AT A TIME.

    Peak heap therefore holds one record rather than the whole population.
    Normalization runs HERE, on the record decoded from the retained bytes: a
    dict normalized during the router pass is discarded with the pass, so
    stamping it there would be lost and every legacy observation would
    re-materialize with a NULL account_key. A Codex legacy line maps to
    `unattributed` regardless of the cutover value, so this does not depend on
    capture ordering (#496 S4 §6.3).
    """
    for raw in quota_raw:
        if counters is not None:
            counters["lines"] += 1
            counters["bytes"] += len(raw) + 1
        record = _lib_journal.decode_line(raw)
        if record is None:  # pragma: no cover — retained bytes decoded once already
            continue
        if counters is not None:
            counters["decodes"] += 1
        _normalize_legacy_account_stamp(record, cutover_claude)
        yield record


#: Every field the rebuild's F11 fast path needs, captured from ONE read-only
#: snapshot so the certificate and the sequence it is validated against cannot
#: come from two different cache states.
#:
#: ``conn`` is that snapshot's connection, and it is returned still inside its
#: read transaction. Under WAL a read transaction's snapshot is fixed at its
#: first read, so a caller that keeps it open sees the SAME cache state for
#: every later read — which is what lets §4.4's projection bundle be captured
#: from the snapshot the coverage verdict was decided against, rather than from
#: a second connection opened later.
_CoverageSnapshot = collections.namedtuple(
    "_CoverageSnapshot", ("certificate", "physical_seq", "conn"))


def _close_coverage_snapshot(snapshot) -> None:
    """End the snapshot's read transaction and close it. Never raises."""
    if snapshot is None or snapshot.conn is None:
        return
    try:
        snapshot.conn.rollback()
    except sqlite3.Error:
        pass
    try:
        snapshot.conn.close()
    except sqlite3.Error:
        pass


def _read_coverage_snapshot(cache_path) -> "_CoverageSnapshot | None":
    """The stored coverage certificate and physical sequence, in one `BEGIN`.

    Read-only and WAL, so it takes NO writer flock and blocks no writer — which
    is the whole point of the fast path it feeds. Two separate reads could see
    a certificate from before a writer's transaction and a sequence from after
    it, which would validate a certificate the writer had just superseded.

    **The transaction is left OPEN and the caller owns closing it.** Committing
    here would release the snapshot, and §4.4 requires the source roots, the
    observations and the ledger state to come from the same one. Every caller
    goes through `_close_coverage_snapshot`.

    Any failure answers None, and None means replay. Every degraded state here
    falls back silently (spec §6.3).
    """
    import _cctally_cache
    try:
        conn = sqlite3.connect(
            f"file:{cache_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN")
        certificate = _cctally_cache.load_codex_journal_coverage_certificate(conn)
        row = conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_physical_mutation_seq'"
        ).fetchone()
    except sqlite3.Error:
        _close_coverage_snapshot(_CoverageSnapshot(None, 0, conn))
        return None
    try:
        physical_seq = 0 if row is None or row[0] is None else int(row[0])
    except (TypeError, ValueError):
        _close_coverage_snapshot(_CoverageSnapshot(None, 0, conn))
        return None
    return _CoverageSnapshot(certificate, physical_seq, conn)


def recover_quota_cache_from_journal(high_water=None, *, quiet=False) -> dict:
    """Replay the journal's Codex quota records into `cache.db`, no stats work.

    This is the cache half of a rebuild, reachable on its own, which is what
    lets §4.7's open-time reconciliation resume a recovery a previous rebuild
    could not finish without re-publishing a whole generation. It reads the
    journal once, retains the observations as RAW BYTES for the same reason the
    rebuild does, and hands both populations to the same bounded, revalidating
    leg — so the caps, the per-chunk lock release, the restart rules and the
    mint are literally the same code, not a second implementation of them.

    ``quiet`` suppresses the leg's `[rebuild]`-prefixed stderr. This function is
    reachable from an ordinary command, where acceptance criterion 10 requires
    no new stderr line and where a `[rebuild]` prefix would be a lie about which
    operation produced it. The rebuild itself passes the default.

    Returns the leg's coverage record. `complete` is what the caller gates on.
    """
    hw = high_water if high_water is not None else journal_high_water()
    coverage: dict = {
        "status": "skipped", "reason": None, "replayedObservations": 0,
        "complete": True, "remainder": None,
    }
    if hw is None:
        return coverage
    segments = list_segments()
    if hw[0] not in segments:
        coverage.update({"status": "incomplete", "complete": False,
                         "remainder": {"reason": "missingSegment"}})
        return coverage
    segments = segments[:segments.index(hw[0]) + 1]

    quota_raw: list = []
    file_ops: list = []
    cutover_captured = _CUTOVER_UNSEEN
    # Advances for a malformed line too — consumed but not decoded — exactly as
    # the rebuild's `prior_high_water` does, because `_bounded_covered_offset`
    # is documented against the last line the pass CONSUMED.
    consumed_end = None
    for segment, offset, raw in _iter_range_with_segments(None, hw, segments):
        consumed_end = (segment, offset + len(raw) + 1)
        rec = _lib_journal.decode_line(raw)
        if rec is None:
            continue
        if (cutover_captured is _CUTOVER_UNSEEN
                and rec.get("id") == CUTOVER_OP_ID):
            cutover_captured = _cutover_value_of(rec)
        if _is_codex_file_account_op(rec):
            file_ops.append(rec)
        elif _is_codex_quota_obs(rec):
            quota_raw.append(raw)
    cutover_claude = _resolve_cutover_for_rebuild(
        cutover_captured, hw, segments)
    _rebuild_quota_cache_leg_raw(
        quota_raw, file_ops, cutover_claude, None,
        high_water=hw, coverage=coverage, decoded_end=consumed_end, quiet=quiet)
    # An absent cache.db is a clean skip for the REBUILD — the records stay
    # durable in the journal and a later `cache-sync` re-materializes them — but
    # it is not a completed recovery. The leg's `complete: True` describes "no
    # duty", and this caller's gate reads it as "the cache reached its target",
    # which for an absent cache with records to replay it certainly has not. The
    # distinction is made here rather than in the leg so the rebuild's
    # documented missing-cache behaviour is unchanged.
    if (coverage.get("complete") is True
            and (quota_raw or file_ops)
            and not _cctally_core.CACHE_DB_PATH.exists()):
        coverage.update({
            "status": "incomplete", "complete": False,
            "remainder": {
                "observations": len(quota_raw),
                "chunksRemaining": None,
                "reason": "cacheAbsent",
            },
        })
    return coverage


#: How long one process waits before re-attempting a reconciliation that did not
#: clear the flag.
#:
#: Nothing bounded the repetition before this. The states that leave the flag
#: set are ordinary — `locksBusy` under a multi-agent hook storm, `restartLimit`
#: under a competing `cache-sync --rebuild`, `noCoverageEstablished` and
#: `mintRefused` — and in every one of them the next attempt re-read the whole
#: journal and failed the same way. The marker is stamped on ATTEMPT rather than
#: on outcome, because the cost this bounds is the read, which a failing attempt
#: pays in full.
_PROJECTION_RECONCILE_RETRY_SECONDS = 300.0


def _projection_reconcile_marker_path():
    """Where the last reconciliation attempt is recorded.

    Resolved at call time from `APP_DIR`, not captured at import, because the
    path constants are re-pointed by `_init_paths_from_env` and by every test
    fixture. It is a marker file rather than a column so the throttle costs no
    schema change: `stats_quota_projection_state` is part of the epoch-1009
    fingerprint, and widening it would move the hardcoded literal and every
    doctor golden with it.
    """
    return _cctally_core.APP_DIR / "stats.quota-reconcile.attempt"


def _projection_reconcile_throttled() -> bool:
    """True when the previous attempt is too recent to repeat."""
    if _PROJECTION_RECONCILE_RETRY_SECONDS <= 0:
        return False
    try:
        age = time.time() - _projection_reconcile_marker_path().stat().st_mtime
    except OSError:
        return False
    return 0 <= age < _PROJECTION_RECONCILE_RETRY_SECONDS


def _stamp_projection_reconcile_attempt() -> None:
    """Record an attempt. Never raises — a marker that cannot be written costs
    only a repeat, and failing an unrelated command over it would be worse."""
    try:
        _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
        _projection_reconcile_marker_path().touch()
        os.utime(_projection_reconcile_marker_path(), None)
    except OSError:
        pass


def _clear_projection_reconcile_attempt() -> None:
    """Drop the marker after a reconciliation that actually cleared the flag.

    Same never-raises posture as the stamp: a marker that cannot be removed
    costs only a delayed retry, and failing the caller over it would be worse.
    """
    try:
        _projection_reconcile_marker_path().unlink()
    except OSError:
        pass


def _cache_writer_flocks_available() -> bool:
    """One non-blocking probe of the two cache writer flocks.

    The journal read is the expensive half of a recovery and was paid FIRST: a
    pass that cannot take these flocks applies no row, so reading 1.64 GB to
    discover that is pure waste. Taken here, after the maintenance and ingest
    locks, so the probe respects §4.7's lock order rather than inverting it.
    """
    from _lib_cache_writer_lock import (
        acquire_cache_writer_flocks, release_cache_writer_flocks,
    )
    try:
        held = acquire_cache_writer_flocks(
            _cctally_core.CACHE_LOCK_PATH,
            _cctally_core.CACHE_LOCK_CODEX_PATH,
            timeout=None,
        )
    except OSError:
        return False
    if held is None:
        return False
    release_cache_writer_flocks(held)
    return True


def reconcile_incomplete_quota_projection(conn) -> bool:
    """Resume cache recovery and re-materialize a gated quota projection.

    Returns whether the flag was cleared. Called from `open_db` ahead of the
    current-epoch fast return, because §4.7's "the next open reconciles it" is
    only enforceable if some open actually does it — `RebuildResult` is
    process-local and a current-epoch open otherwise returns without any
    reconciliation at all.

    `open_db` only reaches this for an ARMED process
    (`_cctally_core.enable_quota_projection_reconciliation`), because the work
    below reads the journal from zero to the current high water and `open_db`
    is on the status-line path. Three things bound what an armed process pays:
    the maintenance acquire is non-blocking rather than a thirty-second wait,
    the cache flocks are probed BEFORE the journal read, and a durable marker
    throttles the repeat.

    Lock order is the repository's, stated in §4.7 and taken in this order:
    maintenance-exclusive, then the ingest lock, then (inside the leg) the
    global and Codex cache flocks, then the cache snapshot, then the stats
    transaction. The ingest acquire is OPPORTUNISTIC: an open that loses it
    leaves the flag set and the projection gated, which is the fail-closed
    direction, rather than blocking an interactive command behind an ingest
    cycle.

    A caller that already holds the ingest lock is skipped outright. That
    context is the serialized writer — a rebuild or an ingest cycle — and it
    sets or clears this flag itself; reconciling underneath it would run a
    second recovery inside its transaction.
    """
    import _cctally_db
    import _cctally_store
    if conn is None or _cctally_store.holds_ingest_lock():
        return False
    # #146's rule is literally about advancing `user_version`, and this path
    # runs after the `_uv == STATS_INDEX_EPOCH` fast return, so it changes no
    # schema and no version. But it WRITES data rows to the real prod stats.db
    # and cache.db, and the cutover branch beside it in `open_db` already
    # refuses that from a dev checkout. `db rebuild` is the only setter of the
    # flag and already carries the guard, so the exposure is small — small is
    # not a reason for the two neighbouring write paths to disagree.
    if _cctally_db._would_block_prod_stats(_cctally_core.DB_PATH):
        return False
    try:
        row = conn.execute(
            "SELECT incomplete FROM stats_quota_projection_state WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        # NOT "the flag must be clear" — that justification is false and
        # `assert_projection_readable` no longer uses it. This is the opposite
        # decision on the opposite question: a connection whose probe fails
        # cannot safely START a reconciliation, because the reconciliation
        # writes through this very connection and would take the maintenance,
        # ingest and cache locks to do it. Declining leaves the flag set, and
        # the gate — which fails CLOSED on the same error — still refuses every
        # projection read. The two directions agree on the outcome the user
        # sees: no incomplete projection is served.
        return False
    if row is None or not int(row[0] or 0):
        return False
    if _projection_reconcile_throttled():
        return False
    try:
        with _cctally_store.stats_open_time_guard(
                live=True, wait_seconds=0.0):
            # Re-read under the exclusive: another process may have reconciled
            # it while this one waited, and re-running the whole recovery to
            # discover that would cost a journal read for nothing.
            try:
                row = conn.execute(
                    "SELECT incomplete FROM stats_quota_projection_state "
                    "WHERE id = 1").fetchone()
            except sqlite3.Error:
                return False
            if row is None or not int(row[0] or 0):
                return False
            fd = _acquire_ingest_lock("opportunistic", 0.0)
            if fd is None:
                return False
            try:
                if not _cache_writer_flocks_available():
                    return False
                _stamp_projection_reconcile_attempt()
                coverage = recover_quota_cache_from_journal(quiet=True)
                if coverage.get("complete") is not True:
                    return False
                cleared = _rematerialize_and_clear_projection_gate(
                    conn, quiet=True)
                if cleared:
                    # The marker bounds the cost of a FAILING attempt. A success
                    # leaves nothing to bound, and keeping it makes the throttle
                    # punish the next genuine incompleteness: a flag set again
                    # within the interval — a second interrupted rebuild, which
                    # is exactly the sequence an upgrade under lock contention
                    # produces — would wait the interval out for no reason.
                    _clear_projection_reconcile_attempt()
                return cleared
            finally:
                _release_ingest_lock(fd)
    except _cctally_db.StatsDbMaintenanceError:
        # Another process owns stats maintenance. Leaving the flag set is the
        # fail-closed direction and costs nothing but a later attempt.
        return False


def _rematerialize_and_clear_projection_gate(conn, *, quiet=False) -> bool:
    """Re-materialize the projection from a complete cache and clear the flag.

    Both happen in ONE stats transaction, so a crash between them cannot leave a
    cleared flag over a projection that was never rewritten.

    The bundle is read from a read-only cache snapshot taken AFTER recovery
    completed, which is the same ordering the rebuild's recovery path uses and
    for the same reason: a snapshot from before those writes would miss exactly
    the rows recovery restored.

    **No bundle, no clear.** `rematerialize_quota_projection_for_rebuild`
    treats an absent or unreadable cache as a clean no-op, so calling it with
    `bundle=None` returns without touching a single projection row — and
    clearing the flag afterwards would serve the partial projection the flag
    exists to refuse over a projection this function never rewrote. Every other
    degraded path here fails closed and this one must too.

    This is the SECOND of the two guards that close that path, and the two are
    NESTED rather than disjoint: the `cacheAbsent` remainder in
    `recover_quota_cache_from_journal` fires on a subset of the states this one
    covers, because an absent cache also yields no bundle. Both still earn their
    place. The first names the state in the coverage record, which is what the
    `db rebuild --json` `cacheRecovery.remainder.reason` reports and what an
    operator reads; this one additionally covers a cache that EXISTS but cannot
    be read, which the first sees as present and would let through.
    """
    import _cctally_quota as _q
    cache_path = _cctally_core.CACHE_DB_PATH
    bundle = None
    if cache_path.exists():
        try:
            cache = sqlite3.connect(
                f"file:{cache_path}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            return False
        try:
            cache.execute("PRAGMA busy_timeout=5000")
            cache.execute("BEGIN")
            bundle = _q.load_quota_projection_bundle(cache)
        except sqlite3.Error:
            return False
        finally:
            try:
                cache.rollback()
            except sqlite3.Error:
                pass
            cache.close()
    if bundle is None:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        _q.rematerialize_quota_projection_for_rebuild(conn, bundle=bundle)
        conn.execute(
            "UPDATE stats_quota_projection_state SET incomplete = 0, "
            "target_version = 0, recovery_target_json = NULL WHERE id = 1")
        conn.commit()
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        if not quiet:
            print(f"[stats] quota projection reconciliation failed: {exc}",
                  file=sys.stderr)
        return False
    return True


#: The version stamped into `stats_quota_projection_state.target_version`.
#:
#: The target is VERSIONED rather than a bare coordinate so a target written by
#: one binary is never misread by another: a later binary that changes what the
#: target names reads a version it does not recognize and treats the projection
#: as reconcilable-by-full-recovery instead of interpreting fields it would
#: misunderstand.
PROJECTION_RECOVERY_TARGET_VERSION = 1


def _write_quota_projection_state(conn, *, coverage, high_water):
    """Record whether this generation's quota projection is complete.

    Returns the flag it wrote, or **None** when it could not write one. The
    third state is the point: returning `False` for both "wrote clear" and
    "could not write" is what made a shipped test vacuous, because the caller
    then reported a complete projection for a generation whose flag says
    nothing. `None` is fail-closed at the caller — an unwritten flag is
    reported as incomplete, which costs a reconciliation and never serves a
    partial projection.

    Runs inside the caller's transaction, which is the one that materialized
    the projection — the flag and the rows it describes must commit or roll
    back together.

    A coverage record with no `complete` key is a leg that never ran (a
    `update_quota_cache=False` rebuild, or one with nothing to do), and that is
    complete by absence rather than incomplete by ignorance.
    """
    incomplete = bool(coverage) and coverage.get("complete") is False
    target = None
    if incomplete:
        target = json.dumps(
            {
                "highWater": (
                    None if high_water is None
                    else [str(high_water[0]), int(high_water[1])]
                ),
                "coveredHighWater": coverage.get("coveredHighWater"),
                "remainder": coverage.get("remainder"),
            },
            separators=(",", ":"), sort_keys=True,
        )
    try:
        conn.execute(
            "INSERT INTO stats_quota_projection_state"
            "(id, incomplete, target_version, recovery_target_json) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET incomplete = excluded.incomplete, "
            "target_version = excluded.target_version, "
            "recovery_target_json = excluded.recovery_target_json",
            (1 if incomplete else 0,
             PROJECTION_RECOVERY_TARGET_VERSION if incomplete else 0,
             target),
        )
    except sqlite3.Error as exc:  # pragma: no cover — pre-1009 index
        print(f"[rebuild] quota projection state not recorded: {exc}",
              file=sys.stderr)
        return None
    return incomplete


def _read_quota_projection_bundle(snapshot_out):
    """§4.4's projection bundle from a retained coverage snapshot, or None.

    ``snapshot_out`` is the single-element list the leg appends its still-open
    read transaction to on the intact path. The bundle is read from that same
    transaction — under WAL its snapshot is fixed at the certificate read, so
    the certificate, the sequence, the source roots, the observations and the
    ledger state all describe ONE cache state.

    None means "no snapshot to consume", and the projection then opens its own
    connection. That is the recovery path, where a snapshot from before the
    leg's writes would miss the rows recovery restored, and every degraded case,
    where falling back is the same silent full behaviour §6.3 asks for.
    """
    if not snapshot_out:
        return None
    snapshot = snapshot_out[0]
    if snapshot is None or snapshot.conn is None:
        return None
    try:
        import _cctally_quota as _q
        return _q.load_quota_projection_bundle(snapshot.conn)
    except (sqlite3.Error, ValueError):
        return None
    finally:
        _close_coverage_snapshot(snapshot)


def _resolve_quota_cache_coverage(cache_path, high_water, decoded_end=None):
    """``(vector, covered, verdict, snapshot)`` for this coverage decision.

    ``snapshot`` is returned with its read transaction still open, or None when
    none could be taken. The caller closes it — on the intact path after it has
    read §4.4's projection bundle from it, and immediately otherwise.

    ``verdict`` is one of `_lib_cache_coverage`'s reason strings, and `REASON_OK`
    means the intact path: every cache-relevant journal record in the pinned
    prefix is already materialized, so the leg takes no writer flock at all.

    Two independent checks, and BOTH are required. `certificate_is_valid` is an
    identity check — it asks whether the certificate describes the journal and
    cache physically in front of it — and a certificate covering only the first
    of three segments passes it. Whether coverage REACHES this rebuild's pinned
    high-water is a separate comparison, made here.

    ``decoded_end`` is the traversal's own last complete-line boundary, and it
    bounds the covered claim for the reason `_bounded_covered_offset` gives.
    """
    vector = coverage_pinned_vector()
    covered = None
    for name, _raw_extent, covered_offset in vector:
        if high_water is not None and name == str(high_water[0]):
            covered = (name, _bounded_covered_offset(
                name, int(high_water[1]), covered_offset, decoded_end))
    if covered is None:
        # No boundary could be resolved at all. Reporting `identityRoot` here
        # would tell an operator the journal identity moved when in fact the
        # certificate was never consulted.
        return vector, None, _lib_cache_coverage.REASON_NO_BOUNDARY, None
    snapshot = _read_coverage_snapshot(cache_path)
    if snapshot is None:
        return vector, covered, _lib_cache_coverage.REASON_ABSENT, None
    ok, reason = _lib_cache_coverage.certificate_is_valid(
        snapshot.certificate,
        pinned_vector=vector,
        physical_seq=snapshot.physical_seq,
    )
    if not ok:
        return vector, covered, reason, snapshot
    if not _coordinate_covers(snapshot.certificate["coveredHighWater"], covered):
        return (vector, covered,
                _lib_cache_coverage.REASON_COVERED_HIGH_WATER, snapshot)
    return vector, covered, _lib_cache_coverage.REASON_OK, snapshot


def _rebuild_quota_cache_leg_raw(
    quota_raw, decoded, cutover_claude, counters=None, *,
    high_water=None, coverage=None, decoded_end=None, snapshot_out=None,
    quiet=False, elision_gaps=None,
) -> float:
    """Re-materialize cache.db `quota_window_snapshots` AND the #416 Codex
    attribution map from the journal (spec §5.4 + #416 spec §3.4), fed RAW
    ENCODED LINES for the observations instead of decoded dicts.

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
    cleanly).

    Taking raw bytes is what makes the rebuild affordable. 1.81M decoded
    observation dictionaries are roughly six gigabytes against 1.64 GB of raw
    bytes, so retaining bytes removes about four gigabytes of peak heap while
    adding NO file input and NO second traversal — only the JSON decode of
    records already in memory moves inside the flocks (#496 S4 §6.3). That
    decode is why the measured hold is longer than it was before S4; see the
    spec's §6.3 for the measured figures.

    Ordering is preserved: file-account decisions are ops, so the router already
    retains them decoded and they are available before the observation loop
    begins, exactly as the §3.5 precedence rule requires.

    Returns the measured flock hold in seconds, which acceptance criterion 7
    caps.
    """
    file_accounts = [
        r for r in decoded if r is not None and _is_codex_file_account_op(r)
    ]
    if coverage is not None:
        # `complete` is TRUE for a skip, and that is not a slip. Spec §4.7
        # distinguishes stats publication success from cache-recovery
        # completeness, and a leg with nothing to recover has no shortfall to
        # report — the incompleteness this flag exists for is an uncovered
        # REMAINDER, not an absent duty.
        coverage.update({
            "status": "skipped", "reason": None, "replayedObservations": 0,
            "chunks": 0, "plannedChunks": 0, "restarts": 0,
            "concurrentWriter": False, "complete": True, "remainder": None,
        })
    # `elision_gaps` keeps this from returning early on an empty stream: a pass
    # that elided every quota-bearing segment has an empty `quota_raw` and a
    # cache that may still need those observations if the certificate stopped
    # being valid while the pass ran.
    if not quota_raw and not file_accounts and not elision_gaps:
        return 0.0
    cache_path = _cctally_core.CACHE_DB_PATH
    if not cache_path.exists():
        return 0.0

    # F11's intact path (spec §4.4). Decided from a read-only WAL snapshot
    # BEFORE any flock is requested, so a covered cache costs this rebuild zero
    # writer-lock hold rather than the measured 23.0 s it costs today. The
    # vector is pinned here for the same reason `_coverage_advance_plan` pins
    # its own before the flocks: one captured after a concurrent append would
    # match the current journal while a record nobody applied sat inside it.
    vector, covered, verdict, snapshot = _resolve_quota_cache_coverage(
        cache_path, high_water, decoded_end)
    if coverage is not None:
        coverage["reason"] = verdict
    if verdict == _lib_cache_coverage.REASON_OK:
        if coverage is not None:
            coverage.update({
                "status": "covered",
                "coveredHighWater": [covered[0], covered[1]],
                "replayedObservations": 0,
                "complete": True, "remainder": None,
            })
        # The intact path wrote nothing, so this snapshot still describes the
        # cache the verdict was decided against and §4.4's projection bundle may
        # be read from it. Hand it to the caller with its read transaction open.
        if snapshot_out is not None:
            snapshot_out.append(snapshot)
        else:
            _close_coverage_snapshot(snapshot)
        return 0.0

    # The recovery path is about to WRITE to this cache, and a snapshot taken
    # before those writes would miss exactly the rows recovery restores. It is
    # closed here and the projection reads its own afterwards, which is what the
    # pre-change leg did at the same point.
    _close_coverage_snapshot(snapshot)
    if elision_gaps:
        # The read pass elided on a certificate that was valid when it planned,
        # and this leg's own verdict disagrees — an ordinary Codex batch landing
        # mid-pass is enough, because it advances the certificate over a journal
        # this pass did not pin. Recovery is about to replay, so the elided
        # segments' observations are re-read now, OUTSIDE both flocks, and the
        # replay proceeds over the same stream a non-eliding pass would have
        # built. Elision then costs one read in the racy case rather than an
        # unmaterialized observation under a certificate claiming coverage.
        before = len(quota_raw)
        quota_raw, refilled = _refill_elided_quota_raw(quota_raw, elision_gaps)
        if not refilled:
            # A segment this pass elided could not be re-read, so its
            # observations are absent from the stream about to be replayed.
            # `_run_bounded_recovery` mints whenever `covered` is not None, and
            # `covered` is the whole pinned prefix — so leaving it set would
            # certify coverage over rows nobody applied. Dropping it routes the
            # pass through `noCoverageEstablished`: it applies what it holds and
            # certifies nothing.
            covered = None
        if coverage is not None:
            coverage["elisionRefill"] = {
                "segments": len(elision_gaps),
                "observations": len(quota_raw) - before,
                "complete": refilled,
            }
    return _run_bounded_recovery(
        quota_raw, file_accounts, cutover_claude, counters,
        cache_path=cache_path, vector=vector, covered=covered,
        high_water=high_water, coverage=coverage, quiet=quiet,
    )


#: Per-chunk caps for the recovery pass. BOTH are enforced (spec §4.5): capping
#: by records alone lets one chunk of large observations blow the memory bound,
#: and capping by bytes alone lets a chunk of tiny ones carry far more rows than
#: one transaction should.
#:
#: 8 MiB against the maintainer's 905-byte mean observation is roughly 9,000
#: records, so the byte cap binds first on real data and the record cap is the
#: backstop for a journal of unusually small lines. The pair keeps peak decoded
#: memory at one chunk, which is what makes per-chunk decode satisfy F11 and S4's
#: measured 2.09 GB together.
_RECOVERY_CHUNK_BYTES = 8 * 1024 * 1024
_RECOVERY_CHUNK_RECORDS = 20_000

#: How many times one pass may restart from zero before giving up and reporting
#: an uncovered remainder. A restart is triggered by a destructive writer, and a
#: writer that keeps clearing the cache would otherwise make this pass loop for
#: as long as it kept doing so. Three is enough to ride out a single competing
#: `cache-sync --rebuild`; past that the honest answer is an incomplete pass,
#: which §4.7 already has a contract for.
_RECOVERY_MAX_RESTARTS = 3

#: Test-only seam, called with the number of chunks committed so far AFTER both
#: flocks are released and BEFORE the next chunk requests them. That is exactly
#: the window a competing writer can use, so a test proving a hold was RELEASED
#: rather than merely shortened writes here rather than racing a thread.
_RECOVERY_BETWEEN_CHUNKS = None


def _recovery_pass_identity():
    """``(pass_id, started_at)`` for one recovery pass.

    ``started_at`` is a coarse wall clock in microseconds, and it exists only to
    ORDER two passes for the monotonic compare-and-swap. It is never compared for
    equality and never used as a deadline.
    """
    return (
        hashlib.sha256(
            f"{os.getpid()}:{time.time_ns()}:{id(object())}".encode("utf-8")
        ).hexdigest()[:16],
        int(time.time() * 1_000_000),
    )


def _recovery_state(cache):
    """``(physical_seq, source_roots_digest)`` read inside the caller's txn."""
    import _cctally_quota
    seq = _cctally_quota.codex_physical_mutation_seq(cache)
    digest = _lib_cache_coverage.source_roots_digest(
        _cctally_quota._cache_root_keys(cache))
    return seq, digest


def _run_bounded_recovery(
    quota_raw, file_accounts, cutover_claude, counters, *,
    cache_path, vector, covered, high_water, coverage, quiet=False,
) -> float:
    """Recovery as resumable chunks, each capped by bytes AND record count.

    Per chunk: decode that chunk's raw lines, acquire the global then the Codex
    flock, `BEGIN IMMEDIATE`, revalidate, apply, persist progress, commit,
    release both locks, discard the decoded chunk. The decode is DELIBERATELY
    per chunk and not once up front: decoding everything first would
    re-materialize the roughly six gigabytes of observation dictionaries S4
    removed to bring peak heap from 6.50 GB to 2.09 GB.

    **Every chunk revalidates after reacquiring the locks, because releasing
    them admits a destructive writer.** A concurrent `cache-sync --rebuild` can
    take the locks between two chunks, run `_clear_codex_derived_rows`, and
    delete both the materialized quota state and the certificate. A pass that
    resumed from an in-memory cursor would continue over cleared state and mint
    a certificate claiming coverage the cache does not have. Progress is
    therefore persisted separately from the certificate, every destructive clear
    deletes it in the same transaction (`_invalidate_codex_journal_coverage_
    certificate` does both), and a pass that finds it gone restarts from zero.
    Restarting is always sound, because every apply is idempotent on its natural
    key.

    Two pieces of per-record state survive the chunking (spec §4.6). The anchor
    resolver is RECONSTRUCTED per chunk — `_apply_quota_records` builds one per
    call, and it seeds lazily from the rows already stored, so each chunk's
    anchor decisions are made against the state committed at that moment rather
    than against a cache another writer may have mutated underneath a
    long-lived resolver. The conflict-report set is THREADED across the chunks
    of one pass, so a single recovery reports at most what a single unchunked
    call reports today.

    Returns the total measured flock hold across every chunk.
    """
    import _cctally_cache
    from _lib_cache_writer_lock import (
        acquire_cache_writer_flocks,
        release_cache_writer_flocks,
    )

    pass_id, started_at = _recovery_pass_identity()
    identity_root = _lib_cache_coverage.identity_root(vector)
    spans = _lib_cache_coverage.chunk_spans(
        [len(raw) + 1 for raw in quota_raw],
        byte_cap=_RECOVERY_CHUNK_BYTES, record_cap=_RECOVERY_CHUNK_RECORDS,
    )
    # NOT cleared on a restart, deliberately. §4.6 requires that one recovery
    # report at most what a single unchunked call reports today, and a restart
    # replays records this pass has already reported a conflict for.
    reported_conflicts: set = set()
    # The replay counters are cumulative across every decode this pass makes, so
    # a restart would count the re-decoded prefix twice and inflate
    # `traversal["quota_replay"]` against what one unchunked call reports. They
    # are snapshotted here and restored on each restart.
    counter_baseline = dict(counters) if counters is not None else None
    hold_seconds = 0.0
    restarts = 0
    chunk_index = 0
    applied = 0
    committed_chunks = 0
    outcome = "recovered"
    stop_reason = None
    concurrent_writer = False

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)

    def _record(status):
        if coverage is None:
            return
        coverage.update({
            "status": status,
            "coveredHighWater": (
                None if covered is None else [covered[0], covered[1]]),
            "replayedObservations": applied,
            "chunks": committed_chunks,
            "plannedChunks": len(plan),
            "restarts": restarts,
            "concurrentWriter": concurrent_writer,
            "complete": status == "recovered",
            "remainder": (
                None if status == "recovered"
                else {
                    "observations": max(0, len(quota_raw) - applied),
                    "chunksRemaining": max(
                        0, len(plan) - committed_chunks),
                    "reason": stop_reason,
                }
            ),
        })

    # Chunk 0 carries EVERY file-account decision, because §3.5 makes the
    # file/range decision authoritative over the observation stamp and a
    # decision must already govern the observations it covers. That population
    # is the retained decision records — 5.08% of a production journal — and it
    # is bounded by the decisions rather than by the observations, so it does
    # not reintroduce the unbounded hold the chunking exists to remove.
    #
    # It shares chunk 0's transaction with the first observation span rather
    # than taking a lock cycle of its own, so a recovery small enough to fit one
    # chunk takes exactly ONE hold — byte-identical in shape to the unchunked
    # form this replaces.
    if spans:
        plan = [(True, spans[0])] + [(False, span) for span in spans[1:]]
    else:
        plan = [(True, None)]

    while chunk_index < len(plan):
        with_decisions, span = plan[chunk_index]
        decoded_chunk = None
        if span is not None:
            start, stop, _bytes = span
            decoded_chunk = list(_decoded_quota_stream(
                quota_raw[start:stop], cutover_claude, counters))
        try:
            held = acquire_cache_writer_flocks(
                _cctally_core.CACHE_LOCK_PATH,
                _cctally_core.CACHE_LOCK_CODEX_PATH,
                timeout=15.0,
            )
        except OSError as exc:
            if not quiet:
                print(f"[rebuild] quota cache leg lock failed: {exc}",
                      file=sys.stderr)
            outcome, stop_reason = "incomplete", "lockFailed"
            break
        if held is None:
            if not quiet:
                print("[rebuild] quota cache leg locks busy; skipping",
                      file=sys.stderr)
            outcome, stop_reason = "incomplete", "locksBusy"
            break
        held_from = time.monotonic()
        cache = None
        try:
            try:
                cache = sqlite3.connect(str(cache_path), timeout=15.0)
                cache.execute("PRAGMA busy_timeout=15000")
                cache.execute("BEGIN IMMEDIATE")
                if chunk_index > 0:
                    seq, digest = _recovery_state(cache)
                    verdict, why, saw_writer = (
                        _lib_cache_coverage.resume_verdict(
                            _cctally_cache.load_codex_recovery_progress(cache),
                            pass_id=pass_id, started_at=started_at,
                            identity_root=identity_root, physical_seq=seq,
                            source_roots_digest=digest,
                        )
                    )
                    concurrent_writer = concurrent_writer or saw_writer
                    if verdict == _lib_cache_coverage.YIELD:
                        cache.rollback()
                        outcome, stop_reason = "incomplete", why
                        break
                    if verdict == _lib_cache_coverage.RESTART:
                        cache.rollback()
                        restarts += 1
                        if restarts > _RECOVERY_MAX_RESTARTS:
                            outcome, stop_reason = "incomplete", "restartLimit"
                            break
                        chunk_index = 0
                        applied = 0
                        committed_chunks = 0
                        if counter_baseline is not None:
                            counters.clear()
                            counters.update(counter_baseline)
                        continue
                file_conflicts = 0
                if with_decisions:
                    # Decisions FIRST inside this transaction — the same §3.5
                    # precedence ordering `_cache_applier` keeps.
                    _restored, file_conflicts = _apply_file_account_records(
                        cache, file_accounts)
                if decoded_chunk is not None:
                    _apply_quota_records(
                        cache, decoded_chunk,
                        reported_conflicts=reported_conflicts, quiet=quiet)
                    applied += len(decoded_chunk)
                last = chunk_index == len(plan) - 1
                if last and covered is None:
                    # No boundary was resolvable at all, so this pass covered
                    # bytes it cannot name and established nothing. Reporting
                    # `complete` here would say "cache recovery complete" for a
                    # pass that certified no prefix. The progress record goes
                    # too: it describes a run that will never be finished.
                    cache.execute(
                        "DELETE FROM cache_meta WHERE key = ?",
                        (_lib_cache_coverage.PROGRESS_KEY,))
                    cache.commit()
                    committed_chunks += 1
                    chunk_index += 1
                    _report_file_account_conflicts(file_conflicts, quiet=quiet)
                    outcome, stop_reason = "incomplete", "noCoverageEstablished"
                    break
                if last and covered is not None:
                    # The pass has now replayed the whole pinned prefix, so it
                    # is the one caller allowed to ESTABLISH coverage rather
                    # than only extend it — it is the only writer that reads the
                    # journal. Inside the same transaction as the last rows it
                    # certifies, so a rollback leaves no certificate over rows
                    # that never landed. The progress record goes away with it:
                    # a certificate supersedes progress, and leaving both would
                    # let a later pass resume a run that already finished.
                    minted = _cctally_cache._advance_codex_journal_coverage(
                        cache, prior=None, covered=covered,
                        applied_through=(
                            str(high_water[0]), int(high_water[1])),
                        pinned_vector=vector, allow_mint=True)
                    cache.execute(
                        "DELETE FROM cache_meta WHERE key = ?",
                        (_lib_cache_coverage.PROGRESS_KEY,))
                    if not minted:
                        # The mint refused — a stored certificate already reaches
                        # further than this pass consumed to. Nothing false was
                        # certified, but no coverage was established either, so
                        # the honest report is an incomplete pass rather than
                        # `complete: True` over an absent certificate.
                        cache.commit()
                        committed_chunks += 1
                        chunk_index += 1
                        _report_file_account_conflicts(file_conflicts, quiet=quiet)
                        outcome, stop_reason = "incomplete", "mintRefused"
                        break
                else:
                    seq, digest = _recovery_state(cache)
                    _cctally_cache._store_codex_recovery_progress(
                        cache,
                        _lib_cache_coverage.make_progress(
                            pass_id=pass_id, started_at=started_at,
                            chunks=chunk_index + 1,
                            identity_root=identity_root, physical_seq=seq,
                            source_roots_digest=digest,
                            covered=(covered if covered is not None
                                     else ("", 0))),
                    )
                cache.commit()
                committed_chunks += 1
                chunk_index += 1
                _report_file_account_conflicts(file_conflicts, quiet=quiet)
            except sqlite3.Error as exc:
                if cache is not None:
                    try:
                        cache.rollback()
                    except sqlite3.Error:
                        pass
                if not quiet:
                    print(f"[rebuild] quota cache leg write failed: {exc}",
                          file=sys.stderr)
                outcome, stop_reason = "failed", "writeFailed"
                break
            finally:
                if cache is not None:
                    cache.close()
        finally:
            hold_seconds += time.monotonic() - held_from
            release_cache_writer_flocks(held)
        decoded_chunk = None
        if _RECOVERY_BETWEEN_CHUNKS is not None:
            _RECOVERY_BETWEEN_CHUNKS(committed_chunks)

    _record(outcome)
    return hold_seconds


#: `_resolve_cutover_for_rebuild` distinguishes "the streaming pass never saw the
#: op" from "it saw the op and the op recorded no account". `find_accounts_cutover_op`
#: makes the same distinction by returning at the first matching RECORD id, so a
#: plain `None` cannot stand in for both without changing which answer wins.
_CUTOVER_UNSEEN = object()


def _cutover_value_of(record) -> "str | None":
    """The cutover op's recorded `claude_legacy_account`, or None when this
    record is not the canonical cutover op. Shared by the rebuild's inline
    capture and `find_accounts_cutover_op` so the two cannot disagree."""
    if record is None or record.get("id") != CUTOVER_OP_ID:
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload.get("claude_legacy_account")


def _resolve_cutover_for_rebuild(captured, hw, segments, counters=None) -> str:
    """The cutover account for one rebuild, reading each byte at most once.

    `captured` is what the streaming pass saw inside the pinned prefix, or
    `_CUTOVER_UNSEEN`. When the prefix did not contain the op — reachable,
    because correction recovery, journal repair and rederive all pin
    high-waters, and the op sits at 92.9% of a production journal — scan ONLY
    the unvisited suffix, from the pinned high-water to the current one,
    stopping at the first match. Resolving from the prefix alone would flip
    those rebuilds to `unattributed` and restamp every legacy Claude
    observation, moving `accounts.last_seen_utc` with it (#496 S4 §5.1).
    """
    if captured is not _CUTOVER_UNSEEN:
        return captured if captured is not None else _lib_accounts.UNATTRIBUTED
    if hw is None or not segments:
        return _lib_accounts.UNATTRIBUTED
    current = journal_high_water()
    if current is None or current == hw:
        return _lib_accounts.UNATTRIBUTED
    if current[0] not in segments:
        # A segment appended after this rebuild's snapshot. Every pass of one
        # rebuild reads the same snapshot (§4), so the suffix stops at its end
        # rather than silently adopting a different journal shape.
        last = segments[-1]
        current = (last, os.path.getsize(_cctally_core.JOURNAL_DIR / last))
        if current == hw:
            return _lib_accounts.UNATTRIBUTED
    for _segment, _offset, raw in _iter_range_with_segments(hw, current, segments):
        if counters is not None:
            counters["lines"] += 1
            counters["bytes"] += len(raw) + 1
        record = _lib_journal.decode_line(raw)
        if counters is not None and record is not None:
            counters["decodes"] += 1
        if record is not None and record.get("id") == CUTOVER_OP_ID:
            value = _cutover_value_of(record)
            return value if value is not None else _lib_accounts.UNATTRIBUTED
    return _lib_accounts.UNATTRIBUTED


def _observe_selector_desynchronization(path) -> "dict | None":
    """The selector prefix of the index at ``path``, when it is behind its cursor.

    Returns `None` for the healthy case and for every unreadable one — an absent
    destination, a pre-1009 index without the selector tables, a missing cursor
    row. This is diagnostic reporting over a re-derivable artifact, so it must
    never be the reason a rebuild fails.
    """
    import _lib_stats_wal

    path = pathlib.Path(path)
    if not path.exists():
        return None
    wal_index = _lib_stats_wal.inspect_wal_index_family(path)
    if wal_index.get("verdict") not in {"coherent", "wal_absent", "wal_empty"}:
        # This observation is optional. Opening an unproven WAL/SHM family can
        # rewrite its headers before the cutover path preserves the incident
        # bytes, so only let SQLite see a raw-classified safe family (#514).
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        state = _read_selector_state(conn)
        if state is None:
            return None
        cursor = _read_cursor(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    covered = (state.covered_segment, state.covered_offset)
    covered_at = _prefix_position(covered)
    cursor_at = _prefix_position(cursor)
    if cursor_at is None or (covered_at is not None and covered_at >= cursor_at):
        return None
    # A gap the live path REFUSES to re-fold is a different state from one it
    # will close on the next tick, and only the rebuild record makes either
    # visible. `gapBytes` is `None` when the distance could not be determined,
    # which the live path also treats as over the cap.
    gap_bytes = _selector_gap_bytes(covered, cursor)
    return {
        "coveredSegment": covered[0],
        "coveredOffset": covered[1],
        "cursorSegment": cursor[0],
        "cursorOffset": int(cursor[1]),
        "gapBytes": gap_bytes,
        "gapByteCap": _GAP_REFOLD_BYTE_CAP,
        "gapExceedsCap": (
            gap_bytes is None or gap_bytes > _GAP_REFOLD_BYTE_CAP
        ),
    }


def rebuild_stats_index(
    *,
    context: RebuildContext,
    target_path=None,
    high_water: "tuple[str, int] | None" = None,
    update_quota_cache: bool = True,
    before_swap=None,
) -> RebuildResult:
    """Rebuild the stats index under a SHARED `artifact-retention.lock` hold.

    #496 S6 §5.3. A rebuild publishes three artifacts that reclamation must not
    mark while they are being written: the preserved-family incident manifest,
    the second manifest write `_record_post_checkpoint_damage` performs after
    the explicit checkpoint, and the rebuild record that names both. The hold
    spans all three, so no observer ever sees the incident half-described.

    The hold is taken here rather than only at the producer call sites because
    `db rebuild` and the auto-heal worker are not the only callers — the epoch
    rebuild and the deferred rebuild reach the same cutover. It is refcounted,
    so a caller that already holds it pays nothing.

    See `_rebuild_stats_index_locked` for the rebuild itself.
    """
    import _cctally_retention

    with _cctally_retention.retention_shared(label="stats rebuild"):
        return _rebuild_stats_index_locked(
            context=context,
            target_path=target_path,
            high_water=high_water,
            update_quota_cache=update_quota_cache,
            before_swap=before_swap,
        )


def _rebuild_stats_index_locked(
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
    # Imported HERE, not at module scope: `_cctally_journal` is on the ingest
    # path every status-line tick reaches, and `import tracemalloc` measured
    # 2.9 ms. Only a rebuild reads the peak, so only a rebuild pays for it.
    import tracemalloc

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
    # ONE segment snapshot for the whole rebuild (#496 S4 §4). `list_segments()`
    # re-enumerates the directory at call time and orders bootstrap segments
    # first, so a bootstrap segment appearing mid-rebuild would shift the indices
    # `iter_range` addresses by; before this, the read pass and the cutover scan
    # each listed separately and could already disagree about the journal's shape.
    all_segments = list_segments()
    segments = all_segments
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

    # Observed BEFORE the scratch is built, because it describes the index this
    # rebuild is about to replace (#496 S5b). It is the only report a persistent
    # selector desynchronization gets: the live path's own recovery is silent by
    # §6.3's uniform policy, so without this a repeatedly degrading generation
    # check would leave no trace anywhere.
    selector_desync = _observe_selector_desynchronization(dest)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    scratch = dest.with_name(dest.name + f".rebuilding-{stamp}")
    _remove_db_family(scratch)

    # Build a fresh schema'd empty index at the scratch path. `_target_path`
    # DISARMS open_db's auto-heal (no recursion) and yields the current schema
    # (migrations stamped, gated backfills no-op on empty, fixups marked).
    conn = _cctally_core.open_db(_target_path=str(scratch))
    malformed = 0
    lines_folded = 0
    phase_seconds: dict = {}
    traversal = {
        name: {"lines": 0, "bytes": 0, "decodes": 0}
        for name in ("stats_prefix", "cutover_suffix", "protocol_evidence",
                     "quota_replay")
    }
    quota_lock_hold = 0.0
    #: Whether this generation's quota projection was materialized from a cache
    #: whose recovery left an uncovered remainder. Declared out here because the
    #: record and the result are assembled outside the try that sets it.
    stats_projection_incomplete = False
    #: §4.4's single read-only WAL snapshot, carried from the quota cache leg to
    #: the projection pass. The leg appends it ONLY on the intact path, where it
    #: wrote nothing and its snapshot therefore still describes the cache the
    #: coverage verdict was decided against. A destructive clear landing between
    #: the verdict and the projection would otherwise publish a generation whose
    #: quota projection was materialized from a cleared cache while the verdict
    #: already read `covered`. Declared out here so the `finally` that closes it
    #: cannot meet an unbound name.
    quota_snapshot: list = []
    #: #496 S5b §4.7 "recorded, not silent": the coverage verdict, the boundary
    #: it reached and how many observations it had to replay. Additive to a
    #: `schemaVersion: 1` record under S4's rule, and the ONLY surface reporting
    #: it — §6.3 makes every degraded coverage state a silent full replay.
    quota_coverage: dict = {
        "status": "skipped", "reason": None, "replayedObservations": 0}
    tracing = tracemalloc.is_tracing()
    if tracing:
        tracemalloc.reset_peak()
    try:
        # ONE streaming pass. Decode each line once, feed the account last-seen
        # accumulator, capture the cutover inline, and retain only what a
        # consumer actually needs: the decision records decoded (5.08% of a
        # production journal) and the Codex quota observations as RAW BYTES
        # (1.64 GB against roughly six gigabytes of dicts). Everything else is
        # dropped as soon as it has contributed (#496 S4 §4).
        decoded: list = []
        quota_raw: list = []
        protocol_evidence = []
        prior_high_water = None
        cutover_captured = _CUTOVER_UNSEEN
        # Sequence -> journal coordinate, for correction-batch MARKERS only
        # (#496 S5b §3.5). That is what removes `_correction_commit_high_water`'s
        # separate traversal from the live fast path, and it is bounded to the
        # markers because nothing else is ever looked up: a production journal's
        # 64,248 correction records carry far fewer markers than lines.
        marker_coordinates: dict = {}
        last_seen = _lib_journal_router.LastSeenAccumulator()
        summaries = _SegmentSummaryCollector()
        # F12 (#496 S5b §5). Planned before the first segment is reached, from
        # the sidecar this pass's predecessor wrote and the coverage certificate
        # Stage 3 mints. A plan that elides nothing is exactly today's pass.
        elision = plan_segment_elision(segments, hw)
        hasher = _lib_journal_router.PrefixHashAccumulator()
        evidence_seconds = 0.0
        prefix = traversal["stats_prefix"]
        read_started = time.monotonic()

        def _elide_segment(name, lo, hi, stat_result) -> bool:
            """Contribute an elided segment's share instead of reading it."""
            nonlocal prior_high_water
            nonlocal malformed
            summary = elision.decide(name, hi, stat_result)
            if summary is None:
                return False
            prefix["lines"] += summary.lines
            prefix["bytes"] += summary.bytes
            prefix["decodes"] += summary.decodes
            malformed += summary.malformed
            # EXACTLY the segment's `decoded`-entry count. A quota-only segment
            # contributes only placeholders, and `resolve_effective_events`
            # numbers candidates with `enumerate(records)` — three of the seven
            # structural violation kinds hash that number into a DURABLE
            # fingerprint that `journal_protocol_resolution` ops reference by
            # name, so contributing the wrong count makes an acknowledged
            # violation unresolvable (#496 S5b §5.4).
            decoded.extend([None] * int(summary.decoded_entry_count))
            last_seen.merge(
                summary.last_seen_stamped,
                summary.last_seen_legacy_claude_at,
                summary.last_seen_legacy_codex_at,
            )
            summaries.adopt(summary)
            elision.quota_gaps.append(
                (name, len(quota_raw), summary.summarized_size, summary.lines))
            prior_high_water = (name, int(summary.summarized_size))
            return True

        if hw is not None:
            for segment, offset, raw in _iter_range_with_segments(
                None, hw, segments,
                on_segment=lambda name: hasher.begin_segment(
                    name, prior_high_water),
                on_bytes=hasher.extend,
                on_extent=lambda name, lo, hi, st: summaries.begin(
                    name, lo, hi, st, last_seen),
                elide=_elide_segment,
            ):
                prefix["lines"] += 1
                prefix["bytes"] += len(raw) + 1
                summaries.line(raw, offset + len(raw) + 1)
                rec = _lib_journal.decode_line(raw)
                if rec is None:
                    malformed += 1
                    summaries.malformed_line()
                    prior_high_water = (
                        segment,
                        offset + len(raw) + 1,
                    )
                    continue
                prefix["decodes"] += 1
                # `_capture_protocol_prefix_evidence` returns immediately for
                # anything that is not an op, so this guard changes nothing it
                # does — it moves the phase attribution's two clock reads from
                # every record to every op. That is 195 ops against 1,954,007
                # lines on a production journal, where the phase itself measures
                # zero because the journal carries no resolution operation.
                if rec.get("t") == "op":
                    evidence_started = time.monotonic()
                    _capture_protocol_prefix_evidence(
                        rec,
                        prior_high_water,
                        protocol_evidence,
                        # OPTIMISTIC ELISION, RE-READ FALLBACK (#496 S5b §5.1).
                        # `PrefixHashAccumulator` absorbs completed segments
                        # into ONE sequential sha256 and `hashlib` can neither
                        # export nor restore midstate, so a digest over a prefix
                        # containing an elided segment is not computable from
                        # the bytes this pass read. Handing `None` routes the
                        # digest to `journal_prefix_hash`, which re-reads the
                        # prefix from disk — including the elided segments — and
                        # produces the byte-identical durable digest. The op's
                        # own claimed hash is never accepted without this
                        # recomputation.
                        hasher=None if elision.elided else hasher,
                    )
                    evidence_seconds += time.monotonic() - evidence_started
                    if (isinstance(rec.get("payload"), dict)
                            and rec["payload"].get("kind")
                            == _lib_journal._PROTOCOL_RESOLUTION_KIND):
                        # Nothing further is elided in this pass. The
                        # accumulator stays non-composable for the rest of it —
                        # a gap already read is still a gap — so every later
                        # evidence point also recomputes from disk; what this
                        # stops is a NEW gap opening after an op that will
                        # certainly be followed by more evidence points.
                        elision.resolution_seen = True
                # First cutover op wins, exactly as `find_accounts_cutover_op`
                # scans — captured here so the rebuild reads the journal once.
                if (cutover_captured is _CUTOVER_UNSEEN
                        and rec.get("id") == CUTOVER_OP_ID):
                    cutover_captured = _cutover_value_of(rec)
                # Folded into the OPEN SEGMENT's accumulator, which
                # `_SegmentSummaryCollector.close` merges into `last_seen` at the
                # boundary. The merge is a per-key maximum, so the resolved map
                # is identical to a single whole-pass fold (#496 S5b §5.4).
                summaries.last_seen.observe(rec, classify_legacy_provider)
                if rec.get("t") == "correction_batch":
                    marker_coordinates[len(decoded)] = (
                        segment,
                        offset + len(raw) + 1,
                    )
                retained = (
                    rec.get("t") in _lib_journal_router.RETAINED_RECORD_TYPES)
                summaries.decoded(retained)
                if retained:
                    decoded.append(rec)
                else:
                    if update_quota_cache and _is_codex_quota_obs(rec):
                        quota_raw.append(raw)
                    # A PLACEHOLDER, not a dropped element. `resolve_effective_events`
                    # numbers candidates with `enumerate(records)`, and three of the
                    # seven structural violation kinds put that number inside
                    # `ProtocolViolation.evidence` — which the fingerprint hashes.
                    # That fingerprint is durable: it lands in
                    # `journal_protocol_violations` and is referenced by name from a
                    # `journal_protocol_resolution` op, which `_cctally_journal_repair`
                    # mints from the UNFILTERED record list. Renumbering here would
                    # therefore make a previously acknowledged violation unresolvable
                    # and raise on every later rebuild. The selector skips a non-dict
                    # element, so this costs one pointer and keeps every sequence
                    # identical to the pre-change numbering (#496 S4; corrects §4.7).
                    decoded.append(None)
                prior_high_water = (
                    segment,
                    offset + len(raw) + 1,
                )
        summaries.close(last_seen)
        # §6.3 "recorded, not silent": every elision refusal is a SILENT
        # fallback, and this block is the only surface that says which one.
        traversal["elision"] = elision.counters()
        phase_seconds["journal_read_decode"] = round(
            max(0.0, time.monotonic() - read_started - evidence_seconds), 6)
        phase_seconds["protocol_evidence"] = round(evidence_seconds, 6)
        traversal["protocol_evidence"]["bytes"] = hasher.bytes_hashed
        traversal["protocol_evidence"]["lines"] = hasher.digests_computed
        hasher = None
        # The sidecar is a pure re-derivable cache, so it is refreshed on the
        # way past rather than guarded by anything: this pass just pinned every
        # extent it describes, and `write_sidecar` swallows its own I/O failures
        # because a pass that could not write it is still a correct pass.
        if summaries.summaries:
            _lib_segment_summary.write_sidecar(
                segment_summary_sidecar_path(),
                [summaries.summaries[name] for name in segments
                 if name in summaries.summaries],
            )

        # Legacy account normalisation (#341, spec §2 / handoff item 2): a
        # pre-#341 real-account line lacks an account stamp — inject the cutover
        # mapping BEFORE the fold (Claude legacy -> the cutover op's account;
        # Codex legacy -> unattributed). `*`-families + already-stamped lines are
        # untouched. Resolved from the journal's own cutover op — inline when the
        # streamed prefix contained it, otherwise from the unvisited suffix alone
        # (falls back to `unattributed` when neither has it), so a fresh
        # single-account rebuild is byte-neutral.
        cutover_started = time.monotonic()
        cutover_claude = _resolve_cutover_for_rebuild(
            cutover_captured, hw, all_segments, traversal["cutover_suffix"])
        phase_seconds["cutover_suffix"] = round(
            time.monotonic() - cutover_started, 6)
        for rec in decoded:
            if rec is not None:
                _normalize_legacy_account_stamp(rec, cutover_claude)

        # Resolve corrections BEFORE either disposable index is mutated. A
        # malformed revision, divergent same-revision candidate, or invalid
        # committed manifest leaves the existing destination untouched.
        selection_started = time.monotonic()
        selector_accumulators: dict = {}
        effective = _lib_journal.resolve_effective_events(
            decoded,
            protocol_prefix_evidence=protocol_evidence,
            accumulators=selector_accumulators,
        )
        # Durable selector state comes from THIS pinned traversal (#496 S5b
        # §3.3), so publication carries the fold and the index content together
        # and nothing has to read the journal a second time to derive it. The
        # generation identity is deliberately absent here: `stats_publication_
        # stamp` is written after the scratch is built, so a row populated now
        # cannot carry the identity it will publish under.
        selector_rows = _lib_selector_state.rows_from_selection(
            effective,
            accumulators=selector_accumulators,
            next_sequence=len(decoded),
            coordinates=marker_coordinates,
            covered=hw,
            cutover_seen=cutover_captured is not _CUTOVER_UNSEEN,
            cutover_account_key=(
                None if cutover_captured is _CUTOVER_UNSEEN
                else cutover_captured
            ),
        )
        phase_seconds["effective_selection"] = round(
            time.monotonic() - selection_started, 6)

        # Cache leg BEFORE any stats txn (provider-flock lock-order): journal
        # Codex quota obs -> cache.db quota_window_snapshots. The retained bytes
        # are decoded inside the leg's existing transaction and freed here, so
        # the SQLite fold never runs on top of them.
        leg_started = time.monotonic()
        if update_quota_cache:
            quota_lock_hold = _rebuild_quota_cache_leg_raw(
                quota_raw, decoded, cutover_claude, traversal["quota_replay"],
                high_water=hw, coverage=quota_coverage,
                decoded_end=prior_high_water, snapshot_out=quota_snapshot,
                elision_gaps=elision.quota_gaps)
        quota_raw = []
        phase_seconds["quota_cache_leg"] = round(
            time.monotonic() - leg_started, 6)

        # One ordered fold stream: op-folds (order 5) + evts, keyed by
        # (fold_order, canonical seq) so referenced families resolve before
        # referencing ones and crash-replay duplicates fold idempotently.
        stream: list = []
        for seq, rec in enumerate(decoded):
            if rec is None:
                continue
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
        fold_started = time.monotonic()

        # Phase 1 (txn A) — structural folds: op floors, snapshot_accept, cost
        # snapshots, resets+suppression, block_close, arming, credit effects.
        conn.execute("BEGIN IMMEDIATE")
        try:
            _write_selector_state(conn, selector_rows)
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
        # Reported separately from `stats_fold` because this span is PART of how
        # long the retained cache read snapshot stays open past the point it
        # could have been consumed — see the WAL-pinning note in
        # docs/journal-gotchas.md. Measured at 0.56 s over a 1.6 GB journal, and
        # that figure is a LOWER BOUND on the pin, not the pin itself: this
        # timer starts at `fold_started`, which is set after the fold stream is
        # built and sorted above, and the snapshot opened earlier still inside
        # `_rebuild_quota_cache_leg_raw`. The full pin is the leg's post-snapshot
        # span plus the stream build and sort plus this fold. `quota_cache_leg`
        # is reported separately, so an operator can bound the first term.
        phase_seconds["structural_fold"] = round(
            time.monotonic() - fold_started, 6)

        # §4.4's bundle is read from the retained snapshot here, BEFORE phase 2a
        # and before the stats transaction, so the cache read transaction closes
        # as early as it can rather than being held across a stats write. An
        # absent or unreadable snapshot leaves the bundle None, and the
        # projection reads its own connection exactly as it did before.
        #
        # An open WAL read transaction holds a read mark, so
        # `wal_checkpoint(TRUNCATE)` from any other process returns busy for as
        # long as this snapshot lives — which disables both of issue #297's
        # persistent defences. Reading it here rather than after phase 2a is
        # what bounds that window: measured on a 1.6 GB journal, the structural
        # fold above is 0.56 s and the open-block projection below is 27.7 s, so
        # this placement removes about 98% of the pin. The RELATIVE claim is
        # what those two figures support; neither is the absolute pin, because
        # the snapshot opens inside the quota cache leg and both timers start
        # later (see the note on `structural_fold` above). It cannot move any
        # earlier without holding the bundle's 254 MiB of observations (measured
        # on the same store, 232,466 rows) across the structural fold as well.
        quota_bundle = _read_quota_projection_bundle(quota_snapshot)

        # Phase 2a — OPEN 5h block projection (own txn; block-only). Closed blocks
        # came from block_close evts; this materializes the never-closed window(s)
        # so the five_hour_milestone block_id derived_fk resolves. Best-effort
        # (the open block is a projection, §5.3).
        projection_started = time.monotonic()
        try:
            cctally = sys.modules.get("cctally")
            bf = getattr(cctally, "_backfill_five_hour_blocks", None)
            if bf is not None:
                bf(conn, only_missing=True)
        except Exception as exc:  # pragma: no cover — projection is best-effort
            print(f"[rebuild] open 5h block re-materialization failed: {exc}",
                  file=sys.stderr)
        phase_seconds["open_block_projection"] = round(
            time.monotonic() - projection_started, 6)

        # Phase 2b + 3 (txn B) — quota projection re-materialization (after the
        # order-45 arming folds) + milestone/budget folds + cursor advance.
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                import _cctally_quota as _q
                _q.rematerialize_quota_projection_for_rebuild(
                    conn, bundle=quota_bundle)
            except Exception as exc:  # pragma: no cover — projection best-effort
                print(f"[rebuild] quota projection re-materialization failed: {exc}",
                      file=sys.stderr)
            # §4.7's durable, per-transaction gate. It is set in the SAME
            # transaction that materializes the projection it describes, so a
            # rollback leaves neither, and it rides into the live index with the
            # generation's own content — a process-local `RebuildResult` field
            # could not survive publication, and in-place publication
            # deliberately keeps already-open readers alive, so a connection can
            # observe the incomplete generation without ever calling `open_db`
            # again.
            _written = _write_quota_projection_state(
                conn, coverage=quota_coverage, high_water=hw)
            # `None` is "could not write the flag at all". Fail CLOSED: report
            # incomplete, so the generation is reconciled rather than served on
            # the strength of a flag nobody could store.
            stats_projection_incomplete = (
                True if _written is None else _written)
            for _order, _seq, _kind, rec in tail:
                _apply_evt(conn, rec)
                lines_folded += 1
            # Fold-time `last_seen_utc` derivation (#341): re-derive each
            # account's last-seen from the whole journal. The map was
            # accumulated during the single read pass — `decoded` no longer
            # contains observations, so deriving from it here would silently
            # drop every observation's contribution (#496 S4 §4.6).
            _apply_account_last_seen(
                conn,
                last_seen.resolve(cutover_claude, _lib_accounts.UNATTRIBUTED),
            )
            if hw is not None:
                _write_cursor(conn, hw[0], hw[1])
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        phase_seconds["stats_fold"] = round(time.monotonic() - fold_started, 6)

        validate_started = time.monotonic()
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
        # The SEMANTIC half (#496 S5b §6.2). The structural checks above cover
        # the new tables' existence and definition; without this, a scratch
        # carrying correct legacy rows and WRONG selector state would validate,
        # receive a matching generation stamp, and then be trusted by the fast
        # path — which is the precise failure shape epic #496 exists to
        # eliminate, reintroduced one layer up. `selector_rows` is the full
        # selection this pass already derived from the pinned traversal, so the
        # comparison costs no second journal read.
        _validate_selector_state(conn, selector_rows)
        _stats_rebuild_test_pause("rebuild_scratch_complete")
        phase_seconds["scratch_validate"] = round(
            time.monotonic() - validate_started, 6)
    finally:
        conn.close()
        # A retained coverage snapshot holds an open read transaction on
        # `cache.db`, which pins the WAL against checkpointing for as long as it
        # lives. `_read_quota_projection_bundle` closes it on every ordinary
        # path; this covers the paths that raise before reaching it, which
        # matters most in a long-lived process such as the dashboard's auto-heal
        # thread. Closing twice is harmless.
        for _retained in quota_snapshot:
            _close_coverage_snapshot(_retained)
        quota_snapshot = []

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
    # The pre-publication window is what F9's memory acceptance is measured
    # over: everything after this point is publication, whose own WAL cost S3
    # already accounts for.
    peak_heap_bytes = (
        tracemalloc.get_traced_memory()[1] if tracing else 0)
    decoded = effective = stream = structural = tail = None
    segments = protocol_evidence = last_seen = None

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

    publication_started = time.monotonic()
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
            "sqliteRuntimeVersion": sqlite3.sqlite_version,
            "highWater": [hw[0], hw[1]] if hw is not None else None,
            "destination": str(dest),
            "targetPath": str(target_path) if target_path is not None else None,
            "segmentsRead": segments_read,
            "linesFolded": lines_folded,
            "malformed": malformed,
            "rowsByTable": rows_by_table,
            "buildSeconds": round(time.monotonic() - start, 3),
            "prePublicationValidation": {"ok": True, "error": None},
            # Additive instrumentation (#496 S4 §8.7). Additive keys do not bump
            # `schemaVersion` and no existing field changes meaning. `publication`
            # is absent HERE and present on `RebuildResult`: publication copies
            # this dict before it writes it, so its own duration cannot be known
            # at the time the record is written.
            "phaseSeconds": dict(phase_seconds),
            "traversal": {
                name: dict(counts) for name, counts in traversal.items()
            },
            "peakHeapBytes": peak_heap_bytes,
            "quotaLockHoldSeconds": round(quota_lock_hold, 6),
            "selectorDesynchronized": selector_desync,
            "quotaCacheCoverage": quota_coverage,
            # §4.7: distinct from `quotaCacheCoverage`, which describes the
            # CACHE. This describes the PUBLISHED INDEX, and a consumer must not
            # read one as the other. The record is written before publication
            # returns, so a crash afterwards still leaves the remainder
            # discoverable.
            "statsQuotaProjectionIncomplete": stats_projection_incomplete,
        },
    )
    phase_seconds["publication"] = round(
        time.monotonic() - publication_started, 6)

    return RebuildResult(
        rows_by_table=rows_by_table, malformed=malformed,
        duration_s=time.monotonic() - start, segments_read=segments_read,
        lines_folded=lines_folded, conflicts=conflicts,
        protocol_violations=protocol_violations,
        acknowledged_protocol_violations=acknowledged,
        quarantine_dir=incident,
        phase_seconds=phase_seconds,
        traversal=traversal,
        peak_heap_bytes=peak_heap_bytes,
        quota_lock_hold_seconds=round(quota_lock_hold, 6),
        selector_desynchronized=selector_desync,
        quota_cache_coverage=quota_coverage,
        stats_quota_projection_incomplete=stats_projection_incomplete,
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


def _encode_bootstrap_lines(lines: list) -> bytes:
    """The cutover export as one verified blob (spec §8 verify step).

    Encoding is separated from writing because `run_cutover` digests the blob
    before it decides whether a byte-identical segment already exists (#496 S5
    §3). Encoding twice would compute the reuse digest over a different object
    than the one written, so this is the single encode both uses.
    """
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
    return blob


def _reusable_bootstrap(candidate_digest: str, candidate_size: int):
    """`(name, size)` of a published bootstrap identical to the candidate blob.

    Every published bootstrap is REPORTED, because `reusable_bootstrap_name`
    refuses a match that is not the canonically newest one; only segments whose
    byte length already equals the candidate's are READ, so the comparison costs
    one pass over the same-size candidates rather than one over the journal.
    `list_segments` excludes `.partial` files, so a cutover that is still writing
    can never be reused (#496 S5 §3). A segment whose length cannot be read is
    reported with a `None` length rather than dropped, which refuses reuse
    instead of promoting an older segment to canonically newest.
    """
    journal_dir = _cctally_core.JOURNAL_DIR
    if not journal_dir.exists():
        return None
    existing = []
    for name in list_segments():
        if not name.startswith(_lib_journal.BOOTSTRAP_PREFIX):
            continue
        path = journal_dir / name
        try:
            size = os.path.getsize(path)
        except OSError:
            # Report it anyway. Dropping the entry would let an OLDER match
            # look canonically newest, which is the reuse this scan refuses.
            existing.append((name, None, None))
            continue
        if size != candidate_size:
            existing.append((name, size, None))
            continue
        digest = hashlib.sha256()
        with _open_segment_for_read(path) as handle:
            while True:
                chunk = handle.read(_SEGMENT_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        existing.append((name, size, digest.hexdigest()))
    name = _lib_journal.reusable_bootstrap_name(
        candidate_digest, candidate_size, existing)
    return None if name is None else (name, candidate_size)


def _fsync_published_segment(seg_name: str) -> None:
    """Make an already-renamed segment and its directory entry durable.

    `_write_bootstrap_segment` establishes this for a segment it writes itself.
    A reused segment was published by a different attempt, which may have
    crashed anywhere in that sequence, so the reuse path repeats the file and
    directory fsyncs before anything durable is allowed to name the file.
    """
    journal_dir = _cctally_core.JOURNAL_DIR
    fd = os.open(str(journal_dir / seg_name), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(journal_dir)


def _write_bootstrap_segment(seg_name: str, blob: bytes) -> int:
    """Materialize the bootstrap segment atomically (spec §8 rename-then-stamp):
    write the verified blob to a `.partial` sibling, fsync file + dir, then
    `os.replace` into `seg_name`. Returns the final byte size."""
    journal_dir = _cctally_core.JOURNAL_DIR
    dir_created = not journal_dir.exists()
    journal_dir.mkdir(parents=True, exist_ok=True)
    if dir_created:
        try:
            os.chmod(journal_dir, 0o700)
        except OSError:
            pass
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
    bootstrap segment basename, or None when nothing was exported.

    A crash between the `os.replace` and the commit leaves a published segment
    the rolled-back transaction never referenced. The retry re-exports the same
    rows byte for byte, so it reuses that orphan rather than publishing a twin
    (#496 S5 §3) — the retry is now idempotent on disk, not only on fold. When
    the retry's export genuinely differs, no digest matches and a new segment is
    written exactly as before."""
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

        blob = _encode_bootstrap_lines(lines)
        reuse = _reusable_bootstrap(
            hashlib.sha256(blob).hexdigest(), len(blob))
        if reuse is None:
            seg_name = _cutover_segment_name(now_utc)
            seg_size = _write_bootstrap_segment(seg_name, blob)
        else:
            seg_name, seg_size = reuse
            # The adopted segment was renamed by ANOTHER attempt, whose rename
            # may still be only in the page cache. The cursor stamped below is
            # made durable by SQLite's own commit fsync, so without this the
            # index could name a bootstrap that a power loss then leaves absent.
            _fsync_published_segment(seg_name)

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
    appended yet. Cheap enough for the one-time transition + the retry check.

    Streams rather than materializing (#496 S4): the previous form built each
    segment's whole line list before its first-match return, so the early exit
    could not stop reading inside the containing segment. The None-on-absence
    contract is UNCHANGED — the cache and conversations migrations depend on it
    to defer their backfill.
    """
    for seg in list_segments():
        seg_path = _cctally_core.JOURNAL_DIR / seg
        try:
            size = os.path.getsize(seg_path)
        except OSError:
            continue
        for _name, _off, raw in _iter_segment_lines(seg_path, 0, size):
            rec = _lib_journal.decode_line(raw)
            if rec is not None and rec.get("id") == CUTOVER_OP_ID:
                return _cutover_value_of(rec)
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
