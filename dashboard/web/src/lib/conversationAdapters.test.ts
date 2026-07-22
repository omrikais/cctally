import { describe, expect, it } from 'vitest';
import {
  adaptQualifiedBrowse,
  adaptQualifiedDetail,
  adaptQualifiedFind,
  adaptQualifiedOutline,
  adaptQualifiedPrompts,
  adaptQualifiedSearch,
} from './conversationAdapters';

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
      { kind: 'text', text: 'Synthetic closeout prose remains visible.' },
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
    expect(adaptQualifiedFind({
      status: 'ok', conversation_key: ref.key, total: 1,
      anchors: [{ item_key: 'civ1.item', match_kinds: ['tool'] }],
      anchors_truncated: false, search_depth: 'full', kind: 'all', mode: 'fts',
    }).anchors).toEqual([{ uuid: 'civ1.item', match_kinds: ['tool'] }]);
    expect(adaptQualifiedPrompts({
      status: 'ok', conversation_key: ref.key,
      prompts: [{ item_key: 'civ1.prompt', text: 'Prompt' }],
    })).toEqual({ prompts: [{ uuid: 'civ1.prompt', text: 'Prompt' }] });
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
});
