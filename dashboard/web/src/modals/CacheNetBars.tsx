// CacheNetBars — section 3 of the Cache Report modal: per-day net $.
//
// Hand-rolled SVG bar chart (no chart library — same constraint as the
// rest of the cache-report surfaces; spec §3.5). 14 bars, one per day,
// composed as:
//
//   Positive net day: green bar (--accent-green) rising from the
//     horizontal mid-axis; if ``wasted_usd > 0``, a thin red segment
//     (--accent-red, 0.75 opacity) is stacked above the green portion
//     to surface the cost penalty visually.
//   Negative net day: a downward amber bar (--accent-amber, no green
//     segment).
//
// X-axis labels: short M-D date for the first 13 days, ``Today`` for
// the last (the rightmost bar always represents today since the
// envelope days array is newest-first and we render reversed).
//
// Spec 2026-05-21 §3.5.
import type { CacheReportDailyRow } from '../types/envelope';

export interface CacheNetBarsProps {
  days: CacheReportDailyRow[];   // newest-first; render oldest-first
}

const W = 800;
const H = 110;
const PAD = 28;            // axis-label spacing (left/right + bottom labels)
const BAR_GAP = 4;

export function CacheNetBars({ days }: CacheNetBarsProps) {
  const ordered = [...days].reverse();
  if (ordered.length === 0) {
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

  // Scale: vertical range is half the available height in each
  // direction (positive above mid, negative below). MaxAbsNet picks the
  // larger of (saved_usd, |net_usd|) so the green-plus-red stack on
  // positive days fits even when the red segment pushes the visible
  // top above |net_usd|.
  const maxAbsNet = Math.max(
    1e-9,
    ...ordered.map((d) => Math.max(d.saved_usd, Math.abs(d.net_usd))),
  );
  const yScale = (H - PAD * 2) / 2 / maxAbsNet;
  const midY = PAD + (H - PAD * 2) / 2;
  const barWidth = (W - PAD * 2) / ordered.length - BAR_GAP;

  return (
    <div className="crm-section">
      <div className="crm-section-head crm-sh-net">
        Net $ per day · saved (green) − wasted (red)
        <span className="meta">positive bars = caching helped</span>
      </div>
      <div className="crm-chart-frame netbars">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          height={H}
          aria-label={`Per-day net dollar chart, ${ordered.length} days`}
        >
          {/* zero line */}
          <line
            x1={PAD}
            x2={W - PAD}
            y1={midY}
            y2={midY}
            stroke="var(--border-soft)"
            strokeWidth={1}
          />
          {ordered.map((d, i) => {
            const x = PAD + i * (barWidth + BAR_GAP);
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
            const x = PAD + i * (barWidth + BAR_GAP) + barWidth / 2;
            const isLast = i === ordered.length - 1;
            // YYYY-MM-DD -> M-D (drop the year prefix).
            const label = isLast ? 'Today' : d.date.slice(5);
            return (
              <text
                key={`l-${d.date}`}
                x={x}
                y={H - 6}
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
