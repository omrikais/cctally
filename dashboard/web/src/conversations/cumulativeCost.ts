import type { ConversationItem } from '../types/conversation';

// #217 S6 F3 — cumulative assistant cost from the start of the loaded window
// through the cutoff TURN (inclusive). `cutoffUuid` is the topmost-visible turn
// (convCurrentTurnUuid); it may be a folded MEMBER uuid, so we match against each
// item's member_uuids (falling back to its own uuid). `approx` is true exactly
// when `hasPrev` — any unloaded earlier page makes this prefix-sum a lower bound,
// regardless of where the cutoff sits in the window (Codex P1). Pure; no store.
//
// #463 S2 §4.3 — the through-the-turn semantic is now DELIBERATE rather than
// incidental. `CumulativeCostChip` already claims it, in both its tooltip
// ("Cumulative cost through the current turn / session total") and its
// accessible name. But the arithmetic used to stop at the cutoff ITEM, and since
// #463 S1 made the segment the item, the figure landed on a turn boundary only
// because a turn's cost happens to sit on its FIRST segment while every follower
// holds null. Nothing pinned that ordering, so a later change to it — or to null
// handling — would have silently changed what the figure means. Grouping
// explicitly on the turn key the neutral item already carries makes the answer
// the same regardless of where in the turn the carrier sits.
//
// The turn stays the costing unit. Cost is never interpolated across segments:
// the carrier holds the whole turn's cost, every other segment holds null by
// contract, and a fabricated per-segment share is forbidden.
function turnKeyOf(item: ConversationItem): string {
  return item.turn_uuid ?? item.anchor.uuid;
}

export function cumulativeCostThrough(
  items: ConversationItem[],
  cutoffUuid: string | null,
  opts: { hasPrev: boolean },
): { cost: number; approx: boolean } {
  if (cutoffUuid == null) return { cost: 0, approx: false };
  const cutoffIndex = items.findIndex((it) => {
    const members = it.member_uuids?.length ? it.member_uuids : [it.anchor.uuid];
    return members.includes(cutoffUuid);
  });
  // An unresolvable cutoff keeps the prior behaviour: sum the whole window.
  let last = items.length - 1;
  if (cutoffIndex >= 0) {
    const turn = turnKeyOf(items[cutoffIndex]);
    last = cutoffIndex;
    for (let i = cutoffIndex + 1; i < items.length; i += 1) {
      if (turnKeyOf(items[i]) !== turn) break;
      last = i;
    }
  }
  let cost = 0;
  for (let i = 0; i <= last; i += 1) {
    const it = items[i];
    if (it.kind === 'assistant' && typeof it.cost_usd === 'number') cost += it.cost_usd;
  }
  return { cost, approx: opts.hasPrev };
}
