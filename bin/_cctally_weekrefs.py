"""WeekRef / reset-event cluster (impure-glue sibling).

Holds the seven DB-touching WeekRef helpers + the two reset-drop-threshold
constants that drive mid-week reset-event detection:
`_get_canonical_boundary_for_date`, `get_recent_weeks`,
`_apply_reset_events_to_weekrefs`, `_backfill_week_reset_events`,
`_week_ref_has_reset_event`, `_compute_cost_for_weekref`,
`_apply_overlap_clamp_to_weekrefs`, `_RESET_PCT_DROP_THRESHOLD`,
`_FIVE_HOUR_RESET_PCT_DROP_THRESHOLD`.

These operate on the `WeekRef` type and take `sqlite3.Connection` — the
IMPURE counterpart to the PURE `SubWeek` math in `_lib_subscription_weeks.py`
(which owns `_apply_reset_events_to_subweeks` / `_apply_overlap_clamp_to_subweeks`).

Honest *name* imports are KERNEL-ONLY (`_cctally_core`). The three
cctally-ns re-exports this module needs — `_floor_to_hour` (of `_lib_blocks`),
`_clamp_end_ats_to_next_start` (of `_lib_subscription_weeks`), and
`_sum_cost_for_range` (defined in `bin/cctally`) — are reached via the
call-time `c = _cctally()` accessor so test monkeypatches through the
`cctally` namespace are preserved. (No `for c in ...` row-loop in this
cluster → the accessor binds the conventional `c`.)

bin/cctally eager-re-exports all 7 functions + 2 constants; consumers reach
them via `c.` (forecast/percent_breakdown/view_models/tui/core/diff_kernel)
or bare `def`-shims (record.py). No consumer source edits.

Spec: docs/superpowers/specs/2026-06-01-extract-weekrefs-5h-backfill-design.md
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from dataclasses import replace

from _cctally_core import (
    WeekRef,
    _canonicalize_optional_iso,
    make_week_ref,
    parse_iso_datetime,
)


def _cctally():
    """Resolve the current `cctally` module at call-time."""
    return sys.modules["cctally"]


def _get_canonical_boundary_for_date(
    conn: sqlite3.Connection,
    week_start_date_str: str,
) -> tuple[str | None, str | None]:
    """Return the first established (week_start_at, week_end_at) for a week."""
    row = conn.execute(
        """
        SELECT week_start_at, week_end_at
        FROM weekly_usage_snapshots
        WHERE week_start_date = ?
          AND week_start_at IS NOT NULL AND week_start_at != ''
          AND week_end_at IS NOT NULL AND week_end_at != ''
        ORDER BY captured_at_utc ASC, id ASC
        LIMIT 1
        """,
        (week_start_date_str,),
    ).fetchone()
    if row:
        start_at = _canonicalize_optional_iso(row["week_start_at"], "weekStartAt")
        end_at = _canonicalize_optional_iso(row["week_end_at"], "weekEndAt")
        if start_at and end_at:
            return start_at, end_at
    return None, None


def get_recent_weeks(conn: sqlite3.Connection, limit: int) -> list[WeekRef]:
    rows = conn.execute(
        """
        SELECT week_start_date, MAX(week_end_date) AS week_end_date
        FROM (
          SELECT week_start_date, week_end_date FROM weekly_usage_snapshots
          UNION ALL
          SELECT week_start_date, week_end_date FROM weekly_cost_snapshots
        )
        GROUP BY week_start_date
        ORDER BY week_start_date DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    refs: list[WeekRef] = []
    for row in rows:
        date_str = row["week_start_date"]
        canon_start, canon_end = _get_canonical_boundary_for_date(conn, date_str)
        try:
            ref = make_week_ref(
                week_start_date=date_str,
                week_end_date=row["week_end_date"],
                week_start_at=canon_start,
                week_end_at=canon_end,
            )
        except ValueError:
            continue
        refs.append(ref)
    # Reset-event boundary override runs BEFORE the generic overlap clamp.
    # After the override, pre/post-reset refs are contiguous at the reset
    # moment, so the clamp becomes a no-op for them; for installs with no
    # reset events the clamp still does all the work it did before.
    return _apply_overlap_clamp_to_weekrefs(
        _apply_reset_events_to_weekrefs(conn, refs)
    )


