import type {
  ConversationBlock,
  ConversationDetail,
  ConversationFindResult,
  ConversationItem,
  ConversationOutline,
  ConversationRef,
  ConversationSearchResult,
  ConversationSummary,
  FindAnchor,
  FullPayload,
  OutlineTurn,
  SearchHit,
  TokenUsage,
  NativePatchFile,
  NativeResultEnvelope,
  NativeToolCard,
  CodexLifecycleState,
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
  payload_which?: string;
  output?: { text?: string | null; detail?: Record<string, unknown> | null } | null;
  timestamp_utc?: string | null;
};

const GIT_MARKER_ACTIONS = new Set(['create_branch', 'stage', 'commit', 'push', 'create_pr']);

function nonBlank(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined;
}

function codexReasoning(block: QualifiedBlock): Extract<ConversationBlock, { kind: 'codex_reasoning' }> | null {
  const detail = record(block.detail?.reasoning);
  if (detail?.schema_version === 1) {
    const title = nonBlank(detail.title);
    const summary = nonBlank(detail.summary);
    const body = nonBlank(detail.body);
    if (!title && !summary && !body) return null;
    return {
      kind: 'codex_reasoning', source: nonBlank(detail.source) ?? 'codex',
      title, summary, body,
    };
  }
  const body = nonBlank(block.text);
  return body ? { kind: 'codex_reasoning', source: 'codex', body } : null;
}

function systemActions(value: unknown): Extract<ConversationBlock, { kind: 'system_actions' }>['actions'] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const actions: Extract<ConversationBlock, { kind: 'system_actions' }>['actions'] = [];
  for (const raw of value) {
    const marker = record(raw);
    if (!marker || marker.schema_version !== 1) return null;
    if (marker.type === 'git' && typeof marker.action === 'string' && GIT_MARKER_ACTIONS.has(marker.action)) {
      actions.push({
        type: 'git',
        action: marker.action as Extract<(typeof actions)[number], { type: 'git' }>['action'],
        ...(marker.action === 'create_pr' && typeof marker.draft === 'boolean' ? { draft: marker.draft } : {}),
      });
      continue;
    }
    if (marker.type === 'memory_citation'
        && Number.isSafeInteger(marker.citation_count) && Number(marker.citation_count) >= 0
        && Number.isSafeInteger(marker.rollout_count) && Number(marker.rollout_count) >= 0) {
      actions.push({
        type: 'memory_citation',
        citation_count: Number(marker.citation_count), rollout_count: Number(marker.rollout_count),
      });
      continue;
    }
    return null;
  }
  return actions;
}

function codexLifecycle(block: QualifiedBlock): Extract<ConversationBlock, { kind: 'codex_lifecycle' }> | null {
  const lifecycle = record(block.detail?.lifecycle);
  if (lifecycle?.schema_version !== 1
      || (lifecycle.event !== 'task_started' && lifecycle.event !== 'task_complete')) return null;
  return {
    kind: 'codex_lifecycle', event: lifecycle.event,
    ...(nonBlank(lifecycle.message) ? { message: nonBlank(lifecycle.message) } : {}),
    ...(nonBlank(lifecycle.error) ? { error: nonBlank(lifecycle.error) } : {}),
    ...(typeof lifecycle.duration_ms === 'number' && Number.isFinite(lifecycle.duration_ms)
      ? { duration_ms: lifecycle.duration_ms } : {}),
    ...(block.block_key ? { payload_key: block.block_key } : {}),
  };
}

