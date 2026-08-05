import type { ConversationItem } from '../types/conversation';

// #463 S2 §2.6 — which reasoning heading keys the reader must NOT render.
//
// Codex writes consecutive reasoning blocks inside one turn whose summaries are
// CUMULATIVE: a later block re-states the headings of the block before it and
// appends one or two new ones. Before S2 each of those blocks rendered as a
// single clipped line, so the repetition cost one line and was invisible. Now
// that every authored heading is its own line, the same turn renders the same
// text several times — measured worst case 170 repeated lines in a turn of 271.
//
// MEASURED, over 473 Codex conversations in a read-only copy of the production
// store (477 turns hold two or more reasoning blocks, 17,562 headings between
// them):
//
//   * 9,711 adjacent block pairs inside a turn. 6,567 (67.6%) are DISJOINT —
//     entirely new reasoning, which no rule may touch. 1,924 (19.8%) repeat the
//     previous block's list exactly. 1,214 (12.5%) extend it as a strict
//     prefix. Only 6 pairs (0.06%) merely overlap without a prefix relation.
//   * 7,155 of the 17,562 headings (40.7%) repeat a text an earlier block of
//     the same turn already rendered.
//   * Of those 7,155, all but 5 also appear in the IMMEDIATELY PRECEDING block.
//     So repetition is essentially always a cumulative re-render, not a distant
//     recurrence.
//
// The measurement therefore supports the superset hypothesis in its "identical
// or prefix-extended" form, but only for a third of adjacent pairs; two thirds
// carry genuinely new reasoning. Three candidate rules were evaluated against
// the same data and suppress within 11 headings of each other: turn-scoped
// first-occurrence-wins (7,155), adjacent-prefix-only (7,144), and
// adjacent-any-repeat (7,150). The first is implemented because it is one
// running set with no pairwise special-casing and it degrades gracefully on the
// 6 partial-overlap pairs, where a prefix rule would suppress nothing.
//
// Scope is the TURN, not the item: after S1 a turn can span several segments,
// and 1,427 adjacent block pairs cross a segment boundary. Item scope would
// have suppressed 7,137 rather than 7,155 — a difference of 18 headings across
// the whole corpus — so the choice is about stating the correct unit, not about
// the size of the effect.
//
// A repeated heading carries no information the reader loses. A reasoning block
// renders its headings and (when retained) a body, and no reasoning block in
// the corpus carries a body at all, so two occurrences of one heading text are
// indistinguishable in the rendered output.
//
// This is a RENDER-LAYER rule for the conversation envelope. The #482 find
// projector independently applies the same turn-scoped rule so it cannot
// return an occurrence the reader deliberately omits. The wire stays additive:
// the envelope still publishes every heading for legacy and outline consumers.
export function suppressedHeadingKeys(items: readonly ConversationItem[]): Set<string> {
  const occurrences: HeadingOccurrence[] = [];
  for (const item of items) {
    const turn = item.turn_uuid ?? item.anchor.uuid;
    for (const block of item.blocks) {
      if (block.kind !== 'codex_reasoning' || !block.headings?.length) continue;
      for (const heading of block.headings) {
        occurrences.push({ turn, key: heading.key, text: heading.text });
      }
    }
  }
  return suppressedFromOccurrences(occurrences);
}

// #463 S4 §4.6 — the outline needs the SAME rule over a different input. It
// holds landmarks, not `ConversationItem`s, and a second copy of the rule is
// exactly how the reader and the rail would drift apart, so the rule itself
// lives here over a shape both sides can produce.
//
// RECORDED DIVERGENCE, accepted rather than engineered away: the reader applies
// this to the currently loaded, focus-visible items while the outline applies it
// to the whole conversation, so the same function on different inputs can
// suppress different occurrences. An outline row can therefore be absent while
// the corresponding duplicate heading is visible in a partially loaded reader.
// Making the reader suppress from a full-session basis would require it to
// reason about headings it has not loaded.
export interface HeadingOccurrence {
  turn: string;
  key: string;
  text: string;
}

export function suppressedFromOccurrences(
  occurrences: readonly HeadingOccurrence[],
): Set<string> {
  const suppressed = new Set<string>();
  const seenByTurn = new Map<string, Set<string>>();
  for (const occurrence of occurrences) {
    let seen = seenByTurn.get(occurrence.turn);
    if (!seen) {
      seen = new Set<string>();
      seenByTurn.set(occurrence.turn, seen);
    }
    // Document order decides: the FIRST rendering of a text keeps its place,
    // and every later one in the same turn is dropped.
    if (seen.has(occurrence.text)) suppressed.add(occurrence.key);
    else seen.add(occurrence.text);
  }
  return suppressed;
}
