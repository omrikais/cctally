// CacheNetBars — per-day net $ bar chart, hand-rolled SVG.
//
// Two layout variants (parallel to CacheSparkline's mini/large split):
//
//   - large: 800x110 viewBox at width="100%". Used by the Cache Report
//     modal section 3. Symmetric bars about a horizontal mid-axis;
//     positive days = green bar rising up with an optional red
//     (wasted) segment stacked on top; negative days = amber bar
//     pointing down. X-axis labels are short M-D dates (or "Today"
//     for the rightmost bar). Spec 2026-05-21 §3.5.
//
//   - mini: viewBox 0 0 272 28, rendered at width=100% / height=100%
//     with preserveAspectRatio="none" so the SVG stretches to fill
//     whatever flex slot the panel gives it. The panel wraps the bars
//     in a flex: 1 1 auto container so they edge-to-edge fill the panel
//     between the sparkline above and the 14d-net subline below.
//     Single-direction bars (always rising from a baseline); color
//     encodes sign — green for positive net, amber for negative. No
//     axis labels, no wasted-red overlay, no section/chart-frame
//     chrome — the panel positions it directly. The tighter format
//     trades the modal's symmetric-bar precision for legibility at
//     a wide range of panel heights.
//
// X-axis ordering: envelope ``days`` is newest-first; we reverse to
// render oldest -> newest so the rightmost bar is today (matches the
// sparkline above it on the panel).
import type { CacheReportDailyRow } from '../types/envelope';
import { fmt } from '../lib/fmt';

export interface CacheNetBarsProps {
  days: CacheReportDailyRow[];   // newest-first; render oldest-first
  size: 'mini' | 'large';
}

const SIZES = {
  mini:  { width: 272, height: 28,  padX: 0,  padTop: 2,  padBot: 2,  barGap: 1 },
  large: { width: 800, height: 110, padX: 28, padTop: 28, padBot: 28, barGap: 4 },
} as const;

