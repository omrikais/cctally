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
import os
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass, field

import _cctally_core
import _lib_journal
import _lib_record


# Torn-tail scan window (spec §4.3). A single line must fit inside it — a
# longer line is a hard error by construction, which is what keeps torn-tail
# repair bounded (the previous newline is guaranteed to fall inside the window).
_TAIL_WINDOW = 64 * 1024
_MAX_LINE_BYTES = _TAIL_WINDOW


class JournalError(Exception):
    """A structural journal-append failure (line too long, unrepairable tail)."""


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

def append_record(record: dict, *, now_utc: dt.datetime | None = None) -> tuple[str, int]:
    """Append one encoded line to the current UTC month's segment.

    Implements spec §4.3 exactly: ``O_RDWR|O_APPEND|O_CREAT`` (0o600) → blocking
    leaf flock → torn-tail repair → loop-write the full line → ``fsync(fd)`` →
    parent-dir fsync on first segment/dir creation → unlock.

    ``now_utc`` selects the segment (defaults to the current UTC time). Returns
    ``(segment_basename, end_offset)`` where ``end_offset`` is the file size
    just past the appended line — the byte position the ingest cursor advances
    to when it consumes this line."""
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
        # Durably record new directory entries (spec §4.3: fsync the parent
        # directory on first creation of a segment or the journal dir).
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


def journal_high_water() -> tuple[str, int] | None:
    """Snapshot ``(latest segment basename, size)`` under a µs leaf-lock hold.

    "Latest" is the canonically-last segment (spec §4.1 order). The ingest
    cycle takes this snapshot and consumes only ``cursor → HW`` so a line
    appended after the snapshot belongs to the next cycle (spec §5.2.1).
    Returns ``None`` when no segment exists yet."""
    lock_fd = _acquire_leaf_lock()
    try:
        segments = list_segments()
        if not segments:
            return None
        latest = segments[-1]
        size = os.stat(_cctally_core.JOURNAL_DIR / latest).st_size
        return (latest, size)
    finally:
        _release_leaf_lock(lock_fd)


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
#   3. cache leg (Codex quota) BEFORE the stats txn.        QUOTA_APPLIER seam
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
#   QUOTA_APPLIER    the Codex quota cache leg (Task 7; wired to `_quota_applier`
#                    below). Contract: (decoded) -> stop_index | None. `decoded`
#                    is the ordered list of (record, segment, offset); a non-None
#                    int is a prefix-stop boundary (busy `cache.db.codex.lock` at
#                    a quota line): the cycle processes decoded[:stop] and
#                    advances the cursor to decoded[stop]'s offset (spec §5.2
#                    step 3). Always-on: a Claude-only batch scans + returns None.
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

    def as_of_for(self, record: dict) -> str:
        return record["at"]


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


# --------------------------------------------------------------------------
# cursor (spec §5.2.2: segment-aware, prior-month tails covered)
# --------------------------------------------------------------------------

