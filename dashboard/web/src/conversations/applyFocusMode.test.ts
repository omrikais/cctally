import { describe, expect, it } from 'vitest';
import { applyFocusMode, nodeUuid, type FilteredNode } from './applyFocusMode';
import type { RenderNode } from './groupSidechains';
import type { ConversationItem, ConversationBlock } from '../types/conversation';

// ---- factories ---------------------------------------------------------
function item(
  over: Partial<ConversationItem> & {
    uuid: string;
    kind?: ConversationItem['kind'];
  },
): ConversationItem {
  const { uuid, kind = 'human', ...rest } = over;
  return {
    kind,
    anchor: { session_id: 's', uuid, id: 0 },
    member_uuids: [uuid],
    ts: 't',
    text: '',
    blocks: [],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
    ...rest,
  } as ConversationItem;
}

const itemNode = (it: ConversationItem): RenderNode => ({ kind: 'item', item: it });
const errBlock = (): ConversationBlock => ({
  kind: 'tool_call',
  name: 'Bash',
  input_summary: '{}',
  preview: 'x',
  tool_use_id: 't',
  result: { text: 'boom', truncated: false, is_error: true },
});
const okToolBlock = (): ConversationBlock => ({
  kind: 'tool_call',
  name: 'Read',
  input_summary: '{}',
  preview: '/a',
  tool_use_id: 't',
  result: { text: 'ok', truncated: false, is_error: false },
});
const thinkBlock = (): ConversationBlock => ({ kind: 'thinking', text: 'hm' });
const orphanResult = (is_error: boolean): ConversationBlock => ({
  kind: 'tool_result',
  text: 'r',
  truncated: false,
  is_error,
});

// Representative nodes used across the matrix.
const human = itemNode(item({ uuid: 'h', kind: 'human', text: 'hi' }));
const proseAssistant = itemNode(
  item({ uuid: 'ap', kind: 'assistant', text: 'reply', blocks: [{ kind: 'text', text: 'reply' }] } as never),
);
const thinkingOnlyAssistant = itemNode(
  item({ uuid: 'at', kind: 'assistant', text: '', blocks: [thinkBlock()] } as never),
);
const toolOnlyAssistant = itemNode(
  item({ uuid: 'ao', kind: 'assistant', text: '', blocks: [okToolBlock()] } as never),
);
const erroringAssistant = itemNode(
  item({ uuid: 'ae', kind: 'assistant', text: 'oops', blocks: [{ kind: 'text', text: 'oops' }, errBlock()] } as never),
);
const orphan = itemNode(item({ uuid: 'o', kind: 'tool_result', blocks: [orphanResult(false)] }));
const erroringOrphan = itemNode(item({ uuid: 'oe', kind: 'tool_result', blocks: [orphanResult(true)] }));
const meta = itemNode(item({ uuid: 'm', kind: 'meta', blocks: [] } as never));

const subagentOk: RenderNode = {
  kind: 'subagent',
  subagentKey: 'k1',
  nested: false,
  depth: 0,
  spawnAnchorUuid: null,
  children: [],
  items: [item({ uuid: 'sa1', is_sidechain: true, subagent_key: 'k1', blocks: [okToolBlock()] } as never)],
};
const subagentErr: RenderNode = {
  kind: 'subagent',
  subagentKey: 'k2',
  nested: false,
  depth: 0,
  spawnAnchorUuid: null,
  children: [],
  items: [item({ uuid: 'se1', is_sidechain: true, subagent_key: 'k2', blocks: [errBlock()] } as never)],
};
const trrOk: RenderNode = {
  kind: 'tool_result_run',
  items: [
    item({ uuid: 'tr1', kind: 'tool_result', blocks: [orphanResult(false)] }),
    item({ uuid: 'tr2', kind: 'tool_result', blocks: [orphanResult(false)] }),
  ],
};
const trrErr: RenderNode = {
  kind: 'tool_result_run',
  items: [
    item({ uuid: 'te1', kind: 'tool_result', blocks: [orphanResult(false)] }),
    item({ uuid: 'te2', kind: 'tool_result', blocks: [orphanResult(true)] }),
  ],
};

// `kept(mode)` = the uuids of the NON-hidden_run nodes after filtering.
function kept(nodes: RenderNode[], mode: Parameters<typeof applyFocusMode>[1]): string[] {
  return applyFocusMode(nodes, mode)
    .filter((n) => n.kind !== 'hidden_run')
    .map((n) => nodeUuid(n));
}

describe('applyFocusMode — `all` is the identity render path', () => {
  it('returns the SAME array reference for mode "all"', () => {
    const nodes = [human, proseAssistant, toolOnlyAssistant];
    expect(applyFocusMode(nodes, 'all')).toBe(nodes);
  });
});

describe('applyFocusMode — keep/suppress matrix', () => {
  const all = [
    human,
    proseAssistant,
    thinkingOnlyAssistant,
    toolOnlyAssistant,
    erroringAssistant,
    orphan,
    erroringOrphan,
    subagentOk,
    subagentErr,
    trrOk,
    trrErr,
    meta,
  ];

  it('chat keeps human + prose/thinking assistants; suppresses tools/orphans/subagents/runs/meta', () => {
    expect(kept(all, 'chat')).toEqual(['h', 'ap', 'at', 'ae']);
  });

  it('prompts keeps human turns only', () => {
    expect(kept(all, 'prompts')).toEqual(['h']);
  });

  it('errors keeps erroring items + erroring orphan + erroring subagent/run only', () => {
    expect(kept(all, 'errors')).toEqual(['ae', 'oe', 'se1', 'te1']);
  });
});

describe('applyFocusMode — hidden-run coalescing', () => {
  it('coalesces a leading run of hidden nodes', () => {
    const out = applyFocusMode([toolOnlyAssistant, orphan, human], 'prompts');
    expect(out.map((n) => n.kind)).toEqual(['hidden_run', 'item']);
    const hr = out[0] as Extract<FilteredNode, { kind: 'hidden_run' }>;
    expect(hr.count).toBe(2);
    expect(hr.firstUuid).toBe('ao');
  });

  it('coalesces a trailing run of hidden nodes', () => {
    const out = applyFocusMode([human, toolOnlyAssistant, orphan], 'prompts');
    expect(out.map((n) => n.kind)).toEqual(['item', 'hidden_run']);
    expect((out[1] as Extract<FilteredNode, { kind: 'hidden_run' }>).count).toBe(2);
  });

  it('coalesces a run BETWEEN two keepers (one hidden_run per gap)', () => {
    const out = applyFocusMode([human, toolOnlyAssistant, orphan, human], 'prompts');
    expect(out.map((n) => n.kind)).toEqual(['item', 'hidden_run', 'item']);
    expect((out[1] as Extract<FilteredNode, { kind: 'hidden_run' }>).count).toBe(2);
  });

  it('a subagent/tool_result_run node counts as ONE hidden node', () => {
    const out = applyFocusMode([subagentOk, trrOk], 'prompts');
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe('hidden_run');
    expect((out[0] as Extract<FilteredNode, { kind: 'hidden_run' }>).count).toBe(2);
  });
});

describe('nodeUuid', () => {
  it('resolves the jump anchor for each node shape', () => {
    expect(nodeUuid(human)).toBe('h');
    expect(nodeUuid(subagentOk)).toBe('sa1');
    expect(nodeUuid(trrOk)).toBe('tr1');
    expect(nodeUuid({ kind: 'hidden_run', count: 3, firstUuid: 'x' })).toBe('x');
  });
});
