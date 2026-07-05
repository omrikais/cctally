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
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, NamedTuple

from _cctally_core import parse_iso_datetime

# #271: the current-bucket accumulator (below) reuses the aggregators' single
# per-entry fold primitive so the incremental append is byte-identical to the
# full pass (spec §6/§7). This is the ONE deliberate runtime coupling to
# _lib_aggregators — the cache primitives above stay decoupled (BucketUsage is
# still a TYPE_CHECKING-only hint). _lib_aggregators imports only _cctally_core,
# so there is no import cycle.
from _lib_aggregators import _fold_entry, _finalize_bucket, _new_bucket_acc

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


# === Task 2.1 — Group A cached-bucket recompute helper =====================
#
# The shared per-past-bucket aggregate cache for the three calendar
# builders (daily / monthly / weekly), plus the two entry points the
# dashboard builders call:
#
# - ``cached_buckets`` — the PURE per-bucket assembly loop: recompute the
#   current + caller-marked-dirty buckets whole (from a timestamp-ordered
#   fetch, spec §5.1 / Codex F3), serve clean past buckets from the
#   ``BucketCache``, and recompute (cold-miss) any label the cache lacks.
# - ``build_cached_group_a`` — the STATEFUL wrapper: each Group A builder
#   is self-caching and independently byte-correct (M2 key decision — no
#   dependency on the not-yet-built M5 dispatch). It tracks THIS builder's
#   own last-seen ``(MAX(session_entries.id), extra_signature)`` alongside
#   the builder's ``BucketCache`` namespace, derives the dirty predicate
#   from the new-entry timestamp watermark (``new_min_timestamp``), and
#   full-invalidates the namespace on an ``extra_signature`` change (weekly
#   snapshot/reset legs, or the daily/monthly display-tz flip) or a
#   ``MAX(id)`` regression (cache.db rebuilt). Cold, warm, and invalidated
#   ticks are all byte-identical to a from-scratch aggregation.
#
# Module-level state follows the ``_PROJECTS_ENV_MEMO`` precedent (spec §7):
# a single shared ``BucketCache`` namespaced by ``builder_key`` and a
# per-builder last-seen dict. Every cached value is an IMMUTABLE
# ``BucketUsage``; the builders build FRESH presentation rows over the
# assembled list each tick and never mutate a cached aggregate (Codex F7).

_GROUP_A_CACHE = BucketCache()
_GROUP_A_LAST_SEEN: "dict[str, dict]" = {}


def group_a_cache() -> BucketCache:
    """Return the shared Group A ``BucketCache`` (daily/monthly/weekly)."""
    return _GROUP_A_CACHE


def reset_group_a_state() -> None:
    """Clear the Group A bucket cache AND every builder's last-seen state.

    Test hook + the M5 orphan-prune / ``cache-sync --rebuild`` invalidation
    entry point: after history is deleted or rewritten in place, drop every
    cached bucket and reset the watermarks so the next rebuild recomputes
    from scratch (spec §7 / Codex F4).
    """
    _GROUP_A_CACHE.clear()
    _GROUP_A_LAST_SEEN.clear()
    reset_group_a_current_state()


# === #271 — incremental current-bucket accumulator =========================
#
# The Group A builders (daily / monthly / weekly) re-fold the whole OPEN
# bucket every warm tick (`cached_buckets` recomputes `current_label` from a
# full window fetch). On a recency-dense instance that open week/month/day
# holds tens of thousands of entries, so the recompute dominates the residual
# warm rebuild (#271 §1). This holds a persisted single-left-fold accumulator
# per builder key: each warm tick folds only the DELTA (new-by-id OR
# newly-in-window-because-`now`-advanced) into the running aggregate, with a
# full-recompute fallback when a late older-timestamp row lands mid-bucket.
#
# Byte-identity rests on the pinned `(timestamp_utc, id)` fold order (#271 §5)
# + the shared `_fold_entry` primitive (§6): the incremental append reproduces
# the full left-fold exactly. Single-writer (sync thread only), module state,
# never reachable from a published DataSnapshot (F7 — the snapshot holds the
# finalized BucketUsage, not `acc`).


@dataclass
class CurrentBucketAccumulator:
    """Persisted running fold of one Group A builder's CURRENT bucket (§7a)."""
    label: str
    acc: dict                    # running _new_bucket_acc() shape
    tail: "tuple | None"         # (timestamp_utc, id) of the last folded entry
    last_seen_id: int            # max session_entries.id reconciled (= cur_max_id)
    last_now: dt.datetime        # now_utc upper bound used to clamp the last fold


_GROUP_A_CURRENT: "dict[str, CurrentBucketAccumulator]" = {}


def reset_group_a_current_state() -> None:
    """Drop every builder's current-bucket accumulator (prune-site + full-invalidate)."""
    _GROUP_A_CURRENT.clear()


def _finalize_or_none(label, acc, tail):
    """Finalize ``acc`` into a ``BucketUsage``, or ``None`` when nothing was
    folded (``tail is None`` ⇒ an empty/gap bucket, matching a from-scratch
    ``aggregate_one`` returning ``None``)."""
    return None if tail is None else _finalize_bucket(label, acc)


