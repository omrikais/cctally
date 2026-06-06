// Recent-alerts rendering for the codex_budget axis + the period-aware budget
// label (calendar-period-codex-budgets, spec §6).
//
// The sixth alert axis surfaces in the existing Recent-alerts panel + modal with
// a distinct "CODEX" chip and a period-aware context label ("Month of …" /
// "Calendar week of …"), read from the alert `period` / `period_start_at`
// context — NOT the hardcoded "Week of …". These tests seed codex_budget +
// calendar-period budget envelope items and assert the surfaces render the
// CODEX chip, the period noun (Month / Calendar week, not "Week"), the
// consumption-of-budget secondary, and the actual-spend Cost cell.
import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { RecentAlertsModal } from './RecentAlertsModal';
import { _resetForTests, dispatch } from '../store/store';
import type { AlertEntry } from '../types/envelope';
import type { AlertsConfig } from '../store/store';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 100],
};

function ingest(alerts: AlertEntry[]) {
  act(() => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts,
      alertsSettings: CONFIG,
      isFirstTick: true, // cold-start: no toast side effects
    });
  });
}

function codexBudgetEntry(partial: Partial<AlertEntry> = {}): AlertEntry {
  return {
    // period_start_at is the `+00:00` offset form the production code writes
    // (isoformat(timespec="seconds")), NOT a Z suffix.
    id: 'codex_budget:2026-06-01T00:00:00+00:00:100',
    axis: 'codex_budget',
    threshold: 100,
    severity: 'critical',
    crossed_at: '2026-06-15T12:00:00Z',
    alerted_at: '2026-06-15T12:00:00Z',
    context: {
      period: 'calendar-month',
      period_start_at: '2026-06-01T00:00:00+00:00',
      budget_usd: 200,
      spent_usd: 210,
      consumption_pct: 105,
    },
    ...partial,
  };
}

// A Claude budget alert over a CALENDAR period (the budget-axis period
// generalization): the same `budget` axis, but `period: calendar-month` must
// render "Month of …", not the legacy "Week of …".
function calendarBudgetEntry(partial: Partial<AlertEntry> = {}): AlertEntry {
  return {
    id: 'budget:2026-06-01T00:00:00+00:00:100',
    axis: 'budget',
    threshold: 100,
    severity: 'critical',
    crossed_at: '2026-06-15T12:00:00Z',
    alerted_at: '2026-06-15T12:00:00Z',
    context: {
      week_start_at: '2026-06-01T00:00:00+00:00',
      period: 'calendar-month',
      period_start_at: '2026-06-01T00:00:00+00:00',
      budget_usd: 300,
      spent_usd: 312,
      consumption_pct: 104,
    },
    ...partial,
  };
}

beforeEach(() => {
  _resetForTests();
});

describe('codex_budget axis in Recent alerts', () => {
  it('renders the CODEX chip in the panel', () => {
    ingest([codexBudgetEntry()]);
    render(<RecentAlertsPanel />);
    const chip = screen.getByText('CODEX');
    expect(chip.className).toContain('chip--codex_budget');
  });

  it('renders the CODEX chip + a Month period label in the modal', () => {
    ingest([codexBudgetEntry()]);
    render(<RecentAlertsModal />);
    // CODEX chip in the Axis column.
    expect(screen.getByText('CODEX')).toBeInTheDocument();
    // Context cell leads with the period-aware noun ("Month"), NOT "Week".
    const ctx = document.querySelector('.alert-context--codex_budget');
    expect(ctx).not.toBeNull();
    expect(ctx!.textContent).toContain('Month of');
    expect(ctx!.textContent).not.toContain('Week');
    expect(ctx!.textContent).toContain('105% of budget');
    // Cost column shows the Codex actual API spend (spent_usd, $210.00).
    expect(screen.getByText('$210.00')).toBeInTheDocument();
  });

  it('renders a Calendar week label for a calendar-week Codex budget', () => {
    ingest([
      codexBudgetEntry({
        context: {
          period: 'calendar-week',
          period_start_at: '2026-06-01T00:00:00+00:00',
          budget_usd: 50,
          spent_usd: 48,
          consumption_pct: 96,
        },
      }),
    ]);
    render(<RecentAlertsModal />);
    const ctx = document.querySelector('.alert-context--codex_budget');
    expect(ctx!.textContent).toContain('Calendar week of');
  });
});

describe('budget axis period generalization', () => {
  it('labels a calendar-month Claude budget as "Month of …", not "Week"', () => {
    ingest([calendarBudgetEntry()]);
    render(<RecentAlertsModal />);
    const ctx = document.querySelector('.alert-context--budget');
    expect(ctx).not.toBeNull();
    expect(ctx!.textContent).toContain('Month of');
    expect(ctx!.textContent).not.toContain('Week');
  });

  it('keeps the legacy "Week of …" label when period is subscription-week', () => {
    ingest([
      calendarBudgetEntry({
        context: {
          week_start_at: '2026-06-08T00:00:00+00:00',
          period: 'subscription-week',
          period_start_at: '2026-06-08T00:00:00+00:00',
          budget_usd: 300,
          spent_usd: 312,
          consumption_pct: 104,
        },
      }),
    ]);
    render(<RecentAlertsModal />);
    const ctx = document.querySelector('.alert-context--budget');
    expect(ctx!.textContent).toContain('Week of');
  });

  it('falls back to "Week of …" when period is absent (stale envelope)', () => {
    ingest([
      calendarBudgetEntry({
        context: {
          week_start_at: '2026-06-08T00:00:00+00:00',
          budget_usd: 300,
          spent_usd: 312,
          consumption_pct: 104,
        },
      }),
    ]);
    render(<RecentAlertsModal />);
    const ctx = document.querySelector('.alert-context--budget');
    expect(ctx!.textContent).toContain('Week of');
  });
});