def _apply_reset_events_to_weekrefs(
    conn: sqlite3.Connection, refs: list[WeekRef]
) -> list[WeekRef]:
    """Override API-derived boundaries with reset-event effective moments.

    For each row in week_reset_events:
      - A ref whose week_end_at matches `old_week_end_at` was the PRE-reset
        week: its API-declared end is in the future but Anthropic cut it
        early. Override ref.week_end_at = effective_reset_at_utc so display
        shows the real cut-off.
      - A ref whose week_end_at matches `new_week_end_at` is the POST-reset
        week: its API-derived start (= new resets_at - 7d) backdates into
        the pre-reset week. Override ref.week_start_at = effective_reset_at_utc
        so the new week starts at the actual reset moment.
      - **In-place credit (v1.7.2 round-3, Bug B).** Detected via the row
        shape ``old_week_end_at == effective_reset_at_utc`` (the live and
        backfill detection paths both write this shape — see
        ``test_event_row_old_is_effective_not_cur_end``). For these events,
        the credited week's ref matches ``new_week_end_at`` (the original
        resets_at is unchanged), so the post-credit override above
        rewrites ``week_start_at`` to ``effective``. But the pre-credit
        segment of the SAME week — where the user spent the bulk of their
        usage before the credit — is dropped, because no other ref in
        ``refs`` carries ``week_end_at == effective``. Synthesize a
        pre-credit ref alongside the post-credit one: its
        ``week_start_at`` stays at the ref's original API-derived value,
        its ``week_end_at`` becomes ``effective`` (closes the pre-credit
        segment). Credited weeks render as TWO trend rows downstream.

    The ref's `week_start` (date) and `key` fields are intentionally left at
    the API-derived values — they're the lookup keys for
    weekly_usage_snapshots / weekly_cost_snapshots. Only the display-facing
    `week_start_at` / `week_end_at` (and the derived `week_end` date) shift.
    Both the pre-credit and post-credit synthesized refs share the same
    `key` so downstream per-segment readers
    (``cmd_percent_breakdown`` / dashboard milestone panel) can still
    filter milestones by ``reset_event_id`` against the same lookup keys.
    """
    events = conn.execute(
        "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
        "FROM week_reset_events"
    ).fetchall()
    if not events:
        return refs
    pre_map  = {e["old_week_end_at"]: e["effective_reset_at_utc"] for e in events}
    post_map = {e["new_week_end_at"]: e["effective_reset_at_utc"] for e in events}
    # In-place credit events have `old == effective` (the row shape the
    # live + backfill detection paths agree on). Project the set of
    # `new_week_end_at` values for those events so we can detect them
    # while iterating refs and split the credited week into TWO refs.
    in_place_credit_new_ends: set[str] = {
        e["new_week_end_at"]
        for e in events
        if e["old_week_end_at"] == e["effective_reset_at_utc"]
    }
    out: list[WeekRef] = []
    for ref in refs:
        new_ref = ref
        if ref.week_end_at and ref.week_end_at in pre_map:
            reset_at = pre_map[ref.week_end_at]
            try:
                reset_dt = parse_iso_datetime(reset_at, "reset_event.effective")
                # internal fallback: host-local intentional
                new_end_date = (reset_dt - dt.timedelta(seconds=1)).astimezone().date()
                new_ref = replace(new_ref, week_end_at=reset_at, week_end=new_end_date)
            except ValueError:
                pass
        if ref.week_end_at and ref.week_end_at in post_map:
            reset_at = post_map[ref.week_end_at]
            # In-place credit: synthesize a pre-credit ref FIRST so it
            # lands in `out` before the post-credit ref. The pre-credit
            # ref keeps the ORIGINAL API-derived week_start_at; only its
            # week_end_at shifts to `effective`. The post-credit ref
            # (constructed below via the standard `replace`) carries
            # week_start_at = effective, week_end_at = original.
            # Order: pre-credit BEFORE post-credit so chronological
            # iteration in cmd_report's trend table renders them
            # naturally (older segment above the newer one in DESC
            # ordering: post-credit is "more recent" so the post-credit
            # row should come FIRST in the DESC list — but the original
            # ref was already in DESC position, and we insert pre-credit
            # AFTER the post-credit. Concretely: post-credit takes the
            # ref's original slot; pre-credit goes one slot later).
            if ref.week_end_at in in_place_credit_new_ends:
                try:
                    reset_dt = parse_iso_datetime(
                        reset_at, "reset_event.effective"
                    )
                    pre_end_date = (
                        # internal fallback: host-local intentional
                        reset_dt - dt.timedelta(seconds=1)
                    ).astimezone().date()
                    pre_credit_ref = replace(
                        ref,
                        week_end_at=reset_at,
                        week_end=pre_end_date,
                    )
                except ValueError:
                    pre_credit_ref = None
            else:
                pre_credit_ref = None
            new_ref = replace(new_ref, week_start_at=reset_at)
            out.append(new_ref)
            if pre_credit_ref is not None:
                out.append(pre_credit_ref)
            continue
        out.append(new_ref)
    return out


