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
import collections
import datetime as dt
import json
import sqlite3
import sys

import _lib_accounts  # pure stdlib kernel; UNATTRIBUTED sentinel (#341)
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
    live block closed. Rows with no ``five_hour_window_key`` are included here
    too, unchanged, because the latest row's key is the answer even when it is
    NULL.

    **The weekly value is CREDIT-AWARE**, which is the #835 change. It comes from
    the most recent row that is not a post-floor stale replica — the same band
    and floor predicate the stale-replica DELETE applies, applied as a read
    filter. Held-inclusive is still right in principle here, because a held row
    stores the EFFECTIVE weekly value and that is what a block's weekly end
    means. What is not right is reporting a value a credit RETIRED, and #835
    made that reachable by preserving held rows: before it, the DELETE removed
    every post-floor replica, so this read could not see one. Excluding held rows
    outright would be the wrong repair — it would report the block end from an
    older tick, which is the mistake #824's comment here warned against.

    **The weekly walk is BOUNDED three ways, and each bound closes a hole tranche
    A left open (#834 S1 Gate A R1).**

    *Scope.* The walk descends only within the NEWEST row's own
    ``(week_start_date, account_key)``. A candidate belonging to another week or
    another account ends the walk with ``None``, because week W-1's percentage is
    not week W's weekly end and account B's is not account A's, under any reading.
    Before #835 this read took exactly one row, so the absent predicates could not
    matter; a walk makes them matter.

    *Floor.* The walk also ends with ``None`` at the first row captured BEFORE the
    latest credit floor in that scope. This is the soundness bound: a pre-floor row
    is never classified as retired, so without it the walk terminated on one and
    returned its value — and that value is by construction the PRE-credit reading,
    which is exactly the value a retirement band exists to suppress. A sub-one-point
    credit reaches it immediately, because such a credit is legal
    (`_doomed_snapshot_rows`) and places the post-credit synthetic inside a band
    centred on ``from_pct``, leaving every post-floor row in the band. An
    unparseable capture stamp inside a credited week is treated as pre-floor for
    the same reason: the row cannot be placed relative to the floor, so it cannot
    be certified as able to state the current value.

    *Retirement.* Within the scope and at or after the floor, a retired replica is
    skipped, and a retired value is therefore never returned.

    The floor bound applies to the NEWEST row as well as to the candidates below
    it, so a newest row captured before the latest floor answers ``None`` where the
    pre-#835 code answered its value. That state is unreachable through either
    production credit path: `record-credit` writes a post-credit synthetic snapshot
    at its own effective instant, and the automatic path's floor is derived from a
    capture that is itself in the store, so in both cases at least one row exists at
    or after the floor and it is the newest.

    An UNCREDITED week resolves no bands, so there is no floor, nothing is skipped,
    and the read returns the newest row's value exactly as it did before #835. A
    ``NULL`` weekly value is not a retired replica, so it still terminates the walk
    and still answers ``None``. Both contracts are byte-stable against today.

    **The residual, deliberately unfixed and filed as #840.** A genuine later climb
    back INTO the band is still classified as a replay, so the weekly end reads the
    newest out-of-band value until the true value passes the band's upper edge.
    That is preferred over returning a value a credit retired, and no sound
    discriminator is available: `record-credit`'s own synthetic post-credit
    snapshot is written before any later replay, so "a below-band row has been
    observed" would close the band immediately and let every replay through.
    `test_840_characterization_a_genuine_climb_back_reads_the_older_value`
    characterizes it.

    ``account_key`` (#341, spec §3): ``None`` = merged / byte-stable; a real key /
    ``unattributed`` scopes both selections to that account. The retirement bands
    are resolved against the NEWEST ROW's own account, which is also the walk's
    scope, because a credit under one account retires nothing under another.
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
        # Axis 1: the five-hour window key, from the latest row, held-inclusive.
        key = row["five_hour_window_key"]
        # Axis 2: the weekly value. The scope, the bands and the floor all come
        # from the NEWEST row and are resolved ONCE, because the walk may not
        # leave that scope — so there is nothing to memoize per candidate.
        #
        # Dropping `LIMIT 1` does cost something at the storage layer: SQLite
        # materializes the full sort before yielding the first row, measured at
        # 9.0ms against 3.4ms on a 25,053-row store. The cost is immaterial
        # because this runs once per invocation, but the earlier claim that the
        # ordinary case "reads exactly one row, which is the work the single-row
        # query did" described only the rows the cursor yields, not the work the
        # query does, and was wrong.
        scope = (row["week_start_date"], row["account_key"])
        bands = _c._credit_retirement_bands(
            conn, week_start_date=row["week_start_date"],
            week_start_at=row["week_start_at"],
            week_end_at=row["week_end_at"],
            account_key=row["account_key"])
        floor = _c._latest_credit_floor_instant(bands)
        pct = None
        while row is not None:
            if (row["week_start_date"], row["account_key"]) != scope:
                break
            if floor is not None:
                try:
                    captured = parse_iso_datetime(
                        row["captured_at_utc"], "captured_at").timestamp()
                except (ValueError, TypeError):
                    captured = None
                if captured is None or captured < floor:
                    break
            if not _c._weekly_value_is_retired_replica(
                    bands,
                    captured_at_utc=row["captured_at_utc"],
                    weekly_percent=row["weekly_percent"]):
                pct = row["weekly_percent"]
                break
            row = cur.fetchone()
    except sqlite3.DatabaseError:
        return None, None
    return (
        float(pct) if pct is not None else None,
        int(key) if key is not None else None,
    )


def _parsed_block_instant(block, column):
    """``block[column]`` when it is present and parses as an instant, else ``None``.

    #834 S1 (#835) Gate A R8. Both observation stamps are declared `TEXT NOT NULL`,
    so absence is not a live shape — but a fixture, a hand-built store or a legacy
    projection can still carry an unparseable one, and the two instant helpers
    below must degrade rather than raise.
    """
    value = block.get(column)
    if not value:
        return None
    try:
        parse_iso_datetime(value, column)
    except (ValueError, TypeError):
        return None
    return value


def _block_weekly_start_instant(block):
    """The instant a block's stored weekly START was captured at, as ISO text.

    #834 S1 (#835) Gate A R8. `first_observed_at_utc`, because that is the stamp
    of the tick that wrote `seven_day_pct_at_block_start`. The live upsert in
    bin/_cctally_record.py sets both from one fold dict on the INSERT (both are
    omitted from its `DO UPDATE`, so the first writer owns the pair), and
    `_backfill_five_hour_blocks` takes `first_obs` and `pct_start_7d` from its
    single MIN-captured snapshot row.

    NOT `block_start_at`, which is the window's NOMINAL start — `five_hour_resets_at`
    minus five hours — and therefore at or before the first tick. The gap is not
    cosmetic: a block whose nominal start precedes a credit floor but whose first
    tick landed after it would have the floor's retired value compared against a
    PRE-floor instant and rendered. It falls back to `block_start_at` only when the
    observation stamp is absent or unparseable, which keeps the pre-R8 disposition
    rather than blanking the axis.
    """
    return (_parsed_block_instant(block, "first_observed_at_utc")
            or block.get("block_start_at"))


def _block_weekly_end_instant(block, *, now_utc):
    """The instant a block's stored weekly END was captured at, as an ISO text.

    #834 S1 (#835) Gate A R8. `last_observed_at_utc`, because that is the stamp of
    the tick that wrote `seven_day_pct_at_block_end`. All four writers of the pair
    move the two together: the live upsert assigns
    `last_observed_at_utc = excluded.last_observed_at_utc` and
    `seven_day_pct_at_block_end = excluded.seven_day_pct_at_block_end` in one
    `DO UPDATE SET` from one fold dict, `_backfill_five_hour_blocks` takes
    `last_obs` and `pct_end_7d` from its single MAX-captured snapshot row,
    `five_hour_block_close` freezes the whole row into one evt, and
    `_migration_merge_5h_block_duplicates_v1` (bin/_cctally_db.py) copies both
    from the group row whose `last_observed_at_utc` is MAX.

    Gate A S1 used `five_hour_resets_at` instead, falling back to the current
    instant while that reset was still in the future. That is the block's END, not
    a capture instant, and it was wrong in BOTH directions. A crossed-reset block's
    reset falls in the SUCCESSOR week, so `_block_weekly_axis_weeks` resolved the
    successor's bounds and the credited week's bands were never consulted. A closed
    block whose last tick predates the floor had its genuinely effective end
    withheld, which contradicts the historical-truth rule this filter rests on.

    The reset-or-now form is KEPT as the fallback for an absent or unparseable
    stamp, so a store that cannot supply a capture instant behaves as it did
    before R8 rather than losing the axis.
    """
    _c = _cctally()
    observed = _parsed_block_instant(block, "last_observed_at_utc")
    if observed is not None:
        return observed
    now_iso = _c._iso_z(now_utc)
    resets = block.get("five_hour_resets_at")
    if not resets:
        return now_iso
    try:
        moment = parse_iso_datetime(resets, "five_hour_resets_at").timestamp()
    except (ValueError, TypeError):
        return now_iso
    return resets if moment <= now_utc.timestamp() else now_iso


#: One candidate week for `_block_weekly_axis_weeks`, as loaded once per account
#: per render. ``lo`` / ``hi`` are the parsed `[week_start_at, week_end_at)`
#: epochs, or ``None`` when the week carries no usable bounds.
_AxisWeek = collections.namedtuple(
    "_AxisWeek",
    "week_start_date week_end_date week_start_at week_end_at lo hi")


def _parsed_week_bounds(week_start_at, week_end_at):
    """``(lo, hi)`` epochs for a week's bounds, or ``(None, None)``.

    #834 S1 (#835) Gate A R9. Both loaders need the same verdict on the same two
    nullable columns — `_account_axis_weeks` to decide whether a week is placed by
    epoch or by date, `_account_axis_capture_weeks` to decide whether the identity
    it returns carries bounds for the `week_reset_events` leg — so the parse lives
    in one place and the two cannot disagree about which weeks have usable bounds.
    Absent or unparseable on either side yields no bounds, which is what
    `_credit_retirement_bands` already treats as a leg it cannot scope.
    """
    if week_start_at is None or week_end_at is None:
        return None, None
    try:
        return (parse_iso_datetime(week_start_at, "week_start_at").timestamp(),
                parse_iso_datetime(week_end_at, "week_end_at").timestamp())
    except (ValueError, TypeError):
        return None, None


def _account_axis_weeks(conn, *, account_key, cache):
    """Every distinct week ``account_key``'s snapshot rows name, in no order.

    #834 S1 (#835) Gate A R8. ONE query per account per render, replacing one query
    per axis instant. The per-instant form was measured at 22x the whole render's
    prior cost on a 25,000-row store with 50 blocks, because its
    ``unixepoch(week_start_at)`` predicate makes `idx_usage_week_start_at_time`
    unusable — `EXPLAIN QUERY PLAN` reported `SCAN weekly_usage_snapshots` plus
    `USE TEMP B-TREE FOR ORDER BY`, and the 50-block render performed about 51 full
    scans and 51 full sorts. An instant-keyed memo could not collapse them: two
    instants per block are two distinct keys.

    THE WIN IS THE COLLAPSE TO ONE QUERY, NOT RESTORED INDEX USE. This query is
    equally unindexed: `EXPLAIN QUERY PLAN` reports `SCAN weekly_usage_snapshots`
    plus a temp B-tree. About 51 unindexed scans became one unindexed scan, which is
    the whole of the improvement; do not read `idx_usage_week_start_at_time` as being
    back in play. The scan verdict was measured on an empty store, on a 25,500-row
    store and again after `ANALYZE` while this query was still a `GROUP BY`; R10
    replaced that clause with `SELECT DISTINCT`, which changed the B-tree's stated
    purpose and not the scan (see the R10 paragraph below for the measured plan).

    THE RESIDUAL IS NOT IRREDUCIBLE, and it is accepted as shipped rather than
    defended as minimal. `idx_usage_week_time` leads on `week_start_date`, so
    narrowing the projection to that column alone does use it — `SELECT DISTINCT
    week_start_date` reports `SCAN weekly_usage_snapshots USING INDEX
    idx_usage_week_time`, measured on the same two stores, and an earlier form said
    "grouping on that column alone", which is stale terminology since R10 replaced
    the `GROUP BY` with `SELECT DISTINCT` — and a render only
    needs the weeks overlapping its blocks' instant range rather than the account's
    whole history. Both are real reductions and neither is taken, because the
    property that matters is that the cost is CONSTANT in the number of rendered
    blocks, which it already is. `week_end_date` and the two bounds are in the
    projection because the date pass needs all four, which is exactly what a
    `week_start_date`-only projection would give up.

    ONE COMPARISON SEMANTIC CHANGED WITH THE FORM, unreachably. The per-instant
    predicate compared SQLite `unixepoch()` integers, which truncate a fractional
    second; this form compares `parse_iso_datetime(...).timestamp()` floats, which
    keep it. The two disagree for a week bound carrying a fractional second: a
    `week_start_at` of `…T00:00:00.500000+00:00` against a probe at
    `…T00:00:00+00:00` is INSIDE the week under the integer compare and outside it
    under the float one. No writer can produce that bound.
    `_canonicalize_optional_iso` returns `isoformat(timespec="seconds")` over a
    `_normalize_week_boundary_dt` result, which zeroes second and microsecond and
    snaps to the hour, and `cmd_record_usage`'s pipeline derives both bounds from
    that same normalizer and writes them with the same `timespec="seconds"`.

    THE SET IS UNORDERED, AND THE QUERY IS A PLAIN `SELECT DISTINCT` (Gate A R10).
    Through R9 it carried a `MAX(captured_at_utc || char(1) || printf('%020d', id))`
    aggregate whose SQL alias was `rk` and whose `_AxisWeek` field was `rank` — call
    it `rk`, because that is the alias, and earlier text here called the column
    `rank` — introduced to reproduce the per-instant query's
    ``ORDER BY captured_at_utc DESC, id DESC`` tie-break exactly so that which week
    won an overlap did not change with the query's form, and kept as documentary
    after R9 deleted the tie-break. Nothing read it: this function has one caller,
    every pass in `_block_weekly_axis_weeks` builds an order-insensitive set, and
    the caller ORs a predicate over the result. The grouping was there only to carry
    the aggregate, so both went.

    NOTHING PINS THE ORDER NOW. There was never a SQL `ORDER BY` on this query: the
    order came from a Python `weeks.sort(key=lambda w: w.rank, reverse=True)` over the
    aggregate. Through R8 a consumer tie-break in the epoch pass read it; R9 deleted
    that tie-break and left the aggregate documentary; R10 deleted the aggregate and
    the sort together. So the returned order is now whatever SQLite produces, and no
    test asserts order-insensitivity of the consumer, so a future consumer that cares
    about order has nothing to fail against. It is safe today because the one consumer
    ORs a monotone band predicate over the whole set. A consumer that needs an order
    must establish it itself rather than assume this one.

    The statement-count test cannot see R10's REMOVAL of the aggregate and the
    grouping, because the statement count is unchanged by it; the removal was
    measured instead, best of five on a 25,194-row 13-week 50-block store, with the
    order of the three forms reversed to rule out page-cache bias and the three
    result sets asserted identical: 13.57 ms with the aggregate, 9.36 ms for the
    bare `GROUP BY`, 2.34 ms for `SELECT DISTINCT` — so removing both took about 83%
    off the statement, and the plan changed from `SCAN weekly_usage_snapshots` +
    `USE TEMP B-TREE FOR GROUP BY` to the same scan + `USE TEMP B-TREE FOR DISTINCT`.
    It is still an unindexed scan; the `idx_usage_week_time` reduction named above is
    still not taken, for the reason given there.

    DO NOT QUOTE A SPLIT BETWEEN THE TWO REMOVALS. On those figures the aggregate is
    31% of the statement and the grouping a further 52%, but the review that checked
    this measured 12.87-13.09 / 6.30-6.77 / 2.02-2.23 ms on its own 25,194-row store,
    which makes the aggregate about 50% and the grouping about 34% — the same 83%
    total, with the two components in the opposite order. The total reproduces and
    the ranking does not, so treat the split as machine- and data-dependent.

    `week_start_at` / `week_end_at` are both nullable and
    `_derive_week_from_payload` leaves them `None` on three of its four paths, so
    a week with no bounds is a live shape; it is retained here with ``lo``/``hi``
    of ``None`` and placed by date instead (see `_block_weekly_axis_weeks`).
    """
    if account_key in cache:
        return cache[account_key]
    try:
        rows = conn.execute(
            "SELECT DISTINCT week_start_date, week_end_date, week_start_at, "
            "       week_end_at "
            "  FROM weekly_usage_snapshots "
            " WHERE account_key = ?",
            (account_key,),
        ).fetchall()
    except sqlite3.DatabaseError:
        rows = []
    weeks: list = []
    for r in rows:
        lo, hi = _parsed_week_bounds(r["week_start_at"], r["week_end_at"])
        weeks.append(_AxisWeek(
            week_start_date=r["week_start_date"],
            week_end_date=r["week_end_date"],
            week_start_at=r["week_start_at"] if lo is not None else None,
            week_end_at=r["week_end_at"] if hi is not None else None,
            lo=lo, hi=hi))
    cache[account_key] = weeks
    return weeks


def _account_axis_capture_weeks(conn, *, account_key, cache):
    """``{capture second -> the week identities a snapshot row at that instant
    names}``, for the capture instants ``account_key``'s blocks actually carry.

    #834 S1 (#835) Gate A R9. THE STORE KEEPS A RECORD OF WHICH WEEK A CAPTURE WAS
    OBSERVED UNDER, and this is the read of it. Round five documented its
    over-withholding on the premise that it does not. All four writers of
    `five_hour_blocks.seven_day_pct_at_block_start` / `_at_block_end` take the
    matching axis STAMP from a `weekly_usage_snapshots` row's `captured_at_utc`:
    the live upsert passes `captured_at = saved["capturedAt"]` into
    `first_observed_at_utc` and `last_observed_at_utc` in one `INSERT … DO UPDATE`
    and that value IS the row's `captured_at_utc` (bin/_cctally_record.py:2351,
    :2633-2634, :6828), `_backfill_five_hour_blocks` takes them from its MIN- and
    MAX-captured rows, `five_hour_block_close` freezes the row, and
    `_migration_merge_5h_block_duplicates_v1` copies from the MAX-captured group
    row. Every snapshot row carries all four week columns. So a row whose
    `captured_at_utc` equals an axis instant NAMES the week in force at that
    capture, with no date-range guess involved.

    HELD ROWS ARE NOT EXCLUDED, which is the one judgement call in this function.
    The rest of this tranche excludes `weekly_observation_held = 1` from weekly
    VALUE reads, because a held row carries a reading no fresh observation
    confirmed. This function reads no value: it reads which week the value on the
    block's axis came from, and on a held tick that is the BASIS week by
    construction — the held write copies the basis's four week columns verbatim
    while keeping the tick's own `capture_at` (bin/_cctally_record.py:7190-7203),
    and the block's axis takes `fold.weekly.effective_pct`, the same
    carried-forward value, stamped with that same `capture_at`. The held row is
    therefore the provenance record of the number on the axis, and the credits
    that retired that number are the basis week's.

    Two consequences follow. A held row is exactly how a pre-credit weekly value
    reaches a post-credit capture instant, which is the case this whole tranche
    exists to handle, and excluding it would drop the only exact evidence of which
    week's floor applies. And because no predicate names
    `weekly_observation_held`, a store that stops short of epoch 1015 is served
    unchanged and `weekly_held_exclusion` is not needed here. Pinned by
    `tests/test_five_hour_breakdown.py::test_835_r9_a_held_capture_record_names_the_basis_week_it_carried`
    and `::test_835_r9_a_pre_column_store_resolves_the_capture_record`.

    THE SET IS BOUNDED BY THE ACCOUNT'S BLOCK COUNT, NOT BY ITS SNAPSHOT HISTORY,
    and the form was chosen on measurement rather than on shape. Re-measured at Gate
    A R10, because R10 replaced `_account_axis_weeks`' grouped query with a plain
    `SELECT DISTINCT` and the earlier round's figures for that statement no longer
    described what ships. All three were timed in ONE session on a 25,194-row,
    13-week, 50-block store — the store `_account_axis_weeks` documents its own
    numbers against — best of five each, with the order reversed to rule out
    page-cache bias:

      - this narrowed query: 2.10-2.27 ms, 50 rows, `SCAN weekly_usage_snapshots`
        plus a `LIST SUBQUERY` that SEARCHes `five_hour_blocks` on its own index;
      - `_account_axis_weeks`' `SELECT DISTINCT`: 2.06-2.13 ms, 13 rows;
      - one ungrouped full-row scan serving BOTH passes from a single statement:
        14.18-14.38 ms, 25,194 rows, a bare `SCAN weekly_usage_snapshots`.

    So the two statements together cost 4.40 ms against the single statement's
    14.18 ms and retain 63 rows against 25,194 — a 400th. Two statements per account
    per render is the cheaper form on both axes, and it keeps the property that
    matters: the cost is CONSTANT in the number of rendered blocks. Bounded by the
    account's own `five_hour_blocks` rows rather than by which of them this render
    shows, which is why no instant range has to be threaded down from the caller.

    TWO EARLIER ROUNDS DISAGREED ABOUT THE SINGLE-STATEMENT FIGURE — this docstring
    said 22.03 ms and `docs/five-hour-gotchas.md` said 21.97 ms for the same query on
    the same store — and neither number survives, because both were taken against the
    superseded grouped form of the week query. The figures above replace both rather
    than reconciling them.

    THE NARROWING AND THE MATCH USE DIFFERENT READERS, deliberately. SQLite's
    `unixepoch()` selects the rows; `parse_iso_datetime(...).timestamp()` keys
    them and keys the probe, so the match itself is Python's and is consistent
    between map and probe. Both readers truncate to the second, which is how two
    ISO spellings of one moment — `Z` against `+00:00` — compare equal.

    A NAIVE STAMP IS A LIVE SHAPE, and the two readings of it differ. An earlier
    form of this paragraph said no writer produces one, citing `now_utc_iso()`.
    That is wrong: `_coerce_payload_captured_at` (bin/_cctally_record.py:6576-6585)
    returns the PAYLOAD's `capturedAt` verbatim whenever `parse_iso_datetime`
    accepts it, `parse_iso_datetime` accepts a naive string and reads it host-local,
    and `insert_usage_snapshot` then stores that text. Driven with
    `TZ=America/Los_Angeles`: a payload `capturedAt` of `2026-04-30T13:00:00` is
    stored as `captured_at_utc = '2026-04-30T13:00:00'`, and `unixepoch()` reads it
    1777554000 against Python's 1777579200.

    THE FAILURE IS A MIXED PAIR, NOT A NAIVE STAMP, AND IT IS THE MATCH THAT MISSES
    RATHER THAN THE NARROWING THAT EXCLUDES. The same earlier paragraph stated it
    the other way round. Each reader is self-consistent across the two sides it
    compares, so the spelling only matters when the snapshot stamp and the block
    stamp are spelled DIFFERENTLY. All four pairings, driven on that same non-UTC
    host: equal naive text narrows IN and the Python match HITS; `Z` against
    `+00:00` narrows IN and HITS; naive against offset-carrying, in either
    direction, narrows IN and the PYTHON MATCH MISSES, after which the instant falls
    through to the two fallback passes.

    SO THE RESIDUAL IS NARROWER THAN A NAIVE STAMP'S EXISTENCE. Every one of the
    four axis writers takes the block stamp from a `weekly_usage_snapshots` row's
    own `captured_at_utc` text — the live upsert from `saved["capturedAt"]`, the
    backfill from its MIN- and MAX-captured rows, the close from the frozen row, the
    merge migration from the MAX-captured group row — so the pair the capture pass
    compares always shares a spelling and the pass is correct on a naive store. A
    mixed pair needs the two texts to diverge after both were written, which is a
    hand-built or externally-edited store. The degradation is then a fallback rather
    than a misclassification, which is the one part of the earlier paragraph that
    held.
    """
    if account_key in cache:
        return cache[account_key]
    try:
        rows = conn.execute(
            "SELECT captured_at_utc, week_start_date, week_start_at, week_end_at "
            "  FROM weekly_usage_snapshots "
            " WHERE account_key = ? "
            "   AND unixepoch(captured_at_utc) IN ("
            "        SELECT unixepoch(first_observed_at_utc) "
            "          FROM five_hour_blocks WHERE account_key = ? "
            "        UNION "
            "        SELECT unixepoch(last_observed_at_utc) "
            "          FROM five_hour_blocks WHERE account_key = ?)",
            (account_key, account_key, account_key),
        ).fetchall()
    except sqlite3.DatabaseError:
        rows = []
    carried: dict = {}
    for r in rows:
        try:
            second = int(parse_iso_datetime(
                r["captured_at_utc"], "captured_at_utc").timestamp())
        except (ValueError, TypeError):
            continue
        lo, _hi = _parsed_week_bounds(r["week_start_at"], r["week_end_at"])
        identity = (
            r["week_start_date"],
            r["week_start_at"] if lo is not None else None,
            r["week_end_at"] if lo is not None else None,
        )
        # A dict as an ordered set: two rows of one week at one instant are one
        # identity, and `_credit_retirement_bands` would answer both alike.
        carried.setdefault(second, {})[identity] = None
    out = {second: tuple(ids) for second, ids in carried.items()}
    cache[account_key] = out
    return out


def _account_axis_bounds_by_start_date(weeks):
    """``{week_start_date -> that week's bounds-carrying identities}``, from an
    already-loaded `_account_axis_weeks` set.

    #834 S1 (#835) Gate A R10. The index the capture pass substitutes from when its
    matched row carries no bounds of its own. Derived from the account's OWN week
    set and nothing else, which is the property that separates it from
    `_get_canonical_boundary_for_date`; built once per account per render, so the
    substitution adds no query and no per-instant scan.

    Bounds-free weeks are absent by construction: an entry exists only for a
    `week_start_date` some row of that account spells bounds for, which is exactly
    the condition under which a substitution is possible.
    """
    out: dict = {}
    for week in weeks:
        if week.lo is None:
            continue
        out.setdefault(week.week_start_date, {})[
            (week.week_start_date, week.week_start_at, week.week_end_at)] = None
    return {date: tuple(ids) for date, ids in out.items()}


def _block_weekly_axis_weeks(conn, *, account_key, instant_iso, cache):
    """Every week identity whose credits can have retired a value captured at
    ``instant_iso`` under ``account_key``, as ``(week_start_date, week_start_at,
    week_end_at)`` triples. Empty when no week names the instant.

    THREE PASSES, IN THIS ORDER: the CAPTURE pass matches the instant against the
    `captured_at_utc` of the account's own snapshot rows and answers from the week
    those rows name; the EPOCH pass places the instant inside a week's parsed
    `[week_start_at, week_end_at)` bounds; the DATE pass asks which bounds-free
    weeks declare the instant's calendar day. Each runs only when the one before it
    resolved nothing.

    THE CAPTURE PASS IS EXACT ABOUT WHICH WEEK, AND IT REPLACED AN ARGUMENT THAT
    WAS WRONG. Gate A R8 had only the other two, and round five defended the
    over-withholding they cause on the premise that the store keeps no record of
    which week a capture was observed under. It keeps exactly that record: every
    axis stamp on a block IS some snapshot row's `captured_at_utc`, and that row
    carries all four week columns — see `_account_axis_capture_weeks`, which also
    records why held rows are consulted rather than excluded. So for an instant a
    capture record carries there is no week to guess, and neither fallback runs.

    BEING EXACT ABOUT THE WEEK IS NOT THE SAME AS RESOLVING BOTH LEGS, which an
    earlier form of this paragraph left unsaid while the date pass's own paragraphs
    below state it carefully. `week_start_at` and `week_end_at` are nullable on the
    matched row too, and `_credit_retirement_bands`' `week_reset_events` leg is
    scoped purely by containment of `effective_reset_at_utc` in
    ``[week_start_at, week_end_at)`` — that table has no `week_start_date` column at
    all — so an identity carrying no bounds resolves the `weekly_credit_floors` leg
    and NOT the automatic one. Through R9 that degradation combined with this pass's
    precedence into the tranche's own defect class: a bounds-free capture row
    pre-empted a bounds-carrying placement of the SAME week and the value an
    automatic credit retired was published. R10 closes it by SUBSTITUTING the
    account's own bounds (next paragraph), so the per-leg behaviour now is that both
    legs resolve whenever any row of the named week carries parseable bounds, and
    the manual leg alone when none does.

    A BOUNDS-FREE CAPTURE IDENTITY IS REPLACED BY THE ACCOUNT'S OWN BOUNDS-CARRYING
    ROWS OF THE SAME `week_start_date`, and the substitution is lossless. Those rows
    are already in memory from `_account_axis_weeks`, so no query is added. It is
    lossless because every substituted identity shares the bounds-free one's
    `week_start_date`, so each resolves a `weekly_credit_floors` leg identical to the
    one it replaces and ADDS the automatic leg its bounds can scope; the replaced
    identity therefore contributes no band the substitutes do not. Several of them
    are unioned, the way the epoch pass unions overlapping candidates, and that
    union is here FOR ROBUSTNESS rather than for a reachable shape: no writer is
    known to put two bound pairs under one `week_start_date`. What pins one pair per
    date is `_usage_snapshot_columns` (bin/_cctally_record.py:6698-6710), which
    overrides every later row's derived bounds with `_get_canonical_boundary_for_date`
    — the FIRST established pair for that date, `ORDER BY captured_at_utc ASC, id ASC
    LIMIT 1` — so a sub-day re-anchoring is overridden back to the first pair, and a
    day-scale one lands under a DIFFERENT `week_start_date` because the date derives
    from the payload's own week start. No code change is owed either way, because
    `named.update(dict.fromkeys(… or (identity,)))` already handles one through N.
    Two earlier forms of this sentence cited a producer and both were false. Do NOT
    cite the timezone fork: it produces the INVERSE shape, identical bounds under two
    different `week_start_date` values (see the two-identities paragraph below),
    which puts one identity under each date rather than two under one. Do NOT cite
    the boundary-shift branch of `_backfill_week_reset_events`
    (bin/_cctally_weekrefs.py) either: it writes only `week_reset_events` and never
    the snapshot bounds this substitution reads, and its reset test needs THREE
    signals rather than the two an earlier form named — the boundary shifted, the
    prior boundary was still in the future, AND `_is_reset_drop(prior_pct, cur_pct,
    allow_reset_to_zero=False)`, which with the zero leg off is the >=25pp drop
    (`bin/_cctally_weekrefs.py:625-637`).

    CONTAINMENT OF THE INSTANT IS DELIBERATELY NOT REQUIRED OF A SUBSTITUTE, and
    requiring it would reopen the defect on the shape this tranche exists for. A
    HELD row carries the BASIS week's four columns with the tick's own capture
    stamp, so a held tick after the week's end names a week whose bounds do not
    contain its own capture instant. Filtering substitutes by containment would
    decline to enrich exactly there.

    THAT GENERALITY IS WIDER THAN THE HELD-ROW ARGUMENT NEEDS, and the excess is a
    stated limit rather than a defended choice. The held-row shape requires only that
    containment of the CAPTURE instant not be required. Nothing here additionally
    requires a substitute's bounds to be consistent with the `week_start_date` they
    are filed under, so a same-date row spelling bounds that start a week early lets
    the `week_reset_events` leg admit a reset belonging to the PREDECESSOR week, and
    the value is withheld although no credit of the named week retired it. No writer
    produces a date/bounds inconsistency of that size, so the shape needs a
    hand-built or externally-edited store; the narrower rule that would close it is
    to require the substitute's `week_start_at` UTC date to sit within a day of
    `week_start_date`, and it is not taken because the reachable benefit is nil and a
    date/bounds tolerance is one more constant to get wrong.

    NOTHING WIDER THAN THE ACCOUNT'S OWN ROWS IS CONSULTED.
    `_get_canonical_boundary_for_date` (bin/_cctally_weekrefs.py:51) is the repo's
    canonical-bounds reader and the obvious candidate, and it carries no
    `account_key` predicate: it returns the EARLIEST bounds-carrying row for a
    `week_start_date` across every account. On a multi-account store that does not
    merely attach the wrong provenance, it fails to fix the defect, because another
    account's re-anchored bounds cannot scope THIS account's credit — driven and
    pinned by
    `tests/test_five_hour_breakdown.py::test_835_r10_the_substitution_takes_the_accounts_own_bounds_not_another_accounts`.

    ONE RESIDUAL SURVIVES, AND IT IS PRE-EXISTING RATHER THAN INTENDED. When NO row
    of the named week carries parseable bounds there is nothing account-scoped to
    substitute, so an automatic credit in that week is structurally invisible to
    this filter and the value it retired is published. Closing it needs a source of
    a week's bounds outside the account's own snapshot rows, and the paragraph above
    says why the one that exists is not usable. Pinned, as a gap rather than as
    correct behaviour, by the second half of
    `::test_835_r10_a_bounds_free_capture_record_resolves_the_weeks_automatic_credit`.

    A CAPTURE INSTANT CARRIED BY TWO WEEK IDENTITIES RETURNS BOTH. No tie-break is
    invented for it and the caller's union withholds if either week retired the
    value, which is this subsystem's established direction and what both fallback
    passes do as well. The shape is not hypothetical: bin/_cctally_record.py:6601-6614
    records a host briefly running the wrong timezone forking ONE physical week's
    `week_start_date` across 18 rows, and `weekly_credit_floors` matches
    `week_start_date` by equality, so a floor can sit under one fork and not the
    other.

    #834 S1 (#835) Gate A S1. `five_hour_blocks` carries no week columns, and the
    retirement bands need all three: the `weekly_credit_floors` leg matches
    `week_start_date` by equality and the `week_reset_events` leg is scoped by the
    week's bounds. The week is therefore resolved from the account's own snapshot
    rows, whose `week_start_at` / `week_end_at` are the same bounds
    `_credit_retirement_bands` is written against.

    The comparison is on PARSED epochs rather than on text, for the reason every
    SQL site in this area wraps both sides in ``unixepoch()``: `block_start_at` is
    host-local while `week_start_at` is `+00:00`, so a lexicographic compare would
    mis-order them.

    THE TWO LEGS DEGRADE INDEPENDENTLY, the way `_credit_retirement_bands`' own two
    legs already do (#834 S1 Gate A R8). `week_start_at` and `week_end_at` are both
    nullable and `_derive_week_from_payload` leaves them ``None`` on three of its
    four paths, so a week with no bounds is a live shape. Gate A S1 required both
    bounds and resolved NOTHING without them, which threw away a leg it could still
    answer: the `weekly_credit_floors` leg matches on `week_start_date` alone, and
    `week_start_date` / `week_end_date` are `TEXT NOT NULL`. So a bounds-free week
    is placed by DATE instead and returned with ``None`` bounds, which resolves the
    manual-credit leg and skips the `week_reset_events` leg — exactly what
    `_credit_retirement_bands` does with absent bounds.

    A bounds-carrying week outranks a bounds-free one, so the date pass runs only
    when the epoch pass places the instant nowhere. The
    date window is INCLUSIVE of `week_end_date`, because the usual bounds-free week
    carries the week's LAST DAY there: `_derive_week_from_payload`'s third path
    writes `week_start_date + 6 days` and its fourth derives the same through
    `compute_week_bounds`. THE TWO READINGS DO MEET, which an earlier form of this
    docstring denied. `_derive_week_from_payload`'s second path takes both dates
    from the payload verbatim behind nothing but a `weekEndDate >= weekStartDate`
    check, and `_account_axis_weeks` sends a week down this pass when its bounds are
    present but UNPARSEABLE, where `week_end_date` came from the bounds-carrying
    writer and is the exclusive boundary's date by construction. Either shape puts
    one calendar day inside two weeks' inclusive ranges.

    SO THE DATE PASS RETURNS EVERY CANDIDATE THAT CONTAINS THE DAY AND PICKS NONE,
    and the caller unions their bands. Two single-choice rules were tried and each
    published a retired value on the mirror image of the shape it fixed. Rank order
    placed an instant in the PREDECESSOR whenever the predecessor's newest snapshot
    row was the newer of the two. Preferring the greatest `week_start_date` fixed
    that and broke the case where the floor sits on a LONGER declared week
    `2026-04-20..2026-05-04` beside an uncredited `2026-04-27..2026-05-04` — the
    exact shape whose existence is the argument against a seven-day window, so the
    prohibition and that repair rested on one premise pointing opposite ways. No
    reading of one week's own columns separates two candidates that both declare the
    day, so choosing is a guess in either direction and consulting all of them is
    not.

    NEITHER FALLBACK PASS HAS A TIE-BREAK ANY MORE. Both return every candidate they
    place the instant in, and the caller unions the bands. Through Gate A R8 the
    epoch pass returned ONE week — the one whose group held the newest
    `(captured_at_utc, id)`, which is what the since-removed `rk` aggregate ordered
    by — justified by the claim that the newest anchoring is the best available
    evidence of which window was in force at capture. That claim is vacuous on the shape that makes overlapping
    bounds-carrying anchorings reachable at all: bin/_cctally_record.py:6601-6614
    records a host running the wrong timezone for seven minutes forking ONE physical
    week's `week_start_date` across 18 rows, and those two anchorings carry
    IDENTICAL bounds and differ only in the column `weekly_credit_floors` is keyed
    on. Both anchorings are the same window, so there is no "which window was in
    force" to answer, and a newest-anchoring choice decides only which of two
    spellings of one week's floors gets consulted. It also inverts the claim's own
    logic: where two anchorings genuinely differ, that tie-break publishes the
    loser's retired value rather than withholding it.

    The two passes still differ in PRECEDENCE, deliberately. The epoch pass has real
    half-open boundaries to test an instant against, so every candidate it keeps
    genuinely contains the instant; the date pass has no boundaries, only two date
    columns of which a live writer fills one with the EXCLUSIVE boundary's date, so
    "contains the day" there does not mean "belongs to the week". A bounds-carrying
    placement is therefore the more precise one and pre-empts the date pass
    entirely. Pinned by
    `tests/test_five_hour_breakdown.py::test_835_r9_the_epoch_pass_unions_every_bounds_carrying_week_it_contains`
    and `::test_835_r8_a_bounds_carrying_week_outranks_a_bounds_free_one`.

    THE DATE PASS'S UNION OVER-WITHHOLDS, AND THAT RESIDUAL IS SMALLER THAN ROUND
    FIVE'S. When an instant genuinely belongs to an uncredited candidate and an
    overlapping candidate was credited, the union withholds a value no credit ever
    retired. Round five paid that on EVERY block observed on a week-boundary day,
    because `cmd_record_usage` writes `week_end_date = week_start_date + 7 days` —
    the exclusive boundary's date — while this pass compares inclusively, so every
    pair of adjacent weeks shares one calendar day. The capture pass now answers
    every such instant some snapshot row carries, which is the ordinary case, and
    what is left is an instant NO capture record carries on a store whose weeks have
    no parseable bounds.

    The direction is the conservative one — an unavailable marker in preference to a
    number a credit retired — and both fallback passes now take it. It is still not
    the direction EVERY fork in this change takes: an empty result resolves no bands
    and publishes the raw value, which is the pre-#835 disposition kept on purpose.
    `::test_835_r8_the_union_withholds_an_uncredited_weeks_own_value` pins the trade
    rather than leaving a later reader to find it and read it as a defect, and
    `::test_835_r9_a_capture_record_resolves_the_week_a_shared_boundary_day_withheld`
    pins the boundary-day case that no longer pays it.

    A SEVEN-DAY WINDOW OFF `week_start_date` IS STILL NOT AN ALTERNATIVE. It assumes
    every bounds-free week is seven days long, and the second payload path can
    declare a longer one, whose tail a seven-day window would place NOWHERE —
    withdrawing the manual-credit leg this pass exists to resolve. It also buys
    nothing on the weeks whose `week_end_date` is computed rather than declared: the
    third path and `compute_week_bounds` both write `week_start_date + 6 days`, and
    `cmd_record_usage` writes `week_end_date` exactly `week_start_date + 7 days`, so
    over those rows a seven-day exclusive window and this inclusive one name the
    same days and neither separates two overlapping candidates.

    ONE DIRECTION IS BOUNDED BY `cmd_record_credit`, AND THE OTHER IS NOT. That
    command refuses without a canonical weekly boundary and always writes a
    bounds-carrying synthetic snapshot of its own, so a manually credited week
    normally has at least one row with bounds — which bounds the direction where a
    credited week is reachable ONLY through the date pass. It does not bound the
    opposite direction, the one round five introduced: that same credited week's
    OTHER rows still form a bounds-free candidate group, because `week_end_date`
    joins the grouping, so an instant in an adjacent bounds-free week still picks up
    the credited week's floor and over-withholds. Do not read the synthetic snapshot
    as bounding both.

    EMPTY when no week names the instant on any pass. That resolves no bands and
    therefore withholds nothing, which is the pre-#835 behaviour.

    ``cache`` IS THE OUTER RENDER CACHE, NOT A WEEK MEMO, AND IT CHANGED SHAPE
    SILENTLY AT GATE A R8. Gate A S1 passed a dict this function used directly as
    ``{(account_key, instant_iso) -> weeks}``. It now carves its own sub-dicts out
    of the dict the caller owns, and that dict is the same one
    `_credit_aware_block_weekly_axes` carves `__bands` out of, so FOUR reserved
    keys share one namespace:

      ``__weeks``    `{account_key -> [_AxisWeek]}`, `_account_axis_weeks`' memo
      ``__stamps``   `{account_key -> {second -> identities}}`, the capture records
      ``__by_date``  `{account_key -> {week_start_date -> identities}}` (R10)
      ``__bands``    `{(week, account_key) -> bands}`, owned by the caller

    A caller passing the OLD shape is not detected and does not raise: every
    `setdefault` above finds no reserved key, creates a fresh sub-dict, and the
    pre-existing entries are simply never read. The failure is therefore a silent
    loss of memoization — one extra pair of queries per account per render, the cost
    the per-account load exists to remove — and never a wrong answer, because
    nothing reads a key it did not write. The leading double underscore is what
    keeps the two populations apart: an `account_key` is either a 32-character
    lowercase sha256 prefix (`digest.hexdigest()[:32]`, bin/_lib_accounts.py:10,
    :63 and :70) or the literal `unattributed` (bin/_lib_accounts.py:41), so none of
    the four names can collide with one.

    EVERY PASS IS GATED ON `_account_axis_weeks` SUCCEEDING, the capture pass
    included, and that coupling is deliberate. It looks gratuitous, because NEITHER
    QUERY'S COLUMN DEPENDENCY CONTAINS THE OTHER'S, so in principle a store that
    fails one could answer the other. The week query reads `week_start_date`,
    `week_end_date`, `week_start_at` and `week_end_at`; the capture query reads
    `captured_at_utc`, `week_start_date`, `week_start_at` and `week_end_at`, plus
    `first_observed_at_utc` and `last_observed_at_utc` from a SECOND table,
    `five_hour_blocks`. So the divergence has two directions — a store carrying the
    bounds without `week_end_date`, and one carrying `week_end_date` without
    `captured_at_utc` or without the block table — and the claim that the capture
    query's dependency was strictly smaller was FALSE WHEN WRITTEN in R10, because
    the capture query has read a second table since R9. Two earlier statements of
    this are withdrawn: the claim was authored by `f32554505`, which is later than
    `df80ca1dd` that removed the week query's aggregate, so it cannot have "stopped
    being true" in that round; and it was never true, because the capture query has
    selected `first_observed_at_utc` / `last_observed_at_utc` from `five_hour_blocks`
    since `9690c0fae`. Three reasons the gate is kept:

      - The divergence is unreachable. `week_end_date` is `TEXT NOT NULL` in the
        base `CREATE TABLE` (bin/_cctally_core.py:2127) and no migration adds it, so
        no cctally store has the other four columns without it. The legacy shape
        `bin/build-doctor-fixtures.py` builds lacks `week_start_at` / `week_end_at`
        as well, so BOTH queries fail on it together.
      - An empty week set from the ordinary cause — the account has no snapshot rows
        — implies an empty capture map, because the capture query reads the same
        table filtered on the same `account_key`.
      - Since R10 the capture pass READS `weeks`, to build the substitution index
        `_account_axis_bounds_by_start_date` returns, so running it without them
        degrades it to returning bounds-free identities unenriched.

    THAT THIRD REASON IS NOT THAT DECOUPLING WOULD BUY NOTHING. An unenriched
    bounds-free identity still resolves the `weekly_credit_floors` leg, and the empty
    tuple resolves neither leg, so on a store where the week query failed and the
    capture query succeeded, decoupling WOULD buy the manual leg. What makes the gain
    unreachable is reason 1, not any equivalence between the two dispositions. The
    gate is kept because the divergence cannot occur on a store any cctally version
    created, and decoupling would add a second exit path to reason about for a gain
    no store can realise. If a future store shape makes the divergence real, the fix
    is to run the capture pass first and gate only the two fallback passes.
    """
    weeks = _account_axis_weeks(
        conn, account_key=account_key, cache=cache.setdefault("__weeks", {}))
    if not weeks:
        # EVERY pass is gated on the week load, the capture pass included. See the
        # docstring: the coupling is deliberate and, since R10, load-bearing.
        return ()
    try:
        instant = parse_iso_datetime(instant_iso, "axis_instant")
    except (ValueError, TypeError):
        return ()
    moment = instant.timestamp()
    # PASS 1 — the capture record. Truncated to the second on both sides, matching
    # the `unixepoch()` the narrowing query uses; see `_account_axis_capture_weeks`.
    carried = _account_axis_capture_weeks(
        conn, account_key=account_key,
        cache=cache.setdefault("__stamps", {})).get(int(moment))
    if carried:
        # A bounds-free matched row names the week but cannot scope the
        # `week_reset_events` leg, and this pass pre-empts the epoch pass, so
        # substitute the SAME week's bounds as this account's OWN other rows spell
        # them — see the docstring for why the substitution is lossless, why
        # containment is not required of a substitute, and what residual survives.
        named: dict = {}
        for identity in carried:
            if identity[1] is not None:
                named[identity] = None
                continue
            by_date = cache.setdefault("__by_date", {})
            if account_key not in by_date:
                by_date[account_key] = _account_axis_bounds_by_start_date(weeks)
            # Built once per account from the already-loaded week set, so the
            # substitution costs no query and no per-instant scan.
            named.update(dict.fromkeys(
                by_date[account_key].get(identity[0]) or (identity,)))
        return tuple(named)
    # PASS 2 — containment in a week's parsed half-open bounds. A dict as an
    # ordered set throughout: the caller's union is order-insensitive, so the
    # order these are visited in carries no meaning, and membership must not be a
    # linear scan.
    found: dict = {}
    for week in weeks:
        if week.lo is not None and week.lo <= moment < week.hi:
            found[(week.week_start_date, week.week_start_at, week.week_end_at)] = (
                None)
    if found:
        return tuple(found)
    # PASS 3 — the instant's calendar day against the bounds-free weeks' declared
    # date ranges. `.astimezone(utc)` before `.date()` for
    # `_derive_week_from_payload`'s own reason: `parse_iso_datetime` ends in
    # `.astimezone()`, so on a host whose offset puts the moment on another calendar
    # date a bare `.date()` would place the instant in the wrong week. EVERY
    # candidate that declares the day, with no preference among them — see the
    # docstring for why no preference can be grounded here and what the union costs.
    day = instant.astimezone(dt.timezone.utc).date().isoformat()
    for week in weeks:
        if week.lo is not None:
            continue
        # Both columns are `TEXT NOT NULL`, so a None here is a store nobody can
        # place; skip it rather than raise comparing a string to None.
        if week.week_start_date is None or week.week_end_date is None:
            continue
        if week.week_start_date <= day <= week.week_end_date:
            # `week_end_date` is in `_account_axis_weeks`' DISTINCT projection, so
            # two rows differing only there are two candidates carrying ONE identity.
            # The bands would be identical, so the duplicate is dropped rather than
            # resolved twice.
            found[(week.week_start_date, None, None)] = None
    return tuple(found)


def _credit_aware_block_weekly_axes(conn, block, *, now_utc, cache=None):
    """A block's two weekly axes, with a credit-RETIRED value replaced by ``None``.

    #834 S1 (#835) Gate A S1. THE ONE READ-TIME FILTER for
    `five_hour_blocks.seven_day_pct_at_block_start` and
    `seven_day_pct_at_block_end`, called by `cmd_five_hour_blocks` and
    `cmd_five_hour_breakdown` so the two cannot diverge.

    WHY READ TIME, AND WHY A WRITE-TIME FILTER CANNOT SUBSTITUTE. A credit can
    retire a value AFTER the block row was written — that is the ordinary case,
    because the live upsert writes the block's weekly end on every tick while the
    block is open and the credit fires later — so no write-time pass can ever be
    sufficient. FOUR writers store these columns, not three: the live upsert in
    bin/_cctally_record.py, `five_hour_block_close`'s frozen harvest in
    bin/_cctally_journal.py, `_backfill_five_hour_blocks` below, and
    `_migration_merge_5h_block_duplicates_v1` in bin/_cctally_db.py, which writes
    `seven_day_pct_at_block_end` from the group row whose `last_observed_at_utc` is
    MAX. The migration was omitted from the original count; the argument is
    unaffected, because it also stores what was observed, but the number was
    documented and wrong. All four store what the fold OBSERVED; this one read
    decides what a credit retired may still be shown. Making one writer
    credit-aware instead would also break
    rebuild convergence, because the store would then hold a number where a
    rebuilt store holds `NULL` and `tests/test_rederive_command.py` compares
    exactly these two columns across a rebuild.

    EACH AXIS IS COMPARED AGAINST THE INSTANT THAT AXIS'S VALUE WAS CAPTURED AT,
    never against a boundary of the block: the start against
    `_block_weekly_start_instant` (`first_observed_at_utc`), the end against
    `_block_weekly_end_instant` (`last_observed_at_utc`). Gate A S1 used the block's
    own boundary stamps instead and that was wrong in both directions — see those
    two helpers. The rule separates the three positions a block's OBSERVATIONS can
    hold relative to a credit floor. Observations entirely BEFORE the floor render
    both stored values, because they genuinely were effective while the block ran
    and withholding them would destroy historical truth. Observations SPANNING the
    floor render the start and withhold a retired end. Observations entirely AFTER
    the floor withhold both, which is the shape a post-credit replay at the retired
    value produces.

    The bands come from the block's OWN week and `account_key` — a credit under
    one account retires nothing under another, and a crossed-reset block's two
    axes legitimately resolve two different weeks.

    ``cache`` is an optional dict shared across a multi-row render, and it holds
    FOUR reserved keys in ONE namespace: this function's own ``__bands``, resolved
    once per `(week, account_key)`, plus the ``__weeks``, ``__stamps`` and
    ``__by_date`` that `_block_weekly_axis_weeks` carves out of the same dict. That
    function's docstring states each one's shape and what happens to a caller who
    passes the pre-R8 shape, which is a silent loss of memoization rather than a
    wrong answer. Together they hold the account's whole week set, the capture
    records of that account's block axis stamps, the per-`week_start_date` index of
    bounds-carrying identities, and the bands. Each axis instant is then resolved
    against the loaded sets in memory, so the number of queries does not grow with
    the number of rendered blocks. The caller owns the dict and it must not outlive
    the connection.
    """
    _c = _cctally()
    p_start = block.get("seven_day_pct_at_block_start")
    p_end = block.get("seven_day_pct_at_block_end")
    if p_start is None and p_end is None:
        return None, None
    if cache is None:
        cache = {}
    bands = cache.setdefault("__bands", {})
    account_key = block.get("account_key") or _lib_accounts.UNATTRIBUTED
    out = []
    for value, instant in (
        (p_start, _block_weekly_start_instant(block)),
        (p_end, _block_weekly_end_instant(block, now_utc=now_utc)),
    ):
        if value is None or not instant:
            out.append(value)
            continue
        week_ids = _block_weekly_axis_weeks(
            conn, account_key=account_key, instant_iso=instant, cache=cache)
        if not week_ids:
            out.append(value)
            continue
        # THE UNION OF EVERY CANDIDATE WEEK'S BANDS, so a value any one of them
        # retired is withheld. One bounds-carrying week is one candidate; several
        # bounds-free ones are several, because nothing in their own columns says
        # which of them an instant belongs to. Each is still resolved by
        # `_credit_retirement_bands` from its OWN triple, so the per-leg
        # degradation is unchanged: a bounds-free candidate contributes its
        # `weekly_credit_floors` leg and no `week_reset_events` leg, which cannot
        # be scoped without bounds. The memo stays per `(week, account_key)`, so
        # overlapping candidates shared across blocks resolve once each.
        merged: list = []
        for week in week_ids:
            bk = (week, account_key)
            if bk not in bands:
                bands[bk] = _c._credit_retirement_bands(
                    conn, week_start_date=week[0], week_start_at=week[1],
                    week_end_at=week[2], account_key=account_key)
            merged.extend(bands[bk])
        out.append(
            None if _c._weekly_value_is_retired_replica(
                merged, captured_at_utc=instant, weekly_percent=value)
            else value)
    return out[0], out[1]


#: The six `five_hour_milestones` columns every surface already published, plus
#: the joined EFFECTIVE weekly value. Ordering is `captured_at_utc, id` — spec
#: §5.2's merged-stream order, which post-credit threshold repeats depend on.
#: The five `s.*` columns after the joined weekly value are INTERNAL. They exist
#: only so the credit retirement bands can be resolved against the JOINED ROW's
#: own account and week, and they are aliased because `captured_at_utc` exists on
#: both sides of the join. `_load_five_hour_milestones` builds its result dicts
#: key by key, so none of them can reach a published shape.
_FIVE_HOUR_MILESTONE_SQL = """
    SELECT m.percent_threshold, m.captured_at_utc, m.block_cost_usd,
           m.marginal_cost_usd, m.seven_day_pct_at_crossing, m.reset_event_id,
           s.weekly_percent AS effective_seven_day_pct_at_crossing,
           s.captured_at_utc AS snapshot_captured_at_utc,
           s.week_start_date AS snapshot_week_start_date,
           s.week_start_at   AS snapshot_week_start_at,
           s.week_end_at     AS snapshot_week_end_at,
           s.account_key     AS snapshot_account_key
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

    **The joined value is itself CREDIT-AWARE** (#834 S1 Gate A R2). A plain
    `LEFT JOIN` is not enough: `maybe_record_milestone` sets `usage_snapshot_id`
    to the row the tick just wrote, and the FIVE-HOUR milestone is not gated off
    on a weekly-clamped tick — only the weekly one is — so in a credited week the
    milestone can name a HELD row carrying a pre-credit weekly value the credit
    retired. Publishing that as the "effective" value would render the exact
    number #836 exists to remove, on all three human surfaces, on a new read.

    So the retirement bands are resolved against the JOINED ROW's own account and
    week — never the reader's, because a credit under one account retires nothing
    under another — memoized per `(week, account)` across the rows of one call, and
    a retired joined value is published as None. The surfaces already render a None
    as the unavailable marker, so no renderer changes, and the rule above still
    holds: never a fallback to the raw value. (The memoization is this function's
    own: it walks many rows spanning potentially several `(week, account)` pairs.
    `_latest_seven_day_and_window` does NOT memoize, because its walk resolves the
    scope, the bands and the floor once from the newest row and may not leave that
    scope, so there is nothing to memoize per candidate. An earlier version of this
    paragraph said it did.)

    THE BAND IS APPLIED HERE WITHOUT R1's FLOOR BOUND, deliberately. That bound
    exists because a WALK can descend past every post-floor row onto a pre-floor
    one and return the pre-credit reading the band exists to suppress. This read
    performs no walk: each milestone names exactly ONE snapshot row through
    `usage_snapshot_id`, and a pre-floor row's weekly value genuinely WAS the
    effective percentage at that crossing instant. Blanking it would delete
    historical truth rather than protect the reader, which is the same reason the
    block's pre-floor weekly axes render.

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
    _c = _cctally()
    bands_cache: dict = {}
    out: list[dict] = []
    for r in rows:
        eff = r["effective_seven_day_pct_at_crossing"]
        raw = r["seven_day_pct_at_crossing"]
        if eff is not None:
            band_key = (r["snapshot_week_start_date"],
                        r["snapshot_week_start_at"],
                        r["snapshot_week_end_at"],
                        r["snapshot_account_key"])
            if band_key not in bands_cache:
                bands_cache[band_key] = _c._credit_retirement_bands(
                    conn,
                    week_start_date=r["snapshot_week_start_date"],
                    week_start_at=r["snapshot_week_start_at"],
                    week_end_at=r["snapshot_week_end_at"],
                    account_key=r["snapshot_account_key"])
            if _c._weekly_value_is_retired_replica(
                    bands_cache[band_key],
                    captured_at_utc=r["snapshot_captured_at_utc"],
                    weekly_percent=eff):
                eff = None
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
            cum = f"${m['blockCostUSD']:.6f}"
            marg = (
                "n/a" if m["marginalCostUSD"] is None
                else f"${m['marginalCostUSD']:.6f}"
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
    """
    _c = _cctally()
    inserted = 0
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
            keys_sql += (
                "   AND snapshots.id = ("
                "       SELECT latest.id"
                "         FROM weekly_usage_snapshots AS latest"
                "        WHERE latest.account_key IS snapshots.account_key"
                "          AND latest.five_hour_window_key IS NOT NULL"
                "          AND latest.five_hour_percent IS NOT NULL"
                "        ORDER BY unixepoch(latest.captured_at_utc) DESC,"
                "                 latest.id DESC"
                "        LIMIT 1"
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
            # Same MIN-captured pick the insert loop makes below, so the
            # ownership interval and the inserted row cannot disagree.
            anchor_row = conn.execute(
                """
                SELECT five_hour_resets_at
                  FROM weekly_usage_snapshots
                 WHERE five_hour_window_key = ? AND account_key = ?
                   AND five_hour_percent IS NOT NULL
                 ORDER BY captured_at_utc ASC, id ASC LIMIT 1
                """,
                (key, acct),
            ).fetchone()
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
            for key, acct in keys:
                # MIN-captured row defines the immutable block boundary
                # values (deterministic — picking "any in-window row"
                # would be nondeterministic under seconds-level Anthropic
                # ISO jitter that the canonical key collapses). Scoped to
                # (window_key, account_key) so a shared window's per-account
                # blocks each read their own account's boundary rows (#341).
                min_row = conn.execute(
                    """
                    SELECT five_hour_resets_at, captured_at_utc, weekly_percent
                      FROM weekly_usage_snapshots
                     WHERE five_hour_window_key = ? AND account_key = ?
                       AND five_hour_percent IS NOT NULL
                     ORDER BY captured_at_utc ASC, id ASC LIMIT 1
                    """,
                    (key, acct),
                ).fetchone()
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
                totals = _c._compute_block_totals(
                    block_start_dt, last_obs_dt, skip_sync=True,
                    owner_key=key, windows=ownership.get(acct, []),
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
                      is_closed,
                      created_at_utc,
                      last_updated_at_utc,
                      account_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