def accumulate_current_bucket(prior, *, current_label, cur_now, cur_max_id,
                              fetch_all, fetch_delta, membership, mode="auto"):
    """Pure §7b tick algorithm. Returns ``(BucketUsage | None, CurrentBucketAccumulator)``.

    ``fetch_all() -> list[(id, UsageEntry)]`` over the whole current-bucket
    window (cold / fallback); ``fetch_delta(after_id, after_ts) ->
    list[(id, UsageEntry)]`` the ``(id > after_id OR ts > after_ts)`` delta;
    both ordered ``(timestamp_utc, id)``. ``membership(entry) -> bool`` keeps
    exactly the entries the full pass assigns to ``current_label``.
    """
    def _cold():
        acc = _new_bucket_acc()
        tail = None
        for eid, e in fetch_all():
            if e.model == "<synthetic>" or not membership(e):
                continue
            _fold_entry(acc, e, mode)
            tail = (e.timestamp, eid)
        return acc, tail

    if prior is None or prior.label != current_label:
        acc, tail = _cold()
        new = CurrentBucketAccumulator(current_label, acc, tail, cur_max_id, cur_now)
        return _finalize_or_none(current_label, acc, tail), new

    delta = [
        (eid, e) for eid, e in fetch_delta(prior.last_seen_id, prior.last_now)
        if e.model != "<synthetic>" and membership(e)
    ]
    if not delta:
        # From-scratch fold set unchanged since last tick → byte-identical.
        new = CurrentBucketAccumulator(current_label, prior.acc, prior.tail,
                                       cur_max_id, cur_now)
        return _finalize_or_none(current_label, prior.acc, prior.tail), new

    # Guard: every folded delta row must sort AFTER the tail (a strict suffix).
    # tail None ⇒ prior folded nothing ⇒ the delta IS the full member set (each
    # current member has id>last_seen OR ts>last_now), so appending == full fold.
    if prior.tail is not None and any(
        (e.timestamp, eid) <= prior.tail for eid, e in delta
    ):
        acc, tail = _cold()  # late-ingest mid-bucket fallback
        new = CurrentBucketAccumulator(current_label, acc, tail, cur_max_id, cur_now)
        return _finalize_or_none(current_label, acc, tail), new

    acc = prior.acc  # mutate in place (module state, never published)
    tail = prior.tail
    for eid, e in delta:  # fetch_delta returns (timestamp_utc, id) ascending
        _fold_entry(acc, e, mode)
        tail = (e.timestamp, eid)
    new = CurrentBucketAccumulator(current_label, acc, tail, cur_max_id, cur_now)
    return _finalize_or_none(current_label, acc, tail), new


def cached_buckets(
    builder_key: str,
    *,
    cache: BucketCache,
    all_bucket_labels: "list[str]",
    current_label: "str | None",
    dirty_predicate: "Callable[[str], bool]",
    fetch_bucket_entries: "Callable[[str], list]",
    aggregate_one: "Callable[[str, list], object | None]",
    current_override: "Callable[[], object | None] | None" = None,
) -> "list[object]":
    """Assemble one Group A builder's per-bucket aggregates (spec §5.1).

    For each label in ``all_bucket_labels`` (caller order — pass oldest→newest
    so the assembled list matches ``_aggregate_*``'s ascending-key output):

    - If the label is ``current_label`` and ``current_override`` is supplied
      (#271 §8a), the current bucket is produced by ``current_override()`` — the
      incremental accumulator — INSTEAD of the full ``aggregate_one`` recompute.
      The override returns the finalized ``BucketUsage`` (or ``None`` for a
      no-data current bucket, handled by the same gap-drop below).
    - Else if the label is ``current_label`` or ``dirty_predicate`` returns True
      (the watermark reached it, or a forced recompute), recompute it WHOLE via
      ``aggregate_one(label, fetch_bucket_entries(label))`` — a
      timestamp-ordered fetch, so ``_aggregate_buckets`` first-seen model order
      reproduces the full-history pass byte-for-byte (Codex F3).
    - Otherwise serve the cached ``BucketUsage``; on a cache MISS (cold start /
      post-invalidation) recompute it the same way.

    ``aggregate_one`` / ``current_override`` return ``None`` for a label with no
    data (a gap day/month/week): the bucket is omitted from the result and any
    stale cache entry for that label is evicted. The returned list therefore
    contains only buckets-with-data — exactly what
    ``_aggregate_daily/_monthly/_weekly`` over the full entry set returns — in
    ``all_bucket_labels`` order.

    Values are stored/served as-is (immutable ``BucketUsage``); this loop never
    mutates a bucket in place (spec §7). ``current_override`` stays a pure hook:
    the assembly order, past-bucket serve/cold-miss, and gap-drop are untouched.
    """
    result: "list[object]" = []
    for label in all_bucket_labels:
        if label == current_label and current_override is not None:
            bucket = current_override()
        else:
            recompute = (label == current_label) or dirty_predicate(label)
            bucket = None
            if not recompute:
                bucket = cache.get(builder_key, label)
            if bucket is None:  # forced recompute OR cold miss
                bucket = aggregate_one(label, fetch_bucket_entries(label))
        if bucket is None:
            # Gap bucket (no data): drop any stale cache entry, omit from output.
            cache.drop_from(builder_key, lambda lbl, _t=label: lbl == _t)
        else:
            cache.put(builder_key, label, bucket)
            result.append(bucket)
    return result


