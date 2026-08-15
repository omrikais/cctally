import { describe, it, expect } from 'vitest';
import { fmt, roundIsoToTenMinutes } from './fmt';

describe('roundIsoToTenMinutes (5h-block reset-jitter display normalization)', () => {
  it('rounds a jittered :39 boundary up to :40 (mirrors the server helper)', () => {
    expect(roundIsoToTenMinutes('2026-04-15T04:39:59Z'))
      .toBe('2026-04-15T04:40:00.000Z');
    expect(roundIsoToTenMinutes('2026-07-11T10:39:00Z'))
      .toBe('2026-07-11T10:40:00.000Z');
  });
  it('rounds down when nearer the lower boundary', () => {
    expect(roundIsoToTenMinutes('2026-04-15T04:34:00Z'))
      .toBe('2026-04-15T04:30:00.000Z');
  });
  it('rounds the exact half up', () => {
    expect(roundIsoToTenMinutes('2026-04-15T04:35:00Z'))
      .toBe('2026-04-15T04:40:00.000Z');
  });
  it('is idempotent on a boundary', () => {
    expect(roundIsoToTenMinutes('2026-04-15T04:40:00Z'))
      .toBe('2026-04-15T04:40:00.000Z');
  });
  it('rolls over the hour', () => {
    expect(roundIsoToTenMinutes('2026-04-15T04:56:00Z'))
      .toBe('2026-04-15T05:00:00.000Z');
  });
  it('passes unparseable input through unchanged', () => {
    expect(roundIsoToTenMinutes('not-a-date')).toBe('not-a-date');
  });
});

describe('fmt.usd0 (#423 item 25 — accurate adaptive money hero)', () => {
  it('keeps known cents while leaving exact dollars compact', () => {
    expect(fmt.usd0(254.27)).toBe('$254.27');
    expect(fmt.usd0(254.6)).toBe('$254.60');
    expect(fmt.usd0(254)).toBe('$254');
    expect(fmt.usd0(0)).toBe('$0');
  });
  it('renders an em-dash for null/undefined (never NaN or a bare $)', () => {
    expect(fmt.usd0(null)).toBe('—');
    expect(fmt.usd0(undefined)).toBe('—');
  });
});

describe('fmt.durationMs', () => {
  it('formats sub-minute as X.Xs', () => {
    expect(fmt.durationMs(10668)).toBe('10.7s');
    expect(fmt.durationMs(4200)).toBe('4.2s');
  });
  it('formats >= 60s as Xm Ys, dropping a trailing 0s', () => {
    expect(fmt.durationMs(125000)).toBe('2m 5s');
    expect(fmt.durationMs(120000)).toBe('2m');
  });
  it('carries 59.5s+ up to the next whole minute (no "Xm 60s")', () => {
    expect(fmt.durationMs(119999)).toBe('2m');
    expect(fmt.durationMs(179500)).toBe('3m');
    expect(fmt.durationMs(59999)).toBe('1m');
  });
  it('handles null/undefined', () => {
    expect(fmt.durationMs(null)).toBe('—');
    expect(fmt.durationMs(undefined)).toBe('—');
  });
});

describe('fmt.gapDuration (#177 S5)', () => {
  it('renders "—" for null/undefined/NaN/negative', () => {
    expect(fmt.gapDuration(null)).toBe('—');
    expect(fmt.gapDuration(undefined)).toBe('—');
    expect(fmt.gapDuration(NaN)).toBe('—');
    expect(fmt.gapDuration(-5)).toBe('—');
  });
  it('renders < 60 min as whole minutes', () => {
    expect(fmt.gapDuration(2520)).toBe('42 min');   // 42 min
    expect(fmt.gapDuration(600)).toBe('10 min');    // exactly the gap threshold
  });
  it('promotes to hours once rounded minutes hit 60 (no "60 min")', () => {
    // 3599s rounds to 60 min — must read "1 h", not "60 min".
    expect(fmt.gapDuration(3599)).toBe('1 h');
    // 3570s (59.5 min) also rounds to 60 min and must promote.
    expect(fmt.gapDuration(3570)).toBe('1 h');
  });
  it('renders >= 60 min as one-decimal hours, dropping a trailing .0', () => {
    expect(fmt.gapDuration(3600)).toBe('1 h');       // 1.0 -> "1"
    expect(fmt.gapDuration(7200)).toBe('2 h');       // 2.0 -> "2"
    expect(fmt.gapDuration(34200)).toBe('9.5 h');    // 9.5
  });
});

