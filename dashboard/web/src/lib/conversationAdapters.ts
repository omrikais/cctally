import type {
  ConversationBlock,
  ConversationDetail,
  ConversationFindResult,
  ConversationOutline,
  ConversationRef,
  ConversationSearchResult,
  ConversationSummary,
  FindAnchor,
  FullPayload,
  OutlineTurn,
  SearchHit,
  TokenUsage,
} from '../types/conversation';
import type { ConversationSource } from '../types/conversation';
import type { QualifiedBrowseEnvelope, QualifiedSearchEnvelope } from './conversationTransport';

// The S7 envelopes deliberately differ from the long-lived Claude UI model.
// These adapters are the one data/render boundary: shared reader components see
// their established model while qualified identity and provider-native meaning
// stay intact. No caller decodes the opaque v1 key.

export class ConversationNormalizationPending extends Error {
  constructor() { super('Conversation indexing is still finishing.'); }
}

export type NativeTokens = {
  source?: 'codex' | 'claude';
  input?: number;
  output?: number;
  cached_input?: number;
  reasoning_output?: number;
  cache_creation?: number;
  cache_create?: number;
  cache_read?: number;
} | null | undefined;

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function adaptQualifiedTokens(tokens: NativeTokens): TokenUsage | undefined {
  if (!tokens) return undefined;
  if (tokens.source === 'codex' || 'cached_input' in tokens || 'reasoning_output' in tokens) {
    return {
      source: 'codex',
      input: num(tokens.input),
      output: num(tokens.output),
      cache_creation: 0,
      cache_read: 0,
      cached_input: num(tokens.cached_input),
      reasoning_output: num(tokens.reasoning_output),
    };
  }
  return {
    source: 'claude',
    input: num(tokens.input), output: num(tokens.output),
    cache_creation: num(tokens.cache_creation ?? tokens.cache_create), cache_read: num(tokens.cache_read),
  };
}

function stableItemId(key: string): number {
  // Positive, deterministic and collision-resistant enough for the reader's
  // local keyed-window bookkeeping. Network cursors always remain item_key.
  let hash = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0) || 1;
}

function parseArgs(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string' || value.trim() === '') return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : { value: parsed };
  } catch {
    return { raw: value };
  }
}

type QualifiedBlock = {
  kind: string;
  text?: string | null;
  detail?: Record<string, unknown> | null;
  call_id?: string | null;
  block_key?: string;
  output?: { text?: string | null; detail?: Record<string, unknown> | null } | null;
  timestamp_utc?: string | null;
};

function adaptBlocks(blocks: QualifiedBlock[]): ConversationBlock[] {
  const out: ConversationBlock[] = [];
  for (const block of blocks) {
    if (block.kind === 'assistant' || block.kind === 'user' || block.kind === 'text') {
      if (block.text) out.push({ kind: 'text', text: block.text });
      continue;
    }
    if (block.kind === 'reasoning') {
      out.push({ kind: 'thinking', text: block.text ?? '' });
      continue;
    }
    if (block.kind === 'tool_call') {
      const name = typeof block.detail?.name === 'string' ? block.detail.name : null;
      const args = typeof block.detail?.args === 'string' ? block.detail.args : '';
      out.push({
        kind: 'tool_call',
        name,
        input_summary: args || block.text || '',
        input: parseArgs(args),
        preview: (block.text ?? args).split('\n')[0] ?? '',
        tool_use_id: block.block_key ?? null,
        payload_capable: block.block_key != null,
        result: block.output ? {
          text: block.output.text ?? '',
          truncated: false,
          is_error: false,
        } : null,
      });
      continue;
    }
    // Lifecycle, patch, MCP and web events stay explicit, searchable prose.
    if (block.text) out.push({ kind: 'text', text: block.text });
  }
  return out;
}

type QualifiedItem = {
  item_key: string;
  kind: string;
  timestamp_utc: string | null;
  model: string | null;
  blocks: QualifiedBlock[];
  cost_usd: number | null;
  tokens: NativeTokens;
};

function metaKind(item: QualifiedItem): 'compaction' | 'notification' {
  const event = item.blocks.find((b) => b.kind === 'event');
  return event?.text?.includes('context_compacted') ? 'compaction' : 'notification';
}

function adaptItem(ref: ConversationRef, item: QualifiedItem) {
  const anchor = { session_id: ref.key, uuid: item.item_key, id: stableItemId(item.item_key) };
  const blocks = adaptBlocks(item.blocks);
  const text = item.blocks
    .filter((b) => b.kind === 'user' || b.kind === 'assistant' || b.kind === 'text')
    .map((b) => b.text ?? '').filter(Boolean).join('\n\n');
  const common = {
    anchor, member_uuids: [item.item_key], ts: item.timestamp_utc ?? '',
    text, blocks, is_sidechain: false, subagent_key: null, parent_uuid: null,
  };
  if (item.kind === 'assistant') {
    return {
      ...common, kind: 'assistant' as const, model: item.model,
      cost_usd: num(item.cost_usd), tokens: adaptQualifiedTokens(item.tokens),
    };
  }
  if (item.kind === 'user' || item.kind === 'human') return { ...common, kind: 'human' as const };
  return {
    ...common, kind: 'meta' as const, meta_kind: metaKind(item), skill_name: null,
    text: item.blocks.map((b) => b.text ?? '').filter(Boolean).join('\n\n'),
  };
}

