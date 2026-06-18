import { render } from '@testing-library/react';
import { describe, it, expect, beforeEach } from 'vitest';
import { Header } from './Header';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

function envWithDelta(d: number | null): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-20T12:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'Apr 14–21', used_pct: 17.4, five_hour_pct: 42,
      dollar_per_pct: 1.23, forecast_pct: 68.5, forecast_verdict: 'ok',
      vs_last_week_delta: d,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as unknown as Envelope;
}

describe('Header — vs last week (B1)', () => {
  beforeEach(() => { localStorage.clear(); _resetForTests(); });

  function statFor(d: number | null) {
    updateSnapshot(envWithDelta(d));
    const { container } = render(<Header />);
    return container.querySelector('[data-stat="vs-last-week"]') as HTMLElement | null;
  }

  it('renders nothing when the delta is null', () => {
    expect(statFor(null)).toBeNull();
  });

  it('cheaper (negative) → green + trending-down', () => {
    const el = statFor(-0.12)!;
    expect(el).not.toBeNull();
    expect(el.querySelector('use')?.getAttribute('href')).toContain('#trending-down');
    expect(el.querySelector('svg')?.getAttribute('style')).toContain('--accent-green');
    expect(el.textContent).toContain('vs last week');
    expect(el.getAttribute('aria-label')?.toLowerCase()).toContain('down');
  });

  it('costlier (positive) → red + trending-up', () => {
    const el = statFor(0.34)!;
    expect(el.querySelector('use')?.getAttribute('href')).toContain('#trending-up');
    expect(el.querySelector('svg')?.getAttribute('style')).toContain('--accent-red');
    expect(el.getAttribute('aria-label')?.toLowerCase()).toContain('up');
  });

  it('flat (|Δ| < 0.02) → dim + minus', () => {
    const el = statFor(0.01)!;
    expect(el.querySelector('use')?.getAttribute('href')).toContain('#minus');
    expect(el.querySelector('svg')?.getAttribute('style')).toContain('--text-dim');
    expect(el.getAttribute('aria-label')?.toLowerCase()).toContain('flat');
  });
});
