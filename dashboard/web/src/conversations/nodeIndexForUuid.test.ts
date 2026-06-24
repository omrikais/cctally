import { describe, it, expect } from 'vitest';
import { nodeIndexForUuid } from './nodeIndexForUuid';
import type { TimedNode } from './insertTimeMarkers';

// Minimal node fixtures — only the fields nodeIndexForUuid reads.
const item = (uuid: string, members: string[] = []): TimedNode =>
  ({ kind: 'item', item: { anchor: { uuid }, member_uuids: members } } as unknown as TimedNode);
const marker = (): TimedNode =>
  ({ kind: 'time_marker', gapSeconds: null, dayLabel: 'Jun 24', key: 'tm-0-x' } as TimedNode);
const subagent = (rootUuid: string, childUuid: string): TimedNode =>
  ({ kind: 'subagent', subagentKey: 'k', items: [{ anchor: { uuid: rootUuid }, member_uuids: [] }],
     children: [{ kind: 'subagent', subagentKey: 'k2',
       items: [{ anchor: { uuid: childUuid }, member_uuids: [] }], children: [] }] } as unknown as TimedNode);

describe('nodeIndexForUuid', () => {
  it('returns array + virtual index for a plain item match', () => {
    const nodes = [marker(), item('a'), item('b')];
    expect(nodeIndexForUuid(nodes, 'b', 1_000_000)).toEqual({ arrayIndex: 2, virtualIndex: 1_000_002 });
  });
  it('matches a folded member uuid to its containing item', () => {
    const nodes = [item('a', ['a', 'a-frag']), item('b')];
    expect(nodeIndexForUuid(nodes, 'a-frag', 0)).toEqual({ arrayIndex: 0, virtualIndex: 0 });
  });
  it('matches a uuid nested inside a subagent child to the top-level node', () => {
    const nodes = [item('a'), subagent('root', 'deep')];
    expect(nodeIndexForUuid(nodes, 'deep', 10)).toEqual({ arrayIndex: 1, virtualIndex: 11 });
  });
  it('returns null when absent', () => {
    expect(nodeIndexForUuid([item('a')], 'zzz', 0)).toBeNull();
  });
  it('never matches a time_marker', () => {
    expect(nodeIndexForUuid([marker()], 'tm-0-x', 0)).toBeNull();
  });
});
