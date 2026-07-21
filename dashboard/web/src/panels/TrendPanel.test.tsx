import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { TrendPanel } from './TrendPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
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

// #294 S5 — the source seam: TrendPanel must not leak the legacy top-level
// `env.trend` (Claude $/1% data) under a Codex selection. The panel is wrapped
// in SourcePanelShell — Claude renders unchanged, Codex renders nothing, All
// renders the Claude-labeled provider section.
function trendLeakEnv(): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = makeCodexSourceEntry();
  const slice = makeSourceEnvelope({
    sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
  });
  // A populated legacy top-level trend (the exact leak surface from the QA).
  const weeks = Array.from({ length: 3 }, (_, i) => trendRow(3 - i));
  return {
    ...baseEnvelope(),
    ...slice,
    trend: { weeks, spark_heights: weeks.map((_, i) => i + 1), history: weeks },
  } as unknown as Envelope;
}

describe('TrendPanel source seam — no Claude leak under Codex (#294 S5)', () => {
  it('Codex mode renders the shared trend visualization with Codex weekly cost', () => {
    updateSnapshot(trendLeakEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<TrendPanel />);
    expect(container.querySelector('#panel-trend[data-source="codex"]')).not.toBeNull();
    expect(container.querySelector('.trend-chart')).not.toBeNull();
    expect(container.querySelectorAll('.trend-table tbody tr').length).toBeGreaterThan(0);
  });

  it('Codex mode aligns Used%, $/1%, and vs-prior values with all four headers', () => {
    const env = trendLeakEnv();
    const template = env.sources!.codex.data!.periods.weekly.rows[0];
    env.sources!.codex.data!.periods.weekly.rows = [
      {
        ...template,
        label: '07-18 06:24',
        cost_usd: 639.31,
        used_pct: 30,
        dollar_per_pct: 21.31,
      },
      {
        ...template,
        label: '07-16 07:16',
        cost_usd: 418.35,
        used_pct: 23,
        dollar_per_pct: 18.19,
      },
    ];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<TrendPanel />);

    const headers = Array.from(container.querySelectorAll('.trend-table thead th'))
      .map((cell) => cell.getAttribute('data-col'));
    const cells = Array.from(container.querySelectorAll('.trend-table tbody tr:last-child td'))
      .map((cell) => cell.textContent);
    expect(headers).toEqual(['week', 'used_pct', 'dollar_per_pct', 'delta']);
    expect(cells).toEqual(['07-18 06:24', '30%', '$21.31', '+3.12 ↑']);
  });

  it('All mode renders provider-separated trend sections, never one combined series', () => {
    updateSnapshot(trendLeakEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<TrendPanel />);
    expect(container.querySelectorAll('#panel-trend')).toHaveLength(1);
    expect(container.querySelector('#panel-trend[data-source="all"]')).not.toBeNull();
    const sections = container.querySelectorAll('[data-provider-section]');
    expect(sections).toHaveLength(2);
    expect(Array.from(sections).map((section) => section.getAttribute('data-provider-section')))
      .toEqual(['claude', 'codex']);
    expect(sections[0].textContent).toContain('Claude');
    expect(sections[1].textContent).toContain('Codex');
    expect(sections[0].querySelectorAll('.trend-spark')).toHaveLength(1);
    expect(sections[1].querySelectorAll('.trend-spark')).toHaveLength(1);
    expect(Array.from(container.querySelectorAll('.trend-table tbody')).map((body) => body.id))
      .toEqual(['trend-rows-claude', 'trend-rows-codex']);
  });

  it('Claude mode still renders the trend table through the wrap (transparent)', () => {
    updateSnapshot(trendLeakEnv());
    render(<TrendPanel />);
    expect(document.querySelector('#panel-trend')).not.toBeNull();
    expect(document.querySelectorAll('.trend-table tbody tr').length).toBe(3);
  });
});

describe('TrendPanel layout — chart above scrollable table (#265 B)', () => {
  it('renders the chart block before the scrollable table wrapper', () => {
    updateSnapshot(envWithTrend(3));
    const { container } = render(<TrendPanel />);
    const body = container.querySelector('#panel-trend .panel-body') as HTMLElement;
    const chart = body.querySelector('.trend-chart');
    const tableWrap = body.querySelector('.trend-table-wrap');
    expect(chart).not.toBeNull();
    expect(tableWrap).not.toBeNull();
    expect(tableWrap!.querySelector('table.trend-table')).not.toBeNull();
    // chart precedes the table wrapper in document order
    expect(
      chart!.compareDocumentPosition(tableWrap!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});
