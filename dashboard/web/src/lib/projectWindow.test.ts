import { describe, expect, it } from 'vitest';
import { exclusiveWindowSpan, formatSpan } from './projectWindow';

const UTC = { tz: 'UTC', offsetLabel: 'UTC' };

// The anchor every case uses: Monday 2026-08-10, the shape
// `projects.current_week.week_start_at` carries.
const MONDAY = '2026-08-10T00:00:00Z';
const ONE_WEEK = {
  startAt: '2026-08-10T00:00:00.000Z',
  endAt: '2026-08-16T23:59:59.999Z',
};
const EIGHT_WEEKS = {
  startAt: '2026-06-22T00:00:00.000Z',
  endAt: '2026-08-16T23:59:59.999Z',
};

describe('exclusiveWindowSpan — server-published project bounds', () => {
  it('turns the exclusive server end into the last covered instant', () => {
    expect(exclusiveWindowSpan(
      '2026-07-20T00:00:00Z',
      '2026-08-17T00:00:00Z',
    )).toEqual({
      startAt: '2026-07-20T00:00:00.000Z',
      endAt: '2026-08-16T23:59:59.999Z',
    });
  });

  it('withholds a span when either authoritative bound is invalid', () => {
    expect(exclusiveWindowSpan(null, '2026-08-17T00:00:00Z')).toBeNull();
    expect(exclusiveWindowSpan('2026-07-20T00:00:00Z', 'bad')).toBeNull();
    expect(exclusiveWindowSpan(
      '2026-08-17T00:00:00Z',
      '2026-08-17T00:00:00Z',
    )).toBeNull();
  });
});

describe('formatSpan', () => {
  it('renders both resolved dates', () => {
    expect(formatSpan(EIGHT_WEEKS, UTC)).toBe('Jun 22 – Aug 16');
  });

  it('collapses a single displayed day to one date', () => {
    expect(formatSpan(
      { startAt: '2026-08-16T01:00:00Z', endAt: '2026-08-16T23:00:00Z' },
      UTC,
    )).toBe('Aug 16');
  });

  it('renders in the display timezone, not UTC', () => {
    // Composed from the real window rather than from hard-coded bounds. The
    // literals this case used to carry were `anchor + 6 days` — the last day's
    // MIDNIGHT — so it blessed "Aug 15" as correct display-timezone behaviour
    // when the drill had in fact reported through Aug 16.
    expect(formatSpan(ONE_WEEK, { tz: 'America/Los_Angeles', offsetLabel: 'PDT' }))
      .toBe('Aug 09 – Aug 16');
  });

  it('returns null on an unrenderable bound rather than printing half a span', () => {
    expect(formatSpan(null, UTC)).toBeNull();
    expect(formatSpan({ startAt: 'nope', endAt: MONDAY }, UTC)).toBeNull();
  });
});

// #556 S2 QA P2-5 — the last COVERED instant, not the last day's midnight.
describe('exclusive server end outside UTC', () => {
  it('names the last covered local day in a zone behind UTC', () => {
    // `anchor + 6 days` is the last day's MIDNIGHT. Rendered in a zone behind
    // UTC that instant resolves to the PREVIOUS local day, so the drill named
    // a day one short of what it reported. The bound is the last covered
    // INSTANT — `anchor + 7 days - 1ms`.
    const span = exclusiveWindowSpan(MONDAY, '2026-08-17T00:00:00Z')!;
    expect(span.endAt).toBe('2026-08-16T23:59:59.999Z');
    expect(formatSpan(span, { tz: 'America/Los_Angeles', offsetLabel: 'PDT' }))
      .toBe('Aug 09 – Aug 16');
  });

  it('still names Aug 16 in UTC, where the old bound happened to agree', () => {
    expect(formatSpan(EIGHT_WEEKS, UTC)).toBe('Jun 22 – Aug 16');
  });
});

// #556 S2 QA P2-4 — no stated span may name a day that has not happened.
describe('formatSpan clamps a future end to the snapshot instant', () => {
  it('clamps a window end past `generated_at` back to it', () => {
    // The Claude drill window ends on the current week's Sunday, so on six days
    // in seven the header named days no data can exist for, beside a cost.
    expect(formatSpan(EIGHT_WEEKS, UTC, {
      clampEndTo: '2026-08-14T09:00:00Z',
    })).toBe('Jun 22 – Aug 14');
  });

  it('leaves an end already in the past alone', () => {
    expect(formatSpan(EIGHT_WEEKS, UTC, {
      clampEndTo: '2026-09-01T00:00:00Z',
    })).toBe('Jun 22 – Aug 16');
  });

  it('never clamps below the start, which would invert the span', () => {
    expect(formatSpan(
      { startAt: '2026-08-10T00:00:00Z', endAt: '2026-08-16T00:00:00Z' },
      UTC,
      { clampEndTo: '2026-08-01T00:00:00Z' },
    )).toBe('Aug 10');
  });

  it('ignores an unusable clamp instant rather than dropping the span', () => {
    expect(formatSpan(EIGHT_WEEKS, UTC, { clampEndTo: null }))
      .toBe('Jun 22 – Aug 16');
    expect(formatSpan(EIGHT_WEEKS, UTC, { clampEndTo: 'nope' }))
      .toBe('Jun 22 – Aug 16');
  });
});

// #556 S2 QA P1-3 — a span crossing a year boundary must say which year.
describe('formatSpan year disambiguation', () => {
  it('names both years when the two bounds fall in different ones', () => {
    // The Codex project drill reports a 365-day window. Rendered with the
    // year-free form its two bounds printed identically ("Aug 14 → Aug 14"),
    // which reads as a zero-width range rather than as a year of history.
    expect(formatSpan(
      { startAt: '2025-08-14T00:55:00Z', endAt: '2026-08-14T00:55:00Z' },
      UTC,
    )).toBe('Aug 14 2025 – Aug 14 2026');
  });

  it('keeps the year-free form inside one year', () => {
    expect(formatSpan(
      { startAt: '2026-06-22T00:00:00Z', endAt: '2026-08-16T00:00:00Z' },
      UTC,
    )).toBe('Jun 22 – Aug 16');
  });
});