def build_cached_group_a(
    builder_key: str,
    *,
    cache_conn: sqlite3.Connection,
    all_bucket_labels: "list[str]",
    current_label: "str | None",
    bucket_end_of: "Callable[[str], dt.datetime | None]",
    fetch_bucket_entries: "Callable[[str], list]",
    aggregate_one: "Callable[[str, list], object | None]",
    extra_signature: object = None,
    use_current_accumulator: bool = False,
    now_utc: "dt.datetime | None" = None,
    current_all_fetch: "Callable[[str], list] | None" = None,
    current_delta_fetch: "Callable[[str, int, dt.datetime], list] | None" = None,
    membership_of: "Callable[[object], bool] | None" = None,
) -> "list[object]":
    """Stateful Group A assembly: invalidation + watermark + ``cached_buckets``.

    ``cache_conn`` is a ``cache.db`` connection (reads
    ``MAX(session_entries.id)`` + the new-entry watermark). ``bucket_end_of``
    maps a label to an aware-UTC datetime that is at-or-after that bucket's
    window end — a bucket is watermark-dirty when its end is after
    ``new_min_timestamp`` (over-estimating the end only over-marks buckets
    dirty, which is safe: it never serves stale past data). ``extra_signature``
    is any hashable value whose change forces a full namespace invalidation
    (the weekly snapshot/reset legs, or the daily/monthly display-tz label) —
    e.g. a weekly bucket still transitioning through a credit rides the
    weekly builder's ``extra_signature`` full-invalidate rather than a
    per-label recompute flag.

    Returns the assembled ``BucketUsage`` list (cache hits for clean past
    labels, fresh recompute for current + dirty), in ``all_bucket_labels``
    order, and updates this builder's last-seen state.
    """
    cur_max_id = _max_id(cache_conn, "session_entries")
    state = _GROUP_A_LAST_SEEN.get(builder_key)
    full_invalidate = state is not None and (
        state.get("extra") != extra_signature
        or cur_max_id < int(state.get("max_id", 0))
    )
    if full_invalidate:
        _GROUP_A_CACHE.drop_from(builder_key, lambda _lbl: True)
        # The current bucket's boundaries may have shifted (a weekly signature
        # move / cache.db rebuild) → discard the accumulator so the override
        # cold-refolds this tick (#271 §8c).
        _GROUP_A_CURRENT.pop(builder_key, None)

    if state is None or full_invalidate:
        # Cold start OR post-invalidation: recompute every label. (Cold already
        # cache-misses; being explicit also covers the boundary/signature-shift
        # case where a stale label could otherwise collide with a fresh window.)
        def dirty(_label: str) -> bool:
            return True
    else:
        new_min_ts = new_min_timestamp(cache_conn, int(state.get("max_id", 0)))

        def dirty(label: str) -> bool:
            if new_min_ts is None:
                return False
            end = bucket_end_of(label)
            return end is not None and end > new_min_ts

    # #271 §8a/§8b: when the incremental accumulator is enabled, the current
    # bucket is produced by folding only the delta each tick instead of a full
    # re-aggregate. The delta's id lower bound reuses the SAME previous-tick
    # `state["max_id"]` the watermark path reads (via prior.last_seen_id inside
    # the accumulator). Gated ON only by the sync-thread `_group_a_*_buckets`
    # closures; every other caller leaves it off (byte-identical to today).
    current_override = None
    if use_current_accumulator and current_label is not None:
        def current_override():
            prior = _GROUP_A_CURRENT.get(builder_key)
            bucket, new_state = accumulate_current_bucket(
                prior,
                current_label=current_label,
                cur_now=now_utc,
                cur_max_id=cur_max_id,
                fetch_all=lambda: current_all_fetch(current_label),
                fetch_delta=lambda aid, ats: current_delta_fetch(
                    current_label, aid, ats),
                membership=membership_of,
                mode="auto",
            )
            _GROUP_A_CURRENT[builder_key] = new_state
            return bucket

    buckets = cached_buckets(
        builder_key,
        cache=_GROUP_A_CACHE,
        all_bucket_labels=all_bucket_labels,
        current_label=current_label,
        dirty_predicate=dirty,
        fetch_bucket_entries=fetch_bucket_entries,
        aggregate_one=aggregate_one,
        current_override=current_override,
    )
    _GROUP_A_LAST_SEEN[builder_key] = {"max_id": cur_max_id, "extra": extra_signature}
    return buckets


# === Task 3.1 — changed-session resolution (join + filename-stem fallback) ==


