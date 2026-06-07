import { describe, expect, it } from 'vitest';
import { groupSidechains } from './groupSidechains';
import type { ConversationItem } from '../types/conversation';

function item(uuid: string, sc: boolean): ConversationItem {
  return { kind: 'human', anchor: { session_id: 's', uuid, id: 0 }, member_uuids: [uuid], ts: 't', text: uuid, blocks: [], is_sidechain: sc };
}

describe('groupSidechains', () => {
  it('passes non-sidechain items through as single entries', () => {
    const out = groupSidechains([item('a', false), item('b', false)]);
    expect(out).toEqual([
      { kind: 'item', item: expect.objectContaining({ anchor: expect.objectContaining({ uuid: 'a' }) }) },
      { kind: 'item', item: expect.objectContaining({ anchor: expect.objectContaining({ uuid: 'b' }) }) },
    ]);
  });

  it('collapses a contiguous run of sidechain items into one group', () => {
    const out = groupSidechains([item('a', false), item('s1', true), item('s2', true), item('b', false)]);
    expect(out).toHaveLength(3);
    expect(out[0]).toMatchObject({ kind: 'item' });
    expect(out[1]).toMatchObject({ kind: 'sidechain' });
    expect((out[1] as { kind: 'sidechain'; items: ConversationItem[] }).items).toHaveLength(2);
    expect(out[2]).toMatchObject({ kind: 'item' });
  });

  it('a non-sidechain item breaks the run into two groups', () => {
    const out = groupSidechains([item('s1', true), item('x', false), item('s2', true)]);
    expect(out.map((g) => g.kind)).toEqual(['sidechain', 'item', 'sidechain']);
  });

  it('returns an empty list for empty input', () => {
    expect(groupSidechains([])).toEqual([]);
  });

  it('collapses an all-sidechain run into a single group', () => {
    const out = groupSidechains([item('s1', true), item('s2', true), item('s3', true)]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: 'sidechain' });
    expect((out[0] as { kind: 'sidechain'; items: ConversationItem[] }).items).toHaveLength(3);
  });
});
