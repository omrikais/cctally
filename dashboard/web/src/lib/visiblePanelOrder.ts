// #294 S5 — the derived visible-panel order (§6.11). Pure functions only.
//
// One derived list — the persisted full panel order filtered through §5.5 for
// the active source — is the single list consumed by the App grid, DnD rows,
// digit shortcuts (openPanelByPosition), focus movement, Help's panel listing,
// and share affordances. The persisted full order is NEVER rewritten by a source
// switch; a DnD reorder in a filtered view maps its result back into the full
// order, preserving hidden panels' relative positions.

import type { GridPanelId } from './panelIds';
import { isPanelVisible } from './sourceGating';
import type { SourceView } from '../store/sourceView';

// Filter the persisted full order down to the panels visible for the active
// source. Returns a NEW array — the caller's `order` is never mutated.
export function deriveVisiblePanelOrder(
  order: GridPanelId[],
  view: SourceView,
): GridPanelId[] {
  return order.filter((panel) => isPanelVisible(view, panel));
}

// Map a reorder performed on the FILTERED (visible) list back into the full
// order, holding every hidden panel at its exact index. `visibleBefore` is the
// visible subsequence of `full`; `visibleAfter` is a permutation of it after the
// reorder. Each visible slot in `full` is refilled, in order, from
// `visibleAfter`; hidden panels keep their positions.
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