def affected_session_keys(
    cache_conn: sqlite3.Connection,
    last_seen_max_id: int,
) -> "set[str]":
    """Resolved session identities for entries with ``id > last_seen`` (spec §5.2).

    Mirrors ``_aggregate_claude_sessions`` grouping EXACTLY: identity is
    ``session_files.session_id`` when the ``LEFT JOIN`` on ``source_path``
    yields a non-null id, else the filename-stem of ``source_path``
    (``os.path.splitext(os.path.basename(path))[0]``) — the same fallback
    the aggregator applies when ``entry.session_id is None``
    (``bin/_lib_aggregators.py``). ``<synthetic>``-model rows are skipped
    (the aggregator skips them before the fallback), so a purely-synthetic
    new row contributes no key.

    ``session_entries`` has NO ``session_id`` column — identity comes from
    the join to ``session_files`` — so the returned keys key IDENTICALLY to
    the aggregator's session grouping (Codex F5). Returns an empty set on a
    missing table (fresh / partially-migrated DB) so callers never raise.
    """
    try:
        rows = cache_conn.execute(
            "SELECT se.source_path, sf.session_id "
            "FROM session_entries se "
            "LEFT JOIN session_files sf ON sf.path = se.source_path "
            "WHERE se.id > ? AND se.model != '<synthetic>'",
            (last_seen_max_id,),
        ).fetchall()
    except sqlite3.Error:
        return set()
    keys: "set[str]" = set()
    for source_path, session_id in rows:
        if session_id is not None:
            keys.add(session_id)
        else:
            keys.add(os.path.splitext(os.path.basename(source_path))[0])
    return keys


# === Task 3.2 — Group B session aggregate cache over the FULL window =======
#
# Module-level state following the Group A precedent (spec §7): a single
# shared ``SessionCache`` holding ALL sessions in the 365-day window (NOT
# just the visible top 100 — Codex F5), plus this builder's own last-seen
# ``MAX(session_entries.id)`` watermark. Every cached value is an immutable
# ``ClaudeSessionUsage``; the builder builds FRESH ``TuiSessionRow`` objects
# from the assembled list each tick and never mutates a cached aggregate
# (Codex F7). Mutated ONLY on the sync thread (single-writer, like Group A).

_SESSION_CACHE = SessionCache()
_SESSION_LAST_SEEN: "dict" = {}


def session_cache() -> SessionCache:
    """Return the shared Group B ``SessionCache`` (all sessions in-window)."""
    return _SESSION_CACHE


def reset_session_cache_state() -> None:
    """Clear the session cache AND its last-seen watermark (full invalidation).

    Test hook + the M5 orphan-prune / ``cache-sync --rebuild`` invalidation
    entry point: after history is deleted or rewritten in place, drop every
    cached session and reset the watermark so the next rebuild re-aggregates
    the whole window from scratch (spec §7 / Codex F4).
    """
    _SESSION_CACHE.clear()
    _SESSION_LAST_SEEN.clear()


def build_cached_sessions(
    *,
    cache_conn: sqlite3.Connection,
    aggregate_all: "Callable[[], list]",
    reaggregate: "Callable[[int, set], list]",
    extra_signature: object = None,
) -> "list":
    """Stateful Group B assembly: cold full-aggregate / warm affected-only (spec §5.2).

    ``cache_conn`` is a ``cache.db`` connection (reads
    ``MAX(session_entries.id)`` + the affected-session set). ``aggregate_all``
    returns the full ``list[ClaudeSessionUsage]`` for the window (cold path).
    ``reaggregate(last_seen, affected_keys)`` returns the re-aggregated
    ``ClaudeSessionUsage`` for exactly the sessions touched since ``last_seen``
    (warm path) — a straddling/resumed session re-aggregates WHOLE from its
    own full in-window entry set, so no split-row bug. ``extra_signature`` is
    any hashable whose change forces a full cold rebuild.

    Cold when: no prior state, ``extra_signature`` changed, or a
    ``MAX(id)`` regression (cache.db rebuilt). Warm otherwise, re-aggregating
    only the affected sessions and updating their cache rows in place of the
    stale ones. Returns the FULL cached session list (UNSORTED — the caller
    window-filters, sorts by ``last_activity`` desc, and truncates to the
    view limit, which is what preserves correct eviction/**promotion** at the
    100-row boundary, Codex F5).

    Each returned/stored value is an immutable ``ClaudeSessionUsage`` keyed by
    its resolved ``session_id`` (which the aggregator sets to the stem for
    fallback sessions), so the cache keys match ``affected_session_keys``.
    """
    cur_max_id = _max_id(cache_conn, "session_entries")
    state = _SESSION_LAST_SEEN
    cold = (
        not state
        or state.get("extra") != extra_signature
        or cur_max_id < int(state.get("max_id", 0))
    )
    if cold:
        _SESSION_CACHE.clear()
        for sess in aggregate_all():
            _SESSION_CACHE.put(sess.session_id, sess)
    else:
        last_seen = int(state.get("max_id", 0))
        if cur_max_id > last_seen:
            affected = affected_session_keys(cache_conn, last_seen)
            if affected:
                for sess in reaggregate(last_seen, affected):
                    _SESSION_CACHE.put(sess.session_id, sess)
    _SESSION_LAST_SEEN.clear()
    _SESSION_LAST_SEEN["max_id"] = cur_max_id
    _SESSION_LAST_SEEN["extra"] = extra_signature
    return list(_SESSION_CACHE.get_all().values())


