"""5-hour-window command family.

Holds the three 5h commands — `cmd_blocks`, `cmd_five_hour_blocks`,
`cmd_five_hour_breakdown` — their family-local helpers, AND the shared 5h
recorded-window resolution layer (`_load_recorded_five_hour_windows`,
`_select_non_overlapping_recorded_windows`, `_maybe_swap_active_block_to_canonical`,
`_resolve_block_selector`, `_CANONICAL_WEIGHT_THRESHOLD`).

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

from _cctally_core import _command_as_of, eprint, open_db, parse_iso_datetime


def _cctally():
    """Resolve the current `cctally` module at call-time (spec §3.1)."""
    return sys.modules["cctally"]


def _resolve_block_selector(
    conn: sqlite3.Connection,
    *,
    block_start: str | None,
    ago: int | None,
) -> dict | None:
    """Resolve a five-hour-breakdown selector to one ``five_hour_blocks`` row.

    Returns a dict-mapped ``sqlite3.Row`` (or ``None`` if no block matches).
    Raises ``ValueError`` on conflicting / malformed input.

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
            "SELECT * FROM five_hour_blocks WHERE five_hour_window_key = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None

    # Default or --ago: order DESC by block_start_at, take the (ago or 0)-th.
    offset = int(ago) if ago is not None else 0
    if offset < 0:
        raise ValueError(f"--ago must be non-negative (got {ago})")
    row = conn.execute(
        """
        SELECT * FROM five_hour_blocks
         ORDER BY block_start_at DESC, id DESC
         LIMIT 1 OFFSET ?
        """,
        (offset,),
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
                "SELECT five_hour_resets_at "
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
                    "SELECT five_hour_resets_at, block_start_at "
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
    counts: dict[dt.datetime, int] = {}
    for row in rows:
        raw = row["five_hour_resets_at"] if hasattr(row, "keys") else row[0]
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
        snapped = _c._floor_to_ten_minutes(d)
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
    canonical_pairs: list[tuple[dt.datetime, dt.datetime]] = []
    for row in canonical_rows:
        rs_raw = row["five_hour_resets_at"] if hasattr(row, "keys") else row[0]
        bs_raw = row["block_start_at"]      if hasattr(row, "keys") else row[1]
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
        canonical_pairs.append((bs, rs))
    canonical_pairs.sort(key=lambda p: p[0])

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
    for bs, rs in canonical_pairs:
        snapped = _c._floor_to_ten_minutes(rs)
        canonical_intervals[snapped] = (bs, rs)

    # Detect overlap-with-credit and replace the earlier R with a
    # credit-truncated anchor. The (anchor → real_block_start) map is
    # returned alongside the anchor list so the renderer can show the
    # real block_start_at on the display row (instead of the default
    # R - 5h, which would be hours earlier for a 2h-truncated block).
    block_start_overrides: dict[dt.datetime, dt.datetime] = {}
    truncated_pairs: list[tuple[dt.datetime, dt.datetime]] = []
    for i, (bs, rs) in enumerate(canonical_pairs):
        truncated_R = rs
        if i + 1 < len(canonical_pairs):
            next_bs, _next_rs = canonical_pairs[i + 1]
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
                            block_start_overrides[cm_floored] = bs
                            # Rewrite canonical_intervals[snapped_orig]
                            # to the truncated interval under the
                            # truncated key. issue #76: the
                            # partitioner reads canonical_intervals
                            # for the exact bs/rs; the truncated entry
                            # must reflect the credit-shifted upper
                            # bound (cm_floored) AND the real bs (the
                            # override) so partition + Phase 1.5
                            # render the credit-shortened block
                            # consistently.
                            snapped_orig = _c._floor_to_ten_minutes(rs)
                            canonical_intervals.pop(snapped_orig, None)
                            canonical_intervals[cm_floored] = (
                                bs, cm_floored,
                            )
                            break
        truncated_pairs.append((bs, truncated_R))

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
    for bs, rs in truncated_pairs:
        snapped = _c._floor_to_ten_minutes(rs)
        if rs != _c._floor_to_ten_minutes(rs):
            if rs in block_start_overrides:
                block_start_overrides[snapped] = block_start_overrides.pop(rs)
        # Identify truncated anchors by membership in the override map
        # (only credit-truncated entries land there).
        if snapped in block_start_overrides:
            truncated_anchors.add(snapped)
        counts[snapped] = counts.get(snapped, 0) + _CANONICAL_WEIGHT_THRESHOLD

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
    _maybe_swap_active_block_to_canonical(blocks, all_entries, now=now_utc, mode=args.mode)

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
                print('{\n  "blocks": [],\n  "message": "No active block"\n}')
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


def _maybe_swap_active_block_to_canonical(
    blocks: list[Any],
    all_entries: list[Any],
    *,
    now: dt.datetime,
    mode: str = "auto",
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
    # Re-aggregate entries over the canonical interval. Build a fresh
    # Block via ``_build_activity_block`` so every total stays in one code
    # path — no field-by-field assignment that could drift if the dataclass
    # grows new fields. Thread the caller's ``mode`` so the active block's
    # cost honors --mode like the main grouping (Session C / Codex F1).
    canonical_entries = [
        e for e in all_entries
        if block_start_utc <= e.timestamp < block_end_utc
    ]
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
    """
    _c = _cctally()
    return _c.format_display_dt(iso, tz, fmt="%Y-%m-%d %H:%M", suffix=True)


def _format_hhmm_in_tz(iso: str, tz: "ZoneInfo | None") -> str:
    """Render the HH:MM portion of an ISO timestamp in the resolved tz.

    Mirrors ``_format_block_start``'s tz resolution so paired start/end
    cells in the same row stay in the same zone. Naive inputs are
    treated as UTC; ``tz=None`` means host-local. No suffix.
    """
    _c = _cctally()
    return _c.format_display_dt(iso, tz, fmt="%H:%M", suffix=False)


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
    conn: sqlite3.Connection,
) -> tuple[float | None, int | None]:
    """Return ``(latest_7d_percent, latest_5h_window_key)`` from
    ``weekly_usage_snapshots``.

    Selects the most-recent snapshot row regardless of whether the 5h
    fields are populated (some rows lack a ``five_hour_window_key``).
    Either or both elements may be ``None``. Used by
    ``cmd_five_hour_breakdown`` to override
    ``seven_day_pct_at_block_end`` on the active row.
    """
    try:
        row = conn.execute(
            """
            SELECT weekly_percent, five_hour_window_key
              FROM weekly_usage_snapshots
             ORDER BY captured_at_utc DESC, id DESC
             LIMIT 1
            """
        ).fetchone()
    except sqlite3.DatabaseError:
        return None, None
    if row is None:
        return None, None
    pct = row[0]
    key = row[1]
    return (
        float(pct) if pct is not None else None,
        int(key) if key is not None else None,
    )


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
            extra = conn.execute(
                """
                SELECT 1 FROM five_hour_blocks
                 ORDER BY block_start_at DESC, id DESC
                 LIMIT 1 OFFSET ?
                """,
                (cap,),
            ).fetchone()
            truncated = extra is not None

        # Latest live 7d% from the latest weekly_usage_snapshots row, used
        # to fill seven_day_pct_at_block_end on the active row.
        latest_7d, latest_window_key = _latest_seven_day_and_window(conn)

        # Pre-load credit events for every window_key the rows query
        # returned. Single index-scan over `five_hour_reset_events`;
        # build a window_key -> list[Credit] map keyed for in-process
        # JOIN against each block dict. Used by both the text/JSON
        # render path AND the share-output snapshot wiring (spec §5.1.1).
        # Loaded in a single pass — no per-block SELECT.
        credit_rows = conn.execute(
            "SELECT five_hour_window_key, prior_percent, post_percent, "
            "       effective_reset_at_utc "
            "  FROM five_hour_reset_events "
            " ORDER BY five_hour_window_key, effective_reset_at_utc"
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
        block_dicts: list[dict] = []
        for r in rows:
            d = dict(r)
            is_active = _block_is_active(d, latest_window_key, now_utc)
            d["__is_active"] = is_active
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
                tail = block_dicts[-1].get("block_start_at")
                period_start = _c._share_parse_date_to_dt(
                    (tail or "").split("T")[0] or None,
                    args._resolved_tz,
                )
            else:
                period_start = _c._share_now_utc()
            if until_iso:
                period_end = _c._share_parse_date_to_dt(
                    until_iso, args._resolved_tz,
                )
            elif block_dicts:
                head = block_dicts[0].get("block_start_at")
                period_end = _c._share_parse_date_to_dt(
                    (head or "").split("T")[0] or None,
                    args._resolved_tz,
                )
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
                theme=args.theme,
                reveal_projects=args.reveal_projects,
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
            print(json.dumps(
                _c._five_hour_blocks_to_json(
                    block_dicts, since_iso, until_iso,
                    cap, truncated, args.breakdown,
                ),
                indent=2,
            ))
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
    conn = open_db()
    try:
        try:
            block = _resolve_block_selector(
                conn,
                block_start=args.block_start,
                ago=args.ago,
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

        # Spec §5.2: ORDER BY captured_at_utc ASC (NOT percent_threshold)
        # so post-credit segments interleave with pre-credit ones in
        # time-order — same human threshold number can appear twice
        # (once per reset_event_id segment) and must render in the
        # order it crossed. Bucket B per §3.2: read ALL segments (no
        # ``reset_event_id`` filter).
        milestones = conn.execute(
            """
            SELECT percent_threshold, captured_at_utc,
                   block_cost_usd, marginal_cost_usd,
                   seven_day_pct_at_crossing, reset_event_id
              FROM five_hour_milestones
             WHERE block_id = ?
             ORDER BY captured_at_utc ASC, id ASC
            """,
            (block["id"],),
        ).fetchall()

        # Spec §5.2 — load in-place credit events for this block's
        # window, ascending by effective_reset_at_utc, so the text
        # renderer can interleave a ``⚡ CREDIT  -Xpp @ HH:MM`` divider
        # row between pre- and post-credit milestone segments and JSON
        # consumers see the parallel ``credits[]`` array (Section 5.2).
        credit_rows = conn.execute(
            """
            SELECT effective_reset_at_utc, prior_percent, post_percent
              FROM five_hour_reset_events
             WHERE five_hour_window_key = ?
             ORDER BY effective_reset_at_utc ASC
            """,
            (block["five_hour_window_key"],),
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
        p_start = block.get("seven_day_pct_at_block_start")
        p_end = block.get("seven_day_pct_at_block_end")

        # Live 7d_end on active row.
        latest_7d, latest_window_key = _latest_seven_day_and_window(conn)
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
                "resetEventId":          int(m["reset_event_id"] or 0),
            }
            for m in milestones
        ]

        if args.json:
            # Spec §5.2: ``credits`` is the parallel array to
            # ``milestones`` — same shape as the ``credits`` field on
            # ``five-hour-blocks --json`` (§5.1). Stacked credits across
            # distinct 10-min slots produce multiple entries.
            print(json.dumps(
                {
                    "schemaVersion": 1,
                    "block": block_out,
                    "milestones": ms_out,
                    "credits": credits_list,
                },
                indent=2,
            ))
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
            p7d = (
                "—" if m["sevenDayPctAtCrossing"] is None
                else f"{m['sevenDayPctAtCrossing']:.0f}%"
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


