import { describe, expect, it } from 'vitest';
import { cumulativeCostThrough } from './cumulativeCost';
import type { ConversationItem } from '../types/conversation';

function a(uuid: string, cost: number, members: string[] = [uuid]): ConversationItem {
  return {
    kind: 'assistant', anchor: { session_id: 's1', uuid, id: 0 },
    member_uuids: members, ts: '', text: 't', blocks: [], model: null,
    is_sidechain: false, subagent_key: null, parent_uuid: null, cost_usd: cost,
  } as ConversationItem;
}

describe('cumulativeCostThrough', () => {
  const items = [a('u1', 0.10), a('u2', 0.20), a('u3', 0.04)];

  it('sums assistant cost from the start through the cutoff turn (inclusive)', () => {
    // toBeCloseTo on the cost (the prefix-sum is exact but 0.10+0.20 carries IEEE
    // float drift — the value, not the algorithm); approx asserted exactly.
    const through2 = cumulativeCostThrough(items, 'u2', { hasPrev: false });
    expect(through2.cost).toBeCloseTo(0.30, 9);
    expect(through2.approx).toBe(false);
    // Single item, no summation → the cost is IEEE-exact (0.10), so toEqual is
    // intentional here; contrast the 0.10+0.20 sum above, which needs toBeCloseTo.
    expect(cumulativeCostThrough(items, 'u1', { hasPrev: false })).toEqual({ cost: 0.10, approx: false });
  });
  it('matches the cutoff on a folded member uuid', () => {
    const withMember = [a('u1', 0.10), a('u2', 0.20, ['u2', 'm2b'])];
    expect(cumulativeCostThrough(withMember, 'm2b', { hasPrev: false }).cost).toBeCloseTo(0.30, 9);
  });
  it('marks approx=true whenever hasPrev (earlier pages unloaded) — even mid-window', () => {
    // cutoff is in the MIDDLE of the loaded window, not the first item: still approximate.
    const mid = cumulativeCostThrough(items, 'u2', { hasPrev: true });
    expect(mid.cost).toBeCloseTo(0.30, 9);
    expect(mid.approx).toBe(true);
  });
  it('null cutoff → 0 (nothing scrolled past yet)', () => {
    expect(cumulativeCostThrough(items, null, { hasPrev: false })).toEqual({ cost: 0, approx: false });
  });
});

// ── #463 S2 §4.3 — the turn semantic is deliberate, not incidental ──────────

describe('#463 S2 cumulativeCostThrough turn semantic', () => {
  // A turn's cost lands on ONE carrier item (segment 0); every follower segment
  // reports null by contract, because a zero is indistinguishable from a
  // genuinely free turn. Cost is never interpolated across segments.
  function carrier(turn: string, cost: number): ConversationItem {
    return {
      kind: 'assistant', anchor: { session_id: 's1', uuid: turn, id: 0 },
      member_uuids: [turn], ts: '', text: '', blocks: [], model: null,
      is_sidechain: false, subagent_key: null, parent_uuid: null,
      cost_usd: cost, turn_uuid: turn, segment_ordinal: 0,
    } as unknown as ConversationItem;
  }
  function follower(turn: string, ordinal: number): ConversationItem {
    return {
      kind: 'assistant', anchor: { session_id: 's1', uuid: `${turn}-s${ordinal}`, id: 0 },
      member_uuids: [`${turn}-s${ordinal}`], ts: '', text: '', blocks: [], model: null,
      is_sidechain: false, subagent_key: null, parent_uuid: null,
      cost_usd: null, turn_uuid: turn, segment_ordinal: ordinal,
    } as unknown as ConversationItem;
  }

  const items = [carrier('t0', 1.0), follower('t0', 1), follower('t0', 2), carrier('t1', 2.0)];

  it('reports cost through the END of the current turn, not through the segment', () => {
    // Deliberate, not incidental: today the sum lands on a turn boundary only
    // because the carrier happens to come first and followers hold null. Nothing
    // pinned that, so a later change to ordering or null handling could silently
    // make the figure mean something else. `CumulativeCostChip` already claims
    // this semantic in its tooltip and accessible name.
    expect(cumulativeCostThrough(items, 't0-s1', { hasPrev: false }).cost).toBeCloseTo(1.0, 9);
    expect(cumulativeCostThrough(items, 't0-s2', { hasPrev: false }).cost).toBeCloseTo(1.0, 9);
    expect(cumulativeCostThrough(items, 't1', { hasPrev: false }).cost).toBeCloseTo(3.0, 9);
  });

  it('reaches the whole turn even when the CARRIER follows the cutoff segment', () => {
    // The load-bearing case. If the carrier were not first, a cutoff on an
    // earlier segment of the same turn would stop the prefix sum before the
    // turn's cost and report a figure that is neither through-the-turn nor
    // through-the-segment. Grouping on the turn key makes the answer the same
    // either way.
    const carrierLast = [follower('t0', 1), carrier('t0', 1.0), carrier('t1', 2.0)];
    expect(cumulativeCostThrough(carrierLast, 't0-s1', { hasPrev: false }).cost)
      .toBeCloseTo(1.0, 9);
  });

  it('keeps the lower-bound flag when earlier pages are unloaded', () => {
    expect(cumulativeCostThrough(items, 't1', { hasPrev: true }).approx).toBe(true);
  });

  it('never interpolates a per-segment cost', () => {
    // A follower reports null, so no segment ever contributes a fabricated
    // share of its turn's cost. Both the epic's direction section and S1's spec
    // forbid that.
    const followers = items.filter((i) => i.segment_ordinal !== 0);
    // Non-vacuity: the filter must actually select the two follower segments.
    expect(followers).toHaveLength(2);
    // `cost_usd` is declared on the assistant arms of ConversationItem and not on
    // the `meta` arm, so the union is narrowed on `kind` before the field is read.
    expect(followers.every((i) => i.kind === 'assistant' && i.cost_usd === null)).toBe(true);
  });
});
