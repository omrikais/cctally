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
from typing import NamedTuple

from _cctally_core import parse_iso_datetime


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
