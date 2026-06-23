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
  // Soft cap in ITEMS (page-alignment is the caller's concern). When the loaded
  // window exceeds this, the far edge is trimmed back toward it.
  cap: number;
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

export function planTrim(input: PlanTrimInput): TrimPlan {
  const { items, op, cap, protectedUuids, fetchInFlight } = input;
  // Never trim mid-fetch, on a window reset, or when already within the cap.
  if (fetchInFlight || op === 'reset' || items.length <= cap) return NO_TRIM(items);

  if (op === 'prepend') {
    // Scrolling UP — drop the far BOTTOM. Keep the top `cap` items, but extend the
    // kept region downward past `cap` if a protected item sits in the drop zone
    // (we must keep through the LAST protected item at/after `cap`).
    let keepCount = cap;
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
  // amount needed to reach `cap` (keep the bottom `cap`).
  const want = items.length - cap;
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
