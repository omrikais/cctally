import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { TrendPanel } from './TrendPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope, TrendRow } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

// Minimal envelope; only `trend` matters for the sparkline grid. Mirror
// the fuller factory in ProjectsPanel.test.tsx if more fields are needed.
function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
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
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function trendRow(i: number): TrendRow {
  return { label: `wk${i}`, used_pct: 10 + i, dollar_per_pct: 1 + i, delta: null, is_current: i === 0 };
}

function envWithTrend(weekCount: number): Envelope {
  const env = baseEnvelope();
  const weeks = Array.from({ length: weekCount }, (_, i) => trendRow(weekCount - i));
  env.trend = { weeks, spark_heights: weeks.map((_, i) => i + 1), history: weeks };
  return env;
}

describe('TrendPanel card week count (TR-1 / #251)', () => {
  it('renders the real week count in the card sub, never hardcoded "8 weeks"', () => {
    updateSnapshot(envWithTrend(6));
    render(<TrendPanel />);
    const sub = document.querySelector('#panel-trend .sub') as HTMLElement;
    expect(sub.textContent).toBe('(6 weeks)');
  });

  it('singularizes the count for a one-week stub', () => {
    updateSnapshot(envWithTrend(1));
    render(<TrendPanel />);
    const sub = document.querySelector('#panel-trend .sub') as HTMLElement;
    expect(sub.textContent).toBe('(1 week)');
  });
});

describe('TrendPanel sparkline track count (#207 C6)', () => {
  it('declares exactly one grid track per trend week (8)', () => {
    updateSnapshot(envWithTrend(8));
    render(<TrendPanel />);
    const spark = document.getElementById('trend-spark')!;
    expect(spark.style.gridTemplateColumns).toBe('repeat(8, 1fr)');
  });

  it('handles the single-week stub without an empty track', () => {
    updateSnapshot(envWithTrend(1));
    render(<TrendPanel />);
    const spark = document.getElementById('trend-spark')!;
    expect(spark.style.gridTemplateColumns).toBe('repeat(1, 1fr)');
  });
});
