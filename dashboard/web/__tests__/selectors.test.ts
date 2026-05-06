import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { SessionRow, TrendRow, ForecastEnvelope, Envelope } from '../src/types/envelope';
import {
  sessionComparator,
  applySessionFilter,
  buildTrendSparkData,
  buildTrendHistoryData,
  buildRangeBarLayout,
} from '../src/store/selectors';
import {
  _resetForTests,
  dispatch,
  getRenderedRows,
  updateSnapshot,
} from '../src/store/store';

function row(partial: Partial<SessionRow>): SessionRow {
  return {
    session_id: 'x',
    started_utc: '2026-04-24T10:00:00Z',
    duration_min: 10,
    model: 'sonnet',
    project: 'repo',
    cost_usd: 1.0,
    ...partial,
  };
}

describe('sessionComparator', () => {
  it('started desc — newest first', () => {
    const a = row({ started_utc: '2026-04-24T10:00:00Z' });
    const b = row({ started_utc: '2026-04-24T11:00:00Z' });
    const out = [a, b].sort(sessionComparator('started desc'));
    expect(out[0]).toBe(b);
  });
  it('cost desc — highest first', () => {
    const a = row({ cost_usd: 1.0 });
    const b = row({ cost_usd: 5.0 });
    expect([a, b].sort(sessionComparator('cost desc'))[0]).toBe(b);
  });
  it('duration desc', () => {
    const a = row({ duration_min: 5 });
    const b = row({ duration_min: 50 });
    expect([a, b].sort(sessionComparator('duration desc'))[0]).toBe(b);
  });
  it('model asc — alphabetical', () => {
    const a = row({ model: 'sonnet' });
    const b = row({ model: 'opus' });
    expect([a, b].sort(sessionComparator('model asc'))[0].model).toBe('opus');
  });
  it('project asc — alphabetical', () => {
    const a = row({ project: 'zeta' });
    const b = row({ project: 'alpha' });
    expect([a, b].sort(sessionComparator('project asc'))[0].project).toBe('alpha');
  });
  it('default falls back to started desc', () => {
    const a = row({ started_utc: '2026-04-24T10:00:00Z' });
    const b = row({ started_utc: '2026-04-24T11:00:00Z' });
    expect([a, b].sort(sessionComparator('bogus'))[0]).toBe(b);
  });
});

describe('applySessionFilter', () => {
  const rows = [
    row({ session_id: '1', model: 'opus',   project: 'repo-foo' }),
    row({ session_id: '2', model: 'sonnet', project: 'repo-bar' }),
    row({ session_id: '3', model: 'haiku',  project: 'other' }),
    row({ session_id: '4', model: 'opus',   project: 'node runner' }),
  ];
  it('empty filter returns everything', () => {
    expect(applySessionFilter(rows, '')).toEqual(rows);
  });
  it('matches model substring', () => {
    const out = applySessionFilter(rows, 'opus');
    expect(out.map((r) => r.session_id).sort()).toEqual(['1', '4']);
  });
  it('matches project substring', () => {
    const out = applySessionFilter(rows, 'repo');
    expect(out.map((r) => r.session_id).sort()).toEqual(['1', '2']);
  });
  it('whitespace is a literal part of the needle (legacy substring semantics)', () => {
    // Legacy behavior: "node runner" matches the project "node runner"
    // exactly. It does NOT fall back to an OR over ["node", "runner"];
    // "node zeta" would no longer match "node runner".
    const out = applySessionFilter(rows, 'node runner');
    expect(out.map((r) => r.session_id)).toEqual(['4']);
    const outMismatch = applySessionFilter(rows, 'node zeta');
    expect(outMismatch).toEqual([]);
  });
  it('case-insensitive', () => {
    const out = applySessionFilter(rows, 'OPUS');
    expect(out.map((r) => r.session_id).sort()).toEqual(['1', '4']);
  });
});