def _backfill_week_reset_events(conn: sqlite3.Connection) -> None:
    """One-shot scan over historical snapshots to synthesize reset events
    for past mid-week resets the tool lived through before this feature
    shipped. Idempotent via UNIQUE(old_week_end_at, new_week_end_at) +
    INSERT OR IGNORE — safe to re-run, safe to ship alongside the DDL.

    Rule mirrors the runtime detection in cmd_record_usage: when a new
    week_end_at arrives in a snapshot whose captured_at_utc is still
    BEFORE the prior week's end, that's a mid-week reset. Boundary ISO
    strings get canonicalized via `_canonicalize_optional_iso` and the
    effective reset moment is floored to the hour via `_floor_to_hour`
    so minute/second-level Anthropic jitter ("in X hr Y min" relative-text
    drift) doesn't masquerade as a reset.

    ONE deliberate divergence from the live rule: backfill passes
    ``allow_reset_to_zero=False`` to ``_is_reset_drop``, so it fires only on
    the unambiguous ``>=25pp`` drop. The lenient reset-to-zero signal is
    live-only — the live path debounces a transient API zero (issue #128),
    but this one-shot historical scan has no debounce and would otherwise
    mis-read a stale-replica 0% blip (``6% → 0% → 1%`` on a still-future
    week_end) as a credit, segmenting the week into a degenerate zero-width
    window. See ``_is_reset_drop`` for the full rationale.
    """
    c = _cctally()
    try:
        rows = conn.execute(
            "SELECT captured_at_utc, week_end_at, weekly_percent "
            "FROM weekly_usage_snapshots "
            "WHERE week_end_at IS NOT NULL "
            "ORDER BY captured_at_utc ASC, id ASC"
        ).fetchall()
    except sqlite3.DatabaseError:
        return
    # Canonicalized (hour-rounded) previous end; stored canonical form is
    # what WeekRef.week_end_at carries after make_week_ref, so maps in
    # _apply_reset_events_to_weekrefs stay joinable without extra parsing.
    prior_end = None
    prior_pct: float | None = None
    for row in rows:
        cur_end_raw = row["week_end_at"]
        cur_pct = row["weekly_percent"]
        if not cur_end_raw:
            continue
        try:
            cur_end = _canonicalize_optional_iso(cur_end_raw, "backfill.cur")
        except ValueError:
            continue
        if cur_end is None:
            continue
        if prior_end and cur_end != prior_end:
            try:
                prior_end_dt = parse_iso_datetime(prior_end, "backfill.prior")
                captured_dt  = parse_iso_datetime(row["captured_at_utc"], "backfill.cap")
            except ValueError:
                prior_end = cur_end
                prior_pct = cur_pct
                continue
            # Real mid-week reset needs three signals:
            # 1. Boundary shifted (already checked).
            # 2. Prior boundary was still in the future (not natural rollover).
            # 3. weekly_percent dropped substantially (prior_pct - cur_pct
            #    >= RESET_PCT_DROP_THRESHOLD). Filters out API jitter where
            #    Anthropic briefly reported a different reset_at but usage
            #    stayed roughly the same.
            if (
                captured_dt < prior_end_dt
                and prior_pct is not None and cur_pct is not None
                and _is_reset_drop(prior_pct, cur_pct, allow_reset_to_zero=False)
            ):
                # Floor to the hour so the display boundary lands on the
                # natural hour mark (Anthropic's reset times are always
                # hour-aligned, and users think of weeks in hour-mark
                # units). A reset at 18:08Z becomes 18:00Z in the event
                # row, rendering as "21:00" local instead of "21:08".
                effective_iso = c._floor_to_hour(captured_dt).isoformat(timespec="seconds")
                conn.execute(
                    "INSERT OR IGNORE INTO week_reset_events "
                    "(detected_at_utc, old_week_end_at, new_week_end_at, "
                    " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
                    (row["captured_at_utc"], prior_end, cur_end, effective_iso),
                )
        elif prior_end and cur_end == prior_end:
            # In-place credit branch (v1.7.2). Mirrors the live detection
            # in cmd_record_usage: same end_at across two captures + ≥25pp
            # drop in weekly_percent + prior end still in the future at
            # captured_dt → Anthropic-issued goodwill credit. One event
            # row with old == new == cur_end, effective = floor_to_hour
            # of the captured_at when the drop was first observed.
            try:
                prior_end_dt = parse_iso_datetime(prior_end, "backfill.prior")
                captured_dt  = parse_iso_datetime(row["captured_at_utc"], "backfill.cap")
            except ValueError:
                prior_end = cur_end
                prior_pct = cur_pct
                continue
            if (
                captured_dt < prior_end_dt
                and prior_pct is not None and cur_pct is not None
                and _is_reset_drop(prior_pct, cur_pct, allow_reset_to_zero=False)
            ):
                # Pre-check on ``new_week_end_at`` (mirrors the live
                # detection path's pre-check). Necessary because the
                # UNIQUE(old, new) constraint alone WON'T dedup against
                # legacy/broken-shape rows: pre-fix DBs may have
                # ``(cur_end, cur_end)`` rows for the same credit that
                # the new shape writes as ``(effective_iso, cur_end)``.
                # Without this pre-check, the backfill writes a second
                # row for the same credit on every open_db() call after
                # upgrade. (See round-2 review Bug 1.)
                already = conn.execute(
                    "SELECT 1 FROM week_reset_events "
                    "WHERE new_week_end_at = ? LIMIT 1",
                    (cur_end,),
                ).fetchone()
                if already is not None:
                    prior_end = cur_end
                    prior_pct = cur_pct
                    continue
                # Canonicalize to UTC before isoformat so the stored
                # offset is `+00:00`, matching the live detection path
                # (cmd_record_usage uses now_utc which is already UTC).
                # parse_iso_datetime returns .astimezone() (host-local
                # fallback at bin/cctally:_local_tz_name gate); without
                # this normalization, non-UTC hosts would store the
                # column as e.g. `+03:00`, breaking lex comparisons
                # downstream (CLAUDE.md gotcha: 5h-block cross-reset
                # comparisons go through unixepoch(), NOT lex
                # BETWEEN/</>; the reset-aware DB clamp here applies
                # the same rule). The reset-aware clamp now wraps both
                # sides with unixepoch() (Bug 2 fix), but a canonical
                # UTC offset on write is the right defense-in-depth.
                effective_iso = (
                    c._floor_to_hour(captured_dt.astimezone(dt.timezone.utc))
                    .isoformat(timespec="seconds")
                )
                # Row shape: old=effective_iso, new=cur_end (distinct
                # values). See the live-detection site in
                # bin/_cctally_record.py for the full rationale; in
                # short, old==new collapses the credited week to a
                # zero-width window in _apply_reset_events_to_weekrefs.
                conn.execute(
                    "INSERT OR IGNORE INTO week_reset_events "
                    "(detected_at_utc, old_week_end_at, new_week_end_at, "
                    " effective_reset_at_utc) VALUES (?, ?, ?, ?)",
                    (row["captured_at_utc"], effective_iso, cur_end, effective_iso),
                )
        prior_end = cur_end
        prior_pct = cur_pct
    # Flush implicit transaction so callers using explicit BEGIN
    # (e.g. _backfill_five_hour_blocks) don't trip "cannot start a
    # transaction within a transaction".
    conn.commit()


