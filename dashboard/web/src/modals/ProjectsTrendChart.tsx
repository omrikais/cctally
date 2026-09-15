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
import { useDisplayTz } from '../hooks/useDisplayTz';
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

/** X-axis labels for the projects trend chart, disambiguated per display zone.
 *
 * Exported so the zone behaviour is unit-testable without mocking a hook.
 *
 * Both halves of a label come from ONE instant in ONE zone. `week_label`
 * arrives baked in UTC, so appending a display-zone time to it produced a
 * label whose date and time disagreed: under America/Los_Angeles the axis read
 * `Apr 17 20:00` for an instant whose local date is Apr 16, and so appeared to
 * run backwards. Deriving the date here also makes this axis agree with the
 * Trend panel, which already labels in the display zone.
 *
 * A suffix is added only where two cycles genuinely render the same local date.
 * That needs no canonical-week key the way the server kernel does: this window
 * holds at most twelve consecutive cycles, so two ordinary cycles a year apart
 * cannot both appear and any collision here IS one credited week's segments.
 * `week_label` remains the fallback for an envelope predating `week_start_at`.
 */
export function projectsAxisLabels(
  weeks: { week_label: string; week_start_at?: string | null }[],
  zone: string,
): string[] {
  const dayFmt = new Intl.DateTimeFormat('en-US', {
    timeZone: zone, month: 'short', day: '2-digit',
  });
  const timeFmt = new Intl.DateTimeFormat('en-US', {
    timeZone: zone, hour: '2-digit', minute: '2-digit', hour12: false,
  });
  const base = weeks.map((w) => (
    w.week_start_at ? dayFmt.format(new Date(w.week_start_at)) : w.week_label
  ));
  const counts = new Map<string, number>();
  for (const label of base) counts.set(label, (counts.get(label) ?? 0) + 1);
  return weeks.map((w, i) => (
    (counts.get(base[i]) ?? 0) > 1 && w.week_start_at
      ? `${base[i]} ${timeFmt.format(new Date(w.week_start_at))}`
      : base[i]
  ));
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
  // Read ABOVE the early return below: a hook placed after it unmounts on
  // the empty branch and blanks the dashboard.
  const display = useDisplayTz();

  if (prepared.weeks.length === 0 || prepared.series.length === 0) {
    return (
      <div className="panel-empty">
        No project activity in the last {windowWeeks} cycle{windowWeeks === 1 ? '' : 's'}.
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

  // #750 S4 §3.5: keyed on the segment INSTANT. `week_start_date` is the
  // shared billing-cycle join key, so both cycles of a credited week carry
  // one value for it and this axis rendered duplicate React keys. The
  // fallback keeps an older envelope rendering rather than crashing.
  //
  // §3.2a: the keys were distinct but the LABELS were not — two cycles of a
  // credited week that fall on one date both rendered `Apr 17`, so a reader
  // could not tell the columns apart. The suffix is derived here rather than on
  // the wire because `week_label` is UTC-anchored by contract to keep the JSON
  // timezone-agnostic, while this axis can honour the viewer's own zone.
  // Grouping needs no canonical-week key the way the server kernel does: this
  // window holds at most twelve consecutive cycles, so two ordinary cycles a
  // year apart cannot both appear, and any collision here IS two segments of
  // one credited week. A window with no collision is byte-identical.
  // The DATE is derived here too, not just the suffix. `week_label` is baked
  // UTC on the wire, so appending a display-zone time to it produced a label
  // whose two halves came from different zones: under America/Los_Angeles the
  // axis read `Apr 17 20:00` for an instant whose local date is Apr 16, so it
  // appeared to run backwards. Deriving both halves from one instant in one
  // zone also makes this axis agree with the Trend panel, which already labels
  // in the display zone — under that same setting the panel reads Apr 16 / Apr
  // 17 and needs no suffix at all, and now so does this. `week_label` remains
  // the fallback for an envelope that predates `week_start_at`.
  const labelTexts = projectsAxisLabels(prepared.weeks, display.resolvedTz);
  const xAxisLabels = prepared.weeks.map((w, i) => (
    <span key={w.week_start_at ?? `${w.week_start_date}:${i}`}>{labelTexts[i]}</span>
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
              aria-label={`Stacked area: project ${yMode === 'share' ? 'share %' : 'cost'} over ${weekCount} ${weekCount === 1 ? 'cycle' : 'cycles'}`}
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