type QualifiedDetailEnvelope = {
  status: 'ok' | 'normalization_pending' | 'not_found';
  conversation_key: string;
  title?: string | null;
  items?: QualifiedItem[];
  page?: { total: number; returned: number; before: string | null; after: string | null; has_before: boolean; has_after: boolean };
  children?: { conversation_key: string; title: string | null; cost_usd: number }[];
  parent?: { conversation_key: string; title: string | null } | null;
  total_cost_usd?: number;
  unattributed_cost_usd?: number;
  tokens?: NativeTokens;
};

export function adaptQualifiedDetail(ref: ConversationRef, body: QualifiedDetailEnvelope): ConversationDetail {
  if (body.status === 'normalization_pending') throw new ConversationNormalizationPending();
  if (body.status !== 'ok') throw new Error('Conversation not found.');
  const items = (body.items ?? []).map((item) => adaptItem(ref, item));
  const models = Array.from(new Set((body.items ?? []).map((item) => item.model).filter((m): m is string => !!m)));
  const firstTs = (body.items ?? []).find((item) => item.timestamp_utc)?.timestamp_utc ?? '';
  const lastTs = [...(body.items ?? [])].reverse().find((item) => item.timestamp_utc)?.timestamp_utc ?? firstTs;
  const page = body.page;
  return {
    session_id: ref.key,
    title: body.title ?? undefined,
    // Qualified detail intentionally does not repeat collection-only project
    // metadata. An em dash is truthful; the provider strip carries source.
    project_label: '—',
    git_branch: null,
    started_utc: firstTs,
    last_activity_utc: lastTs,
    cost_usd: num(body.total_cost_usd),
    models,
    items,
    page: {
      next_after: page?.has_after ? page.after : null,
      has_more: page?.has_after ?? false,
      prev_before: page?.has_before ? page.before : null,
      has_prev: page?.has_before ?? false,
    },
    last_anchor: items.length ? items[items.length - 1].anchor : null,
    provider_meta: {
      source: ref.source,
      conversation_key: body.conversation_key,
      tokens: adaptQualifiedTokens(body.tokens),
      unattributed_cost_usd: num(body.unattributed_cost_usd),
      parent: body.parent ?? null,
      children: body.children ?? [],
    },
  };
}

export function adaptQualifiedBrowse(source: ConversationSource, body: QualifiedBrowseEnvelope): {
  rows: ConversationSummary[]; cursor: string | null; total: number; pending: boolean;
} {
  if (body.status === 'normalization_pending') return { rows: [], cursor: null, total: 0, pending: true };
  return {
    rows: body.rows.map((row) => ({
      conversation_ref: { source, key: row.conversation_key },
      session_id: row.conversation_key,
      title: row.title || 'Untitled conversation',
      project_label: row.project_label || '—',
      git_branch: row.parent ? 'child thread' : row.is_fork ? 'fork' : null,
      started_utc: row.started_utc || row.last_activity_utc || '',
      last_activity_utc: row.last_activity_utc || row.started_utc || '',
      msg_count: row.count,
      cost_usd: row.cost_usd,
      models: row.models,
    })),
    cursor: body.page.cursor ?? null,
    total: body.page.total,
    pending: false,
  };
}

export function adaptQualifiedSearch(source: ConversationSource, body: QualifiedSearchEnvelope): ConversationSearchResult & { cursor: string | null; pending: boolean } {
  if (body.status === 'normalization_pending') {
    return { query: body.query, mode: body.mode, hits: [], total: 0, search_depth: body.depth, cursor: null, pending: true };
  }
  const hits: SearchHit[] = body.hits.map((hit) => ({
    conversation_ref: { source, key: hit.conversation_key },
    session_id: hit.conversation_key,
    uuid: hit.item_key ?? '',
    project_label: hit.project_label ?? '—',
    title: hit.title ?? 'Untitled conversation',
    ts: hit.last_activity_utc ?? '',
    snippet: hit.snippet,
    cost_usd: 0,
    match_kinds: hit.badges.filter((badge): badge is NonNullable<SearchHit['match_kinds']>[number] =>
      badge === 'tool' || badge === 'thinking' || badge === 'title' || badge === 'file'),
  }));
  return {
    query: body.query, mode: body.mode, hits, total: body.total,
    search_depth: body.depth, cursor: body.page?.cursor ?? null, pending: false,
  };
}

