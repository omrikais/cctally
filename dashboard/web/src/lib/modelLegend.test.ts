import { describe, it, expect } from 'vitest';
import { modelLegend } from './modelLegend';

const m = (display: string, chip: string, cost_pct: number, model = `id-${display}`) =>
  ({ model, display, chip, cost_pct });

describe('modelLegend', () => {
  it('one model → one item, zero more', () => {
    expect(modelLegend([m('Opus 4.8', 'opus', 100, 'claude-opus-4-8')])).toEqual({
      items: [{ model: 'claude-opus-4-8', display: 'Opus 4.8', pct: 100, chip: 'opus' }], more: 0,
    });
  });
  it('three models → top two items + more:1, input order preserved', () => {
    const out = modelLegend([m('Opus 4.8', 'opus', 70), m('Sonnet 5', 'sonnet', 20), m('Haiku', 'haiku', 10)]);
    expect(out.items.map((i) => i.display)).toEqual(['Opus 4.8', 'Sonnet 5']);
    expect(out.more).toBe(1);
  });
  it('carries the canonical model id so distinct same-display builds stay keyable', () => {
    const out = modelLegend([m('opus-4-8', 'opus', 60, 'claude-opus-4-8-20260101'),
                             m('opus-4-8', 'opus', 40, 'claude-opus-4-8-20260615')]);
    expect(out.items.map((i) => i.model)).toEqual(['claude-opus-4-8-20260101', 'claude-opus-4-8-20260615']);
  });
  it('zero models → empty items, zero more', () => {
    expect(modelLegend([])).toEqual({ items: [], more: 0 });
  });
});
