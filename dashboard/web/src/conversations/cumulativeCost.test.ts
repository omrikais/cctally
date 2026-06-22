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
