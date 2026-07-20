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
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import type { AlertEntry, Envelope } from '../types/envelope';
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

function emptyEnv(usedPct: number | null): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: usedPct, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [90, 95], five_hour_thresholds: [90, 95], budget_thresholds: [90, 95] },
  };
}

beforeEach(() => {
  _resetForTests();
});

describe('RecentAlertsPanel severity', () => {
  it('renders the class from alert.severity, not recomputed from threshold', () => {
    // severity:'critical' but threshold 50 ⇒ if the panel recomputed it would
    // be 'info'. Consuming severity ⇒ critical class present, info absent.
    ingest([entry({ id: 'x:1', severity: 'critical', threshold: 50 })]);
    render(<RecentAlertsPanel />);
    const cell = screen.getByText('50%');
    expect(cell.className).toContain('severity-critical');
    expect(cell.className).not.toContain('severity-info');
  });

  it('falls back to threshold bands when severity is absent', () => {
    // threshold 90 ⇒ warn (no severity field).
    ingest([entry({ id: 'y:1', threshold: 90 })]);
    render(<RecentAlertsPanel />);
    const cell = screen.getByText('90%');
    expect(cell.className).toContain('severity-warn');
    // threshold 100 ⇒ critical.
    _resetForTests();
    ingest([entry({ id: 'z:1', threshold: 100 })]);
    render(<RecentAlertsPanel />);
    const critCell = screen.getByText('100%');
    expect(critCell.className).toContain('severity-critical');
  });
});

// #264 S1 (VOID-1) — the empty Alerts tile gains a teaching gauge below the
// one-liner (mirrors the RecentAlertsModal empty-state gauge vocabulary): a
// fill at the current used%, with a tick per configured weekly fire threshold,
// so "you're at 42%, alerts fire at 90/95" is SHOWN, not just told.
describe('RecentAlertsPanel empty-state teaching gauge (#264 S1)', () => {
  function seed(usedPct: number | null) {
    act(() => {
      updateSnapshot(emptyEnv(usedPct));
      dispatch({
        type: 'INGEST_SNAPSHOT_ALERTS',
        alerts: [],
        alertsSettings: CONFIG,
        isFirstTick: true,
      });
    });
  }

  it('renders a gauge filled to used_pct with one tick per weekly threshold', () => {
    seed(42);
    const { container } = render(<RecentAlertsPanel />);
    const fill = container.querySelector('.ra-gauge-fill') as HTMLElement;
    expect(fill).not.toBeNull();
    expect(fill.style.width).toBe('42%');
    const ticks = container.querySelectorAll('.ra-gauge-tick');
    expect(Array.from(ticks).map((t) => (t as HTMLElement).style.left)).toEqual([
      '90%',
      '95%',
    ]);
  });

  it('keeps the canonical empty-state gauge when used_pct is unknown', () => {
    seed(null);
    const { container } = render(<RecentAlertsPanel />);
    expect((container.querySelector('.ra-gauge-fill') as HTMLElement).style.width).toBe('0%');
    expect(container.querySelector('.ra-gauge-hero')?.textContent).toBe('—');
    expect(container.querySelector('.panel-empty')).toBeNull();
    expect(container.textContent).toContain('No alerts yet');
  });
});
