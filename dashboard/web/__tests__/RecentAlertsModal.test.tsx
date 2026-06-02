import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RecentAlertsModal } from '../src/components/RecentAlertsModal';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../src/store/store';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  uninstallGlobalKeydown,
} from '../src/store/keymap';
import type { AlertEntry, Envelope } from '../src/types/envelope';
import fixture from './fixtures/envelope.json';

function mkAlert(idx: number, threshold: 90 | 95 = 90): AlertEntry {
  return {
    id: `weekly:2026-04-21:${idx}-${threshold}`,
    axis: 'weekly',
    threshold,
    crossed_at: `2026-04-23T${String(10 + (idx % 10)).padStart(2, '0')}:00:00Z`,
    alerted_at: `2026-04-23T${String(10 + (idx % 10)).padStart(2, '0')}:05:00Z`,
    context: {
      week_start_date: '2026-04-21',
      cumulative_cost_usd: 12.34 + idx,
      dollars_per_percent: 0.53,
    },
  };
}

const DEFAULT_ALERTS_SETTINGS = {
  enabled: false,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 100],
  budget_enabled: false,
  projected_weekly_enabled: false,
  projected_budget_enabled: false,
};

function mkProjectedWeeklyAlert(threshold: 90 | 100 = 100): AlertEntry {
  return {
    id: `projected:2026-04-21T00:00:00Z:weekly_pct:${threshold}`,
    axis: 'projected',
    metric: 'weekly_pct',
    threshold,
    crossed_at: '2026-04-23T13:00:00Z',
    alerted_at: '2026-04-23T13:00:00Z',
    context: {
      week_start_at: '2026-04-21T00:00:00Z',
      metric: 'weekly_pct',
      projected_value: 102,
      denominator: 100,
    },
  };
}

function mkProjectedBudgetAlert(threshold: 90 | 100 = 100): AlertEntry {
  return {
    id: `projected:2026-04-21T00:00:00Z:budget_usd:${threshold}`,
    axis: 'projected',
    metric: 'budget_usd',
    threshold,
    crossed_at: '2026-04-23T14:00:00Z',
    alerted_at: '2026-04-23T14:00:00Z',
    context: {
      week_start_at: '2026-04-21T00:00:00Z',
      metric: 'budget_usd',
      projected_value: 312,
      denominator: 300,
    },
  };
}

function mkBudgetAlert(idx: number, threshold: 90 | 100 = 90): AlertEntry {
  return {
    id: `budget:2026-04-21T00:00:00Z:${idx}-${threshold}`,
    axis: 'budget',
    threshold,
    crossed_at: `2026-04-23T12:00:00Z`,
    alerted_at: `2026-04-23T12:0${idx % 10}:00Z`,
    context: {
      week_start_at: '2026-04-21T00:00:00Z',
      budget_usd: 300,
      spent_usd: 270.5,
      consumption_pct: threshold,
    },
  };
}

function mkFiveHourAlert(idx: number, threshold: 90 | 95 = 95): AlertEntry {
  return {
    id: `five_hour:1714459200:${idx}-${threshold}`,
    axis: 'five_hour',
    threshold,
    crossed_at: `2026-04-30T15:30:00Z`,
    alerted_at: `2026-04-30T15:30:00Z`,
    context: {
      five_hour_window_key: 1714459200 + idx,
      block_start_at: '2026-04-30T14:30:00Z',
      block_cost_usd: 8.12,
      // Note: T5 envelope rebuild does NOT carry primary_model for
      // 5h alerts. Only the live-dispatch payload (T4) has it.
      // The modal must NOT reference it to avoid a phantom column.
    },
  };
}