export function CacheNetBars({ days, size }: CacheNetBarsProps) {
  const cfg = SIZES[size];
  const isLarge = size === 'large';
  const ordered = [...days].reverse();

  if (ordered.length === 0) {
    if (isLarge) {
      return (
        <div className="crm-section">
          <div className="crm-section-head crm-sh-net">
            Net $ per day
            <span className="meta">no data</span>
          </div>
          <div className="crm-chart-frame netbars">
            <div style={{ color: 'var(--text-dim)', fontSize: 11, padding: '8px 4px' }}>
              No daily activity to render.
            </div>
          </div>
        </div>
      );
    }
    return (
      <svg
        className="cr-netbars-mini"
        width="100%"
        height="100%"
        viewBox={`0 0 ${cfg.width} ${cfg.height}`}
        preserveAspectRatio="none"
        aria-label="no data"
      />
    );
  }

  if (isLarge) {
    // Symmetric scale. Positive days are drawn as a green ``saved_usd``
    // bar with a red ``wasted_usd`` segment stacked on top, so the scale
    // MUST include the full stacked height (``saved_usd + wasted_usd``).
    // Pre-fix the denominator used ``max(saved_usd, |net_usd|)``, which
    // omits the wasted segment entirely: with ``saved=10, wasted=9,
    // net=1`` the chosen scale = 10 while the stacked bar reaches 19,
    // so the red segment was drawn at negative y and clipped off the top
    // of the SVG. Negative days are unaffected — ``|net_usd|`` is bounded
    // by the new denominator for free.
    const maxAbsNet = Math.max(
      1e-9,
      ...ordered.map((d) =>
        Math.max(d.saved_usd + d.wasted_usd, Math.abs(d.net_usd)),
      ),
    );
    const yScale = (cfg.height - cfg.padTop - cfg.padBot) / 2 / maxAbsNet;
    const midY = cfg.padTop + (cfg.height - cfg.padTop - cfg.padBot) / 2;
    const barWidth = (cfg.width - cfg.padX * 2) / ordered.length - cfg.barGap;

    return (
      <div className="crm-section">
        <div className="crm-section-head crm-sh-net">
          Net $ per day · saved (green) − wasted (red)
          <span className="meta">positive bars = caching helped</span>
        </div>
        <div className="crm-chart-frame netbars">
          <svg
            viewBox={`0 0 ${cfg.width} ${cfg.height}`}
            width="100%"
            height={cfg.height}
            aria-label={`Per-day net dollar chart, ${ordered.length} days`}
          >
            {/* zero line */}
            <line
              x1={cfg.padX}
              x2={cfg.width - cfg.padX}
              y1={midY}
              y2={midY}
              stroke="var(--border-soft)"
              strokeWidth={1}
            />
            {ordered.map((d, i) => {
              const x = cfg.padX + i * (barWidth + cfg.barGap);
              if (d.net_usd >= 0) {
                const greenH = d.saved_usd * yScale;
                const redH = d.wasted_usd * yScale;
                return (
                  <g key={d.date} data-testid="crm-netbar" data-date={d.date} data-sign="pos">
                    <rect
                      x={x}
                      y={midY - greenH}
                      width={barWidth}
                      height={greenH}
                      fill="var(--accent-green)"
                      rx={2}
                    />
                    {redH > 0 && (
                      <rect
                        x={x}
                        y={midY - greenH - redH}
                        width={barWidth}
                        height={redH}
                        fill="var(--accent-red)"
                        opacity={0.75}
                        rx={2}
                      />
                    )}
                  </g>
                );
              }
              const amberH = Math.abs(d.net_usd) * yScale;
              return (
                <rect
                  key={d.date}
                  data-testid="crm-netbar"
                  data-date={d.date}
                  data-sign="neg"
                  x={x}
                  y={midY}
                  width={barWidth}
                  height={amberH}
                  fill="var(--accent-amber)"
                  rx={2}
                />
              );
            })}
            {/* x-axis labels: M-D for day 0..N-2; "Today" for the last. */}
            {ordered.map((d, i) => {
              const x = cfg.padX + i * (barWidth + cfg.barGap) + barWidth / 2;
              const isLast = i === ordered.length - 1;
              const label = isLast ? 'Today' : fmt.calDate(d.date);
              return (
                <text
                  key={`l-${d.date}`}
                  x={x}
                  y={cfg.height - 6}
                  fill="var(--text-dim)"
                  fontSize={9}
                  textAnchor="middle"
                >
                  {label}
                </text>
              );
            })}
          </svg>
        </div>
      </div>
    );
  }

  // Mini: single-direction bars; color encodes sign. Scale by max |net|
  // so the tallest bar fills the chart height minus the small pad.
  const usable = cfg.height - cfg.padTop - cfg.padBot;
  const maxAbsNet = Math.max(
    1e-9,
    ...ordered.map((d) => Math.abs(d.net_usd)),
  );
  const barWidth = (cfg.width - cfg.padX * 2) / ordered.length - cfg.barGap;

  return (
    <svg
      className="cr-netbars-mini"
      width="100%"
      height="100%"
      viewBox={`0 0 ${cfg.width} ${cfg.height}`}
      preserveAspectRatio="none"
      aria-label={`14-day net dollar bars, ${ordered.length} days`}
    >
      {ordered.map((d, i) => {
        const x = cfg.padX + i * (barWidth + cfg.barGap);
        // Floor at 1 px so a $0 day still leaves a visible nub.
        const h = Math.max(1, (Math.abs(d.net_usd) / maxAbsNet) * usable);
        const y = cfg.height - cfg.padBot - h;
        const fill = d.net_usd >= 0 ? 'var(--accent-green)' : 'var(--accent-amber)';
        return (
          <rect
            key={d.date}
            data-testid="crm-netbar-mini"
            data-date={d.date}
            data-sign={d.net_usd >= 0 ? 'pos' : 'neg'}
            x={x}
            y={y}
            width={barWidth}
            height={h}
            fill={fill}
            rx={1}
          />
        );
      })}
    </svg>
  );
}
