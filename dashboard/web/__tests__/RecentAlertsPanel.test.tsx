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
};

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

  it('renders empty state when alerts: []', () => {
    render(<RecentAlertsPanel />);
    expect(
      screen.getByText(/No alerts yet/i),
    ).toBeInTheDocument();
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

  it('severity color: 90 → amber class, 95 → red class', () => {
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [mkAlert(1, 90), mkFiveHourAlert(2, 95)],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    render(<RecentAlertsPanel />);
    const cells = document.querySelectorAll('.alert-threshold');
    expect(cells.length).toBe(2);
    const classNames = Array.from(cells).map((c) => c.className);
    // First row is the 90-amber alert (alerts list ordered as supplied;
    // panel renders newest-first directly from store).
    expect(classNames[0]).toContain('amber');
    expect(classNames[1]).toContain('red');
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
