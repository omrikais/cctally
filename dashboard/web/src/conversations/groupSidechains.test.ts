import { describe, expect, it } from 'vitest';
import { groupSidechains, flattenSubagents, walkSubagents, type RenderNode, type SubagentNode } from './groupSidechains';
import type { ConversationItem, SubagentMeta } from '../types/conversation';

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

// #164: a run of >=2 consecutive top-level orphan tool_result items collapses
// into one `tool_result_run` node (the residual bare-result texture). A lone
// orphan stays a plain item node.
function tr(uuid: string): ConversationItem {
  return {
    kind: 'tool_result',
    anchor: { session_id: 's', uuid, id: 1 },
    member_uuids: [uuid],
    ts: '',
    text: '',
    blocks: [],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
  };
}

describe('collapseToolResultRuns (orphan tool_result runs)', () => {
  it('collapses a run of >=2 orphan tool_result items into one node', () => {
    const nodes = groupSidechains([tr('u1'), tr('u2'), tr('u3')]);
    const run = nodes.find((n) => n.kind === 'tool_result_run');
    expect(run).toBeTruthy();
    if (run!.kind !== 'tool_result_run') throw new Error('expected tool_result_run');
    expect(run!.items.map((i) => i.anchor.uuid)).toEqual(['u1', 'u2', 'u3']);
  });

  it('a single orphan tool_result stays an item node', () => {
    const nodes = groupSidechains([tr('u1')]);
    expect(nodes.every((n) => n.kind !== 'tool_result_run')).toBe(true);
    expect(nodes).toHaveLength(1);
    expect(nodes[0].kind).toBe('item');
  });

  it('a non-tool_result item between two orphans breaks the run', () => {
    const nodes = groupSidechains([tr('u1'), mk('h'), tr('u2')]);
    // No run of >=2 contiguous results → no run node; all three are items.
    expect(nodes.every((n) => n.kind !== 'tool_result_run')).toBe(true);
    expect(nodes.map((n) => n.kind)).toEqual(['item', 'item', 'item']);
  });

  it('collapses only the contiguous orphan run, leaving surrounding items intact', () => {
    const nodes = groupSidechains([mk('h1'), tr('u1'), tr('u2'), mk('h2')]);
    expect(nodes.map((n) => n.kind)).toEqual(['item', 'tool_result_run', 'item']);
    const run = nodes[1];
    if (run.kind !== 'tool_result_run') throw new Error('expected tool_result_run');
    expect(run.items.map((i) => i.anchor.uuid)).toEqual(['u1', 'u2']);
  });
});

// §5 — recursive nesting tree from the kernel's read-time linkage. A subagent's
// parent is resolved from subagent_meta[k].parent_subagent_key (+ spawn_uuid):
// a parent that is another LOADED subagent nests inside that parent's `children`
// at depth+1; a null/main parent nests at its spawn_uuid main anchor. Legacy
// transcripts (no kernel linkage) fall back to root.parent_uuid. No bucket is
// ever dropped.
function smeta(over: Partial<SubagentMeta> & { kind: string }): SubagentMeta {
  return { ...over };
}

