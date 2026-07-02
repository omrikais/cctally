import { describe, it, expect } from 'vitest';
import { DEFAULT_PANEL_ORDER, CARD_LAYOUT, SHARE_CAPABLE_PANELS } from './panelIds';

describe('CARD_LAYOUT (#264 S1 bento)', () => {
  it('DEFAULT_PANEL_ORDER is the 8 grid cards in bento order', () => {
    expect(DEFAULT_PANEL_ORDER).toEqual([
      'sessions', 'trend', 'projects',
      'history', 'cache-report',
      'forecast', 'blocks', 'alerts',
    ]);
  });

  it('DEFAULT_PANEL_ORDER drops current-week + weekly/monthly/daily, keeps history (8 entries)', () => {
    const order = DEFAULT_PANEL_ORDER as readonly string[];
    expect(order).not.toContain('current-week');
    expect(order).not.toContain('weekly');
    expect(order).not.toContain('monthly');
    expect(order).not.toContain('daily');
    expect(order).toContain('history');
    expect(order).toHaveLength(8);
  });

  it('every default panel has a layout entry in one of the three height rows', () => {
    for (const id of DEFAULT_PANEL_ORDER) {
      expect(CARD_LAYOUT[id]).toBeDefined();
      expect(['tall', 'medium', 'short']).toContain(CARD_LAYOUT[id].row);
    }
  });

  it('each height row spans sum to 12', () => {
    const sums: Record<string, number> = { tall: 0, medium: 0, short: 0 };
    for (const id of DEFAULT_PANEL_ORDER) sums[CARD_LAYOUT[id].row] += CARD_LAYOUT[id].span;
    expect(sums).toEqual({ tall: 12, medium: 12, short: 12 });
  });

  it('classifies each card into its designed row', () => {
    const byRow = (r: 'tall' | 'medium' | 'short') =>
      DEFAULT_PANEL_ORDER.filter((id) => CARD_LAYOUT[id].row === r).sort();
    expect(byRow('tall')).toEqual(['projects', 'sessions', 'trend']);
    expect(byRow('medium')).toEqual(['cache-report', 'history']);
    expect(byRow('short')).toEqual(['alerts', 'blocks', 'forecast']);
  });

  it('the History grid card is share-capable (it shares the daily view via keyboardShare)', () => {
    expect(SHARE_CAPABLE_PANELS.has('history')).toBe(true);
  });
});
