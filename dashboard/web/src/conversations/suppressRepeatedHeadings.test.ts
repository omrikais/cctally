import { describe, expect, it } from 'vitest';
import { suppressedHeadingKeys } from './suppressRepeatedHeadings';
import type { ConversationItem } from '../types/conversation';

// #463 S2 §2.6 — the cumulative-aggregate rule. See the kernel's header for the
// measurement these cases are drawn from.

function item(
  uuid: string,
  turn: string,
  blocks: { key: string; text: string }[][],
): ConversationItem {
  return {
    kind: 'assistant',
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid], ts: '', text: '', model: null,
    is_sidechain: false, subagent_key: null, parent_uuid: null, cost_usd: null,
    turn_uuid: turn,
    blocks: blocks.map((headings) => ({
      kind: 'codex_reasoning', source: 'response_item', headings,
    })),
  } as unknown as ConversationItem;
}

describe('suppressedHeadingKeys', () => {
  it('drops a cumulative re-render, keeping the first occurrence and the new tail', () => {
    // The measured majority shape: 3 headings, then those 3 plus a 4th.
    const items = [item('s0', 't0', [
      [{ key: 'b0#0', text: 'A' }, { key: 'b0#1', text: 'B' }, { key: 'b0#2', text: 'C' }],
      [{ key: 'b1#0', text: 'A' }, { key: 'b1#1', text: 'B' }, { key: 'b1#2', text: 'C' },
       { key: 'b1#3', text: 'D' }],
    ])];
    expect([...suppressedHeadingKeys(items)].sort())
      .toEqual(['b1#0', 'b1#1', 'b1#2']);
  });

  it('drops an exactly-repeated list entirely (19.8% of adjacent pairs)', () => {
    const items = [item('s0', 't0', [
      [{ key: 'b0#0', text: 'A' }, { key: 'b0#1', text: 'B' }],
      [{ key: 'b1#0', text: 'A' }, { key: 'b1#1', text: 'B' }],
    ])];
    expect([...suppressedHeadingKeys(items)].sort()).toEqual(['b1#0', 'b1#1']);
  });

  it('touches nothing when the blocks are disjoint (67.6% of adjacent pairs)', () => {
    const items = [item('s0', 't0', [
      [{ key: 'b0#0', text: 'A' }],
      [{ key: 'b1#0', text: 'B' }],
      [{ key: 'b2#0', text: 'C' }],
    ])];
    expect(suppressedHeadingKeys(items).size).toBe(0);
  });

  it('suppresses only the overlapping members of a partial overlap', () => {
    // 6 pairs of 9,711 in the corpus. A prefix-only rule would suppress nothing
    // here; first-occurrence-wins still removes the duplicate line.
    const items = [item('s0', 't0', [
      [{ key: 'b0#0', text: 'A' }, { key: 'b0#1', text: 'B' }],
      [{ key: 'b1#0', text: 'B' }, { key: 'b1#1', text: 'C' }],
    ])];
    expect([...suppressedHeadingKeys(items)]).toEqual(['b1#0']);
  });

  it('spans SEGMENTS of one turn — the scope is the turn, not the item', () => {
    const items = [
      item('s0', 't0', [[{ key: 'b0#0', text: 'A' }]]),
      item('s1', 't0', [[{ key: 'b1#0', text: 'A' }, { key: 'b1#1', text: 'B' }]]),
    ];
    expect([...suppressedHeadingKeys(items)]).toEqual(['b1#0']);
  });

  it('never suppresses across a TURN boundary', () => {
    // The same heading text in two different turns is two different pieces of
    // reasoning; only repetition WITHIN a turn is a cumulative re-render.
    const items = [
      item('s0', 't0', [[{ key: 'b0#0', text: 'A' }]]),
      item('s1', 't1', [[{ key: 'b1#0', text: 'A' }]]),
    ];
    expect(suppressedHeadingKeys(items).size).toBe(0);
  });

  it('falls back to the item key when the envelope predates turn_uuid', () => {
    const legacy = {
      kind: 'assistant', anchor: { session_id: 's', uuid: 's0', id: 0 },
      member_uuids: ['s0'], ts: '', text: '', model: null,
      is_sidechain: false, subagent_key: null, parent_uuid: null, cost_usd: null,
      blocks: [
        { kind: 'codex_reasoning', source: 'response_item', headings: [{ key: 'b0#0', text: 'A' }] },
        { kind: 'codex_reasoning', source: 'response_item', headings: [{ key: 'b1#0', text: 'A' }] },
      ],
    } as unknown as ConversationItem;
    expect([...suppressedHeadingKeys([legacy])]).toEqual(['b1#0']);
  });
});
