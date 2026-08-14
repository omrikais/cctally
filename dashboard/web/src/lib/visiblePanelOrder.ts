// Canonical ten-card order shared by Claude, Codex, and All. Pure functions only.
// Source selection changes card contents, never board membership, digit
// positions, or persisted order.

import type { GridPanelId } from './panelIds';
import type { SourceView } from '../store/sourceView';

// Return a NEW canonical-order array; the caller's `order` is never mutated.
//
// #556 S4 — READ THE SIGNATURE AS DOCUMENTED, NOT AS IMPLIED. The `view`
// parameter is deliberately unused and no source filtering exists anywhere in
// this module. There is ONE persisted ten-card order; this function returns it
// verbatim for every source selection, and `mapVisibleReorderToFull` therefore
// maps a complete canonical permutation rather than a filtered subsequence.
// The parameter is retained because six call sites and two persistence
// reducers pass it, and because callers depend on the fresh-array copy; its
// presence is the only thing in the code that still suggests a filtered board,
// which is why it is named here. Full deletion of this seam is filed as its
// own cleanup.
export function deriveVisiblePanelOrder(
  order: GridPanelId[],
  view: SourceView,
): GridPanelId[] {
  // Visual parity contract: every source selection owns the same canonical
  // ten-card board. Capability differences render inside the card as honest
  // empty/unavailable states; they never remove a shell and reflow the bento.
  void view;
  return [...order];
}

// Map a reorder back into the persisted full order. The visible arguments keep
// the historical API shape used by DnD, but the fixed board makes them complete
// canonical permutations rather than source-filtered subsequences.
export function mapVisibleReorderToFull(
  full: GridPanelId[],
  visibleBefore: GridPanelId[],
  visibleAfter: GridPanelId[],
): GridPanelId[] {
  const visibleSet = new Set(visibleBefore);
  let cursor = 0;
  return full.map((id) =>
    visibleSet.has(id) && cursor < visibleAfter.length ? visibleAfter[cursor++] : id,
  );
}
