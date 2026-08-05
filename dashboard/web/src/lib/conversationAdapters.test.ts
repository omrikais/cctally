import { describe, expect, it } from 'vitest';
import {
  adaptBlocks,
  adaptQualifiedBrowse,
  adaptQualifiedDetail,
  adaptQualifiedFind,
  adaptQualifiedOutline,
  adaptQualifiedPayload,
  adaptQualifiedPrompts,
  adaptQualifiedSearch,
  buildQualifiedOutlinePositions,
} from './conversationAdapters';
import type { ConversationBlock, NativeTerminalOutput, NativeToolCard } from '../types/conversation';

const ref = { source: 'codex' as const, key: 'v1.root-a' };

describe('qualified Codex conversation adapters', () => {
  it('keeps the qualified key on browse and search rows', () => {
    const browse = adaptQualifiedBrowse('codex', {
      status: 'ok',
      rows: [{
        conversation_key: ref.key,
        title: 'Codex thread',
        project_key: 'project:opaque',
        project_label: 'project-red',
        started_utc: '2026-07-14T12:00:00Z',
        last_activity_utc: '2026-07-14T12:05:00Z',
        count: 4,
        cost_usd: 0.25,
        models: ['gpt-5.6-codex'],
        parent: null,
        is_fork: false,
      }],
      facets: { projects: [], models: [] },
      page: { total: 1, returned: 1, cursor: null },
    });
    expect(browse.rows[0].conversation_ref).toEqual(ref);
    expect(browse.rows[0]).toMatchObject({ msg_count: 4, project_label: 'project-red' });

    const search = adaptQualifiedSearch('codex', {
      status: 'ok', query: 'needle', total: 1, mode: 'fts', depth: 'full',
      hits: [{
        conversation_key: ref.key,
        item_key: 'civ1.item',
        title: 'Codex thread',
        snippet: 'needle in reasoning',
        badges: ['thinking'],
        last_activity_utc: '2026-07-14T12:05:00Z',
        project_label: 'project-red',
      }],
      page: { returned: 1, cursor: null },
    });
    expect(search.hits[0]).toMatchObject({
      conversation_ref: ref,
      uuid: 'civ1.item',
      match_kinds: ['thinking'],
    });
  });

  it('maps provider-native blocks without inventing Claude cache semantics', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok',
      conversation_key: ref.key,
      title: 'Codex thread',
      items: [{
        item_key: 'civ1.answer',
        kind: 'assistant',
        timestamp_utc: '2026-07-14T12:03:10Z',
        model: 'gpt-5.6-codex',
        blocks: [
          { kind: 'assistant', text: 'Answer', detail: null, call_id: null, timestamp_utc: '2026-07-14T12:03:10Z' },
          { kind: 'reasoning', text: 'Reasoning', detail: null, call_id: null, timestamp_utc: '2026-07-14T12:03:11Z' },
          {
            kind: 'tool_call', text: 'fixture\n{}', detail: { name: 'fixture', args: '{}' },
            call_id: 'call-1', block_key: 'cbk1.call', timestamp_utc: '2026-07-14T12:03:12Z',
            output: { text: '{"ok":true}', detail: null },
          },
        ],
        cost_usd: 0.125,
        tokens: { source: 'codex', input: 1200, output: 400, cached_input: 300, reasoning_output: 100 },
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0.125, unattributed_cost_usd: 0,
      tokens: { source: 'codex', input: 1200, output: 400, cached_input: 300, reasoning_output: 100 },
    });
    const item = detail.items[0];
    expect(item.kind).toBe('assistant');
    expect(item.blocks.map((block) => block.kind)).toEqual(['text', 'codex_reasoning', 'tool_call']);
    expect(item.kind === 'assistant' && 'tokens' in item ? item.tokens : undefined).toEqual({
      source: 'codex', input: 1200, output: 400, cache_creation: 0, cache_read: 0,
      cached_input: 300, reasoning_output: 100,
    });
    expect(detail.provider_meta).toMatchObject({ source: 'codex', unattributed_cost_usd: 0 });
  });

  it('adapts card-ready Codex terminal and patch records without wrapper noise or duplicate lifecycle prose', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 'Session B cards',
      items: [
        {
          item_key: 'civ1.tools', kind: 'tool_call', timestamp_utc: '2026-07-21T11:00:03Z', model: 'gpt-synthetic-codex',
          blocks: [
            {
              kind: 'tool_call', text: 'exec\nconst r = await tools.exec_command(...)', call_id: 'exec-ok', block_key: 'cbk.exec',
              detail: { name: 'exec', args: 'const r = await tools.exec_command(...)', card: {
                schema_version: 1, type: 'terminal', status: 'completed', commands: [{
                  command: "printf 'alpha\\n'", workdir: '/synthetic/project', metadata: { yield_time_ms: 10000 },
                }],
              } },
              output: { text: 'alpha\n', detail: { card: {
                schema_version: 1, type: 'terminal_output', status: 'completed', is_error: false,
                parts: [{ type: 'text', stream: 'stdout', text: 'alpha\n' }], truncated: false,
              } } },
            },
            {
              kind: 'tool_call', text: 'apply_patch', call_id: 'patch-ok', block_key: 'cbk.patch',
              detail: { name: 'apply_patch', args: '*** Begin Patch', card: {
                schema_version: 1, type: 'patch', source: 'apply_patch', status: 'completed',
                files: [{ path: 'src/a.ts', status: 'modified' }], patch: '*** Begin Patch', truncated: false,
                completion: {
                  schema_version: 1, type: 'patch', source: 'patch_apply_end', status: 'completed', success: true,
                  stdout: 'Done!', stderr: '', has_diff: true, event_block_key: 'cbk.patch-event', truncated: false,
                  files: [{ path: 'src/a.ts', status: 'modified', unified_diff: '--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n' }],
                },
              } },
              output: { text: 'Done!', detail: null },
            },
          ],
          cost_usd: 0, tokens: null,
        },
        {
          item_key: 'civ1.diff-less', kind: 'event', timestamp_utc: '2026-07-21T11:00:25Z', model: null,
          blocks: [{
            kind: 'event', text: 'patch_apply synthetic-summary.txt', call_id: 'diff-less', block_key: 'cbk.diff-less',
            detail: { event: 'patch_apply_end', card: {
              schema_version: 1, type: 'patch', source: 'patch_apply_end', status: 'failed', success: false,
              stdout: '', stderr: 'synthetic failure', has_diff: false, truncated: false,
              files: [{ path: 'synthetic-summary.txt', status: 'modified' }],
            } },
          }],
          cost_usd: null, tokens: null,
        },
      ],
      page: { total: 2, returned: 2, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);

    const terminal = detail.items[0].blocks[0] as Extract<(typeof detail.items)[number]['blocks'][number], { kind: 'tool_call' }> & { native_card?: unknown };
    expect(terminal).toMatchObject({
      kind: 'tool_call', name: 'exec', input: { command: "printf 'alpha\\n'", workdir: '/synthetic/project' },
      preview: "printf 'alpha\\n'", result: { text: 'alpha\n', is_error: false },
      native_card: { type: 'terminal', commands: [{ command: "printf 'alpha\\n'" }], output: { parts: [{ stream: 'stdout', text: 'alpha\n' }] } },
    });
    expect(JSON.stringify(terminal)).not.toContain('tools.exec_command');

    const patch = detail.items[0].blocks[1] as typeof terminal;
    expect(patch).toMatchObject({
      name: 'apply_patch', native_card: {
        type: 'patch', files: [{ path: 'src/a.ts', status: 'modified', unified_diff: expect.stringContaining('-old\n+new') }],
        event_payload_key: 'cbk.patch-event',
      },
    });

    expect(detail.items[1].kind).toBe('assistant');
    const summary = detail.items[1].blocks[0] as typeof terminal;
    expect(summary).toMatchObject({
      kind: 'tool_call', name: 'patch_apply_end', tool_use_id: 'cbk.diff-less', payload_kind: 'event',
      result: { is_error: true },
      native_card: { type: 'patch', has_diff: false, files: [{ path: 'synthetic-summary.txt', status: 'modified' }] },
    });
  });

  it('adapts valid plan, web, MCP, and agent cards while malformed records stay generic', () => {
    const cards = [
      {
        schema_version: 1, type: 'plan', source: 'update_plan', call_status: 'requested', explanation: 'Synthetic plan',
        items: [{ step: 'First', status: 'completed' }, { step: 'Second', status: 'in_progress' }],
        result: { status: 'returned', truncated: false, value: 'Plan updated' },
      },
      {
        schema_version: 1, type: 'web_search', source: 'web_search_call', call_status: 'completed', query: 'cctally 332', action: {},
        completion: { status: 'returned', query: 'cctally 332', action: {}, results: [{ type: 'computer_initialize_state', title: 'Issue', url: 'https://example.com/332', snippet: 'Task B', ref_id: 'turn0search0' }] },
      },
      {
        schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_search_issues', call_status: 'completed',
        completion: { server: 'fixture', tool: 'search_issues', arguments: { query: '332' }, duration: { secs: 0, nanos: 20 }, result: { Ok: { content: [] } }, status: 'ok' },
      },
      {
        schema_version: 1, type: 'agent', operation: 'spawn_agent', call_status: 'requested', arguments: { task_name: 'child' },
        result: { status: 'returned', truncated: false, value: { agent_id: 'child-id' } },
        child_conversation: { conversation_key: 'v1.child', role: 'cctally_reviewer', nickname: 'Synthetic Child' },
      },
      {
        schema_version: 1, type: 'plan', source: 'update_plan', call_status: 'interrupted',
        items: [{ step: 'Still pending', status: 'pending' }],
      },
      {
        schema_version: 1, type: 'agent', operation: 'wait_agent', call_status: 'requested', arguments: { timeout_ms: 30_000 },
      },
      { schema_version: 1, type: 'plan', items: [{ step: 42, status: 'pending' }] },
    ];
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 'Session C',
      items: [{
        item_key: 'civ1.cards', kind: 'tool_call', timestamp_utc: '2026-07-22T08:00:00Z', model: 'gpt-synthetic-codex',
        blocks: cards.map((card, index) => ({
          kind: 'tool_call', text: `tool-${index}`, call_id: `call-${index}`, block_key: `cbk.${index}`,
          detail: { name: index === 0 ? 'update_plan' : `tool-${index}`, args: '{}', card },
          ...(index < 4 ? { output: { text: index === 1 ? 'search result' : 'ok', detail: null } } : {}),
        })),
        cost_usd: 0, tokens: null,
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    const blocks = detail.items[0].blocks as Array<Record<string, unknown>>;
    expect(blocks.slice(0, 4).map((block) => (block.native_card as { type?: string })?.type)).toEqual([
      'plan', 'web_search', 'mcp', 'agent',
    ]);
    expect(blocks[1]).toMatchObject({
      input: { query: 'cctally 332' },
      web_search: { query: 'cctally 332', links: [{ title: 'Issue', url: 'https://example.com/332', snippet: 'Task B', ref_id: 'turn0search0' }] },
    });
    expect(blocks[4]).toMatchObject({
      native_card: { type: 'plan', call_status: 'interrupted' },
      result: null,
    });
    expect(blocks[5]).toMatchObject({
      native_card: { type: 'agent', operation: 'wait_agent', call_status: 'requested' },
      result: null,
    });
    expect(blocks[6].native_card).toBeUndefined();
  });

  it('routes standalone Codex reasoning, tools, outputs, and injected metadata semantically', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key,
      title: '[$cctally-session-kickoff](/private/skills/cctally-session-kickoff/SKILL.md) Task B of issue #330.',
      items: [
        {
          item_key: 'civ1.reasoning', kind: 'reasoning', timestamp_utc: '2026-07-14T12:00:01Z', model: 'gpt-5.6-sol',
          blocks: [{ kind: 'reasoning', text: '**Planning the fix**' }], cost_usd: 0.01, tokens: null,
        },
        {
          item_key: 'civ1.tool', kind: 'tool_call', timestamp_utc: '2026-07-14T12:00:02Z', model: 'gpt-5.6-sol',
          blocks: [{ kind: 'tool_call', text: 'exec\n{}', detail: { name: 'exec', args: '{}' }, block_key: 'cbk.tool' }],
          cost_usd: 0.02, tokens: null,
        },
        {
          item_key: 'civ1.output', kind: 'tool_output', timestamp_utc: '2026-07-14T12:00:03Z', model: null,
          blocks: [{ kind: 'tool_output', text: 'done', block_key: 'cbk.output' }], cost_usd: null, tokens: null,
        },
        {
          item_key: 'civ1.role', kind: 'meta', timestamp_utc: '2026-07-14T12:00:04Z', model: null,
          meta_kind: 'context', meta_label: 'role', meta_sections: ['agents'],
          blocks: [{ kind: 'meta', text: 'You are /root.', detail: { meta_kind: 'context', meta_label: 'role' } }],
          cost_usd: null, tokens: null,
        },
        {
          item_key: 'civ1.skill', kind: 'meta', timestamp_utc: '2026-07-14T12:00:05Z', model: null,
          meta_kind: 'skill', meta_label: 'skill', skill_name: 'cctally-session-kickoff',
          blocks: [{ kind: 'meta', text: '<skill>...</skill>', detail: { meta_kind: 'skill', meta_label: 'skill' } }],
          cost_usd: null, tokens: null,
        },
        {
          item_key: 'civ1.started', kind: 'event', timestamp_utc: '2026-07-14T12:00:06Z', model: null,
          blocks: [{ kind: 'event', text: 'task_started', detail: { event: 'task_started' } }], cost_usd: null, tokens: null,
        },
      ],
      page: { total: 6, returned: 6, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0.03, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);

    expect(detail.title).toBe('$cctally-session-kickoff Task B of issue #330.');
    expect(detail.items.map((item) => item.kind)).toEqual([
      'assistant', 'assistant', 'tool_result', 'meta', 'meta', 'meta',
    ]);
    expect(detail.items[0].blocks.map((block) => block.kind)).toEqual(['codex_reasoning']);
    expect(detail.items[1].blocks.map((block) => block.kind)).toEqual(['tool_call']);
    expect(detail.items[2].blocks.map((block) => block.kind)).toEqual(['tool_result']);
    expect(detail.items[3]).toMatchObject({ meta_kind: 'context', meta_label: 'role', meta_sections: ['agents'] });
    expect(detail.items[4]).toMatchObject({ meta_kind: 'skill', meta_label: 'skill', skill_name: 'cctally-session-kickoff' });
    expect(detail.items[5]).toMatchObject({ meta_kind: 'notification', meta_label: 'task_started' });
  });

  it('adapts Session D reasoning, lifecycle, and harness markers without Claude chrome or private syntax', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 'Session D',
      items: [
        {
          item_key: 'civ1.reasoning', kind: 'assistant', timestamp_utc: '2026-07-22T06:00:00Z', model: 'gpt-synthetic-codex',
          blocks: [
            { kind: 'reasoning', text: '**Inspecting synthetic state**', detail: { reasoning: {
              schema_version: 1, source: 'response_item', title: 'Inspecting synthetic state',
            } } },
            { kind: 'reasoning', text: 'Synthetic provider summary.\nDetailed synthetic reasoning body.', detail: { reasoning: {
              schema_version: 1, source: 'response_item', summary: 'Synthetic provider summary.', body: 'Detailed synthetic reasoning body.',
            } } },
            { kind: 'reasoning', text: '  ', detail: { reasoning: { schema_version: 1, source: 'response_item' } } },
          ],
          cost_usd: 0, tokens: null,
        },
        {
          item_key: 'civ1.folded', kind: 'assistant', timestamp_utc: '2026-07-22T06:05:00Z', model: 'gpt-synthetic-codex',
          lifecycle: {
            schema_version: 1, state: 'completed',
            events: [
              { event: 'task_started', payload_which: 'event', block_key: 'cbk.started' },
              { event: 'task_complete', payload_which: 'event', block_key: 'cbk.completed' },
            ],
          },
          blocks: [{ kind: 'assistant', text: 'Folded lifecycle answer.' }], cost_usd: 0, tokens: null,
        },
        {
          item_key: 'civ1.fallback', kind: 'event', timestamp_utc: '2026-07-22T06:06:00Z', model: null,
          blocks: [{
            kind: 'event', text: 'task_complete Unique completion message.', block_key: 'cbk.fallback', payload_which: 'event',
            detail: { lifecycle: { schema_version: 1, event: 'task_complete', message: 'Unique completion message.', duration_ms: 2000 } },
          }], cost_usd: null, tokens: null,
        },
        {
          item_key: 'civ1.markers', kind: 'assistant', timestamp_utc: '2026-07-22T06:09:00Z', model: 'gpt-synthetic-codex',
          blocks: [{
            kind: 'assistant', text: 'Synthetic closeout prose remains visible.', block_key: 'cbk.markers', payload_which: 'event',
            detail: { markers: [
              { schema_version: 1, type: 'git', action: 'stage' },
              { schema_version: 1, type: 'git', action: 'create_pr', draft: false },
              { schema_version: 1, type: 'memory_citation', citation_count: 1, rollout_count: 2 },
            ] },
          }], cost_usd: 0, tokens: null,
        },
        {
          item_key: 'civ1.lookalike', kind: 'assistant', timestamp_utc: '2026-07-22T06:10:00Z', model: 'gpt-synthetic-codex',
          blocks: [{ kind: 'assistant', text: 'Authored ::git-stage{cwd="/synthetic/user"} stays prose.', detail: null }],
          cost_usd: 0, tokens: null,
        },
      ],
      page: { total: 5, returned: 5, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);

    expect(detail.items[0].blocks).toEqual([
      {
        kind: 'codex_reasoning', title: 'Inspecting synthetic state', summary: undefined,
        body: undefined, source: 'response_item',
      },
      {
        kind: 'codex_reasoning', title: undefined, summary: 'Synthetic provider summary.',
        body: 'Detailed synthetic reasoning body.', source: 'response_item',
      },
    ]);
    expect(detail.items[1]).toMatchObject({
      kind: 'assistant',
      lifecycle: { schema_version: 1, state: 'completed', events: [{ block_key: 'cbk.started' }, { block_key: 'cbk.completed' }] },
      blocks: [{ kind: 'text', text: 'Folded lifecycle answer.' }],
    });
    expect(detail.items[2]).toMatchObject({
      kind: 'meta', meta_kind: 'notification', meta_label: 'codex_task_complete',
      blocks: [{
        kind: 'codex_lifecycle', event: 'task_complete', message: 'Unique completion message.',
        duration_ms: 2000, payload_key: 'cbk.fallback',
      }],
    });
    expect(detail.items[3].blocks).toEqual([
      // #463 S2 §3.2 — the adapter now retains the server's per-row anchor.
      { kind: 'text', text: 'Synthetic closeout prose remains visible.', block_key: 'cbk.markers' },
      {
        kind: 'system_actions',
        actions: [
          { type: 'git', action: 'stage' },
          { type: 'git', action: 'create_pr', draft: false },
          { type: 'memory_citation', citation_count: 1, rollout_count: 2 },
        ],
        payload_key: 'cbk.markers',
      },
    ]);
    expect(JSON.stringify(detail.items[3])).not.toContain('/synthetic');
    expect(detail.items[4].blocks).toEqual([
      { kind: 'text', text: 'Authored ::git-stage{cwd="/synthetic/user"} stays prose.' },
    ]);
  });

  it('preserves lifecycle events, parents, children, files, and item-key navigation', () => {
    const outline = adaptQualifiedOutline(ref, {
      status: 'ok', conversation_key: ref.key,
      turns: [{ item_key: 'civ1.compact', label: 'context_compacted', timestamp_utc: null, kinds: { event: 1 } }],
      stats: { items: 1, kinds: { event: 1 } },
      files: [{ file_path: 'src/app.ts', tool: 'patch_apply', count: 2 }],
      children: [{ conversation_key: 'v1.child', title: 'Child', cost_usd: 0.01 }],
    }, {
      total_cost_usd: 0.5,
      tokens: { source: 'codex', input: 10, output: 20, cached_input: 3, reasoning_output: 4 },
    });
    expect(outline.turns[0]).toMatchObject({ uuid: 'civ1.compact', meta_kind: 'compaction' });
    expect(outline.files).toEqual([]);
    expect(outline.provider_files).toEqual([{ path: 'src/app.ts', tool: 'patch_apply', count: 2 }]);
    expect(outline.stats.tokens).toMatchObject({ source: 'codex', cached_input: 3, reasoning_output: 4 });
  });

  it('keeps the qualified outline focused on real turns and compactions', () => {
    const outline = adaptQualifiedOutline(ref, {
      status: 'ok', conversation_key: ref.key,
      turns: [
        { item_key: 'civ1.started', label: 'task_started', timestamp_utc: null, kinds: { event: 1 } },
        {
          item_key: 'civ1.role', label: 'Harness role', timestamp_utc: null, kinds: { meta: 1 },
          meta_kind: 'context', meta_label: 'role', skill_name: null,
        },
        {
          item_key: 'civ1.prompt',
          label: '[$cctally-session-kickoff](/private/skills/cctally-session-kickoff/SKILL.md) Task B of issue #330.',
          timestamp_utc: null, kinds: { user: 1 },
        },
        { item_key: 'civ1.reply', label: 'Implemented.', timestamp_utc: null, kinds: { reasoning: 3, assistant: 1, tool_call: 2 } },
        { item_key: 'civ1.compact', label: 'context_compacted', timestamp_utc: null, kinds: { event: 1 } },
        { item_key: 'civ1.patch', label: 'patch_apply', timestamp_utc: null, kinds: { event: 1 } },
      ],
      stats: { items: 6, kinds: { event: 3, meta: 1, user: 1, assistant: 1 } },
      files: [], children: [],
    } as Parameters<typeof adaptQualifiedOutline>[1], {}, new Set(['civ1.prompt']));

    expect(outline.turns.map((turn) => ({ uuid: turn.uuid, kind: turn.kind, label: turn.label, meta_kind: turn.meta_kind }))).toEqual([
      { uuid: 'civ1.prompt', kind: 'human', label: '$cctally-session-kickoff Task B of issue #330.', meta_kind: undefined },
      { uuid: 'civ1.reply', kind: 'assistant', label: 'Implemented.', meta_kind: undefined },
      { uuid: 'civ1.compact', kind: 'meta', label: 'context_compacted', meta_kind: 'compaction' },
    ]);
    expect(outline.stats.turns).toEqual({ total: 3, human: 1, assistant: 1, tool_result: 0, meta: 1 });
  });

  it('uses the same prompt-clean title on browse and search surfaces', () => {
    const rawTitle = '[$cctally-session-kickoff](/private/skills/cctally-session-kickoff/SKILL.md) Task B of issue #330.';
    const browse = adaptQualifiedBrowse('codex', {
      status: 'ok',
      rows: [{
        conversation_key: ref.key, title: rawTitle, project_key: null, project_label: null,
        started_utc: null, last_activity_utc: null, count: 2, cost_usd: 0, models: [], parent: null, is_fork: false,
      }],
      facets: { projects: [], models: [] }, page: { total: 1, returned: 1, cursor: null },
    });
    const search = adaptQualifiedSearch('codex', {
      status: 'ok', query: 'Task B', total: 1, mode: 'like', depth: 'full',
      hits: [{
        conversation_key: ref.key, item_key: 'civ1.prompt', title: rawTitle, snippet: 'Task B', badges: ['title'],
        last_activity_utc: null, project_label: null,
      }],
      page: { returned: 1, cursor: null },
    });

    expect(browse.rows[0].title).toBe('$cctally-session-kickoff Task B of issue #330.');
    expect(search.hits[0].title).toBe('$cctally-session-kickoff Task B of issue #330.');
  });

  it('adapts item-key find and prompt envelopes', () => {
    const find = adaptQualifiedFind({
      status: 'ok', conversation_key: ref.key, total: 1,
      anchors: [{ item_key: 'civ1.item', match_kinds: ['tool'] }],
      anchors_truncated: false, search_depth: 'full', kind: 'all', mode: 'fts',
    });
    expect('anchors' in find ? find.anchors : []).toEqual([{ uuid: 'civ1.item', match_kinds: ['tool'] }]);
    expect(adaptQualifiedPrompts({
      status: 'ok', conversation_key: ref.key,
      prompts: [{ item_key: 'civ1.prompt', text: 'Prompt' }],
    })).toEqual({ prompts: [{ uuid: 'civ1.prompt', text: 'Prompt' }] });
  });

  it('preserves occurrence-exact find fragments, disclosures, and indexing state', () => {
    const exact = adaptQualifiedFind({
      schema_version: 2,
      semantics: 'occurrence',
      status: 'ready',
      query_id: 'query-1',
      total: 2,
      selection_stale: false,
      mode: 'literal',
      kind: 'all',
      search_depth: 'full',
      page: {
        start_index: 0,
        previous_cursor: null,
        next_cursor: 'ofc1.next',
        occurrences: [{
          occurrence_id: 'o1.hit', item_key: 'civ1.item', block_key: 'cbv1.row',
          container_block_key: 'cbv1.call', surface: 'output',
          match_kinds: ['tool'], disclosure: ['cbv1.call'],
          fragments: [
            { leaf_key: 't0', start: 2, end: 5 },
            { leaf_key: 't1', start: 0, end: 1 },
          ],
        }],
      },
      additive_future_field: true,
    });
    expect(exact).toMatchObject({
      schema_version: 2,
      semantics: 'occurrence',
      status: 'ready',
      total: 2,
      page: {
        next_cursor: 'ofc1.next',
        occurrences: [{
          occurrence_id: 'o1.hit',
          uuid: 'civ1.item',
          block_key: 'cbv1.row',
          container_block_key: 'cbv1.call',
          disclosure: ['cbv1.call'],
          fragments: [
            { leaf_key: 't0', start: 2, end: 5 },
            { leaf_key: 't1', start: 0, end: 1 },
          ],
        }],
      },
    });

    const indexing = adaptQualifiedFind({
      schema_version: 2, semantics: 'occurrence', status: 'indexing',
      query_id: 'query-2', selection_stale: false, mode: 'literal', kind: 'all',
      search_depth: 'full',
      page: { start_index: 0, previous_cursor: null, next_cursor: null, occurrences: [] },
    });
    expect('status' in indexing ? indexing.status : null).toBe('indexing');
    expect(indexing.total).toBeUndefined();
  });

  it('uses qualified Claude prompt keys to preserve the outline role spine', () => {
    const claudeRef = { source: 'claude' as const, key: 'v1.claude' };
    const outline = adaptQualifiedOutline(claudeRef, {
      status: 'ok', conversation_key: claudeRef.key,
      turns: [
        { item_key: 'cliv1.prompt', label: 'Prompt', timestamp_utc: '2026-07-14T12:00:00Z', kinds: { text: 1 } },
        { item_key: 'cliv1.reply', label: 'Reply', timestamp_utc: '2026-07-14T12:00:05Z', kinds: { text: 1 } },
      ],
      stats: {
        turns: { total: 2, human: 1, assistant: 1, tool_result: 0, meta: 0 },
        tool_counts: {}, error_count: 0, models: { 'claude-opus-4-8': 1 }, duration_seconds: 5,
        tokens: { source: 'claude', input: 10, output: 20, cache_creation: 3, cache_read: 4 },
        cost_usd: 0.5, cache_saved_usd: 0.1,
      },
      files: [], children: [],
    }, {}, new Set(['cliv1.prompt']));

    expect(outline.turns.map((turn) => turn.kind)).toEqual(['human', 'assistant']);
    expect(outline.stats).toMatchObject({
      turns: { human: 1, assistant: 1 },
      models: { 'claude-opus-4-8': 1 },
      tokens: { source: 'claude', cache_creation: 3, cache_read: 4 },
      duration_seconds: 5,
    });
  });

  it('preserves qualified Claude navigation, files, subagents, rebuilds, and completion', () => {
    const claudeRef = { source: 'claude' as const, key: 'v1.claude-rich' };
    const outline = adaptQualifiedOutline(claudeRef, {
      status: 'ok', conversation_key: claudeRef.key,
      turns: [{
        item_key: 'cliv1.assistant', kind: 'assistant', label: 'Failed command',
        timestamp_utc: '2026-07-14T12:00:05Z', kinds: { assistant: 1 },
        member_item_keys: ['cliv1.folded'],
        subagent_key: 'child', parent_item_key: 'cliv1.parent', is_sidechain: true,
        tools: [{ name: 'Bash', is_error: true }], thinking: ['Investigating'],
        model: 'claude-opus-4-8',
        tokens: { input: 1, output: 2, cache_creation: 3, cache_read: 4 },
        cache_failure: { tokens_recreated: 10, prev_cached: 20, est_wasted_usd: 0.2 },
      }],
      stats: {
        turns: { total: 1, human: 0, assistant: 1, tool_result: 0, meta: 0 },
        tool_counts: { Bash: 1 }, error_count: 1,
        models: { 'claude-opus-4-8': 1 }, duration_seconds: 5,
        tokens: { source: 'claude', input: 1, output: 2, cache_creation: 3, cache_read: 4 },
        cost_usd: 0.5, cache_saved_usd: 0.1,
        cache_failures: {
          count: 1, tokens_recreated: 10, est_wasted_usd: 0.2,
          rebuilds: [{
            uuid: 'cliv1.assistant', subagent_key: 'child', ts: '2026-07-14T12:00:05Z',
            tokens_recreated: 10, est_wasted_usd: 0.2,
          }],
        },
      },
      files: [{
        file_path: 'src/app.py', tool: 'Edit', count: 1, added: 2, removed: 1,
        touches: [{
          item_key: 'cliv1.assistant', timestamp_utc: null, tool_use_id: 'toolu_edit',
          op: 'edit', added: 2, removed: 1,
        }],
      }],
      subagent_meta: {
        child: { kind: 'general-purpose', parent_subagent_key: null, spawn_uuid: 'cliv1.assistant' },
      },
      subagent_costs: { child: 1.25 },
      task_completion: { all_done: true, total: 2, completed: 2, anchor_uuid: 'cliv1.assistant' },
      children: [],
    });

    expect(outline.turns[0]).toMatchObject({
      uuid: 'cliv1.assistant', kind: 'assistant',
      member_uuids: ['cliv1.assistant', 'cliv1.folded'],
      subagent_key: 'child', parent_uuid: 'cliv1.parent', is_sidechain: true,
      tools: [{ name: 'Bash', is_error: true }], thinking: ['Investigating'],
      model: 'claude-opus-4-8',
      tokens: { input: 1, output: 2, cache_creation: 3, cache_read: 4 },
      cache_failure: { tokens_recreated: 10, prev_cached: 20, est_wasted_usd: 0.2 },
    });
    expect(outline.stats.cache_failures?.rebuilds[0].uuid).toBe('cliv1.assistant');
    expect(outline.subagent_meta?.child.spawn_uuid).toBe('cliv1.assistant');
    expect(outline.subagent_costs).toEqual({ child: 1.25 });
    expect(outline.task_completion?.anchor_uuid).toBe('cliv1.assistant');
    expect(outline.files).toEqual([{
      path: 'src/app.py', add: 2, del: 1,
      touches: [{
        uuid: 'cliv1.assistant', tool_use_id: 'toolu_edit', op: 'edit', add: 2, del: 1,
      }],
    }]);
  });

  it('preserves qualified Claude detail grouping and cache navigation', () => {
    const claudeRef = { source: 'claude' as const, key: 'v1.claude-detail' };
    const detail = adaptQualifiedDetail(claudeRef, {
      status: 'ok', conversation_key: claudeRef.key, title: 'Claude detail',
      items: [{
        item_key: 'cliv1.assistant', kind: 'assistant',
        timestamp_utc: '2026-07-14T12:00:05Z', model: 'claude-opus-4-8',
        blocks: [{ kind: 'assistant', text: 'Answer' }], cost_usd: 0.5,
        tokens: { source: 'claude', input: 1, output: 2, cache_creation: 3, cache_read: 4 },
        member_item_keys: ['cliv1.folded'],
        subagent_key: 'child', parent_item_key: 'cliv1.parent', is_sidechain: true,
        cache_failure: { tokens_recreated: 10, prev_cached: 20, est_wasted_usd: 0.2 },
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      subagent_meta: {
        child: { kind: 'general-purpose', parent_subagent_key: null, spawn_uuid: 'cliv1.parent' },
      },
      children: [], parent: null, total_cost_usd: 0.5,
      tokens: { source: 'claude', input: 1, output: 2, cache_creation: 3, cache_read: 4 },
    });

    expect(detail.items[0]).toMatchObject({
      kind: 'assistant', member_uuids: ['cliv1.assistant', 'cliv1.folded'],
      subagent_key: 'child', parent_uuid: 'cliv1.parent', is_sidechain: true,
      cache_failure: { tokens_recreated: 10, prev_cached: 20, est_wasted_usd: 0.2 },
    });
    expect(detail.subagent_meta?.child.spawn_uuid).toBe('cliv1.parent');
  });

  // #463 S1 — segmentation additions to the wire, consumed by the client.
  const block = (text: string) => ({
    kind: 'assistant', text, detail: null, call_id: null,
    timestamp_utc: '2026-07-14T12:03:10Z',
  });
  const page = { total: 2, returned: 2, before: null, after: null, has_before: false, has_after: false };

  it('threads the server member_item_keys instead of a singleton', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key,
      items: [{
        item_key: 'civ1.turn', kind: 'assistant', timestamp_utc: '2026-07-14T12:03:10Z',
        model: 'gpt-5.6-codex', blocks: [block('Answer')],
        member_item_keys: ['civ1.folded-a', 'civ1.folded-b'],
        turn_item_key: 'civ1.turn', segment_ordinal: 0,
        cost_usd: 0.42, tokens: null,
      }],
      page: { ...page, total: 1, returned: 1 },
      children: [], parent: null, total_cost_usd: 0.42, unattributed_cost_usd: 0,
    });
    expect(detail.items[0].member_uuids).toEqual(['civ1.turn', 'civ1.folded-a', 'civ1.folded-b']);
  });

  it('keeps a null segment cost distinct from a zero cost', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key,
      items: [
        {
          item_key: 'civ1.turn', kind: 'assistant', timestamp_utc: '2026-07-14T12:03:10Z',
          model: 'gpt-5.6-codex', blocks: [block('Carrier')],
          turn_item_key: 'civ1.turn', segment_ordinal: 0, cost_usd: 0.42, tokens: null,
        },
        {
          item_key: 'civ1.seg1', kind: 'assistant', timestamp_utc: '2026-07-14T12:04:10Z',
          model: 'gpt-5.6-codex', blocks: [block('Follower')],
          turn_item_key: 'civ1.turn', segment_ordinal: 1, cost_usd: null, tokens: null,
        },
      ],
      page, children: [], parent: null, total_cost_usd: 0.42, unattributed_cost_usd: 0,
    });
    const [carrier, follower] = detail.items;
    expect(carrier.kind).toBe('assistant');
    expect(follower.kind).toBe('assistant');
    if (carrier.kind !== 'assistant' || follower.kind !== 'assistant') return;
    expect(carrier.cost_usd).toBe(0.42);
    // NOT 0: a non-carrier segment has no cost of its own, and a zero is
    // indistinguishable from a genuinely free turn.
    expect(follower.cost_usd).toBeNull();
  });

  it('threads turn membership and the segment ordinal onto the neutral item', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key,
      items: [
        {
          item_key: 'civ1.turn', kind: 'assistant', timestamp_utc: '2026-07-14T12:03:10Z',
          model: null, blocks: [block('One')],
          turn_item_key: 'civ1.turn', segment_ordinal: 0, cost_usd: 0.1, tokens: null,
        },
        {
          item_key: 'civ1.seg1', kind: 'assistant', timestamp_utc: '2026-07-14T12:04:10Z',
          model: null, blocks: [block('Two')],
          turn_item_key: 'civ1.turn', segment_ordinal: 1, cost_usd: null, tokens: null,
        },
      ],
      page, children: [], parent: null, total_cost_usd: 0.1, unattributed_cost_usd: 0,
    });
    expect(detail.items.map((item) => item.turn_uuid)).toEqual(['civ1.turn', 'civ1.turn']);
    expect(detail.items.map((item) => item.segment_ordinal)).toEqual([0, 1]);
  });

  it('carries the outline segment keys on a channel distinct from member_uuids', () => {
    const outline = adaptQualifiedOutline(ref, {
      status: 'ok', conversation_key: ref.key,
      turns: [{
        item_key: 'civ1.turn', label: 'Reply',
        timestamp_utc: '2026-07-14T12:03:10Z', kinds: { assistant: 1 },
        member_item_keys: ['civ1.folded'],
        segment_item_keys: ['civ1.turn', 'civ1.seg1', 'civ1.seg2'],
      }],
      files: [], children: [],
    });
    expect(outline.turns[0].member_uuids).toEqual(['civ1.turn', 'civ1.folded']);
    // Placing segment keys in member_uuids would make loadToTarget's
    // "already loaded" test report true for an unfetched segment, so the drain
    // would never run and the jump would land nowhere.
    expect(outline.turns[0].segment_uuids).toEqual(['civ1.turn', 'civ1.seg1', 'civ1.seg2']);
    expect(outline.turns[0].member_uuids).not.toContain('civ1.seg1');
  });

  // #463 S1 P0 — `turns` is the NAVIGATION subset: the filter above drops every
  // event-bearing non-compaction turn, which on real Codex data is every heavy
  // assistant response and therefore every multi-segment turn. `loadToTarget`
  // needs a total document order to choose a paging direction, so the adapter
  // publishes one over the FULL wire list, dropped turns included.
  it('indexes document position over every wire turn, including ones the navigation filter drops', () => {
    const outline = adaptQualifiedOutline(ref, {
      status: 'ok', conversation_key: ref.key,
      turns: [
        {
          item_key: 'civ1.prompt', label: 'Ask', timestamp_utc: null, kinds: { user: 1 },
          segment_item_keys: ['civ1.prompt'],
        },
        {
          // Dropped from `turns`: it carries event rows and is not a compaction.
          item_key: 'civ1.reply', label: 'Long reply', timestamp_utc: null,
          kinds: { event: 22, assistant: 15, tool_call: 142 },
          member_item_keys: ['civ1.folded'],
          segment_item_keys: ['civ1.reply', 'civ1.reply.s1', 'civ1.reply.s2'],
        },
        {
          item_key: 'civ1.after', label: 'Next ask', timestamp_utc: null, kinds: { user: 1 },
          segment_item_keys: ['civ1.after'],
        },
      ],
      files: [], children: [],
    } as Parameters<typeof adaptQualifiedOutline>[1], {}, new Set(['civ1.prompt', 'civ1.after']));

    // The navigation subset genuinely excludes the reply turn ...
    expect(outline.turns.map((turn) => turn.uuid)).toEqual(['civ1.prompt', 'civ1.after']);
    // ... but its segments still have a position, in document order.
    expect(outline.positionByKey?.get('civ1.prompt')).toBe(0);
    expect(outline.positionByKey?.get('civ1.reply')).toBe(1);
    expect(outline.positionByKey?.get('civ1.reply.s1')).toBe(2);
    expect(outline.positionByKey?.get('civ1.reply.s2')).toBe(3);
    expect(outline.positionByKey?.get('civ1.after')).toBe(4);
    // A folded member key resolves to the head segment of its owning turn.
    expect(outline.positionByKey?.get('civ1.folded')).toBe(1);
  });

  // #463 S1 — the OTHER shape, and the reason it is a unit test rather than a
  // fixture assertion. A sweep of all 730 conversations in a real store on
  // 2026-08-02 found 589 multi-segment turns in the Codex corpus and no
  // multi-segment turn at all in the Claude corpus; every one of the 589 carries
  // `event > 0`, none is a `context_compacted` turn and none carries `meta > 0`,
  // so the navigation filter drops all of them. A multi-segment turn that
  // SURVIVES the filter therefore exists only in fixtures and in this test, and
  // it is the sub-path where a kept turn contributes several positions.
  it('gives a KEPT multi-segment turn one position per segment', () => {
    const outline = adaptQualifiedOutline(ref, {
      status: 'ok', conversation_key: ref.key,
      turns: [
        {
          item_key: 'civ1.ask', label: 'Ask', timestamp_utc: null, kinds: { user: 1 },
          segment_item_keys: ['civ1.ask'],
        },
        {
          // No event rows and no meta rows, so the navigation filter keeps this
          // turn while it still holds three segments.
          item_key: 'civ1.reply', label: 'Long reply', timestamp_utc: null,
          kinds: { assistant: 9 },
          member_item_keys: ['civ1.folded'],
          segment_item_keys: ['civ1.reply', 'civ1.reply.s1', 'civ1.reply.s2'],
        },
        {
          item_key: 'civ1.after', label: 'Next ask', timestamp_utc: null, kinds: { user: 1 },
          segment_item_keys: ['civ1.after'],
        },
      ],
      files: [], children: [],
    } as Parameters<typeof adaptQualifiedOutline>[1], {}, new Set(['civ1.ask', 'civ1.after']));

    // The turn is genuinely present in the navigation subset ...
    expect(outline.turns.map((turn) => turn.uuid)).toEqual(['civ1.ask', 'civ1.reply', 'civ1.after']);
    // ... and its followers still advance the document position, so the turn
    // after it is not mis-numbered.
    expect(outline.positionByKey?.get('civ1.reply')).toBe(1);
    expect(outline.positionByKey?.get('civ1.reply.s1')).toBe(2);
    expect(outline.positionByKey?.get('civ1.reply.s2')).toBe(3);
    expect(outline.positionByKey?.get('civ1.after')).toBe(4);
    expect(outline.positionByKey?.get('civ1.folded')).toBe(1);
  });

  // #463 S1 — `resolveTurnIndex` checks the own-key map before the member map so
  // that a turn listing another turn's key as a member cannot shadow the real
  // owner, and `outlineNavigation.test.ts` pins that. The position index must not
  // disagree with the skeleton index it replaces.
  it('lets a turn own its key even when an earlier turn claimed it as a member', () => {
    const positions = buildQualifiedOutlinePositions([
      { item_key: 'civ1.first', member_item_keys: ['civ1.second'] },
      { item_key: 'civ1.second' },
    ]);
    expect(positions.get('civ1.first')).toBe(0);
    expect(positions.get('civ1.second')).toBe(1);
  });
});

