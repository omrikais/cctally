import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { deriveOutline } from './deriveOutline';
import { groupSidechains, flattenSubagents, type RenderNode } from './groupSidechains';
import { SidechainGroup } from './SidechainGroup';
import type { ConversationItem, OutlineTurn, SubagentMeta } from '../types/conversation';

// #188 S3/C3 (Codex P2) — lock the three-way coincidence the cardRefs design
// depends on: the outline subagent entry's JUMP ANCHOR (deriveOutline bucket
// root = b[0].uuid), the render-tree group's first item anchor
// (groupSidechains items[0].anchor.uuid → the reader's `rootUuid` prop and the
// cardRefs key), and the card's rendered `data-uuid`. All three derive from the
// SAME document-ordered subagent_key bucket and MUST coincide — INCLUDING when
// the subagent thread opens with a `meta` row (a skill-body / SessionStart
// injection). The bucket ROOT for jump/anchor is the LITERAL first member
// (b[0] / items[0]), even though groupSidechains' nesting + subagentSummaryLabel
// skip a leading meta to find the task-prompt root — those are placement/label
// concerns, not the anchor identity. If this FAILS, an outline subagent click
// would jump to a uuid that has no card in cardRefs → the Bug-1 regression.

// One synthetic session: a main human prompt, then a subagent thread `sk1`
// whose FIRST item is a `meta` row (intra-file skill body), then a real
// assistant member. The same topology expressed in both the OutlineTurn shape
// (deriveOutline) and the ConversationItem shape (groupSidechains).
const OUTLINE_TURNS: OutlineTurn[] = [
  {
    uuid: 'h1', kind: 'human', ts: null, label: 'do the audit',
    member_uuids: ['h1'], subagent_key: null, parent_uuid: null, is_sidechain: false,
  },
  {
    // META FIRST: a leading injected meta row inside the subagent file.
    uuid: 'm1', kind: 'meta', ts: null, label: 'Base directory for this skill: /x',
    member_uuids: ['m1'], subagent_key: 'sk1', parent_uuid: null, is_sidechain: true,
    meta_kind: 'skill',
  },
  {
    uuid: 's1', kind: 'assistant', ts: null, label: 'auditing module A',
    member_uuids: ['s1'], subagent_key: 'sk1', parent_uuid: 'm1', is_sidechain: true,
  },
];

function citem(over: Partial<ConversationItem> & { uuid: string; kind: ConversationItem['kind'] }): ConversationItem {
  const { uuid, kind, ...rest } = over;
  return {
    kind,
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid],
    ts: 't',
    text: uuid,
    blocks: [],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
    ...rest,
  } as ConversationItem;
}

const ITEMS: ConversationItem[] = [
  citem({ uuid: 'h1', kind: 'human', text: 'do the audit' }),
  citem({
    uuid: 'm1', kind: 'meta', text: 'Base directory for this skill: /x',
    is_sidechain: true, subagent_key: 'sk1', meta_kind: 'skill',
  } as Partial<ConversationItem> as never),
  citem({
    uuid: 's1', kind: 'assistant', text: 'auditing module A',
    is_sidechain: true, subagent_key: 'sk1', parent_uuid: 'm1', model: 'claude-opus-4', cost_usd: 0.1,
  } as Partial<ConversationItem> as never),
];

describe('subagent anchor invariant (meta-first sidechain, #188 S3/C3)', () => {
  it('deriveOutline bucket root == groupSidechains first-item anchor == card data-uuid', () => {
    // 1. Outline jump anchor for the sk1 subagent entry.
    const { entries } = deriveOutline(OUTLINE_TURNS, { sk1: { kind: 'explore' } });
    const sub = entries.find((e) => e.type === 'subagent' && e.subagentKey === 'sk1');
    expect(sub, 'deriveOutline must emit a subagent entry for sk1').toBeTruthy();
    const outlineAnchor = sub!.uuid;
    // The anchor is the LITERAL first bucket member (the meta row m1), NOT the
    // first non-meta task root.
    expect(outlineAnchor).toBe('m1');

    // 2. Render-tree group first-item anchor (the reader's `rootUuid`).
    const nodes: RenderNode[] = groupSidechains(ITEMS);
    const group = nodes.find((n) => n.kind === 'subagent' && n.subagentKey === 'sk1');
    expect(group, 'groupSidechains must emit a subagent node for sk1').toBeTruthy();
    const groupRoot = (group as Extract<RenderNode, { kind: 'subagent' }>).items[0].anchor.uuid;

    // 3. The card's rendered data-uuid (the cardRefs key), fed `rootUuid` exactly
    //    as ConversationReader does: `rootUuid={g.items[0].anchor.uuid}`.
    const g = group as Extract<RenderNode, { kind: 'subagent' }>;
    const { container } = render(
      <SidechainGroup subagentKey={g.subagentKey} items={g.items} nested={g.nested} rootUuid={groupRoot} />,
    );
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    const cardDataUuid = det.getAttribute('data-uuid');

    // The three-way coincidence.
    expect(groupRoot).toBe(outlineAnchor);
    expect(cardDataUuid).toBe(outlineAnchor);
  });
});