# Minimum weekly_percent drop that counts as a goodwill reset (percentage
# points). Real resets zero out usage, so the drop is large; transient API
# flaps show similar percents on both sides. 25pp catches both the known
# 86→0 and 34→0 cases while filtering 35→33-style jitter.
_RESET_PCT_DROP_THRESHOLD = 25.0

# In-place 5h-credit threshold. Mirrors `_RESET_PCT_DROP_THRESHOLD` but
# scaled down for the 5h dimension: typical 5h usage stays under ~10pp in
# a single block, so a 5pp drop sits well above natural variation while
# proportionally being a larger signal than 25pp is on the weekly scale.
# See spec docs/superpowers/specs/2026-05-16-5h-in-place-credit-detection.md
# §2.1 (Q1) for rationale.
_FIVE_HOUR_RESET_PCT_DROP_THRESHOLD = 5.0

# Reset-to-zero discriminator (2026-06-01 surprise-reset fix). Anthropic's
# weekly reset zeroes the counter mid-window, but the 25pp magnitude gate
# above silently masks it for any user below ~25% usage (e.g. the observed
# 14→0). A reset-to-zero is unambiguous REGARDLESS of magnitude: a lagging
# API replica reports a slightly-lower number, never a clean 0 against real
# usage. So the detector ALSO fires when the post value collapses to ~0
# (<= _RESET_ZERO_FLOOR_PCT) with a drop clearing a small min-drop floor.
# The floor rejects 1%→0% stale-replica jitter, which would otherwise write
# a spurious week_reset_events row and segment the week.
_RESET_ZERO_FLOOR_PCT = 1.0
_RESET_ZERO_MIN_DROP_PCT = 3.0


