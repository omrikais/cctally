import { describe, it, expect } from 'vitest';
import { fmt, type FmtCtx } from '../src/lib/fmt';

// Stable ctx fixtures. UTC matches the legacy hard-coded suffix; PDT
// exercises a positive non-UTC tz with a real zone abbreviation.
const CTX_UTC: FmtCtx = { tz: 'Etc/UTC', offsetLabel: 'UTC' };
const CTX_PDT: FmtCtx = { tz: 'America/Los_Angeles', offsetLabel: 'PDT' };
const CTX_NUMERIC: FmtCtx = { tz: 'Asia/Riyadh', offsetLabel: '+03' };

describe('fmt.pct0', () => {
  it('rounds to integer percent', () => {
    expect(fmt.pct0(17.4)).toBe('17%');
    expect(fmt.pct0(17.5)).toBe('18%');
  });
  it('returns — for null/undefined', () => {
    expect(fmt.pct0(null)).toBe('—');
    expect(fmt.pct0(undefined)).toBe('—');
  });
});

describe('fmt.pct1', () => {
  it('formats to 1 decimal', () => {
    expect(fmt.pct1(17.42)).toBe('17.4%');
    expect(fmt.pct1(0)).toBe('0.0%');
  });
  it('returns — for null', () => {
    expect(fmt.pct1(null)).toBe('—');
  });
});

describe('fmt.usd2', () => {
  it('formats with dollar sign and 2 decimals', () => {
    expect(fmt.usd2(0.001)).toBe('$0.00');
    expect(fmt.usd2(1.5)).toBe('$1.50');
    expect(fmt.usd2(123.456)).toBe('$123.46');
  });
  it('returns — for null', () => {
    expect(fmt.usd2(null)).toBe('—');
  });
});

describe('fmt.agoSec', () => {
  it('formats as "Ns ago"', () => {
    expect(fmt.agoSec(0)).toBe('0s ago');
    expect(fmt.agoSec(42)).toBe('42s ago');
  });
  it('clamps negative to 0', () => {
    expect(fmt.agoSec(-5)).toBe('0s ago');
  });
  it('returns — for null', () => {
    expect(fmt.agoSec(null)).toBe('—');
  });
});

describe('fmt.hhmm', () => {
  it('formats hours and minutes with zero-padding', () => {
    expect(fmt.hhmm(3660)).toBe('1h 01m');
    expect(fmt.hhmm(86399)).toBe('23h 59m');
    expect(fmt.hhmm(0)).toBe('0h 00m');
  });
  it('returns — for null', () => {
    expect(fmt.hhmm(null)).toBe('—');
  });
});

describe('fmt.ddhh', () => {
  it('formats days and hours', () => {
    expect(fmt.ddhh(0)).toBe('0d 0h');
    expect(fmt.ddhh(90_000)).toBe('1d 1h');
  });
  it('returns — for null', () => {
    expect(fmt.ddhh(null)).toBe('—');
  });
});

describe('fmt.datetimeShort (ctx-based)', () => {
  it('formats ISO-Z into "Mon D HH:MM UTC" against UTC ctx', () => {
    expect(fmt.datetimeShort('2026-04-24T13:07:00Z', CTX_UTC)).toBe('Apr 24 13:07 UTC');
  });
  it('shifts wall-clock and labels with non-UTC ctx', () => {
    // 13:07 UTC on 2026-04-24 → 06:07 PDT (UTC-7 in April).
    expect(fmt.datetimeShort('2026-04-24T13:07:00Z', CTX_PDT)).toBe('Apr 24 06:07 PDT');
  });
  it('uses numeric offset_label when provided', () => {
    expect(fmt.datetimeShort('2026-04-24T13:07:00Z', CTX_NUMERIC)).toBe('Apr 24 16:07 +03');
  });
  it('derives suffix per-timestamp across a DST boundary (CR fix)', () => {
    // ctx.offsetLabel is snapshot-pinned to "EDT" (May), but the rendered
    // timestamp is in January where America/New_York is UTC-5 / EST. The
    // suffix must reflect the per-instant zone, not the ctx label, else
    // the body shows EST wall-clock with an "EDT" suffix.
    const CTX_NY_EDT_SNAPSHOT: FmtCtx = {
      tz: 'America/New_York',
      offsetLabel: 'EDT', // computed at a May 2026 generated_at
    };
    expect(fmt.datetimeShort('2026-01-15T13:07:00Z', CTX_NY_EDT_SNAPSHOT))
      .toBe('Jan 15 08:07 EST');
  });
  it('returns — for null/invalid', () => {
    expect(fmt.datetimeShort(null, CTX_UTC)).toBe('—');
    expect(fmt.datetimeShort('not a date', CTX_UTC)).toBe('—');
  });
});