// ── #463 S2 — block identity survives adaptation ────────────────────────────

describe('#463 S2 adapters', () => {
  it('adapts headings onto the neutral reasoning block', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      items: [{
        item_key: 'civ1.r', kind: 'assistant', timestamp_utc: '2026-07-22T06:00:00Z',
        model: 'gpt-synthetic-codex', cost_usd: 0, tokens: null,
        blocks: [{
          kind: 'reasoning', text: 'x', block_key: 'bk',
          detail: { reasoning: {
            schema_version: 1, source: 'response_item', summary: '**A**\n**B**',
            headings: [{ key: 'bk#0', text: 'A' }, { key: 'bk#1', text: 'B' }],
          } },
        }],
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(detail.items[0].blocks[0]).toMatchObject({
      kind: 'codex_reasoning',
      headings: [{ key: 'bk#0', text: 'A' }, { key: 'bk#1', text: 'B' }],
    });
  });

  it('drops a malformed headings array rather than passing it through', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      items: [{
        item_key: 'civ1.r', kind: 'assistant', timestamp_utc: '2026-07-22T06:00:00Z',
        model: 'gpt-synthetic-codex', cost_usd: 0, tokens: null,
        blocks: [{
          kind: 'reasoning', text: 'x', block_key: 'bk',
          detail: { reasoning: {
            schema_version: 1, source: 'response_item', summary: 'S',
            headings: [{ key: 'bk#0' }, 'nope'],
          } },
        }],
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(detail.items[0].blocks[0]).not.toHaveProperty('headings');
    expect(detail.items[0].blocks[0]).toMatchObject({ summary: 'S' });
  });

  it('retains the block key on an adapted text block', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      items: [{
        item_key: 'civ1.a', kind: 'assistant', timestamp_utc: '2026-07-22T06:00:00Z',
        model: 'gpt-synthetic-codex', cost_usd: 0, tokens: null,
        blocks: [
          { kind: 'assistant', text: 'hi', block_key: 'bk0', detail: null },
          { kind: 'assistant', text: 'there', block_key: 'bk1', detail: null },
        ],
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(detail.items[0].blocks).toEqual([
      { kind: 'text', text: 'hi', block_key: 'bk0' },
      { kind: 'text', text: 'there', block_key: 'bk1' },
    ]);
    // §3.1 — the JOINED item.text stays as it is. Its consumers depend on the
    // joined form: the human turn renders it directly, both CopyButtons copy
    // it, `isSystemMarker(item.text)` folds on it, and `applyFocusMode` tests
    // it for non-emptiness.
    expect(detail.items[0].text).toBe('hi\n\nthere');
  });

  it('never places a heading key in member_uuids', () => {
    // §1.3 — `loadToTarget` reads member_uuids as "already loaded" and would
    // no-op on content that has not been fetched.
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      items: [{
        item_key: 'civ1.r', kind: 'assistant', timestamp_utc: '2026-07-22T06:00:00Z',
        model: 'gpt-synthetic-codex', cost_usd: 0, tokens: null, member_item_keys: [],
        blocks: [{
          kind: 'reasoning', text: 'x', block_key: 'bk',
          detail: { reasoning: {
            schema_version: 1, source: 'response_item', summary: '**A**',
            headings: [{ key: 'bk#0', text: 'A' }],
          } },
        }],
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(detail.items[0].member_uuids.some((u) => u.includes('#'))).toBe(false);
  });
});

// #463 S3 §5.1/§5.2 — the result-side unlock and the additive neutral model.
// The wire shapes asserted here are the published contract
// (docs/superpowers/specs/2026-08-03-463-s3-wire-contract.md), not a reading of
// server source.
describe('#463 S3 — Codex tool legibility on the neutral model', () => {
  const outputCard = (over: Record<string, unknown> = {}) => ({
    schema_version: 1, type: 'terminal_output', status: 'completed', is_error: false,
    parts: [{ type: 'text', stream: 'output', text: 'body\n' }],
    truncated: false, exit_code: null, wall_time_seconds: null, ...over,
  });

  it('publishes outcome for an uncarded Codex call and sets is_error', () => {
    const block = {
      kind: 'tool_call', detail: { name: 'wait', args: '{}' },
      output: { detail: { card: outputCard({ status: 'failed', is_error: true, exit_code: 3, wall_time_seconds: 1.5 }) } },
    };
    const [out] = adaptBlocks([block as never], 'codex');
    expect(out).toMatchObject({ kind: 'tool_call' });
    const call = out as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.outcome).toEqual({ status: 'failed', exit_code: 3, wall_time_seconds: 1.5 });
    expect(call.result?.is_error).toBe(true);
    expect(call.native_card).toBeUndefined();   // the call side is still uncarded
  });

  it('publishes a running outcome without calling it an error', () => {
    const block = {
      kind: 'tool_call', detail: { name: 'exec_command', args: '{}' },
      output: { detail: { card: outputCard({ status: 'running', wall_time_seconds: 1.0034 }) } },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.outcome).toEqual({ status: 'running', exit_code: null, wall_time_seconds: 1.0034 });
    expect(call.result?.is_error).toBe(false);
  });

  it('publishes an explicit unknown outcome rather than nothing', () => {
    const block = {
      kind: 'tool_call', detail: { name: 'wait', args: '{}' },
      output: { detail: { card: outputCard({ status: 'unknown' }) } },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.outcome?.status).toBe('unknown');
  });

  it('normalizes an unrecognized status to unknown rather than passing it through', () => {
    const block = {
      kind: 'tool_call', detail: { name: 'wait', args: '{}' },
      output: { detail: { card: outputCard({ status: 'weird-future-state' }) } },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.outcome?.status).toBe('unknown');
  });

  it('does NOT publish outcome for a qualified Claude conversation', () => {
    const block = {
      kind: 'tool_call', detail: { name: 'Bash', args: '{}' },
      output: { detail: { card: outputCard({ status: 'failed', is_error: true }) } },
    };
    const call = adaptBlocks([block as never], 'claude')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.outcome).toBeUndefined();
    expect(call.result?.is_error).toBe(false);
  });

  it('validates a program card and keeps its discriminated invocations', () => {
    const block = {
      kind: 'tool_call',
      detail: {
        name: 'exec', args: '{}',
        card: {
          schema_version: 1, type: 'program', title: null, complete: false, truncated: false,
          invocations: [
            { kind: 'command', command: 'ls -1', workdir: '/synthetic', metadata: {} },
            { kind: 'session', scope: 'shell', ref: '1', operation: 'write', chars: 'yes\n' },
            { kind: 'other', name: 'view_image' },
          ],
        },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.native_card).toEqual({
      schema_version: 1, type: 'program', title: null, complete: false, truncated: false,
      invocations: [
        { kind: 'command', command: 'ls -1', workdir: '/synthetic', metadata: {} },
        { kind: 'session', scope: 'shell', ref: '1', operation: 'write', chars: 'yes\n' },
        { kind: 'other', name: 'view_image' },
      ],
    });
  });

  it('keeps the generic disclosure for a malformed program card', () => {
    const block = {
      kind: 'tool_call',
      detail: { name: 'exec', args: '{}', card: { schema_version: 1, type: 'program', invocations: 'not-an-array' } },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.native_card).toBeUndefined();
  });

  it('keeps the generic disclosure for a program whose invocation kind is unknown', () => {
    const block = {
      kind: 'tool_call',
      detail: { name: 'exec', args: '{}', card: {
        schema_version: 1, type: 'program', title: null, complete: true, truncated: false,
        invocations: [{ kind: 'teleport', name: 'x' }],
      } },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.native_card).toBeUndefined();
  });

  it('validates session_ref at both scopes and keeps a null ref null', () => {
    const shell = adaptBlocks([{
      kind: 'tool_call', detail: { name: 'write_stdin', args: '{}', card: {
        schema_version: 1, type: 'session_ref', scope: 'shell', ref: '2',
        operation: 'write', chars: 'no\n', truncated: false } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(shell.native_card).toMatchObject({ type: 'session_ref', scope: 'shell', ref: '2', operation: 'write' });

    const cell = adaptBlocks([{
      kind: 'tool_call', detail: { name: 'wait', args: '{}', card: {
        schema_version: 1, type: 'session_ref', scope: 'cell', ref: '12',
        operation: 'poll', chars: null, truncated: false } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(cell.native_card).toMatchObject({ type: 'session_ref', scope: 'cell', ref: '12', operation: 'poll', chars: null });

    // §3.2 coverage limitation: a session named only inside a program body has
    // no ordinal, and the client must carry that through as null rather than
    // inventing one.
    const unnamed = adaptBlocks([{
      kind: 'tool_call', detail: { name: 'write_stdin', args: '{}', card: {
        schema_version: 1, type: 'session_ref', scope: 'shell', ref: null,
        operation: 'write', chars: 'y', truncated: false } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect((unnamed.native_card as { ref: string | null }).ref).toBeNull();
  });

  it('keeps the generic disclosure for a session_ref with an unknown scope', () => {
    const call = adaptBlocks([{
      kind: 'tool_call', detail: { name: 'write_stdin', args: '{}', card: {
        schema_version: 1, type: 'session_ref', scope: 'process', ref: '1',
        operation: 'write', chars: null, truncated: false } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.native_card).toBeUndefined();
  });

  it('validates a tool_search card', () => {
    const call = adaptBlocks([{
      kind: 'tool_call', detail: { name: 'tool_search_call', args: '{}', card: {
        schema_version: 1, type: 'tool_search', query: 'synthetic', limit: 5 } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.native_card).toEqual({ schema_version: 1, type: 'tool_search', query: 'synthetic', limit: 5 });
  });

  it('carries per-file truncated and diff_source through a patch event card', () => {
    const call = adaptBlocks([{
      kind: 'event', block_key: 'ev1', text: 'patch_apply',
      detail: { event: 'patch_apply_end', card: {
        schema_version: 1, type: 'patch', source: 'patch_apply_end', status: 'completed',
        success: true, stdout: '', stderr: '', has_diff: true, truncated: false,
        files: [
          { path: '/s/a.py', status: 'add', truncated: false, diff_source: 'derived',
            unified_diff: '--- /dev/null\n+++ /s/a.py\n@@ -0,0 +1,1 @@\n+one\n' },
          { path: '/s/b.py', status: 'update', truncated: true, diff_source: 'retained' },
        ] } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    const card = call.native_card as Extract<NativeToolCard, { type: 'patch' }>;
    expect(card.files[0]).toMatchObject({ truncated: false, diff_source: 'derived' });
    // "there is a diff and none of it survived the budget" — truncated with no
    // renderable diff is a real, distinct state (wire contract §4).
    expect(card.files[1]).toMatchObject({ truncated: true, diff_source: 'retained' });
    expect(card.files[1].unified_diff).toBeUndefined();
  });

  it('never lifts a diff key onto a call-side apply_patch file entry', () => {
    const call = adaptBlocks([{
      kind: 'tool_call', detail: { name: 'apply_patch', args: '{}', card: {
        schema_version: 1, type: 'patch', source: 'apply_patch', status: 'completed',
        patch: '*** Begin Patch\n', truncated: false,
        files: [{ path: 'synthetic.txt', status: 'modified' }],
        completion: {
          schema_version: 1, type: 'patch', source: 'patch_apply_end', status: 'completed',
          success: true, stdout: '', stderr: '', has_diff: false, truncated: false,
          files: [{ path: 'synthetic.txt', status: 'update', truncated: false }],
          event_block_key: 'ev9',
        } } },
    } as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    const card = call.native_card as Extract<NativeToolCard, { type: 'patch' }>;
    expect(card.request_files).toEqual([{ path: 'synthetic.txt', status: 'modified' }]);
    expect(card.request_files?.[0]).not.toHaveProperty('truncated');
    expect(card.request_files?.[0]).not.toHaveProperty('diff_source');
  });

  it('adapts the conversation-level session index onto the detail', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      session_index: {
        sessions: { '1': { ordinal: 1, opener_block_key: 'cbk1_open' }, '2': { ordinal: 2, opener_block_key: null } },
        truncated: false,
      },
      items: [{
        item_key: 'civ1.a', kind: 'assistant', timestamp_utc: '2026-08-02T09:00:00Z',
        model: 'gpt-synthetic-codex', cost_usd: 0, tokens: null,
        blocks: [{ kind: 'assistant', text: 'hi', block_key: 'bk0', detail: null }],
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(detail.session_index).toEqual({
      sessions: { '1': { ordinal: 1, opener_block_key: 'cbk1_open' }, '2': { ordinal: 2, opener_block_key: null } },
      truncated: false,
    });
  });

  it('drops a malformed session index rather than passing it through half-built', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      session_index: { sessions: { '1': { ordinal: 'one', opener_block_key: null } }, truncated: false },
      items: [],
      page: { total: 0, returned: 0, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(detail.session_index).toBeUndefined();
  });

  it('builds the session map without a prototype, so a "__proto__" key stays data', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      // Built through JSON.parse because an object LITERAL treats `__proto__`
      // as the prototype setter, while the wire arrives parsed — where it is an
      // ordinary own key, exactly as a hostile payload would deliver it.
      session_index: JSON.parse('{"sessions":{"__proto__":{"ordinal":1,"opener_block_key":null}},"truncated":false}'),
      items: [],
      page: { total: 0, returned: 0, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    const sessions = detail.session_index?.sessions as Record<string, unknown> | undefined;
    expect(Object.getPrototypeOf(sessions)).toBeNull();
    expect(Object.keys(sessions ?? {})).toEqual(['__proto__']);
  });

  it('names a program\'s leading session invocation the way the card body does', () => {
    // One thing, one name: the collapsed row said "wrote to shell 1" while the
    // expanded body said "session 1" for the same invocation.
    const block = {
      kind: 'tool_call',
      detail: {
        name: 'exec', args: '{}',
        card: {
          schema_version: 1, type: 'program', title: null, complete: true, truncated: false,
          invocations: [
            { kind: 'session', scope: 'shell', ref: '1', operation: 'write', chars: 'y\n' },
            { kind: 'command', command: 'ls', workdir: null, metadata: {} },
          ],
        },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.preview).toBe('wrote to session 1 +1 more');
  });

  it('names a program\'s leading cell invocation as a cell', () => {
    const block = {
      kind: 'tool_call',
      detail: {
        name: 'exec', args: '{}',
        card: {
          schema_version: 1, type: 'program', title: null, complete: true, truncated: false,
          invocations: [{ kind: 'session', scope: 'cell', ref: '12', operation: 'poll', chars: null }],
        },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.preview).toBe('polled cell 12');
  });

  it('claims no reference at all for a program session the server could not resolve', () => {
    const block = {
      kind: 'tool_call',
      detail: {
        name: 'exec', args: '{}',
        card: {
          schema_version: 1, type: 'program', title: null, complete: true, truncated: false,
          invocations: [{ kind: 'session', scope: 'shell', ref: null, operation: 'write', chars: null }],
        },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.preview).toBe('wrote to');
  });

  it('clamps a session_ref preview to the same bound the card body uses', () => {
    const chars = 'x'.repeat(200);
    const block = {
      kind: 'tool_call',
      detail: {
        name: 'write_stdin', args: '{}',
        card: {
          schema_version: 1, type: 'session_ref', scope: 'shell', ref: '1',
          operation: 'write', chars, truncated: false,
        },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.preview).toBe(`session 1 · ${'x'.repeat(60)}…`);
  });

  it('falls through a blank authored title to the first recognized invocation', () => {
    // A bare `??` treats `""` as a value, so a blank authored title produced an
    // EMPTY collapsed row where the pre-S3 fallback showed something.
    const block = {
      kind: 'tool_call', text: 'js\n{"title":""}',
      detail: {
        name: 'js', args: '{"title":""}',
        card: {
          schema_version: 1, type: 'program', title: '', complete: true, truncated: false,
          invocations: [{ kind: 'other', name: 'view_image' }],
        },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.preview).toBe('view_image');
  });

  it('falls back to the first text line when a tool_search query is blank', () => {
    const block = {
      kind: 'tool_call', text: 'tool_search_call\n{"query":""}',
      detail: {
        name: 'tool_search_call', args: '{"query":""}',
        card: { schema_version: 1, type: 'tool_search', query: '', limit: null },
      },
    };
    const call = adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(call.preview).toBe('tool_search_call');
  });

  it('renders the external-agent marker as structure and removes its span from the prose', () => {
    const text = 'Delegating to the search tool.\n\n[external_agent_tool_call: ToolSearch]\ninput: {"query": "select:A,B"}';
    const blocks = adaptBlocks([{
      kind: 'assistant', block_key: 'bk9', text,
      detail: { external_call: {
        schema_version: 1, name: 'ToolSearch', input: { query: 'select:A,B' },
        truncated: false, span: [32, text.length],
      } },
    } as never], 'codex');
    expect(blocks).toEqual([
      { kind: 'text', text: 'Delegating to the search tool.', block_key: 'bk9' },
      { kind: 'external_call', name: 'ToolSearch', input: { query: 'select:A,B' }, truncated: false, block_key: 'bk9' },
    ]);
  });

  it('renders the prose ALONE when the span does not resolve', () => {
    // Wire contract §7 — the span exists to stop the marker being shown twice,
    // once as prose and once as the structured disclosure. When it does not
    // resolve the prose is served whole, so emitting the block as well is the
    // very double render the span prevents. Degrade to the prose.
    const text = 'no marker here';
    const blocks = adaptBlocks([{
      kind: 'assistant', block_key: 'bk9', text,
      detail: { external_call: {
        schema_version: 1, name: 'ToolSearch', input: {}, truncated: false, span: [4, 900],
      } },
    } as never], 'codex');
    expect(blocks).toEqual([{ kind: 'text', text: 'no marker here', block_key: 'bk9' }]);
  });

  it('renders the prose ALONE when the card carries no span at all', () => {
    const blocks = adaptBlocks([{
      kind: 'assistant', block_key: 'bk9', text: 'prose with no span',
      detail: { external_call: {
        schema_version: 1, name: 'ToolSearch', input: {}, truncated: false,
      } },
    } as never], 'codex');
    expect(blocks).toEqual([{ kind: 'text', text: 'prose with no span', block_key: 'bk9' }]);
  });

  it('ignores an external_call on a human turn', () => {
    // The server publishes the marker on `assistant` blocks only. A `user`
    // block renders its prose from `item.text`, which retains the marker, so
    // honouring the field here would render it twice.
    const text = 'ask\n\n[external_agent_tool_call: ToolSearch]\ninput: {}';
    const blocks = adaptBlocks([{
      kind: 'user', block_key: 'bk9', text,
      detail: { external_call: {
        schema_version: 1, name: 'ToolSearch', input: {}, truncated: false,
        span: [5, text.length],
      } },
    } as never], 'codex');
    expect(blocks).toEqual([{ kind: 'text', text, block_key: 'bk9' }]);
  });

  it('alters nothing but the orphaned newline the removal leaves behind', () => {
    // The non-marker path serves `text` exactly as stored, so the marker path
    // must strip only the newline the removal orphaned. A whole-remainder trim
    // also swallows a two-space Markdown hard break at the end of the prose
    // that FOLLOWS the marker, which the reader then renders as one run-on line.
    const head = 'before\n\n';
    const marker = '[external_agent_tool_call: ToolSearch]\ninput: {}\n';
    const text = `${head}${marker}after  `;
    const blocks = adaptBlocks([{
      kind: 'assistant', block_key: 'bk9', text,
      detail: { external_call: {
        schema_version: 1, name: 'ToolSearch', input: {}, truncated: false,
        span: [head.length, head.length + marker.length],
      } },
    } as never], 'codex');
    expect(blocks[0]).toEqual({ kind: 'text', text: 'before\n\nafter  ', block_key: 'bk9' });
  });

  it('keeps ordinary prose for a malformed external_call', () => {
    const blocks = adaptBlocks([{
      kind: 'assistant', block_key: 'bk9', text: 'prose',
      detail: { external_call: { schema_version: 1, name: '', input: {}, truncated: false } },
    } as never], 'codex');
    expect(blocks).toEqual([{ kind: 'text', text: 'prose', block_key: 'bk9' }]);
  });
});

// #463 S3 §6.5 — the privacy gate on the CLIENT model. The provider's own
// session id is never published on a card, so no S3-derived field on the
// adapted model can carry it. The pre-existing generic disclosure
// (`detail.args` → `input` / `input_summary`) is the one recorded boundary and
// carried the token long before S3; the wire contract names it in §8.
describe('#463 S3 — no raw provider identifier reaches an S3-derived field', () => {
  const wireBlock = {
    kind: 'tool_call',
    block_key: 'cbk1_stdin',
    detail: {
      name: 'write_stdin',
      args: '{"chars":"yes\\n","session_id":70001}',
      card: {
        schema_version: 1, type: 'session_ref', scope: 'shell', ref: '1',
        operation: 'write', chars: 'yes\n', truncated: false,
      },
    },
    output: { text: 'ok\n', detail: { card: {
      schema_version: 1, type: 'terminal_output', status: 'completed', is_error: false,
      parts: [{ type: 'text', stream: 'output', text: 'ok\n' }],
      truncated: false, exit_code: 0, wall_time_seconds: 0.5,
    } } },
  };

  it('keeps the token out of every field S3 added', () => {
    const [block] = adaptBlocks([wireBlock as never], 'codex');
    const call = block as Extract<ConversationBlock, { kind: 'tool_call' }>;
    for (const derived of [call.native_card, call.outcome, call.preview]) {
      expect(JSON.stringify(derived ?? null)).not.toContain('70001');
    }
    // Non-vacuity: the token really is in this fixture, on the one recorded
    // boundary, so the assertions above are not passing over empty fields.
    expect(JSON.stringify(call.input)).toContain('70001');
    expect(call.input_summary).toContain('70001');
    // And the reference the reader actually sees is the conversation-local
    // ordinal, not the provider id.
    expect((call.native_card as { ref: string }).ref).toBe('1');
  });

  it('keeps the token out of the adapted session index', () => {
    const detail = adaptQualifiedDetail(ref, {
      status: 'ok', conversation_key: ref.key, title: 't',
      session_index: { sessions: { '1': { ordinal: 1, opener_block_key: 'cbk1_open' } }, truncated: false },
      items: [{
        item_key: 'civ1.a', kind: 'assistant', timestamp_utc: '2026-08-02T09:00:00Z',
        model: 'gpt-synthetic-codex', cost_usd: 0, tokens: null, blocks: [wireBlock],
      }],
      page: { total: 1, returned: 1, before: null, after: null, has_before: false, has_after: false },
      children: [], parent: null, total_cost_usd: 0, unattributed_cost_usd: 0, tokens: null,
    } as Parameters<typeof adaptQualifiedDetail>[1]);
    expect(JSON.stringify(detail.session_index)).not.toContain('70001');
  });
});

// #463 S4 Task 7 — tier-1 enrichment, tier-2 landmarks, and the retention rule.
describe('#463 S4 — the two-tier outline model', () => {
  // A wire envelope shaped like real Codex data: the heavy assistant turn
  // carries event rows, so the navigation filter drops it, and it is the turn
  // that owns every landmark. S1 measured all 589 multi-segment Codex turns in
  // the corpus carrying event rows and being dropped by that filter.
  type OutlineWire = Parameters<typeof adaptQualifiedOutline>[1];
  const heavyEnvelope = (): OutlineWire => ({
    status: 'ok', conversation_key: ref.key,
    turns: [
      {
        item_key: 'civ1.prompt', label: 'Ask', timestamp_utc: null,
        kinds: { user: 1 }, segment_item_keys: ['civ1.prompt'],
      },
      {
        item_key: 'civ1.reply', label: 'Long reply', timestamp_utc: '2026-08-02T09:00:00Z',
        kinds: { event: 22, assistant: 15, tool_call: 142 },
        member_item_keys: ['civ1.folded'],
        segment_item_keys: ['civ1.reply', 'civ1.reply.s1', 'civ1.reply.s2'],
        tools: [
          { name: 'exec', is_error: true },
          { name: 'apply_patch', is_error: false },
        ],
        tool_call_count: 142,
        first_failure_name: 'exec',
        thinking: ['Read the failing case', 'Apply the patch'],
        model: 'gpt-synthetic-codex',
      },
    ],
    landmarks: [
      {
        landmark_key: 'cbk1.head#0', block_key: 'cbk1.head', item_key: 'civ1.reply',
        parent_item_key: 'civ1.reply', kind: 'reasoning' as const,
        label: 'Read the failing case', timestamp_utc: '2026-08-02T09:00:01Z',
      },
      {
        landmark_key: 'cbk1.err#tool_error', block_key: 'cbk1.err',
        item_key: 'civ1.reply.s2', parent_item_key: 'civ1.reply',
        kind: 'tool_error', label: 'exec',
        timestamp_utc: '2026-08-02T09:00:07Z',
      },
      {
        landmark_key: 'cbk1.plan#plan', block_key: 'cbk1.plan',
        item_key: 'civ1.reply.s1', parent_item_key: 'civ1.reply',
        kind: 'plan', label: 'update_plan',
        timestamp_utc: '2026-08-02T09:00:05Z',
      },
    ],
    stats: {
      items: 2, kinds: { user: 1, assistant: 15 },
      tool_counts: { exec: 2, apply_patch: 1 }, error_count: 1,
      models: { 'gpt-synthetic-codex': 1 }, duration_seconds: 181,
    },
    files: [{
      file_path: 'src/app.ts', tool: 'apply_patch', count: 2,
      added: 5, removed: 1,
      touches: [
        { item_key: 'civ1.reply.s1', timestamp_utc: '2026-08-02T09:00:05Z', op: 'update' },
        { item_key: 'civ1.reply.s2', timestamp_utc: '2026-08-02T09:00:09Z', op: 'add' },
      ],
    }],
    children: [],
  });

  it('retains an event-bearing turn that owns a landmark', () => {
    const wire = heavyEnvelope();
    // Non-vacuity: with no landmarks the existing filter drops it, which is
    // exactly why every landmark would otherwise be an orphan.
    const without = adaptQualifiedOutline(
      ref, { ...wire, landmarks: [] }, {}, new Set(['civ1.prompt']));
    expect(without.turns.map((turn) => turn.uuid)).toEqual(['civ1.prompt']);

    const outline = adaptQualifiedOutline(
      ref, wire, {}, new Set(['civ1.prompt']));
    expect(outline.turns.map((turn) => turn.uuid)).toEqual(['civ1.prompt', 'civ1.reply']);
    // The retained turn keeps its document position, so the merged rail reads
    // in wire order rather than with the landmark owner appended at the end.
    expect(outline.turns[1].kind).toBe('assistant');
  });

  it('populates the tier-1 enrichment the jump families and stats card consume', () => {
    const outline = adaptQualifiedOutline(
      ref, heavyEnvelope(), {}, new Set(['civ1.prompt']));
    const reply = outline.turns[1];
    expect(reply.tools).toEqual([
      { name: 'exec', is_error: true },
      { name: 'apply_patch', is_error: false },
    ]);
    // Dedupe destroys both of these, so the wire republishes them (§4.4).
    expect(reply.tool_call_count).toBe(142);
    expect(reply.first_failure_name).toBe('exec');
    expect(reply.thinking).toEqual(['Read the failing case', 'Apply the patch']);
    expect(reply.model).toBe('gpt-synthetic-codex');
    // Deliberately hardcoded (§6.2): Codex nests through separate child
    // conversations, and `cache_failure` is a Claude concept.
    expect(reply.subagent_key).toBeNull();
    expect(reply.cache_failure).toBeUndefined();
  });

  it('maps landmarks onto their segment anchor and their owning turn', () => {
    const outline = adaptQualifiedOutline(
      ref, heavyEnvelope(), {}, new Set(['civ1.prompt']));
    expect(outline.landmarks).toEqual([
      {
        landmark_key: 'cbk1.head#0', block_key: 'cbk1.head', uuid: 'civ1.reply',
        parent_uuid: 'civ1.reply', kind: 'reasoning', label: 'Read the failing case',
        ts: '2026-08-02T09:00:01Z',
      },
      {
        landmark_key: 'cbk1.err#tool_error', block_key: 'cbk1.err',
        uuid: 'civ1.reply.s2', parent_uuid: 'civ1.reply', kind: 'tool_error',
        label: 'exec', ts: '2026-08-02T09:00:07Z',
      },
      {
        landmark_key: 'cbk1.plan#plan', block_key: 'cbk1.plan',
        uuid: 'civ1.reply.s1', parent_uuid: 'civ1.reply', kind: 'plan',
        label: 'update_plan', ts: '2026-08-02T09:00:05Z',
      },
    ]);
    // The anchor is a SEGMENT, which is what makes a jump land within forty
    // blocks of the failure rather than at the top of a 142-call turn.
    expect(outline.turns[1].segment_uuids).toContain(outline.landmarks![1].uuid);
  });

  it('publishes Codex files in the rich shape and omits provider_files', () => {
    const outline = adaptQualifiedOutline(
      ref, heavyEnvelope(), {}, new Set(['civ1.prompt']));
    expect(outline.files).toEqual([{
      path: 'src/app.ts', add: 5, del: 1,
      touches: [
        { uuid: 'civ1.reply.s1', tool_use_id: null, op: 'update', add: null, del: null },
        { uuid: 'civ1.reply.s2', tool_use_id: null, op: 'add', add: null, del: null },
      ],
    }]);
    // Rendering both arrays would list every Codex file twice, and the inert
    // provider row is what kept the rich FileRow path unreachable (§6.6).
    expect(outline.provider_files).toBeUndefined();
  });

  it('keeps a count-free file shape on provider_files', () => {
    const outline = adaptQualifiedOutline(ref, {
      status: 'ok', conversation_key: ref.key,
      turns: [{ item_key: 'civ1.prompt', label: 'Ask', timestamp_utc: null, kinds: { user: 1 } }],
      files: [{ file_path: 'src/app.ts', tool: 'patch_apply', count: 2 }],
      children: [],
    } as Parameters<typeof adaptQualifiedOutline>[1], {}, new Set(['civ1.prompt']));
    expect(outline.files).toEqual([]);
    expect(outline.provider_files).toEqual([{ path: 'src/app.ts', tool: 'patch_apply', count: 2 }]);
  });

  it('carries a null error_count through instead of coercing it to zero', () => {
    const wire = heavyEnvelope();
    const outline = adaptQualifiedOutline(ref, {
      ...wire, stats: { ...wire.stats, error_count: null },
    }, {}, new Set(['civ1.prompt']));
    // `0` and `null` are different claims: one says nothing failed, the other
    // says nobody could tell. Coercing turned the second into the first.
    expect(outline.stats.error_count).toBeNull();
  });
});

// #463 S4 §6.4 — the client half of ONE enumerated definition of a failed call.
// The server kernel applies the uniform `{failed, error}` set to all four
// disjuncts. Until these two edits landed, the server flagged a `status:
// "error"` call as failed while the client resolved that card to `unknown` and
// rendered no error badge — so the reader would jump to a call the interface
// insists succeeded. §8.3 names that divergence as a risk and this is its
// control: the two sides ship together.
describe('#463 S4 — the client agrees with the server about a failed call', () => {
  const outputCard = (over: Record<string, unknown> = {}) => ({
    schema_version: 1, type: 'terminal_output', status: 'completed', is_error: false,
    parts: [{ type: 'text', stream: 'output', text: 'body\n' }],
    truncated: false, exit_code: null, wall_time_seconds: null, ...over,
  });
  const call = (block: unknown) =>
    adaptBlocks([block as never], 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;

  it('treats a terminal status of "error" as a failure, not as unknown', () => {
    // `OUTCOME_STATUSES` excluded 'error', so the card collapsed to 'unknown'
    // and `outcome?.status === 'failed'` never fired. §6.4 takes correctness
    // over bug-compatibility: reproducing the client "exactly" would have
    // frozen the defect.
    const out = call({
      kind: 'tool_call', detail: { name: 'exec_command', args: '{}' },
      output: { detail: { card: outputCard({ status: 'error' }) } },
    });
    expect(out.outcome?.status).toBe('error');
    expect(out.result?.is_error).toBe(true);
  });

  it('treats a web or MCP completion status of "failed" as a failure too', () => {
    // The kernel applies ONE failure-status set to all four disjuncts. Reading
    // `error` on web and MCP but `failed` on the others would mean a web
    // completion carrying `failed` is a failure nobody flags — the same defect
    // as the 'error' collapse with the two words exchanged.
    const web = call({
      kind: 'tool_call',
      detail: {
        name: 'web_search', args: '{}',
        card: {
          schema_version: 1, type: 'web_search', source: 'web_search_call',
          call_status: 'completed', query: 'q', action: {},
          completion: { status: 'failed', query: 'q', action: {}, results: [], error: 'upstream refused' },
        },
      },
    });
    expect(web.result?.is_error).toBe(true);
    const mcp = call({
      kind: 'tool_call',
      detail: {
        name: 'fixture_tool', args: '{}',
        card: {
          schema_version: 1, type: 'mcp', source: 'function_call', name: 'fixture_tool',
          call_status: 'completed',
          completion: {
            server: 'fixture', tool: 't', arguments: {}, duration: { secs: 0, nanos: 1 },
            result: { Err: 'nope' }, status: 'failed',
          },
        },
      },
    });
    expect(mcp.result?.is_error).toBe(true);
  });

  it('still refuses to call running or unknown a failure', () => {
    // `unknown` is a real state covering 17.6% of outputs — 4,585 of them are
    // open sessions, measured — rather than an absence.
    for (const status of ['running', 'unknown']) {
      const out = call({
        kind: 'tool_call', detail: { name: 'wait', args: '{}' },
        output: { detail: { card: outputCard({ status }) } },
      });
      expect(out.result?.is_error).toBe(false);
    }
  });

  // §7.3 — the second assertion. The shipped version asserted only
  // `result === null`, which holds for ANY classification of `status: 'error'`
  // — including one that ignores it — so it could not go red. This one pins the
  // relationship it names: `'error'` must be a failure status, and the web
  // family must read the same set as every other.
  it('flags a web completion whose status is the word `error`', () => {
    const web = (status: string) => call({
      kind: 'tool_call',
      detail: {
        name: 'web_search', args: '{}',
        card: {
          schema_version: 1, type: 'web_search', source: 'web_search_call',
          call_status: 'completed', query: 'q', action: {},
          completion: { status, query: 'q', action: {}, results: [], error: 'upstream 502' },
        },
      },
    });
    // Drop `'error'` from FAILED_STATUSES and this flips to false.
    expect(web('error').result?.is_error).toBe(true);
    // Non-vacuity in the other direction: the flag is not simply always on.
    expect(web('completed').result?.is_error).toBe(false);
  });

  // §7.3 / §8.2, corrected. The spec claimed such a card "can have
  // `outputError === true` while `result` stays null, so the filter and the
  // badge are not the same set even in principle". They ARE the same set here:
  // `outcome` is terminal-only, so the badge has nothing to read either, and a
  // card with no renderable output reports its failure on NEITHER surface.
  it('reports a resultless web failure on neither surface, badge included', () => {
    const out = call({
      kind: 'tool_call',
      detail: {
        name: 'web_search', args: '{}',
        card: {
          schema_version: 1, type: 'web_search', source: 'web_search_call',
          call_status: 'completed', query: 'q', action: {},
          completion: { status: 'error', query: 'q', action: {}, results: [] },
        },
      },
    });
    expect(out.result).toBeNull();
    expect(out.outcome).toBeUndefined();
  });
});

// #463 S4 F-A — the finer jump anchor has to survive adaptation, because the
// landmark names a physical row and the reader can only find it in the DOM if
// the adapted block still carries that row's key.
describe('#463 S4 F-A — block keys reach the rendered block', () => {
  it('retains the block key on an adapted Codex tool call', () => {
    const out = adaptBlocks([{
      kind: 'tool_call', block_key: 'cbk1.e1', detail: { name: 'exec', args: '{}' },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(out.block_key).toBe('cbk1.e1');
  });

  it('retains the block key on an adapted patch_apply_end event', () => {
    const out = adaptBlocks([{
      kind: 'event', block_key: 'cbk1.pa',
      detail: {
        card: {
          schema_version: 1, type: 'patch', source: 'patch_apply_end',
          success: true, files: [{ path: 'a.py', kind: 'update' }],
        },
      },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;
    expect(out.block_key).toBe('cbk1.pa');
  });

  it('retains the block key on an adapted Codex reasoning block', () => {
    const out = adaptBlocks([{
      kind: 'reasoning', block_key: 'cbk1.r',
      detail: { reasoning: { schema_version: 1, title: 'Checking', source: 'codex' } },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'codex_reasoning' }>;
    expect(out.kind).toBe('codex_reasoning');
    expect(out.block_key).toBe('cbk1.r');
  });
});

// C-4 — an unfolded failing `tool_output` is its own group head and therefore
// its own landmark, so the outline can publish its block key as a jump address.
// The adapter dropped that key on the floor, so the address named nothing.
describe('#463 S4 remediation — the unfolded tool_output keeps its address', () => {
  it('retains the block key on an adapted Codex tool_output block', () => {
    const out = adaptBlocks([{
      kind: 'tool_output', text: 'boom', block_key: 'cbk1.o',
      call_id: 'call-shared', detail: { is_error: true },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_result' }>;
    expect(out.kind).toBe('tool_result');
    expect(out.is_error).toBe(true);
    expect(out.block_key).toBe('cbk1.o');
  });

  // Round 3 — found by the golden F3 added, not by reading the code. The wire
  // block for the ambiguous-call output carries its verdict on the DECODED CARD
  // (`detail.card.is_error` / `detail.card.status`), which is where
  // `decode_tool_output_card` writes it; `detail.is_error` is the Claude-shaped
  // field and is absent on every Codex output. So the one block the C-4 work
  // exists to make addressable rendered without the failure treatment, and the
  // new golden would have certified `is_error: false` for a row the server had
  // already classified as a failure and published a `tool_error` landmark for.
  it('reads the failure verdict off the decoded card on a Codex tool_output', () => {
    const out = adaptBlocks([{
      kind: 'tool_output', text: 'which call was this\n', block_key: 'cbk1.amb',
      call_id: 'shared', detail: {
        card: {
          schema_version: 1, type: 'terminal_output', status: 'failed',
          is_error: true, truncated: false, exit_code: null,
          parts: [{ type: 'text', stream: 'output', text: 'which call was this\n' }],
        },
      },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_result' }>;
    expect(out.is_error).toBe(true);
  });

  // The sibling field on the same card, swept with it: `truncated` was hard
  // coded `false` on this branch, so an output the text budget had cut rendered
  // as if it were whole. A Claude `tool_result` decodes no card and keeps
  // `false`, which is what it always was.
  it('reports truncation from the same decoded card', () => {
    const out = adaptBlocks([{
      kind: 'tool_output', text: 'partial', block_key: 'cbk1.cut', call_id: 'shared',
      detail: {
        card: {
          schema_version: 1, type: 'terminal_output', status: 'completed',
          is_error: false, truncated: true, exit_code: 0,
          parts: [{ type: 'text', stream: 'output', text: 'partial' }],
        },
      },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_result' }>;
    expect(out.truncated).toBe(true);
  });

  it('does not invent a failure on a decoded card that completed', () => {
    const out = adaptBlocks([{
      kind: 'tool_output', text: 'ok\n', block_key: 'cbk1.ok', call_id: 'shared',
      detail: {
        card: {
          schema_version: 1, type: 'terminal_output', status: 'completed',
          is_error: false, truncated: false, exit_code: 0,
          parts: [{ type: 'text', stream: 'output', text: 'ok\n' }],
        },
      },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_result' }>;
    expect(out.is_error).toBe(false);
  });

  it('retains the block key on an adapted Claude tool_result block', () => {
    const out = adaptBlocks([{
      kind: 'tool_result', text: 'boom', block_key: 'cbk1.tr',
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_result' }>;
    expect(out.block_key).toBe('cbk1.tr');
  });

  // The sibling site: an UNFOLDED `web_search_end` / `mcp_tool_call_end` falls
  // through to the prose fallback, and a failing one of those is a landmark
  // anchored on its own block key.
  it('retains the block key on the prose fallback for an unfolded event', () => {
    const out = adaptBlocks([{
      kind: 'event', text: 'web_search_end', block_key: 'cbk1.ev',
      detail: { event: 'web_search_end' },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'text' }>;
    expect(out.kind).toBe('text');
    expect(out.block_key).toBe('cbk1.ev');
  });

  // Round 3 (F12) — the prose fallback was gated on `block.text`, so an event
  // row with no display text emitted NO block and a landmark anchored on it had
  // no address at all. Pre-existing rather than introduced by S4, and inert on
  // the store measured here — 0 of the 219,503 normalized rows in the real
  // conversations database have an empty display text on any kind that reaches
  // this branch (11,479 event rows and 2,982 meta rows, all non-empty via
  // `_row_display`'s text / search_thinking / search_tool chain) — but the
  // address is exactly what a `tool_error` landmark on such a row needs, so the
  // block is emitted for its key rather than dropped.
  it('emits a keyed anchor for an unfolded event with no display text', () => {
    const out = adaptBlocks([{
      kind: 'event', text: '', block_key: 'cbk1.silent',
      detail: { event: 'mcp_tool_call_end' },
    }] as never, 'codex') as Extract<ConversationBlock, { kind: 'text' }>[];
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe('text');
    expect(out[0].text).toBe('');
    expect(out[0].block_key).toBe('cbk1.silent');
  });

  it('still emits nothing for a text-less block that carries no key to anchor', () => {
    expect(adaptBlocks([{
      kind: 'event', text: '', detail: { event: 'mcp_tool_call_end' },
    }] as never, 'codex')).toEqual([]);
  });
});

// ---- #463 S4 remediation round 4 — the last two Claude-shaped verdicts ------
//
// `classify_tool_failure` (`bin/_lib_codex_landmarks.py`) treats a patch as
// failed on `success is False` OR `status in {"failed", "error"}`, and
// `decode_patch_event_card` passes the provider's raw status through. The
// client tested `status === 'failed'` as a bare literal on both the adapter and
// the card, so a `patch_apply_end` carrying `status: "error"` without
// `success: false` got a `tool_error` landmark from the server — counted by the
// Errors badge — while the client called it not-failed and the Errors filter
// hid the very turn the badge had counted.
describe('#463 S4 — a patch event reads the same failure-status set as the server', () => {
  const patchEvent = (card: Record<string, unknown>) => adaptBlocks([{
    kind: 'event', block_key: 'cbk1.pa', text: 'patch',
    detail: {
      card: {
        schema_version: 1, type: 'patch', source: 'patch_apply_end',
        files: [{ path: 'a.py', status: 'update' }], has_diff: false,
        stdout: '', stderr: '', truncated: false, ...card,
      },
    },
  }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;

  it('calls a patch whose status is the word `error` a failure', () => {
    expect(patchEvent({ status: 'error' }).result?.is_error).toBe(true);
  });

  it('keeps calling `failed` a failure', () => {
    expect(patchEvent({ status: 'failed' }).result?.is_error).toBe(true);
  });

  it('keeps calling `success: false` a failure whatever the status says', () => {
    expect(patchEvent({ status: 'completed', success: false }).result?.is_error).toBe(true);
  });

  it('does not invent a failure on a completed patch', () => {
    expect(patchEvent({ status: 'completed', success: true }).result?.is_error).toBe(false);
  });
});

// P3-3 — the `FAILED_STATUSES.has(outputCard?.status)` disjunct on the
// unfolded-`tool_output` branch was never exercised on its own: the three
// round-3 tests set `is_error` and `status` consistently, so the earlier
// disjunct short-circuited and the status read could have been deleted without
// reddening anything.
describe('#463 S4 — the tool_output status disjunct stands on its own', () => {
  it('flags a card whose is_error is false but whose status is `error`', () => {
    const out = adaptBlocks([{
      kind: 'tool_output', text: 'boom\n', block_key: 'cbk1.statusonly', call_id: 'shared',
      detail: {
        card: {
          schema_version: 1, type: 'terminal_output', status: 'error',
          is_error: false, truncated: false, exit_code: 1,
          parts: [{ type: 'text', stream: 'output', text: 'boom\n' }],
        },
      },
    }] as never, 'codex')[0] as Extract<ConversationBlock, { kind: 'tool_result' }>;
    expect(out.is_error).toBe(true);
  });
});

// P3-1 — `adaptQualifiedPayload` hard-coded `is_error: false` on the result
// branch, discarding the verdict the server publishes on the same card the
// paged detail path reads. Dead today, because `useFullPayload` consumers read
// only `text`/`input`, but it is the third instance of the same Claude-shaped
// assumption and the one a future consumer would trust.
describe('#463 S4 — the full-payload result branch keeps the server verdict', () => {
  const payload = (card?: NativeToolCard | NativeTerminalOutput) => {
    const out = adaptQualifiedPayload('cbk1.o', 'result', {
      which: 'output', content: 'boom\n', truncated: false, card,
    });
    // Narrow the discriminated union so `is_error` is reachable at all — the
    // field exists only on the result arm, which is the point of the branch.
    if (out.which !== 'result') throw new Error(`expected a result payload, got ${out.which}`);
    return out;
  };

  // Round 5 — no cast. The body annotation is a union that includes
  // `NativeTerminalOutput`, so the card this branch really receives is now
  // expressible; before, the only way to hand the adapter a correct card was to
  // cast it through `unknown` into a union it does not belong to.
  const outputCard = (over: Partial<NativeTerminalOutput>): NativeTerminalOutput => ({
    schema_version: 1, type: 'terminal_output', status: 'completed',
    is_error: false, truncated: false, exit_code: 0,
    parts: [{ type: 'text', stream: 'output', text: 'boom\n' }],
    ...over,
  });

  it('reports the failure the card states', () => {
    expect(payload(outputCard({ is_error: true, status: 'failed' })).is_error).toBe(true);
  });

  it('reports a failure stated only by the status', () => {
    expect(payload(outputCard({ is_error: false, status: 'error' })).is_error).toBe(true);
  });

  it('reports no failure on a completed card', () => {
    expect(payload(outputCard({})).is_error).toBe(false);
  });

  it('reports no failure when the route published no card at all', () => {
    expect(payload(undefined).is_error).toBe(false);
  });
});

// #463 S4 remediation round 6 — the event branch's fail-closed card drop is
// runtime behavior, not an annotation. Round 5 stopped passing `body.card`
// straight through and substitutes `undefined` for a `terminal_output` card,
// because `FullPayload`'s event arm means an event card specifically and a
// result card arriving there would be a server publishing the wrong family.
// Nothing tested that substitution, so it could have been reverted silently.
describe('#463 S4 — the full-payload event branch drops a foreign card family', () => {
  const eventPayload = (card?: NativeToolCard | NativeTerminalOutput) => {
    const out = adaptQualifiedPayload('cbk1.e', 'event', {
      which: 'event', content: 'raw event\n', truncated: false, card,
    });
    if (out.which !== 'event') throw new Error(`expected an event payload, got ${out.which}`);
    return out;
  };

  const terminalOutputCard: NativeTerminalOutput = {
    schema_version: 1, type: 'terminal_output', status: 'failed',
    is_error: true, truncated: false, exit_code: 1,
    parts: [{ type: 'text', stream: 'output', text: 'boom\n' }],
  };

  const eventCard: NativeToolCard = {
    schema_version: 1, type: 'plan', source: 'update_plan', call_status: 'requested',
    explanation: null, items: [{ step: 'Only step', status: 'completed' }],
  };

  it('drops a terminal_output card rather than publishing it as an event card', () => {
    expect(eventPayload(terminalOutputCard).card).toBeUndefined();
  });

  it('passes a genuine event card through unchanged', () => {
    expect(eventPayload(eventCard).card).toBe(eventCard);
  });

  it('leaves the rest of the event payload intact when the card is dropped', () => {
    const out = eventPayload(terminalOutputCard);
    expect(out.text).toBe('raw event\n');
    expect(out.tool_use_id).toBe('cbk1.e');
    expect(out.full_length).toBe('raw event\n'.length);
  });
});

describe('qualified Claude conversation adapters', () => {
  it('preserves the canonical top-level tool-call contract', () => {
    const input = { query: 'select:TaskCreate,TaskUpdate', max_results: 10 };
    const result = {
      text: '{"matches":["TaskCreate","TaskUpdate"]}',
      truncated: false,
      full_length: 45,
      is_error: false,
    };
    const call = adaptBlocks([{
      kind: 'tool_call',
      name: 'ToolSearch',
      input,
      input_summary: '{"query":"select:TaskCreate,TaskUpdate","max_results":10}',
      input_truncated: false,
      preview: 'select:TaskCreate,TaskUpdate',
      result,
      tool_use_id: 'toolu_fixture',
    } as never], 'claude')[0] as Extract<ConversationBlock, { kind: 'tool_call' }>;

    expect(call).toMatchObject({
      kind: 'tool_call',
      name: 'ToolSearch',
      input,
      input_summary: '{"query":"select:TaskCreate,TaskUpdate","max_results":10}',
      preview: 'select:TaskCreate,TaskUpdate',
      result,
      tool_use_id: 'toolu_fixture',
    });
  });
});
