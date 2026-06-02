// RecentAlertsPanel — severity consumption (Task F).
//
// The panel must render the color class from `alert.severity` (the kernel's
// single authority) and only fall back to threshold derivation when the
// field is absent. The smoking-gun case feeds a severity that DISAGREES with
// what the threshold would derive, proving the panel consumes rather than
// recomputes.
import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { _resetForTests, dispatch } from '../store/store';
import type { AlertEntry } from '../types/envelope';
import type { AlertsConfig } from '../store/store';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 95],
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

function entry(partial: Partial<AlertEntry>): AlertEntry {
  return {
    id: 'weekly:2026-04-13:90:0',
    axis: 'weekly',
    threshold: 90,
    crossed_at: '2026-04-16T12:00:00Z',
    alerted_at: '2026-04-16T12:00:00Z',
    context: { week_start_date: '2026-04-13' },
    ...partial,
  };
}

beforeEach(() => {
  _resetForTests();
});

describe('RecentAlertsPanel severity', () => {
  it('renders the class from alert.severity, not recomputed from threshold', () => {
    // severity:'red' but threshold 50 ⇒ if the panel recomputed it would be
    // 'amber'. Consuming severity ⇒ red class present, amber absent.
    ingest([entry({ id: 'x:1', severity: 'red', threshold: 50 })]);
    render(<RecentAlertsPanel />);
    const cell = screen.getByText('50%');
    expect(cell.className).toContain('severity-red');
    expect(cell.className).not.toContain('severity-amber');
  });

  it('falls back to threshold derivation when severity is absent', () => {
    ingest([entry({ id: 'y:1', threshold: 95 })]); // no severity field
    render(<RecentAlertsPanel />);
    const cell = screen.getByText('95%');
    expect(cell.className).toContain('severity-red');
  });
});
