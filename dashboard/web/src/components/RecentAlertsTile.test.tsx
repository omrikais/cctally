// #248 Task 6 — Recent Alerts compact tile (C7).
//
// Empty state collapses from a 540x340 box with ~200px of dead air to a single
// glanceable line: "✓ No alerts · You're at <used%>. Alerts fire at 90% / 95%."
// (used% read from header.used_pct). Populated state keeps the top-N rows + the
// "N of M shown" foot, the collapse chevron, and open-on-click.
import { act, render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import type { AlertEntry, Envelope } from '../types/envelope';
import type { AlertsConfig } from '../store/store';

const CONFIG: AlertsConfig = {
  enabled: true,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 95],
};

function env(usedPct: number): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: usedPct, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function entry(i: number): AlertEntry {
  return {
    id: `weekly:2026-06-${10 + i}:90:0`,
    axis: 'weekly',
    threshold: 90,
    crossed_at: '2026-06-16T12:00:00Z',
    alerted_at: '2026-06-16T12:00:00Z',
    context: { week_start_date: '2026-06-13' },
  };
}

function ingest(alerts: AlertEntry[]) {
  act(() => {
    dispatch({ type: 'INGEST_SNAPSHOT_ALERTS', alerts, alertsSettings: CONFIG, isFirstTick: true });
  });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('#248 Task 6 — Recent Alerts compact tile', () => {
  it('empty → one compact line with used% + fire thresholds (no tall .alerts-empty box)', () => {
    updateSnapshot(env(11));
    const { container } = render(<RecentAlertsPanel />);
    const tile = container.querySelector('.alerts-empty-tile');
    expect(tile, 'expected the compact .alerts-empty-tile').not.toBeNull();
    const text = tile?.textContent ?? '';
    expect(text).toContain('No alerts');
    expect(text).toContain('11.0%');
    expect(text).toContain('90% / 95%');
    // The old 32px-padded `.alerts-empty` box is retired.
    expect(container.querySelector('.alerts-empty')).toBeNull();
  });

  it('populated → top-N rows + "N of M shown" foot', () => {
    const alerts = Array.from({ length: 12 }, (_, i) => entry(i));
    ingest(alerts);
    updateSnapshot(env(96));
    const { container } = render(<RecentAlertsPanel />);
    const rows = container.querySelectorAll('.alert-row');
    expect(rows.length).toBeLessThanOrEqual(10);
    expect(container.querySelector('.alerts-foot')?.textContent).toContain('10 of 12 shown');
  });

  it('keeps the collapse chevron and open-on-click', () => {
    updateSnapshot(env(11));
    const { container } = render(<RecentAlertsPanel />);
    expect(container.querySelector('.panel-collapse-toggle')).not.toBeNull();
    const panel = container.querySelector('[data-panel-kind="alerts"]') as HTMLElement;
    expect(panel).not.toBeNull();
  });
});