def _read_cursor(conn: sqlite3.Connection) -> tuple[str, int] | None:
    """Return `(segment_basename, offset)` from `journal_cursor`, or None when
    nothing has been consumed yet (start of the first segment)."""
    row = conn.execute(
        "SELECT segment, offset FROM journal_cursor WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return (row[0], int(row[1]))


def _write_cursor(conn: sqlite3.Connection, segment: str, offset: int) -> None:
    conn.execute(
        "INSERT INTO journal_cursor (id, segment, offset) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET segment = excluded.segment, "
        "offset = excluded.offset",
        (segment, offset),
    )


def _read_segment_lines(seg_path, lo: int, hi: int) -> list[tuple[str, int, bytes]]:
    """`(basename, absolute-offset, raw-line-without-newline)` for every
    complete line in `[lo, hi)`. `hi` is a line boundary (a HW snapshot size or
    an immutable prior segment's full size), so no partial trailing line
    appears."""
    with open(seg_path, "rb") as fh:
        fh.seek(lo)
        data = fh.read(hi - lo)
    out = []
    start = 0
    while True:
        nl = data.find(b"\n", start)
        if nl == -1:
            break
        out.append((seg_path.name, lo + start, data[start:nl]))
        start = nl + 1
    return out


def _read_range(cursor, hw) -> list[tuple[str, int, bytes]]:
    """Read `cursor -> HW` across segments in canonical order (spec §5.2.2).

    Prior segments (before HW's) are immutable and read to their full size;
    HW's segment is read only up to the snapshot size, so appends past HW
    belong to the next cycle.
    """
    hw_seg, hw_size = hw
    segments = list_segments()
    if hw_seg not in segments:
        return []
    hw_idx = segments.index(hw_seg)
    if cursor is None:
        start_idx, start_off = 0, 0
    else:
        cur_seg, cur_off = cursor
        if cur_seg in segments:
            start_idx, start_off = segments.index(cur_seg), cur_off
        else:
            start_idx, start_off = 0, 0
    lines: list[tuple[str, int, bytes]] = []
    for idx in range(start_idx, hw_idx + 1):
        seg = segments[idx]
        seg_path = _cctally_core.JOURNAL_DIR / seg
        lo = start_off if idx == start_idx else 0
        hi = hw_size if idx == hw_idx else os.path.getsize(seg_path)
        if lo >= hi:
            continue
        lines.extend(_read_segment_lines(seg_path, lo, hi))
    return lines


# --------------------------------------------------------------------------
# cache leg — Codex quota obs -> cache.db quota_window_snapshots (spec §5.2
# step 3, Task 7 Item 2). Runs BEFORE the stats BEGIN IMMEDIATE, under the
# `cache.db.codex.lock` provider flock (lock-order law: provider flocks precede
# SQLite write transactions). The journal Codex quota obs are the DURABLE truth
# (§1 latent data-loss hole — the source rollout JSONL evaporates); this leg
# re-materializes the disposable cache.db index from them, idempotently
# (INSERT OR IGNORE on the natural key). Distinct from the direct cache write in
# `sync_codex_cache._write_codex_file_batch` (kept byte-identical, Item 1) — the
# two converge on the same rows.
# --------------------------------------------------------------------------

_QUOTA_OBS_KIND = "quota_window_snapshot"

_QUOTA_SNAPSHOT_INSERT = (
    "INSERT OR IGNORE INTO quota_window_snapshots "
    "(source, source_root_key, source_path, line_offset, captured_at_utc, "
    " observed_slot, logical_limit_key, limit_id, limit_name, window_minutes, "
    " used_percent, resets_at_utc, plan_type, individual_limit_json, "
    " reached_type, observed_model) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_QUOTA_SNAPSHOT_COLS = (
    "source", "source_root_key", "source_path", "line_offset", "captured_at_utc",
    "observed_slot", "logical_limit_key", "limit_id", "limit_name",
    "window_minutes", "used_percent", "resets_at_utc", "plan_type",
    "individual_limit_json", "reached_type", "observed_model",
)


def _is_codex_quota_obs(rec: dict) -> bool:
    return (
        rec.get("t") == "obs"
        and rec.get("provider") == "codex"
        and (rec.get("payload") or {}).get("kind") == _QUOTA_OBS_KIND
    )


def _quota_applier(decoded) -> int | None:
    """Cache leg (spec §5.2 step 3): materialize this batch's Codex quota obs
    into cache.db `quota_window_snapshots`, under a NON-BLOCKING
    `cache.db.codex.lock`. Contract (journal seam): `(decoded) -> stop | None`,
    `decoded = [(record, segment, offset), ...]` in canonical order.

    - No Codex quota obs in the batch → return None (no flock taken).
    - Busy codex flock, OR a cache write it cannot complete → PREFIX-STOP:
      return the index of the FIRST codex quota obs, so the cycle processes only
      `decoded[:stop]` and advances the cursor to `decoded[stop]`'s offset,
      retrying the remainder next cycle (the scalar cursor never advances past an
      unmaterialized obs — spec §5.2 step 3).
    - Flock acquired + all obs upserted → return None (full consumption).
    """
    quota_idx = [i for i, (rec, _s, _o) in enumerate(decoded)
                 if _is_codex_quota_obs(rec)]
    if not quota_idx:
        return None
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_cctally_core.CACHE_LOCK_CODEX_PATH),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return quota_idx[0]  # busy codex flock -> prefix-stop
        try:
            cache = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH), timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover — cache.db unopenable
            print(f"[ingest] quota cache leg connect failed: {exc}", file=sys.stderr)
            return quota_idx[0]
        try:
            cache.execute("PRAGMA busy_timeout=15000")
            cache.execute("BEGIN IMMEDIATE")
            for i in quota_idx:
                p = decoded[i][0]["payload"]
                cache.execute(_QUOTA_SNAPSHOT_INSERT,
                              tuple(p.get(col) for col in _QUOTA_SNAPSHOT_COLS))
            cache.commit()
        except sqlite3.Error as exc:
            try:
                cache.rollback()
            except sqlite3.Error:
                pass
            # Could not materialize -> prefix-stop so the cursor holds and the
            # next cycle retries (the obs stay durable in the journal regardless).
            print(f"[ingest] quota cache leg write failed: {exc}", file=sys.stderr)
            return quota_idx[0]
        finally:
            cache.close()
        return None
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# Wire the seam (declared None near the top as the contract stub). Always-on:
# a Claude-only cycle's scan finds no Codex quota obs and returns None before
# any flock/DB touch, so the cost is one list comprehension over the batch.
QUOTA_APPLIER = _quota_applier


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


