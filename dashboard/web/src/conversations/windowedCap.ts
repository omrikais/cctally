// #228 S3 B3 — the pure trim helper for the hand-rolled windowed DOM cap. Keeps
// the reader's DOM bounded on a very long transcript by dropping the FAR edge
// (the one OPPOSITE the scroll direction) once the loaded window exceeds a soft
// cap. Pure + side-effect-free so the cap logic is unit-testable in isolation,
// decoupled from the React commit (the hook applies the plan a tick AFTER the
// paging op's anchor-restore / stick has settled — never in the same commit,
// which is the Codex P0 hazard).
//
// Safety properties:
//   • Trim the edge OPPOSITE the op — a prepend (scrolling up) drops the bottom;
//     an append / live-tail (scrolling down) drops the top. The viewport sits
//     near the edge being paged TOWARD, so the far-edge drop is off-screen and
//     can't perturb the visible scroll position.
//   • NEVER drop a protected uuid (the active find match, the current/pinned
//     turn, an in-flight jump target). The trim stops short of any page holding
//     one, trimming less that round. Correctness wins over the cap.
//   • No-op when a fetch is in flight, on a reset op, or under/at the cap.
//   • Reset only the OPPOSITE edge cursor — a bottom-drop re-arms the bottom
//     cursor (so scroll-down re-fetches the dropped tail) and leaves the top edge
//     untouched, mirroring the hook's "a `before` page never touches the bottom
//     edge" invariant. Vice-versa for a top-drop.

import type { ConversationItem } from '../types/conversation';

export interface PlanTrimInput {
  items: ConversationItem[];
  op: 'prepend' | 'append' | 'reset';
  // #463 S1 — the PRIMARY soft cap, in BLOCKS. Blocks are what correlate with
  // DOM node count and render work, and until segmentation the cap was item-count
  // arithmetic with no concept of size, so one 827-block Codex turn counted as a
  // single item and the trim had never fired on a Codex conversation at all.
  capBlocks: number;
  // The SECONDARY soft cap, in ITEMS (page-alignment is the caller's concern).
  // It binds for the many-tiny-items case, where the block budget never would.
  capItems: number;
  // uuids that must survive the trim — matched against each item's anchor.uuid
  // AND its member_uuids (a protected uuid can be a folded fragment).
  protectedUuids: Set<string>;
  // When true the window is mid-fetch (loadMore / loadPrev / pollTail /
  // loadToTarget) — never trim then, so a trim can't race an in-progress page
  // apply or the live-tail overlap re-fetch.
  fetchInFlight: boolean;
}

export interface TrimPlan {
  // The items to keep (a contiguous slice of the input). Reference-equal to the
  // input array when nothing is dropped (a cheap no-op signal for the caller).
  keep: ConversationItem[];
  droppedTop: number;
  droppedBottom: number;
  // The new TOP-edge cursor (anchor.id of the first kept item) when the top was
  // trimmed — feeds the hook's prevBeforeRef so scroll-up re-fetches; null when
  // the top was not trimmed.
  resetTopCursorTo: number | null;
  // The new BOTTOM-edge cursor (anchor.id of the last kept item) when the bottom
  // was trimmed — feeds the hook's nextAfterRef so scroll-down re-fetches; null
  // when the bottom was not trimmed.
  resetBottomCursorTo: number | null;
}

// An item with no `blocks` array costs no blocks — the item ceiling still binds
// for it. Some legacy Claude fixtures build items that way.
function blockCount(it: ConversationItem): number {
  return it.blocks?.length ?? 0;
}

function isProtected(it: ConversationItem, protectedUuids: Set<string>): boolean {
  if (protectedUuids.size === 0) return false;
  if (protectedUuids.has(it.anchor.uuid)) return true;
  for (const u of it.member_uuids) if (protectedUuids.has(u)) return true;
  return false;
}

const NO_TRIM = (items: ConversationItem[]): TrimPlan => ({
  keep: items,
  droppedTop: 0,
  droppedBottom: 0,
  resetTopCursorTo: null,
  resetBottomCursorTo: null,
});

// How many items fit within BOTH budgets, counted from one end.
//
// `fromTop` walks forward from index 0 (what a prepend keeps); otherwise it
// walks backward from the last item (what an append keeps). At least one item
// is always kept, however large it is, so an oversized single turn is still
// rendered rather than trimmed to nothing.
function fitCount(items: ConversationItem[], input: PlanTrimInput, fromTop: boolean): number {
  const { capBlocks, capItems } = input;
  let blocks = 0;
  let kept = 0;
  for (let step = 0; step < items.length; step++) {
    const item = items[fromTop ? step : items.length - 1 - step];
    const next = blocks + blockCount(item);
    if (kept > 0 && (next > capBlocks || kept + 1 > capItems)) break;
    blocks = next;
    kept += 1;
  }
  return kept;
}

export function planTrim(input: PlanTrimInput): TrimPlan {
  const { items, op, capBlocks, capItems, protectedUuids, fetchInFlight } = input;
  // Never trim mid-fetch, on a window reset, or when already within BOTH budgets.
  if (fetchInFlight || op === 'reset') return NO_TRIM(items);
  let totalBlocks = 0;
  for (const item of items) totalBlocks += blockCount(item);
  if (items.length <= capItems && totalBlocks <= capBlocks) return NO_TRIM(items);

  if (op === 'prepend') {
    // Scrolling UP — drop the far BOTTOM. Keep as much of the top as fits both
    // budgets, but extend the kept region downward past that point if a
    // protected item sits in the drop zone (we must keep through the LAST
    // protected item at or after the fit boundary).
    let keepCount = fitCount(items, input, true);
    for (let i = items.length - 1; i >= keepCount; i--) {
      if (isProtected(items[i], protectedUuids)) { keepCount = i + 1; break; }
    }
    if (keepCount >= items.length) return NO_TRIM(items);
    const keep = items.slice(0, keepCount);
    return {
      keep,
      droppedTop: 0,
      droppedBottom: items.length - keepCount,
      resetTopCursorTo: null,
      resetBottomCursorTo: keep[keep.length - 1].anchor.id,
    };
  }

  // op === 'append' — scrolling DOWN, drop the far TOP. The largest droppable
  // prefix is up to (but not including) the first protected item, capped at the
  // amount needed to bring the kept BOTTOM within both budgets.
  const want = items.length - fitCount(items, input, false);
  let dropTop = want;
  for (let i = 0; i < want; i++) {
    if (isProtected(items[i], protectedUuids)) { dropTop = i; break; }
  }
  if (dropTop <= 0) return NO_TRIM(items);
  const keep = items.slice(dropTop);
  return {
    keep,
    droppedTop: dropTop,
    droppedBottom: 0,
    resetTopCursorTo: keep[0].anchor.id,
    resetBottomCursorTo: null,
  };
}
