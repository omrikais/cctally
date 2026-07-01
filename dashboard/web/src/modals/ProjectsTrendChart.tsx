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
import { useIsMobile } from '../hooks/useIsMobile';
import type { ProjectsTrendEnvelope } from '../types/envelope';
import { ProjectsRankedBars } from './ProjectsRankedBars';
import {
  bucketRankedProjects,
  isDominant,
  OTHER_KEY,
  colorFor,
  basenameOf,
} from './projectsChart';

const VW = 400;
const VH = 150;

export interface ProjectsTrendChartProps {
  trend: ProjectsTrendEnvelope;
  yMode: 'absolute' | 'share';
  windowWeeks: number;
  onProjectSelect?: (key: string) => void;
}

export function ProjectsTrendChart({
  trend,
  yMode,
  windowWeeks,
  onProjectSelect,
}: ProjectsTrendChartProps) {
  // Build-once bucketing shared by both render modes (PR-2).
  const prepared = useMemo(
    () => bucketRankedProjects(trend, windowWeeks),
    [trend, windowWeeks],
  );
  // Dominance is measured over real projects only (excludes `(other)`).
  const dominant = useMemo(
    () => isDominant(trend, windowWeeks),
    [trend, windowWeeks],
  );

  const isMobile = useIsMobile();

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

  // Issue #68: when `weekCount === 1` (fresh installs, 1w pill, or any
  // trend with a single week of data) the prior `xFor` collapsed every
  // point to `VW/2`, drawing each polygon as a zero-width vertical line.
  // Synthesize a `[VW*0.1, VW*0.9]` horizontal span instead so each
  // series renders as a rectangle (a wide stacked-bar segment).
  const xFor = (j: number, edge: 'left' | 'right' = 'left'): number =>
    weekCount <= 1
      ? edge === 'left'
        ? VW * 0.1
        : VW * 0.9
      : (j / (weekCount - 1)) * VW;

  // Stack-accumulator passed across series. Each polygon contributes its
  // own contribution to `accum`, walking bottom→top across the x-axis.
  const accum = new Array(weekCount).fill(0);
  type Poly = { color: string; key: string; points: string };
  const polygons: Poly[] = prepared.series.map((p, i) => {
    const points: string[] = [];
    // Bottom edge — left → right at current accum. The 1-week degenerate
    // path emits both synthesized edges at the same accum y so each
    // polygon closes as a rectangle (issue #68).
    for (let j = 0; j < weekCount; j++) {
      const y = VH - (accum[j] / yMax) * VH;
      points.push(`${xFor(j, 'left').toFixed(2)},${y.toFixed(2)}`);
      if (weekCount === 1) {
        points.push(`${xFor(j, 'right').toFixed(2)},${y.toFixed(2)}`);
      }
    }
    // Add this series' contribution, then walk back right → left along
    // the new top edge. Mirror the 1-week doubling so the rectangle
    // closes with the right-edge corner first.
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
      if (weekCount === 1) {
        points.push(`${xFor(j, 'right').toFixed(2)},${y.toFixed(2)}`);
      }
      points.push(`${xFor(j, 'left').toFixed(2)},${y.toFixed(2)}`);
    }
    return { color: colorFor(p.key, i), key: p.key, points: points.join(' ') };
  });

  const xAxisLabels = prepared.weeks.map((w) => (
    <span key={w.week_start_date}>{w.week_label}</span>
  ));

  // PR-2 y-axis labels for the stacked-area mode: absolute labels $total
  // (top) / $0 (bottom); share labels 100% / 0%.
  const yTopLabel = yMode === 'share' ? '100%' : `$${yMax.toFixed(0)}`;
  const yBotLabel = yMode === 'share' ? '0%' : '$0';

  return (
    <div className="projects-trend">
      {dominant ? (
        // PR-2 conditional swap: under a dominant distribution the stacked
        // area is an unreadable near-solid block — render ranked bars
        // instead (skip the SVG + x-axis), keeping the legend below.
        <ProjectsRankedBars series={prepared.series} onProjectSelect={onProjectSelect} />
      ) : (
        <>
          <div className="projects-trend-plot">
            <div className="projects-trend-yaxis" data-testid="projects-yaxis">
              <span>{yTopLabel}</span>
              <span>{yBotLabel}</span>
            </div>
            <svg
              viewBox={`0 0 ${VW} ${VH}`}
              preserveAspectRatio="none"
              role="img"
              aria-label={`Stacked area: project ${yMode === 'share' ? 'share %' : 'cost'} over ${weekCount} weeks`}
            >
              {polygons.map((p) => {
                const isOther = p.key === OTHER_KEY;
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
          </div>
          <div className="projects-trend-xaxis">{xAxisLabels}</div>
        </>
      )}
      <div className="projects-trend-legend">
        {prepared.series.map((p, i) => {
          const isOther = p.key === OTHER_KEY;
          const swatch = <span className="sw" style={{ background: colorFor(p.key, i) }} />;
          const label = basenameOf(p.bucket_path);
          const content = <>{swatch}{label}</>;
          if (isMobile && !isOther) {
            return (
              <button
                key={p.key}
                type="button"
                className="projects-trend-legend-item"
                data-series-key={p.key}
                title={p.bucket_path}
                onClick={() => onProjectSelect?.(p.key)}
              >
                {content}
              </button>
            );
          }
          return (
            <span
              key={p.key}
              className="projects-trend-legend-item"
              data-series-key={p.key}
              title={p.bucket_path}
            >
              {content}
            </span>
          );
        })}
      </div>
    </div>
  );
}
