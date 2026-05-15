"""Subscription-week boundary computation.

Self-contained subscription-week domain: the `SubWeek` frozen dataclass +
the helpers that compute, clamp, and reset-event-shift a list of weeks
from `weekly_usage_snapshots` / `weekly_cost_snapshots` / config-based
calendar-week math.

This is the first `_lib_*` module to back-reference `bin/cctally` for
shared utility helpers (`parse_iso_datetime`, `load_config`,
`get_week_start_name`, `WEEKDAY_MAP`). The back-reference uses the same
`_cctally()` call-time accessor pattern established in
`bin/_cctally_release.py` (spec §5.5) — never `import cctally` at module
top, which would pin to the *original* module instance and break
SourceFileLoader-based test isolation. Module-load time stays
self-contained; only call-time resolves through `sys.modules["cctally"]`.

Sibling dependency: `_compute_subscription_weeks` calls `_resolve_tz` /
`_local_tz_name` (from `_lib_display_tz`) in the no-snapshot
config-based fallback path; loaded via `_load_lib` at module load time
(same shape as `bin/_lib_alerts_payload.py`).

Why the planned-extract set of 4 became 6: `_apply_overlap_clamp_to_subweeks`
calls `_clamp_end_ats_to_next_start` (originally listed as private,
implicit), and `_compute_subscription_weeks` calls
`_apply_reset_events_to_subweeks` (originally elsewhere in `bin/cctally`).
Moving both keeps the subscription-week domain self-contained and avoids
inventing a call-time back-reference to `_apply_reset_events_to_subweeks`.
`_apply_overlap_clamp_to_weekrefs` (operates on `WeekRef`, NOT `SubWeek`)
stays in `bin/cctally` and reaches `_clamp_end_ats_to_next_start` through
the re-export block.

`bin/cctally` re-exports every public symbol below so the ~50 internal
call sites + SourceFileLoader-based tests (`tests/test_subweek_display_dates`,
`tests/test_dashboard_period_builders`) resolve unchanged.

Spec: docs/superpowers/specs/2026-05-13-bin-cctally-split-design.md
"""
from __future__ import annotations

import bisect
import datetime as dt
import pathlib
import sqlite3
import sys
from dataclasses import dataclass, replace


def _cctally():
    """Resolve the current `cctally` module at call-time.

    Spec §5.5 — defers the lookup so SourceFileLoader-loaded test instances
    of `bin/cctally` (which reassign `sys.modules["cctally"]`) are seen by
    this module's back-references. Mirror of `bin/_cctally_release._cctally()`.
    """
    return sys.modules["cctally"]


def _load_lib(name: str):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    import importlib.util as _ilu
    p = pathlib.Path(__file__).resolve().parent / f"{name}.py"
    spec = _ilu.spec_from_file_location(name, p)
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_lib_display_tz = _load_lib("_lib_display_tz")
_resolve_tz = _lib_display_tz._resolve_tz
_local_tz_name = _lib_display_tz._local_tz_name


@dataclass(frozen=True)
class SubWeek:
    """One subscription-week bounded interval.

    `start_ts` / `end_ts` are ISO-8601 strings with TZ offset.

    `start_date` doubles as the **internal bucket key** (matched against
    `BucketUsage.bucket`) and the **lookup key** for
    `weekly_usage_snapshots.week_start_date`. It reflects the API-derived
    boundary at snapshot capture time and is intentionally NOT shifted by
    `_apply_reset_events_to_subweeks` so the usage-% join stays joinable
    after an early reset.

    `display_start_date` is the user-facing start date and tracks `start_ts`
    after `_apply_reset_events_to_subweeks` may have rewritten the latter
    to the early-reset effective moment. For weeks never touched by a reset
    event, `display_start_date == start_date`.

    `end_date` is the inclusive last-day for display; it is NOT a lookup key
    (`_get_latest_row_for_week` joins on `week_start_date` only) and is
    already shifted in lockstep with `end_ts` by the existing clamp /
    reset-event code, so a separate display field for the end is redundant.

    `source` is either "snapshot" (boundary came from a `weekly_usage_snapshots`
    row) or "extrapolated" (inferred from the anchor via 7-day multiples).
    """
    start_ts: str         # ISO-8601, e.g. "2026-04-14T03:00:00+00:00"
    end_ts: str           # ISO-8601, start_ts + 7d
    start_date: dt.date
    end_date: dt.date     # start_date + 6d (inclusive last day for display)
    source: str           # "snapshot" | "extrapolated"
    display_start_date: dt.date


