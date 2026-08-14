import { fmt } from './fmt';
import type { FmtCtx } from './fmt';
import type { Envelope } from '../types/envelope';

// #556 S2 — the resolved spans two Projects surfaces have to state.
//
// Two different windows meet in the All Projects experience and neither
// contains the other, which is why both now name resolved dates instead of a
// bare unit count:
//
//   - The RANKING is the shared absolute range, thirty calendar days ending
//     today, published once at `sources.all.data.aggregates.range`.
//   - The DRILL is `weeks_back` whole weeks anchored at the CURRENT Monday.
//     `_project_detail_for_window` walks `[cw_start - 7 * (weeks_back - 1),
//     cw_start + 7d]`, and the server widens `weeks_back` for an
//     All-originated row so the drill reaches the ranking's own start.
//
// So a row ranked over thirty days opens a detail reporting fifty-six, and
// `window_cost_usd >= ` the ranked figure with nothing reconciling them. Naming
// each span is what keeps the two numbers from implying each other.

export interface ResolvedSpan {
  startAt: string;
  endAt: string;
}

const DAY_MS = 86_400_000;

/**
 * The drill window a `window_weeks` detail actually reported.
 *
 * Mirrors `_project_detail_for_window`'s bounds
 * (`bin/_cctally_dashboard.py`), which is the only place the arithmetic
 * lives on the server: `since = cw_start - 7 * (weeks - 1) days` and
 * `until = cw_start + 7 days`.
 *
 * `endAt` is `until - 1ms`, the LAST INSTANT the window covers, because
 * `until` is the following Monday's midnight and naming it would read as a
 * day the drill reports on. Every other bound here is passed through
 * unchanged.
 *
 * It is deliberately NOT `anchor + 6 days`, which is the last day's MIDNIGHT.
 * `formatSpan` renders each bound as the calendar day it falls on inside the
 * display timezone, and a midnight instant falls on the PREVIOUS local day in
 * every zone behind UTC — so that form named a day one short of what the drill
 * actually reported for every user west of Greenwich.
 *
 * Returns `null` rather than guessing when the anchor is missing or
 * unparseable, or when `weeks` is not a positive integer — a span nothing
 * established is exactly what this helper exists to stop the modal from
 * stating.
 */
export function claudeDrillWindow(
  weekStartAt: string | null | undefined,
  weeks: number | null | undefined,
): ResolvedSpan | null {
  if (weekStartAt == null || typeof weeks !== 'number') return null;
  if (!Number.isInteger(weeks) || weeks < 1) return null;
  const anchor = Date.parse(weekStartAt);
  if (Number.isNaN(anchor)) return null;
  const since = anchor - 7 * (weeks - 1) * DAY_MS;
  const lastInstant = anchor + 7 * DAY_MS - 1;
  return {
    startAt: new Date(since).toISOString(),
    endAt: new Date(lastInstant).toISOString(),
  };
}

/**
 * The Monday anchor the drill window is measured from.
 *
 * The server reads `env["current_week"]["week_start_at"]` off the projects
 * envelope, which reaches the wire in two places carrying the same value: the
 * legacy top-level envelope and the Claude source domain.
 *
 * THE TOP-LEVEL BLOCK WINS, and the order matters. `env.projects` is
 * `snap.projects_envelope` — the very object the drill-down route resolves
 * against, rebuilt on every tick. The source-domain copy lives inside a
 * provider state that is deliberately RETAINED across ticks, so at a week
 * rollover with a retained bundle it still carries the previous Monday, and
 * preferring it made the drill state a span one whole week behind what the
 * server reported. The earlier order was justified on the grounds that the
 * source copy is present under every selection; that is not a reason to
 * prefer it, because the top-level block is present in every payload too.
 */
export function claudeCurrentWeekStartAt(
  env: Envelope | null | undefined,
): string | null {
  const anchor = env?.projects?.current_week?.week_start_at
    ?? env?.sources?.claude?.data?.projects?.current_week?.week_start_at
    ?? env?.sources?.all?.data?.providers?.claude?.projects?.current_week
      ?.week_start_at;
  return typeof anchor === 'string' && anchor !== '' ? anchor : null;
}

export interface FormatSpanOptions {
  /**
   * The instant the displayed end is clamped back to — always the snapshot's
   * own `generated_at`, never the client clock. Pass `null`/`undefined` (or an
   * unparseable value) to state the window's own end unclamped.
   */
  clampEndTo?: string | number | null;
}

/**
 * "Jul 20 – Aug 16", or "Aug 16" when both bounds land on the same displayed
 * day. Returns `null` when either bound cannot be rendered, so a caller can
 * fall back rather than print a half-span.
 *
 * Both bounds go through the `lib/fmt.ts` chokepoint, so the dates are the
 * DISPLAY timezone's calendar days.
 *
 * TWO RULES BEYOND PLAIN FORMATTING, and every span-stating surface gets both
 * because they all call this one function.
 *
 * 1. A YEAR-CROSSING span names both years. The year-free form is right for
 *    almost every span the dashboard states and is noise there, but two bounds
 *    twelve months apart render as the same string in it — the Codex project
 *    drill's 365-day window printed "Aug 14 → Aug 14", which reads as a
 *    zero-width range rather than as a year of history.
 *
 * 2. A FUTURE end is clamped to `clampEndTo`. Several windows here are
 *    calendar windows rather than data extents: the Claude drill window ends
 *    on the current week's Sunday and an active five-hour block ends at its
 *    projected reset, so on six days in seven the stated span named days no
 *    data can exist for, beside a cost figure. The clamp never moves the end
 *    below the start, which would invert the span; a same-day result collapses
 *    to the single date, which is the honest reading of "one day so far".
 */
export function formatSpan(
  span: ResolvedSpan | null | undefined,
  ctx: FmtCtx,
  options: FormatSpanOptions = {},
): string | null {
  if (span == null) return null;
  const endAt = clampedEnd(span, options.clampEndTo);
  const startYear = yearOf(span.startAt, ctx);
  const endYear = yearOf(endAt, ctx);
  const render = startYear != null && endYear != null && startYear !== endYear
    ? fmt.dateShortWithYear
    : fmt.dateShort;
  const start = render(span.startAt, ctx);
  const end = render(endAt, ctx);
  if (start == null || end == null) return null;
  return start === end ? start : `${start} – ${end}`;
}

function clampedEnd(span: ResolvedSpan, clampEndTo: string | number | null | undefined): string {
  if (clampEndTo == null) return span.endAt;
  const limit = typeof clampEndTo === 'number' ? clampEndTo : Date.parse(clampEndTo);
  const end = Date.parse(span.endAt);
  const start = Date.parse(span.startAt);
  if (Number.isNaN(limit) || Number.isNaN(end) || Number.isNaN(start)) return span.endAt;
  if (limit >= end) return span.endAt;
  return new Date(Math.max(limit, start)).toISOString();
}

function yearOf(iso: string, ctx: FmtCtx): string | null {
  const withYear = fmt.dateShortWithYear(iso, ctx);
  return withYear == null ? null : withYear.slice(-4);
}
