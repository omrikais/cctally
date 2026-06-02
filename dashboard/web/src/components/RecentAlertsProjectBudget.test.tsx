// Recent-alerts rendering for the project_budget axis (issue #19/#121).
//
// The fifth alert axis surfaces in the existing Recent-alerts panel + modal
// with a distinct "PROJECT" chip and a context cell that leads with the
// project basename + consumption-of-budget. These tests seed a
// project_budget envelope item and assert both surfaces render the chip and
// the project context (basename + "% of budget" + spend in the Cost column).
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
  project_alerts_enabled: true,
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

function projectBudgetEntry(partial: Partial<AlertEntry> = {}): AlertEntry {
  return {
    id: 'project_budget:2026-04-13T00:00:00Z:/repos/foo:104',
    axis: 'project_budget',
    threshold: 100,
    severity: 'critical',
    crossed_at: '2026-04-16T12:00:00Z',
    alerted_at: '2026-04-16T12:00:00Z',
    context: {
      week_start_at: '2026-04-13T00:00:00Z',
      project: 'foo',
      project_key: '/repos/foo',
      budget_usd: 25,
      spent_usd: 26,
      consumption_pct: 104,
    },
    ...partial,
  };
}

beforeEach(() => {
  _resetForTests();
});

describe('project_budget axis in Recent alerts', () => {
  it('renders the PROJECT chip in the panel', () => {
    ingest([projectBudgetEntry()]);
    render(<RecentAlertsPanel />);
    const chip = screen.getByText('PROJECT');
    expect(chip.className).toContain('chip--project_budget');
  });

  it('renders the project basename + budget context in the modal', () => {
    ingest([projectBudgetEntry()]);
    render(<RecentAlertsModal />);
    // The PROJECT chip is present in the Axis column.
    expect(screen.getByText('PROJECT')).toBeInTheDocument();
    // Context cell leads with the project basename + consumption-of-budget.
    const ctx = document.querySelector('.alert-context--project_budget');
    expect(ctx).not.toBeNull();
    expect(ctx!.textContent).toContain('foo');
    expect(ctx!.textContent).toContain('104% of budget');
    // Cost column shows the project's actual spend (spent_usd, $26.00).
    expect(screen.getByText('$26.00')).toBeInTheDocument();
  });
});