describe('buildTrendSparkData / buildTrendHistoryData — CLAUDE.md gotcha', () => {
  const mk = (n: number): TrendRow[] =>
    Array.from({ length: n }, (_, i) => ({
      label: `W${i}`,
      used_pct: i * 5,
      dollar_per_pct: i * 0.5,
      delta: null,
      is_current: i === n - 1,
    }));
  it('buildTrendSparkData reads weeks[], length 8', () => {
    const env = { trend: { weeks: mk(8), history: mk(12), spark_heights: Array(8).fill(10) } };
    const data = buildTrendSparkData(env as any);
    expect(data.length).toBe(8);
  });
  it('buildTrendSparkData passes spark_heights parallel to weeks[]', () => {
    const env = { trend: { weeks: mk(8), history: mk(12), spark_heights: [1, 2, 3, 4, 5, 6, 7, 8] } };
    const data = buildTrendSparkData(env as any);
    expect(data.map((d) => d.spark_height)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });
  it('buildTrendHistoryData reads history[], length 12', () => {
    const env = { trend: { weeks: mk(8), history: mk(12), spark_heights: Array(8).fill(10) } };
    const data = buildTrendHistoryData(env as any);
    expect(data.length).toBe(12);
  });
  it('both return [] when trend is null', () => {
    expect(buildTrendSparkData({ trend: null } as any)).toEqual([]);
    expect(buildTrendHistoryData({ trend: null } as any)).toEqual([]);
  });
  it('both return [] when env is null', () => {
    expect(buildTrendSparkData(null)).toEqual([]);
    expect(buildTrendHistoryData(null)).toEqual([]);
  });

  it('warns when spark_heights length mismatches weeks length', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const weeks = Array.from({ length: 8 }, (_, i) => ({
      label: `W${i}`, used_pct: 0, dollar_per_pct: 0, delta: null, is_current: false,
    }));
    const env = { trend: { weeks, history: [], spark_heights: Array(7).fill(10) } };
    buildTrendSparkData(env as any);
    expect(spy).toHaveBeenCalledWith(expect.stringMatching(/spark_heights length 7/));
    spy.mockRestore();
  });

  it('does not warn when spark_heights matches weeks length', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const weeks = Array.from({ length: 8 }, (_, i) => ({
      label: `W${i}`, used_pct: 0, dollar_per_pct: 0, delta: null, is_current: false,
    }));
    const env = { trend: { weeks, history: [], spark_heights: Array(8).fill(10) } };
    buildTrendSparkData(env as any);
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});

describe('buildRangeBarLayout', () => {
  // Smoke-level: assert that given a simple forecast with distinct projections,
  // callouts appear at sensible x-positions. Overlap threshold details are
  // exercised by fixture goldens once the component exists.
  it('returns zones and callouts for valid forecast', () => {
    const fc: ForecastEnvelope = {
      verdict: 'cap',
      week_avg_projection_pct: 85,
      recent_24h_projection_pct: 115,
      budget_100_per_day_usd: null,
      budget_90_per_day_usd: null,
      confidence: 'high',
      confidence_score: 7,
      explain: null,
    };
    const layout = buildRangeBarLayout(fc, 400);
    expect(layout.zones.length).toBeGreaterThan(0);
    expect(layout.callouts.length).toBeGreaterThan(0);
    layout.callouts.forEach((c) => {
      expect(c.x).toBeGreaterThanOrEqual(0);
      expect(c.x).toBeLessThanOrEqual(400);
    });
  });

  it('returns empty callouts when projections are null', () => {
    const fc: ForecastEnvelope = {
      verdict: 'ok',
      week_avg_projection_pct: null,
      recent_24h_projection_pct: null,
      budget_100_per_day_usd: null,
      budget_90_per_day_usd: null,
      confidence: 'unknown',
      confidence_score: 0,
      explain: null,
    };
    expect(buildRangeBarLayout(fc, 400).callouts).toEqual([]);
  });

  it('suppresses lower-priority callout (recent-24h) when within 48px of week-avg', () => {
    // width=400, maxPct=100, so 1% = 4px. 48px threshold ≈ 12%.
    // Put wa=50, r24=55 → 5% apart = 20px → should suppress r24.
    const fc: ForecastEnvelope = {
      verdict: 'cap',
      week_avg_projection_pct: 50,
      recent_24h_projection_pct: 55,
      budget_100_per_day_usd: null,
      budget_90_per_day_usd: null,
      confidence: 'high',
      confidence_score: 7,
      explain: null,
    };
    const layout = buildRangeBarLayout(fc, 400);
    const wa = layout.callouts.find((c) => c.kind === 'week-avg');
    const r24 = layout.callouts.find((c) => c.kind === 'recent-24h');
    expect(wa?.visible).toBe(true);
    expect(r24?.visible).toBe(false);
  });

  it('keeps both callouts visible when separation exceeds threshold', () => {
    // width=400, maxPct=120 (from r24). 1% ≈ 3.33px. 48px ≈ 14.4%.
    // wa=30, r24=120 → 90% apart → both should remain visible.
    const fc: ForecastEnvelope = {
      verdict: 'cap',
      week_avg_projection_pct: 30,
      recent_24h_projection_pct: 120,
      budget_100_per_day_usd: null,
      budget_90_per_day_usd: null,
      confidence: 'high',
      confidence_score: 7,
      explain: null,
    };
    const layout = buildRangeBarLayout(fc, 400);
    expect(layout.callouts.every((c) => c.visible)).toBe(true);
  });

  it('returns empty when width <= 0', () => {
    const fc: ForecastEnvelope = {
      verdict: 'cap',
      week_avg_projection_pct: 50,
      recent_24h_projection_pct: 80,
      budget_100_per_day_usd: null,
      budget_90_per_day_usd: null,
      confidence: 'high',
      confidence_score: 7,
      explain: null,
    };
    expect(buildRangeBarLayout(fc, 0)).toEqual({ zones: [], callouts: [] });
  });

  it('returns empty when width is NaN (ResizeObserver race)', () => {
    const fc: ForecastEnvelope = {
      verdict: 'cap', week_avg_projection_pct: 85, recent_24h_projection_pct: 115,
      budget_100_per_day_usd: null, budget_90_per_day_usd: null,
      confidence: 'high', confidence_score: 7, explain: null,
    };
    expect(buildRangeBarLayout(fc, NaN)).toEqual({ zones: [], callouts: [] });
  });

  it('returns empty when width is Infinity', () => {
    const fc: ForecastEnvelope = {
      verdict: 'cap', week_avg_projection_pct: 85, recent_24h_projection_pct: 115,
      budget_100_per_day_usd: null, budget_90_per_day_usd: null,
      confidence: 'high', confidence_score: 7, explain: null,
    };
    expect(buildRangeBarLayout(fc, Infinity)).toEqual({ zones: [], callouts: [] });
  });
});

function snapWith(rows: SessionRow[]): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-28T00:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: null, used_pct: null, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: null,
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: rows.length, sort_key: 'started desc', rows },
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [] },
  };
}