function record(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function patchFiles(value: unknown): NativePatchFile[] | null {
  if (!Array.isArray(value)) return null;
  const files: NativePatchFile[] = [];
  for (const raw of value) {
    const entry = record(raw);
    if (!entry) return null;
    const file: NativePatchFile = {};
    for (const key of ['path', 'move_path', 'status', 'unified_diff', 'raw', 'raw_extra'] as const) {
      if (typeof entry[key] === 'string') file[key] = entry[key] as string;
    }
    files.push(file);
  }
  return files;
}

function terminalOutputCard(value: unknown): Extract<NativeToolCard, { type: 'terminal' }>['output'] | undefined {
  const card = record(value);
  if (card?.schema_version !== 1 || card.type !== 'terminal_output' || !Array.isArray(card.parts)) return undefined;
  const parts = card.parts.map((raw) => {
    const part = record(raw);
    if (!part || (part.type !== 'text' && part.type !== 'raw') || typeof part.text !== 'string') return null;
    const stream = part.stream === 'stdout' || part.stream === 'stderr' ? part.stream : 'output';
    return { type: part.type, stream, text: part.text };
  });
  if (parts.some((part) => part == null)) return undefined;
  return {
    schema_version: 1, type: 'terminal_output',
    status: typeof card.status === 'string' ? card.status : 'unknown',
    is_error: card.is_error === true,
    parts: parts as NonNullable<Extract<NativeToolCard, { type: 'terminal' }>['output']>['parts'],
    truncated: card.truncated === true,
  };
}

function nativeResult(value: unknown): NativeResultEnvelope | null {
  const result = record(value);
  if (!result || typeof result.status !== 'string' || typeof result.truncated !== 'boolean' || !('value' in result)) return null;
  return { status: result.status, value: result.value, truncated: result.truncated };
}

function nativePlanCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'plan' }> | undefined {
  if (card.source !== 'update_plan' || typeof card.call_status !== 'string' || !Array.isArray(card.items)) return undefined;
  const items = card.items.map((raw) => {
    const item = record(raw);
    return item && typeof item.step === 'string' && typeof item.status === 'string'
      ? { step: item.step, status: item.status }
      : null;
  });
  const result = card.result === undefined ? undefined : nativeResult(card.result);
  if (items.some((item) => item == null) || (card.result !== undefined && !result)) return undefined;
  return {
    schema_version: 1, type: 'plan', source: 'update_plan', call_status: card.call_status,
    explanation: typeof card.explanation === 'string' ? card.explanation : null,
    items: items as { step: string; status: string }[],
    ...(result ? { result } : {}),
  };
}

function nativeWebSearchCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'web_search' }> | undefined {
  const completion = record(card.completion);
  if (card.source !== 'web_search_call' || typeof card.call_status !== 'string' || typeof card.query !== 'string'
      || !record(card.action) || !completion || typeof completion.status !== 'string'
      || typeof completion.query !== 'string' || !record(completion.action) || !Array.isArray(completion.results)) return undefined;
  const results = completion.results.map((raw) => {
    const result = record(raw);
    if (!result || typeof result.title !== 'string' || typeof result.url !== 'string') return null;
    return {
      title: result.title, url: result.url,
      ...(typeof result.domain === 'string' ? { domain: result.domain } : {}),
      ...(typeof result.snippet === 'string' ? { snippet: result.snippet } : {}),
      ...(typeof result.ref_id === 'string' ? { ref_id: result.ref_id } : {}),
      ...(typeof result.type === 'string' ? { type: result.type } : {}),
    };
  });
  if (results.some((result) => result == null)) return undefined;
  return {
    schema_version: 1, type: 'web_search', source: 'web_search_call', call_status: card.call_status,
    query: card.query, action: record(card.action)!,
    completion: {
      status: completion.status, query: completion.query, action: record(completion.action)!,
      results: results as Extract<NativeToolCard, { type: 'web_search' }>['completion']['results'],
      ...(typeof completion.error === 'string' ? { error: completion.error } : {}),
      ...(typeof completion.event_block_key === 'string' ? { event_block_key: completion.event_block_key } : {}),
    },
  };
}

function nativeMcpCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'mcp' }> | undefined {
  const completion = record(card.completion);
  const duration = record(completion?.duration);
  if (card.source !== 'function_call' || typeof card.name !== 'string' || typeof card.call_status !== 'string'
      || !completion || typeof completion.status !== 'string' || typeof completion.server !== 'string'
      || typeof completion.tool !== 'string' || !record(completion.arguments) || !record(completion.result)
      || !duration || typeof duration.secs !== 'number' || !Number.isFinite(duration.secs)
      || typeof duration.nanos !== 'number' || !Number.isFinite(duration.nanos)) return undefined;
  return {
    schema_version: 1, type: 'mcp', source: 'function_call', name: card.name, call_status: card.call_status,
    completion: {
      status: completion.status, server: completion.server, tool: completion.tool,
      arguments: record(completion.arguments)!, result: record(completion.result)!,
      duration: { secs: duration.secs, nanos: duration.nanos },
      ...(typeof completion.event_block_key === 'string' ? { event_block_key: completion.event_block_key } : {}),
    },
  };
}