describe('fmt.tokens (#177 S5)', () => {
  it('renders "—" for null/undefined/NaN', () => {
    expect(fmt.tokens(null)).toBe('—');
    expect(fmt.tokens(undefined)).toBe('—');
    expect(fmt.tokens(NaN)).toBe('—');
  });
  it('renders < 1000 as a raw integer', () => {
    expect(fmt.tokens(873)).toBe('873');
    expect(fmt.tokens(0)).toBe('0');
  });
  it('renders >= 1000 as one-decimal k (trailing .0 dropped)', () => {
    expect(fmt.tokens(1200)).toBe('1.2k');
    expect(fmt.tokens(310000)).toBe('310k');
  });
  it('renders >= 1_000_000 as one-decimal M (trailing .0 dropped)', () => {
    expect(fmt.tokens(4_100_000)).toBe('4.1M');
    expect(fmt.tokens(2_000_000)).toBe('2M');
  });
  it('gates the unit on post-rounding magnitude at the k→M edge (#184)', () => {
    // 999_949 one-decimal-rounds to 999.9k — still in the k band.
    expect(fmt.tokens(999_949)).toBe('999.9k');
    // 999_950 one-decimal-rounds to 1000.0k, which must promote to "1M"
    // (not "1000k").
    expect(fmt.tokens(999_950)).toBe('1M');
  });
});

describe('fmt.calDate (S5 CR-5)', () => {
  it('formats a YYYY-MM-DD calendar date as "Mon DD" with no tz shift', () => {
    expect(fmt.calDate('2026-06-29')).toBe('Jun 29');
    expect(fmt.calDate('2026-01-05')).toBe('Jan 05');
  });
  it('returns null on null/invalid input', () => {
    expect(fmt.calDate(null)).toBeNull();
    expect(fmt.calDate(undefined)).toBeNull();
    expect(fmt.calDate('not-a-date')).toBeNull();
  });
});

describe('fmt.calendarDateKey', () => {
  const ctx = (tz: string) => ({ tz, offsetLabel: 'test' });

  it('derives the YYYY-MM-DD key in the configured display timezone', () => {
    const instant = '2026-07-19T23:30:00Z';
    expect(fmt.calendarDateKey(instant, ctx('Asia/Jerusalem'))).toBe('2026-07-20');
    expect(fmt.calendarDateKey(instant, ctx('America/Los_Angeles'))).toBe('2026-07-19');
  });

  it('returns null on null or invalid input', () => {
    expect(fmt.calendarDateKey(null, ctx('Etc/UTC'))).toBeNull();
    expect(fmt.calendarDateKey('not-a-date', ctx('Etc/UTC'))).toBeNull();
  });
});

describe('fmt.durationCompact', () => {
  it('drops the 0h prefix and zero-pad for sub-hour durations', () => {
    expect(fmt.durationCompact(7 * 60)).toBe('7m');
    expect(fmt.durationCompact(30)).toBe('0m');   // sub-minute floors to 0m
    expect(fmt.durationCompact(0)).toBe('0m');
  });
  it('keeps the "Xh YYm" form (padded minutes) at/above one hour', () => {
    expect(fmt.durationCompact(3600 + 56 * 60)).toBe('1h 56m');
    expect(fmt.durationCompact(3600 + 7 * 60)).toBe('1h 07m');
  });
  it('renders an em dash for null/undefined (mirrors hhmm)', () => {
    expect(fmt.durationCompact(null)).toBe('—');
    expect(fmt.durationCompact(undefined)).toBe('—');
  });
});

