"""Snapshot-rebuild cache infrastructure for the dashboard/TUI (#268).

Pure, unit-testable cache primitives for the per-tick `DataSnapshot`
rebuild. The dashboard's background sync thread rebuilds the whole
snapshot every `--sync-interval`; on a large instance a from-scratch
rebuild re-aggregates the entire history each tick and pegs a CPU core.
This module holds the cheap change-signals + immutable-aggregate caches
that let the rebuild recompute only the current/dirty slice and serve
immutable past periods from memory.

Design spec: docs/superpowers/specs/2026-07-04-dashboard-rebuild-perf-design.md

What lives here (M0):
- `SnapshotSignature` + `compute_signature` — a composite data-version
  signature over EVERY table the cached surfaces read (spec §3). Cheap
  `MAX(id)` b-tree descents + a `(count, max-rowid)` change-signal over
  the reset-event tables, plus a monotonic generation counter. When the
  signature is unchanged the rebuild can take the idle path.
- `new_min_timestamp` — the per-builder timestamp watermark (spec §3,
  Codex F1): the earliest EVENT time among genuinely-new rows, so a
  late/backfilled entry (new `id`, OLD `timestamp_utc`) forces recompute
  of the affected PAST bucket, not just the current one.
- `bump_generation` / `current_generation` — a monotonic counter bumped
  by any path that deletes/rewrites history in place (orphan prune,
  `cache-sync --rebuild`); part of the signature so a deletion that
  leaves `MAX(id)` unchanged still invalidates (spec §7, Codex F4).
- `BucketCache` — immutable per-past-bucket `BucketUsage` cache for the
  Group A calendar builders (daily / monthly / weekly), spec §5.1.
- `SessionCache` — immutable per-session aggregate cache over the FULL
  window for the Group B sessions builder, spec §5.2 / Codex F5.

Design invariants (spec §7):
- Every cached value is treated as IMMUTABLE — callers store finished
  aggregates and never mutate them in place (SSE client threads read the
  published snapshot concurrently).
- These functions take explicit `sqlite3.Connection` objects and plain
  values; no dashboard/TUI imports, no DB opening of their own.

`session_entries` / `codex_session_entries` live in `cache.db`;
`weekly_usage_snapshots` / `weekly_cost_snapshots` / `week_reset_events`
/ `weekly_credit_floors` live in `stats.db` (bin/_cctally_db.py,
bin/_cctally_core.py). `compute_signature` takes both conns for that
reason.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from typing import TYPE_CHECKING, Callable, NamedTuple

from _cctally_core import parse_iso_datetime

if TYPE_CHECKING:  # type hints only — no runtime coupling to the aggregators
    from _lib_aggregators import BucketUsage, ClaudeSessionUsage


# === Task 0.1 — composite data-version signature ===========================


class SnapshotSignature(NamedTuple):
    """Cheap composite change-signal over every source table (spec §3).

    Equality is value-equality (NamedTuple), so an unchanged signature
    across two ticks means no cached surface's inputs moved → idle path.
    """

    max_entry_id: int
    max_wus_id: int
    max_wcs_id: int
    reset_sig: tuple[int, int]
    max_codex_id: int
    generation: int


def _max_id(conn: sqlite3.Connection, table: str) -> int:
    """`MAX(id)` on an autoincrement table, 0 on empty or missing table.

    A single rowid b-tree descent (O(1)-ish). Returns 0 on a fresh /
    partially-migrated DB where the table does not yet exist, so the
    signature never raises.
    """
    try:
        row = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()
        return int(row[0])
    except sqlite3.Error:
        return 0


def _reset_sig(conn: sqlite3.Connection) -> tuple[int, int]:
    """Change-signal over the two reset-event tables combined (spec §3).

    A credit / reset re-shapes a PAST weekly bucket with NO new
    `session_entries` row, so the composite signature must cover it.
    Uses `(COUNT(*), MAX(rowid))` over `week_reset_events` +
    `weekly_credit_floors`: the count catches inserts, the max-rowid
    catches the (rare) case where a delete+insert keeps the count level.
    `rowid` aliases the tables' `INTEGER PRIMARY KEY id`. Returns (0, 0)
    on a fresh DB where the tables are absent.
    """
    try:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM week_reset_events)"
            "     + (SELECT COUNT(*) FROM weekly_credit_floors),"
            "       (SELECT COALESCE(MAX(rowid), 0) FROM week_reset_events)"
            "     + (SELECT COALESCE(MAX(rowid), 0) FROM weekly_credit_floors)"
        ).fetchone()
        return (int(row[0]), int(row[1]))
    except sqlite3.Error:
        return (0, 0)


def compute_signature(
    cache_conn: sqlite3.Connection,
    stats_conn: sqlite3.Connection,
    *,
    generation: int,
) -> SnapshotSignature:
    """Composite data-version signature across cache.db + stats.db (spec §3).

    ``cache_conn`` reads ``session_entries`` / ``codex_session_entries``
    (cache.db); ``stats_conn`` reads ``weekly_usage_snapshots`` /
    ``weekly_cost_snapshots`` / the reset-event tables (stats.db).
    ``generation`` is the current cache-generation counter, folded in so
    an in-place history deletion (which need not lower any ``MAX(id)``)
    still advances the signature.
    """
    return SnapshotSignature(
        max_entry_id=_max_id(cache_conn, "session_entries"),
        max_wus_id=_max_id(stats_conn, "weekly_usage_snapshots"),
        max_wcs_id=_max_id(stats_conn, "weekly_cost_snapshots"),
        reset_sig=_reset_sig(stats_conn),
        max_codex_id=_max_id(cache_conn, "codex_session_entries"),
        generation=int(generation),
    )


# === Task 0.2 — new-entry timestamp watermark ==============================


def new_min_timestamp(
    cache_conn: sqlite3.Connection,
    last_seen_max_id: int,
) -> "dt.datetime | None":
    """Earliest EVENT time among genuinely-new session_entries rows.

    Returns ``MIN(timestamp_utc)`` over rows with ``id > last_seen_max_id``
    as an aware UTC datetime, or ``None`` when there are no such rows.

    This is the per-builder dirty-bucket watermark (spec §3, Codex F1).
    ``session_entries.id`` is INGEST order, not event time — a resumed or
    late-ingested file produces a NEW ``id`` carrying an OLD
    ``timestamp_utc``. So when the signature advances, the affected time
    window starts at the earliest event time among the new rows, which may
    reach back into a PAST calendar bucket; each builder recomputes every
    one of its buckets whose window ends after this watermark and serves
    strictly-older buckets from cache.

    Returns ``None`` on a missing table (fresh DB) so callers can treat it
    as "no new rows".
    """
    try:
        row = cache_conn.execute(
            "SELECT MIN(timestamp_utc) FROM session_entries WHERE id > ?",
            (last_seen_max_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    # Parse via the repo's canonical ISO helper (handles both `...Z` and
    # `...+00:00` stored forms), then normalize to a UTC-tzinfo instant so
    # downstream comparisons against UTC bucket boundaries are exact.
    parsed = parse_iso_datetime(row[0], "session_entries.timestamp_utc")
    return parsed.astimezone(dt.timezone.utc)


# === Task 0.3 — cache-generation counter ===================================
#
# A monotonic counter bumped by any path that deletes/rewrites history in
# place (orphan prune, `cache-sync --rebuild`). Folded into the composite
# signature (§3) so a deletion that leaves MAX(id) unchanged still
# invalidates the caches (spec §7, Codex F4). Guarded by a lock: the
# counter is bumped from the sync thread and read during signature compute
# on the same thread, but the lock makes any future off-thread bump safe.

_GENERATION_LOCK = threading.Lock()
_GENERATION = 0


def bump_generation() -> int:
    """Monotonically advance the cache-generation counter; return the new value."""
    global _GENERATION
    with _GENERATION_LOCK:
        _GENERATION += 1
        return _GENERATION


def current_generation() -> int:
    """Return the current cache-generation counter."""
    with _GENERATION_LOCK:
        return _GENERATION


# === Task 0.4 — Group A bucket cache holder ================================


class BucketCache:
    """Immutable per-past-bucket `BucketUsage` cache for the calendar builders.

    Stores the raw immutable aggregate (a `BucketUsage`) per past bucket,
    keyed by ``(builder_key, bucket_label)`` where ``builder_key`` is one
    of ``"daily"`` / ``"monthly"`` / ``"weekly"`` and ``bucket_label`` is
    that builder's bucket identifier (``"2026-06-30"`` daily, ``"2026-06"``
    monthly, the SubWeek key for weekly).

    Spec §5.1: the cache holds the RAW aggregate, never the final
    presentation row, and values are treated IMMUTABLE — a recomputed
    bucket is put whole (never mutated in place), so an SSE client thread
    reading a previously-published snapshot's rows can never observe a
    torn value (spec §7 / Codex F7).
    """

    def __init__(self) -> None:
        self._store: "dict[tuple[str, str], BucketUsage]" = {}

    def get(self, builder_key: str, bucket_label: str) -> "BucketUsage | None":
        """Return the cached aggregate for this bucket, or None on a miss."""
        return self._store.get((builder_key, bucket_label))

    def put(self, builder_key: str, bucket_label: str, bucket: "BucketUsage") -> None:
        """Store the (immutable) aggregate for this bucket."""
        self._store[(builder_key, bucket_label)] = bucket

    def drop_from(
        self, builder_key: str, predicate: "Callable[[str], bool]"
    ) -> None:
        """Evict cached buckets for ``builder_key`` whose label matches ``predicate``.

        Used to invalidate the dirty tail: pass a predicate that is True for
        every bucket label at/after the watermark (or otherwise known dirty).
        Only the given ``builder_key``'s namespace is touched.
        """
        doomed = [
            key
            for key in self._store
            if key[0] == builder_key and predicate(key[1])
        ]
        for key in doomed:
            del self._store[key]

    def clear(self) -> None:
        """Drop every cached bucket across all builders (full invalidation)."""
        self._store.clear()


# === Task 0.5 — Group B session cache holder ===============================


class SessionCache:
    """Immutable per-session aggregate cache over the FULL sessions window.

    Holds ALL sessions in the builder's window (spec §5.2 / Codex F5),
    keyed by the resolved session identity — NOT just the visible top 100.
    Sorting/truncating for the 100-row view is done over ``get_all()`` each
    tick, so a session that was previously below the cut can promote into
    view once it gets new activity; caching only the visible slice would
    make that impossible.

    Values are aggregated ``ClaudeSessionUsage`` rows, treated immutable: a
    changed session is fully re-aggregated and ``put`` whole (a resumed /
    straddling session re-aggregates from its entire entry set, so there is
    no split-row bug), never mutated in place (spec §7 / Codex F7).
    """

    def __init__(self) -> None:
        self._store: "dict[str, ClaudeSessionUsage]" = {}

    def get_all(self) -> "dict[str, ClaudeSessionUsage]":
        """Return a shallow COPY of every cached session, keyed by identity.

        A copy so a caller's sort/truncate over the candidate set can never
        mutate the module-level store (the immutable-cache discipline). The
        aggregate values are shared (they are themselves immutable).
        """
        return dict(self._store)

    def put(self, session_key: str, session: "ClaudeSessionUsage") -> None:
        """Store (or replace) the aggregate for one session identity."""
        self._store[session_key] = session

    def drop(self, session_keys: "set[str]") -> None:
        """Remove the given session identities; absent keys are ignored."""
        for key in session_keys:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Drop every cached session (full invalidation)."""
        self._store.clear()