const AGENT_OPERATIONS = new Set(['spawn_agent', 'wait_agent', 'send_message', 'list_agents', 'followup_task', 'interrupt_agent']);

function nativeAgentCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'agent' }> | undefined {
  const operation = typeof card.operation === 'string' && AGENT_OPERATIONS.has(card.operation) ? card.operation : null;
  const args = record(card.arguments);
  const result = card.result === undefined ? undefined : nativeResult(card.result);
  const child = card.child_conversation == null ? null : record(card.child_conversation);
  if (!operation || typeof card.call_status !== 'string' || !args
      || (card.result !== undefined && !result) || (card.child_conversation != null && !child)) return undefined;
  if (child && typeof child.conversation_key !== 'string') return undefined;
  return {
    schema_version: 1, type: 'agent', operation: operation as Extract<NativeToolCard, { type: 'agent' }>['operation'],
    call_status: card.call_status, arguments: args,
    ...(result ? { result } : {}),
    ...(child ? { child_conversation: {
      conversation_key: child.conversation_key as string,
      ...(typeof child.role === 'string' ? { role: child.role } : {}),
      ...(typeof child.nickname === 'string' ? { nickname: child.nickname } : {}),
    } } : {}),
  };
}

function nativeToolCard(block: QualifiedBlock): NativeToolCard | undefined {
  const card = record(block.detail?.card);
  if (card?.schema_version !== 1) return undefined;
  if (card.type === 'terminal') {
    if (!Array.isArray(card.commands) || card.commands.length === 0) return undefined;
    const commands = card.commands.map((raw) => {
      const command = record(raw);
      if (!command || typeof command.command !== 'string') return null;
      return {
        command: command.command,
        workdir: typeof command.workdir === 'string' ? command.workdir : null,
        metadata: record(command.metadata) ?? {},
      };
    });
    if (commands.some((command) => command == null)) return undefined;
    return {
      schema_version: 1, type: 'terminal',
      status: typeof card.status === 'string' ? card.status : 'unknown',
      commands: commands as Extract<NativeToolCard, { type: 'terminal' }>['commands'],
      output: terminalOutputCard(block.output?.detail?.card),
      truncated: card.truncated === true || block.output?.detail?.card != null && record(block.output.detail.card)?.truncated === true,
    };
  }
  if (card.type === 'plan') return nativePlanCard(card);
  if (card.type === 'web_search') return nativeWebSearchCard(card);
  if (card.type === 'mcp') return nativeMcpCard(card);
  if (card.type === 'agent') return nativeAgentCard(card);
  if (card.type !== 'patch') return undefined;
  const requestFiles = patchFiles(card.files);
  if (requestFiles == null) return undefined;
  const completion = record(card.completion);
  const display = completion?.schema_version === 1 && completion.type === 'patch' ? completion : card;
  const files = patchFiles(display.files);
  if (files == null) return undefined;
  return {
    schema_version: 1, type: 'patch',
    source: typeof card.source === 'string' ? card.source : 'patch_apply_end',
    status: typeof display.status === 'string' ? display.status : 'unknown',
    files,
    request_files: completion ? requestFiles : undefined,
    patch: typeof card.patch === 'string' ? card.patch : undefined,
    success: typeof display.success === 'boolean' ? display.success : null,
    stdout: typeof display.stdout === 'string' ? display.stdout : null,
    stderr: typeof display.stderr === 'string' ? display.stderr : null,
    has_diff: typeof display.has_diff === 'boolean'
      ? display.has_diff
      : files.some((file) => typeof file.unified_diff === 'string'),
    truncated: card.truncated === true || display.truncated === true,
    event_payload_key: typeof display.event_block_key === 'string' ? display.event_block_key : undefined,
  };
}

type QualifiedMetaKind = 'skill' | 'command' | 'context' | 'compaction' | 'notification';

function cleanQualifiedTitle(title: string | null | undefined): string | undefined {
  if (!title) return undefined;
  // Codex skill invocations are real prompts, but their native Markdown link
  // leaks a private filesystem path into every title surface. Preserve the
  // skill identity and prompt text; remove only that leading SKILL.md target.
  return title.replace(
    /^\[((?:\$)[^\]\r\n]+)\]\([^\)\r\n]*\/SKILL\.md\)(?=\s|$)/,
    (_match, label: string) => label,
  );
}

