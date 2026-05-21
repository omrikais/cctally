// CacheSparkline — 14-day cache-hit % line; hand-rolled SVG.
//
// Used by CacheReportPanel (mini variant, edge-to-edge in the panel
// width) and the CacheReportModal section 2 (large variant, axis labels
// in HTML siblings stacked above/below the SVG via flex column).
// Spec 2026-05-21 §2.3 + §3.4.
//
// Layout:
//   - mini: viewBox 0 0 272 32, rendered at width="100%" / fixed 32 px
//     height with preserveAspectRatio="none" so the line spans the
//     panel edge-to-edge alongside the bars below it (issue #77 P2-4
//     Round 2). No axis labels. The today-marker circle becomes a
//     slight horizontal ellipse after the asymmetric stretch — an
//     accepted trade for the matching width with the net-bars.
//   - large: viewBox 800x90 rendered with width="100%" so it shrinks
//     to fit the modal body at narrow viewports (issue #77 P2-1).
//     Axis labels ("100%" / "0%") live in HTML <span> siblings
//     stacked above/below the SVG via flex column so the polyline
//     can't collide with the "100%" text when cache_hit_percent hugs
//     the top (issue #77 P2-2; Round 2 restructure after Round 1's
//     absolute-over-SVG version regressed). Five faint horizontal
//     gridlines at 0/25/50/75/100% give the user a visual cue that
//     the chart is bounded to [0, 100] — without them a polyline
//     hugging 97-98% reads as a flat line floating in nowhere.
//   - x-axis: oldest -> newest, evenly spaced. We reverse the
//     newest-first envelope days[] in-place so the today marker sits at
//     the right edge.
//   - y-axis: 0% at the bottom, 100% at the top, clamped to [0, 100].
//   - When `baseline_median_percent` is non-null, a tinted +/- 5pp band
//     and a dashed median line render behind the polyline as visual
//     baseline cues for the modal-large variant. The mini variant is
//     visually compact but follows the same pattern for consistency.
//   - Today's marker is the last point (rightmost) drawn as a circle.
//     Its `fill` is parameterized so the panel can pass amber on
//     anomaly_triggered and green when healthy.
//
// No new dependencies — pure inline SVG, mirrors ProjectsTrendChart.tsx
// and BlockTimeline.tsx precedent.
import type { CacheReportDailyRow } from '../types/envelope';

export interface CacheSparklineProps {
  /** Newest-first daily rows from the envelope. Up to 14 entries. */
  days: CacheReportDailyRow[];
  /** Baseline median (14d) used to draw the dashed mid-line; null = thin. */
  baseline_median_percent: number | null;
  /** Color of today's marker — amber on anomaly, green when healthy. */
  today_marker_color: string;
  /**
   * Layout variant.
   * - mini: width="100%" / fixed height / preserveAspectRatio="none"
   *   so the line stretches edge-to-edge alongside the panel net-bars.
   * - large: viewBox 800x90 rendered width="100%" with gridlines and
   *   HTML axis labels for the modal.
   */
  size: 'mini' | 'large';
}

const SIZES = {
  mini:  { width: 272, height: 32, padTop: 4, padBot: 4 },
  large: { width: 800, height: 90, padTop: 6, padBot: 6 },
} as const;

// Y-axis gridline percentages drawn on the large variant. Includes the
// outer bounds (0/100) plus quarter marks so the user can read the
// polyline position by eye without a tooltip.
const GRIDLINE_PCTS = [0, 25, 50, 75, 100] as const;