# === Task 4.2 — doctor payload TTL memo (spec §6) ==========================
#
# The dashboard envelope used to re-fork the `security` keychain subprocess
# (via `doctor_gather_state`) once PER SSE CLIENT PER TICK. §6 moves the
# doctor gather onto the sync-thread `DataSnapshot` (precomputed once per
# rebuild). This short-TTL memo further guards against back-to-back WARM
# rebuilds re-forking `security` every tick — the keychain/symlink/log state
# it reads changes rarely. The `compute` callable is INJECTED so this module
# stays decoupled from the doctor I/O layer (no `_cctally_doctor` import).
# The lazy `GET /api/doctor` endpoint deliberately does NOT route through
# this memo — an explicit user refresh must be live.

DOCTOR_MEMO_TTL_S = 30.0

_DOCTOR_MEMO_LOCK = threading.Lock()
_DOCTOR_MEMO: "dict" = {}


def doctor_payload_memo(
    now_utc: dt.datetime,
    runtime_bind: "str | None",
    *,
    ttl_s: float,
    compute: "Callable[[dt.datetime, str | None], dict]",
) -> dict:
    """Return the doctor envelope payload, recomputing via ``compute`` only
    when the memo is cold — never computed, older than ``ttl_s``, a clock
    regression (``now_utc`` before the cached instant), or a ``runtime_bind``
    change (the bind feeds ``safety.dashboard_bind``).

    ``compute(now_utc, runtime_bind) -> dict`` is the injected
    gather→checks→envelope-dict step; it runs OUTSIDE the lock so the
    `security` subprocess fork never serializes other readers. In practice
    only the sync thread calls this, so the (harmless) double-compute a
    concurrent caller could trigger never happens.
    """
    with _DOCTOR_MEMO_LOCK:
        cached = _DOCTOR_MEMO
        computed_at = cached.get("computed_at")
        fresh = (
            bool(cached)
            and cached.get("runtime_bind") == runtime_bind
            and computed_at is not None
            and now_utc >= computed_at
            and (now_utc - computed_at).total_seconds() < ttl_s
        )
        if fresh:
            return cached["payload"]
    payload = compute(now_utc, runtime_bind)
    with _DOCTOR_MEMO_LOCK:
        _DOCTOR_MEMO.clear()
        _DOCTOR_MEMO["payload"] = payload
        _DOCTOR_MEMO["computed_at"] = now_utc
        _DOCTOR_MEMO["runtime_bind"] = runtime_bind
    return payload


def reset_doctor_memo() -> None:
    """Drop the memoized doctor payload (test hook + M5 invalidation entry)."""
    with _DOCTOR_MEMO_LOCK:
        _DOCTOR_MEMO.clear()


# === Task 5.1 — idle-path dispatch state (last signature + snapshot) ========
#
# The dashboard sync-thread rebuild computes the composite ``SnapshotSignature``
# at the top of every tick; when it is UNCHANGED versus the last published
# rebuild AND no wall-clock day/week/month boundary has rolled over, the rebuild
# takes the IDLE path (spec §3): it reuses the last published snapshot's heavy
# period/session rows and re-patches only the time-derived fields, skipping ALL
# re-aggregation — so an idle dashboard sits near 0% CPU. This module holds that
# last ``(signature, snapshot)`` pair.
#
# Sync-thread-only, single-writer — same discipline as the Group A / session
# caches (spec §7). The snapshot is stored as an OPAQUE object: this module
# never introspects it, keeping the "no dashboard/TUI import" design (the caller
# in bin/_cctally_tui.py owns the ``DataSnapshot`` type and all patching).

_LAST_DISPATCH_KEY: object = None
_LAST_PUBLISHED_SNAPSHOT: object = None


def dispatch_state() -> "tuple[object, object]":
    """Return the ``(last dispatch key, last published snapshot)`` pair (spec §3).

    ``(None, None)`` before the first rebuild or after a reset. The dispatch key
    is the caller's opaque hashable — the composite ``SnapshotSignature`` bundled
    with a render key (resolved display-tz + config) covering the render inputs
    the DB signature does not; the caller compares it whole. The snapshot is an
    opaque ``DataSnapshot`` reference the caller owns.
    """
    return (_LAST_DISPATCH_KEY, _LAST_PUBLISHED_SNAPSHOT)


def store_dispatch_state(dispatch_key: object, snapshot: object) -> None:
    """Record the dispatch key + published snapshot for the next idle short-circuit.

    Called once per rebuild (idle or full) so the next tick compares against the
    key the just-published snapshot was built from.
    """
    global _LAST_DISPATCH_KEY, _LAST_PUBLISHED_SNAPSHOT
    _LAST_DISPATCH_KEY = dispatch_key
    _LAST_PUBLISHED_SNAPSHOT = snapshot


def reset_dispatch_state() -> None:
    """Drop the idle-path ``(key, snapshot)`` memo (test hook + isolation).

    A fresh process starts with no memo; tests reset it between rebuilds so a
    prior test's leftover snapshot can't be idle-served under a matching key. Not
    part of the M5.2 prune invalidation — the generation bump (a signature leg)
    already forces the next rebuild off the idle path.
    """
    global _LAST_DISPATCH_KEY, _LAST_PUBLISHED_SNAPSHOT
    _LAST_DISPATCH_KEY = None
    _LAST_PUBLISHED_SNAPSHOT = None


