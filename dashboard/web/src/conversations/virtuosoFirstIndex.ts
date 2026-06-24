// The Virtuoso `firstItemIndex` accounting (#232). Virtuoso speaks a "virtual"
// index space: virtualIndex = firstItemIndex + arrayIndex, where arrayIndex is
// the position in the reader's `nodes` array. `firstItemIndex` must move so that
// data[0] keeps a stable virtual index across head mutations, which is how
// Virtuoso pins the viewport over a reverse-paging prepend.
//
// Only the HEAD of the array matters: a prepend pushes data[0] earlier (index
// must DROP by the count prepended); a head-trim pushes data[0] later (index
// must RISE by the count trimmed). Appends and tail-trims leave data[0] fixed.
// (Codex P0-2/P0-3: this is the single source of truth for the offset; it lives
// in useConversation's state, updated atomically with detail.items.)
export const VIRTUAL_INDEX_BASE = 1_000_000;

export interface HeadDelta {
  addedTop: number;    // items prepended at the head (WindowOp.addedTop)
  droppedTop: number;  // items trimmed from the head (WindowOp.droppedTop)
}

export function applyFirstItemDelta(prev: number, delta: HeadDelta): number {
  const next = prev - delta.addedTop + delta.droppedTop;
  return next < 0 ? 0 : next;
}
