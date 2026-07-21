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
    expect(item.blocks.map((block) => block.kind)).toEqual(['text', 'thinking', 'tool_call']);
    expect(item.kind === 'assistant' && 'tokens' in item ? item.tokens : undefined).toEqual({
      source: 'codex', input: 1200, output: 400, cache_creation: 0, cache_read: 0,
      cached_input: 300, reasoning_output: 100,
    });
    expect(detail.provider_meta).toMatchObject({ source: 'codex', unattributed_cost_usd: 0 });
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
