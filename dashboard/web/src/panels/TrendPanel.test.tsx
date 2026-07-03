import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { TrendPanel } from './TrendPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
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

// S3 (#264 · finding 3): the Cost column lives in TREND_COLUMNS but is
// MODAL-ONLY. The panel renders — and sorts by — a subset that omits it, so a
// stale/hand-edited `trendSortOverride.column==='cost_usd'` can't reorder the
// panel by a column it doesn't show.
function trendRowCost(label: string, cost: number, isCurrent = false): TrendRow {
  return { label, used_pct: 20, dollar_per_pct: 1.0, delta: null, is_current: isCurrent, cost_usd: cost };
}

function envWithCosts(): Envelope {
  const env = baseEnvelope();
  // Chronological weeks[] order wk1..wk4; costs are non-monotonic so a
  // cost-desc sort WOULD reorder them to wk3(4), wk1(3), wk4(2), wk2(1).
  const weeks = [
    trendRowCost('wk1', 3.0),
    trendRowCost('wk2', 1.0),
    trendRowCost('wk3', 4.0),
    trendRowCost('wk4', 2.0, true),
  ];
  env.trend = { weeks, spark_heights: weeks.map((_, i) => i + 1), history: weeks };
  return env;
}

describe('TrendPanel Cost column is modal-only (S3 #264 · finding 3)', () => {
  it('renders no Cost header — only Week · Used% · $/1% · Δ', () => {
    updateSnapshot(envWithCosts());
    render(<TrendPanel />);
    const cols = Array.from(
      document.querySelectorAll('#panel-trend table.trend-table thead th'),
    ).map((th) => th.getAttribute('data-col'));
    expect(cols).toEqual(['week', 'used_pct', 'dollar_per_pct', 'delta']);
    expect(cols).not.toContain('cost_usd');
  });

  it('a stale cost_usd sort override does NOT reorder the panel', () => {
    updateSnapshot(envWithCosts());
    // Persisted override points at a column the panel does not render.
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'trend',
      override: { column: 'cost_usd', direction: 'desc' },
    });
    render(<TrendPanel />);
    const labels = Array.from(
      document.querySelectorAll('#panel-trend table.trend-table tbody tr td:first-child'),
    ).map((td) => td.textContent);
    // Chronological weeks[] order preserved — NOT cost-desc (wk3, wk1, wk4, wk2).
    expect(labels).toEqual(['wk1', 'wk2', 'wk3', 'wk4']);
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