def _insert_or_ignore(conn: sqlite3.Connection, table: str, cols: dict):
    keys = list(cols.keys())
    colnames = ", ".join(keys)
    placeholders = ", ".join("?" for _ in keys)
    return conn.execute(
        f"INSERT OR IGNORE INTO {table} ({colnames}) VALUES ({placeholders})",
        tuple(cols[k] for k in keys),
    )


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

    clamp_floor_iso = _cctally_core._reset_aware_floor(
        conn, week_start_date, week_start_at, week_end_at
    ) or "1970-01-01T00:00:00Z"
    max_row = conn.execute(
        "SELECT MAX(weekly_percent) FROM weekly_usage_snapshots "
        "WHERE week_start_date = ? AND unixepoch(captured_at_utc) >= unixepoch(?)",
        (week_start_date, clamp_floor_iso),
    ).fetchone()
    max_v = max_row[0] if max_row else None
    if _lib_record.hwm_clamp_applies(weekly_percent, max_v):
        return True, five_hour_percent

    adjusted_5h = five_hour_percent
    if five_hour_percent is not None and five_hour_window_key is not None:
        max_5h_row = conn.execute(
            "SELECT MAX(five_hour_percent) FROM weekly_usage_snapshots "
            "WHERE five_hour_window_key = ? "
            "  AND unixepoch(captured_at_utc) >= unixepoch(COALESCE("
            "        (SELECT effective_reset_at_utc FROM five_hour_reset_events "
            "          WHERE five_hour_window_key = ? ORDER BY id DESC LIMIT 1),"
            "        '1970-01-01T00:00:00Z'))",
            (int(five_hour_window_key), int(five_hour_window_key)),
        ).fetchone()
        max_5h = max_5h_row[0] if max_5h_row else None
        if _lib_record.hwm_clamp_applies(float(five_hour_percent), max_5h):
            adjusted_5h = float(max_5h)

    last = conn.execute(
        "SELECT weekly_percent, five_hour_percent FROM weekly_usage_snapshots "
        "WHERE week_start_date = ? ORDER BY captured_at_utc DESC, id DESC LIMIT 1",
        (week_start_date,),
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
    })


