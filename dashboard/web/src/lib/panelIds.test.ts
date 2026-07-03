import { describe, it, expect } from 'vitest';
import { DEFAULT_PANEL_ORDER, CARD_LAYOUT, SHARE_CAPABLE_PANELS } from './panelIds';

describe('CARD_LAYOUT (#264 S2 / #266 bento — Blocks promoted to medium)', () => {
  it('DEFAULT_PANEL_ORDER is the 10 grid cards in bento order', () => {
    expect(DEFAULT_PANEL_ORDER).toEqual([
      'sessions', 'trend', 'projects',
      'daily', 'cache-report', 'weekly', 'monthly', 'blocks', 'forecast',
      'alerts',
    ]);
  });

  it('has daily/weekly/monthly, no history/current-week (10 entries)', () => {
    const order = DEFAULT_PANEL_ORDER as readonly string[];
    expect(order).not.toContain('history');
    expect(order).not.toContain('current-week');
    for (const id of ['daily', 'weekly', 'monthly']) expect(order).toContain(id);
    expect(order).toHaveLength(10);
  });

  it('every default panel has a layout entry in one of the three height rows', () => {
    for (const id of DEFAULT_PANEL_ORDER) {
      expect(CARD_LAYOUT[id]).toBeDefined();
      expect(['tall', 'medium', 'short']).toContain(CARD_LAYOUT[id].row);
    }
  });

  it('tall + short rows sum to 12; the medium 3×2 sums to 36', () => {
    const sums: Record<string, number> = { tall: 0, medium: 0, short: 0 };
    for (const id of DEFAULT_PANEL_ORDER) sums[CARD_LAYOUT[id].row] += CARD_LAYOUT[id].span;
    expect(sums).toEqual({ tall: 12, medium: 36, short: 12 });
  });

  it('classifies each card into its designed row', () => {
    const byRow = (r: 'tall' | 'medium' | 'short') =>
      DEFAULT_PANEL_ORDER.filter((id) => CARD_LAYOUT[id].row === r).sort();
    expect(byRow('tall')).toEqual(['projects', 'sessions', 'trend']);
    expect(byRow('medium')).toEqual(['blocks', 'cache-report', 'daily', 'forecast', 'monthly', 'weekly']);
    expect(byRow('short')).toEqual(['alerts']);
  });

  it('daily/weekly/monthly are share-capable; history is not present', () => {
    for (const id of ['daily', 'weekly', 'monthly']) expect(SHARE_CAPABLE_PANELS.has(id)).toBe(true);
    expect(SHARE_CAPABLE_PANELS.has('history')).toBe(false);
  });
});
