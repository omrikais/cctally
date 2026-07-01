// Projects trend bucketing + dominance detection (PR-2, #250).
//
// `bucketRankedProjects` is the window-slice + top-5-by-cost + `(other)`
// rollup logic lifted verbatim out of ProjectsTrendChart's useMemo so
// BOTH render modes (stacked area + ranked bars) share one bucketing.
// `isDominant` decides which mode to render — measured over REAL
// projects only, never the synthetic `(other)` rollup.
import type { ProjectsTrendEnvelope, ProjectsTrendWeek } from '../types/envelope';

export const TOP_N = 5;
export const DOMINANCE_THRESHOLD = 0.60;
export const OTHER_KEY = '(other)';

// Series palette — top-5 take SERIES_COLORS by descending rank, `(other)`
// is a muted slate. Single source of truth shared by the stacked-area
// chart, the ranked bars, and the legend so a palette edit can never
// desync a bar from its own legend swatch within one modal.
export const SERIES_COLORS = ['#d946ef', '#c084fc', '#60a5fa', '#fbbf24', '#22d3ee'];
export const OTHER_COLOR = '#64748b';

// Series color by rank position (top-5 by SERIES_COLORS, `(other)` slate).
export const colorFor = (key: string, i: number): string =>
  key === OTHER_KEY ? OTHER_COLOR : (SERIES_COLORS[i] ?? OTHER_COLOR);

// Basename of a project's canonical bucket_path (PR-3) — the mobile legend
// truncates the disambiguated display `key` mid-word, so we label with the
// basename and carry the full path on the `title` attr.
export const basenameOf = (bucketPath: string): string =>
  bucketPath === OTHER_KEY ? OTHER_KEY : (bucketPath.split('/').filter(Boolean).pop() ?? bucketPath);

export interface PreparedSeries {
  key: string;
  bucket_path: string;
  weekly: number[];
  cost: number;
}

export interface BucketedProjects {
  weeks: ProjectsTrendWeek[];
  series: PreparedSeries[]; // top-N by cost desc, then (other) if any tail cost
}

export function bucketRankedProjects(
  trend: ProjectsTrendEnvelope,
  windowWeeks: number,
  topN: number = TOP_N,
): BucketedProjects {
  const n = Math.max(0, Math.min(windowWeeks, trend.window_weeks));
  const weeks = trend.weeks.slice(-n);
  const projWindow: PreparedSeries[] = trend.projects
    .map((p) => {
      const weekly = p.weekly_cost.slice(-n);
      const cost = weekly.reduce((s, c) => s + c, 0);
      return { key: p.key, bucket_path: p.bucket_path, weekly, cost };
    })
    .sort((a, b) => b.cost - a.cost);
  const top = projWindow.slice(0, topN);
  const tail = projWindow.slice(topN);
  const otherWeekly = weeks.map((_, j) => tail.reduce((s, p) => s + (p.weekly[j] ?? 0), 0));
  const otherCost = otherWeekly.reduce((s, c) => s + c, 0);
  const series: PreparedSeries[] = [...top];
  if (tail.length > 0 && otherCost > 0) {
    series.push({ key: OTHER_KEY, bucket_path: OTHER_KEY, weekly: otherWeekly, cost: otherCost });
  }
  return { weeks, series };
}

// Dominance is measured over REAL projects only (never the (other)
// rollup), so a fat tail cannot false-trigger the ranked-bar mode.
export function isDominant(
  trend: ProjectsTrendEnvelope,
  windowWeeks: number,
  threshold: number = DOMINANCE_THRESHOLD,
): boolean {
  const n = Math.max(0, Math.min(windowWeeks, trend.window_weeks));
  const costs = trend.projects.map((p) => p.weekly_cost.slice(-n).reduce((s, c) => s + c, 0));
  const total = costs.reduce((s, c) => s + c, 0);
  if (total <= 0) return false;
  return Math.max(...costs) / total >= threshold;
}
