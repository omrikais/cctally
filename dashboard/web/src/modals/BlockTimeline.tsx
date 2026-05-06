import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt, type FmtCtx } from '../lib/fmt';
import type { BlockDetail } from '../types/envelope';

// SVG geometry — matches the spec's coordinate system. Rendered into a
// 720x180 viewBox so it scales with the modal-body width.
const VB_W = 720;
const VB_H = 180;
const PAD = { left: 46, right: 20, top: 18, bottom: 20 } as const;

function fmtUsdAxis(usd: number): string {
  if (usd === 0) return '$0';
  if (usd < 10) return '$' + usd.toFixed(2);
  return '$' + Math.round(usd).toString();
}

// F4 (Codex round 1): a component-local `fmtHHMMInTz` previously called
// `new Date(iso).toLocaleTimeString(...)` directly, bypassing the
// `lib/fmt.ts` chokepoint. Migrated to `fmt.timeHHmm(..., { noSuffix: true })`
// so all dashboard datetime rendering routes through the single chokepoint.
// `noSuffix: true` because SVG axis labels have no anchor text for a tz
// suffix; the surrounding modal header carries the offset_label.

function useGeneratedAt(): string {
  return useSyncExternalStore(
    subscribeStore,
    () => getState().snapshot?.generated_at ?? '',
  );
}

export function BlockTimeline({ detail }: { detail: BlockDetail }) {
  const generatedAt = useGeneratedAt();
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const startMs = Date.parse(detail.start_at);
  const endMs   = Date.parse(detail.end_at);
  const span    = endMs - startMs || 1;
  const xOf = (iso: string) =>
    PAD.left + ((Date.parse(iso) - startMs) / span) * (VB_W - PAD.left - PAD.right);
  const yMax = Math.max(detail.cost_usd, detail.projection?.total_cost_usd ?? 0);
  const yOf = (cost: number) => {
    const top = PAD.top;
    const bot = VB_H - PAD.bottom;
    if (yMax <= 0) return bot;
    return bot - (cost / yMax) * (bot - top);
  };

  // Hour boundaries: start, +1h, +2h, +3h, +4h, end (5h block by definition).
  const hours: string[] = [];
  for (let i = 0; i <= 5; i++) {
    hours.push(new Date(startMs + i * 3600_000).toISOString());
  }

  // Sample → polyline. Prepend (start, 0); append (now, last_cum) on
  // active blocks so the line visibly reaches "now" between SSE ticks.
  const lastCum = detail.samples.length > 0
    ? detail.samples[detail.samples.length - 1].cum
    : 0;
  const nowIso = detail.is_active ? (generatedAt || new Date().toISOString()) : null;
  const polyPts: Array<[number, number]> = [];
  if (detail.samples.length > 0) {
    polyPts.push([xOf(detail.start_at), yOf(0)]);
    for (const s of detail.samples) polyPts.push([xOf(s.t), yOf(s.cum)]);
    if (nowIso) polyPts.push([xOf(nowIso), yOf(lastCum)]);
  }
  const polyAttr = polyPts
    .map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`)
    .join(' ');
  const areaAttr = polyPts.length > 0
    ? `M ${polyAttr.replace(/ /g, ' L ')} L ${polyPts[polyPts.length - 1][0].toFixed(1)},${yOf(0).toFixed(1)} Z`
    : '';

  // Y-axis tick labels: 0, mid, max. When yMax === 0, only show $0.
  const yTicks = yMax <= 0 ? [0] : [0, yMax / 2, yMax];

  return (
    <div className="mblock-timeline">
      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        style={{ width: '100%', height: 'auto' }}
        role="img"
        aria-label="Cumulative cost over the 5-hour block window"
      >
        {/* Y-axis labels */}
        {yTicks.map((v) => (
          <text key={v}
                x={8} y={yOf(v) + 4}
                fill="var(--text-dim)" fontSize={10}>
            {fmtUsdAxis(v)}
          </text>
        ))}
        {/* Axes */}
        <line x1={PAD.left} y1={PAD.top}
              x2={PAD.left} y2={VB_H - PAD.bottom}
              stroke="var(--border-soft)" strokeWidth={0.8} />
        <line x1={PAD.left} y1={VB_H - PAD.bottom}
              x2={VB_W - PAD.right} y2={VB_H - PAD.bottom}
              stroke="var(--border-soft)" strokeWidth={0.8} />
        {/* Hour grid */}
        {hours.slice(1, -1).map((h) => (
          <line key={h}
                x1={xOf(h)} y1={PAD.top}
                x2={xOf(h)} y2={VB_H - PAD.bottom}
                stroke="var(--border-soft)" strokeDasharray="2 3"
                strokeWidth={0.5} />
        ))}
        {/* Hour labels */}
        {hours.map((h) => (
          <text key={h}
                x={xOf(h) - 14} y={VB_H - 5}
                fill="var(--text-dim)" fontSize={9}>
            {fmt.timeHHmm(h, ctx, { noSuffix: true })}
          </text>
        ))}
        {/* Area + cumulative line */}
        {polyPts.length > 0 ? (
          <>
            <path d={areaAttr} fill="var(--accent-green)" opacity={0.12} />
            <polyline points={polyAttr}
                      fill="none" stroke="var(--accent-green)" strokeWidth={2} />
            <circle cx={polyPts[polyPts.length - 1][0]}
                    cy={polyPts[polyPts.length - 1][1]}
                    r={3} fill="var(--accent-green)" />
          </>
        ) : (
          <text x={VB_W / 2} y={VB_H / 2}
                fill="var(--text-faint)" fontSize={11}
                textAnchor="middle">
            No spend recorded yet in this block.
          </text>
        )}
        {/* Projection ghost + now-marker (active only, projection present) */}
        {detail.is_active && detail.projection && nowIso && polyPts.length > 0 ? (
          <>
            <polyline
              points={`${xOf(nowIso).toFixed(1)},${yOf(lastCum).toFixed(1)} ${xOf(detail.end_at).toFixed(1)},${yOf(detail.projection.total_cost_usd).toFixed(1)}`}
              fill="none"
              stroke="var(--accent-amber)"
              strokeWidth={1.5}
              strokeDasharray="5 4"
              opacity={0.85}
            />
            <circle
              cx={xOf(detail.end_at)}
              cy={yOf(detail.projection.total_cost_usd)}
              r={3}
              fill="var(--accent-amber)"
              opacity={0.85}
            />
            <text
              x={xOf(detail.end_at) - 50}
              y={yOf(detail.projection.total_cost_usd) - 6}
              fill="var(--accent-amber)"
              fontSize={9}
            >
              proj ${detail.projection.total_cost_usd.toFixed(2)}
            </text>
            <line
              x1={xOf(nowIso)} y1={PAD.top}
              x2={xOf(nowIso)} y2={VB_H - PAD.bottom}
              stroke="var(--accent-amber)" strokeWidth={1.2}
            />
            <text
              x={xOf(nowIso) + 4} y={PAD.top + 10}
              fill="var(--accent-amber)" fontSize={9}
            >
              now
            </text>
          </>
        ) : null}
      </svg>
    </div>
  );
}
