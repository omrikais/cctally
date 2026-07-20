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

describe('fmt.usd0 (#264 S1 — whole-dollar hero)', () => {
  it('rounds to whole dollars with a leading $', () => {
    expect(fmt.usd0(254.27)).toBe('$254');
    expect(fmt.usd0(254.6)).toBe('$255');
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