# Obs/op fold registry (spec §5.3 "fold op"). Keyed by `payload.kind`; the
# built-in `_pipeline_op_fold` pipeline hook dispatches through it. 6b may
# register more op folds; obs no longer fold directly (see the NOTE above).
FOLD_APPLIERS = {"weekly_credit_floor": _apply_op_weekly_credit_floor}


# --------------------------------------------------------------------------
# evt fold appliers (step 4a replay + the emit_model_a apply path)
# --------------------------------------------------------------------------

_BLOCK_CHILDREN = (
    ("_models", "five_hour_block_models"),
    ("_projects", "five_hour_block_projects"),
)
_BLOCK_CHILD_KEYS = frozenset(k for k, _t in _BLOCK_CHILDREN)


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
    # since the open block is a projection with no logical id). 0 when absent.
    for column, (ref_table, lookup_col) in spec.derived_fk.items():
        row = conn.execute(
            f"SELECT id FROM {ref_table} WHERE {lookup_col} = ?",
            (cols.get(lookup_col),),
        ).fetchone()
        cols[column] = int(row[0]) if row is not None else 0
    return _insert_or_ignore(conn, spec.table, cols)


def _apply_weekly_credit_effects(conn, evt):
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
    if floor:
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
    reconcile honors it (no historical re-fires). Applied as an UPSERT on the
    arming natural key, in canonical order, so the latest state per key wins and
    re-applying an already-present evt is a clean no-op. `quota_alert_arming` has
    no `journal_id` column (it is not in the Task-4 additive list); idempotence
    is the natural-key upsert, not a journal_id INSERT OR IGNORE."""
    p = evt.get("payload") or {}
    conn.execute(
        "INSERT INTO quota_alert_arming "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        " window_minutes, rule_fingerprint, activated_at_utc) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(source, source_root_key, logical_limit_key, observed_slot, "
        "            window_minutes) DO UPDATE SET "
        "  rule_fingerprint=excluded.rule_fingerprint, "
        "  activated_at_utc=excluded.activated_at_utc",
        (p.get("source"), p.get("source_root_key"), p.get("logical_limit_key"),
         p.get("observed_slot"), p.get("window_minutes"),
         p.get("rule_fingerprint"), p.get("activated_at_utc")),
    )
    return None


def _apply_block_close(conn, evt):
    """Fold a `five_hour_block_close` evt (spec §5.3): insert the frozen parent
    block (`INSERT OR IGNORE` on window-key / journal_id), then its embedded
    `_models`/`_projects` rollup children under the resolved parent block_id
    (each idempotent on its own natural key). Live harvest only STAMPS the
    parent's journal_id — 6b's close hook already wrote parent+children — so this
    insert path runs for replay/rebuild."""
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
    prow = conn.execute(
        "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ?",
        (parent.get("five_hour_window_key"),),
    ).fetchone()
    if prow is None:
        return None
    block_id = int(prow[0])
    for payload_key, child_table in _BLOCK_CHILDREN:
        for child in children.get(payload_key, []):
            cols = dict(child)
            cols["block_id"] = block_id
            _insert_or_ignore(conn, child_table, cols)
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


def _apply_evt(conn, evt):
    """Dispatch one evt line to its fold applier by `payload.kind` (step 4a
    replay + the emit_model_a apply path). A kind with a bespoke `applier`
    (weekly_credit_effects, five_hour_block_close) uses it; everything else
    goes through the generic column-map fold. Apply-only: NO alert dispatch,
    NO ctx — replay is structurally unable to fire alerts (spec §5.2 step 4a)."""
    spec = _EVT_SPECS.get((evt.get("payload") or {}).get("kind"))
    if spec is not None and spec.applier is not None:
        return spec.applier(conn, evt)
    return _apply_generic_evt(conn, evt)


# --------------------------------------------------------------------------
# harvest registry (natural-keyed families, spec §5.3)
# --------------------------------------------------------------------------

_HARVEST_SPECS = [
    _HarvestSpec(
        "week_reset_events", "week_reset", "wr",
        id_parts=("old_week_end_at", "new_week_end_at"),
        at_column="detected_at_utc", order=30, suppression=True,
    ),
    _HarvestSpec(
        "five_hour_reset_events", "five_hour_credit", "fhc",
        id_parts=("five_hour_window_key", "effective_reset_at_utc"),
        at_column="detected_at_utc", order=30, suppression=True,
    ),
    _HarvestSpec(
        "five_hour_blocks", "five_hour_block_close", "fhbc",
        id_parts=("five_hour_window_key",),
        at_column="last_updated_at_utc", order=40, closed_only=True,
        children=_BLOCK_CHILDREN,
    ),
    _HarvestSpec(
        "percent_milestones", "percent_milestone", "pm",
        id_parts=("week_start_date", "reset_event_id", "percent_threshold"),
        fk_refs={
            "usage_snapshot_id": ("weekly_usage_snapshots", "usage_snapshot_ref"),
            "cost_snapshot_id": ("weekly_cost_snapshots", "cost_snapshot_ref"),
            "reset_event_id": ("week_reset_events", "reset_event_ref"),
        },
        at_column="captured_at_utc", order=60,
    ),
    _HarvestSpec(
        "five_hour_milestones", "five_hour_milestone", "fhm",
        id_parts=("five_hour_window_key", "reset_event_id", "percent_threshold"),
        fk_refs={
            "usage_snapshot_id": ("weekly_usage_snapshots", "usage_snapshot_ref"),
            "reset_event_id": ("five_hour_reset_events", "reset_event_ref"),
        },
        # block_id points at the OPEN five_hour_blocks row (a projection with no
        # journal_id) — re-derive it at fold from the journaled window key
        # instead of a broken logical FK (harvest-order violation otherwise).
        derived_fk={"block_id": ("five_hour_blocks", "five_hour_window_key")},
        at_column="captured_at_utc", order=60,
    ),
    _HarvestSpec(
        "budget_milestones", "budget", "bm",
        id_parts=("vendor", "period_start_at", "period", "threshold"),
        at_column="crossed_at_utc", order=60,
    ),
    _HarvestSpec(
        "projected_milestones", "projected", "pjm",
        id_parts=("week_start_at", "period", "metric", "threshold"),
        at_column="crossed_at_utc", order=60,
    ),
    _HarvestSpec(
        "project_budget_milestones", "project_budget", "pbm",
        id_parts=("week_start_at", "project_key", "threshold"),
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
    append_record(evt)
    ctx.events_emitted += 1
    _apply_evt(ctx.conn, evt)
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
            evt = _build_harvest_evt(ctx, spec, row)
            append_record(evt)
            ctx.events_emitted += 1
            conn.execute(
                f"UPDATE {spec.table} SET journal_id = ? WHERE id = ?",
                (evt["id"], row["id"]),
            )


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
        # QUOTA_APPLIER attempts the cache.db.codex.lock NB upsert; on a busy
        # flock it returns a prefix-stop index — the cycle processes
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
    journal_evts = [r for r in records if r.get("t") == "evt"]

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
                        events_emitted=ctx.events_emitted, alerts=alerts)


def run_stats_ingest(*, mode: str = "opportunistic", timeout_s: float = 10.0,
                     conn: sqlite3.Connection | None = None,
                     reconcile_config=None, codex_apply=None,
                     post_commit=None) -> IngestResult:
    """Run one ingest cycle as the single-flight stats.db writer (spec §5.1/§5.2).

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
    lock_fd = _acquire_ingest_lock(mode, timeout_s)
    if lock_fd is None:
        return IngestResult(ran=False, consumed=0, malformed=0,
                            events_emitted=0, alerts=[])
    own_conn = conn is None
    try:
        if own_conn:
            conn = _cctally_core.open_db()
        try:
            return _run_cycle(conn, reconcile_config=reconcile_config,
                              codex_apply=codex_apply, post_commit=post_commit)
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
        _release_ingest_lock(lock_fd)


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
    "quota_percent_milestones", "quota_threshold_events",
)