export function CacheSparkline({
  days,
  baseline_median_percent,
  today_marker_color,
  size,
}: CacheSparklineProps) {
  const cfg = SIZES[size];
  const isLarge = size === 'large';
  // Reverse newest-first envelope so the polyline renders oldest -> newest.
  const ordered = [...days].reverse();

  // Mini = width:100% + fixed height + preserveAspectRatio="none" so
  // the polyline x-positions stretch the full panel width to match
  // the net-bars below. Large = width:100% with default preserveAspectRatio
  // so the viewBox aspect ratio is preserved and the chart scales
  // proportionally at narrow viewports.
  const svgSizeProps = isLarge
    ? { width: '100%' as const }
    : {
        width: '100%' as const,
        height: cfg.height,
        preserveAspectRatio: 'none',
      };

  if (ordered.length === 0) {
    const emptySvg = (
      <svg
        className="cr-spark"
        {...svgSizeProps}
        viewBox={`0 0 ${cfg.width} ${cfg.height}`}
        aria-label="no data"
      />
    );
    return isLarge ? (
      <div className="cr-spark-wrap">
        <span className="cr-spark-axis cr-spark-axis-top">100%</span>
        {emptySvg}
        <span className="cr-spark-axis cr-spark-axis-bot">0%</span>
      </div>
    ) : emptySvg;
  }

  // y-axis: 0% at bottom, 100% at top.
  const yFor = (pct: number): number => {
    const t = Math.max(0, Math.min(100, pct)) / 100;
    return cfg.padTop + (1 - t) * (cfg.height - cfg.padTop - cfg.padBot);
  };
  const xFor = (i: number): number => {
    if (ordered.length === 1) return cfg.width / 2;
    return (i / (ordered.length - 1)) * cfg.width;
  };

  const points = ordered
    .map((d, i) =>
      `${xFor(i).toFixed(1)},${yFor(d.cache_hit_percent).toFixed(1)}`,
    )
    .join(' ');
  const todayIdx = ordered.length - 1;
  const todayCx = xFor(todayIdx);
  const todayCy = yFor(ordered[todayIdx].cache_hit_percent);

  const svg = (
    <svg
      className="cr-spark"
      {...svgSizeProps}
      viewBox={`0 0 ${cfg.width} ${cfg.height}`}
      aria-label={`Cache hit % timeline, ${ordered.length} days`}
    >
      {/* Gridlines (large only) — five horizontal rules at 0/25/50/75/100%
          so the user can read the polyline's y-position without a
          tooltip. The 0 and 100 rules are solid white at moderate
          opacity (they're the y-axis bounds and need to read as
          structural); the 25/50/75 rules are dashed and fainter so
          they cue the quarters without competing with the polyline.
          Theme-token colors like --border-soft were too low-contrast
          against the dark modal background to be visible at all. */}
      {isLarge &&
        GRIDLINE_PCTS.map((pct) => {
          const isBound = pct === 0 || pct === 100;
          return (
            <line
              key={`grid-${pct}`}
              x1={0}
              x2={cfg.width}
              y1={yFor(pct)}
              y2={yFor(pct)}
              stroke={isBound ? 'rgba(255,255,255,0.45)' : 'rgba(255,255,255,0.18)'}
              strokeWidth={isBound ? 1 : 0.5}
              strokeDasharray={isBound ? undefined : '4,3'}
              opacity={1}
              data-testid={`cr-spark-gridline-${pct}`}
            />
          );
        })}
      {baseline_median_percent !== null && (
        <>
          {/* Tinted baseline band: +/- 5pp around the median. */}
          <rect
            x={0}
            y={yFor(baseline_median_percent + 5)}
            width={cfg.width}
            height={
              yFor(baseline_median_percent - 5)
              - yFor(baseline_median_percent + 5)
            }
            fill="var(--accent-cyan)"
            opacity={0.10}
          />
          {/* Dashed median line. */}
          <line
            x1={0}
            x2={cfg.width}
            y1={yFor(baseline_median_percent)}
            y2={yFor(baseline_median_percent)}
            stroke="var(--accent-cyan)"
            strokeWidth={0.5}
            strokeDasharray="3,3"
            opacity={0.6}
          />
        </>
      )}
      <polyline
        points={points}
        fill="none"
        stroke="var(--accent-cyan)"
        strokeWidth={isLarge ? 2 : 1.5}
      />
      <circle
        cx={todayCx}
        cy={todayCy}
        r={isLarge ? 5 : 3.5}
        fill={today_marker_color}
        data-testid="cr-spark-today-marker"
      />
    </svg>
  );

  return isLarge ? (
    <div className="cr-spark-wrap">
      <span className="cr-spark-axis cr-spark-axis-top">100%</span>
      {svg}
      <span className="cr-spark-axis cr-spark-axis-bot">0%</span>
    </div>
  ) : svg;
}
