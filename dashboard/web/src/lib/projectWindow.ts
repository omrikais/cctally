import { fmt } from './fmt';
import type { FmtCtx } from './fmt';

// #556 S2 / #571 — the resolved spans two Projects surfaces have to state.
//
// Two different windows meet in the All Projects experience and neither
// contains the other, which is why both now name resolved dates instead of a
// bare unit count:
//
//   - The RANKING is the shared absolute range, thirty calendar days ending
//     today, published once at `sources.all.data.aggregates.range`.
//   - The DRILL is the authoritative half-open interval published on the
//     detail response as `window_start_at` / `window_end_at`.
//
// So a row ranked over thirty days opens a detail reporting fifty-six, and
// `window_cost_usd >= ` the ranked figure with nothing reconciling them. Naming
// each span is what keeps the two numbers from implying each other.

export interface ResolvedSpan {
  startAt: string;
  endAt: string;
}

/**
 * Convert one authoritative half-open server interval into a display span.
 *
 * The server's end is exclusive. `formatSpan` names covered calendar days, so
 * it receives the last covered millisecond rather than the next interval's
 * first instant. No subscription-window arithmetic lives in the client.
 */
export function exclusiveWindowSpan(
  startAt: string | null | undefined,
  endExclusiveAt: string | null | undefined,
): ResolvedSpan | null {
  if (startAt == null || endExclusiveAt == null) return null;
  const start = Date.parse(startAt);
  const endExclusive = Date.parse(endExclusiveAt);
  if (Number.isNaN(start) || Number.isNaN(endExclusive) || endExclusive <= start) {
    return null;
  }
  return {
    startAt: new Date(start).toISOString(),
    endAt: new Date(endExclusive - 1).toISOString(),
  };
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
