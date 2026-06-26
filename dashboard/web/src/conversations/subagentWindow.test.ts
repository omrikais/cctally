import { describe, it, expect } from 'vitest';
import {
  planSubagentWindow,
  centeredWindow,
  resolveSubagentAnchorIndex,
  SUBAGENT_WINDOW_CAP,
} from './subagentWindow';
import type { ConversationItem } from '../types/conversation';
import type { SubagentNode } from './groupSidechains';

// Minimal ConversationItem stub — only the fields the helpers read.
function item(uuid: string, members: string[] = []): ConversationItem {
  return { anchor: { uuid, id: 0 }, member_uuids: members.length ? members : [uuid] } as unknown as ConversationItem;
}
function items(n: number, prefix = 'm'): ConversationItem[] {
  return Array.from({ length: n }, (_, i) => item(`${prefix}${i}`));
}
function subagent(key: string, its: ConversationItem[], spawn: string | null, children: SubagentNode[] = [], depth = 1): SubagentNode {
  return { kind: 'subagent', subagentKey: key, items: its, nested: true, depth, spawnAnchorUuid: spawn, children };
}

describe('planSubagentWindow', () => {
  it('renders everything at/under cap (windowed=false)', () => {
    const p = planSubagentWindow({ itemCount: 150, anchorIndex: 0, cap: 150, revealedStart: 0, revealedEnd: 150 });
    expect(p).toEqual({ start: 0, end: 150, hiddenBefore: 0, hiddenAfter: 0, windowed: false });
  });

  it('centers a cap-window on a mid anchor over cap', () => {
    // cap 10, anchor 100 of 1000 -> start = 100 - 5 = 95
    const c = centeredWindow(1000, 100, 10);
    expect(c).toEqual({ start: 95, end: 105 });
    const p = planSubagentWindow({ itemCount: 1000, anchorIndex: 100, cap: 10, revealedStart: 95, revealedEnd: 105 });
    expect(p).toEqual({ start: 95, end: 105, hiddenBefore: 95, hiddenAfter: 895, windowed: true });
  });

  it('clamps the window at the head (anchor near 0)', () => {
    expect(centeredWindow(1000, 2, 10)).toEqual({ start: 0, end: 10 });
  });

  it('clamps the window at the tail (anchor near end)', () => {
    expect(centeredWindow(1000, 999, 10)).toEqual({ start: 990, end: 1000 });
  });

  it('grows via reveal bounds (union, never trims)', () => {
    // centered {95,105}; reveal earlier to 50 -> start 50; reveal later to 200 -> end 200
    const p = planSubagentWindow({ itemCount: 1000, anchorIndex: 100, cap: 10, revealedStart: 50, revealedEnd: 200 });
    expect(p.start).toBe(50);
    expect(p.end).toBe(200);
    expect(p.hiddenBefore).toBe(50);
    expect(p.hiddenAfter).toBe(800);
  });

  it('defensively clamps out-of-range revealed bounds / shrunk itemCount', () => {
    const p = planSubagentWindow({ itemCount: 100, anchorIndex: 0, cap: 10, revealedStart: -5, revealedEnd: 9999 });
    expect(p.start).toBe(0);
    expect(p.end).toBe(100);
  });

  it('uses the default cap constant value 150', () => {
    expect(SUBAGENT_WINDOW_CAP).toBe(150);
  });
});

describe('resolveSubagentAnchorIndex', () => {
  const own = items(200);
  it('returns the own-member index (by anchor.uuid)', () => {
    expect(resolveSubagentAnchorIndex(own, [], 'm130')).toBe(130);
  });
  it('matches a folded member_uuid', () => {
    const its = [item('a', ['a', 'a-frag']), item('b')];
    expect(resolveSubagentAnchorIndex(its, [], 'a-frag')).toBe(0);
  });
  it('returns null when the anchor is absent', () => {
    expect(resolveSubagentAnchorIndex(own, [], 'nope')).toBeNull();
    expect(resolveSubagentAnchorIndex(own, [], null)).toBeNull();
  });
  it('centers on the direct child spawn-anchor member for a child-owned anchor', () => {
    const parent = items(200, 'p');                       // p0..p199
    const child = subagent('c1', items(50, 'c'), 'p180');  // spawns after p180
    expect(resolveSubagentAnchorIndex(parent, [child], 'c25')).toBe(180);
  });
  it('returns the path direct-child spawn member for a GRANDCHILD anchor', () => {
    const parent = items(200, 'p');
    const grand = subagent('g1', items(30, 'g'), 'c10', [], 2);
    const child = subagent('c1', items(50, 'c'), 'p180', [grand]);
    // anchor g15 lives in the grandchild -> parent must center on p180 (the child's spawn)
    expect(resolveSubagentAnchorIndex(parent, [child], 'g15')).toBe(180);
  });
  it('returns null (head) when the owning child has a null spawn anchor (trailing)', () => {
    const parent = items(200, 'p');
    const child = subagent('c1', items(50, 'c'), null);   // trailing — mounts unconditionally
    expect(resolveSubagentAnchorIndex(parent, [child], 'c25')).toBeNull();
  });
});
