import { describe, expect, it } from 'vitest';
import { groupSidechains, type RenderNode } from './groupSidechains';
import type { ConversationItem } from '../types/conversation';

function mk(
  uuid: string,
  opts: { subagentKey?: string | null; parentUuid?: string | null } = {},
): ConversationItem {
  const subagent_key = opts.subagentKey ?? null;
  return {
    kind: 'human',
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid],
    ts: 't',
    text: uuid,
    blocks: [],
    is_sidechain: subagent_key != null,
    subagent_key,
    parent_uuid: opts.parentUuid ?? null,
  };
}

function group(n: RenderNode) {
  if (n.kind !== 'subagent') throw new Error('expected subagent node');
  return n;
}

describe('groupSidechains', () => {
  it('passes main items (null subagent_key) through as item nodes', () => {
    const out = groupSidechains([mk('a'), mk('b')]);
    expect(out.map((n) => n.kind)).toEqual(['item', 'item']);
  });

  it('groups one subagent file into a single subagent node at its first member', () => {
    const out = groupSidechains([mk('h'), mk('s1', { subagentKey: 'k1' }), mk('s2', { subagentKey: 'k1' }), mk('h2')]);
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent', 'item']);
    const g = group(out[1]);
    expect(g.subagentKey).toBe('k1');
    expect(g.items.map((i) => i.anchor.uuid)).toEqual(['s1', 's2']);
    expect(g.nested).toBe(false);
  });

  it('separates PARALLEL interleaved subagents into distinct groups (the core fix)', () => {
    // A,B,A,B interleave: the OLD contiguous-run logic fused these into ONE
    // group. subagent_key grouping must yield TWO groups.
    const out = groupSidechains([
      mk('a1', { subagentKey: 'A' }),
      mk('b1', { subagentKey: 'B' }),
      mk('a2', { subagentKey: 'A' }),
      mk('b2', { subagentKey: 'B' }),
    ]);
    const subs = out.filter((n) => n.kind === 'subagent').map((n) => group(n));
    expect(subs.map((g) => g.subagentKey)).toEqual(['A', 'B']); // document order, distinct
    expect(subs[0].items.map((i) => i.anchor.uuid)).toEqual(['a1', 'a2']);
    expect(subs[1].items.map((i) => i.anchor.uuid)).toEqual(['b1', 'b2']);
  });

  it('nests a group under a main item when the root parent_uuid resolves to it', () => {
    const main = mk('m1');                                  // main item, uuid m1
    const root = mk('c1', { subagentKey: 'C', parentUuid: 'm1' });
    const out = groupSidechains([main, root, mk('c2', { subagentKey: 'C' })]);
    // main item emitted, then the nested group right after it.
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent']);
    const g = group(out[1]);
    expect(g.nested).toBe(true);
    expect(g.items.map((i) => i.anchor.uuid)).toEqual(['c1', 'c2']);
  });

  it('nests under a parent that appears AFTER the bucket in document order (nesting beats position)', () => {
    // The subagent rows precede their parent main item in time, but a nested
    // group emits UNDER its parent — not at the bucket's first-member position.
    // Locks the "nesting wins over document position" placement invariant.
    const out = groupSidechains([
      mk('c1', { subagentKey: 'C', parentUuid: 'm1' }),
      mk('c2', { subagentKey: 'C' }),
      mk('m1'),
    ]);
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent']);
    const first = out[0];
    if (first.kind !== 'item') throw new Error('expected item node');
    expect(first.item.anchor.uuid).toBe('m1');
    const g = group(out[1]);
    expect(g.nested).toBe(true);
    expect(g.items.map((i) => i.anchor.uuid)).toEqual(['c1', 'c2']);
  });

  it('does NOT nest when the root parent_uuid does not resolve to a loaded main item', () => {
    const out = groupSidechains([mk('h'), mk('c1', { subagentKey: 'C', parentUuid: 'missing' })]);
    expect(group(out[1]).nested).toBe(false);
  });

  it('emits an orphan bucket via the final sweep (no dropped items)', () => {
    // A nested-classified bucket whose parent main item is absent from items
    // must still be emitted (defensive: the parent map can only resolve loaded
    // mains, so this stays non-nested and surfaces in the sweep).
    const out = groupSidechains([mk('c1', { subagentKey: 'C', parentUuid: 'nope' })]);
    expect(out).toHaveLength(1);
    expect(group(out[0]).items.map((i) => i.anchor.uuid)).toEqual(['c1']);
  });

  it('returns an empty list for empty input', () => {
    expect(groupSidechains([])).toEqual([]);
  });
});
