import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RecentAlertsPanel } from '../src/components/RecentAlertsPanel';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../src/store/store';
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
};

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
      spent_usd: 270 + idx,
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
      primary_model: 'claude-sonnet-4-6',
    },
  };
}

describe('<RecentAlertsPanel />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    // Snapshot is required so the panel mounts in a "ready" state and
    // tz formatters have a display block to read.
    updateSnapshot(fixture as unknown as Envelope);
  });

  it('renders the compact empty tile when alerts: [] (#248 §5)', () => {
    const { container } = render(<RecentAlertsPanel />);
    const tile = container.querySelector('.alerts-empty-tile');
    expect(tile).not.toBeNull();
    // One-liner: "✓ No alerts · You're at <used%>. Alerts fire at 90% / 95%."
    expect(tile?.textContent).toMatch(/No alerts/i);
    expect(tile?.textContent).toMatch(/90% \/ 95%/);
  });

  it('renders up to 10 most-recent alerts (slices a 15-alert store)', () => {
    const alerts = Array.from({ length: 15 }, (_, i) => mkAlert(i));
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts,
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsPanel />);
    const rows = document.querySelectorAll('.alert-row');
    expect(rows.length).toBe(10);
    // Empty state must not render alongside rows.
    expect(screen.queryByText(/No alerts yet/i)).toBeNull();
  });

  it('severity tier: 90/95 → warn class, 100 → critical class', () => {
    // Phase B 3-tier bands: 90-99 ⇒ warn, >=100 ⇒ critical. (95 is no longer
    // a distinct "red" tier — it sits in the warn band alongside 90.) The tier
    // class is axis-agnostic (weekly / five_hour / budget all go through
    // alertSeverity), so exercise all three axes here.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkAlert(1, 90), mkFiveHourAlert(2, 95), mkBudgetAlert(3, 100)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsPanel />);
    const cells = document.querySelectorAll('.alert-threshold');
    expect(cells.length).toBe(3);
    const classNames = Array.from(cells).map((c) => c.className);
    // List ordered as supplied; panel renders newest-first directly from store.
    expect(classNames[0]).toContain('warn'); // 90 → warn
    expect(classNames[1]).toContain('warn'); // 95 → warn (no longer red)
    expect(classNames[1]).not.toContain('critical');
    expect(classNames[2]).toContain('critical'); // 100 → critical
  });

  it('renders a BUDGET chip for budget-axis alerts (issue #19)', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkBudgetAlert(1, 90)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsPanel />);
    const chip = document.querySelector('.chip--budget');
    expect(chip).not.toBeNull();
    expect(chip).toHaveTextContent('BUDGET');
    expect(screen.queryByText(/No alerts yet/i)).toBeNull();
  });

  it('chevron toggle dispatches SAVE_PREFS to flip alertsCollapsed', async () => {
    render(<RecentAlertsPanel />);
    const user = userEvent.setup();
    expect(getState().prefs.alertsCollapsed).toBe(false);
    const toggle = document.querySelector(
      '.panel-collapse-toggle',
    ) as HTMLButtonElement;
    expect(toggle).toBeTruthy();
    await user.click(toggle);
    expect(getState().prefs.alertsCollapsed).toBe(true);
  });

  it('panel click dispatches openAction (OPEN_MODAL alerts)', async () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkAlert(1, 90)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsPanel />);
    const user = userEvent.setup();
    const panel = document.querySelector(
      '[data-panel-kind="alerts"]',
    ) as HTMLElement;
    expect(panel).toBeTruthy();
    await user.click(panel);
    expect(getState().openModal).toBe('alerts');
  });

  it('chevron click does NOT bubble up to open the modal', async () => {
    render(<RecentAlertsPanel />);
    const user = userEvent.setup();
    const toggle = document.querySelector(
      '.panel-collapse-toggle',
    ) as HTMLButtonElement;
    await user.click(toggle);
    // After clicking the chevron the modal must remain closed —
    // stopPropagation in the chevron handler is the contract.
    expect(getState().openModal).toBeNull();
  });
});