@dataclass
class RebuildResult:
    """Outcome of a `rebuild_stats_index` call (spec §5.4)."""

    rows_by_table: dict       # journal-covered table -> row count in the rebuild
    malformed: int            # journal lines that failed to decode (spec §4.4)
    duration_s: float         # wall time of the whole rebuild
    segments_read: int        # journal segments folded
    lines_folded: int         # op + evt lines applied (obs are rederive input)


def _remove_db_sidecars(path) -> None:
    for suffix in ("-wal", "-shm"):
        try:
            pathlib.Path(str(path) + suffix).unlink()
        except OSError:
            pass


def _remove_db_family(path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            pathlib.Path(str(path) + suffix).unlink()
        except OSError:
            pass


def _rebuild_quota_cache_leg(records) -> None:
    """Re-materialize cache.db `quota_window_snapshots` from the journal's Codex
    quota obs (spec §5.4). The journal obs are the DURABLE source (§1 latent
    data-loss hole — the rollout JSONL evaporates); this INSERT OR IGNOREs them
    on the natural key, mirroring `_quota_applier`. Runs BEFORE any stats
    transaction, under the `cache.db.codex.lock` provider flock (lock-order law).
    Best-effort: a missing/busy cache.db is a clean skip (the obs stay durable in
    the journal; the stats quota projection pass then degrades cleanly)."""
    quota_obs = [r for r in records if _is_codex_quota_obs(r)]
    if not quota_obs:
        return
    cache_path = _cctally_core.CACHE_DB_PATH
    if not cache_path.exists():
        return
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_cctally_core.CACHE_LOCK_CODEX_PATH),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            cache = sqlite3.connect(str(cache_path), timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover — cache.db unopenable
            print(f"[rebuild] quota cache leg connect failed: {exc}", file=sys.stderr)
            return
        try:
            cache.execute("PRAGMA busy_timeout=15000")
            cache.execute("BEGIN IMMEDIATE")
            for r in quota_obs:
                p = r["payload"]
                cache.execute(_QUOTA_SNAPSHOT_INSERT,
                              tuple(p.get(col) for col in _QUOTA_SNAPSHOT_COLS))
            cache.commit()
        except sqlite3.Error as exc:
            try:
                cache.rollback()
            except sqlite3.Error:
                pass
            print(f"[rebuild] quota cache leg write failed: {exc}", file=sys.stderr)
        finally:
            cache.close()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def rebuild_stats_index(*, target_path=None) -> RebuildResult:
    """Build a FRESH stats index from the journal alone (spec §5.4).

    Replays every segment in canonical `(segment, offset)` order into a fresh
    schema'd DB at a scratch sibling of the destination, then ATOMICALLY swaps it
    in — crash-safe (a mid-fold crash leaves only a discardable scratch). Folds
    are apply-only: the PIPELINE never runs, so no Model-A emission, no harvest,
    no alerts, no `reconcile_config` (see the module note above). Post-rebuild the
    cursor equals the journal high-water.

    `target_path` selects the destination (default `DB_PATH`). The caller
    (auto-heal HEAL_HOOK / `db rebuild`) forensics-quarantines the damaged/old DB
    FIRST, so the destination is absent at swap time; a `target_path` build (used
    by determinism tests) writes an independent index without touching `DB_PATH`.
    """
    start = time.monotonic()
    dest = (pathlib.Path(target_path) if target_path is not None
            else pathlib.Path(_cctally_core.DB_PATH))

    # HW snapshot at the START — lines appended during the rebuild are past HW
    # and belong to the next ingest cycle (they replay idempotently); mirrors the
    # live cycle's §5.2.1 HW-prefix rule.
    hw = journal_high_water()
    segments = list_segments()

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
        if hw is not None:
            for _seg, _off, raw in _read_range(None, hw):
                rec = _lib_journal.decode_line(raw)
                if rec is None:
                    malformed += 1
                    continue
                decoded.append(rec)

        # Cache leg BEFORE any stats txn (provider-flock lock-order): journal
        # Codex quota obs -> cache.db quota_window_snapshots.
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
            elif t == "evt":
                stream.append((_fold_order(rec), seq, "evt", rec))
        stream.sort(key=lambda x: (x[0], x[1]))
        structural = [s for s in stream if s[0] < _REBUILD_MILESTONE_ORDER]
        tail = [s for s in stream if s[0] >= _REBUILD_MILESTONE_ORDER]

        # Phase 1 (txn A) — structural folds: op floors, snapshot_accept, cost
        # snapshots, resets+suppression, block_close, arming, credit effects.
        conn.execute("BEGIN IMMEDIATE")
        try:
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
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    # Atomic swap: the freshly-built scratch becomes the destination. Its WAL was
    # drained above; drop the empty sidecars, rename, and clear any stale
    # destination sidecars (a fresh open recreates its own).
    _remove_db_sidecars(scratch)
    os.replace(str(scratch), str(dest))
    _remove_db_sidecars(dest)

    return RebuildResult(
        rows_by_table=rows_by_table, malformed=malformed,
        duration_s=time.monotonic() - start, segments_read=len(segments),
        lines_folded=lines_folded,
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
# cutover does NOT take the ingest lock (open_db reaches it from INSIDE
# run_stats_ingest's own ingest-lock hold — re-acquiring would self-deadlock):
# single-flight of the STAMP is provided by `BEGIN IMMEDIATE`, and concurrent
# cutovers converge by id.


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
    # natural-key form (`<natural_key_prefix>:<col>:<col>…`) matching the LIVE
    # emission (so a cutover-exported record and a later live re-emission share
    # one id) instead of the `b:<table>:<rowid>` bootstrap id.
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
    # `qaa:` natural-key form (matching the live emission in
    # `_cctally_quota._codex_leg._emit_arming`), so a cutover-exported arming
    # record and a later live re-emission for the same identity are ONE record.
    _CutoverSpec("quota_alert_arming", "quota_alert_arming", "evt",
                 "activated_at_utc", stamp=False, natural_key_prefix="qaa",
                 natural_key_id=("source", "source_root_key", "logical_limit_key",
                                 "observed_slot", "window_minutes")),
)

# Journal-covered stats tables whose rows get a `journal_id` stamp at cutover.
# five_hour_blocks stamps only its CLOSED rows (open blocks stay NULL — they are
# re-materialized projections). `stamp=False` families (quota_alert_arming: no
# journal_id column) are excluded — they converge by natural-key upsert.
_CUTOVER_STAMP_TABLES = tuple(s.table for s in _CUTOVER_SPECS if s.stamp)


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
            if key in ("id", "journal_id") or key in spec.fk_refs \
                    or key in spec.exclude:
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
        if spec.natural_key_id:
            # §5.3 "state" family: the evt id is the natural-key form (matching
            # the live emission), NOT the b:<table>:<rowid> bootstrap id.
            bid = _lib_journal.evt_id(
                spec.natural_key_prefix, *(row[c] for c in spec.natural_key_id))
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
        cols = ", ".join(_QUOTA_SNAPSHOT_COLS)
        rows = cache.execute(
            f"SELECT id, {cols} FROM quota_window_snapshots").fetchall()
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
        at = payload.get("captured_at_utc") or _now_iso()
        rec = _lib_journal.make_obs(at=at, src="bootstrap", provider="codex",
                                    payload=payload)
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
