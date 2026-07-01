// ForecastModal — FC-1 pill resolver (collapse-to-range on narrow wraps,
// true pixel-space min-gap on wide wraps). The pure `resolvePillLayout`
// is unit-tested directly (JSDOM-blind chart math); the DOM-mutating
// range-bar effect (now-marker / scale / legend, FC-2) is exercised via a
// full modal render below (#250 S4 · plan Tasks 5 & 6).
import { describe, it, expect } from 'vitest';
import { resolvePillLayout } from './ForecastModal';

const pins = () => [
  { kind: 'wa', pos: 19.8, raw: 19.8, pillWidthPx: 44 },
  { kind: 'r24', pos: 30.6, raw: 30.6, pillWidthPx: 44 },
];

describe('resolvePillLayout', () => {
  it('collapses to a range pill when both cannot fit (narrow wrap)', () => {
    const r = resolvePillLayout(pins() as never, /*wrapPx*/ 90, 8);
    expect(r.collapsed).toBe(true);
    expect(r.rangeText).toBe('19.8–30.6%');
  });
  it('keeps both with an edge gap >= minGap on a wide wrap', () => {
    const r = resolvePillLayout(pins() as never, /*wrapPx*/ 600, 8);
    expect(r.collapsed).toBe(false);
    const [a, b] = r.pins!;
    const ax = (a.resolvedXPct / 100) * 600,
      bx = (b.resolvedXPct / 100) * 600;
    const edgeGap =
      Math.abs(bx - ax) - (a.pillWidthPx / 2 + b.pillWidthPx / 2);
    expect(edgeGap).toBeGreaterThanOrEqual(8 - 0.01);
  });
});