def _is_reset_drop(
    prior_pct: float, cur_pct: float, *, allow_reset_to_zero: bool = True
) -> bool:
    """True when ``prior_pct → cur_pct`` is a genuine weekly reset/credit.

    Two independent percent-shape signals (OR):

    * **Partial credit** — drop ``>= _RESET_PCT_DROP_THRESHOLD`` (25pp).
    * **Reset-to-zero** — ``cur_pct`` collapses to ~0
      (``<= _RESET_ZERO_FLOOR_PCT``) with a drop clearing
      ``_RESET_ZERO_MIN_DROP_PCT``. Gated on ``allow_reset_to_zero``.

    ``allow_reset_to_zero`` scopes the lenient reset-to-zero signal to the
    sites that can afford it. **Live** current-week detection passes the
    default ``True``: the live in-place path debounces a transient API zero
    (issue #128 — arm on the first ~0, confirm only if it stays low, clear
    on recovery). The **historical backfill**
    (``_backfill_week_reset_events``) passes ``False`` — it is a one-shot
    scan with NO debounce, so a single stale-replica 0% reading on a
    still-future ``week_end`` (e.g. a ``6% → 0% → 1%`` blip) would otherwise
    be mis-read as a goodwill credit and segment the week into a degenerate
    zero-width window. Backfill therefore fires only on the unambiguous
    ``>=25pp`` drop and defers sub-25pp reset-to-zero to the live path.

    Callers retain the boundary predicates (same/advanced ``week_end_at``
    AND ``prior_end_dt > now``); this helper owns ONLY the percent-shape
    discrimination.
    """
    cur = float(cur_pct)
    drop = float(prior_pct) - cur
    if drop >= _RESET_PCT_DROP_THRESHOLD:
        return True
    if not allow_reset_to_zero:
        return False
    return cur <= _RESET_ZERO_FLOOR_PCT and drop >= _RESET_ZERO_MIN_DROP_PCT


