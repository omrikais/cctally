// #620 S1 D11/D12 — the client scope kernel, mirroring `bin/_lib_alert_scope.py`.
import { describe, expect, it } from 'vitest';
import {
  CLOSED_WINDOW_REASON,
  LIVE_WEEK_MISMATCH_REASON,
  NO_BLOCK_IDENTITY_REASON,
  NO_CALENDAR_WEEK_SURFACE_REASON,
  VENDOR_WIDE_PROJECT_REASON,
  alertNavigation,
  deriveScope,
  envelopeNow,
  isWindowLive,
  parseInstantMs,
  periodWindowEndMs,
} from '../src/lib/alertScope';
import type { AlertEntry, Envelope } from '../src/types/envelope';

const WEEK_START = '2026-04-13T14:00:00Z';
const WEEK_END = '2026-04-20T14:00:00Z';
const INSIDE_WEEK = new Date('2026-04-16T09:00:00Z');
const AFTER_WEEK = new Date('2026-04-21T09:00:00Z');

function entry(over: Partial<AlertEntry> & { axis: AlertEntry['axis'] }): AlertEntry {
  return {
    id: 'opaque:never-parsed',
    threshold: 90,
    crossed_at: '2026-04-16T08:00:00Z',
    alerted_at: '2026-04-16T08:00:00Z',
    context: {},
    ...over,
  } as AlertEntry;
}

function envAt(resetAtUtc: string | null, generatedAt = '2026-04-16T09:00:00Z'): Envelope {
  return {
    generated_at: generatedAt,
    current_week: resetAtUtc == null ? null : { reset_at_utc: resetAtUtc },
  } as unknown as Envelope;
}

const LIVE_ENV = envAt(WEEK_END);

describe('#620 S1 — parsing agrees with the Python kernel', () => {
  it('reads a naive retained instant as UTC, not as local time', () => {
    expect(parseInstantMs('2026-04-13T14:00:00')).toBe(Date.parse(WEEK_START));
    expect(parseInstantMs('2026-04-13 14:00:00')).toBe(Date.parse(WEEK_START));
  });

  it('returns null for an unparseable or empty value', () => {
    for (const value of ['', '   ', 'not-a-date', null, undefined, 17]) {
      expect(parseInstantMs(value)).toBeNull();
    }
  });

  it('derives each budget period end, and only the three it knows', () => {
    const start = Date.parse('2026-01-31T00:00:00Z');
    expect(periodWindowEndMs('subscription-week', start))
      .toBe(Date.parse('2026-02-07T00:00:00Z'));
    expect(periodWindowEndMs('calendar-week', start))
      .toBe(Date.parse('2026-02-07T00:00:00Z'));
    // Jan 31 + one civil month clamps to the last day February has.
    expect(periodWindowEndMs('calendar-month', start))
      .toBe(Date.parse('2026-02-28T00:00:00Z'));
    expect(periodWindowEndMs('fortnight', start)).toBeNull();
    expect(periodWindowEndMs('calendar-month', null)).toBeNull();
  });
});

