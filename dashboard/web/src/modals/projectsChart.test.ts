import { describe, it, expect } from 'vitest';
import { bucketRankedProjects, isDominant } from './projectsChart';
import type { ProjectsTrendEnvelope } from '../types/envelope';

function trend(costs: Record<string, number[]>): ProjectsTrendEnvelope {
  const nweeks = Object.values(costs)[0]?.length ?? 0;
  return {
    window_weeks: nweeks,
    weeks: Array.from({ length: nweeks }, (_, i) => ({
      week_start_date: `2026-06-${String(i + 1).padStart(2, '0')}`,
      week_label: `W${i}`, total_cost_usd: 0, total_pct: null,
    })),
    projects: Object.entries(costs).map(([key, weekly_cost]) => ({
      key, bucket_path: `/repos/${key}`, weekly_cost,
      weekly_pct: weekly_cost.map(() => null),
      sessions_per_week: weekly_cost.map(() => 1),
      first_seen_per_week: weekly_cost.map(() => null),
      last_seen_per_week: weekly_cost.map(() => null),
    })),
  };
}

describe('bucketRankedProjects', () => {
  it('keeps top-5 by cost and rolls the rest into (other)', () => {
    const t = trend({
      a: [10], b: [8], c: [6], d: [4], e: [2], f: [1], g: [1],
    });
    const { series } = bucketRankedProjects(t, 1, 5);
    const keys = series.map((s) => s.key);
    expect(keys.slice(0, 5)).toEqual(['a', 'b', 'c', 'd', 'e']);
    expect(keys[keys.length - 1]).toBe('(other)');
    expect(series.find((s) => s.key === '(other)')!.cost).toBe(2); // f+g
  });
});

describe('isDominant', () => {
  it('is true when the top real project >= 60% of total', () => {
    expect(isDominant(trend({ a: [92], b: [3], c: [5] }), 1)).toBe(true);
  });
  it('is false at 59% (boundary)', () => {
    expect(isDominant(trend({ a: [59], b: [41] }), 1)).toBe(false);
  });
  it('is true at exactly 60% (boundary)', () => {
    expect(isDominant(trend({ a: [60], b: [40] }), 1)).toBe(true);
  });
  it('ignores the (other) rollup — a fat tail does NOT trigger dominance', () => {
    // 7 equal small projects: top real share ~14%, but (other) rollup is large.
    const t = trend({ a: [15], b: [14], c: [14], d: [14], e: [14], f: [14], g: [15] });
    expect(isDominant(t, 1)).toBe(false);
  });
  it('is false when total is zero', () => {
    expect(isDominant(trend({ a: [0], b: [0] }), 1)).toBe(false);
  });
});
