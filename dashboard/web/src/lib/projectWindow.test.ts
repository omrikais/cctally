import { describe, expect, it } from 'vitest';
import { claudeCurrentWeekStartAt, claudeDrillWindow, formatSpan } from './projectWindow';
import type { Envelope } from '../types/envelope';

const UTC = { tz: 'UTC', offsetLabel: 'UTC' };

// The anchor every case uses: Monday 2026-08-10, the shape
// `projects.current_week.week_start_at` carries.
const MONDAY = '2026-08-10T00:00:00Z';

describe('claudeDrillWindow — the span a project detail actually reported', () => {
  it('resolves one week as the current week alone', () => {
    expect(claudeDrillWindow(MONDAY, 1)).toEqual({
      startAt: '2026-08-10T00:00:00.000Z',
      endAt: '2026-08-16T23:59:59.999Z',
    });
  });

  it('reaches back 7 * (weeks - 1) days, mirroring the server bounds', () => {
    // `_project_detail_for_window`: since = cw_start - 7 * (weeks - 1) days.
    expect(claudeDrillWindow(MONDAY, 4)?.startAt).toBe('2026-07-20T00:00:00.000Z');
    expect(claudeDrillWindow(MONDAY, 8)?.startAt).toBe('2026-06-22T00:00:00.000Z');
    expect(claudeDrillWindow(MONDAY, 12)?.startAt).toBe('2026-05-25T00:00:00.000Z');
  });

  it('never names the following Monday, which the window does not report on', () => {
    // The server's upper bound is `cw_start + 7d`. Naming it would read as a
    // day the drill covers, so the last COVERED INSTANT is what is stated —
    // one millisecond before it, not the last day's midnight.
    expect(claudeDrillWindow(MONDAY, 8)?.endAt).toBe('2026-08-16T23:59:59.999Z');
  });

  it('returns null rather than guessing when the anchor is missing', () => {
    expect(claudeDrillWindow(null, 4)).toBeNull();
    expect(claudeDrillWindow(undefined, 4)).toBeNull();
    expect(claudeDrillWindow('not-a-date', 4)).toBeNull();
  });

  it('returns null for a window count that is not a positive whole number', () => {
    expect(claudeDrillWindow(MONDAY, 0)).toBeNull();
    expect(claudeDrillWindow(MONDAY, -4)).toBeNull();
    expect(claudeDrillWindow(MONDAY, 4.5)).toBeNull();
    expect(claudeDrillWindow(MONDAY, null)).toBeNull();
  });
});

describe('formatSpan', () => {
  it('renders both resolved dates', () => {
    expect(formatSpan(claudeDrillWindow(MONDAY, 8), UTC)).toBe('Jun 22 – Aug 16');
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
    expect(formatSpan(
      claudeDrillWindow(MONDAY, 1),
      { tz: 'America/Los_Angeles', offsetLabel: 'PDT' },
    )).toBe('Aug 09 – Aug 16');
  });

  it('returns null on an unrenderable bound rather than printing half a span', () => {
    expect(formatSpan(null, UTC)).toBeNull();
    expect(formatSpan({ startAt: 'nope', endAt: MONDAY }, UTC)).toBeNull();
  });
});

// #556 S2 QA P2-5 — the last COVERED instant, not the last day's midnight.
describe('claudeDrillWindow end bound outside UTC', () => {
  it('names the last covered local day in a zone behind UTC', () => {
    // `anchor + 6 days` is the last day's MIDNIGHT. Rendered in a zone behind
    // UTC that instant resolves to the PREVIOUS local day, so the drill named
    // a day one short of what it reported. The bound is the last covered
    // INSTANT — `anchor + 7 days - 1ms`.
    const span = claudeDrillWindow(MONDAY, 1)!;
    expect(span.endAt).toBe('2026-08-16T23:59:59.999Z');
    expect(formatSpan(span, { tz: 'America/Los_Angeles', offsetLabel: 'PDT' }))
      .toBe('Aug 09 – Aug 16');
  });

  it('still names Aug 16 in UTC, where the old bound happened to agree', () => {
    expect(formatSpan(claudeDrillWindow(MONDAY, 8), UTC)).toBe('Jun 22 – Aug 16');
  });
});

// #556 S2 QA P2-4 — no stated span may name a day that has not happened.
describe('formatSpan clamps a future end to the snapshot instant', () => {
  it('clamps a window end past `generated_at` back to it', () => {
    // The Claude drill window ends on the current week's Sunday, so on six days
    // in seven the header named days no data can exist for, beside a cost.
    expect(formatSpan(claudeDrillWindow(MONDAY, 8), UTC, {
      clampEndTo: '2026-08-14T09:00:00Z',
    })).toBe('Jun 22 – Aug 14');
  });

  it('leaves an end already in the past alone', () => {
    expect(formatSpan(claudeDrillWindow(MONDAY, 8), UTC, {
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
    expect(formatSpan(claudeDrillWindow(MONDAY, 8), UTC, { clampEndTo: null }))
      .toBe('Jun 22 – Aug 16');
    expect(formatSpan(claudeDrillWindow(MONDAY, 8), UTC, { clampEndTo: 'nope' }))
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

// #556 S2 QA P2-9 — the top-level block is the object the route reads.
describe('claudeCurrentWeekStartAt precedence', () => {
  it('prefers the top-level projects envelope over the retained source copy', () => {
    // `env.projects` is `snap.projects_envelope`, rebuilt every tick. The
    // source-domain copy lives in a provider state deliberately RETAINED
    // across ticks, so at a week rollover with a retained bundle the source
    // copy is one week behind what the server reported this tick.
    const env = {
      projects: { current_week: { week_start_at: '2026-08-10T00:00:00Z' } },
      sources: {
        claude: {
          data: { projects: { current_week: { week_start_at: '2026-08-03T00:00:00Z' } } },
        },
      },
    } as unknown as Envelope;
    expect(claudeCurrentWeekStartAt(env)).toBe('2026-08-10T00:00:00Z');
  });

  it('falls back to the source copy when the top-level block is absent', () => {
    const env = {
      sources: {
        claude: {
          data: { projects: { current_week: { week_start_at: '2026-08-03T00:00:00Z' } } },
        },
      },
    } as unknown as Envelope;
    expect(claudeCurrentWeekStartAt(env)).toBe('2026-08-03T00:00:00Z');
  });
});
