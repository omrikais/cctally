import type { OutlineLandmark, OutlineLandmarkKind, OutlineTurn } from '../types/conversation';

// #463 S4 §3.3/§6.1 — the one place tier 1 and tier 2 are joined.
//
// `ConversationOutline.turns` stays tier-1 only, because `stats.turns.*`,
// `positionByKey`, reading-position restore and S1's deep-link resolution all
// assume one entry per turn. Everything that needs the two tiers together —
// the adapter's retention rule, the rendered outline, and the jump targets —
// derives it here, so those three can never disagree about which turn owns a
// landmark or where a landmark sits in the rail.
//
// Pure: no React, no store, no DOM.

// Which tier-1 turns own at least one landmark. The adapter's retention rule
// reads this BEFORE it filters, because the navigation filter drops every
// event-bearing non-compaction turn and S1 measured that all 589 multi-segment
// Codex turns in the corpus are exactly those — so without the rule every
// landmark is parented to a turn that is not in the list.
export function landmarkOwners(landmarks: readonly OutlineLandmark[]): Set<string> {
  return new Set(landmarks.map((landmark) => landmark.parent_uuid));
}

// Owner turn key → its landmarks, in wire order. Wire order is PHYSICAL order:
// §3.4 records that `timestamp_utc` is monotone within a turn only, so nothing
// here sorts by it.
export function landmarksByOwner(
  landmarks: readonly OutlineLandmark[],
): Map<string, OutlineLandmark[]> {
  const byOwner = new Map<string, OutlineLandmark[]>();
  for (const landmark of landmarks) {
    const bucket = byOwner.get(landmark.parent_uuid);
    if (bucket) bucket.push(landmark);
    else byOwner.set(landmark.parent_uuid, [landmark]);
  }
  return byOwner;
}

// #463 S4 F-A — the key of the element INSIDE the owning item that a landmark
// names, which is what a jump aligns once the item is loaded. A reasoning
// heading is addressed by its own `landmark_key`, because `<block_key>#<ordinal>`
// is exactly the identity the reader renders as `data-heading-key` on that one
// heading; every other kind is addressed by the physical row's `block_key`,
// which the reader renders as `data-block-key` on that row's rendered block.
export function landmarkAnchorKey(landmark: OutlineLandmark): string {
  return landmark.kind === 'reasoning' ? landmark.landmark_key : landmark.block_key;
}

// The landmark families `deriveOutline` and `buildOutlineTargets` already have
// a vocabulary for. A reasoning heading is the same kind of row a
// Markdown-heading-led Claude turn produces, which is why it maps onto
// `heading` rather than introducing a fourth glyph for the same idea.
export const LANDMARK_ENTRY_TYPE: Record<OutlineLandmarkKind, 'error' | 'plan' | 'heading'> = {
  tool_error: 'error',
  plan: 'plan',
  reasoning: 'heading',
};

export interface MergedLandmark {
  landmark: OutlineLandmark;
  // Index into the TIER-1 `turns` array this was merged against — never into
  // the merged list. Every consumer that needs a whole `OutlineTurn` (the
  // focus-mode visibility test, the reader's turn lookup) dereferences it
  // there, which is what lets the target representation carry an anchor and its
  // owner without the two arrays having to be the same length (§6.3).
  ownerTurnIndex: number;
}

// Landmarks in rail order: grouped under their owning turn, and each group in
// wire order. A landmark whose owner is absent from `turns` is DROPPED rather
// than floated to the top — it has no place to indent under, and the retention
// rule exists so this does not happen; surviving it silently is the graceful
// half, and the outline golden is what would show it happening.
export function mergeLandmarks(
  turns: readonly OutlineTurn[],
  landmarks: readonly OutlineLandmark[] | undefined,
): MergedLandmark[] {
  if (!landmarks?.length) return [];
  const byOwner = landmarksByOwner(landmarks);
  const merged: MergedLandmark[] = [];
  turns.forEach((turn, ownerTurnIndex) => {
    for (const landmark of byOwner.get(turn.uuid) ?? []) {
      merged.push({ landmark, ownerTurnIndex });
    }
  });
  return merged;
}
