import { describe, it, expect } from 'vitest';
import { DEFAULT_PANEL_ORDER, CARD_TIER, SHARE_CAPABLE_PANELS } from './panelIds';

describe('panelIds #248 + S8 #254', () => {
  it('DEFAULT_PANEL_ORDER drops current-week + weekly/monthly/daily, adds history (8 entries)', () => {
    const order = DEFAULT_PANEL_ORDER as readonly string[];
    expect(order).not.toContain('current-week');
    expect(order).not.toContain('weekly');
    expect(order).not.toContain('monthly');
    expect(order).not.toContain('daily');
    expect(order).toContain('history');
    expect(order).toHaveLength(8);
  });
  it('CARD_TIER classifies every default panel and only into tile|wide', () => {
    for (const id of DEFAULT_PANEL_ORDER) {
      expect(['tile', 'wide']).toContain(CARD_TIER[id]);
    }
  });
  it('the three summary tiles and five wide cards are classified as designed', () => {
    expect(DEFAULT_PANEL_ORDER.filter((id) => CARD_TIER[id] === 'tile').sort())
      .toEqual(['alerts', 'blocks', 'forecast']);
    expect(DEFAULT_PANEL_ORDER.filter((id) => CARD_TIER[id] === 'wide').sort())
      .toEqual(['cache-report', 'history', 'projects', 'sessions', 'trend']);
  });
  it('the History grid card is share-capable (it shares the daily view via keyboardShare)', () => {
    expect(SHARE_CAPABLE_PANELS.has('history')).toBe(true);
  });
});