# === #269 M0 — shared per-weekref immutable-cost cache (B1 trend + B3 forecast)
#
# A closed subscription week's cost is IMMUTABLE, so it is computed once and
# reused until a signal invalidates it (spec §4). Both `build_trend_view`'s
# reset-event weeks (`_compute_cost_for_weekref`) and forecast's trailing-4-week
# fallback (`_select_dollars_per_percent` → `_sum_cost_for_range`) call the same
# per-closed-week cost primitive from two sites; one shared cache keyed by the
# week's `(week_start_at, week_end_at)` boundaries serves both. The OPEN (current)
# week is never cached — it is decided per call from `week_end_at > now_utc`, so a
# just-closed week caches on the next tick and the newly-opened week always
# recomputes (no `_snapshot_period_rolled_over` dependence).
#
# Module-level state follows the Group A / session-cache precedent (spec §7): a
# plain dict of immutable floats + a per-cache last-seen dict, mutated only on the
# dashboard sync thread (single-writer). Every cached value is an immutable float;
# each rebuild builds FRESH trend / forecast presentation objects and never
# mutates a cached value (Codex F7).

_WEEKREF_COST_CACHE: dict = {}          # {(week_start_iso, week_end_iso): cost_usd}
_WEEKREF_COST_LAST_SEEN: dict = {}      # {"max_id": int, "reset_sig": tuple}


def _weekref_key(week_start_at, week_end_at):
    """Canonical UTC-ISO key for a subscription week's cost.

    Normalizes both boundaries to UTC before serializing, so two callers that
    resolve the same physical week in different tzinfos key identically.
    """
    return (
        week_start_at.astimezone(dt.timezone.utc).isoformat(),
        week_end_at.astimezone(dt.timezone.utc).isoformat(),
    )


def reset_weekref_cost_state():
    """Clear the weekref-cost cache + its watermark (full invalidation).

    Called from the orphan-prune site (a prune deletes ``session_entries``
    possibly WITHOUT lowering ``MAX(id)``, so the reconcile's max-id-regression
    check cannot catch it — the explicit clear must) and as a test hook.
    """
    _WEEKREF_COST_CACHE.clear()
    _WEEKREF_COST_LAST_SEEN.clear()


def cached_weekref_cost(*, week_start_at, week_end_at, now_utc, compute):
    """Get-or-compute a subscription week's cost (spec §4).

    The OPEN week (``week_end_at > now_utc``) is always recomputed and never
    cached — open-vs-closed is decided per call so a just-closed week caches on
    the next tick and the newly-opened week always recomputes. A CLOSED week is
    served from ``_WEEKREF_COST_CACHE`` on a hit, else computed via ``compute``
    and stored. ``compute`` is the caller's from-scratch closure
    (``_compute_cost_for_weekref`` for B1, ``_sum_cost_for_range`` for B3), so
    the returned float is bit-identical to today's.
    """
    if week_end_at > now_utc:
        return compute()
    key = _weekref_key(week_start_at, week_end_at)
    hit = _WEEKREF_COST_CACHE.get(key)
    if hit is not None:  # 0.0 is a legitimate cached value, not a miss
        return hit
    val = compute()
    _WEEKREF_COST_CACHE[key] = val
    return val


def reconcile_weekref_cache(cache_conn, *, max_entry_id, reset_sig):
    """Once-per-non-idle-rebuild invalidation for the weekref-cost cache (spec §4).

    Driven by ``_tui_build_snapshot`` after the idle-path check, before the
    builders run, using the already-computed dispatch-signature legs
    (``max_entry_id`` + ``reset_sig`` are passed in — no extra query for those):

    - Cold (no last-seen): record last-seen, return — no eviction.
    - ``reset_sig`` changed OR ``max_entry_id < last_seen`` (cache.db rebuilt
      out-of-process): full ``clear()``. A credit/reset re-shapes a past week's
      cost; reset events are rare, so a conservative full clear is correct and
      cheap. A max-id regression means the ids no longer map to the same rows.
    - ``max_entry_id > last_seen``: evict cached weeks whose ``week_end_at`` is
      ``>= new_min_timestamp(cache_conn, last_seen)`` — a genuinely-new row
      could fall inside them (F1 late-ingest). The bound is ``>=``, NOT ``>``,
      because ``_sum_cost_for_range`` / ``iter_entries`` sum an inclusive
      ``[start, end]`` window, so a row whose timestamp lands exactly on
      ``week_end_at`` belongs to that week (Codex-1). Over-eviction is byte-safe
      (forces a recompute). Normally ``wm`` is recent and no closed week drops.

    Idempotent within a tick: after the first call updates last-seen, a later
    call in the same tick sees no delta (``max_entry_id == last_seen``,
    ``reset_sig`` unchanged) and no-ops — never re-running the watermark query.

    Connection lifecycle (Codex-4): the ``new_min_timestamp`` watermark query is
    the only use of ``cache_conn`` and runs only on the
    ``max_entry_id > last_seen`` branch; the caller passes a short-lived cache
    connection, opened for that query.
    """
    ls = _WEEKREF_COST_LAST_SEEN
    if not ls:  # cold
        ls["max_id"] = max_entry_id
        ls["reset_sig"] = reset_sig
        return
    if reset_sig != ls["reset_sig"] or max_entry_id < ls["max_id"]:
        _WEEKREF_COST_CACHE.clear()  # reset/credit, or cache.db rebuilt
        ls["max_id"] = max_entry_id
        ls["reset_sig"] = reset_sig
        return
    if max_entry_id > ls["max_id"]:
        wm = new_min_timestamp(cache_conn, ls["max_id"])
        if wm is not None:
            for key in list(_WEEKREF_COST_CACHE):
                # key = (start_iso, end_iso); inclusive [start,end] window, so
                # evict when the week's end >= the earliest new event time.
                if dt.datetime.fromisoformat(key[1]) >= wm:
                    del _WEEKREF_COST_CACHE[key]
        ls["max_id"] = max_entry_id
        ls["reset_sig"] = reset_sig