describe('<RecentAlertsModal />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
  });

  it('renders all alerts up to 100 in a sortable table', () => {
    // Seed 120 alerts; the modal must cap at 100. (The store can hold
    // more than 100 in transit — the cap is the modal's UI policy.)
    const alerts = Array.from({ length: 120 }, (_, i) => mkAlert(i));
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts,
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const rows = document.querySelectorAll('tbody tr');
    expect(rows.length).toBe(100);
  });

  it('table columns: %, axis, cost, context, alerted', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkAlert(1, 90), mkFiveHourAlert(2, 95)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const headers = Array.from(document.querySelectorAll('thead th')).map(
      (th) => th.textContent?.trim().toLowerCase() ?? '',
    );
    expect(headers).toEqual(['%', 'axis', 'cost', 'context', 'alerted']);
  });

  it('ESC closes modal via existing modal pattern', async () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkAlert(1, 90)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    expect(getState().openModal).toBe('alerts');
    const user = userEvent.setup();
    await user.keyboard('{Escape}');
    expect(getState().openModal).toBeNull();
    uninstallGlobalKeydown();
  });

  it('5h alert context does NOT reference primary_model', () => {
    // Defensive: even when a five_hour alert lands without primary_model
    // (the envelope rebuild path strips it), the modal renders the row
    // with "Block <time>" and never the literal text "primary_model"
    // or a model name we didn't supply.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkFiveHourAlert(2, 95)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const tbody = document.querySelector('tbody')!;
    expect(tbody.textContent ?? '').not.toMatch(/primary_model/);
    // 5h-block context cell must contain "Block" and a time fragment.
    expect(tbody.textContent ?? '').toMatch(/Block/);
  });

  it('weekly alert context includes week-of date and $/1% when present', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkAlert(1, 90)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const tbody = document.querySelector('tbody')!;
    expect(tbody.textContent ?? '').toMatch(/Week of/);
    expect(tbody.textContent ?? '').toMatch(/\$0\.53\/1%/);
  });

  it('renders a budget-axis row end-to-end: BUDGET chip + spend cost + context (issue #19)', () => {
    // Parent-modal integration test (per the *modal-level integration
    // test* memory): drives a budget alert through the full
    // RecentAlertsModal render — chip label, CostCell (→ spent_usd), and
    // ContextCell ("Week of …" + "% of budget").
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkBudgetAlert(1, 90)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const row = document.querySelector('tbody tr')!;
    // Axis chip.
    const chip = row.querySelector('.chip--budget');
    expect(chip).not.toBeNull();
    expect(chip).toHaveTextContent('BUDGET');
    // Cost column shows actual spend ($270.50), NOT the budget total.
    expect(row.textContent ?? '').toMatch(/\$270\.50/);
    // Context column: week-of + consumption-of-budget.
    const context = row.querySelector('.alert-context--budget');
    expect(context).not.toBeNull();
    expect(context!.textContent ?? '').toMatch(/Week of/);
    expect(context!.textContent ?? '').toMatch(/90% of budget/);
  });

  it('renders a projected weekly_pct row end-to-end: PROJECTED chip + "projected …% of cap" (issue #121)', () => {
    // Modal-level integration: drives a projected weekly_pct alert through
    // the full RecentAlertsModal render — chip label + metric-aware
    // ContextCell ("projected 102% of cap").
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkProjectedWeeklyAlert(100)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const row = document.querySelector('tbody tr')!;
    const chip = row.querySelector('.chip--projected');
    expect(chip).not.toBeNull();
    expect(chip).toHaveTextContent('PROJECTED');
    const context = row.querySelector('.alert-context--projected');
    expect(context).not.toBeNull();
    expect(context!.textContent ?? '').toMatch(/projected 102% of cap/);
  });

  it('renders a projected budget_usd row: "projected $312 of $300" (issue #121)', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkProjectedBudgetAlert(100)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const row = document.querySelector('tbody tr')!;
    expect(row.querySelector('.chip--projected')).toHaveTextContent('PROJECTED');
    const context = row.querySelector('.alert-context--projected');
    expect(context!.textContent ?? '').toMatch(/projected \$312 of \$300/);
  });

  it('renders empty state when no alerts', () => {
    render(<RecentAlertsModal />);
    expect(screen.getByText(/No alerts yet/i)).toBeInTheDocument();
  });

  // Phase B 3-tier severity: the % cell class is keyed off `alertSeverity`,
  // which consumes the kernel's `info|warn|critical` token. Modal-level
  // integration (per the *modal-level integration test* convention): drive a
  // critical and a warn alert through the full modal render and assert the
  // parent wiring lands the right tier class — not just the helper.
  it('applies the critical tier class to a critical alert', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      // severity:'critical' on a threshold (90) that would otherwise derive
      // 'warn' — proves consumption, not recompute.
      alerts: [{ ...mkAlert(1, 90), severity: 'critical' }],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const cell = document.querySelector('td.alert-threshold')!;
    expect(cell.className).toContain('severity-critical');
    expect(cell.className).toContain('critical');
    expect(cell.className).not.toContain('severity-warn');
  });

  it('applies the warn tier class to a warn alert', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [{ ...mkAlert(1, 90), severity: 'warn' }],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsModal />);
    const cell = document.querySelector('td.alert-threshold')!;
    expect(cell.className).toContain('severity-warn');
    expect(cell.className).toContain('warn');
    expect(cell.className).not.toContain('severity-critical');
  });
});
