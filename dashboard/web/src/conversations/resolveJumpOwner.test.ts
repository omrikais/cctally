// #234 §2.3-1 — render-tree owner resolution for the R2 force-open. Replaces the
// flat detail.items.find(...member_uuids.includes) lookup that failed to
// force-open the enclosing subagent card (measured: no conv-sidechain--force
// across 109 mounted samples). Pure tree walk — no DOM.
import { describe, expect, it } from 'vitest';
import { resolveJumpOwner } from './resolveJumpOwner';
import type { TimedNode } from './insertTimeMarkers';

const item = (uuid: string, members: string[] = []) =>
  ({ anchor: { uuid }, member_uuids: [uuid, ...members] }) as any;

const nodes: TimedNode[] = [
  { kind: 'item', item: item('top-1') } as any,
  {
    kind: 'subagent', subagentKey: 'sk-root', items: [item('card-root'), item('card-2', ['m2a'])],
    children: [{ subagentKey: 'sk-child', items: [item('child-1')], children: [] } as any],
  } as any,
];

describe('resolveJumpOwner', () => {
  it('returns null owner for a top-level item', () => {
    expect(resolveJumpOwner(nodes, 'top-1')).toEqual({ ownerSubagentKey: null, rootUuid: 'top-1', isCardRoot: false });
  });
  it('resolves a nested second-member to its owning subagent (the #234 R2 topology)', () => {
    expect(resolveJumpOwner(nodes, 'm2a')).toEqual({ ownerSubagentKey: 'sk-root', rootUuid: 'card-root', isCardRoot: false });
  });
  it('flags the card root', () => {
    expect(resolveJumpOwner(nodes, 'card-root')).toEqual({ ownerSubagentKey: 'sk-root', rootUuid: 'card-root', isCardRoot: true });
  });
  it('resolves a grandchild to the deepest owning child key, with the top-level root uuid', () => {
    expect(resolveJumpOwner(nodes, 'child-1')).toEqual({ ownerSubagentKey: 'sk-child', rootUuid: 'card-root', isCardRoot: false });
  });
  it('returns null when the uuid is absent', () => {
    expect(resolveJumpOwner(nodes, 'nope')).toBeNull();
  });
});
