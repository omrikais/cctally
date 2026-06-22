import type { ConversationItem } from '../types/conversation';

// #217 S6 F3 — cumulative assistant cost from the start of the loaded window
// through the cutoff turn (inclusive). `cutoffUuid` is the topmost-visible turn
// (convCurrentTurnUuid); it may be a folded MEMBER uuid, so we match against each
// item's member_uuids (falling back to its own uuid). `approx` is true exactly
// when `hasPrev` — any unloaded earlier page makes this prefix-sum a lower bound,
// regardless of where the cutoff sits in the window (Codex P1). Pure; no store.
export function cumulativeCostThrough(
  items: ConversationItem[],
  cutoffUuid: string | null,
  opts: { hasPrev: boolean },
): { cost: number; approx: boolean } {
  if (cutoffUuid == null) return { cost: 0, approx: false };
  let cost = 0;
  for (const it of items) {
    const members = it.member_uuids?.length ? it.member_uuids : [it.anchor.uuid];
    if (it.kind === 'assistant' && typeof it.cost_usd === 'number') cost += it.cost_usd;
    if (members.includes(cutoffUuid)) break;
  }
  return { cost, approx: opts.hasPrev };
}
