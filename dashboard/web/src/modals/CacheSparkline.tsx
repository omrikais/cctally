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
//     panel edge-to-edge alongside the bars below it. No axis labels.
//     The today-marker circle becomes a slight horizontal ellipse
//     after the asymmetric stretch — an accepted trade for matching
//     width with the net-bars.
//   - large: viewBox 800x90 rendered with width="100%" so it shrinks
//     to fit the modal body at narrow viewports. The y-axis AUTO-ZOOMS
//     (CR-1, #250) via computeAutoZoomDomain — it fits to the data band
//     plus the median +/-band instead of a fixed 0-100, so a series
//     clustered at 96-98% shows real day-to-day variation and the
//     dashed-median band reads clearly. The two HTML <span> axis labels
//     (top/bottom) show the zoomed domain's hi/lo %, and three faint
//     horizontal gridlines at hi / mid / lo replace the old fixed
//     0/25/50/75/100 rules. The `mini` variant keeps the fixed 0-100
//     scale (a silent zoom on the label-less mini would mislead).
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
import { CACHE_REPORT_BAND_PP } from '../lib/cache-report-constants';
import { computeAutoZoomDomain } from '../lib/chartDomain';

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

  // Auto-zoom the y-domain for the large variant only (CR-1, #250): fit
  // to the data band + the median +/-band so the polyline shows real
  // variation instead of pinning to the top edge. The mini variant keeps
  // the fixed 0-100 scale (a silent zoom on the label-less mini would
  // mislead). Empty `ordered` degenerates to {lo:0, hi:100}.
  const domain = isLarge
    ? computeAutoZoomDomain(
        ordered.map((d) => d.cache_hit_percent),
        baseline_median_percent,
        CACHE_REPORT_BAND_PP,
      )
    : { lo: 0, hi: 100 };

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
        <span className="cr-spark-axis cr-spark-axis-top">{Math.round(domain.hi)}%</span>
        {emptySvg}
        <span className="cr-spark-axis cr-spark-axis-bot">{Math.round(domain.lo)}%</span>
      </div>
    ) : emptySvg;
  }

  // y-axis: domain.lo at the bottom, domain.hi at the top. For the mini
  // variant the domain is a fixed {0, 100} so this is the classic scale.
  const yFor = (pct: number): number => {
    const span = domain.hi - domain.lo || 1;
    const t = (Math.max(domain.lo, Math.min(domain.hi, pct)) - domain.lo) / span;
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
      {/* Gridlines (large only) — three horizontal rules at the zoomed
          domain's hi / mid / lo so the user can read the polyline's
          y-position without a tooltip (CR-1, #250). The hi/lo bounds are
          solid white at moderate opacity (they're the y-axis bounds and
          need to read as structural); the mid rule is dashed and fainter
          so it cues the middle without competing with the polyline.
          Theme-token colors like --border-soft were too low-contrast
          against the dark modal background to be visible at all. */}
      {isLarge &&
        (
          [
            { k: 'hi', pct: domain.hi, bound: true },
            { k: 'mid', pct: (domain.lo + domain.hi) / 2, bound: false },
            { k: 'lo', pct: domain.lo, bound: true },
          ] as const
        ).map(({ k, pct, bound }) => (
          <line
            key={`grid-${k}`}
            x1={0}
            x2={cfg.width}
            y1={yFor(pct)}
            y2={yFor(pct)}
            stroke={bound ? 'rgba(255,255,255,0.45)' : 'rgba(255,255,255,0.18)'}
            strokeWidth={bound ? 1 : 0.5}
            strokeDasharray={bound ? undefined : '4,3'}
            opacity={1}
            data-testid={`cr-spark-gridline-${k}`}
          />
        ))}
      {baseline_median_percent !== null && (
        <>
          {/* Tinted baseline band: ±BAND_PP around the median. */}
          <rect
            x={0}
            y={yFor(baseline_median_percent + CACHE_REPORT_BAND_PP)}
            width={cfg.width}
            height={
              yFor(baseline_median_percent - CACHE_REPORT_BAND_PP)
              - yFor(baseline_median_percent + CACHE_REPORT_BAND_PP)
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
      <span className="cr-spark-axis cr-spark-axis-top">{Math.round(domain.hi)}%</span>
      {svg}
      <span className="cr-spark-axis cr-spark-axis-bot">{Math.round(domain.lo)}%</span>
    </div>
  ) : svg;
}