describe('getRenderedRows with sessionsSortOverride', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  const rows: SessionRow[] = [
    { session_id: 'a', started_utc: '2026-04-27T10:00:00Z', duration_min: 5,
      model: 'opus', project: 'beta', cost_usd: 1.0 },
    { session_id: 'b', started_utc: '2026-04-26T10:00:00Z', duration_min: 5,
      model: 'sonnet', project: 'alpha', cost_usd: 9.0 },
    { session_id: 'c', started_utc: '2026-04-28T10:00:00Z', duration_min: 5,
      model: 'haiku', project: 'gamma', cost_usd: 5.0 },
  ];

  it('falls back to sessionComparator when no override is set', () => {
    updateSnapshot(snapWith(rows));
    // Default state.sessionsSort is 'started desc' — newest first
    expect(getRenderedRows().map((r) => r.session_id)).toEqual(['c', 'a', 'b']);
  });

  it('uses applyTableSort path when sessionsSortOverride is set', () => {
    updateSnapshot(snapWith(rows));
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'desc' },
    });
    expect(getRenderedRows().map((r) => r.session_id)).toEqual(['b', 'c', 'a']);
  });

  it('override overrides state.sessionsSort even when both are set', () => {
    updateSnapshot(snapWith(rows));
    dispatch({ type: 'SET_SORT', key: 'started desc' });
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'project', direction: 'asc' },
    });
    expect(getRenderedRows().map((r) => r.session_id)).toEqual(['b', 'a', 'c']);
  });
});
