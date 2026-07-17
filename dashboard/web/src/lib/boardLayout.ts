// Board responsive-layout policy (#293 S1) — the single source of truth for
// which span each card gets at each viewport mode. Kept OUT of panelIds.ts
// (which must stay zero-import to break the store cycle); this leaf module may
// safely import both panelIds and breakpoints.
import { CARD_LAYOUT, type GridPanelId } from './panelIds';
import { BENTO_BREAKPOINT_PX, BOARD_WIDE_PX } from './breakpoints';

export type BoardMode = 'stack' | 'intermediate' | 'bento';

// Intermediate-only tall-row overrides: Sessions goes full-width (12) on its
// own row; Trend/Projects each take half (6) so they pair. Every other card —
// and every card in stack/bento — keeps its CARD_LAYOUT (bento base) span.
const INTERMEDIATE_TALL_SPAN: Partial<Record<GridPanelId, number>> = {
  sessions: 12,
  trend: 6,
  projects: 6,
};

export function boardMode(width: number): BoardMode {
  if (width < BENTO_BREAKPOINT_PX) return 'stack';
  if (width < BOARD_WIDE_PX) return 'intermediate';
  return 'bento';
}

export function boardSpan(id: GridPanelId, mode: BoardMode): number {
  if (mode === 'intermediate') {
    const override = INTERMEDIATE_TALL_SPAN[id];
    if (override !== undefined) return override;
  }
  return CARD_LAYOUT[id].span;
}