describe('fmt.datetimeShortZ (ctx-based)', () => {
  it('emits numeric offset for UTC ctx', () => {
    // shortOffset Intl emits "GMT" for Etc/UTC; after stripping GMT → "".
    // This is the spec-compliant degenerate case; the body remains.
    const out = fmt.datetimeShortZ('2026-04-24T13:07:00Z', CTX_UTC);
    expect(out.startsWith('Apr 24 13:07')).toBe(true);
  });
  it('formats numeric offset for non-UTC ctx', () => {
    // PDT is UTC-7; shortOffset emits "GMT-7" → strip → "-7" → pad → "-07".
    expect(fmt.datetimeShortZ('2026-04-24T13:07:00Z', CTX_PDT)).toBe('Apr 24 06:07-07');
  });
  it('returns — for null', () => {
    expect(fmt.datetimeShortZ(null, CTX_UTC)).toBe('—');
  });
});

describe('fmt.dateShort (ctx-based)', () => {
  it('returns "Mon DD" without offset suffix', () => {
    expect(fmt.dateShort('2026-04-24T13:07:00Z', CTX_UTC)).toBe('Apr 24');
  });
  it('shifts the calendar day across midnight in the target tz', () => {
    // 02:00 UTC on 2026-04-25 is still 19:00 on 2026-04-24 in PDT.
    expect(fmt.dateShort('2026-04-25T02:00:00Z', CTX_PDT)).toBe('Apr 24');
  });
  it('returns null for null', () => {
    expect(fmt.dateShort(null, CTX_UTC)).toBe(null);
  });
});

describe('fmt.weekStart (calendar-only)', () => {
  // weekStart input is a calendar date (`YYYY-MM-DD`), not a clock
  // instant. Routing it through the tz-converting `dateShort` path
  // would interpret the date-only ISO as UTC midnight and shift the
  // wall-clock day for users west of UTC, rendering "Apr 26" for
  // PDT when the user's snapshot context says the week starts on
  // Apr 27.
  it('renders "Apr 27" for a calendar date regardless of ctx.tz (UTC)', () => {
    expect(fmt.weekStart('2026-04-27', CTX_UTC)).toBe('Apr 27');
  });
  it('does NOT shift the day for a west-of-UTC ctx.tz', () => {
    // Regression: prior behavior shifted to "Apr 26" in PDT.
    expect(fmt.weekStart('2026-04-27', CTX_PDT)).toBe('Apr 27');
  });
  it('does NOT shift the day for an east-of-UTC ctx.tz', () => {
    // East-of-UTC zones never tripped the off-by-one in the same
    // direction, but verify the calendar value is preserved verbatim.
    expect(fmt.weekStart('2026-04-27', CTX_NUMERIC)).toBe('Apr 27');
  });
  it('returns null for null/undefined input', () => {
    expect(fmt.weekStart(null, CTX_UTC)).toBe(null);
    expect(fmt.weekStart(undefined, CTX_UTC)).toBe(null);
  });
  it('returns null for malformed input (e.g. ISO timestamp instead of date)', () => {
    expect(fmt.weekStart('2026-04-27T00:00:00Z', CTX_UTC)).toBe(null);
  });
});

