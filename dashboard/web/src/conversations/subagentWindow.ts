// #239 — pure helpers for internally windowing a large subagent thread's
// rendered members. Mirrors windowedCap.ts: pure + side-effect-free, so the
// window math is unit-testable in isolation, decoupled from the React commit.
// The reader/SidechainGroup integration lives elsewhere.

import type { ConversationItem } from '../types/conversation';
import type { SubagentNode } from './groupSidechains';

// Soft render cap in MEMBERS. A thread with <= CAP members renders fully (the
// unchanged common case — ~95%+ of real threads per the #239 size measurement).
// Beyond CAP the body renders a CAP-sized window centered on the anchor.
export const SUBAGENT_WINDOW_CAP = 150;
// "Show N earlier/later" reveal chunk size, in members.
export const SUBAGENT_WINDOW_CHUNK = 100;

export interface PlanSubagentWindowInput {
  itemCount: number;
  anchorIndex: number;
  cap: number;
  revealedStart: number;
  revealedEnd: number;
}

export interface SubagentWindowPlan {
  start: number;        // inclusive slice start
  end: number;          // exclusive slice end
  hiddenBefore: number; // members hidden above the window
  hiddenAfter: number;  // members hidden below the window
  windowed: boolean;    // false when itemCount <= cap (render everything)
}

function clamp(n: number, lo: number, hi: number): number {
  return n < lo ? lo : n > hi ? hi : n;
}

// The centered cap-window for an anchor (no manual reveal). Always spans `cap`
// when over cap; clamps inward at the head/tail. Even-cap safe.
export function centeredWindow(itemCount: number, anchorIndex: number, cap: number): { start: number; end: number } {
  if (itemCount <= cap || cap <= 0) return { start: 0, end: itemCount };
  const a = clamp(anchorIndex, 0, itemCount - 1);
  const start = clamp(a - Math.floor(cap / 2), 0, Math.max(0, itemCount - cap));
  return { start, end: Math.min(itemCount, start + cap) };
}

// The effective window = the centered window UNIONed with the (clamped) manual
// reveal bounds, so a reveal only grows it, never trims. Defensive clamping
// keeps a stale `revealed*` (after a session reset) or live-tail growth from
// producing an out-of-range slice.
export function planSubagentWindow(input: PlanSubagentWindowInput): SubagentWindowPlan {
  const { itemCount, anchorIndex, cap, revealedStart, revealedEnd } = input;
  if (itemCount <= cap || cap <= 0) {
    return { start: 0, end: itemCount, hiddenBefore: 0, hiddenAfter: 0, windowed: false };
  }
  const c = centeredWindow(itemCount, anchorIndex, cap);
  const start = Math.min(c.start, clamp(revealedStart, 0, itemCount));
  const end = Math.max(c.end, clamp(revealedEnd, 0, itemCount));
  return { start, end, hiddenBefore: start, hiddenAfter: itemCount - end, windowed: true };
}

// Path-aware anchor resolution (Codex P0). Returns the index in `items` of the
// member to center the window on so that `anchorUuid` — which may live in this
// thread OR any descendant subtree — becomes reachable:
//   1. anchorUuid is one of this group's members (anchor.uuid OR member_uuids,
//      covering a folded fragment) -> that member's index.
//   2. anchorUuid is owned by a descendant subtree -> the index of the member
//      that is the DIRECT child's spawnAnchorUuid on the path to it, so the
//      child card mounts (it renders only after its in-window spawn-anchor
//      member). The child then resolves its own anchor recursively. A descendant
//      child with a null spawnAnchorUuid renders in trailingChildren (mounts
//      unconditionally), so it imposes no window constraint -> skip it.
//   3. otherwise -> null (caller defaults to head, index 0).
export function resolveSubagentAnchorIndex(
  items: ConversationItem[],
  children: SubagentNode[],
  anchorUuid: string | null | undefined,
): number | null {
  if (anchorUuid == null) return null;
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.anchor.uuid === anchorUuid || it.member_uuids.includes(anchorUuid)) return i;
  }
  for (const child of children) {
    if (child.spawnAnchorUuid == null) continue;
    if (!subtreeContains(child, anchorUuid)) continue;
    const idx = items.findIndex((it) => it.anchor.uuid === child.spawnAnchorUuid);
    if (idx >= 0) return idx;
  }
  return null;
}

function subtreeContains(node: SubagentNode, anchorUuid: string): boolean {
  for (const it of node.items) {
    if (it.anchor.uuid === anchorUuid || it.member_uuids.includes(anchorUuid)) return true;
  }
  for (const c of node.children) if (subtreeContains(c, anchorUuid)) return true;
  return false;
}