type QualifiedOutlineEnvelope = {
  status: 'ok' | 'normalization_pending' | 'not_found';
  conversation_key: string;
  turns?: { item_key: string; label: string; timestamp_utc: string | null; kinds: Record<string, number> }[];
  stats?: {
    items?: number;
    kinds?: Record<string, number>;
    turns?: { total: number; human: number; assistant: number; tool_result: number; meta: number };
    tool_counts?: Record<string, number>;
    error_count?: number;
    models?: Record<string, number>;
    duration_seconds?: number | null;
    tokens?: NativeTokens;
    cost_usd?: number;
    cache_saved_usd?: number;
  };
  files?: { file_path: string; tool: string; count: number }[];
  children?: { conversation_key: string; title: string | null; cost_usd: number }[];
};

export function adaptQualifiedOutline(
  ref: ConversationRef,
  body: QualifiedOutlineEnvelope,
  totals: { total_cost_usd?: number; tokens?: NativeTokens } = {},
  promptItemKeys?: ReadonlySet<string>,
): ConversationOutline {
  if (body.status === 'normalization_pending') throw new ConversationNormalizationPending();
  if (body.status !== 'ok') throw new Error('Conversation not found.');
  const turns: OutlineTurn[] = (body.turns ?? []).map((turn) => {
    const isEvent = (turn.kinds.event ?? 0) > 0;
    const isPrompt = promptItemKeys?.has(turn.item_key) ?? false;
    return {
      uuid: turn.item_key,
      kind: isEvent ? 'meta' : isPrompt || (turn.kinds.user ?? 0) > 0 ? 'human' : 'assistant',
      ts: turn.timestamp_utc,
      label: turn.label,
      member_uuids: [turn.item_key], subagent_key: null, parent_uuid: null, is_sidechain: false,
      ...(isEvent ? { meta_kind: turn.label.includes('context_compacted') ? 'compaction' as const : 'notification' as const } : {}),
    };
  });
  const tokenTotals = adaptQualifiedTokens(totals.tokens ?? body.stats?.tokens) ?? {
    source: ref.source, input: 0, output: 0, cache_creation: 0, cache_read: 0,
    ...(ref.source === 'codex' ? { cached_input: 0, reasoning_output: 0 } : {}),
  };
  const human = turns.filter((turn) => turn.kind === 'human').length;
  const assistant = turns.filter((turn) => turn.kind === 'assistant').length;
  const meta = turns.filter((turn) => turn.kind === 'meta').length;
  const legacyStats = body.stats?.turns;
  return {
    session_id: ref.key,
    stats: {
      turns: legacyStats ?? { total: turns.length, human, assistant, tool_result: 0, meta },
      tool_counts: body.stats?.tool_counts ?? {},
      error_count: num(body.stats?.error_count),
      models: body.stats?.models ?? {},
      duration_seconds: body.stats?.duration_seconds ?? null,
      tokens: tokenTotals,
      cost_usd: num(totals.total_cost_usd ?? body.stats?.cost_usd),
      cache_saved_usd: num(body.stats?.cache_saved_usd),
    },
    files: [],
    provider_files: (body.files ?? []).map((file) => ({
      path: file.file_path, tool: file.tool, count: file.count,
    })),
    turns,
  };
}

export function adaptQualifiedFind(body: {
  status: string; conversation_key?: string; anchors?: { item_key: string; match_kinds: string[] }[];
  total?: number; anchors_truncated?: boolean; mode?: 'fts' | 'like' | 'regex'; search_depth?: 'prose-only' | 'full'; kind?: string;
}): ConversationFindResult {
  const anchors: FindAnchor[] = (body.anchors ?? []).map((anchor) => ({
    uuid: anchor.item_key,
    match_kinds: anchor.match_kinds.filter((kind): kind is 'tool' | 'thinking' => kind === 'tool' || kind === 'thinking'),
  }));
  return {
    anchors, total: body.total ?? 0, anchors_truncated: body.anchors_truncated ?? false,
    mode: body.mode ?? 'like', search_depth: body.search_depth ?? 'full',
  };
}

export function adaptQualifiedPrompts(body: { status?: string; conversation_key?: string; prompts?: { item_key: string; text: string }[] }): { prompts: { uuid: string; text: string }[] } {
  return { prompts: (body.prompts ?? []).map((prompt) => ({ uuid: prompt.item_key, text: prompt.text })) };
}

export function adaptQualifiedPayload(
  blockKey: string,
  requested: 'input' | 'result',
  body: { which?: 'call' | 'output'; content?: string; truncated?: boolean },
): FullPayload {
  if (requested === 'input') {
    const parsed = parseArgs((body.content ?? '').split('\n').slice(1).join('\n')) ?? { raw: body.content ?? '' };
    return { which: 'input', tool_use_id: blockKey, input: parsed, full_length: (body.content ?? '').length, truncated: body.truncated === true };
  }
  return {
    which: 'result', tool_use_id: blockKey, text: body.content ?? '',
    full_length: (body.content ?? '').length, truncated: body.truncated === true, is_error: false,
  };
}
