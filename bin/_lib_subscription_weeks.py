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
lives in `bin/_cctally_weekrefs.py` and reaches `_clamp_end_ats_to_next_start`
through the cctally namespace (the re-export block + its call-time `c.` accessor).

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


# === Honest imports from extracted homes ===================================
# Spec 2026-05-17-cctally-core-kernel-extraction.md §3.3: kernel symbols
# import from _cctally_core. `load_config` stays on the _cctally()
# accessor per spec §3.5 monkeypatch carve-out (tests reach it via
# ``ns["load_config"]``).
from _cctally_core import (
    parse_iso_datetime,
    get_week_start_name,
    _ordered_in_place_cuts,
    WEEKDAY_MAP,
)


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

    `segment_key` (see the property below) is the **bucket key**, and is NOT
    the same thing as `start_date`. An in-place weekly credit splits one week
    into two billing cycles that share `start_date`, so `start_date` stopped
    being unique across the list the moment
    `_apply_reset_events_to_subweeks` learned to synthesize the pre-credit
    segment.
    """
    start_ts: str         # ISO-8601, e.g. "2026-04-14T03:00:00+00:00"
    end_ts: str           # ISO-8601, start_ts + 7d
    start_date: dt.date
    end_date: dt.date     # start_date + 6d (inclusive last day for display)
    source: str           # "snapshot" | "extrapolated"
    display_start_date: dt.date

    @property
    def segment_key(self) -> str:
        """This segment's identity: the UTC-canonicalized `start_ts` instant.

        `_aggregate_weekly` keys buckets on this and `_aggregate_buckets`
        returns `sorted(by_bucket.keys())`, which every consumer reads as
        chronological ascending. Canonicalizing to UTC is what makes that
        true: `start_ts` is a raw snapshot string that may be written in a
        non-UTC offset, so sorting the raw strings would order two segments
        by their written offset rather than by time.

        Derived rather than stored so `dataclasses.replace(w, start_ts=...)`
        — which both appliers use — can never leave a stale key behind.
        """
        return parse_iso_datetime(
            self.start_ts, "subweek.start_ts"
        ).astimezone(dt.timezone.utc).isoformat()


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
    conn: sqlite3.Connection, weeks: list[SubWeek], *,
    account_key: "str | None" = None,
) -> list[SubWeek]:
    """Override SubWeek boundaries with reset-event effective moments.

    ``account_key`` (#750 S3 B1) scopes the event read to one account.
    ``None`` is the explicit merged read and is byte-identical to the
    account-blind behaviour this applier had before, which is what keeps a
    single-account install unchanged. A real key matters as soon as two
    accounts hold credits in weeks that share an end instant: the events are
    matched on that instant alone, so an unscoped read splits one account's
    week at another account's cut.

    Same semantics as `_apply_reset_events_to_weekrefs` but for SubWeek:
      - SubWeek whose end_ts equals event.old_week_end_at (instant)
        is the PRE-reset week → end_ts := effective_reset_at_utc
        and end_date := (reset_dt - 1s).astimezone().date()
      - SubWeek whose end_ts equals event.new_week_end_at (instant)
        is the POST-reset week → start_ts := effective_reset_at_utc
        (start_date kept; it is the lookup key for
        weekly_usage_snapshots.week_start_date).
      - **In-place credit**, detected by the row shape
        ``old_week_end_at == effective_reset_at_utc`` (the shape both the
        live and the backfill detection paths write). An Anthropic reset
        never moves a week's boundaries, but it does end one billing cycle
        and begin another inside that week, so a week credited N times IS
        N+1 billing cycles and must come back as N+1 SubWeeks. Without the
        synthesized siblings, every entry before the last cut falls into a
        gap that ``_aggregate_weekly`` drops — the week's spend disappears
        from the table AND from the totals.

    The N-ary form (#750 S3 B2): gather every in-place cut strictly inside
    the week's original interval, deduplicate on the parsed instant, sort
    ascending as ``c1 < … < cn``, and emit ``[start, c1)``, each
    ``[ci, ci+1)`` and ``[cn, end)``. A single slot for one cut used to hold
    the pre-credit segment, so a second credit in one week silently
    overwrote the first and the interval between them was lost.

    Every segment keeps the same ``start_date`` (the snapshot join key the
    credit does not move), so they are told apart by ``SubWeek.segment_key``
    — the UTC-canonicalized ``start_ts``, which yields n+1 distinct keys for
    n+1 segments with no ordinal and no new field. `_apply_overlap_clamp_to_
    subweeks`, `_aggregate_weekly`'s bisect and `cmd_weekly`'s ``weeks[0]``
    all require ascending order, so the synthesized segments are emitted in
    their sorted positions rather than appended.

    Mirrors `_apply_reset_events_to_weekrefs`, which grew the in-place-credit
    case in v1.7.2 while this twin did not. The two must stay in step;
    `test_subweek_and_weekref_appliers_agree_on_every_event_shape` is the tie,
    and it covers all three event shapes rather than only the credit one.

    Compares by parsed datetime instant — SubWeek.{start,end}_ts are
    raw snapshot strings that may be written in non-UTC offsets while
    `week_reset_events.{old,new}_week_end_at` are canonicalized UTC.
    """
    acct_pred = "" if account_key is None else " WHERE account_key = ?"
    acct_p: tuple = () if account_key is None else (account_key,)
    # ORDERED THE WAY `_latest_reset_event_for_end` ORDERS (#750 S3, Unit B
    # review), and the weekrefs twin carries the identical clause. Two rows can
    # share one `(old_week_end_at, new_week_end_at)` pair and carry different
    # effective instants — epoch 1013 admits that whenever the rows carry
    # distinct origins — and the two single-valued roles below take the FIRST
    # matching row. Unordered, "first" meant "whichever row was written last",
    # which is not what the chokepoint answers, so two consumers disagreed
    # about where a week starts. `unixepoch(...)`, never a lexical compare:
    # the column carries mixed offset spellings.
    rows = conn.execute(
        "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
        "FROM week_reset_events" + acct_pred
        + " ORDER BY unixepoch(effective_reset_at_utc) DESC, id DESC",
        acct_p,
    ).fetchall()
    if not rows:
        return weeks
    parsed_events: list[
        tuple[dt.datetime, dt.datetime, dt.datetime, str, bool]] = []
    for r in rows:
        # The effective instant is parsed ONCE, here, rather than at each of
        # the three use sites. A row whose effective instant is unparseable is
        # dropped, which is what the per-site `continue` already amounted to.
        try:
            old_dt = parse_iso_datetime(r["old_week_end_at"], "evt.old_end")
            new_dt = parse_iso_datetime(r["new_week_end_at"], "evt.new_end")
            eff_dt = parse_iso_datetime(
                r["effective_reset_at_utc"], "evt.eff")
        except ValueError:
            continue
        parsed_events.append((
            old_dt, new_dt, eff_dt, r["effective_reset_at_utc"],
            r["old_week_end_at"] == r["effective_reset_at_utc"],
        ))
    if not parsed_events:
        return weeks

    out: list[SubWeek] = []
    synthesized = False
    for w in weeks:
        try:
            end_dt = parse_iso_datetime(w.end_ts, "subweek.end_ts")
        except ValueError:
            out.append(w)
            continue
        # The three roles this week's events can play, resolved in ONE pass so
        # the two single-valued ones can take the FIRST matching row (the rows
        # arrive latest-effective-first, so that is the row the chokepoint
        # would return) while the in-place cuts still collect every row.
        end_shift: tuple[dt.datetime, str] | None = None
        start_shift: tuple[dt.datetime, str] | None = None
        # Every in-place cut this week carries, as (instant, raw text). The
        # raw text is what lands in `start_ts` / `end_ts`, so the pair keeps
        # the stored spelling while the instant does the ordering.
        cuts: list[tuple[dt.datetime, str]] = []
        for old_dt, new_dt, eff_dt, reset_at, is_in_place_credit in (
                parsed_events):
            if end_dt == old_dt and end_shift is None:
                end_shift = (eff_dt, reset_at)
            if end_dt == new_dt:
                if is_in_place_credit:
                    cuts.append((eff_dt, reset_at))
                elif start_shift is None:
                    start_shift = (eff_dt, reset_at)
        # The week's EFFECTIVE start. A boundary shift moves it, and a cut is
        # only inside this week when it falls after that shift, so the shift is
        # both the lower bound on the cuts and the head segment's start.
        # Deriving the head from the API-derived start instead discards the
        # shift, overlaps the previous week, and lets
        # `_apply_overlap_clamp_to_subweeks` move that week's spend into this
        # one (#750 S3, Unit B review).
        eff_start_raw = w.start_ts if start_shift is None else start_shift[1]
        # N cuts make N+1 billing cycles: `[start, c1)`, each `[ci, ci+1)` and
        # `[cn, end)`. The shared helper deduplicates on the INSTANT rather
        # than on the stored text, because two rows can spell one instant in
        # different offsets, and sorts ascending so the emitted list stays
        # ordered whatever order the rows arrived in.
        ordered = _ordered_in_place_cuts(cuts, eff_start_raw, end_dt)

        new_w = w
        if end_shift is not None:
            shift_dt, shift_raw = end_shift
            # internal fallback: host-local intentional
            new_end_date = (
                shift_dt - dt.timedelta(seconds=1)).astimezone().date()
            new_w = replace(new_w, end_ts=shift_raw, end_date=new_end_date)
        if start_shift is not None:
            new_w = replace(
                new_w,
                start_ts=start_shift[1],
                # internal fallback: host-local intentional
                display_start_date=start_shift[0].astimezone().date(),
            )
            # start_date intentionally NOT touched — it is the lookup
            # key into weekly_usage_snapshots.week_start_date, shared by
            # both segments. `segment_key` tells them apart.
        if ordered:
            # Every segment but the last is derived from `new_w` BEFORE its
            # start moves to the last cut: two in-place credits in one week
            # both carry that week's unchanged `new_week_end_at`, so deriving a
            # later segment from an already-split week would drop everything
            # before the first credit. `new_w`, not `w`, so the head keeps the
            # boundary shift; its own end shift is overwritten below by the
            # cut, which is what closes every segment but the last.
            base = new_w
            for index, (cut_dt, cut_raw) in enumerate(ordered):
                # internal fallback: host-local intentional
                seg_end_date = (
                    cut_dt - dt.timedelta(seconds=1)
                ).astimezone().date()
                if index == 0:
                    out.append(replace(
                        base, end_ts=cut_raw, end_date=seg_end_date))
                else:
                    prev_dt, prev_raw = ordered[index - 1]
                    out.append(replace(
                        base,
                        start_ts=prev_raw,
                        # internal fallback: host-local intentional
                        display_start_date=prev_dt.astimezone().date(),
                        end_ts=cut_raw,
                        end_date=seg_end_date,
                    ))
            last_dt, last_raw = ordered[-1]
            new_w = replace(
                new_w,
                start_ts=last_raw,
                # internal fallback: host-local intentional
                display_start_date=last_dt.astimezone().date(),
            )
            # Emitting the head and the middles immediately before the tail
            # keeps `out` ascending, which `_apply_overlap_clamp_to_subweeks`,
            # `_aggregate_weekly`'s bisect and `cmd_weekly`'s `weeks[0]` all
            # require. The defensive re-sort below covers the residual case
            # where the input itself was not ascending.
            synthesized = True
        out.append(new_w)
    if synthesized:
        try:
            out = sorted(out, key=lambda s: parse_iso_datetime(
                s.start_ts, "subweek.start_ts"))
        except ValueError:
            pass  # a malformed start_ts: keep insertion order rather than raise
    return out


def subscription_window_probe_range(
    anchor_utc: "dt.datetime", weeks_back: int,
) -> "tuple[dt.datetime, dt.datetime]":
    """The generous ``[range_start, range_end)`` to compute subscription
    weeks over when the caller wants ``weeks_back`` intervals ending at the
    one containing ``anchor_utc``.

    ONE definition, shared by the dashboard Projects panel and by
    ``cctally project``. The range matters beyond covering the answer:
    ``_compute_subscription_weeks`` picks its extrapolation anchor relative
    to ``range_start``, so two callers asking the same question over
    different ranges can receive differently-phased intervals for the same
    pre-snapshot history.

    The lower bound allows one extra week because the anchor's own interval
    can start up to a week before an ISO-Monday snap of it; the upper bound
    allows two so the containing interval is always emitted whole.
    """
    base = anchor_utc.astimezone(dt.timezone.utc)
    monday = (base - dt.timedelta(days=base.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (monday - dt.timedelta(days=7 * (weeks_back + 1)),
            monday + dt.timedelta(days=14))


def subscription_window_ending_at(
    bounds: "list[tuple[dt.datetime, dt.datetime]]",
    cw_start: "dt.datetime",
    weeks_back: int,
) -> "list[tuple[dt.datetime, dt.datetime]]":
    """The last ``weeks_back`` intervals up to and including the one starting
    at ``cw_start``, oldest first.

    ONE definition, shared by the dashboard Projects panel
    (``_ProjectsWeekGrid.window_ending_at``) and by ``cctally project``, so
    the two surfaces cannot resolve different windows for the same request.
    ``cmd_project`` used to step back in seven-day multiples instead, which
    is short of the truth by the accumulated shortfall of every drifted week
    in the window — one day per six-day week — and the leading extrapolated
    slice covering that deficit then carried real cost, moved the
    ``usedPercent`` denominator by a whole extra week, and drove a rendered
    note claiming a week had no usage snapshot.

    ``bounds`` must be sorted by start. When fewer than ``weeks_back``
    intervals sit at or before ``cw_start``, the head is padded backwards in
    seven-day steps: that padding is a genuine no-anchor tail — history older
    than any snapshot — so the seven-day assumption is the right one there.

    Returns ``[]`` when ``cw_start`` is not itself an interval start in
    ``bounds``; a caller that cannot locate its own anchor must not silently
    receive a window anchored somewhere else.
    """
    starts = [b[0] for b in bounds]
    idx = bisect.bisect_right(starts, cw_start) - 1
    if idx < 0 or starts[idx] != cw_start:
        return []
    lo = max(0, idx + 1 - weeks_back)
    window = [(bounds[i][0], bounds[i][1]) for i in range(lo, idx + 1)]
    while len(window) < weeks_back:
        s = window[0][0] - dt.timedelta(days=7)
        window.insert(0, (s, window[0][0]))
    return window


def _compute_subscription_weeks(
    conn: sqlite3.Connection,
    range_start: dt.datetime,
    range_end: dt.datetime,
    config: "dict | None" = None,
    *,
    account_key: "str | None",
) -> list[SubWeek]:
    """Generate the ordered list of subscription weeks overlapping [range_start, range_end].

    ``account_key`` (#341, review finding 11): MANDATORY account context — no
    silent global fallback. A real key scopes the snapshot-derived reset anchors
    to that account so two accounts with different reset cadences never
    re-anchor each other's week walk; ``None`` is the explicit "all accounts"
    (merged) read used by the analytics callers, byte-identical to today on a
    single-account install.

    Prefers snapshot rows (authoritative reset boundaries from actual data)
    and extrapolates by 7-day multiples only for the range tail before the
    earliest snapshot. When no snapshots exist at all, falls back to
    config-based calendar-week boundaries with every week tagged
    "extrapolated".

    ``config`` (issue #88 ``--config`` surface): the resolved config dict
    used by the no-snapshot Case-B calendar-week fallback. When the caller
    already loaded config honoring the per-invocation ``--config <path>``
    override (``_load_claude_config_for_args``), it MUST pass it here so the
    fallback's ``collector.week_start`` matches the explicit override rather
    than re-reading (and first-run-creating) the persisted default config.
    ``None`` preserves the legacy bare-``load_config()`` behavior for callers
    with no ``--config`` surface (dashboard) and for the monkeypatch
    carve-out (tests reach ``load_config`` via ``ns["load_config"]``).

    Anthropic's reset day-of-week is not strictly stable across long spans —
    it can shift (observed: Thursday cycles in Feb, Friday cycles from Mar
    onward). A single-anchor 7-day-multiple extrapolation therefore generates
    dates that miss actual snapshot boundaries for middle weeks. Using
    snapshot rows directly for weeks they cover avoids that drift.
    """
    # Case A: snapshots exist. Account scoping (#341): a real key filters the
    # reset anchors to that account; None is the merged (all-accounts) read.
    acct_pred = "" if account_key is None else " AND account_key = ?"
    acct_params: tuple = () if account_key is None else (account_key,)
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
        f"  {acct_pred} "
        "GROUP BY week_start_date "
        "ORDER BY MIN(week_start_at) ASC",
        acct_params,
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
            _apply_reset_events_to_subweeks(
                conn, weeks, account_key=account_key)
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
    # Honor the caller's `--config <path>` override when supplied (issue
    # #88): `cmd_weekly` / `cmd_project` pass the config resolved by
    # `_load_claude_config_for_args` so this fallback reads the explicit
    # path's `collector.week_start` instead of recreating / reading the
    # persisted default. When `config is None` (dashboard, or the spec §3.5
    # monkeypatch carve-out where tests reach `load_config` via
    # `ns["load_config"]`), fall back to a bare `load_config()` on the
    # `_cctally()` accessor — identical to the prior behavior.
    if config is None:
        config = _cctally().load_config()
    week_start_name = get_week_start_name(config)
    week_start_idx = WEEKDAY_MAP[week_start_name]
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