def _discover_week_anchor(conn: sqlite3.Connection) -> str | None:
    """Return one known `week_start_at` value, or None if unavailable.

    Preference order (per spec A1.6 Step 1):
      1. earliest week_start_at in weekly_usage_snapshots
      2. earliest week_start_at in weekly_cost_snapshots
      3. None  — caller falls back to config-based calendar-week math.
    """
    for table in ("weekly_usage_snapshots", "weekly_cost_snapshots"):
        row = conn.execute(
            f"SELECT week_start_at FROM {table} "
            f"WHERE week_start_at IS NOT NULL "
            f"ORDER BY week_start_at ASC LIMIT 1"
        ).fetchone()
        if row is not None and row[0]:
            return row[0]
    return None


def _clamp_end_ats_to_next_start(
    pairs: list[tuple[str | None, str | None]],
) -> list[str | None]:
    """For each (start_at, end_at) pair, return a clamped end_at.

    When the next pair's start_at falls strictly inside the current pair's
    (start_at, end_at) interval, the current end_at is replaced by that
    next start_at. This corrects weekly_usage_snapshots.week_end_at, which
    is captured once from Anthropic's --resets-at at week-start and never
    updated when a later early reset actually ends the week sooner. The
    overlap of the next week's start_at inside the current week's
    interval is the observable ground-truth signal of an early reset.

    `pairs` must be sorted by start_at ascending. None values are
    passed through unchanged and never participate in clamping.
    """
    parse_iso_datetime = _cctally().parse_iso_datetime
    n = len(pairs)
    if n < 2:
        return [p[1] for p in pairs]
    out: list[str | None] = []
    for i, (cur_start, cur_end) in enumerate(pairs):
        if cur_end is None or i + 1 >= n:
            out.append(cur_end)
            continue
        nxt_start = pairs[i + 1][0]
        if nxt_start is None:
            out.append(cur_end)
            continue
        cur_end_dt = parse_iso_datetime(cur_end, "week.end_at")
        nxt_start_dt = parse_iso_datetime(nxt_start, "week.start_at")
        if cur_start is not None:
            cur_start_dt = parse_iso_datetime(cur_start, "week.start_at")
            if not (cur_start_dt < nxt_start_dt < cur_end_dt):
                out.append(cur_end)
                continue
        elif nxt_start_dt >= cur_end_dt:
            out.append(cur_end)
            continue
        out.append(nxt_start)
    return out


def _apply_overlap_clamp_to_subweeks(weeks: list[SubWeek]) -> list[SubWeek]:
    """Clamp each SubWeek's end_ts to the next SubWeek's start_ts on overlap.

    The early-reset fix: weekly_usage_snapshots.week_end_at stays stale
    across Anthropic early resets, so _compute_subscription_weeks() emits
    SubWeeks whose end_ts may sit past the real end. The next week's
    start_ts (itself from a fresh snapshot) reveals the true boundary.
    Clamping here corrects both display (--json weekEndAt) and the
    _aggregate_weekly bucketing interval [start_ts, end_ts).

    Input must be sorted by start_ts ascending (invariant of
    _compute_subscription_weeks in all three branches).
    """
    parse_iso_datetime = _cctally().parse_iso_datetime
    if len(weeks) < 2:
        return weeks
    pairs: list[tuple[str | None, str | None]] = [(w.start_ts, w.end_ts) for w in weeks]
    new_ends = _clamp_end_ats_to_next_start(pairs)
    result: list[SubWeek] = []
    for w, new_end in zip(weeks, new_ends):
        if new_end is None or new_end == w.end_ts:
            result.append(w)
            continue
        new_end_dt = parse_iso_datetime(new_end, "week.end_ts (clamped)")
        # internal fallback: host-local intentional
        new_end_date = (new_end_dt - dt.timedelta(seconds=1)).astimezone().date()
        result.append(replace(w, end_ts=new_end, end_date=new_end_date))
    return result


