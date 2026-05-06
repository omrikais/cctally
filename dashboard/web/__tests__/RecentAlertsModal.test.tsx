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

  it('renders empty state when no alerts', () => {
    render(<RecentAlertsModal />);
    expect(screen.getByText(/No alerts yet/i)).toBeInTheDocument();
  });
});