// #574 — the Recent Alerts when-cell formatter. Every case below passes `nowMs`
// explicitly, so no assertion here depends on the wall clock.
//
// The terminal branch is the subject of this issue: it used to emit a bare
// calendar day (`Apr 16`), which collapsed every alert that fired on one day
// into one string. It now emits the canonical absolute instant.
describe('fmt.relativeOrAbsolute (#574 — inspectable firing instant)', () => {
  const UTC = { tz: 'Etc/UTC', offsetLabel: 'UTC' };
  // Midday, so the whole sub-24h ladder stays inside one calendar day.
  const NOON = Date.parse('2026-04-17T12:00:00Z');
  // The last millisecond of a day, which is the only vantage point from which
  // an instant can be both nearly 48 hours old and still on yesterday's date.
  const END_OF_DAY = Date.parse('2026-04-17T23:59:59.999Z');

  // The instant that is `deltaMs` older than `nowMs`. A negative delta yields a
  // future instant.
  const ago = (nowMs: number, deltaMs: number): string =>
    new Date(nowMs - deltaMs).toISOString();

  it('reads "just now" up to but not including 60 seconds', () => {
    expect(fmt.relativeOrAbsolute(ago(NOON, 0), UTC, NOON)).toBe('just now');
    expect(fmt.relativeOrAbsolute(ago(NOON, 59_999), UTC, NOON)).toBe('just now');
    // Exact boundary: 60_000 leaves the rung. A `<` → `<=` regression reds here.
    expect(fmt.relativeOrAbsolute(ago(NOON, 60_000), UTC, NOON)).toBe('1m ago');
  });

  it('reads "just now" for a future instant (clock skew)', () => {
    expect(fmt.relativeOrAbsolute(ago(NOON, -5_000), UTC, NOON)).toBe('just now');
    expect(fmt.relativeOrAbsolute(ago(NOON, -86_400_000), UTC, NOON)).toBe('just now');
  });

  it('reads "Nm ago" up to but not including one hour', () => {
    expect(fmt.relativeOrAbsolute(ago(NOON, 300_000), UTC, NOON)).toBe('5m ago');
    expect(fmt.relativeOrAbsolute(ago(NOON, 3_599_999), UTC, NOON)).toBe('59m ago');
    // Exact boundary.
    expect(fmt.relativeOrAbsolute(ago(NOON, 3_600_000), UTC, NOON)).toBe('1h ago');
  });

  it('reads "Nh ago" up to but not including 24 hours', () => {
    expect(fmt.relativeOrAbsolute(ago(NOON, 7_200_000), UTC, NOON)).toBe('2h ago');
    expect(fmt.relativeOrAbsolute(ago(NOON, 86_399_999), UTC, NOON)).toBe('23h ago');
    // Exact boundary: at 24h the instant is the previous calendar day here.
    expect(fmt.relativeOrAbsolute(ago(NOON, 86_400_000), UTC, NOON)).toBe('Yesterday');
  });

  it('reads "Yesterday" up to but not including 48 hours, then the absolute instant', () => {
    // 172_799_999 ms before the last millisecond of 2026-04-17 is
    // 2026-04-16T00:00:00Z — still yesterday's calendar day.
    expect(fmt.relativeOrAbsolute(ago(END_OF_DAY, 172_799_999), UTC, END_OF_DAY))
      .toBe('Yesterday');
    // Exact boundary: one millisecond older lands on 2026-04-15.
    expect(fmt.relativeOrAbsolute(ago(END_OF_DAY, 172_800_000), UTC, END_OF_DAY))
      .toBe('Apr 15 23:59 UTC');
  });

  it('falls through to the absolute branch between 24h and 48h when the instant is already two calendar days back', () => {
    // 30 hours before 2026-04-17T02:00:00Z is 2026-04-15T20:00:00Z. The delta is
    // under 48 hours, but the calendar day is not yesterday, so the `Yesterday`
    // rung does not apply. The old header comment denied this branch existed.
    const now = Date.parse('2026-04-17T02:00:00Z');
    expect(fmt.relativeOrAbsolute(ago(now, 108_000_000), UTC, now))
      .toBe('Apr 15 20:00 UTC');
  });

  it('decides the "Yesterday" rung by the calendar day in ctx.tz, not by the delta', () => {
    // One instant, one `nowMs`, two display zones. In UTC the instant is two
    // calendar days back and reaches the absolute branch; in Los Angeles the
    // same instant is yesterday.
    const now = Date.parse('2026-04-18T00:30:00Z');
    const instant = '2026-04-16T20:00:00Z';
    expect(fmt.relativeOrAbsolute(instant, UTC, now)).toBe('Apr 16 20:00 UTC');
    expect(fmt.relativeOrAbsolute(
      instant, { tz: 'America/Los_Angeles', offsetLabel: 'PDT' }, now,
    )).toBe('Yesterday');
  });

  it('finds the previous calendar day across a fall-back DST transition', () => {
    // New York's 2026 fall-back makes this local day 25 hours long. Subtracting
    // exactly 24 hours from 23:30 EST lands at 00:30 EDT on the same date, so
    // millisecond arithmetic cannot identify the previous calendar day.
    const now = Date.parse('2026-11-02T04:30:00Z'); // Nov 1 23:30 EST
    const instant = '2026-11-01T03:45:00Z'; // Oct 31 23:45 EDT
    expect(fmt.relativeOrAbsolute(
      instant, { tz: 'America/New_York', offsetLabel: 'EST' }, now,
    )).toBe('Yesterday');
  });

  it('renders the absolute branch in ctx.tz with the instant\'s own zone abbreviation', () => {
    const now = Date.parse('2026-04-20T00:00:00Z');
    const instant = '2026-04-16T21:32:00Z';
    expect(fmt.relativeOrAbsolute(
      instant, { tz: 'America/Los_Angeles', offsetLabel: 'PDT' }, now,
    )).toBe('Apr 16 14:32 PDT');
  });

  it('renders a normalized numeric offset for a zone Intl gives no abbreviation for', () => {
    const now = Date.parse('2026-04-20T00:00:00Z');
    // Etc/GMT-3 is three hours EAST of UTC (the POSIX sign convention), and
    // CLDR supplies no abbreviation for it, so the suffix is the padded "+03".
    expect(fmt.relativeOrAbsolute(
      '2026-04-16T21:32:00Z', { tz: 'Etc/GMT-3', offsetLabel: '+03' }, now,
    )).toBe('Apr 17 00:32 +03');
  });

  it('distinguishes two instants minutes apart on one long-past calendar day', () => {
    const now = Date.parse('2026-08-15T09:00:00Z');
    const first = fmt.relativeOrAbsolute('2026-04-16T13:56:00Z', UTC, now);
    const last = fmt.relativeOrAbsolute('2026-04-16T13:59:00Z', UTC, now);
    expect(first).toBe('Apr 16 13:56 UTC');
    expect(last).toBe('Apr 16 13:59 UTC');
    expect(first).not.toBe(last);
  });

  it('renders an em dash for absent, empty and unparseable input', () => {
    expect(fmt.relativeOrAbsolute(null, UTC, NOON)).toBe('—');
    expect(fmt.relativeOrAbsolute(undefined, UTC, NOON)).toBe('—');
    expect(fmt.relativeOrAbsolute('', UTC, NOON)).toBe('—');
    expect(fmt.relativeOrAbsolute('not-a-date', UTC, NOON)).toBe('—');
  });
});