function adaptBlocks(blocks: QualifiedBlock[], source: ConversationSource): ConversationBlock[] {
  const out: ConversationBlock[] = [];
  for (const block of blocks) {
    if (block.kind === 'assistant' || block.kind === 'user' || block.kind === 'text') {
      if (block.text) out.push({ kind: 'text', text: block.text });
      const actions = systemActions(block.detail?.markers);
      if (actions) out.push({
        kind: 'system_actions', actions,
        ...(block.block_key ? { payload_key: block.block_key } : {}),
      });
      continue;
    }
    if (block.kind === 'thinking') {
      if (block.text?.trim()) out.push({ kind: 'thinking', text: block.text });
      continue;
    }
    if (block.kind === 'reasoning') {
      if (source === 'codex') {
        const reasoning = codexReasoning(block);
        if (reasoning) out.push(reasoning);
      } else if (block.text?.trim()) {
        out.push({ kind: 'thinking', text: block.text });
      }
      continue;
    }
    if (block.kind === 'tool_call') {
      const name = typeof block.detail?.name === 'string' ? block.detail.name : null;
      const args = typeof block.detail?.args === 'string' ? block.detail.args : '';
      const nativeCard = nativeToolCard(block);
      const terminal = nativeCard?.type === 'terminal' ? nativeCard : null;
      const patch = nativeCard?.type === 'patch' ? nativeCard : null;
      const plan = nativeCard?.type === 'plan' ? nativeCard : null;
      const web = nativeCard?.type === 'web_search' ? nativeCard : null;
      const mcp = nativeCard?.type === 'mcp' ? nativeCard : null;
      const agent = nativeCard?.type === 'agent' ? nativeCard : null;
      const input = terminal
        ? { command: terminal.commands[0].command, workdir: terminal.commands[0].workdir }
        : web ? { query: web.query, action: web.action }
        : mcp ? mcp.completion.arguments
        : agent ? agent.arguments
        : parseArgs(args);
      const outputError = terminal?.output?.is_error === true || patch?.success === false
        || web?.completion.status === 'error' || mcp?.completion.status === 'error';
      const semanticPreview = plan?.explanation
        ?? web?.query
        ?? (mcp ? `${mcp.completion.tool} · ${mcp.completion.server}` : null)
        ?? (agent ? agent.operation : null);
      const semanticResult = plan?.result?.value
        ?? web?.completion.error
        ?? mcp?.completion.result
        ?? agent?.result?.value
        ?? null;
      const semanticResultText = typeof semanticResult === 'string'
        ? semanticResult
        : semanticResult == null ? null : JSON.stringify(semanticResult, null, 2);
      out.push({
        kind: 'tool_call',
        name,
        input_summary: terminal ? JSON.stringify({ commands: terminal.commands }) : args || block.text || '',
        input,
        preview: terminal?.commands[0].command
          ?? patch?.files.map((file) => file.move_path ?? file.path).filter(Boolean).join(', ')
          ?? semanticPreview
          ?? (block.text ?? args).split('\n')[0] ?? '',
        tool_use_id: block.block_key ?? null,
        payload_capable: block.block_key != null,
        payload_kind: 'call',
        native_card: nativeCard,
        web_search: web ? { query: web.query, links: web.completion.results } : undefined,
        result: block.output || semanticResultText != null ? {
          text: semanticResultText ?? block.output?.text ?? '',
          truncated: terminal?.output?.truncated === true || patch?.truncated === true || plan?.result?.truncated === true || agent?.result?.truncated === true,
          is_error: outputError,
        } : null,
      });
      continue;
    }
    if (block.kind === 'event') {
      const nativeCard = nativeToolCard(block);
      if (nativeCard?.type === 'patch') {
        const text = [nativeCard.stdout, nativeCard.stderr].filter((part): part is string => typeof part === 'string').join('');
        out.push({
          kind: 'tool_call', name: 'patch_apply_end', input_summary: block.text ?? '', input: null,
          preview: nativeCard.files.map((file) => file.move_path ?? file.path).filter(Boolean).join(', ') || 'patch summary',
          tool_use_id: block.block_key ?? null, payload_capable: block.block_key != null, payload_kind: 'event',
          native_card: nativeCard,
          result: { text, truncated: nativeCard.truncated === true, is_error: nativeCard.success === false || nativeCard.status === 'failed' },
        });
        continue;
      }
      const lifecycle = codexLifecycle(block);
      if (lifecycle) {
        out.push(lifecycle);
        continue;
      }
    }
    if (block.kind === 'tool_output' || block.kind === 'tool_result') {
      out.push({
        kind: 'tool_result',
        text: block.text ?? '',
        truncated: false,
        is_error: block.detail?.is_error === true,
        tool_use_id: block.call_id ?? block.block_key ?? null,
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
  meta_kind?: string | null;
  meta_label?: string | null;
  meta_sections?: string[] | null;
  skill_name?: string | null;
  lifecycle?: unknown;
};

function eventLabel(item: QualifiedItem): string | null {
  const event = item.blocks.find((block) => block.kind === 'event');
  const lifecycle = record(event?.detail?.lifecycle);
  if (lifecycle?.event === 'task_started' || lifecycle?.event === 'task_complete') {
    return `codex_${lifecycle.event}`;
  }
  if (typeof event?.detail?.event === 'string') return event.detail.event;
  const firstLine = event?.text?.split('\n')[0]?.trim();
  return firstLine || null;
}

function foldedLifecycle(value: unknown): CodexLifecycleState | undefined {
  const lifecycle = record(value);
  if (lifecycle?.schema_version !== 1 || typeof lifecycle.state !== 'string' || !Array.isArray(lifecycle.events)) return undefined;
  const events = lifecycle.events.map((raw) => {
    const event = record(raw);
    return event && typeof event.event === 'string' && event.payload_which === 'event' && typeof event.block_key === 'string'
      ? { event: event.event, payload_which: 'event' as const, block_key: event.block_key }
      : null;
  });
  if (events.some((event) => event == null)) return undefined;
  return {
    schema_version: 1, state: lifecycle.state,
    ...(record(lifecycle.started) ? { started: record(lifecycle.started)! } : {}),
    ...(record(lifecycle.completed) ? { completed: record(lifecycle.completed)! } : {}),
    events: events as CodexLifecycleState['events'],
  };
}

function qualifiedMeta(item: QualifiedItem): {
  meta_kind: QualifiedMetaKind;
  meta_label: string | null;
  meta_sections: string[] | undefined;
  skill_name: string | null;
} {
  const blockMeta = item.blocks.find((block) => block.kind === 'meta')?.detail;
  const rawKind = item.meta_kind ?? blockMeta?.meta_kind;
  const rawLabel = item.meta_label ?? blockMeta?.meta_label;
  const label = typeof rawLabel === 'string' ? rawLabel : eventLabel(item);
  const kind: QualifiedMetaKind =
    rawKind === 'skill' || rawKind === 'command' || rawKind === 'context'
      || rawKind === 'compaction' || rawKind === 'notification'
      ? rawKind
      : label === 'context_compacted' ? 'compaction'
        : item.kind === 'event' ? 'notification' : 'context';
  return {
    meta_kind: kind,
    meta_label: label,
    meta_sections: Array.isArray(item.meta_sections)
      ? item.meta_sections.filter((section): section is string => typeof section === 'string')
      : undefined,
    skill_name: typeof item.skill_name === 'string' ? item.skill_name : null,
  };
}

function adaptItem(ref: ConversationRef, item: QualifiedItem): ConversationItem {
  const anchor = { session_id: ref.key, uuid: item.item_key, id: stableItemId(item.item_key) };
  const blocks = adaptBlocks(item.blocks, ref.source);
  const text = item.blocks
    .filter((b) => b.kind === 'user' || b.kind === 'assistant' || b.kind === 'text')
    .map((b) => b.text ?? '').filter(Boolean).join('\n\n');
  const common = {
    anchor, member_uuids: [item.item_key], ts: item.timestamp_utc ?? '',
    text, blocks, is_sidechain: false, subagent_key: null, parent_uuid: null,
  };
  const assistantLike = item.kind === 'assistant' || item.kind === 'reasoning' || item.kind === 'tool_call'
    || blocks.some((block) => block.kind === 'tool_call');
  if (assistantLike) {
    const lifecycle = foldedLifecycle(item.lifecycle);
    return {
      ...common, kind: 'assistant' as const, model: item.model,
      cost_usd: num(item.cost_usd), tokens: adaptQualifiedTokens(item.tokens),
      ...(lifecycle ? { lifecycle } : {}),
    };
  }
  if (item.kind === 'user' || item.kind === 'human') return { ...common, kind: 'human' as const };
  if (item.kind === 'tool_output' || item.kind === 'tool_result') {
    return { ...common, kind: 'tool_result' as const, text: '' };
  }
  const meta = qualifiedMeta(item);
  return {
    ...common, kind: 'meta' as const, ...meta,
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
    title: cleanQualifiedTitle(body.title),
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
      title: cleanQualifiedTitle(row.title) || 'Untitled conversation',
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
    title: cleanQualifiedTitle(hit.title) ?? 'Untitled conversation',
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
  turns?: {
    item_key: string;
    label: string;
    timestamp_utc: string | null;
    kinds: Record<string, number>;
    meta_kind?: string | null;
    meta_label?: string | null;
    meta_sections?: string[] | null;
    skill_name?: string | null;
  }[];
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
  const turns: OutlineTurn[] = (body.turns ?? []).flatMap((turn) => {
    const isEvent = (turn.kinds.event ?? 0) > 0;
    const isPrompt = promptItemKeys?.has(turn.item_key) ?? false;
    const isCompaction = isEvent && turn.label.includes('context_compacted');
    // The canonical outline is navigation for the conversation, not a second
    // event log. Keep prompts, logical assistant responses and compactions;
    // detail view still preserves every lifecycle/tool/meta row.
    if (((turn.kinds.meta ?? 0) > 0) || (isEvent && !isCompaction)) return [];
    const isHuman = isPrompt || (turn.kinds.user ?? 0) > 0;
    // Current qualified rows carry the semantic assistant kind. Older Claude
    // projections expose text-only turns, so after prompt/meta/event exclusion
    // the remaining prose row is still the logical assistant response.
    const isAssistant = (turn.kinds.assistant ?? 0) > 0
      || (!isHuman && !isEvent && (turn.kinds.meta ?? 0) === 0);
    if (!isHuman && !isAssistant && !isCompaction) return [];
    return [{
      uuid: turn.item_key,
      kind: isCompaction ? 'meta' : isHuman ? 'human' : 'assistant',
      ts: turn.timestamp_utc,
      label: cleanQualifiedTitle(turn.label) ?? turn.label,
      member_uuids: [turn.item_key], subagent_key: null, parent_uuid: null, is_sidechain: false,
      ...(isCompaction ? { meta_kind: 'compaction' as const } : {}),
    }];
  });
  const tokenTotals = adaptQualifiedTokens(totals.tokens ?? body.stats?.tokens) ?? {
    source: ref.source, input: 0, output: 0, cache_creation: 0, cache_read: 0,
    ...(ref.source === 'codex' ? { cached_input: 0, reasoning_output: 0 } : {}),
  };
  const human = turns.filter((turn) => turn.kind === 'human').length;
  const assistant = turns.filter((turn) => turn.kind === 'assistant').length;
  const toolResult = turns.filter((turn) => turn.kind === 'tool_result').length;
  const meta = turns.filter((turn) => turn.kind === 'meta').length;
  return {
    session_id: ref.key,
    stats: {
      turns: { total: turns.length, human, assistant, tool_result: toolResult, meta },
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
  requested: 'input' | 'result' | 'event',
  body: { which?: 'call' | 'output' | 'event'; content?: string; truncated?: boolean; card?: NativeToolCard },
): FullPayload {
  if (requested === 'input') {
    const parsed = parseArgs((body.content ?? '').split('\n').slice(1).join('\n')) ?? { raw: body.content ?? '' };
    return { which: 'input', tool_use_id: blockKey, input: parsed, full_length: (body.content ?? '').length, truncated: body.truncated === true };
  }
  if (requested === 'event') {
    return {
      which: 'event', tool_use_id: blockKey, text: body.content ?? '',
      full_length: (body.content ?? '').length, truncated: body.truncated === true,
      card: body.card,
    };
  }
  return {
    which: 'result', tool_use_id: blockKey, text: body.content ?? '',
    full_length: (body.content ?? '').length, truncated: body.truncated === true, is_error: false,
  };
}