def _apply_reset_events_to_subweeks(
    conn: sqlite3.Connection, weeks: list[SubWeek]
) -> list[SubWeek]:
    """Override SubWeek boundaries with reset-event effective moments.

    Same semantics as `_apply_reset_events_to_weekrefs` but for SubWeek:
      - SubWeek whose end_ts equals event.old_week_end_at (instant)
        is the PRE-reset week → end_ts := effective_reset_at_utc
        and end_date := (reset_dt - 1s).astimezone().date()
      - SubWeek whose end_ts equals event.new_week_end_at (instant)
        is the POST-reset week → start_ts := effective_reset_at_utc
        (start_date kept; it is the bucket key + lookup key for
        weekly_usage_snapshots.week_start_date).

    Compares by parsed datetime instant — SubWeek.{start,end}_ts are
    raw snapshot strings that may be written in non-UTC offsets while
    `week_reset_events.{old,new}_week_end_at` are canonicalized UTC.
    """
    parse_iso_datetime = _cctally().parse_iso_datetime
    rows = conn.execute(
        "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
        "FROM week_reset_events"
    ).fetchall()
    if not rows:
        return weeks
    parsed_events: list[tuple[dt.datetime, dt.datetime, str]] = []
    for r in rows:
        try:
            old_dt = parse_iso_datetime(r["old_week_end_at"], "evt.old_end")
            new_dt = parse_iso_datetime(r["new_week_end_at"], "evt.new_end")
        except ValueError:
            continue
        parsed_events.append((old_dt, new_dt, r["effective_reset_at_utc"]))
    if not parsed_events:
        return weeks

    out: list[SubWeek] = []
    for w in weeks:
        new_w = w
        try:
            end_dt = parse_iso_datetime(w.end_ts, "subweek.end_ts")
        except ValueError:
            out.append(w)
            continue
        for old_dt, new_dt, reset_at in parsed_events:
            if end_dt == old_dt:
                try:
                    reset_dt = parse_iso_datetime(reset_at, "evt.eff")
                except ValueError:
                    continue
                # internal fallback: host-local intentional
                new_end_date = (reset_dt - dt.timedelta(seconds=1)).astimezone().date()
                new_w = replace(new_w, end_ts=reset_at, end_date=new_end_date)
            if end_dt == new_dt:
                try:
                    reset_dt = parse_iso_datetime(reset_at, "evt.eff")
                except ValueError:
                    continue
                # internal fallback: host-local intentional
                new_display_start = reset_dt.astimezone().date()
                new_w = replace(
                    new_w,
                    start_ts=reset_at,
                    display_start_date=new_display_start,
                )
                # start_date intentionally NOT touched — it is the bucket /
                # lookup key into weekly_usage_snapshots.week_start_date.
        out.append(new_w)
    return out