def _week_ref_has_reset_event(
    conn: sqlite3.Connection, ref: WeekRef
) -> bool:
    """Return True if `ref`'s effective boundaries were rewritten by a
    reset event (the ref went through _apply_reset_events_to_weekrefs
    and either its start or end now equals some effective_reset_at_utc).
    Lets cost callers bypass the weekly_cost_snapshots cache (which was
    computed over API-derived range) and recompute live over the
    effective range instead.
    """
    if not ref.week_start_at and not ref.week_end_at:
        return False
    row = conn.execute(
        "SELECT 1 FROM week_reset_events "
        "WHERE effective_reset_at_utc IN (?, ?) LIMIT 1",
        (ref.week_start_at, ref.week_end_at),
    ).fetchone()
    return row is not None


def _compute_cost_for_weekref(
    ref: WeekRef, *, skip_sync: bool = False
) -> float | None:
    """Live-compute USD cost over `ref`'s (possibly reset-adjusted) range
    straight from session_entries. Mirrors what cmd_sync_week writes into
    weekly_cost_snapshots, minus the cache write — used for reset-affected
    weeks where the cached range disagrees with the effective range.

    ``skip_sync`` (default ``False``) is threaded to ``_sum_cost_for_range``
    so the caller can read the cache without triggering a JSONL ingest.
    The #268 dashboard/TUI sync-thread rebuild passes ``True`` (it ingests
    once at the top of the rebuild); ``build_trend_view`` calls this once per
    reset-event week, so without the flag each reset week re-globbed the whole
    ``~/.claude/projects`` tree — the CPU peg the sync-once refactor removes.
    """
    c = _cctally()
    if not ref.week_start_at or not ref.week_end_at:
        return None
    try:
        start = parse_iso_datetime(ref.week_start_at, "weekRef.week_start_at")
        end = parse_iso_datetime(ref.week_end_at, "weekRef.week_end_at")
    except ValueError:
        return None
    if end <= start:
        return 0.0
    return c._sum_cost_for_range(start, end, mode="auto", skip_sync=skip_sync)


def _apply_overlap_clamp_to_weekrefs(refs: list[WeekRef]) -> list[WeekRef]:
    """Clamp each WeekRef's end to the next WeekRef's start on overlap.

    Caller-visible effect: report --weeks output (and its --json
    weekEndAt / weekEndDate) now reflects the true observed week end
    instead of the stale week_end_at captured from Anthropic's
    --resets-at at week-start. See _clamp_end_ats_to_next_start for
    the underlying signal. Input order is preserved (caller contract:
    get_recent_weeks returns DESC by week_start_date).

    Only refs with both week_start_at and week_end_at participate in
    clamping; date-only refs (pre-boundary-tracking rows) pass through.
    """
    c = _cctally()
    candidates = [(i, r) for i, r in enumerate(refs) if r.week_start_at and r.week_end_at]
    if len(candidates) < 2:
        return refs
    candidates.sort(key=lambda ir: ir[1].week_start_at)  # type: ignore[arg-type,return-value]
    pairs: list[tuple[str | None, str | None]] = [(r.week_start_at, r.week_end_at) for _, r in candidates]
    new_ends = c._clamp_end_ats_to_next_start(pairs)
    out = list(refs)
    for (idx, cur), new_end in zip(candidates, new_ends):
        if new_end is None or new_end == cur.week_end_at:
            continue
        new_end_dt = parse_iso_datetime(new_end, "week.end_at (clamped)")
        # internal fallback: host-local intentional
        new_end_date = (new_end_dt - dt.timedelta(seconds=1)).astimezone().date()
        out[idx] = replace(cur, week_end_at=new_end, week_end=new_end_date)
    return out
