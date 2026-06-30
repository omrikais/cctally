import { describe, it, expect } from 'vitest';
import { DEFAULT_PANEL_ORDER, CARD_TIER } from './panelIds';

describe('panelIds #248', () => {
  it('DEFAULT_PANEL_ORDER no longer contains current-week and has 10 entries', () => {
    expect(DEFAULT_PANEL_ORDER).not.toContain('current-week');
    expect(DEFAULT_PANEL_ORDER).toHaveLength(10);
  });
  it('CARD_TIER classifies every default panel and only into tile|wide', () => {
    for (const id of DEFAULT_PANEL_ORDER) {
      expect(['tile', 'wide']).toContain(CARD_TIER[id]);
    }
  });
  it('the five summary tiles and five wide cards are classified as designed', () => {
    expect(DEFAULT_PANEL_ORDER.filter((id) => CARD_TIER[id] === 'tile').sort())
      .toEqual(['alerts', 'blocks', 'forecast', 'monthly', 'weekly']);
    expect(DEFAULT_PANEL_ORDER.filter((id) => CARD_TIER[id] === 'wide').sort())
      .toEqual(['cache-report', 'daily', 'projects', 'sessions', 'trend']);
  });
});