def _compute_subscription_weeks(
    conn: sqlite3.Connection,
    range_start: dt.datetime,
    range_end: dt.datetime,
) -> list[SubWeek]:
    """Generate the ordered list of subscription weeks overlapping [range_start, range_end].

    Prefers snapshot rows (authoritative reset boundaries from actual data)
    and extrapolates by 7-day multiples only for the range tail before the
    earliest snapshot. When no snapshots exist at all, falls back to
    config-based calendar-week boundaries with every week tagged
    "extrapolated".

    Anthropic's reset day-of-week is not strictly stable across long spans —
    it can shift (observed: Thursday cycles in Feb, Friday cycles from Mar
    onward). A single-anchor 7-day-multiple extrapolation therefore generates
    dates that miss actual snapshot boundaries for middle weeks. Using
    snapshot rows directly for weeks they cover avoids that drift.
    """
    cctally = _cctally()
    parse_iso_datetime = cctally.parse_iso_datetime

    # Case A: snapshots exist.
    snap_rows = conn.execute(
        "SELECT "
        "    MIN(week_start_at) AS week_start_at, "
        "    MIN(week_end_at)   AS week_end_at, "
        "    week_start_date, "
        "    MIN(week_end_date) AS week_end_date "
        "FROM weekly_usage_snapshots "
        "WHERE week_start_at IS NOT NULL "
        "  AND week_end_at   IS NOT NULL "
        "  AND week_start_date IS NOT NULL "
        "GROUP BY week_start_date "
        "ORDER BY MIN(week_start_at) ASC"
    ).fetchall()

    weeks: list[SubWeek] = []

    if snap_rows:
        parsed_snaps: list[tuple[dt.datetime, dt.datetime, str, str, str, str | None]] = []
        for row in snap_rows:
            start_ts, end_ts, start_date_s, end_date_s = row
            start_dt = parse_iso_datetime(start_ts, "week_start_at")
            end_dt = parse_iso_datetime(end_ts, "week_end_at")
            parsed_snaps.append((start_dt, end_dt, start_ts, end_ts, start_date_s, end_date_s))

        snap_start_dts = [r[0] for r in parsed_snaps]

        # Pick initial anchor: first snapshot >= range_start; else last snapshot
        # < range_start; else the earliest snapshot (only happens when all
        # snapshots are before range_start — we'll step forward from it).
        idx_ge = bisect.bisect_left(snap_start_dts, range_start)
        if idx_ge < len(parsed_snaps):
            anchor_dt = parsed_snaps[idx_ge][0]
        elif parsed_snaps:
            anchor_dt = parsed_snaps[-1][0]
        else:  # unreachable given `if snap_rows:` guard, defensive
            anchor_dt = range_start

        # Slide anchor back to land at-or-before range_start.
        current = anchor_dt
        while current > range_start:
            current -= dt.timedelta(days=7)
        # If anchor was already far before range_start, step forward until the
        # slice [current, current+7d) overlaps range_start.
        while current + dt.timedelta(days=7) <= range_start:
            current += dt.timedelta(days=7)

        # Walk forward. For each 7-day slice overlapping the range, emit a
        # SubWeek. When a slice's local start_date matches a snapshot row,
        # use that row's verbatim bounds (drives snapshot-based Used % join).
        # After emitting, re-anchor to the next snapshot whenever it sits
        # within MAX_REANCHOR of `current`. This handles three cases:
        #   - normal 7d cadence (~7d ahead): matches exactly
        #   - day-of-week drift (Thursday → Friday cycles, ~7±1d ahead)
        #   - early reset (snapshot ~1d after current when Anthropic ends the
        #     week before the original --resets-at); the previous heuristic
        #     (|cand - natural_next| <= HALF_WEEK) rejected early-reset
        #     snapshots because they sit ~6d from the +7d natural step.
        # Snapshots farther than MAX_REANCHOR represent a multi-week data
        # gap; extrapolate one week and retry on the next iteration.
        MAX_REANCHOR = dt.timedelta(days=10, hours=12)
        while current < range_end:
            end = current + dt.timedelta(days=7)
            if end > range_start and current < range_end:
                # internal fallback: host-local intentional
                local_start = current.astimezone().date()
                # Match snapshots by datetime equality against the sorted
                # snap_start_dts list — keying on local_start_s (current's
                # date in the *reader's* local TZ) was TZ-unsafe: snapshots
                # written in another TZ whose UTC hour sits near midnight
                # would flip to a different local date on a machine in a
                # different TZ (travel / WSL vs. host), missing the lookup
                # and relabeling the week as "extrapolated" (dropping
                # Used % / $/1%). Datetime equality is TZ-invariant.
                idx = bisect.bisect_left(snap_start_dts, current)
                if idx < len(snap_start_dts) and snap_start_dts[idx] == current:
                    rec = parsed_snaps[idx]
                else:
                    rec = None
                if rec is not None:
                    s_dt, e_dt, s_ts, e_ts, s_date_s, e_date_s = rec
                    start_date_obj = dt.date.fromisoformat(s_date_s)
                    if e_date_s:
                        end_date_obj = dt.date.fromisoformat(e_date_s)
                    else:
                        end_date_obj = start_date_obj + dt.timedelta(days=6)
                    weeks.append(SubWeek(
                        start_ts=s_ts,
                        end_ts=e_ts,
                        start_date=start_date_obj,
                        end_date=end_date_obj,
                        source="snapshot",
                        display_start_date=start_date_obj,
                    ))
                else:
                    local_end = local_start + dt.timedelta(days=6)
                    weeks.append(SubWeek(
                        start_ts=current.isoformat(timespec="seconds"),
                        end_ts=end.isoformat(timespec="seconds"),
                        start_date=local_start,
                        end_date=local_end,
                        source="extrapolated",
                        display_start_date=local_start,
                    ))

            # Determine next `current`: prefer the next snapshot's start_dt
            # when it sits within MAX_REANCHOR of `current` (covers normal
            # cadence, drift, and early-reset weeks). Otherwise step +7d
            # to emit one extrapolated week inside a multi-week data gap.
            natural_next = end
            snap_idx = bisect.bisect_right(snap_start_dts, current)
            re_anchored = False
            while snap_idx < len(snap_start_dts):
                cand = snap_start_dts[snap_idx]
                if cand <= current:  # strictly ahead only
                    snap_idx += 1
                    continue
                if (cand - current) <= MAX_REANCHOR:
                    current = cand
                    re_anchored = True
                    break
                # Next snapshot is far ahead — keep natural step.
                break
            if not re_anchored:
                current = natural_next

        return _apply_overlap_clamp_to_subweeks(
            _apply_reset_events_to_subweeks(conn, weeks)
        )

    # Case A2 (spec A1.6 Step 1 fallback): no usage snapshots, but a
    # cost-snapshot may carry a known reset boundary. Use that anchor to
    # extrapolate 7-day cycles in both directions across the range
    # before falling through to calendar-week math.
    # NOTE: weekly_cost_snapshots contributes timing only — cost is
    # always recomputed from session_entries (see CLAUDE.md gotcha
    # "weekly ignores weekly_cost_snapshots for cost").
    anchor_ts = _discover_week_anchor(conn)
    if anchor_ts is not None:
        anchor_dt = parse_iso_datetime(anchor_ts, "week_start_at (anchor)")
        # Slide anchor back by full weeks until we're at-or-before range_start.
        current = anchor_dt
        while current > range_start:
            current -= dt.timedelta(days=7)
        # If anchor was already far past range_end, current may still be
        # past range_end; outer while loop handles that naturally (zero
        # iterations). Conversely if anchor is before range_start we need
        # to step forward to the first week overlapping the range.
        while current + dt.timedelta(days=7) <= range_start:
            current += dt.timedelta(days=7)
        # Emit one SubWeek per 7-day slice until we pass range_end.
        while current < range_end:
            end = current + dt.timedelta(days=7)
            # internal fallback: host-local intentional
            local_start = current.astimezone().date()
            local_end = local_start + dt.timedelta(days=6)
            weeks.append(SubWeek(
                start_ts=current.isoformat(timespec="seconds"),
                end_ts=end.isoformat(timespec="seconds"),
                start_date=local_start,
                end_date=local_end,
                source="extrapolated",
                display_start_date=local_start,
            ))
            current = end
        return _apply_overlap_clamp_to_subweeks(weeks)

    # Case B: no snapshots — config-based calendar-week fallback.
    config = cctally.load_config()
    week_start_name = cctally.get_week_start_name(config)
    week_start_idx = cctally.WEEKDAY_MAP[week_start_name]
    # internal fallback: host-local intentional
    local_start_date = range_start.astimezone().date()
    diff = (local_start_date.weekday() - week_start_idx) % 7
    current_date = local_start_date - dt.timedelta(days=diff)
    # Use the IANA ZoneInfo so `datetime.combine(date, time, tzinfo=tz)`
    # produces the correct historical offset per-date (handles DST
    # transitions across a long range). Fall back to the fixed-offset
    # snapshot on exotic platforms where IANA resolution fails.
    # internal fallback: host-local intentional (datetime.now().astimezone().tzinfo)
    tz = _resolve_tz(_local_tz_name()) or dt.datetime.now().astimezone().tzinfo
    while True:
        end_date = current_date + dt.timedelta(days=7)
        start_dt = dt.datetime.combine(current_date, dt.time(0, 0), tzinfo=tz)
        end_dt = dt.datetime.combine(end_date, dt.time(0, 0), tzinfo=tz)
        if start_dt >= range_end:
            break
        if end_dt > range_start:
            weeks.append(SubWeek(
                start_ts=start_dt.isoformat(timespec="seconds"),
                end_ts=end_dt.isoformat(timespec="seconds"),
                start_date=current_date,
                end_date=end_date - dt.timedelta(days=1),
                source="extrapolated",
                display_start_date=current_date,
            ))
        current_date = end_date
    return _apply_overlap_clamp_to_subweeks(weeks)
