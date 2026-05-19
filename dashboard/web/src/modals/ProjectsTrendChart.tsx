// ProjectsTrendChart — stacked-area SVG chart for the ProjectsModal
// (spec §3.3, plan Task 5 Step 4).
//
// Renders the top-5 projects by sum-of-cost across the selected window
// + an `(other)` rollup band for everything beyond rank 5. Color map is
// stable per render: top-5 take `SERIES_COLORS` by descending rank,
// `(other)` is a muted slate. Reassigned when the window changes
// (spec §3.3).
//
// Y-axis modes (spec §3.3):
//   - 'absolute' — bands sum to weekly $ total (raw cost on Y).
//   - 'share'    — bands sum to 100 (% share of week's cost).
//
// Click a colored band → `onProjectSelect(key)` (caller selects the
// project in the table + expands the drill). Clicking the `(other)`
// band is a no-op (no synthetic drill key).
//
// Vanilla SVG, no chart library — per project memory "Stdlib-only
// ethos" (no new deps for the web client; vanilla `<svg>` is the
// established pattern).
import { useMemo } from 'react';
import type { ProjectsTrendEnvelope } from '../types/envelope';

const SERIES_COLORS = ['#d946ef', '#c084fc', '#60a5fa', '#fbbf24', '#22d3ee'];
const OTHER_COLOR = '#64748b';
const TOP_N = 5;
const VW = 400;
const VH = 150;

export interface ProjectsTrendChartProps {
  trend: ProjectsTrendEnvelope;
  yMode: 'absolute' | 'share';
  windowWeeks: number;
  onProjectSelect?: (key: string) => void;
}

interface PreparedSeries {
  key: string;
  weekly: number[];
  cost: number;
  color: string;
}

export function ProjectsTrendChart({
  trend,
  yMode,
  windowWeeks,
  onProjectSelect,
}: ProjectsTrendChartProps) {
  const prepared = useMemo(() => {
    // Window slice — take the trailing `min(windowWeeks, trend.window_weeks)`
    // weeks (the envelope already capped to window_weeks but the user
    // may have a smaller pill selected than the snapshot's window).
    const n = Math.max(0, Math.min(windowWeeks, trend.window_weeks));
    const weeks = trend.weeks.slice(-n);
    const projWindow: PreparedSeries[] = trend.projects
      .map((p) => {
        const weekly = p.weekly_cost.slice(-n);
        const cost = weekly.reduce((s, c) => s + c, 0);
        return { key: p.key, weekly, cost, color: '' };
      })
      .sort((a, b) => b.cost - a.cost);
    const top = projWindow.slice(0, TOP_N).map((p, i) => ({
      ...p,
      color: SERIES_COLORS[i] ?? OTHER_COLOR,
    }));
    const tail = projWindow.slice(TOP_N);
    const otherWeekly: number[] = weeks.map((_, j) =>
      tail.reduce((s, p) => s + (p.weekly[j] ?? 0), 0),
    );
    const otherCost = otherWeekly.reduce((s, c) => s + c, 0);
    const series: PreparedSeries[] = [...top];
    if (tail.length > 0 && otherCost > 0) {
      series.push({ key: '(other)', weekly: otherWeekly, cost: otherCost, color: OTHER_COLOR });
    }
    return { weeks, series };
  }, [trend, windowWeeks]);

  if (prepared.weeks.length === 0 || prepared.series.length === 0) {
    return (
      <div className="panel-empty">
        No project activity in the last {windowWeeks} week{windowWeeks === 1 ? '' : 's'}.
      </div>
    );
  }

  const weekCount = prepared.weeks.length;
  const weekTotals = prepared.weeks.map((_, j) =>
    prepared.series.reduce((s, p) => s + (p.weekly[j] ?? 0), 0),
  );
  const yMax = yMode === 'share' ? 100 : Math.max(...weekTotals, 0.01);

  const xFor = (j: number) =>
    weekCount <= 1 ? VW / 2 : (j / (weekCount - 1)) * VW;

  // Stack-accumulator passed across series. Each polygon contributes its
  // own contribution to `accum`, walking bottom→top across the x-axis.
  const accum = new Array(weekCount).fill(0);
  type Poly = { color: string; key: string; points: string };
  const polygons: Poly[] = prepared.series.map((p) => {
    const points: string[] = [];
    // Bottom edge — left → right at current accum.
    for (let j = 0; j < weekCount; j++) {
      const y = VH - (accum[j] / yMax) * VH;
      points.push(`${xFor(j).toFixed(2)},${y.toFixed(2)}`);
    }
    // Add this series' contribution, then walk back right → left along
    // the new top edge.
    for (let j = weekCount - 1; j >= 0; j--) {
      const total = weekTotals[j] ?? 0;
      const contribution =
        yMode === 'share'
          ? total > 0
            ? ((p.weekly[j] ?? 0) / total) * 100
            : 0
          : p.weekly[j] ?? 0;
      accum[j] += contribution;
      const y = VH - (accum[j] / yMax) * VH;
      points.push(`${xFor(j).toFixed(2)},${y.toFixed(2)}`);
    }
    return { color: p.color, key: p.key, points: points.join(' ') };
  });

  const xAxisLabels = prepared.weeks.map((w) => (
    <span key={w.week_start_date}>{w.week_label}</span>
  ));

  return (
    <div className="projects-trend">
      <svg
        viewBox={`0 0 ${VW} ${VH}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Stacked area: project ${yMode === 'share' ? 'share %' : 'cost'} over ${weekCount} weeks`}
      >
        {polygons.map((p) => {
          const isOther = p.key === '(other)';
          return (
            <polygon
              key={p.key}
              fill={p.color}
              opacity={isOther ? 0.5 : 0.65}
              points={p.points}
              data-series-key={p.key}
              onClick={() => {
                if (!isOther) onProjectSelect?.(p.key);
              }}
              style={{ cursor: isOther ? 'default' : 'pointer' }}
            />
          );
        })}
      </svg>
      <div className="projects-trend-xaxis">{xAxisLabels}</div>
      <div className="projects-trend-legend">
        {prepared.series.map((p) => (
          <span key={p.key}>
            <span className="sw" style={{ background: p.color }} />
            {p.key}
          </span>
        ))}
      </div>
    </div>
  );
}