describe('#620 S1 — deriveScope per axis', () => {
  it('weekly prefers the retained reset instant over the calendar label', () => {
    const scope = deriveScope(entry({
      axis: 'weekly',
      context: { week_start_at: WEEK_START, week_start_date: '2026-04-13' },
    }));
    expect(scope.available).toBe(true);
    expect(scope.windowStartMs).toBe(Date.parse(WEEK_START));
    expect(scope.windowEndMs).toBe(Date.parse(WEEK_END));
    expect(scope.windowGranularity).toBe('instant');
    expect(scope.provider).toBe('claude');
  });

  it('weekly falls back to the calendar day and drops the granularity with it', () => {
    const scope = deriveScope(entry({
      axis: 'weekly',
      context: { week_start_date: '2026-04-13' },
    }));
    expect(scope.available).toBe(true);
    expect(scope.windowStartMs).toBe(Date.parse('2026-04-13T00:00:00Z'));
    expect(scope.windowGranularity).toBe('day');
  });

  it('weekly withholds when it retains neither', () => {
    const scope = deriveScope(entry({ axis: 'weekly', context: {} }));
    expect(scope.available).toBe(false);
    expect(scope.withheldReason).toContain('retains no week start');
    expect(scope.windowStartMs).toBeNull();
    expect(scope.windowEndMs).toBeNull();
  });

  it('five_hour reads the reset key as the window END, never as its start', () => {
    const resetMs = Date.parse('2026-04-16T10:00:00Z');
    const scope = deriveScope(entry({
      axis: 'five_hour',
      context: { five_hour_window_key: resetMs / 1000 },
    }));
    expect(scope.windowEndMs).toBe(resetMs);
    expect(scope.windowStartMs).toBe(resetMs - 5 * 3600 * 1000);
  });

  it('five_hour prefers the retained block start', () => {
    const scope = deriveScope(entry({
      axis: 'five_hour',
      context: { block_start_at: '2026-04-16T05:00:00Z', five_hour_window_key: 1 },
    }));
    expect(scope.windowStartMs).toBe(Date.parse('2026-04-16T05:00:00Z'));
    expect(scope.windowEndMs).toBe(Date.parse('2026-04-16T10:00:00Z'));
  });

  it('codex_budget guesses no period when the row names none', () => {
    const scope = deriveScope(entry({
      axis: 'codex_budget',
      context: { period_start_at: '2026-04-01T00:00:00Z' },
    }));
    expect(scope.available).toBe(false);
    expect(scope.withheldReason).toContain('no derivable period');
    expect(scope.provider).toBe('codex');
  });

  it('budget defaults to the subscription week Claude actually has', () => {
    const scope = deriveScope(entry({
      axis: 'budget',
      context: { week_start_at: WEEK_START },
    }));
    expect(scope.available).toBe(true);
    expect(scope.windowEndMs).toBe(Date.parse(WEEK_END));
  });

  it('project_budget is vendor-wide whatever account stamp it carries', () => {
    for (const accountKey of [undefined, '*', 'acct-abc']) {
      const scope = deriveScope(entry({
        axis: 'project_budget',
        accountKey,
        context: { week_start_at: WEEK_START, project_key: '/repo/a' },
      }));
      expect(scope.accountScope).toBe('vendor_wide');
    }
  });

  it('projected supplies subscription-week for weekly_pct and withholds otherwise', () => {
    const weekly = deriveScope(entry({
      axis: 'projected',
      context: { metric: 'weekly_pct', week_start_at: WEEK_START },
    }));
    expect(weekly.available).toBe(true);
    expect(weekly.costBasis).toBeNull();

    const budget = deriveScope(entry({
      axis: 'projected',
      context: { metric: 'codex_budget_usd', period_start_at: '2026-04-01T00:00:00Z' },
    }));
    expect(budget.available).toBe(false);
    expect(budget.provider).toBe('codex');
  });

  it('never reads the alert id — two rows differing only by id derive one scope', () => {
    const context = { week_start_at: WEEK_START };
    const a = deriveScope(entry({ axis: 'weekly', id: 'weekly:1:90', context }));
    const b = deriveScope(entry({
      axis: 'weekly', id: 'nonsense that would not parse', context,
    }));
    expect(a).toEqual(b);
  });
});

describe('#620 S1 — isWindowLive', () => {
  const scope = deriveScope(entry({ axis: 'weekly', context: { week_start_at: WEEK_START } }));

  it('is half-open: inclusive at the start, exclusive at the end', () => {
    expect(isWindowLive(scope, new Date(WEEK_START))).toBe(true);
    expect(isWindowLive(scope, new Date(WEEK_END))).toBe(false);
    expect(isWindowLive(scope, INSIDE_WEEK)).toBe(true);
    expect(isWindowLive(scope, AFTER_WEEK)).toBe(false);
  });

  it('a withheld scope is never live', () => {
    const none = deriveScope(entry({ axis: 'weekly', context: {} }));
    expect(none.available).toBe(false);
    expect(isWindowLive(none, INSIDE_WEEK)).toBe(false);
  });

  it('envelopeNow anchors to the envelope, not the browser clock', () => {
    expect(envelopeNow(envAt(WEEK_END, '2026-04-16T09:00:00Z')).toISOString())
      .toBe('2026-04-16T09:00:00.000Z');
  });
});