# === #269 M4 — projects-envelope per-(project, week) incremental cache =======
#
# `_build_projects_envelope` re-iterates all ~190K window entries every warm
# tick, doing a per-entry `_resolve_project_key` + cost + per-(project, week)
# aggregation. At scale that builder DOMINATES the warm rebuild (spec §13). A
# CLOSED week's per-project aggregate is IMMUTABLE, so cache it and recompute
# only the CURRENT week each warm tick (spec §14 Win 2).
#
# Storage (opaque — this module never introspects the aggregate object, keeping
# the "no dashboard/TUI import" design, exactly like `BucketCache`):
# - `_PROJECTS_ENV_WEEK_CACHE`: `{(bucket_path, week_iso) -> agg}` per closed
#   week. The dashboard packs a `(cost_usd, sessions_count, first_seen,
#   last_seen, first_order, first_id, first_key)` record; here it is opaque.
# - `_PROJECTS_ENV_WEEK_TOTALS`: `{week_iso -> total_cost}` cached as its OWN
#   entry-order aggregate (spec §14(a) — never re-summed from project buckets,
#   which would re-associate the float sum). ALSO the "week computed" registry:
#   a week is a cache HIT iff its `week_iso` is present here (an empty computed
#   week is stored with total 0.0 and no bucket rows, so cold empty weeks are
#   not re-queried every tick).
# - `_PROJECTS_ENV_LAST_SEEN`: `{max_id, max_wus_id, sf_sig}` this cache last
#   reconciled against.
#
# Single-writer (sync thread only), immutable values, fresh presentation each
# tick — the Group A / weekref discipline (spec §6, Codex F7).

_PROJECTS_ENV_WEEK_CACHE: dict = {}   # {(bucket_path, week_iso): agg}
_PROJECTS_ENV_WEEK_TOTALS: dict = {}  # {week_iso: total_cost}  (also the registry)
_PROJECTS_ENV_LAST_SEEN: dict = {}    # {"max_id", "max_wus_id", "sf_sig"}


def projects_env_week_key(week_start):
    """Canonical UTC-ISO key for a Monday-anchored subscription week start.

    The dashboard resolves week starts as aware-UTC datetimes; normalizing to
    UTC before serializing keeps the key stable and parseable back by
    ``reconcile_projects_env_cache`` (for the ``week_end`` watermark compare).
    """
    return week_start.astimezone(dt.timezone.utc).isoformat()


def reset_projects_env_state():
    """Clear the projects-envelope week cache + totals + watermark.

    Called from the orphan-prune site (a prune deletes ``session_entries``
    possibly WITHOUT lowering ``MAX(id)``, so the reconcile's regression check
    cannot catch it — the explicit clear must) and as a test hook.
    """
    _PROJECTS_ENV_WEEK_CACHE.clear()
    _PROJECTS_ENV_WEEK_TOTALS.clear()
    _PROJECTS_ENV_LAST_SEEN.clear()


def session_files_sig(cache_conn) -> "tuple[int, int]":
    """`(COUNT(*), COALESCE(MAX(rowid), 0))` over ``session_files`` (Codex-M4 P2).

    ``sync_cache`` lazily backfills ``session_files.session_id`` /
    ``project_path`` for OLD entries — moving a closed week's row from
    ``(unknown)`` to a project, or changing a per-week session count — WITHOUT
    advancing ``MAX(session_entries.id)`` / ``MAX(weekly_usage_snapshots.id)``.
    So the envelope cache keys this cheap change-signal and full-clears when it
    moves. Returns ``(0, 0)`` on a missing table (fresh DB) so callers never
    raise.

    #271 §9d rider (from the #269 final review): this ``(COUNT(*), MAX(rowid))``
    leg does NOT by itself catch the in-place ``ON CONFLICT(path) DO UPDATE SET
    project_path = COALESCE(...)`` attribution backfill — that UPDATE preserves
    the rowid and the row count, so both legs are unmoved. It is covered
    belt-and-suspenders, though: the backfill lands in the SAME ``sync_cache``
    ingest-loop iteration as the file's new ``session_entries`` rows, which bump
    ``max_entry_id`` — caught by the watermark eviction path. So a pure
    attribution move never both slips this signal and leaves the cache stale.
    """
    try:
        row = cache_conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM session_files"
        ).fetchone()
        return (int(row[0]), int(row[1]))
    except sqlite3.Error:
        return (0, 0)