// §5 (Codex P2-G) — the outline stays a single FLAT list of subagents, but a
// NESTED subagent's anchor must still resolve. deriveOutline buckets by
// subagent_key over the whole list (placement-agnostic), so the flat entry is
// emitted regardless of nesting; and the recursive render tree must be
// TRAVERSED (flattenSubagents) — not just its top level — for the nested
// anchor's group to be found. This locks both halves so a nested-subagent
// outline click resolves to a built card.
describe('nested subagent anchor invariant (§5 recursive tree)', () => {
  // main m1 -> child C -> grandchild G. The outline lists C and G as flat
  // subagent entries; the render tree nests G inside C.children.
  const OUTLINE_NESTED: OutlineTurn[] = [
    { uuid: 'm1', kind: 'human', ts: null, label: 'run audit', member_uuids: ['m1'], subagent_key: null, parent_uuid: null, is_sidechain: false },
    { uuid: 'c1', kind: 'assistant', ts: null, label: 'sync audit', member_uuids: ['c1'], subagent_key: 'C', parent_uuid: null, is_sidechain: true },
    { uuid: 'g1', kind: 'assistant', ts: null, label: 'ground claims', member_uuids: ['g1'], subagent_key: 'G', parent_uuid: null, is_sidechain: true },
  ];
  const META: Record<string, SubagentMeta> = {
    C: { kind: 'code-reviewer', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_c' },
    G: { kind: 'grounding', parent_subagent_key: 'C', spawn_uuid: 'c1', spawn_tool_use_id: 'tu_g' },
  };
  const ITEMS_NESTED: ConversationItem[] = [
    citem({ uuid: 'm1', kind: 'human', text: 'run audit' }),
    citem({ uuid: 'c1', kind: 'assistant', text: 'sync audit', is_sidechain: true, subagent_key: 'C', model: 'm', cost_usd: 0.1 } as Partial<ConversationItem> as never),
    citem({ uuid: 'g1', kind: 'assistant', text: 'ground claims', is_sidechain: true, subagent_key: 'G', model: 'm', cost_usd: 0.1 } as Partial<ConversationItem> as never),
  ];

  it('a NESTED grandchild outline entry resolves to its group via flattenSubagents (not just the top level)', () => {
    // 1. The outline lists BOTH subagents flat (G is not dropped).
    const { entries } = deriveOutline(OUTLINE_NESTED, META);
    const subKeys = entries.filter((e) => e.type === 'subagent').map((e) => e.subagentKey).sort();
    expect(subKeys).toEqual(['C', 'G']);
    const gEntry = entries.find((e) => e.subagentKey === 'G')!;
    const gAnchor = gEntry.uuid;       // jump anchor for the grandchild = its bucket root (g1)
    expect(gAnchor).toBe('g1');

    // 2. The render tree nests G inside C.children — a TOP-LEVEL find misses it.
    const nodes: RenderNode[] = groupSidechains(ITEMS_NESTED, META);
    const topLevel = nodes.find((n) => n.kind === 'subagent' && n.subagentKey === 'G');
    expect(topLevel).toBeUndefined();   // NOT at top level

    // 3. A TRAVERSING find (flattenSubagents) resolves it, and its first item
    //    anchor coincides with the outline jump anchor.
    const gNode = flattenSubagents(nodes).find((n) => n.subagentKey === 'G');
    expect(gNode, 'flattenSubagents must reach the nested grandchild').toBeTruthy();
    expect(gNode!.items[0].anchor.uuid).toBe(gAnchor);
    expect(gNode!.depth).toBe(1);
  });
});
