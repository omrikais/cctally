"""5-hour-window command family.

Holds the three 5h commands — `cmd_blocks`, `cmd_five_hour_blocks`,
`cmd_five_hour_breakdown` — their family-local helpers, the shared 5h
recorded-window resolution layer (`_load_recorded_five_hour_windows`,
`_select_non_overlapping_recorded_windows`, `_maybe_swap_active_block_to_canonical`,
`_resolve_block_selector`, `_CANONICAL_WEIGHT_THRESHOLD`), AND
`_backfill_five_hour_blocks` — the one-shot historical backfill of
`five_hour_blocks` from `weekly_usage_snapshots` (idempotent via
`UNIQUE(five_hour_window_key)` + `INSERT OR IGNORE`, `BEGIN IMMEDIATE` per
#87 — `tests/test_stats_db_busy_timeout.py` reads its source from this file;
`five_hour_milestones` is never backfilled, write-once gotcha).

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). This module
references the bin/cctally RE-EXPORTED names of every library kernel it
needs (`BLOCK_DURATION`, `_canonical_5h_window_key`, `_render_blocks_table`,
`build_blocks_view`, …) — NOT the `_lib_*` module objects — so NO qualified
`_lib_*` import is required; every such name is reached via the call-time
`_cctally()` accessor so test monkeypatches through `cctally`'s namespace
are preserved (spec §3.1). The accessor is bound to ``_c`` (not the usual
``c``) here because several moved functions already use ``c`` as a real
``for c in ...`` loop variable over ``sqlite3.Row`` rows — binding the
accessor to ``c`` would shadow the module after the loop. The four
``_cctally_core`` kernel symbols this module needs at runtime (``open_db``,
``_command_as_of``, ``eprint``, ``parse_iso_datetime``) are honest-imported
(kernel-extraction invariant — ``tests/test_kernel_extraction_invariants.py``),
not reached via ``_c``.

bin/cctally re-exports EVERY moved symbol (eager): the parser resolves
`c.cmd_blocks` / `c.cmd_five_hour_blocks` / `c.cmd_five_hour_breakdown`;
the dashboard reaches `sys.modules["cctally"]._load_recorded_five_hour_windows`;
`_lib_render` reaches `sys.modules["cctally"]._format_block_start`; tests
retrieve `ns["cmd_blocks"]` / `ns["_resolve_block_selector"]` /
`ns["_maybe_swap_active_block_to_canonical"]`.

Spec: docs/superpowers/specs/2026-05-30-extract-five-hour-statusline-cmd-design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys

import _lib_accounts  # pure stdlib kernel; UNATTRIBUTED sentinel (#341)
import _lib_rebuild_progress  # #997 D9 progress ticks (stdlib only)
from _lib_five_hour import latest_five_hour_reading
from _cctally_core import _command_as_of, eprint, now_utc_iso, open_db, parse_iso_datetime


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


def _resolve_block_selector(
    conn: sqlite3.Connection,
    *,
    block_start: str | None,
    ago: int | None,
    account_key: "str | None" = None,
) -> dict | None:
    """Resolve a five-hour-breakdown selector to one ``five_hour_blocks`` row.

    Returns a dict-mapped ``sqlite3.Row`` (or ``None`` if no block matches).
    Raises ``ValueError`` on conflicting / malformed input.

    ``account_key`` (#341, spec §3): ``None`` = merged / byte-stable; a real key /
    ``unattributed`` scopes selection to that account's blocks — required because
    ``five_hour_window_key`` is shared across accounts (one physical window can
    own one block per account).

    Selector rules (spec §3.1):
      * Both ``None`` -> most-recent block (highest ``block_start_at``).
      * ``ago=N`` -> the (N+1)-th most-recent block; ``N=0`` == default.
      * ``block_start=<iso>`` -> parse as ISO 8601; naive forms are UTC.
        Match by computing
        ``_canonical_5h_window_key(parsed_epoch + 5*3600)`` and looking up
        ``five_hour_window_key``.
      * ``block_start`` + ``ago`` together -> ``ValueError``.
      * Date-only ``block_start`` (no ``T``/space separator) -> ``ValueError``
        (cannot derive a unique canonical 5h key from a date alone).
    """
    _c = _cctally()
    _acct_pred = "" if account_key is None else " AND account_key = ?"
    _acct_p: tuple = () if account_key is None else (account_key,)
    if block_start is not None and ago is not None:
        raise ValueError(
            "--block-start and --ago are mutually exclusive"
        )

    if block_start is not None:
        # Reject date-only forms — can't compute a unique canonical key.
        if "T" not in block_start and " " not in block_start:
            raise ValueError(
                f"--block-start requires HH:MM (got '{block_start}')"
            )
        try:
            parsed = dt.datetime.fromisoformat(block_start)
        except ValueError as e:
            raise ValueError(f"--block-start: {e}") from e
        # Naive -> UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        resets_epoch = int(parsed.timestamp()) + 5 * 3600
        key = _c._canonical_5h_window_key(resets_epoch)
        row = conn.execute(
            "SELECT * FROM five_hour_blocks WHERE five_hour_window_key = ?"
            + _acct_pred,
            (key,) + _acct_p,
        ).fetchone()
        return dict(row) if row else None

    # Default or --ago: order DESC by block_start_at, take the (ago or 0)-th.
    offset = int(ago) if ago is not None else 0
    if offset < 0:
        raise ValueError(f"--ago must be non-negative (got {ago})")
    row = conn.execute(
        "SELECT * FROM five_hour_blocks"
        + (" WHERE account_key = ?" if account_key is not None else "")
        + " ORDER BY block_start_at DESC, id DESC LIMIT 1 OFFSET ?",
        _acct_p + (offset,),
    ).fetchone()
    return dict(row) if row else None


# Weight overlay applied per canonical (``five_hour_blocks``) row by
# ``_load_recorded_five_hour_windows``: ``counts[snapped] += _CANONICAL_WEIGHT_THRESHOLD``.
# Gives canonical anchors dominant weight inside the
# ``_select_non_overlapping_recorded_windows`` DP, so any non-canonical
# phantom adjacent to a canonical anchor loses on weight comparison. NOT
# used as a provenance check — the selector takes an explicit
# ``canonical_anchors`` set from the loader for the force-restore bypass
# (issue #116 review follow-up: raw-only buckets with bulk-imported /
# high-frequency snapshot histories can also accumulate >= 1000 weight,
# so the threshold conflates provenance with support count).
_CANONICAL_WEIGHT_THRESHOLD = 1000


def _select_non_overlapping_recorded_windows(
    items: list[tuple[dt.datetime, int]],
    *,
    canonical_anchors: set[dt.datetime] | None = None,
) -> list[dt.datetime]:
    """Pick the max-weight subset of recorded ``R`` values that respect
    the 5h non-overlap constraint, with canonical anchors guaranteed
    to survive.

    Anthropic 5h windows cannot truly overlap: the next window only
    opens once the previous one resets, so consecutive real ``R``
    values are always at least ``BLOCK_DURATION`` apart. When two
    recorded ``R`` values fall within ``BLOCK_DURATION`` of each other
    (e.g. a 2-row anomaly captured during a brief status-line glitch
    sitting next to the 78-row real reset), at most one is genuine.
    This solves weighted interval scheduling where each ``R`` "owns"
    its preceding 5h window and the weight is the number of supporting
    snapshots: the subset that maximizes total support wins. Tie-break
    in the take branch favors including more ``R`` values.

    Canonical bypass (issue #116): any ``R`` passed in ``canonical_anchors``
    came from the authoritative ``five_hour_blocks`` rollup.
    ``maybe_update_five_hour_block`` already deduped via
    ``_canonical_5h_window_key`` pre-insert, so two canonical rows are
    by definition non-overlapping physically — they only appear "in
    conflict" here when their 10-min-floored keys land less than
    ``BLOCK_DURATION`` apart, which happens at every real reset
    boundary when Anthropic's ``resets_at`` jitters sub-second across
    the boundary (e.g. OLD ``R=09:00:01Z`` floors to ``09:00``, NEW
    ``R=13:59:59Z`` floors to ``13:50`` — 4h 50m floored-distance for
    a genuinely-adjacent block pair). The DP still runs over the full
    item set so non-canonical phantoms next to a canonical anchor get
    dropped by weight comparison; the canonical-bypass only force-
    restores anchors the caller marked canonical, never adds back a
    raw-only phantom (even one whose raw weight ≥ ``_CANONICAL_WEIGHT_THRESHOLD``
    — the v1.20.3 fix used weight as a provenance proxy, which the
    review correctly flagged as conflating support count with provenance).

    Args:
      items: ``(R, support_count)`` pairs.
      canonical_anchors: explicit set of ``R`` values sourced from
        ``five_hour_blocks``. Any present in ``items`` is guaranteed to
        appear in the result, even if the DP dropped it on the 5h
        non-overlap constraint. ``None`` / empty set = pure DP behavior
        (no bypass).

    Returns:
      Sorted ascending list of selected ``R`` values.
    """
    if not items:
        return []
    items_sorted = sorted(items, key=lambda x: x[0])
    n = len(items_sorted)
    opt = [0] * n
    chose = [False] * n

    def _last_compatible(i: int) -> int:
        """Index of the latest j < i with items_sorted[j].R <= R_i - 5h."""
        _c = _cctally()
        cutoff = items_sorted[i][0] - _c.BLOCK_DURATION
        lo, hi, j = 0, i - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if items_sorted[mid][0] <= cutoff:
                j = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return j

    for i in range(n):
        skip = opt[i - 1] if i > 0 else 0
        j = _last_compatible(i)
        take = items_sorted[i][1] + (opt[j] if j >= 0 else 0)
        if take >= skip:
            opt[i], chose[i] = take, True
        else:
            opt[i], chose[i] = skip, False

    chosen: list[dt.datetime] = []
    i = n - 1
    while i >= 0:
        if chose[i]:
            chosen.append(items_sorted[i][0])
            i = _last_compatible(i)
        else:
            i -= 1
    chosen.reverse()
    # Canonical bypass: force-restore any canonical anchor the DP dropped
    # (issue #116). Intersect with items' keys so a caller passing anchors
    # outside the item set can't corrupt the result.
    if canonical_anchors:
        items_keys = {R for R, _ in items_sorted}
        present_canonical = canonical_anchors & items_keys
        if present_canonical and not present_canonical.issubset(chosen):
            return sorted(set(chosen) | present_canonical)
    return chosen


def _load_recorded_five_hour_windows(
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> tuple[
    list[dt.datetime],
    dict[dt.datetime, dt.datetime],
    dict[dt.datetime, tuple[dt.datetime, dt.datetime]],
]:
    """Return sorted, UTC-aware recorded ``five_hour_resets_at`` values
    that anchor real 5h windows in ``[range_start, range_end]``.

    Returns a 3-tuple ``(selected, block_start_overrides, canonical_intervals)``:

      * ``selected``: list of 10-min-floored ``R`` anchors (sorted),
        each representing one accepted canonical 5h window. Same shape
        as before — drives `_group_entries_into_blocks`'s
        ``recorded_windows=`` kwarg.

      * ``block_start_overrides``: ``{R_floored → block_start_at_utc}``
        for credit-truncated anchors (Bug J). When a credit moment
        falls inside a canonical block's overlap with the next block,
        the earlier ``R`` is replaced by the credit moment (floored to
        10 min) and the original ``block_start_at`` is recorded here so
        the renderer keeps the real display start.

      * ``canonical_intervals``: ``{R_floored → (bs_utc, rs_utc)}``
        carrying the **exact** ``(block_start_at, five_hour_resets_at)``
        for every selected anchor that has a canonical
        ``five_hour_blocks`` row. ``rs_utc`` is the un-floored reset
        moment (jitter intact), ``bs_utc`` is the API-derived block
        start normalized to UTC. Drives `_group_entries_into_blocks`'s
        partition predicate AND Phase 1.5 block construction
        (issue #76 — 10-min-floor partition trap). Anchors with no
        canonical row (legacy weekly-snapshots-only) are absent from
        the map and the partitioner falls back to ``(R - 5h, R)``.
        Credit-truncated anchors land here with the truncated upper
        bound (``rs = effective_reset``) and the override-supplied
        ``bs`` (the real pre-truncation block start).

    Two sources contribute to the merged anchor set:

    1. ``weekly_usage_snapshots.five_hour_resets_at`` — every
       record-usage tick stores the API-derived reset moment here. The
       count of supporting rows weights each anchor (low-count anchors
       are downvoted in ``_select_non_overlapping_recorded_windows``).

    2. ``five_hour_blocks.five_hour_resets_at`` — the canonical
       API-anchored rollup table. Each row represents ONE accepted 5h
       window after ``maybe_update_five_hour_block`` has merged jittered
       reset values via ``_canonical_5h_window_key``. These are the
       authoritative anchors; we count them with a heavy weight (1000)
       so they always dominate over jittered raw snapshot values when
       both sources see the same physical window. Without this source,
       ``cctally blocks`` falls back to the heuristic anchor for the
       ACTIVE row whenever the most recent
       ``weekly_usage_snapshots.five_hour_resets_at`` value disagrees
       with the canonical anchor — Bug C in v1.7.2 round 3. Tied
       windows (jitter within 10-minute floor) collapse to the same
       key and the canonical weight dominates.

    Each value is parsed as ISO-8601 (the storage format produced by
    ``cmd_record_usage``) and normalized to UTC. Naive datetimes are
    treated as already-UTC. Values are floored to the previous
    10-minute boundary (jitter tolerance) and grouped — each bucket's
    weight is the count of supporting snapshots. Finally, when two
    floored ``R`` values fall within ``BLOCK_DURATION`` of each other,
    ``_select_non_overlapping_recorded_windows`` resolves the conflict
    by keeping the better-supported one (real Anthropic 5h windows do
    not overlap; a low-row-count ``R`` adjacent to a high-row-count
    one is almost always a transient bad reading from the status line).

    Returns ``[]`` when the underlying DB can't be opened, the query
    fails, or the resulting row set is empty. This keeps ``cmd_blocks``
    on the pre-existing heuristic path whenever recorded-anchor data is
    unavailable.
    """
    _c = _cctally()
    try:
        with open_db() as conn:
            rows = conn.execute(
                "SELECT five_hour_resets_at, five_hour_window_key "
                "FROM weekly_usage_snapshots "
                "WHERE five_hour_resets_at IS NOT NULL "
                "  AND five_hour_resets_at >= ? "
                "  AND five_hour_resets_at <= ?",
                (range_start.isoformat(), range_end.isoformat()),
            ).fetchall()
            # Canonical API-anchored windows from the rollup table.
            # Heavy-weight (1000 per row) so they always dominate over
            # any jittered raw-snapshot value sharing the same floored
            # 10-minute bucket. Wrapped in a defensive try in case the
            # five_hour_blocks table doesn't exist yet (very-old DB on
            # first open before the bootstrap migration ran).
            # Pull ``block_start_at`` alongside ``five_hour_resets_at``
            # so Bug J's overlap-truncation step (below) can preserve
            # the real display start for credit-truncated blocks.
            canonical_rows: list[Any] = []
            try:
                canonical_rows = conn.execute(
                    "SELECT five_hour_resets_at, block_start_at, "
                    "       five_hour_window_key "
                    "FROM five_hour_blocks "
                    "WHERE five_hour_resets_at IS NOT NULL "
                    "  AND five_hour_resets_at >= ? "
                    "  AND five_hour_resets_at <= ?",
                    (range_start.isoformat(), range_end.isoformat()),
                ).fetchall()
            except sqlite3.DatabaseError:
                canonical_rows = []
            # In-place credit events — used by Bug J to detect canonical
            # block overlaps that should be resolved by truncating the
            # earlier block at the credit moment (rather than dropping
            # one via _select_non_overlapping_recorded_windows, which
            # leaves the dropped block's entries unanchored and
            # rendered as a phantom heuristic "~" row).
            credit_moments: list[dt.datetime] = []
            try:
                credit_rows = conn.execute(
                    "SELECT effective_reset_at_utc "
                    "FROM week_reset_events "
                    "WHERE old_week_end_at = effective_reset_at_utc"
                ).fetchall()
                for c in credit_rows:
                    raw = c["effective_reset_at_utc"]
                    try:
                        d = dt.datetime.fromisoformat(str(raw))
                    except ValueError:
                        continue
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=dt.timezone.utc)
                    else:
                        d = d.astimezone(dt.timezone.utc)
                    credit_moments.append(d)
                # Issue #44: the inner-loop break below latches onto the
                # first credit in [next_bs, rs]. With two credits inside
                # the same pre-credit canonical 5h window, the wrong one
                # (the later one) wins when SQLite returns rows in
                # insertion order rather than time order — collapsing
                # two distinct truncated anchors onto the same floored
                # bucket and silently dropping one via override-map
                # overwrite. Sort once so the break consistently picks
                # the EARLIEST credit, which is the one that actually
                # ended the earlier block (its floor equals the next
                # block's block_start_at by construction).
                credit_moments.sort()
            except sqlite3.DatabaseError:
                credit_moments = []
    except (sqlite3.DatabaseError, OSError):
        # OSError covers ensure_dirs() failures (read-only FS, permission
        # denied on parent dir) that propagate from open_db() before any
        # SQL runs. Either way, fall back to the heuristic anchor path.
        return [], {}, {}
    # issue #201: identify each 5h window by its canonical, jitter-collapsed
    # ``five_hour_window_key`` (the 10-min-floored epoch the record path
    # already stored via the anchored ``_canonical_5h_window_key`` reuse)
    # instead of re-flooring the raw ``five_hour_resets_at`` string. A
    # 1-second reset jitter straddling a 10-minute floor boundary
    # (``20:39:59`` vs ``20:40:00``) floors to two different buckets and
    # would otherwise fork one physical window into two overlapping blocks
    # — the exact split this column exists to prevent. Falls back to the
    # pure floor for legacy rows whose key wasn't backfilled (``open_db``
    # backfills NULL keys before this query runs, so this is defensive).
    def _bucket_dt(window_key: Any, resets_dt: dt.datetime) -> dt.datetime:
        if window_key is not None:
            return dt.datetime.fromtimestamp(int(window_key), dt.timezone.utc)
        return _c._floor_to_ten_minutes(resets_dt)

    counts: dict[dt.datetime, int] = {}
    for row in rows:
        raw = row["five_hour_resets_at"] if hasattr(row, "keys") else row[0]
        wkey = row["five_hour_window_key"] if hasattr(row, "keys") else row[1]
        if raw is None:
            continue
        try:
            d = dt.datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        else:
            d = d.astimezone(dt.timezone.utc)
        snapped = _bucket_dt(wkey, d)
        counts[snapped] = counts.get(snapped, 0) + 1
    # Overlay canonical rollup anchors at heavy weight. Same flooring
    # rule so a jittered raw value (e.g. 17:48Z) and its canonicalized
    # rollup (e.g. 17:50Z) collapse into the same bucket; without that
    # the high-weight canonical entry would create a NEW bucket and
    # both would be reported as separate windows, then
    # `_select_non_overlapping_recorded_windows` (5h-disjoint
    # invariant) would drop the lower-weight one — but the wrong
    # one would win when jitter exceeds 10 minutes.
    #
    # Bug J (v1.7.2 round-5): collect canonical (block_start, R) pairs
    # so we can detect in-place-credit overlaps before flattening into
    # the weighted scheduler. When two canonical 5h blocks overlap AND
    # an in-place credit event falls inside the overlap, truncate the
    # EARLIER block's R to the credit moment (floored to 10 min so it
    # collapses with any same-bucket raw-snapshot value). The
    # truncated R keeps both blocks visible — without this fix the
    # earlier block's entries are silently rendered as a phantom
    # heuristic "~" row by `_group_entries_into_blocks`.
    # issue #201: each triple is ``(wkey_dt, bs, rs)`` — ``wkey_dt`` is the
    # canonical window_key (decoded to a UTC datetime) and is the SAME
    # bucket identity used for the raw-snapshot counts above, so the heavy
    # canonical overlay always lands in the same bucket as its supporting
    # raw rows (never a jitter-split sibling bucket).
    canonical_pairs: list[tuple[dt.datetime, dt.datetime, dt.datetime]] = []
    for row in canonical_rows:
        rs_raw = row["five_hour_resets_at"] if hasattr(row, "keys") else row[0]
        bs_raw = row["block_start_at"]      if hasattr(row, "keys") else row[1]
        wkey   = row["five_hour_window_key"] if hasattr(row, "keys") else row[2]
        if rs_raw is None or bs_raw is None:
            continue
        try:
            rs = dt.datetime.fromisoformat(str(rs_raw))
            bs = dt.datetime.fromisoformat(str(bs_raw))
        except ValueError:
            continue
        if rs.tzinfo is None:
            rs = rs.replace(tzinfo=dt.timezone.utc)
        else:
            rs = rs.astimezone(dt.timezone.utc)
        if bs.tzinfo is None:
            bs = bs.replace(tzinfo=dt.timezone.utc)
        else:
            bs = bs.astimezone(dt.timezone.utc)
        canonical_pairs.append((_bucket_dt(wkey, rs), bs, rs))
    canonical_pairs.sort(key=lambda p: p[1])

    # issue #76: canonical_intervals maps every floored R -> its EXACT
    # (block_start_at, five_hour_resets_at) — both UTC, rs un-floored
    # (jitter intact). Drives the partition predicate AND Phase 1.5
    # block construction in `_group_entries_into_blocks` so floor-band
    # entries (timestamps in [floor(R), R)) land in the right bucket
    # and the displayed window matches Anthropic's actual interval.
    # Built before the credit-truncation loop below so that loop can
    # rewrite the upper bound in-place (truncated R replaces rs).
    canonical_intervals: dict[
        dt.datetime, tuple[dt.datetime, dt.datetime]
    ] = {}
    for wkey_dt, bs, rs in canonical_pairs:
        canonical_intervals[wkey_dt] = (bs, rs)

    # Detect overlap-with-credit and replace the earlier R with a
    # credit-truncated anchor. The (anchor → real_block_start) map is
    # returned alongside the anchor list so the renderer can show the
    # real block_start_at on the display row (instead of the default
    # R - 5h, which would be hours earlier for a 2h-truncated block).
    block_start_overrides: dict[dt.datetime, dt.datetime] = {}
    truncated_pairs: list[tuple[dt.datetime, dt.datetime, dt.datetime]] = []
    for i, (wkey_dt, bs, rs) in enumerate(canonical_pairs):
        anchor_key = wkey_dt  # canonical window_key identity (issue #201)
        truncated_R = rs
        if i + 1 < len(canonical_pairs):
            _next_wk, next_bs, _next_rs = canonical_pairs[i + 1]
            if rs > next_bs:  # overlap with next block
                # Look for a credit moment inside [next_bs, rs] — the
                # part of the earlier block that overlaps the next.
                for cm in credit_moments:
                    if next_bs <= cm <= rs:
                        cm_floored = _c._floor_to_ten_minutes(cm)
                        # Only truncate if cm is strictly inside the
                        # earlier block; otherwise leave R alone and
                        # let `_select_non_overlapping_recorded_windows`
                        # drop one via its weight-tiebreaker.
                        if bs < cm_floored < rs:
                            truncated_R = cm_floored
                            anchor_key = cm_floored
                            block_start_overrides[cm_floored] = bs
                            # Rewrite canonical_intervals under the
                            # truncated key. issue #76: the partitioner
                            # reads canonical_intervals for the exact
                            # bs/rs; the truncated entry must reflect the
                            # credit-shifted upper bound (cm_floored) AND
                            # the real bs (the override) so partition +
                            # Phase 1.5 render the credit-shortened block
                            # consistently. issue #201: the original
                            # entry is keyed by the canonical window_key,
                            # so pop under ``wkey_dt`` (not floor(rs)).
                            canonical_intervals.pop(wkey_dt, None)
                            canonical_intervals[cm_floored] = (
                                bs, cm_floored,
                            )
                            break
        truncated_pairs.append((anchor_key, bs, truncated_R))

    # Truncated anchors are credit-adjusted and known-good; bypass the
    # `_select_non_overlapping_recorded_windows` weighted scheduler for
    # them (the scheduler treats every R as the END of a fixed 5h
    # window and would see a truncated R conflicting with the adjacent
    # canonical block one slot earlier — e.g. truncated R=17:50 would
    # collide with the prior block's R=15:50 even though their REAL
    # intervals are [15:50, 17:50] and [10:50, 15:50] respectively —
    # adjacent, not overlapping). Add their R directly to the selector
    # input weight (so jittered same-bucket raw values still collapse)
    # but skip them when computing the overlap-safe subset.
    truncated_anchors: set[dt.datetime] = set()
    for anchor_key, _bs, _rs in truncated_pairs:
        # ``anchor_key`` is already a floored canonical key — the window_key
        # for an untruncated anchor (issue #201) or the credit-floored key
        # for a truncated one — and the override map is keyed by it
        # directly, so the legacy floor-relocation dance is no longer
        # needed. Identify truncated anchors by membership in the override
        # map (only credit-truncated entries land there).
        if anchor_key in block_start_overrides:
            truncated_anchors.add(anchor_key)
        counts[anchor_key] = counts.get(anchor_key, 0) + _CANONICAL_WEIGHT_THRESHOLD

    non_truncated_items = [
        (a, w) for a, w in counts.items() if a not in truncated_anchors
    ]
    # Pass canonical provenance explicitly: every key currently in
    # canonical_intervals came from a `five_hour_blocks` row (raw-only
    # buckets never land in this map). Subtract truncated_anchors because
    # those bypass the DP via the separate merge below — keeping them
    # out of canonical_anchors here is a no-op for correctness but
    # mirrors the same scope as non_truncated_items for clarity.
    canonical_anchors_for_dp = set(canonical_intervals.keys()) - truncated_anchors
    selected_non_truncated = _select_non_overlapping_recorded_windows(
        non_truncated_items,
        canonical_anchors=canonical_anchors_for_dp,
    )
    # Merge truncated anchors back in, sorted ascending. Their non-
    # overlap with the surrounding canonical blocks is guaranteed by
    # the credit-moment truncation: a truncated R sits strictly
    # between its real block_start (which equals the prior block's R)
    # and the next block's R.
    selected = sorted(
        list(selected_non_truncated) + list(truncated_anchors)
    )
    # Filter canonical_intervals down to selected anchors. Raw-only
    # anchors (selected via weekly_usage_snapshots but absent from
    # five_hour_blocks) stay out of the map; the partitioner falls
    # back to (R - 5h, R) for them. issue #76 / spec §1.1 D1.
    canonical_intervals = {
        R: canonical_intervals[R]
        for R in selected
        if R in canonical_intervals
    }
    return selected, block_start_overrides, canonical_intervals


def cmd_blocks(args: argparse.Namespace) -> int:
    """Show usage report grouped by 5-hour session blocks."""
    _c = _cctally()
    # -n/--session-length guard (#86 Session F). The flag is a documented
    # no-op (cctally blocks anchor to Anthropic's real 5h resets and are not
    # re-sizable), but a non-positive value still errors for drop-in fidelity
    # with ccusage's "Session length must be a positive number". Runs first,
    # before any data load — matches ccusage's command-flow ordering.
    if getattr(args, "session_length", 5.0) <= 0:
        eprint("blocks: session length must be a positive number")
        return 1

    config = _c._load_claude_config_for_args(args)
    _c._bridge_z_into_tz(args, config)
    tz = _c.resolve_display_tz(args, config)
    args._resolved_tz = tz

    now_utc = _command_as_of()
    # Parse --since / --until into datetime range. Session A (spec §7.1.1)
    # routes through the centralized dual-form helper so YYYY-MM-DD also
    # works and the error message matches the other in-scope cmds.
    if args.since:
        try:
            since_date = _c._parse_dual_form_date(args.since, "--since")
        except ValueError:
            return 1
        range_start = since_date.replace(tzinfo=dt.timezone.utc)
    else:
        # Default: all available data (matches ccusage behavior)
        range_start = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)

    if args.until:
        try:
            until_date = _c._parse_dual_form_date(args.until, "--until")
        except ValueError:
            return 1
        # End of that day
        range_end = until_date.replace(
            hour=23, minute=59, second=59, microsecond=999999,
            tzinfo=dt.timezone.utc,
        )
    else:
        range_end = now_utc

    # Collect all entries
    all_entries = _c.get_entries(range_start, range_end)

    _c._emit_debug_samples_if_set(
        args, all_entries, command_label="blocks",
    )

    # Load recorded 5-hour reset timestamps. Widen both bounds by
    # BLOCK_DURATION: a window covers [R - 5h, R), so a reset R just
    # before ``range_start`` can still anchor entries near it, and a
    # reset R just after ``range_end`` (e.g. the active window when
    # range_end is wall-clock "now") can still anchor entries that fall
    # inside [range_start, range_end].
    recorded_windows, block_start_overrides, canonical_intervals = (
        _load_recorded_five_hour_windows(
            range_start - _c.BLOCK_DURATION, range_end + _c.BLOCK_DURATION,
        )
    )

    # Group into blocks via the view-model kernel (issue #56). The
    # heuristic-aware ``aggregated`` tuple holds the full Block list
    # (gaps included, oldest-first) — same shape the JSON / table
    # renderers expect. We materialize back to a list because
    # ``_maybe_swap_active_block_to_canonical`` mutates in-place.
    #
    # ``skip_rows=True`` (issue #60 review fix) opts out of the
    # dashboard-row construction inside ``build_blocks_view`` — the
    # per-block per-model enrichment that scans every entry per
    # non-gap block (O(B × N)). The CLI never reads ``view.rows``
    # (only ``view.aggregated`` here), so on large all-history blocks
    # runs we avoid quadratic-ish work we'd discard.
    view = _c.build_blocks_view(
        all_entries,
        now_utc=now_utc,
        recorded_windows=recorded_windows,
        block_start_overrides=block_start_overrides,
        canonical_intervals=canonical_intervals,
        range_start=range_start,
        range_end=range_end,
        display_tz=tz,
        mode=args.mode,
        skip_rows=True,
    )
    blocks = list(view.aggregated)

    # Bug E (v1.7.2 round-4): when the ACTIVE block is heuristic-anchored
    # but a canonical ``five_hour_blocks`` row exists for the current 5h
    # window key, swap the active block's times to the API-anchored
    # ``block_start_at`` / ``five_hour_resets_at`` and flip its anchor to
    # ``"recorded"`` so the renderer drops the ``~`` prefix. The
    # heuristic anchor can sit in a different 10-minute floor bucket
    # than the canonical anchor (e.g. 23:00 IDT vs 20:50 IDT — 130 min
    # apart), so round-3's anchor-overlay in
    # ``_load_recorded_five_hour_windows`` doesn't catch this case.
    # Match by the live 5h window key (the same key
    # ``cmd_five_hour_blocks`` would surface for the ACTIVE row) — falls
    # back to heuristic behavior whenever the canonical row is missing.
    #
    # Bug F (v1.7.2 round-5): pass ``all_entries`` so the swap also
    # re-aggregates token / cost totals over the canonical interval. The
    # heuristic block holds only entries from the heuristic anchor
    # onwards; the canonical block may start earlier and include 1-2h of
    # additional entries. Without re-aggregation the displayed window
    # said one thing and the cost said another (live data: window
    # 20:50→01:50 with $45 cost vs the real $128).
    _maybe_swap_active_block_to_canonical(
        blocks, all_entries, now=now_utc, mode=args.mode,
        competing_windows=_ownership_windows_from_recorded(
            recorded_windows, block_start_overrides, canonical_intervals,
        ),
    )

    # ── Session F (#86): resolve token limit, then filter ────────────────
    # Auto-max baseline over ALL blocks (before --recent/--active filtering),
    # matching ccusage's maxTokensFromAll.
    max_completed = _c._max_completed_block_tokens(blocks)
    token_limit = _c._parse_blocks_token_limit(
        getattr(args, "token_limit", None), max_completed
    )
    # ``token_limit_explicit`` is the resolved limit ONLY when -t was passed
    # (any value incl. "max"); the implicit default leaves it None so the
    # box's Token Limit Status sub-block + the JSON tokenLimitStatus key are
    # omitted (ccusage `if (tokenLimit != null)` gate).
    token_limit_explicit = (
        token_limit if getattr(args, "token_limit", None) is not None else None
    )
    auto_max = getattr(args, "token_limit", None) in (None, "", "max")
    if auto_max and token_limit and not args.json:
        # ccusage parity: logger.info → stdout (Codex F1). Suppressed under
        # --json (ccusage sets logger.level=0), so --json goldens stay stable.
        print(f"Using max tokens from previous sessions: {_c._fmt_num(token_limit)}")

    if getattr(args, "recent", False):
        cutoff = now_utc - dt.timedelta(days=3)
        blocks = [b for b in blocks if b.start_time >= cutoff or b.is_active]

    if getattr(args, "active", False):
        blocks = [b for b in blocks if b.is_active and not b.is_gap]
        if not blocks:
            if args.json:
                print(json.dumps(_c.stamp_schema_version(
                    {"blocks": [], "message": "No active block"}), indent=2))
            else:
                print("No active session block found.")
            return 0

    if args.json:
        print(_c._blocks_to_json(blocks, token_limit_status_limit=token_limit_explicit))
        return 0

    if getattr(args, "active", False) and len(blocks) == 1:
        print(_c._render_active_block_box(
            blocks[0], now=now_utc, tz=tz,
            token_limit_explicit=token_limit_explicit,
            color=_c._supports_color_stdout(), unicode_ok=_c._supports_unicode_stdout(),
        ))
        return 0

    # Table output. Session A (spec §7.6.1; Review-A P2-B): thread
    # --compact through so the renderer's scale-down branch fires
    # regardless of terminal width when the flag is set. Session F: thread
    # the resolved token_limit so an explicit -t keys the %/REMAINING/
    # PROJECTED surface (the default path passes the same auto-max the
    # renderer computed internally, so it stays byte-identical).
    print(_c._render_blocks_table(
        blocks, breakdown=args.breakdown, now=now_utc, tz=tz,
        compact=getattr(args, "compact", False), token_limit=token_limit,
    ))
    return 0


def _ownership_windows_from_recorded(
    recorded_windows,
    block_start_overrides=None,
    canonical_intervals=None,
) -> "list[Any]":
    """The `_load_recorded_five_hour_windows` result as ownership windows.

    Resolves each anchor's interval exactly as `_group_entries_into_blocks`
    does — canonical interval first, then the override-supplied start, then
    the legacy ``(R - 5h, R)`` shape — so a consumer outside the grouping
    pass reasons about the same intervals it did.
    """
    _c = _cctally()
    canonical_intervals = canonical_intervals or {}
    block_start_overrides = block_start_overrides or {}
    out: list[Any] = []
    for anchor in recorded_windows or ():
        if anchor in canonical_intervals:
            start, reset = canonical_intervals[anchor]
        else:
            start = block_start_overrides.get(
                anchor, anchor - dt.timedelta(hours=5),
            )
            reset = anchor
        out.append(_c.OwnedWindow(key=anchor, start=start, reset=reset))
    return out


def _maybe_swap_active_block_to_canonical(
    blocks: list[Any],
    all_entries: list[Any],
    *,
    now: dt.datetime,
    mode: str = "auto",
    competing_windows,
) -> None:
    """In-place swap of an ACTIVE heuristic block to its API-anchored
    canonical window — timestamps AND token/cost totals.

    Looks up the live ``five_hour_window_key`` from the most recent
    ``weekly_usage_snapshots`` row, then joins to ``five_hour_blocks``
    for that key. If found AND the canonical window still contains
    ``now`` (resets_at > now), rewrites the active block to span the
    canonical ``[block_start_at, five_hour_resets_at)`` interval and
    flips ``anchor`` to ``"recorded"``. Token / cost totals are
    re-aggregated from ``all_entries`` filtered to that interval via
    ``_aggregate_block`` — the canonical window may contain 1-2h more
    activity than the heuristic grouping did, so the cost shown next
    to the swapped timestamps stays consistent with them (Bug F).

    No-op when:
      - No block is active (no ``is_active`` and not gap).
      - The active block's anchor is already ``"recorded"``.
      - No live snapshot exists, or the snapshot's ``five_hour_window_key``
        is NULL.
      - No canonical ``five_hour_blocks`` row matches the live key.
      - The canonical window's ``five_hour_resets_at`` is already in
        the past relative to ``now`` (canonical block is closed; the
        heuristic block is genuinely the current activity).

    Surgical helper called once from ``cmd_blocks`` after grouping.
    """
    _c = _cctally()
    # Find the active (non-gap, heuristic) block — there's at most one.
    active_idx = None
    for i, b in enumerate(blocks):
        if not b.is_gap and b.is_active:
            active_idx = i
            break
    if active_idx is None or blocks[active_idx].anchor != "heuristic":
        return
    active = blocks[active_idx]
    try:
        with open_db() as conn:
            snap = conn.execute(
                "SELECT five_hour_window_key FROM weekly_usage_snapshots "
                "WHERE five_hour_window_key IS NOT NULL "
                "ORDER BY captured_at_utc DESC, id DESC LIMIT 1"
            ).fetchone()
            if snap is None or snap["five_hour_window_key"] is None:
                return
            key = int(snap["five_hour_window_key"])
            row = conn.execute(
                "SELECT block_start_at, five_hour_resets_at "
                "FROM five_hour_blocks WHERE five_hour_window_key = ? "
                "LIMIT 1",
                (key,),
            ).fetchone()
    except (sqlite3.DatabaseError, OSError):
        return
    if row is None:
        return
    try:
        block_start = parse_iso_datetime(
            row["block_start_at"], "five_hour_blocks.block_start_at"
        )
        block_end = parse_iso_datetime(
            row["five_hour_resets_at"], "five_hour_blocks.five_hour_resets_at"
        )
    except ValueError:
        return
    # Normalize to UTC for stable comparisons (block_start_at can carry
    # the host-local offset; five_hour_resets_at is UTC).
    block_start_utc = block_start.astimezone(dt.timezone.utc)
    block_end_utc = block_end.astimezone(dt.timezone.utc)
    # If the canonical window has already ended, don't displace the
    # heuristic active block — the canonical block is closed and the
    # heuristic anchor reflects real ongoing activity in a later window.
    if block_end_utc <= now.astimezone(dt.timezone.utc):
        return
    # Re-aggregate the entries this canonical window OWNS. Build a fresh
    # Block via ``_build_activity_block`` so every total stays in one code
    # path — no field-by-field assignment that could drift if the dataclass
    # grows new fields. Thread the caller's ``mode`` so the active block's
    # cost honors --mode like the main grouping (Session C / Codex F1).
    #
    # #751a: selection is `partition_entries_by_owner` over the competing
    # windows the caller grouped with, not a `block_start <= ts < block_end`
    # re-filter. After a reset shift the canonical interval overlaps its
    # predecessor's, and the re-filter re-priced the predecessor's entries
    # here even though grouping had already assigned them.
    target = _c.OwnedWindow(
        key=key, start=block_start_utc, reset=block_end_utc,
    )
    ownership = [target] + [
        w for w in competing_windows
        if w.key != target.key
        and (w.start, w.reset) != (target.start, target.reset)
    ]
    owned, _unowned = _c.partition_entries_by_owner(all_entries, ownership)
    canonical_entries = owned[target.key]
    rebuilt = _c._build_activity_block(
        canonical_entries,
        block_start_utc,
        block_end_utc,
        now.astimezone(dt.timezone.utc),
        mode,
        anchor="recorded",
    )
    blocks[active_idx] = rebuilt


def _format_block_start(iso: str, tz: "ZoneInfo | None") -> str:
    """Format a ``block_start_at`` ISO timestamp per the resolved tz.

    Used by both ``cmd_five_hour_blocks`` and ``cmd_five_hour_breakdown``.
    Renders as ``YYYY-MM-DD HH:MM <SUFFIX>`` where the suffix is the
    zone label per ``display_tz_label``. Naive inputs are treated as
    UTC; ``tz=None`` means "host-local via bare astimezone()".

    The displayed time is rounded to the nearest 10-minute boundary to
    normalize Anthropic reset-capture jitter (e.g. a :39 recorded reset
    renders as :40). The stored ``block_start_at`` is unchanged.
    """
    _c = _cctally()
    parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return _c.format_display_dt(
        _c._round_to_ten_minutes(parsed), tz,
        fmt="%Y-%m-%d %H:%M", suffix=True,
    )


def _format_hhmm_in_tz(iso: str, tz: "ZoneInfo | None") -> str:
    """Render the HH:MM portion of an ISO timestamp in the resolved tz.

    Mirrors ``_format_block_start``'s tz resolution so paired start/end
    cells in the same row stay in the same zone. Naive inputs are
    treated as UTC; ``tz=None`` means host-local. No suffix.

    Rounded to the nearest 10-minute boundary for the same reset-jitter
    normalization as ``_format_block_start`` (the paired start/end cells
    must round together or a 5h window renders as 4h59m/5h01m).
    """
    _c = _cctally()
    parsed = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return _c.format_display_dt(
        _c._round_to_ten_minutes(parsed), tz, fmt="%H:%M", suffix=False,
    )


def _block_is_active(
    block: dict,
    latest_window_key: int | None,
    now_utc: dt.datetime,
) -> bool:
    """Active = not flag-closed AND matches latest snapshot's window AND
    natural expiration hasn't passed yet.

    The third clause guards against the natural-expiration sweep in
    ``maybe_update_five_hour_block`` not having fired since the user
    last interacted (collector idle past the 5h reset). Without it,
    ``is_closed`` stays 0 AND the latest snapshot's window_key still
    references the now-expired block — so the simpler 2-clause
    predicate would mark an idle-past-reset block ACTIVE and the
    callers (cmd_five_hour_blocks, cmd_five_hour_breakdown) would
    overwrite ``seven_day_pct_at_block_end`` with stale data.

    ``block`` is a dict-mapped sqlite3.Row from ``five_hour_blocks``;
    ``latest_window_key`` comes from ``_latest_seven_day_and_window``;
    ``now_utc`` is a tz-aware UTC datetime (typically
    ``_command_as_of()`` so fixture-pinned harnesses stay deterministic).

    ``five_hour_resets_at`` is canonical UTC-Z (see ``now_utc_iso`` /
    ``_iso_z``), so a lexicographic ``>`` compare against
    ``_iso_z(now_utc)`` is chronological.
    """
    _c = _cctally()
    return (
        block.get("is_closed") == 0
        and block.get("five_hour_window_key") == latest_window_key
        and (block.get("five_hour_resets_at") or "") > _c._iso_z(now_utc)
    )


def _latest_seven_day_and_window(
    conn: sqlite3.Connection, *, account_key: "str | None" = None,
) -> tuple[float | None, int | None]:
    """Return ``(latest_7d_percent, latest_5h_window_key)`` from
    ``weekly_usage_snapshots``.

    TWO SELECTIONS OVER ONE ROW SET, and they differ deliberately (#834 S1,
    #835). Either or both elements may be ``None``. Used by
    ``cmd_five_hour_blocks`` and ``cmd_five_hour_breakdown`` to override
    ``seven_day_pct_at_block_end`` on the active row, and by ``_block_is_active``
    to decide which block is current.

    **The five-hour window key is HELD-INCLUSIVE**, decided rather than
    overlooked (#769 S11, #824). A `weekly_observation_held` row's five-hour
    fields are the writing tick's own, so it is the freshest statement of which
    window is current; excluding it would resolve an older window and report the
    live block closed. It is the key of the newest row that CARRIES a five-hour
    reading (#825/#832 spec 4.7 revision 7, ``latest_five_hour_reading``): a
    retired replica or a weekly-only reading can be the newest row, and its NULL
    key would report the live block closed and leave its weekly end stale.

    **The weekly value is the newest row's STORED value** (#840, spec section
    4.8). Held-inclusive, because a held row stores the effective weekly value
    and that is what a block's weekly end means. #835 filtered it through a
    retirement band (a value within a point of one a credit retired was skipped
    as a replay), which also withheld every genuine climb back to that value;
    the band is removed. Stale contributors are refused before publication
    (spec section 4.4), and a value already poisoned in a store is corrected by
    `db rederive`'s reviewed `retract`, never by a percentage heuristic.

    *Scope.* The value is the NEWEST row's, within its own
    ``(week_start_date, account_key)``; a read never reports another week's or
    another account's percentage.

    *Floor.* The read answers ``None`` when that row was captured BEFORE the
    latest credit floor in its scope, or carries an unparseable capture stamp
    inside a credited week: such a row cannot state the week's current
    effective weekly value, and its value is by construction the pre-credit
    reading. That state is unreachable through either production credit path:
    `record-credit` writes a post-credit synthetic snapshot at its own effective
    instant, and the automatic path's floor is derived from a capture that is
    itself in the store, so in both cases at least one row exists at or after
    the floor and it is the newest.

    An UNCREDITED week resolves no floor, and the read returns the newest row's
    value. A ``NULL`` weekly value answers ``None``.

    ``account_key`` (#341, spec §3): ``None`` = merged / byte-stable; a real key /
    ``unattributed`` scopes both selections to that account. The credit floor is
    resolved against the NEWEST ROW's own account and week, because a credit
    under one account bounds nothing under another.
    """
    _c = _cctally()
    acct_pred = "" if account_key is None else " WHERE account_key = ?"
    acct_p: tuple = () if account_key is None else (account_key,)
    try:
        cur = conn.execute(
            "SELECT weekly_percent, five_hour_window_key, captured_at_utc, "
            "       week_start_date, week_start_at, week_end_at, account_key "
            "FROM weekly_usage_snapshots" + acct_pred +
            " ORDER BY captured_at_utc DESC, id DESC",
            acct_p,
        )
        row = cur.fetchone()
        if row is None:
            return None, None
        # Axis 1: the five-hour window key, held-inclusive, from the newest
        # five-hour READING of the newest row's own account (#825/#832 spec
        # 4.7 revision 7, the one shared selection). The newest row can be a
        # retired replica or a weekly-only reading, which names no window.
        # Merged mode scopes the weekly walk to that account too, so an
        # account-blind selection here would pair another account's window
        # with this account's weekly value.
        reading = latest_five_hour_reading(conn, account_key=row["account_key"])
        key = None if reading is None else reading.window_key
        # Axis 2: the weekly value, the newest row's stored value, bounded by
        # the latest credit floor of its own week and account (#840).
        bands = _c._credit_retirement_bands(
            conn, week_start_date=row["week_start_date"],
            week_start_at=row["week_start_at"],
            week_end_at=row["week_end_at"],
            account_key=row["account_key"])
        floor = _c._latest_credit_floor_instant(bands)
        pct = row["weekly_percent"]
        if floor is not None:
            try:
                captured = parse_iso_datetime(
                    row["captured_at_utc"], "captured_at").timestamp()
            except (ValueError, TypeError):
                captured = None
            if captured is None or captured < floor:
                pct = None
    except sqlite3.DatabaseError:
        return None, None
    return (
        float(pct) if pct is not None else None,
        int(key) if key is not None else None,
    )


def _credit_aware_block_weekly_axes(conn, block, *, now_utc, cache=None):
    """A block's two stored weekly axes,
    ``(seven_day_pct_at_block_start, seven_day_pct_at_block_end)``.

    #840 (spec section 4.8). #835 replaced a value within a point of one a
    credit retired with ``None`` here, which also withheld every genuine climb
    back to that value; the retirement band is removed. Stale contributors are
    refused before publication (spec section 4.4), and a value already
    poisoned in a store is corrected by `db rederive`'s reviewed `retract`,
    never by a percentage heuristic. The name, the signature and the callers
    (`cmd_five_hour_blocks`, `cmd_five_hour_breakdown` and the dashboard's
    block envelope) are unchanged; ``conn``, ``now_utc`` and ``cache`` are no
    longer read."""
    return (block.get("seven_day_pct_at_block_start"),
            block.get("seven_day_pct_at_block_end"))


#: The six `five_hour_milestones` columns every surface already published, plus
#: the joined EFFECTIVE weekly value. Ordering is `captured_at_utc, id` — spec
#: §5.2's merged-stream order, which post-credit threshold repeats depend on.
_FIVE_HOUR_MILESTONE_SQL = """
    SELECT m.percent_threshold, m.captured_at_utc, m.block_cost_usd,
           m.marginal_cost_usd, m.seven_day_pct_at_crossing, m.reset_event_id,
           s.weekly_percent AS effective_seven_day_pct_at_crossing
      FROM five_hour_milestones m
      LEFT JOIN weekly_usage_snapshots s ON s.id = m.usage_snapshot_id
     WHERE {selector}
     ORDER BY m.captured_at_utc ASC, m.id ASC
"""


def _load_five_hour_milestones(
    conn: sqlite3.Connection, *,
    block_id: "int | None" = None,
    account_key: "str | None" = None,
    five_hour_window_key: "int | None" = None,
) -> list[dict]:
    """The ONE read of a block's five-hour milestones (#834 S1, #836).

    Three human surfaces used to carry their own copy of this query — the CLI
    breakdown, the dashboard's live current-week route and the dashboard's
    historical-detail route — and two of them shared a defect: they filtered on
    `five_hour_window_key` alone while block uniqueness is
    `(account_key, five_hour_window_key)`, so one physical window's per-account
    blocks all showed every account's milestones.

    Returns snake_case dicts carrying the six columns every surface already
    published PLUS `effective_seven_day_pct_at_crossing`, the weekly value a
    reader actually saw. `seven_day_pct_at_crossing` stays the RAW stored value,
    unchanged and frozen; the effective value comes from a `LEFT JOIN` to the
    `weekly_usage_snapshots` row the milestone already names. Since #824 that row
    carries the effective weekly value on a clamped tick, so no recomputation is
    needed. The join is row-count-safe because it joins on a primary key, and the
    existing `ORDER BY captured_at_utc, id` is retained because the merged
    credit/milestone stream depends on it.

    When the referenced snapshot row is ABSENT — which the non-held stale
    replicas the credit DELETE still removes can cause — the `LEFT JOIN` retains
    the milestone and the effective value is None. Every surface must then render
    an unavailable marker. It must NEVER fall back to the raw value: that would
    put the number this change exists to stop showing back on the screen,
    silently and only in the failure case.

    **The joined value is the snapshot's STORED value** (#840, spec section
    4.8). #834 S1 Gate A R2 replaced a joined value within a point of one a
    credit retired with None, which also withheld genuine readings at that
    value; the retirement band is removed, and a value already poisoned in a
    store is corrected by `db rederive`'s reviewed `retract`. A missing joined
    snapshot still publishes None and never falls back to the raw value.

    SELECTOR. Exactly one of `block_id` or `five_hour_window_key` must be given.
    `block_id` is the precise form — `five_hour_milestones.block_id` names one
    block outright. The pair form filters on the window key AND the account,
    which is the same uniqueness expressed as its two parts, and exists for a
    caller that holds a window rather than an id. Passing neither, both, or
    `account_key` alongside `block_id` raises `ValueError` rather than silently
    ignoring an argument, because a silently ignored account predicate is exactly
    the defect this function replaces.
    """
    if (block_id is None) == (five_hour_window_key is None):
        raise ValueError(
            "_load_five_hour_milestones: pass exactly one of block_id or "
            "five_hour_window_key")
    if block_id is not None and account_key is not None:
        raise ValueError(
            "_load_five_hour_milestones: account_key is meaningful only with "
            "five_hour_window_key; block_id already names one account's block")
    if block_id is not None:
        selector = "m.block_id = ?"
        params: tuple = (int(block_id),)
    else:
        selector = "m.five_hour_window_key = ?"
        params = (int(five_hour_window_key),)
        if account_key is not None:
            selector += " AND m.account_key = ?"
            params += (account_key,)
    rows = conn.execute(
        _FIVE_HOUR_MILESTONE_SQL.format(selector=selector), params,
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        eff = r["effective_seven_day_pct_at_crossing"]
        raw = r["seven_day_pct_at_crossing"]
        out.append({
            "percent_threshold": int(r["percent_threshold"]),
            "captured_at_utc":   r["captured_at_utc"],
            "block_cost_usd":    float(r["block_cost_usd"]),
            "marginal_cost_usd": (
                None if r["marginal_cost_usd"] is None
                else float(r["marginal_cost_usd"])
            ),
            "seven_day_pct_at_crossing": (
                None if raw is None else float(raw)
            ),
            "effective_seven_day_pct_at_crossing": (
                None if eff is None else float(eff)
            ),
            "reset_event_id": int(r["reset_event_id"] or 0),
        })
    return out


def _parse_date_filter(value: str, flag_name: str) -> str:
    """Parse ``YYYY-MM-DD`` or ``YYYYMMDD`` into an ISO date for SQL ``WHERE`` clauses.

    Used by ``cmd_five_hour_blocks`` ``--since``/``--until``. Mirrors the
    upstream ccusage convention. Routes through the centralized
    ``_parse_dual_form_date`` (spec §7.1.1) so the dual-form contract and
    error message are shared with cmd_blocks / cmd_daily / etc.

    The helper already eprints its own diagnostic and raises a bare
    ``ValueError``; we propagate that bare exception so callers can
    return an exit code without double-printing (Review-A P1-1; mirrors
    the bare-re-raise pattern used by ``cmd_cache_report``).
    """
    _c = _cctally()
    return _c._parse_dual_form_date(value, flag_name).date().isoformat()


def _load_breakdown(
    conn: sqlite3.Connection, block_id: int, axis: str,
) -> list[dict]:
    """Load rollup-children rows for one block on the given axis.

    ``axis`` is ``"model"`` or ``"project"``. Returns a list of dicts (one
    per child row), sorted by ``cost_usd DESC, id ASC``.
    """
    table = (
        "five_hour_block_models" if axis == "model"
        else "five_hour_block_projects"
    )
    rows = conn.execute(
        f"""
        SELECT * FROM {table}
         WHERE block_id = ?
         ORDER BY cost_usd DESC, id ASC
        """,
        (block_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _blocks_period_instant(raw: object) -> "dt.datetime":
    """A block boundary as a real INSTANT for the artifact's period.

    `block_start_at` and `five_hour_resets_at` are timestamps, not
    calendar labels, so they convert into the display zone (#503 S2 D7).
    Keeping only the UTC date part — which is what this site used to do —
    named the wrong civil day west of UTC and disagreed with the block's
    own row cell, which `format_display_dt` renders in the display zone.

    It takes no zone argument. The conversion into the display zone
    happens in `period_civil_dates`, from the zone the `PeriodSpec` is
    labelled with, so a zone passed here would be a second authority that
    could disagree with the first (#503 S2 second review N8).
    """
    _c = _cctally()
    try:
        return parse_iso_datetime(str(raw), "five_hour_blocks.block_start_at")
    except (TypeError, ValueError):
        return _c._share_now_utc()


def cmd_five_hour_blocks(args: argparse.Namespace) -> int:
    """List API-anchored 5h blocks with rollup totals + 7d-drift columns."""
    _c = _cctally()
    _c._share_validate_args(args)
    config = _c._load_claude_config_for_args(args)
    # Session A (spec §7.2): bridge -z/--timezone into args.tz before
    # resolve_display_tz so the new alias precedence lands.
    _c._bridge_z_into_tz(args, config)
    args._resolved_tz = _c.resolve_display_tz(args, config)
    # Pin "now" once (CCTALLY_AS_OF for fixture-pinned harnesses; mirrors
    # cmd_five_hour_breakdown). Used by the active-predicate to gate
    # natural expiration so an idle-past-reset block doesn't render ACTIVE.
    now_utc = _command_as_of()
    # #341 --account: resolve the render filter (provider=claude). This command
    # renders from the five_hour_blocks stats table (not the entry cache), so
    # needs_cache=False. None = merged / byte-stable.
    acct_key, acct_exit = _c.resolve_account_filter(args, "claude", needs_cache=False)
    if acct_exit is not None:
        return acct_exit
    conn = open_db()
    try:
        # Date filter parsing — same convention as cmd_blocks.
        # _parse_date_filter routes through _parse_dual_form_date, which
        # eprints its own diagnostic and raises a bare ValueError on bad
        # input (Review-A P1-1 — dedup stderr by NOT re-emitting here).
        try:
            since_iso = (
                _parse_date_filter(args.since, "--since")
                if args.since else None
            )
            until_iso = (
                _parse_date_filter(args.until, "--until")
                if args.until else None
            )
        except ValueError:
            return 2

        where: list[str] = []
        params: list[Any] = []
        if since_iso:
            where.append("block_start_at >= ?")
            params.append(since_iso)
        if until_iso:
            # Inclusive of the until date — add 1 day.
            until_dt = dt.date.fromisoformat(until_iso) + dt.timedelta(days=1)
            where.append("block_start_at < ?")
            params.append(until_dt.isoformat())
        if acct_key is not None:  # #341: scope blocks to the selected account
            where.append("account_key = ?")
            params.append(acct_key)
        clause = ("WHERE " + " AND ".join(where)) if where else ""

        # No filter → cap at 50; with filter → unbounded.
        cap = None if (since_iso or until_iso) else 50
        limit_clause = f"LIMIT {cap}" if cap is not None else ""

        rows = conn.execute(
            f"""
            SELECT * FROM five_hour_blocks {clause}
             ORDER BY block_start_at DESC, id DESC
             {limit_clause}
            """,
            params,
        ).fetchall()

        # Issue #89: --debug report scope = the time range spanned by
        # the rendered block rows. When `rows` is empty, pass an empty
        # list to short-circuit the loader entirely.
        if rows:
            # rows are ORDER BY block_start_at DESC; first row is newest,
            # last row is oldest. The rendered window is
            # [oldest_block_start, newest_block_start + BLOCK_DURATION).
            oldest_start_iso = rows[-1]["block_start_at"]
            newest_start_iso = rows[0]["block_start_at"]
            block_window_start = parse_iso_datetime(
                oldest_start_iso, "block_start_at",
            )
            block_window_end = parse_iso_datetime(
                newest_start_iso, "block_start_at",
            ) + _c.BLOCK_DURATION
            _c._emit_debug_samples_if_set(
                args,
                lambda: _c.get_entries(block_window_start, block_window_end),
                command_label="five-hour-blocks",
            )
        else:
            _c._emit_debug_samples_if_set(
                args, [], command_label="five-hour-blocks",
            )

        # Detect truncation: cap applied AND there's at least one older
        # block beyond the cap. Probe with LIMIT 1 OFFSET <cap> over the
        # SAME filter set (none here, but kept symmetric for clarity).
        truncated = False
        if cap is not None and len(rows) == cap:
            # #341: the cap path implies no date filter, but an --account filter
            # can still apply — probe over the SAME account scope.
            _probe_pred = "" if acct_key is None else " WHERE account_key = ?"
            _probe_p: tuple = () if acct_key is None else (acct_key,)
            extra = conn.execute(
                "SELECT 1 FROM five_hour_blocks" + _probe_pred +
                " ORDER BY block_start_at DESC, id DESC LIMIT 1 OFFSET ?",
                _probe_p + (cap,),
            ).fetchone()
            truncated = extra is not None

        # Latest live 7d% from the latest weekly_usage_snapshots row, used
        # to fill seven_day_pct_at_block_end on the active row.
        latest_7d, latest_window_key = _latest_seven_day_and_window(
            conn, account_key=acct_key)

        # Pre-load credit events for every window_key the rows query
        # returned. Single index-scan over `five_hour_reset_events`;
        # build a window_key -> list[Credit] map keyed for in-process
        # JOIN against each block dict. Used by both the text/JSON
        # render path AND the share-output snapshot wiring (spec §5.1.1).
        # Loaded in a single pass — no per-block SELECT.
        _credit_pred = "" if acct_key is None else " WHERE account_key = ?"
        _credit_p: tuple = () if acct_key is None else (acct_key,)
        credit_rows = conn.execute(
            "SELECT five_hour_window_key, prior_percent, post_percent, "
            "       effective_reset_at_utc "
            "  FROM five_hour_reset_events" + _credit_pred +
            " ORDER BY five_hour_window_key, effective_reset_at_utc",
            _credit_p,
        ).fetchall()
        credits_by_window: dict[int, list[dict]] = {}
        for cr in credit_rows:
            credits_by_window.setdefault(
                int(cr["five_hour_window_key"]), []
            ).append({
                "effectiveResetAtUtc": cr["effective_reset_at_utc"],
                "priorPercent": float(cr["prior_percent"]),
                "postPercent": float(cr["post_percent"]),
                "deltaPp": round(
                    float(cr["post_percent"]) - float(cr["prior_percent"]), 1
                ),
            })

        # Build per-block dicts with the active-flag side-channel.
        # #834 S1 (#835) Gate A R8: one cache shared across the render. The week
        # SET is loaded ONCE PER ACCOUNT and each axis instant is then placed in
        # memory; the bands are resolved once per (week, account). The S1 form
        # memoized on `(account_key, instant_iso)` and claimed the lookup was paid
        # "once per week rather than once per block" — it was not, because the two
        # instants of one block are two distinct keys, so the render paid one full
        # table scan and one full sort per axis. Measured on a 25,053-row store
        # with 50 rendered blocks: 100 lookups, 49 hits, and 157.8-162.0 ms for
        # `five-hour-blocks --json` against 7.0-7.8 ms before S1. It must not
        # outlive `conn`.
        _axes_cache: dict = {}
        block_dicts: list[dict] = []
        for r in rows:
            d = dict(r)
            is_active = _block_is_active(d, latest_window_key, now_utc)
            d["__is_active"] = is_active
            # #834 S1 (#835) Gate A S1: BOTH weekly axes are filtered here, for
            # every block, closed or active. The stored end is the retired number
            # by construction on a weekly-clamped tick, and the stored start had
            # no filter and no override at all. The live `latest_7d` below still
            # wins on the active row whenever it is not None.
            (d["seven_day_pct_at_block_start"],
             d["seven_day_pct_at_block_end"]) = (
                _c._credit_aware_block_weekly_axes(
                    conn, d, now_utc=now_utc, cache=_axes_cache))
            if is_active and latest_7d is not None:
                d["seven_day_pct_at_block_end"] = latest_7d
            # Side-channel (parallel to __is_active): list of credit
            # event dicts for this block's window. Empty list when none.
            d["__credits"] = credits_by_window.get(
                int(d["five_hour_window_key"]), []
            )
            block_dicts.append(d)

        # Shareable-reports gate: --format short-circuits the JSON / table
        # dispatch via `_share_render_and_emit`. The mutex in
        # `_add_share_args` keeps `--format` and `--json` from coexisting.
        # Note: --breakdown is a no-op under --format (snapshot focuses on
        # the headline 5h-block trend; per-axis sub-rows aren't in the
        # share spec scope). Cross-reset blocks render with `▲` x-axis
        # markers in the BarChart and `⚡` glyphs in the table cell —
        # both signals route to the share renderer's UTF-8-safe paths.
        # Gate runs BEFORE the optional `_load_breakdown` loop so a
        # 50-block --format invocation doesn't pay 50 wasted SQLite
        # queries the snapshot would discard.
        if getattr(args, "format", None):
            display_tz_str = _c._share_display_tz_label(args._resolved_tz)
            # Period bounds: prefer the user's --since/--until filter
            # window; fall back to oldest/newest block timestamps when no
            # filter was applied so the period label reflects what the
            # snapshot actually covers.
            # block_dicts is DESC-ordered: [-1] is oldest, [0] is newest.
            if since_iso:
                period_start = _c._share_parse_date_to_dt(
                    since_iso, args._resolved_tz,
                )
            elif block_dicts:
                # A block start is a real INSTANT, so it converts into the
                # display zone rather than being grounded (#503 S2 D7).
                # This used to keep only the UTC date part, which named
                # the wrong civil day west of UTC and disagreed with the
                # block's own row cell, rendered in the display zone.
                period_start = _blocks_period_instant(
                    block_dicts[-1].get("block_start_at"))
            else:
                period_start = _c._share_now_utc()
            if until_iso:
                period_end = _c._share_parse_date_to_dt(
                    until_iso, args._resolved_tz,
                )
            elif block_dicts:
                # The newest block's END, not its start. A block runs for
                # five hours and the artifact's rows describe all of it,
                # so ending the stated period at 13:00 for a block that
                # runs to 18:00 understated what the artifact covers —
                # invisible while the period was rendered as a date and
                # visible the moment the frontmatter carried the full
                # timestamp (#503 S2 second review N5).
                newest = block_dicts[0]
                period_end = _blocks_period_instant(
                    newest.get("five_hour_resets_at")
                    or newest.get("block_start_at"))
            else:
                period_end = _c._share_now_utc()
            # Build a BlocksView from the API-anchored table rows
            # (issue #56). Reset-aware totals come from the table's
            # per-block columns (CLAUDE.md 5-hour gotcha block) so the
            # share snapshot's footer reads from the single typed
            # source rather than re-summing inline.
            view = _c.build_blocks_view_from_table_rows(
                block_dicts,
                period_start=period_start,
                period_end=period_end,
                display_tz=args._resolved_tz,
            )
            snap = _c._build_five_hour_blocks_snapshot(
                view,
                period_start=period_start,
                period_end=period_end,
                display_tz=display_tz_str,
                version=_c._share_resolve_version(),
                tz=args._resolved_tz,
            )
            _c._share_render_and_emit(snap, args)
            return 0

        # Optional breakdown.
        if args.breakdown:
            for bd in block_dicts:
                bd["__breakdown_rows"] = _load_breakdown(
                    conn, bd["id"], args.breakdown,
                )

        if args.json:
            _fhb_payload = _c._five_hour_blocks_to_json(
                block_dicts, since_iso, until_iso,
                cap, truncated, args.breakdown,
            )
            _fhb_payload.update(_c.account_json_fields(acct_key))  # #341 R8
            print(json.dumps(_fhb_payload, indent=2))
            return 0

        _c._render_five_hour_blocks_table(block_dicts, args)
        return 0
    finally:
        conn.close()


def cmd_five_hour_breakdown(args: argparse.Namespace) -> int:
    """Per-percent milestone view inside one 5h block."""
    _c = _cctally()
    config = _c.load_config()
    args._resolved_tz = _c.resolve_display_tz(args, config)
    # Resolve `now` once via the as-of testing hook (env-var-only — no public
    # `--as-of` flag here, matching the existing posture for `project` and
    # other testing-hook-only commands). Used for the active-block elapsed
    # display below so fixture-pinned harnesses get deterministic output.
    now_utc = _command_as_of()
    # #341 --account: resolve the render filter (provider=claude). Reads only the
    # five_hour_* stats tables (not the entry cache), so needs_cache=False.
    acct_key, acct_exit = _c.resolve_account_filter(args, "claude", needs_cache=False)
    if acct_exit is not None:
        return acct_exit
    conn = open_db()
    try:
        try:
            block = _resolve_block_selector(
                conn,
                block_start=args.block_start,
                ago=args.ago,
                account_key=acct_key,
            )
        except ValueError as e:
            print(f"five-hour-breakdown: {e}", file=sys.stderr)
            return 2

        if block is None:
            label = (
                args.block_start if args.block_start
                else f"--ago {args.ago}" if args.ago is not None
                else "current"
            )
            print(
                f"five-hour-breakdown: no block matches '{label}'",
                file=sys.stderr,
            )
            return 2

        # #834 S1 (#836): the ONE milestone read, shared with both dashboard
        # routes. It keeps spec §5.2's `ORDER BY captured_at_utc ASC` (NOT
        # `percent_threshold`) so post-credit segments interleave with pre-credit
        # ones in time-order — the same human threshold number can appear twice,
        # once per `reset_event_id` segment, and must render in the order it
        # crossed. Bucket B per §3.2: ALL segments, no `reset_event_id` filter.
        # `block_id` is the precise selector, so no account predicate is needed.
        milestones = _load_five_hour_milestones(conn, block_id=block["id"])

        # Spec §5.2 — load in-place credit events for this block's
        # window, ascending by effective_reset_at_utc, so the text
        # renderer can interleave a ``⚡ CREDIT  -Xpp @ HH:MM`` divider
        # row between pre- and post-credit milestone segments and JSON
        # consumers see the parallel ``credits[]`` array (Section 5.2).
        _bd_credit_pred = "" if acct_key is None else " AND account_key = ?"
        _bd_credit_p: tuple = () if acct_key is None else (acct_key,)
        credit_rows = conn.execute(
            "SELECT effective_reset_at_utc, prior_percent, post_percent "
            "FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ?" + _bd_credit_pred +
            " ORDER BY effective_reset_at_utc ASC",
            (block["five_hour_window_key"],) + _bd_credit_p,
        ).fetchall()
        credits_list: list[dict] = [
            {
                "effectiveResetAtUtc": c["effective_reset_at_utc"],
                "priorPercent": float(c["prior_percent"]),
                "postPercent": float(c["post_percent"]),
                "deltaPp": round(
                    float(c["post_percent"]) - float(c["prior_percent"]), 1
                ),
            }
            for c in credit_rows
        ]

        crossed = bool(block.get("crossed_seven_day_reset"))
        # #834 S1 (#835) Gate A S1: the same ONE read-time filter the blocks
        # command applies, over both axes. `--block-start` and `--ago` reach
        # closed blocks, which carry no override at all.
        p_start, p_end = _c._credit_aware_block_weekly_axes(
            conn, block, now_utc=now_utc)

        # Live 7d_end on active row.
        latest_7d, latest_window_key = _latest_seven_day_and_window(
            conn, account_key=acct_key)
        is_active = _block_is_active(block, latest_window_key, now_utc)
        if is_active and latest_7d is not None:
            p_end = latest_7d

        delta = (
            None if (crossed or p_start is None or p_end is None)
            else round(p_end - p_start, 9)
        )
        pct = block.get("final_five_hour_percent") or 0.0
        cost = block.get("total_cost_usd") or 0.0
        dpp = round(cost / pct, 9) if pct >= 0.5 else None

        block_out = {
            "blockStartAt":            block["block_start_at"],
            "fiveHourWindowKey":       block["five_hour_window_key"],
            "fiveHourResetsAt":        block["five_hour_resets_at"],
            "lastObservedAtUtc":       block["last_observed_at_utc"],
            "status":                  "active" if is_active else "closed",
            "finalFiveHourPercent":    round(pct, 1),
            "totalCost":               round(cost, 9),
            "dollarsPerPercent":       dpp,
            "inputTokens":             block.get("total_input_tokens", 0),
            "outputTokens":            block.get("total_output_tokens", 0),
            "cacheCreationTokens":     block.get("total_cache_create_tokens", 0),
            "cacheReadTokens":         block.get("total_cache_read_tokens", 0),
            "sevenDayPctAtBlockStart": p_start,
            "sevenDayPctAtBlockEnd":   p_end,
            "sevenDayPctDeltaPp":      delta,
            "crossedSevenDayReset":    crossed,
        }
        # Spec §5.2: expose ``resetEventId`` on each milestone so JSON
        # consumers can disambiguate post-credit threshold repeats from
        # pre-credit ones. ``0`` is the pre-credit/no-credit sentinel
        # (matches the schema default).
        # #834 S1 (#836): `sevenDayPctAtCrossing` keeps its type and meaning —
        # the RAW stored value — and the nullable
        # `effectiveSevenDayPctAtCrossing` is added beside it, so the addition is
        # purely additive and `schemaVersion` does not move.
        ms_out = [
            {
                "percentThreshold":      m["percent_threshold"],
                "capturedAt":            m["captured_at_utc"],
                "blockCostUSD":          round(m["block_cost_usd"], 9),
                "marginalCostUSD":       (
                    None if m["marginal_cost_usd"] is None
                    else round(m["marginal_cost_usd"], 9)
                ),
                "sevenDayPctAtCrossing": m["seven_day_pct_at_crossing"],
                "effectiveSevenDayPctAtCrossing": (
                    m["effective_seven_day_pct_at_crossing"]
                ),
                "resetEventId":          int(m["reset_event_id"] or 0),
            }
            for m in milestones
        ]

        if args.json:
            # Spec §5.2: ``credits`` is the parallel array to
            # ``milestones`` — same shape as the ``credits`` field on
            # ``five-hour-blocks --json`` (§5.1). Stacked credits across
            # distinct 10-min slots produce multiple entries.
            _bd_payload = {
                "schemaVersion": 1,
                "block": block_out,
                "milestones": ms_out,
                "credits": credits_list,
            }
            _bd_payload.update(_c.account_json_fields(acct_key))  # #341 R8
            print(json.dumps(_bd_payload, indent=2))
            return 0

        # Human-readable header line.
        formatted = _format_block_start(block["block_start_at"], args._resolved_tz)
        if is_active:
            # Anchor elapsed math to the resolved `now_utc` (CCTALLY_AS_OF
            # honored) instead of wall-clock so pinned harnesses don't see
            # the active-block header drift every run.
            elapsed_s = max(0, int((
                now_utc
                - dt.datetime.fromisoformat(block["block_start_at"])
            ).total_seconds()))
            status_str = (
                f"(active, {elapsed_s // 3600}h "
                f"{(elapsed_s % 3600) // 60:02d}m elapsed)"
            )
        else:
            ended = _format_hhmm_in_tz(block["five_hour_resets_at"], args._resolved_tz)
            status_str = f"(closed, ended {ended})"

        delta_str = "—" if delta is None else f"Δ {delta:+.1f}pp"
        seven_d_str = (
            f"{p_start:.1f}→{p_end:.1f}"
            if p_start is not None and p_end is not None else "—"
        )
        crossed_suffix = " ⚡ crossed weekly reset" if crossed else ""
        print(
            f"Block: {formatted} {status_str} · "
            f"5h%: {pct:.1f}% · 7d% {seven_d_str} ({delta_str}){crossed_suffix}"
        )

        if not ms_out:
            print("No milestones recorded — block did not cross 1%.")
            return 0

        headers = ["#", "Threshold", "Cumulative Cost", "Marginal Cost",
                   "7d at crossing"]
        rows = []
        # Spec §5.2 — merged event stream. Interleave milestones and
        # credits in time-order (``capturedAt`` for milestones,
        # ``effectiveResetAtUtc`` for credits). Credits render as a
        # divider row with ``⚡ CREDIT`` in the Threshold cell and the
        # delta-pp + HH:MM in the rightmost cell; the milestone row
        # numbering counter (``#``) continues across the divider so the
        # ordinal still reflects "the Nth event in this block."
        merged_events: list[tuple[str, dict]] = []
        for m in ms_out:
            merged_events.append(("milestone", m))
        for c in credits_list:
            merged_events.append(("credit", c))
        merged_events.sort(key=lambda ev: (
            ev[1]["effectiveResetAtUtc"] if ev[0] == "credit"
            else ev[1]["capturedAt"]
        ))
        idx = 0
        for kind, ev in merged_events:
            idx += 1
            if kind == "credit":
                # Spec §5.2: ⚡ CREDIT  -Xpp @ HH:MM divider row.
                # HH:MM rendered in the display tz via format_display_dt.
                # ``format_display_dt`` is the documented chokepoint for
                # human-displayed datetimes (CLAUDE.md). The deltaPp
                # value is float; format as integer ppm (mirrors the
                # five-hour-blocks chip in §5.1).
                hhmm = _c.format_display_dt(
                    ev["effectiveResetAtUtc"],
                    args._resolved_tz,
                    fmt="%H:%M",
                    suffix=False,
                )
                rows.append([
                    str(idx),
                    "⚡ CREDIT",
                    f"{ev['deltaPp']:+.0f}pp",
                    "",
                    f"@ {hhmm}",
                ])
                continue
            m = ev
            cum = _c._fmt_usd_accounting(m['blockCostUSD'], 6)
            marg = (
                "n/a" if m["marginalCostUSD"] is None
                else _c._fmt_usd_accounting(m['marginalCostUSD'], 6)
            )
            # #834 S1 (#836): the EFFECTIVE weekly value, never the raw one. A
            # weekly-clamped tick stored a reading no reader ever saw, and this
            # column is what a human reads. When the joined snapshot row is
            # absent the value is unavailable and the marker says so — falling
            # back to `sevenDayPctAtCrossing` would put the raw number back on
            # the screen exactly where nobody would look for it.
            p7d = (
                "—" if m["effectiveSevenDayPctAtCrossing"] is None
                else f"{m['effectiveSevenDayPctAtCrossing']:.0f}%"
            )
            rows.append(
                [str(idx), f"{m['percentThreshold']}%", cum, marg, p7d]
            )
        print()
        print(_c._boxed_table(headers, rows, ["right"] * 5))
        if is_active:
            print("\n(active — more milestones may appear)")
        return 0
    finally:
        conn.close()


def _backfill_five_hour_blocks(
    conn: sqlite3.Connection, *, only_missing: bool = False,
    journaled_first: "dict | None" = None,
) -> int:
    """One-shot historical backfill of five_hour_blocks from existing
    weekly_usage_snapshots data. Idempotent via UNIQUE(five_hour_window_key)
    + INSERT OR IGNORE. Per spec §4.3, five_hour_milestones is NEVER
    backfilled (write-once gotcha).

    Returns the count of newly-inserted parent rows (sum of INSERT OR IGNORE
    rowcounts). Caller uses this to decide whether to re-run the
    003_merge_5h_block_duplicates_v1 handler post-backfill — the dispatcher
    walks the registry BEFORE this backfill, so a fresh run against an
    empty five_hour_blocks no-ops the dedup migration even when the
    snapshot keys we're about to insert are jitter-forked. On failure,
    rolls back and returns 0; gate fires again on next open_db() so
    partial state is recoverable.

    ``only_missing`` (Task 8, spec §5.4 rebuild): scope the window scan to
    windows that have snapshots but NO five_hour_blocks row yet — the OPEN
    (never-closed) window's projection. On a journal rebuild the CLOSED blocks
    are already materialized from ``five_hour_block_close`` evts, so this
    re-materializes only the open block (block-only, no milestones — the
    journaled 5h-milestone evts carry their own values and resolve block_id
    against this row), keeping the pass O(open windows) instead of O(all blocks).

    #825/#832 fix round J4 (#954): the rebuild projection reproduces the open
    block live ingest left, on its accounting columns too.

    * ``journaled_first`` maps ``(account_key, five_hour_window_key)`` to the
      window's FIRST WRITER — its first effective `snapshot_accept` decision
      in journal order (`_cctally_journal.
      journaled_block_first_observations`) — and supplies the block's
      immutable start values (`first_observed_at_utc`,
      `five_hour_resets_at`, `block_start_at`,
      `seven_day_pct_at_block_start`) whenever it is present. Live ingest's
      upsert never updates those columns: the writer that materializes the
      row owns them. Two things separate that writer from the earliest
      surviving snapshot. A five-hour credit's stale-replica removal can
      delete its snapshot (the band starts at floor10 of the arming tick, so
      it reaches the genuine pre-credit reading); the snapshot still observed
      the block, because the removal retires its weekly VALUE, not the
      observation (J4). And a reading captured earlier can be appended, and
      so derived, later (fix round J5, 825-832-CD4-V01): its row survives
      with the earlier capture, but it updated a block another reading had
      already opened. Neither survivor-derived pick reproduces live. A
      window without an entry (a payload without an explicit account) falls
      back to the earliest surviving snapshot.
    * The projection stamps the computation-time pricing provenance of the
      totals it computes, through the live upsert's own helper
      (`_block_pricing_provenance_json`), so a successor's
      `five_hour_block_close` freezes it exactly as after live ingest. The
      ordinary (legacy, pre-journal) backfill keeps NULL: its blocks predate
      provenance, which `_lib_cost_provenance.historical_close_marker` reads
      as unproven.
    """
    _c = _cctally()
    inserted = 0
    journaled_first = (journaled_first or {}) if only_missing else {}

    def _first_row(key, acct):
        """The window's start row: its first writer (J4/J5), else the
        earliest surviving snapshot."""
        journaled = journaled_first.get((acct, key))
        if journaled is not None:
            return journaled
        return conn.execute(
            """
            SELECT five_hour_resets_at, captured_at_utc, weekly_percent
              FROM weekly_usage_snapshots
             WHERE five_hour_window_key = ? AND account_key = ?
               AND five_hour_percent IS NOT NULL
             ORDER BY captured_at_utc ASC, id ASC LIMIT 1
            """,
            (key, acct),
        ).fetchone()

    try:
        # Iterate distinct windows that have BOTH a canonical key AND a
        # 5h percent. The percent guard is critical: MAX(five_hour_percent)
        # over a NULL-only window is NULL, which would trip the
        # final_five_hour_percent NOT NULL constraint at insert time.
        # #341: re-materialize one open block per (account_key, window_key). A
        # shared physical 5h window observed by two accounts yields two distinct
        # open blocks so rebuild reproduces per-account ownership.
        keys_sql = """
                SELECT DISTINCT snapshots.five_hour_window_key,
                                snapshots.account_key
                  FROM weekly_usage_snapshots AS snapshots
                 WHERE snapshots.five_hour_window_key IS NOT NULL
                   AND snapshots.five_hour_percent     IS NOT NULL
        """
        if only_missing:
            # #997 D19: each account's latest qualifying snapshot comes from
            # one ranking pass over the qualifying rows (O(n log n)). A
            # correlated subquery here re-scanned and re-sorted the table per
            # qualifying row (quadratic: 68.4 s on a 27,521-row store, past
            # the rebuild's progress-gap bound). The partition groups NULL
            # account keys as `IS` did; the ORDER BY is unchanged.
            keys_sql += (
                "   AND snapshots.id IN ("
                "       SELECT id FROM ("
                "           SELECT id, ROW_NUMBER() OVER ("
                "                      PARTITION BY account_key"
                "                      ORDER BY unixepoch(captured_at_utc) DESC,"
                "                               id DESC"
                "                  ) AS rn"
                "             FROM weekly_usage_snapshots"
                "            WHERE five_hour_window_key IS NOT NULL"
                "              AND five_hour_percent IS NOT NULL"
                "       ) WHERE rn = 1"
                "   )"
                "   AND NOT EXISTS ("
                "       SELECT 1 FROM five_hour_blocks AS blocks"
                "        WHERE blocks.five_hour_window_key ="
                "              snapshots.five_hour_window_key"
                "          AND blocks.account_key IS snapshots.account_key"
                "   )"
            )
        keys = [(int(r[0]), r[1]) for r in conn.execute(keys_sql).fetchall()]

        now_iso = now_utc_iso()
        now_dt = parse_iso_datetime(now_iso, "now")

        # #751a ownership context. Each window this backfill materializes
        # competes with the windows already in `five_hour_blocks` AND with
        # the other windows this same pass is about to insert, so both are
        # collected before any total is computed. Without the second half a
        # from-empty backfill has no competitors at all and every adjacent
        # pair re-prices its overlap twice.
        ownership: dict[Any, list] = {}
        seen_keys: set = set()
        for row in conn.execute(
            "SELECT five_hour_window_key, block_start_at, "
            "       five_hour_resets_at, account_key FROM five_hour_blocks"
        ).fetchall():
            try:
                w_start = parse_iso_datetime(
                    row["block_start_at"], "five_hour_blocks.block_start_at",
                ).astimezone(dt.timezone.utc)
                w_reset = parse_iso_datetime(
                    row["five_hour_resets_at"],
                    "five_hour_blocks.five_hour_resets_at",
                ).astimezone(dt.timezone.utc)
            except ValueError:
                continue
            w_key = int(row["five_hour_window_key"])
            seen_keys.add((row["account_key"], w_key))
            ownership.setdefault(row["account_key"], []).append(
                _c.OwnedWindow(key=w_key, start=w_start, reset=w_reset)
            )
        for key, acct in keys:
            if (acct, key) in seen_keys:
                continue
            # Same start-row pick the insert loop makes below, so the
            # ownership interval and the inserted row cannot disagree.
            anchor_row = _first_row(key, acct)
            if anchor_row is None:
                continue
            try:
                w_reset = parse_iso_datetime(
                    anchor_row["five_hour_resets_at"], "five_hour_resets_at",
                ).astimezone(dt.timezone.utc)
            except ValueError:
                continue
            seen_keys.add((acct, key))
            ownership.setdefault(acct, []).append(_c.OwnedWindow(
                key=key, start=w_reset - dt.timedelta(hours=5), reset=w_reset,
            ))

        # BEGIN IMMEDIATE (not deferred): this transaction's first DML is a
        # READ (min_row/max_row below), so a plain deferred BEGIN takes a read
        # snapshot and only tries to upgrade to the write lock at the first
        # INSERT OR IGNORE. Under concurrent first-run openers, a competing
        # commit landing between that read and the first write makes the upgrade
        # fail with SQLITE_BUSY_SNAPSHOT *immediately* — busy_timeout cannot
        # absorb it, and the whole backfill rolls back. Acquiring the write lock
        # up front serializes the backfill cleanly behind busy_timeout instead.
        # See cctally-dev#87.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for block_index, (key, acct) in enumerate(keys, 1):
                if not block_index & 1023:
                    # #997 D9: a rebuild's open-block projection reports
                    # progress (throttled; a no-op without a progress sink).
                    _lib_rebuild_progress.tick(blocks=block_index)
                # The start row (the first writer, else the MIN-captured
                # survivor) defines the immutable block boundary values
                # (deterministic — picking "any in-window row" would be
                # nondeterministic under seconds-level Anthropic ISO jitter
                # that the canonical key collapses). Scoped to
                # (window_key, account_key) so a shared window's per-account
                # blocks each read their own account's boundary rows (#341).
                min_row = _first_row(key, acct)
                max_row = conn.execute(
                    """
                    SELECT captured_at_utc, weekly_percent, five_hour_percent
                      FROM weekly_usage_snapshots
                     WHERE five_hour_window_key = ? AND account_key = ?
                       AND five_hour_percent IS NOT NULL
                     ORDER BY captured_at_utc DESC, id DESC LIMIT 1
                    """,
                    (key, acct),
                ).fetchone()
                if min_row is None or max_row is None:
                    continue   # defensive — should be unreachable per the keys query

                resets_at = min_row["five_hour_resets_at"]
                first_obs = min_row["captured_at_utc"]
                last_obs  = max_row["captured_at_utc"]
                pct_start_7d = min_row["weekly_percent"]
                pct_end_7d   = max_row["weekly_percent"]
                final_5h     = float(max_row["five_hour_percent"])

                resets_dt = parse_iso_datetime(resets_at, "resets_at backfill")
                block_start_dt = resets_dt - dt.timedelta(hours=5)
                block_start_at = block_start_dt.isoformat(timespec="seconds")
                last_obs_dt = parse_iso_datetime(last_obs, "last_obs backfill")

                # Cross-reset detection (interval predicate, symmetric with
                # the live path's UPDATE in T4). Two sources:
                #   (a) week_reset_events — Anthropic-shifted mid-week resets.
                #   (b) weekly_usage_snapshots.week_start_at — natural week
                #       boundaries (no event row for these).
                # Strict ``>`` on the lower bound for (b) so a block whose
                # block_start_at coincides with a week boundary is not flagged.
                # ``unixepoch()`` normalizes the comparison across mixed tz
                # suffixes (block_start_at is host-local; week_start_at /
                # effective_reset_at_utc are ``+00:00``); see the live-path
                # comment for rationale.
                cross_row = conn.execute(
                    """
                    SELECT 1 FROM week_reset_events
                     WHERE account_key = ?
                       AND unixepoch(effective_reset_at_utc) >= unixepoch(?)
                       AND unixepoch(effective_reset_at_utc) <= unixepoch(?)
                     LIMIT 1
                    """,
                    (acct, block_start_at, last_obs),
                ).fetchone()
                if cross_row is None:
                    cross_row = conn.execute(
                        """
                        SELECT 1 FROM weekly_usage_snapshots
                         WHERE week_start_at IS NOT NULL
                           AND account_key = ?
                           AND unixepoch(week_start_at) >  unixepoch(?)
                           AND unixepoch(week_start_at) <= unixepoch(?)
                         LIMIT 1
                        """,
                        (acct, block_start_at, last_obs),
                    ).fetchone()
                crossed = 1 if cross_row is not None else 0

                # A rebuild's only-missing row is the trailing unjournaled
                # projection, never a close decision. Wall time may be past its
                # reset, but only a later retained observation may close and
                # clock it (#399). The ordinary legacy backfill keeps its
                # historical wall-time classification.
                is_closed = (
                    0 if only_missing else (1 if resets_dt < now_dt else 0)
                )
                projection_created_at = first_obs if only_missing else now_iso
                projection_updated_at = last_obs if only_missing else now_iso

                # Token + cost totals — recomputed via the shared helper,
                # which routes through get_entries() and falls back to
                # JSONL if cache.db is unreadable.
                # skip_sync=True: backfill runs inside open_db(); a sync_cache
                # cascade here would reopen cache.db recursively.
                pricing_evidence = {} if only_missing else None
                totals = _c._compute_block_totals(
                    block_start_dt, last_obs_dt, skip_sync=True,
                    owner_key=key, windows=ownership.get(acct, []),
                    pricing_evidence=pricing_evidence,
                )
                pricing_provenance_json = (
                    _c._block_pricing_provenance_json(pricing_evidence)
                    if pricing_evidence is not None else None
                )

                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO five_hour_blocks (
                      five_hour_window_key,
                      five_hour_resets_at,
                      block_start_at,
                      first_observed_at_utc,
                      last_observed_at_utc,
                      final_five_hour_percent,
                      seven_day_pct_at_block_start,
                      seven_day_pct_at_block_end,
                      crossed_seven_day_reset,
                      total_input_tokens,
                      total_output_tokens,
                      total_cache_create_tokens,
                      total_cache_read_tokens,
                      total_cost_usd,
                      pricing_provenance_json,
                      is_closed,
                      created_at_utc,
                      last_updated_at_utc,
                      account_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?)
                    """,
                    (
                        key,
                        resets_at,
                        block_start_at,
                        first_obs,
                        last_obs,
                        final_5h,
                        pct_start_7d,
                        pct_end_7d,
                        crossed,
                        totals["input_tokens"],
                        totals["output_tokens"],
                        totals["cache_create_tokens"],
                        totals["cache_read_tokens"],
                        totals["cost_usd"],
                        pricing_provenance_json,
                        is_closed,
                        projection_created_at,
                        projection_updated_at,
                        acct,
                    ),
                )
                inserted += cur.rowcount or 0

                # ── Write per-(block, model) and per-(block, project) child
                # rows for this window. INSERT OR IGNORE handles partial-fail
                # re-runs (UNIQUE(five_hour_window_key, model|project_path)).
                # Same transaction as the parent INSERT.
                parent_id_row = conn.execute(
                    "SELECT id FROM five_hour_blocks "
                    "WHERE five_hour_window_key = ? AND account_key = ?",
                    (key, acct),
                ).fetchone()
                if parent_id_row is not None:
                    parent_id = int(parent_id_row["id"])
                    if totals.get("by_model"):
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO five_hour_block_models (
                              block_id, five_hour_window_key, model,
                              input_tokens, output_tokens,
                              cache_create_tokens, cache_read_tokens,
                              cost_usd, entry_count, account_key
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    parent_id,
                                    key,
                                    model,
                                    b["input_tokens"],
                                    b["output_tokens"],
                                    b["cache_create_tokens"],
                                    b["cache_read_tokens"],
                                    b["cost_usd"],
                                    b["entry_count"],
                                    acct,
                                )
                                for model, b in totals["by_model"].items()
                            ],
                        )
                    if totals.get("by_project"):
                        conn.executemany(
                            """
                            INSERT OR IGNORE INTO five_hour_block_projects (
                              block_id, five_hour_window_key, project_path,
                              input_tokens, output_tokens,
                              cache_create_tokens, cache_read_tokens,
                              cost_usd, entry_count, account_key
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    parent_id,
                                    key,
                                    project_path,
                                    b["input_tokens"],
                                    b["output_tokens"],
                                    b["cache_create_tokens"],
                                    b["cache_read_tokens"],
                                    b["cost_usd"],
                                    b["entry_count"],
                                    acct,
                                )
                                for project_path, b in totals["by_project"].items()
                            ],
                        )

            # Mark child-table migrations done so the upgrade-user gates in
            # open_db() don't re-fire on next open. Inside the same
            # transaction as the per-window inserts so partial-fail leaves
            # both the data AND the markers absent — gates re-fire cleanly.
            conn.executemany(
                """
                INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc)
                VALUES (?, ?)
                """,
                [
                    ("001_five_hour_block_models_backfill_v1",   now_iso),
                    ("002_five_hour_block_projects_backfill_v1", now_iso),
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except Exception as exc:
        eprint(f"[5h-block backfill] failed: {exc}")
        return 0
    return inserted