def projects_env_week_get(week_iso):
    """Return ``(buckets_by_bucket_path, total_cost)`` for a cached week, or
    ``None`` on a miss.

    ``_PROJECTS_ENV_WEEK_TOTALS`` is the presence registry: a week is a HIT iff
    its key is present (an empty computed week is stored with total 0.0 and no
    bucket rows, so it is a HIT with an empty bucket map). The bucket rows are
    reassembled by scanning ``_PROJECTS_ENV_WEEK_CACHE`` for this week's keys —
    the distinct-bucket_path count is small (project count), so the scan is
    cheap.
    """
    if week_iso not in _PROJECTS_ENV_WEEK_TOTALS:
        return None
    total = _PROJECTS_ENV_WEEK_TOTALS[week_iso]
    by_bp = {
        bp: agg
        for (bp, wk), agg in _PROJECTS_ENV_WEEK_CACHE.items()
        if wk == week_iso
    }
    return by_bp, total


def projects_env_week_put(week_iso, buckets_by_bp, total) -> None:
    """Store one CLOSED week's per-bucket aggregates + entry-order total.

    ``total`` is registered even when ``buckets_by_bp`` is empty, so a
    computed-empty week is a HIT (not a perpetual miss). Values are stored
    as-is (immutable); this never mutates a stored aggregate in place.
    """
    _PROJECTS_ENV_WEEK_TOTALS[week_iso] = total
    for bp, agg in buckets_by_bp.items():
        _PROJECTS_ENV_WEEK_CACHE[(bp, week_iso)] = agg


def reconcile_projects_env_cache(cache_conn, *, max_entry_id, max_wus_id, sf_sig):
    """Once-per-non-idle-rebuild invalidation for the projects-envelope cache.

    Driven by ``_tui_build_snapshot`` after the idle-path check, before the
    envelope builder runs, using the dispatch-signature legs (``max_entry_id``,
    ``max_wus_id``) + a ``session_files`` signal (``sf_sig``) passed in:

    - Cold (no last-seen): record last-seen, return — no eviction.
    - ``sf_sig`` changed (attribution backfill, Codex-M4 P2) OR
      ``max_entry_id < last_seen`` (cache.db rebuilt): full clear. Conservative
      but byte-safe (recompute). ``max_wus_id`` stays tracked in last-seen
      but is deliberately NOT a full-clear trigger (#271 §9) — a `record-usage`
      write reuses this cost cache; the whole-envelope memo refreshes attribution.
    - ``max_entry_id > last_seen``: evict cached weeks (and their bucket rows +
      week total) whose ``week_end (= parse(week_iso) + 7d)`` is
      ``>= new_min_timestamp(cache_conn, last_seen)`` — a genuinely-new row
      could fall inside them (F1 late-ingest). The bound is ``>=`` (Codex-1);
      over-eviction is byte-safe.

    Idempotent within a tick: after the first call updates last-seen, a later
    call with the same signature sees no delta and no-ops (never re-running the
    watermark query). The short-lived ``cache_conn`` is used only for the
    watermark query on the ``max_entry_id > last_seen`` branch (Codex-4).
    """
    ls = _PROJECTS_ENV_LAST_SEEN
    if not ls:  # cold
        ls["max_id"] = max_entry_id
        ls["max_wus_id"] = max_wus_id
        ls["sf_sig"] = sf_sig
        return
    if (
        sf_sig != ls["sf_sig"]
        or max_entry_id < ls["max_id"]
    ):
        # NOTE (#271 §9): max_wus_id is deliberately NOT a full-clear trigger. The
        # cached per-(project, week) aggregates are session_entries-only; a WUS
        # bump (a `record-usage` write) changes only the attribution denominator,
        # which the whole-envelope memo (_PROJECTS_ENV_MEMO, still keyed on
        # max_wus_id) recomputes fresh on its own miss. Reusing this cost cache
        # across a WUS bump is byte-identical. Do NOT re-add
        # `max_wus_id != ls["max_wus_id"]` here.
        _PROJECTS_ENV_WEEK_CACHE.clear()
        _PROJECTS_ENV_WEEK_TOTALS.clear()
        ls["max_id"] = max_entry_id
        ls["max_wus_id"] = max_wus_id
        ls["sf_sig"] = sf_sig
        return
    if max_entry_id > ls["max_id"]:
        wm = new_min_timestamp(cache_conn, ls["max_id"])
        if wm is not None:
            dirty = [
                wk
                for wk in list(_PROJECTS_ENV_WEEK_TOTALS)
                if dt.datetime.fromisoformat(wk) + dt.timedelta(days=7) >= wm
            ]
            for wk in dirty:
                _PROJECTS_ENV_WEEK_TOTALS.pop(wk, None)
                for key in [k for k in _PROJECTS_ENV_WEEK_CACHE if k[1] == wk]:
                    del _PROJECTS_ENV_WEEK_CACHE[key]
        ls["max_id"] = max_entry_id
        ls["max_wus_id"] = max_wus_id
        ls["sf_sig"] = sf_sig