describe('groupSidechains — recursive nesting (§5 kernel linkage)', () => {
  it('nests a grandchild as a SubagentNode inside the child node.children at depth 1', () => {
    // main turn m1 spawns child C (parent=main, anchor m1); child C spawns
    // grandchild G (parent=C, anchor c1, a child-thread item).
    const items = [
      mk('m1'),
      mk('c1', { subagentKey: 'C' }),
      mk('g1', { subagentKey: 'G' }),
    ];
    const meta: Record<string, SubagentMeta> = {
      C: smeta({ kind: 'code-reviewer', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_c' }),
      G: smeta({ kind: 'grounding', parent_subagent_key: 'C', spawn_uuid: 'c1', spawn_tool_use_id: 'tu_g' }),
    };
    const out = groupSidechains(items, meta);
    // Top level: the main item, then the child subagent node — NOT the grandchild.
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent']);
    const child = group(out[1]);
    expect(child.subagentKey).toBe('C');
    expect(child.depth).toBe(0);
    expect(child.nested).toBe(true);          // nested under main m1
    expect(child.spawnAnchorUuid).toBe('m1');
    // The grandchild is a SubagentNode in the child's children, at depth 1.
    expect(child.children.map((c) => c.subagentKey)).toEqual(['G']);
    const gc = child.children[0];
    expect(gc.kind).toBe('subagent');
    expect(gc.depth).toBe(1);
    expect(gc.nested).toBe(true);
    expect(gc.spawnAnchorUuid).toBe('c1');     // placed after the child's c1 item
    // The grandchild is NOT also a top-level node (each subagent shown once).
    expect(flattenSubagents(out).map((n) => n.subagentKey).sort()).toEqual(['C', 'G']);
    const topLevelKeys = out.filter((n) => n.kind === 'subagent').map((n) => (n as SubagentNode).subagentKey);
    expect(topLevelKeys).toEqual(['C']);
  });

  it('places a direct child subagent right AFTER its spawn_uuid main anchor', () => {
    // Two main turns; the child spawns from m2 (anchor m2), so it must emit
    // after m2, NOT after m1 (its document-first position).
    const items = [
      mk('m1'),
      mk('m2'),
      mk('c1', { subagentKey: 'C' }),
    ];
    const meta: Record<string, SubagentMeta> = {
      C: smeta({ kind: 'Explore', parent_subagent_key: null, spawn_uuid: 'm2', spawn_tool_use_id: 'tu_c' }),
    };
    const out = groupSidechains(items, meta);
    expect(out.map((n) => n.kind)).toEqual(['item', 'item', 'subagent']);
    const m1 = out[0];
    const m2 = out[1];
    if (m1.kind !== 'item' || m2.kind !== 'item') throw new Error('expected item nodes');
    expect(m1.item.anchor.uuid).toBe('m1');
    expect(m2.item.anchor.uuid).toBe('m2');
    expect(group(out[2]).subagentKey).toBe('C');
    expect(group(out[2]).spawnAnchorUuid).toBe('m2');
  });

  it('keeps a top orphan (unresolved parent) at top level, no drop', () => {
    // parent_subagent_key references a key that is NOT loaded → top-level.
    const items = [mk('h1'), mk('o1', { subagentKey: 'O' })];
    const meta: Record<string, SubagentMeta> = {
      O: smeta({ kind: 'agent', parent_subagent_key: 'MISSING', spawn_uuid: 'gone', spawn_tool_use_id: 'tu_o' }),
    };
    const out = groupSidechains(items, meta);
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent']);
    const o = group(out[1]);
    expect(o.subagentKey).toBe('O');
    expect(o.nested).toBe(false);
    expect(o.depth).toBe(0);
    expect(o.spawnAnchorUuid).toBeNull();
  });

  it('falls back to legacy parent_uuid nesting when no kernel linkage is present', () => {
    // No subagent_meta entry for C (old transcript): nest via root.parent_uuid.
    const items = [mk('m1'), mk('c1', { subagentKey: 'C', parentUuid: 'm1' }), mk('c2', { subagentKey: 'C' })];
    const out = groupSidechains(items, {}); // empty meta -> legacy path
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent']);
    const c = group(out[1]);
    expect(c.nested).toBe(true);
    expect(c.spawnAnchorUuid).toBe('m1');
    expect(c.items.map((i) => i.anchor.uuid)).toEqual(['c1', 'c2']);
  });

  it('never drops a bucket: a kernel-nested grandchild whose parent is absent surfaces top-level', () => {
    // G's parent C is referenced but not loaded (paged out) → G stays top-level
    // (no drop), since parentOf falls to `top` when the parent bucket is absent.
    const items = [mk('h1'), mk('g1', { subagentKey: 'G' })];
    const meta: Record<string, SubagentMeta> = {
      G: smeta({ kind: 'grounding', parent_subagent_key: 'C', spawn_uuid: 'c1', spawn_tool_use_id: 'tu_g' }),
    };
    const out = groupSidechains(items, meta);
    const subs = flattenSubagents(out);
    expect(subs.map((n) => n.subagentKey)).toEqual(['G']);
    expect(subs[0].nested).toBe(false); // surfaced at top level (parent missing)
  });

  it('suppresses two distinct spawns in one item via their distinct spawn_tool_use_id', () => {
    // The s8 two-spawn topology: ONE main item holds two spawns -> two children,
    // each carrying its own spawn_tool_use_id. groupSidechains nests both under
    // the main anchor; the suppression set (built by the reader) covers BOTH ids.
    const items = [
      mk('m1'),
      mk('a1', { subagentKey: 'A' }),
      mk('b1', { subagentKey: 'B' }),
    ];
    const meta: Record<string, SubagentMeta> = {
      A: smeta({ kind: 'Explore', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_a' }),
      B: smeta({ kind: 'code-reviewer', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_b' }),
    };
    const out = groupSidechains(items, meta);
    // main item, then BOTH children interleaved after m1 (document order A, B).
    expect(out.map((n) => n.kind)).toEqual(['item', 'subagent', 'subagent']);
    expect(out.filter((n) => n.kind === 'subagent').map((n) => (n as SubagentNode).subagentKey)).toEqual(['A', 'B']);
    // The reader-side suppression set covers both distinct ids (modeled here).
    const suppress = new Set(Object.values(meta).map((m) => m.spawn_tool_use_id).filter(Boolean) as string[]);
    expect(suppress.has('tu_a')).toBe(true);
    expect(suppress.has('tu_b')).toBe(true);
    expect(suppress.size).toBe(2);
  });

  it('flattenSubagents / walkSubagents visit nested children depth-first', () => {
    const items = [mk('m1'), mk('c1', { subagentKey: 'C' }), mk('g1', { subagentKey: 'G' })];
    const meta: Record<string, SubagentMeta> = {
      C: smeta({ kind: 'k', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_c' }),
      G: smeta({ kind: 'k', parent_subagent_key: 'C', spawn_uuid: 'c1', spawn_tool_use_id: 'tu_g' }),
    };
    const out = groupSidechains(items, meta);
    // Parent BEFORE child (depth-first, tree order).
    expect(flattenSubagents(out).map((n) => n.subagentKey)).toEqual(['C', 'G']);
  });

  it('builds a FINITE acyclic tree when parent_subagent_key forms a cycle (A<->B)', () => {
    // Corrupt/self-referential transcript: A.parent = B AND B.parent = A, both
    // loaded. The unguarded build() produced a CYCLIC node graph (A.children=[B],
    // B.children=[A]) that walkSubagents / flattenSubagents recursed forever on.
    // The build() cycle guard must drop the back-edge so the tree is acyclic and
    // every walk terminates. (Non-vacuous: without the guard, the two walks below
    // never return — the test hangs / stack-overflows rather than asserting.)
    const items = [
      mk('a1', { subagentKey: 'A' }),
      mk('b1', { subagentKey: 'B' }),
    ];
    const meta: Record<string, SubagentMeta> = {
      A: smeta({ kind: 'agent', parent_subagent_key: 'B', spawn_uuid: 'b1', spawn_tool_use_id: 'tu_a' }),
      B: smeta({ kind: 'agent', parent_subagent_key: 'A', spawn_uuid: 'a1', spawn_tool_use_id: 'tu_b' }),
    };
    // groupSidechains itself must return (the build + markEmitted cycle guards).
    const out = groupSidechains(items, meta);

    // flattenSubagents visits each node a bounded number of times and returns.
    const flat = flattenSubagents(out);
    // Both buckets surface exactly once each (no drop, no infinite duplication).
    expect(flat.map((n) => n.subagentKey).sort()).toEqual(['A', 'B']);

    // The constructed tree is acyclic: a manual stack-guarded DFS must find no
    // node reachable from itself. Walking WITHOUT a guard (relying on the tree
    // being acyclic) terminates — proving build() dropped the back-edge.
    let visits = 0;
    const seenOnPath = new Set<string>();
    const assertAcyclic = (n: SubagentNode) => {
      visits++;
      expect(seenOnPath.has(n.subagentKey)).toBe(false); // no node is its own ancestor
      seenOnPath.add(n.subagentKey);
      for (const c of n.children) assertAcyclic(c);
      seenOnPath.delete(n.subagentKey);
    };
    for (const n of out) if (n.kind === 'subagent') assertAcyclic(n);
    // A cyclic graph would make `visits` unbounded; an acyclic 2-node tree visits
    // at most 2 nodes total (one root + one child; the back-edge is dropped).
    expect(visits).toBeLessThanOrEqual(2);

    // walkSubagents (used by the reader's traversals) also terminates.
    let walked = 0;
    walkSubagents(out, () => { walked++; });
    expect(walked).toBeLessThanOrEqual(2);
  });
});