describe('#620 S1 D12 — alertNavigation', () => {
  it('a live weekly alert opens the current week', () => {
    const nav = alertNavigation(
      entry({ axis: 'weekly', context: { week_start_at: WEEK_START } }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(nav.available).toBe(true);
    expect(nav.target?.modal).toBe('current-week');
    expect(nav.target?.source).toBe('claude');
  });

  it('a weekly alert whose window is not the live week opens nothing', () => {
    // Live by the clock, but the dashboard's live week ends elsewhere — the
    // equality D12 requires the client to assert rather than assume.
    const nav = alertNavigation(
      entry({ axis: 'weekly', context: { week_start_at: WEEK_START } }),
      envAt('2026-04-20T20:00:00Z'), INSIDE_WEEK,
    );
    expect(nav.available).toBe(false);
    expect(nav.withheldReason).toBe(LIVE_WEEK_MISMATCH_REASON);
    expect(nav.target).toBeNull();
  });

  it('a closed weekly window states why and opens nothing', () => {
    const nav = alertNavigation(
      entry({ axis: 'weekly', context: { week_start_at: WEEK_START } }),
      LIVE_ENV, AFTER_WEEK,
    );
    expect(nav.available).toBe(false);
    expect(nav.withheldReason).toBe(CLOSED_WINDOW_REASON);
    expect(nav.target).toBeNull();
  });

  it('a historical five-hour block still opens, addressed by block_start_at', () => {
    const nav = alertNavigation(
      entry({ axis: 'five_hour', context: { block_start_at: '2026-01-02T05:00:00Z' } }),
      LIVE_ENV, AFTER_WEEK,
    );
    expect(nav.available).toBe(true);
    expect(nav.target?.modal).toBe('block');
    expect(nav.target?.blockStartAt).toBe('2026-01-02T05:00:00Z');
  });

  it('an alert with no recorded block start states why and opens nothing', () => {
    const nav = alertNavigation(
      entry({
        axis: 'five_hour',
        context: { five_hour_window_key: Date.parse('2026-01-02T10:00:00Z') / 1000 },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(nav.available).toBe(false);
    expect(nav.withheldReason).toBe(NO_BLOCK_IDENTITY_REASON);
    expect(nav.target).toBeNull();
  });

  it('a vendor-wide project-budget row cannot be narrowed and opens nothing', () => {
    const nav = alertNavigation(
      entry({
        axis: 'project_budget',
        accountKey: '*',
        context: { week_start_at: WEEK_START, project_key: '/repo/a' },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(nav.available).toBe(false);
    expect(nav.withheldReason).toBe(VENDOR_WIDE_PROJECT_REASON);
    expect(nav.target).toBeNull();
  });

  it('an undecorated project-budget row is unambiguous and opens the drill', () => {
    const nav = alertNavigation(
      entry({
        axis: 'project_budget',
        context: { week_start_at: WEEK_START, project_key: '/repo/a' },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(nav.available).toBe(true);
    expect(nav.target?.modal).toBe('projects');
    expect(nav.target?.projectKey).toBe('/repo/a');
  });

  it('a live calendar-month codex budget opens the monthly view on Codex', () => {
    const nav = alertNavigation(
      entry({
        axis: 'codex_budget',
        context: { period: 'calendar-month', period_start_at: '2026-04-01T00:00:00Z' },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(nav.available).toBe(true);
    expect(nav.target?.modal).toBe('monthly');
    expect(nav.target?.source).toBe('codex');
  });

  it('names the Codex current-week target a cycle, and leaves Claude on week', () => {
    const codexNav = alertNavigation(
      entry({
        axis: 'codex_budget',
        context: { period: 'subscription-week', period_start_at: WEEK_START },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(codexNav.available).toBe(true);
    expect(codexNav.target?.source).toBe('codex');
    expect(codexNav.target?.modal).toBe('current-week');
    // Codex navigates reset-defined quota cycles, and every other Codex
    // surface says so — `trendVocabulary` and the navigator's "Older cycle".
    expect(codexNav.target?.label).toBe('Open this cycle');

    const claudeNav = alertNavigation(
      entry({
        axis: 'budget',
        context: { period: 'subscription-week', period_start_at: WEEK_START },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(claudeNav.target?.source).toBe('claude');
    expect(claudeNav.target?.label).toBe('Open this week');
  });

  it('a calendar-week budget has no matching surface and says so', () => {
    const nav = alertNavigation(
      entry({
        axis: 'budget',
        context: { period: 'calendar-week', period_start_at: '2026-04-13T00:00:00Z' },
      }),
      LIVE_ENV, new Date('2026-04-16T09:00:00Z'),
    );
    expect(nav.available).toBe(false);
    expect(nav.withheldReason).toBe(NO_CALENDAR_WEEK_SURFACE_REASON);
  });

  it('a live projected alert opens its provider’s forecast', () => {
    const nav = alertNavigation(
      entry({
        axis: 'projected',
        context: { metric: 'weekly_pct', week_start_at: WEEK_START },
      }),
      LIVE_ENV, INSIDE_WEEK,
    );
    expect(nav.available).toBe(true);
    expect(nav.target?.modal).toBe('forecast');
    expect(nav.target?.source).toBe('claude');
  });

  it('every axis either opens something or states a reason — never both, never neither', () => {
    const rows: AlertEntry[] = [
      entry({ axis: 'weekly', context: { week_start_at: WEEK_START } }),
      entry({ axis: 'five_hour', context: { block_start_at: '2026-04-16T05:00:00Z' } }),
      entry({ axis: 'budget', context: { week_start_at: WEEK_START } }),
      entry({
        axis: 'codex_budget',
        context: { period: 'calendar-month', period_start_at: '2026-04-01T00:00:00Z' },
      }),
      entry({
        axis: 'project_budget',
        context: { week_start_at: WEEK_START, project_key: '/repo/a' },
      }),
      entry({ axis: 'projected', context: { metric: 'weekly_pct', week_start_at: WEEK_START } }),
    ];
    // Precondition asserted unconditionally: all six axes are covered.
    expect(new Set(rows.map((r) => r.axis)).size).toBe(6);
    for (const row of rows) {
      const nav = alertNavigation(row, LIVE_ENV, INSIDE_WEEK);
      expect(nav.available).toBe(nav.target != null);
      expect(nav.available).toBe(nav.withheldReason == null);
    }
  });
});