describe('fmt.startedShort (ctx-based)', () => {
  it('formats with offset_label suffix by default', () => {
    expect(fmt.startedShort('2026-04-24T13:07:00Z', CTX_UTC))
      .toBe('2026-04-24 13:07 UTC');
  });
  it('omits offset_label when noSuffix is set (F4)', () => {
    expect(fmt.startedShort('2026-04-24T13:07:00Z', CTX_UTC, { noSuffix: true }))
      .toBe('2026-04-24 13:07');
  });
  it('shifts wall-clock with non-UTC ctx', () => {
    expect(fmt.startedShort('2026-04-24T13:07:00Z', CTX_PDT))
      .toBe('2026-04-24 06:07 PDT');
  });
  it('returns — for null', () => {
    expect(fmt.startedShort(null, CTX_UTC)).toBe('—');
  });
});

describe('fmt.timeHHmm (ctx-based)', () => {
  it('formats HH:MM with offset_label suffix', () => {
    expect(fmt.timeHHmm('2026-04-24T13:07:00Z', CTX_UTC)).toBe('13:07 UTC');
  });
  it('shifts wall-clock with non-UTC ctx', () => {
    expect(fmt.timeHHmm('2026-04-24T13:07:00Z', CTX_PDT)).toBe('06:07 PDT');
  });
  it('uses numeric offset_label when zone has no abbrev', () => {
    expect(fmt.timeHHmm('2026-04-24T13:07:00Z', CTX_NUMERIC)).toBe('16:07 +03');
  });
  it('omits suffix when noSuffix: true', () => {
    expect(fmt.timeHHmm('2026-04-24T13:07:00Z', CTX_UTC, { noSuffix: true }))
      .toBe('13:07');
    expect(fmt.timeHHmm('2026-04-24T13:07:00Z', CTX_PDT, { noSuffix: true }))
      .toBe('06:07');
  });
  it('returns — for null/invalid', () => {
    expect(fmt.timeHHmm(null, CTX_UTC)).toBe('—');
    expect(fmt.timeHHmm('not a date', CTX_UTC)).toBe('—');
  });
});

describe('fmt.delta', () => {
  it('formats positive with up arrow and + sign', () => {
    expect(fmt.delta(1.234)).toBe('+1.23 ↑');
  });
  it('formats negative with down arrow', () => {
    expect(fmt.delta(-0.456)).toBe('-0.46 ↓');
  });
  it('formats zero with centerdot', () => {
    expect(fmt.delta(0)).toBe('0.00 ·');
  });
  it('returns — for null', () => {
    expect(fmt.delta(null)).toBe('—');
  });
});

describe('fmt.deltaCls', () => {
  it('returns delta-pos for positive non-current', () => {
    expect(fmt.deltaCls(1, false)).toBe('delta-pos');
  });
  it('returns delta-neg for negative non-current', () => {
    expect(fmt.deltaCls(-1, false)).toBe('delta-neg');
  });
  it('returns empty for current row regardless of sign', () => {
    expect(fmt.deltaCls(1, true)).toBe('');
    expect(fmt.deltaCls(-1, true)).toBe('');
  });
  it('returns empty for null', () => {
    expect(fmt.deltaCls(null, false)).toBe('');
  });
});

describe('fmt.deltaPct', () => {
  it('formats positive as "+9%"', () => {
    expect(fmt.deltaPct(0.09)).toBe('+9%');
  });
  it('formats negative as "−4%"', () => {
    // Note: real minus sign U+2212 to match other delta rendering.
    expect(fmt.deltaPct(-0.04)).toBe('−4%');
  });
  it('renders null as "—"', () => {
    expect(fmt.deltaPct(null)).toBe('—');
    expect(fmt.deltaPct(undefined)).toBe('—');
  });
  it('renders zero as "0%"', () => {
    expect(fmt.deltaPct(0)).toBe('0%');
  });
});

describe('fmt.compact', () => {
  it('renders thousands as k', () => {
    expect(fmt.compact(414_000)).toBe('414k');
    expect(fmt.compact(1_500)).toBe('1.5k');
  });
  it('renders millions as M', () => {
    expect(fmt.compact(21_300_000)).toBe('21.3M');
    expect(fmt.compact(346_000_000)).toBe('346M');
  });
  it('renders billions as B', () => {
    expect(fmt.compact(2_500_000_000)).toBe('2.5B');
  });
  it('renders sub-thousand as plain', () => {
    expect(fmt.compact(742)).toBe('742');
  });
  it('renders null as "—"', () => {
    expect(fmt.compact(null)).toBe('—');
  });
});
