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
  OutlineFile,
  OutlineFileTouch,
  OutlineLandmark,
  OutlineTurn,
  QualifiedOutlineFile,
  SearchHit,
  TokenUsage,
  ConversationSessionIndex,
  NativePatchFile,
  NativePatchRequestFile,
  NativeProgramInvocation,
  NativeResultEnvelope,
  NativeTerminalOutput,
  NativeToolCard,
  ToolOutcome,
  CodexLifecycleState,
} from '../types/conversation';
import type { ConversationSource } from '../types/conversation';
import type { QualifiedBrowseEnvelope, QualifiedSearchEnvelope } from './conversationTransport';
// #463 S3 §5.4 — one vocabulary for naming a session reference, shared with the
// card components so the collapsed row and the card body cannot drift apart.
import {
  sessionCharsPreview, sessionOperationLabel, sessionReferenceLabel,
} from '../conversations/sessionIndex';
// #463 S4 §1.3 — one definition of "this turn owns a landmark", shared with the
// rendered outline so the retention rule and the rail cannot disagree.
import { landmarkOwners } from '../conversations/mergeLandmarks';

// The S7 envelopes deliberately differ from the long-lived Claude UI model.
// These adapters are the one data/render boundary: shared reader components see
// their established model while qualified identity and provider-native meaning
// stay intact. Adapters never decode the opaque v1 key; URL routing decodes only
// its shape-validated source discriminator so one-segment links stay neutral.

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

// #463 S1 — a per-item cost that may legitimately be ABSENT. The turn remains
// the costing unit under segmentation, so the carrier segment reports the
// turn's cost and every other segment reports null rather than 0. Routing that
// null through `num()` would render $0.00, which is indistinguishable from a
// genuinely free turn.
function costOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
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

// #463 S2 §2.5 — the additive `headings` array. Returns undefined for anything
// that is not a fully-formed array of {key, text}, so a malformed field degrades
// to today's title/summary rendering rather than reaching the reader half-built.
// All-or-nothing, matching the server's own rule for a malformed payload.
function reasoningHeadings(value: unknown): { key: string; text: string }[] | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined;
  const out: { key: string; text: string }[] = [];
  for (const raw of value) {
    const entry = record(raw);
    if (!entry || typeof entry.key !== 'string' || typeof entry.text !== 'string') return undefined;
    out.push({ key: entry.key, text: entry.text });
  }
  return out;
}

function codexReasoning(block: QualifiedBlock): Extract<ConversationBlock, { kind: 'codex_reasoning' }> | null {
  const detail = record(block.detail?.reasoning);
  if (detail?.schema_version === 1) {
    const title = nonBlank(detail.title);
    const summary = nonBlank(detail.summary);
    const body = nonBlank(detail.body);
    if (!title && !summary && !body) return null;
    const headings = reasoningHeadings(detail.headings);
    return {
      kind: 'codex_reasoning', source: nonBlank(detail.source) ?? 'codex',
      title, summary, body,
      ...(headings ? { headings } : {}),
      ...(block.block_key ? { block_key: block.block_key } : {}),
    };
  }
  const body = nonBlank(block.text);
  return body
    ? {
      kind: 'codex_reasoning', source: 'codex', body,
      ...(block.block_key ? { block_key: block.block_key } : {}),
    }
    : null;
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

// #463 S3 §3.5 — the CALL-side `apply_patch` file list. Deliberately narrower
// than `patchFiles` below: these entries never carry a diff, a `diff_source` or
// a per-file `truncated`, so the adapter must not copy those keys onto them
// even if a future server sends them.
function patchRequestFiles(value: unknown): NativePatchRequestFile[] | null {
  if (!Array.isArray(value)) return null;
  const files: NativePatchRequestFile[] = [];
  for (const raw of value) {
    const entry = record(raw);
    if (!entry) return null;
    const file: NativePatchRequestFile = {};
    for (const key of ['path', 'move_path', 'status'] as const) {
      if (typeof entry[key] === 'string') file[key] = entry[key] as string;
    }
    files.push(file);
  }
  return files;
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
    // #463 S3 §3.1 — per-file truncation and diff provenance. Copied only when
    // the server actually supplied them: a pre-S3 entry carries neither, and
    // defaulting `truncated` to false here would let the card claim a file was
    // whole when the server made no such claim.
    if (typeof entry.truncated === 'boolean') file.truncated = entry.truncated;
    if (entry.diff_source === 'retained' || entry.diff_source === 'derived') file.diff_source = entry.diff_source;
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
    // Absent-not-defaulted: a pre-S3 server publishes neither key, and the
    // renderer must be able to tell "the grammar supplied no exit code" (null)
    // from "this server never reads exit codes" (absent).
    ...(typeof card.exit_code === 'number' || card.exit_code === null ? { exit_code: card.exit_code as number | null } : {}),
    ...(typeof card.wall_time_seconds === 'number' || card.wall_time_seconds === null
      ? { wall_time_seconds: card.wall_time_seconds as number | null } : {}),
  };
}

// #463 S4 §6.4 — `'error'` is in this set. It was excluded, so a card carrying
// `status: "error"` on a call whose call-side card is not `terminal` collapsed
// to `'unknown'` and was never flagged, while the server's
// `decode_tool_output_card` set `is_error` for `status in {"failed", "error"}`.
// The spec takes correctness over bug-compatibility and brings the CLIENT to the
// server's definition: reproducing the client "exactly" would have frozen the
// defect, and the server would emit a `tool_error` landmark for a call the
// interface insisted succeeded.
const OUTCOME_STATUSES = new Set(['completed', 'failed', 'error', 'running', 'unknown']);

// The two statuses that mean the call failed, byte-identical to the server
// kernel's `_FAILED_STATUSES`. `running` and `unknown` are NOT here: `unknown`
// is a real state covering 17.6% of outputs — 4,585 of them open sessions,
// measured — rather than an absence, and reporting it as a failure would invent
// errors the reader could then not find.
//
// Round 4 — EXPORTED, because `NativePatchCard` renders its own failure badge
// from the card rather than from the adapted `result`, and it was testing a bare
// `status === 'failed'` literal. A card family that decides "failed" against a
// private copy of the vocabulary is exactly how the server's `tool_error`
// landmark and the reader's rendering came to disagree.
export const FAILED_STATUSES: ReadonlySet<string> = new Set(['failed', 'error']);

// #463 S3 §5.1 — derive the block-level outcome from a VALIDATED result-side
// card. An unrecognized status resolves to 'unknown' rather than passing a
// future vocabulary through: 'unknown' is the truthful "not classifiable here"
// state and is rendered as an explicit neutral outcome, not as an absence.
function toolOutcome(output: Extract<NativeToolCard, { type: 'terminal' }>['output']): ToolOutcome | undefined {
  if (!output) return undefined;
  return {
    status: (OUTCOME_STATUSES.has(output.status) ? output.status : 'unknown') as ToolOutcome['status'],
    exit_code: typeof output.exit_code === 'number' ? output.exit_code : null,
    wall_time_seconds: typeof output.wall_time_seconds === 'number' ? output.wall_time_seconds : null,
  };
}

function programInvocations(value: unknown): NativeProgramInvocation[] | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined;
  const out: NativeProgramInvocation[] = [];
  for (const raw of value) {
    const entry = record(raw);
    if (!entry) return undefined;
    if (entry.kind === 'command') {
      if (typeof entry.command !== 'string') return undefined;
      out.push({
        kind: 'command', command: entry.command,
        workdir: typeof entry.workdir === 'string' ? entry.workdir : null,
        metadata: record(entry.metadata) ?? {},
      });
      continue;
    }
    if (entry.kind === 'session') {
      if ((entry.scope !== 'shell' && entry.scope !== 'cell')
          || (entry.operation !== 'write' && entry.operation !== 'poll')) return undefined;
      out.push({
        kind: 'session', scope: entry.scope, operation: entry.operation,
        ref: typeof entry.ref === 'string' ? entry.ref : null,
        chars: typeof entry.chars === 'string' ? entry.chars : null,
      });
      continue;
    }
    if (entry.kind === 'other' && typeof entry.name === 'string' && entry.name !== '') {
      out.push({ kind: 'other', name: entry.name });
      continue;
    }
    // An unknown invocation kind fails the WHOLE card, per the standing
    // validate-before-dispatch rule: a partial program card would tell the
    // reader the program did less than it did.
    return undefined;
  }
  return out;
}

function nativeProgramCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'program' }> | undefined {
  const invocations = programInvocations(card.invocations);
  if (!invocations || typeof card.complete !== 'boolean' || typeof card.truncated !== 'boolean') return undefined;
  return {
    schema_version: 1, type: 'program',
    title: typeof card.title === 'string' ? card.title : null,
    complete: card.complete, invocations, truncated: card.truncated,
  };
}

function nativeSessionRefCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'session_ref' }> | undefined {
  if ((card.scope !== 'shell' && card.scope !== 'cell')
      || (card.operation !== 'write' && card.operation !== 'poll')
      || typeof card.truncated !== 'boolean') return undefined;
  return {
    schema_version: 1, type: 'session_ref', scope: card.scope, operation: card.operation,
    // `ref` null at shell scope is a KNOWN, recorded limitation — the server
    // registers a session only from a standalone write_stdin row, so a session
    // named inside a program body has no ordinal. Carry the null through; never
    // substitute an ordinal and never fall back to a provider identifier.
    ref: typeof card.ref === 'string' ? card.ref : null,
    chars: typeof card.chars === 'string' ? card.chars : null,
    truncated: card.truncated,
  };
}

function nativeToolSearchCard(card: Record<string, unknown>): Extract<NativeToolCard, { type: 'tool_search' }> | undefined {
  if (typeof card.query !== 'string') return undefined;
  return {
    schema_version: 1, type: 'tool_search', query: card.query,
    limit: typeof card.limit === 'number' && Number.isFinite(card.limit) ? card.limit : null,
    ...(card.truncated === true ? { truncated: true } : {}),
  };
}

// #463 S3 §3.2 — the envelope's session index. All-or-nothing: a half-built map
// would let the reader label a session "opener not retained" when the entry was
// simply malformed.
function sessionIndex(value: unknown): ConversationSessionIndex | undefined {
  const index = record(value);
  const sessions = record(index?.sessions);
  if (!index || !sessions || typeof index.truncated !== 'boolean') return undefined;
  // Prototype-free: the keys come straight off the wire, and assigning a
  // "__proto__" key onto a plain object sets the prototype instead of storing
  // data. `Object.create(null)` makes every key ordinary data.
  const out: ConversationSessionIndex['sessions'] = Object.create(null);
  for (const [key, raw] of Object.entries(sessions)) {
    const entry = record(raw);
    if (!entry || !Number.isSafeInteger(entry.ordinal)
        || (entry.opener_block_key !== null && typeof entry.opener_block_key !== 'string')) return undefined;
    out[key] = { ordinal: entry.ordinal as number, opener_block_key: entry.opener_block_key as string | null };
  }
  return { sessions: out, truncated: index.truncated };
}

// #463 S3 §5.5 / wire contract §7. `span` is half-open [start, end) into THIS
// block's own text and exists so the marker is not rendered twice — once as raw
// prose and once as the structured disclosure. The server verifies the span
// against the exact served text before publishing it, so a published span
// resolves; a span that nevertheless does not is treated as fail-closed here
// too, leaving the prose whole rather than throwing or cutting arbitrary text.
function externalCall(value: unknown): { block: Omit<Extract<ConversationBlock, { kind: 'external_call' }>, 'block_key'>; span: [number, number] | null } | null {
  const call = record(value);
  if (call?.schema_version !== 1 || typeof call.name !== 'string' || call.name === ''
      || !('input' in call) || typeof call.truncated !== 'boolean') return null;
  const raw = call.span;
  const span = Array.isArray(raw) && raw.length === 2
    && Number.isSafeInteger(raw[0]) && Number.isSafeInteger(raw[1])
    && raw[0] >= 0 && raw[1] >= raw[0]
    ? [raw[0] as number, raw[1] as number] as [number, number]
    : null;
  return {
    block: { kind: 'external_call', name: call.name, input: call.input, truncated: call.truncated },
    span,
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
  if (card.type === 'program') return nativeProgramCard(card);
  if (card.type === 'session_ref') return nativeSessionRefCard(card);
  if (card.type === 'tool_search') return nativeToolSearchCard(card);
  if (card.type !== 'patch') return undefined;
  const requestFiles = patchRequestFiles(card.files);
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

// #463 S3 §5.4 — an untitled program's collapsed identity: the first
// recognized invocation with a count of the rest. It never claims to be the
// whole program; the card body carries the `complete: false` correction.
//
// `programInvocations` rejects an empty list outright, so `invocations[0]` is
// always present here. The session wording comes from the shared vocabulary in
// `sessionIndex.ts`, which is what the card body renders too.
function programPreview(invocations: NativeProgramInvocation[]): string {
  const first = invocations[0];
  const head = first.kind === 'command' ? first.command
    : first.kind === 'session'
      ? [sessionOperationLabel(first.operation), sessionReferenceLabel(first.scope, first.ref)]
        .filter(Boolean).join(' ')
    : first.name;
  return invocations.length > 1 ? `${head} +${invocations.length - 1} more` : head;
}

// `write_stdin` shows what it wrote; `wait` shows the cell it polled. A null
// ref contributes no reference at all — never an invented ordinal. The chars
// are clamped by the shared bound, so the collapsed row and the card body
// cannot disagree about how much of the write they show.
function sessionRefPreview(card: Extract<NativeToolCard, { type: 'session_ref' }>): string {
  const reference = sessionReferenceLabel(card.scope, card.ref);
  const chars = card.chars == null ? '' : sessionCharsPreview(card.chars);
  return [reference, chars].filter(Boolean).join(' · ') || card.operation;
}

type QualifiedMetaKind = 'skill' | 'command' | 'context' | 'compaction' | 'notification';

function cleanQualifiedTitle(title: string | null | undefined): string | undefined {
  if (!title) return undefined;
  // Codex skill invocations are real prompts, but their native Markdown link
  // leaks a private filesystem path into every title surface. Preserve the
  // skill identity and prompt text; remove only that leading SKILL.md target.
  // #463 S4 F-E — no trailing lookahead. The `(?=\s|$)` form failed on prompt
  // text written straight against the closing paren, leaving the whole link —
  // absolute filesystem path included — in the title. The horizontal
  // whitespace after the link is consumed and re-inserted as exactly one space
  // when prompt text follows, which is what the server's `_clean_outline_title`
  // join produces, so the two still agree on the same input and applying both
  // stays a no-op.
  return title.replace(
    /^\[((?:\$)[^\]\r\n]+)\]\([^\)\r\n]*\/SKILL\.md\)[ \t]*/,
    (match, label: string, offset: number, whole: string) =>
      (offset + match.length < whole.length ? `${label} ` : label),
  );
}

export function adaptBlocks(blocks: QualifiedBlock[], source: ConversationSource): ConversationBlock[] {
  const out: ConversationBlock[] = [];
  for (const block of blocks) {
    if (block.kind === 'assistant' || block.kind === 'user' || block.kind === 'text') {
      // #463 S3 §5.5 — the external-agent marker. Its raw prose stays in the
      // row's own text because the export bytes are frozen, so the marker's
      // span is removed from the prose block here and the structured
      // disclosure follows it in document order.
      //
      // `assistant` only, matching what the server publishes: a human turn
      // renders its prose from `item.text`, which retains the marker, so
      // honouring the field there would render the call twice.
      //
      // The block is emitted ONLY when its span resolves. An unresolved span
      // leaves the marker in the served prose, so emitting the disclosure as
      // well is exactly the double render the span exists to prevent; the
      // documented degradation is the full text alone.
      const external = block.kind === 'assistant'
        ? externalCall(block.detail?.external_call) : null;
      const span = external?.span != null && block.text != null
        && external.span[1] <= block.text.length ? external.span : null;
      // Strip only the newline the removal orphaned. A whole-remainder
      // `trimEnd()` also swallows a two-space Markdown hard break, which the
      // non-marker path — which alters the served text not at all — keeps.
      const text = span
        ? `${block.text!.slice(0, span[0])}${block.text!.slice(span[1])}`.replace(/[ \t]*\n+$/, '')
        : block.text;
      // #463 S2 §3.2 — retain the server's per-row anchor so a separately
      // authored message keeps its identity through adaptation. `MessageBlocks`
      // then renders one container per source block instead of coalescing a run.
      if (text) out.push({
        kind: 'text', text,
        ...(block.block_key ? { block_key: block.block_key } : {}),
      });
      if (external && span) out.push({
        ...external.block,
        ...(block.block_key ? { block_key: block.block_key } : {}),
      });
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
      // #488 — the qualified Claude dispatcher deliberately publishes the
      // established ConversationBlock shape unchanged: name/input/preview and
      // the folded result are top-level fields, not Codex `detail`/`output`
      // fields. Re-adapting that block through the Codex wire grammar erased
      // every useful field and left a bare "tool" chip. Keep the canonical
      // Claude contract intact; the detail-bearing branch below remains the
      // qualified Codex adapter (and the defensive legacy degradation path).
      const claudeCall = block as unknown as Partial<Extract<ConversationBlock, { kind: 'tool_call' }>>;
      if (source === 'claude' && block.detail == null
          && typeof claudeCall.input_summary === 'string'
          && typeof claudeCall.preview === 'string'
          && 'result' in claudeCall) {
        out.push(claudeCall as Extract<ConversationBlock, { kind: 'tool_call' }>);
        continue;
      }
      const name = typeof block.detail?.name === 'string' ? block.detail.name : null;
      const args = typeof block.detail?.args === 'string' ? block.detail.args : '';
      const nativeCard = nativeToolCard(block);
      const terminal = nativeCard?.type === 'terminal' ? nativeCard : null;
      const patch = nativeCard?.type === 'patch' ? nativeCard : null;
      const plan = nativeCard?.type === 'plan' ? nativeCard : null;
      const web = nativeCard?.type === 'web_search' ? nativeCard : null;
      const mcp = nativeCard?.type === 'mcp' ? nativeCard : null;
      const agent = nativeCard?.type === 'agent' ? nativeCard : null;
      const program = nativeCard?.type === 'program' ? nativeCard : null;
      const sessionCall = nativeCard?.type === 'session_ref' ? nativeCard : null;
      const toolSearch = nativeCard?.type === 'tool_search' ? nativeCard : null;
      const input = terminal
        ? { command: terminal.commands[0].command, workdir: terminal.commands[0].workdir }
        : web ? { query: web.query, action: web.action }
        : mcp ? mcp.completion.arguments
        : agent ? agent.arguments
        : parseArgs(args);
      // #463 S3 §5.1 — the result-side unlock. `nativeToolCard` returns
      // undefined at the CALL-side check, so before this the result structure
      // was read only where the call already validated as a native terminal —
      // which is why 34,935 calls discarded a result structure every one of
      // them possessed. Read it independently here.
      //
      // Gated on Codex deliberately: `adaptBlocks` is provider-neutral and
      // `result.is_error` is presented by four card components, so widening the
      // field for Claude as a side effect of a Codex fix is out of scope and
      // untested.
      const outcome = source === 'codex' ? toolOutcome(terminalOutputCard(block.output?.detail?.card)) : undefined;
      // ADDITIVE: a failing outcome can only turn is_error on, never off. A
      // patch whose `success` is false stays an error even when its output card
      // resolved to `completed`.
      // #463 S4 §6.4 — ONE failure-status set across every disjunct, matching
      // `_lib_codex_landmarks._FAILED_STATUSES`. Reading `error` on web and MCP
      // but `failed` on the others would mean a web completion carrying
      // `failed` is a failure nobody flags, which is the same defect as the
      // `'error'` collapse with the two words exchanged.
      const outputError = terminal?.output?.is_error === true || patch?.success === false
        || FAILED_STATUSES.has(web?.completion.status ?? '')
        || FAILED_STATUSES.has(mcp?.completion.status ?? '')
        || FAILED_STATUSES.has(outcome?.status ?? '');
      // #463 S3 §5.4 — what each family shows in its collapsed row. `program`
      // and `session_ref` have dedicated components that compose their own
      // summary; this keeps the neutral `preview` honest for every other
      // consumer of it. `tool_search` has no component at all, so its query
      // reaches the reader through here and nowhere else.
      // `nonBlank`, not a bare `??`: an empty authored title or an empty query
      // is not nullish, so it won the chain and produced an EMPTY collapsed row
      // where the pre-S3 fallback showed the first line of the call text.
      const semanticPreview = nonBlank(plan?.explanation)
        ?? nonBlank(web?.query)
        ?? (mcp ? nonBlank(`${mcp.completion.tool} · ${mcp.completion.server}`) : undefined)
        ?? (agent ? nonBlank(agent.operation) : undefined)
        ?? nonBlank(toolSearch?.query)
        ?? nonBlank(program?.title)
        ?? (program ? nonBlank(programPreview(program.invocations)) : undefined)
        ?? (sessionCall ? nonBlank(sessionRefPreview(sessionCall)) : undefined);
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
        ...(block.block_key ? { block_key: block.block_key } : {}),
        payload_capable: block.block_key != null,
        payload_kind: 'call',
        native_card: nativeCard,
        ...(outcome ? { outcome } : {}),
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
          tool_use_id: block.block_key ?? null,
          ...(block.block_key ? { block_key: block.block_key } : {}),
          payload_capable: block.block_key != null, payload_kind: 'event',
          native_card: nativeCard,
          // #463 S4 remediation round 4 — `FAILED_STATUSES`, not a bare
          // `'failed'` literal. `classify_tool_failure` fails a patch on
          // `success is False` OR `status in {"failed", "error"}`, and
          // `decode_patch_event_card` passes the provider's raw status through,
          // so a `patch_apply_end` carrying `status: "error"` without
          // `success: false` got a `tool_error` landmark from the server — which
          // the Errors badge counts — while this branch called it not-failed and
          // the Errors filter then hid the very turn the badge had counted. Same
          // disagreement class as the branch directly above, one level up.
          result: {
            text, truncated: nativeCard.truncated === true,
            is_error: nativeCard.success === false || FAILED_STATUSES.has(nativeCard.status),
          },
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
      // #463 S4 remediation round 3 — the failure verdict, from BOTH shapes.
      // `detail.is_error` is the Claude-shaped flag; a Codex `tool_output` puts
      // its verdict on the card `decode_tool_output_card` wrote, and reading
      // only the first field rendered a server-classified failure — the very
      // row that carries the `tool_error` landmark this branch exists to give an
      // address to — with no failure treatment at all. The status is consulted
      // alongside `is_error` for the same reason the call-side disjunction does:
      // a card built by a caller that did not carry the flag forward still
      // states its status, and `FAILED_STATUSES` is the one definition of which
      // statuses are failures.
      const outputCard = terminalOutputCard(block.detail?.card);
      out.push({
        kind: 'tool_result',
        text: block.text ?? '',
        truncated: outputCard?.truncated === true,
        is_error: block.detail?.is_error === true
          || outputCard?.is_error === true
          || FAILED_STATUSES.has(outputCard?.status ?? ''),
        tool_use_id: block.call_id ?? block.block_key ?? null,
        // #463 S4 remediation C-4 — the row's jump address, kept distinct from
        // `tool_use_id`: that field falls back to the block key when there is no
        // call id, so it is not a reliable address. A `tool_output` reaches this
        // branch only when it did NOT fold into a call — the case
        // `_landmark_label` documents, where two calls in the turn own the same
        // id — and that is exactly the case where it carries a landmark of its
        // own and needs an address in the DOM.
        ...(block.block_key ? { block_key: block.block_key } : {}),
      });
      continue;
    }
    // Lifecycle, patch, MCP and web events stay explicit, searchable prose.
    // #463 S4 remediation C-4 (sibling site) — with the key, because this is
    // where an UNFOLDED `web_search_end` or `mcp_tool_call_end` lands, and a
    // failing one of those is a `tool_error` landmark anchored on its own
    // block key. `MessageBlocks` already renders `data-block-key` on a text
    // block; the adapter was the only thing withholding it.
    //
    // #463 S4 remediation round 3 (F12) — the emptiness gate is on the KEY as
    // well as on the text. Gated on text alone, a text-less event row produced
    // no block at all, so a `tool_error` landmark anchored on it named an
    // element that was never rendered and the jump degraded to aligning the
    // whole item. That row does not occur in the store measured here (0 of
    // 219,503 normalized rows have an empty display text on a kind that reaches
    // this branch), so this emits an empty container in a case production does
    // not currently produce — and it closes the class rather than leaving the
    // one branch of the C-4 fix that still drops an address.
    if (block.text) {
      out.push({
        kind: 'text', text: block.text,
        ...(block.block_key ? { block_key: block.block_key } : {}),
      });
    } else if (block.block_key) {
      out.push({ kind: 'text', text: '', block_key: block.block_key });
    }
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
  // #294 S6 — every item key this item subsumes (folded completion events and
  // the like). The server has always emitted it and `_paginate_items` has
  // always resolved a cursor through it; the client used to discard it.
  member_item_keys?: string[];
  // #463 S1 — segmentation. `turn_item_key` equals segment 0's key, so grouping
  // on it recovers the turn; `segment_ordinal` is 0 for a whole item and for a
  // turn's first segment.
  turn_item_key?: string;
  segment_ordinal?: number;
  meta_kind?: string | null;
  meta_label?: string | null;
  meta_sections?: string[] | null;
  skill_name?: string | null;
  command_name?: string | null;
  subagent_key?: string | null;
  parent_item_key?: string | null;
  is_sidechain?: boolean;
  cache_failure?: OutlineTurn['cache_failure'];
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
    anchor,
    // Thread the server's aliases rather than a singleton. The own key stays
    // FIRST so `resolveJumpOwner` and `nodeIndexForUuid` keep resolving the item
    // by its own uuid; the folded fragment keys follow.
    member_uuids: [item.item_key, ...(item.member_item_keys ?? [])],
    ts: item.timestamp_utc ?? '',
    text, blocks,
    is_sidechain: item.is_sidechain ?? false,
    subagent_key: item.subagent_key ?? null,
    parent_uuid: item.parent_item_key ?? null,
    // #463 S1 — turn membership, so a consumer recovers the turn by grouping on
    // `turn_uuid` without recomputing boundaries. Both are absent on a wire
    // envelope from a server that predates segmentation.
    ...(item.turn_item_key !== undefined ? { turn_uuid: item.turn_item_key } : {}),
    ...(item.segment_ordinal !== undefined ? { segment_ordinal: item.segment_ordinal } : {}),
  };
  const assistantLike = item.kind === 'assistant' || item.kind === 'reasoning' || item.kind === 'tool_call'
    || blocks.some((block) => block.kind === 'tool_call');
  if (assistantLike) {
    const lifecycle = foldedLifecycle(item.lifecycle);
    return {
      ...common, kind: 'assistant' as const, model: item.model,
      // NOT `num()`: `num(null)` is 0, and a non-carrier segment's null cost
      // would then render as $0.00 — indistinguishable from a genuinely free
      // turn, which is exactly what the server's null exists to prevent.
      cost_usd: costOrNull(item.cost_usd), tokens: adaptQualifiedTokens(item.tokens),
      ...(lifecycle ? { lifecycle } : {}),
      ...(item.cache_failure ? { cache_failure: item.cache_failure } : {}),
    };
  }
  if (item.kind === 'user' || item.kind === 'human') {
    return {
      ...common, kind: 'human' as const,
      ...(item.command_name !== undefined ? { command_name: item.command_name } : {}),
    };
  }
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
  subagent_meta?: ConversationDetail['subagent_meta'];
  // #463 S3 §3.2 — conversation-scoped, re-sent on every page.
  session_index?: unknown;
};

export function adaptQualifiedDetail(ref: ConversationRef, body: QualifiedDetailEnvelope): ConversationDetail {
  if (body.status === 'normalization_pending') throw new ConversationNormalizationPending();
  if (body.status !== 'ok') throw new Error('Conversation not found.');
  const items = (body.items ?? []).map((item) => adaptItem(ref, item));
  const models = Array.from(new Set((body.items ?? []).map((item) => item.model).filter((m): m is string => !!m)));
  const firstTs = (body.items ?? []).find((item) => item.timestamp_utc)?.timestamp_utc ?? '';
  const lastTs = [...(body.items ?? [])].reverse().find((item) => item.timestamp_utc)?.timestamp_utc ?? firstTs;
  const page = body.page;
  const index = sessionIndex(body.session_index);
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
    ...(body.subagent_meta ? { subagent_meta: body.subagent_meta } : {}),
    ...(index ? { session_index: index } : {}),
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

export function adaptQualifiedBrowse(
  source: ConversationSource,
  body: QualifiedBrowseEnvelope,
  accountKey?: string,
): {
  rows: ConversationSummary[]; cursor: string | null; total: number; pending: boolean;
} {
  if (body.status === 'normalization_pending') return { rows: [], cursor: null, total: 0, pending: true };
  return {
    rows: body.rows.map((row) => ({
      conversation_ref: accountKey
        ? { source, key: row.conversation_key, account_key: accountKey }
        : { source, key: row.conversation_key },
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

export function adaptQualifiedSearch(
  source: ConversationSource,
  body: QualifiedSearchEnvelope,
  accountKey?: string,
): ConversationSearchResult & { cursor: string | null; pending: boolean } {
  if (body.status === 'normalization_pending') {
    return { query: body.query, mode: body.mode, hits: [], total: 0, search_depth: body.depth, cursor: null, pending: true };
  }
  const hits: SearchHit[] = body.hits.map((hit) => ({
    conversation_ref: accountKey
      ? { source, key: hit.conversation_key, account_key: accountKey }
      : { source, key: hit.conversation_key },
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
    kind?: 'assistant' | 'human' | 'tool_result' | 'meta';
    label: string;
    timestamp_utc: string | null;
    kinds: Record<string, number>;
    // Item keys this turn SUBSUMES (folded fragments).
    member_item_keys?: string[];
    // #463 S1 — the keys of this turn's segments, entry `i` being segment `i`.
    // A channel deliberately DISTINCT from member_item_keys: see the note in
    // the mapping below.
    segment_item_keys?: string[];
    meta_kind?: OutlineTurn['meta_kind'] | null;
    meta_label?: string | null;
    meta_sections?: string[] | null;
    skill_name?: string | null;
    // #463 S4 §3.1 — tier-1 enrichment. `tools` is deduplicated by name, so
    // `tool_call_count` and `first_failure_name` carry what dedupe destroys.
    tools?: { name: string | null; is_error: boolean }[];
    tool_call_count?: number;
    first_failure_name?: string | null;
    thinking?: string[];
    model?: string | null;
    tokens?: NativeTokens;
    subagent_key?: string | null;
    parent_item_key?: string | null;
    is_sidechain?: boolean;
    cache_failure?: OutlineTurn['cache_failure'];
  }[];
  // #463 S4 §3.2 — tier 2, anchored on segment keys.
  landmarks?: {
    landmark_key: string;
    block_key: string;
    item_key: string;
    parent_item_key: string;
    kind: string;
    label: string;
    timestamp_utc: string | null;
  }[];
  stats?: {
    items?: number;
    kinds?: Record<string, number>;
    turns?: { total: number; human: number; assistant: number; tool_result: number; meta: number };
    tool_counts?: Record<string, number>;
    // #463 S4 D3 — nullable on the wire, and it must stay nullable here.
    error_count?: number | null;
    models?: Record<string, number>;
    duration_seconds?: number | null;
    tokens?: NativeTokens;
    cost_usd?: number;
    cache_saved_usd?: number;
    cache_failures?: ConversationOutline['stats']['cache_failures'];
  };
  // #463 S4 §3.5 — a Codex file entry is RICH: it carries per-touch segment
  // anchors and diff counts. An entry with no `touches` is the count-only S7
  // shape, which has no jump target and stays on `provider_files`.
  files?: {
    file_path: string; tool: string; count: number;
    added?: number | null; removed?: number | null;
    touches?: {
      item_key: string; timestamp_utc: string | null; op: string | null;
      tool_use_id?: string | null; added?: number | null; removed?: number | null;
    }[];
  }[];
  subagent_meta?: ConversationOutline['subagent_meta'];
  subagent_costs?: ConversationOutline['subagent_costs'];
  task_completion?: ConversationOutline['task_completion'];
  children?: { conversation_key: string; title: string | null; cost_usd: number }[];
};

// #463 S4 §6.6 — the closed op vocabulary. A change kind outside it becomes
// `null`, which the badge renders as nothing; passing an unknown string through
// would reach `OP_LABEL`'s closed record and render an empty label instead.
const FILE_OPS = new Set<string>([
  'edit', 'multiedit', 'write', 'add', 'delete', 'update', 'modified',
]);
const LANDMARK_KINDS = new Set<string>(['reasoning', 'tool_error', 'plan']);

export function adaptQualifiedOutline(
  ref: ConversationRef,
  body: QualifiedOutlineEnvelope,
  totals: { total_cost_usd?: number; tokens?: NativeTokens } = {},
  promptItemKeys?: ReadonlySet<string>,
): ConversationOutline {
  if (body.status === 'normalization_pending') throw new ConversationNormalizationPending();
  if (body.status !== 'ok') throw new Error('Conversation not found.');
  const landmarks: OutlineLandmark[] = (body.landmarks ?? [])
    .filter((landmark) => LANDMARK_KINDS.has(landmark.kind))
    .map((landmark) => ({
      landmark_key: landmark.landmark_key,
      block_key: landmark.block_key,
      uuid: landmark.item_key,
      parent_uuid: landmark.parent_item_key,
      kind: landmark.kind as OutlineLandmark['kind'],
      label: landmark.label,
      ts: landmark.timestamp_utc,
    }));
  const owners = landmarkOwners(landmarks);
  const turns: OutlineTurn[] = (body.turns ?? []).flatMap((turn) => {
    const nativeKind = turn.kind;
    const isEvent = (turn.kinds.event ?? 0) > 0;
    const isPrompt = promptItemKeys?.has(turn.item_key) ?? false;
    const isCompaction = turn.meta_kind === 'compaction'
      || (isEvent && turn.label.includes('context_compacted'));
    // #463 S4 §1.3 — the retention rule. S1 measured all 589 multi-segment
    // Codex turns in the corpus carrying event rows and being dropped by the
    // filter below, and those are exactly the heavy turns that own landmarks.
    // A tier-2 entry parented to a turn absent from `turns` is an orphan, so
    // without this the whole second tier would be unreachable. This is not a
    // weakening of the filter but an application of its stated purpose: the
    // filter exists because the canonical outline is navigation rather than a
    // second event log, and a turn that owns a landmark is navigation.
    //
    // Codex-only IN EFFECT rather than by a source check — Claude turns receive
    // no landmarks, so the predicate reduces to today's on that side.
    const ownsLandmark = owners.has(turn.item_key);
    // The canonical Codex outline is navigation, not a second event log. Keep
    // prompts, logical assistant responses and compactions; detail still
    // preserves every lifecycle/tool/meta row. Claude's native outline role is
    // authoritative, so it bypasses this Codex retention rule.
    if (!nativeKind && !ownsLandmark
        && (((turn.kinds.meta ?? 0) > 0) || (isEvent && !isCompaction))) return [];
    const isHuman = nativeKind === 'human' || isPrompt || (turn.kinds.user ?? 0) > 0;
    // Current qualified rows carry the semantic assistant kind. Older Claude
    // projections expose text-only turns, so after prompt/meta/event exclusion
    // the remaining prose row is still the logical assistant response.
    const isAssistant = nativeKind === 'assistant' || (turn.kinds.assistant ?? 0) > 0
      || (!isHuman && !isEvent && (turn.kinds.meta ?? 0) === 0);
    // The retention rule has to clear BOTH gates. A turn of pure tool traffic
    // — `{event, tool_call}` with no prose row — matches neither branch above,
    // so passing only the first gate would still drop it and orphan its
    // landmarks. It is a model turn, and it files as one.
    if (!nativeKind && !isHuman && !isAssistant && !isCompaction && !ownsLandmark) return [];
    const tokenUsage = adaptQualifiedTokens(turn.tokens);
    return [{
      uuid: turn.item_key,
      kind: nativeKind ?? (isCompaction ? 'meta' : isHuman ? 'human' : 'assistant'),
      ts: turn.timestamp_utc,
      label: cleanQualifiedTitle(turn.label) ?? turn.label,
      member_uuids: [turn.item_key, ...(turn.member_item_keys ?? [])],
      // #463 S1 — segment keys go on their OWN channel, never into
      // member_uuids. `loadToTarget` treats a uuid present in an item's
      // member_uuids as already loaded, so folding segment keys in would make
      // the drain skip a segment that has not been fetched and the jump would
      // land nowhere. Membership for navigation and membership for "this item
      // subsumes that key" are different relations.
      ...(turn.segment_item_keys ? { segment_uuids: turn.segment_item_keys } : {}),
      subagent_key: turn.subagent_key ?? null,
      parent_uuid: turn.parent_item_key ?? null,
      is_sidechain: turn.is_sidechain ?? false,
      // #463 S4 §3.1 — tier-1 enrichment, additive and only where the server
      // said something. Codex omits `subagent_key` and `cache_failure`
      // (§6.2) because it nests through separate child conversations, while
      // qualified Claude retains both from its native outline.
      // fabricating a grouping that does not exist would be worse than an empty
      // family, and a cache failure is a Claude concept.
      ...(turn.tools ? { tools: turn.tools } : {}),
      ...(turn.tool_call_count != null ? { tool_call_count: turn.tool_call_count } : {}),
      ...(turn.first_failure_name !== undefined
        ? { first_failure_name: turn.first_failure_name } : {}),
      ...(turn.thinking ? { thinking: turn.thinking } : {}),
      ...(turn.model ? { model: turn.model } : {}),
      ...(tokenUsage ? { tokens: tokenUsage } : {}),
      ...(turn.cache_failure ? { cache_failure: turn.cache_failure } : {}),
      ...(turn.meta_kind ? { meta_kind: turn.meta_kind } :
        isCompaction ? { meta_kind: 'compaction' as const } : {}),
      ...(turn.skill_name !== undefined ? { skill_name: turn.skill_name } : {}),
    }];
  });
  const richFiles: OutlineFile[] = [];
  const providerFiles: QualifiedOutlineFile[] = [];
  for (const file of body.files ?? []) {
    if (!file.touches) {
      providerFiles.push({ path: file.file_path, tool: file.tool, count: file.count });
      continue;
    }
    richFiles.push({
      path: file.file_path,
      // The wire names these `added`/`removed`; the client's fields are `add`
      // and `del`. Mapped rather than assumed, because the two vocabularies are
      // both correct on their own side.
      add: file.added ?? null,
      del: file.removed ?? null,
      touches: file.touches.map((touch) => ({
        uuid: touch.item_key,
        tool_use_id: touch.tool_use_id ?? null,
        op: (touch.op != null && FILE_OPS.has(touch.op)
          ? touch.op : null) as OutlineFileTouch['op'],
        add: touch.added ?? null,
        del: touch.removed ?? null,
      })),
    });
  }
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
      // #463 S4 D3 — NOT `num(...)`. The coercion turned "nobody could tell"
      // into "nothing failed", which is the literal defect F13 names.
      error_count: typeof body.stats?.error_count === 'number'
        ? body.stats.error_count : null,
      models: body.stats?.models ?? {},
      duration_seconds: body.stats?.duration_seconds ?? null,
      tokens: tokenTotals,
      cost_usd: num(totals.total_cost_usd ?? body.stats?.cost_usd),
      cache_saved_usd: num(body.stats?.cache_saved_usd),
      ...(body.stats?.cache_failures
        ? { cache_failures: body.stats.cache_failures } : {}),
    },
    // #463 S4 §3.5/§6.6 — a file entry carrying `touches` has real segment
    // anchors and diff counts, so it routes through the rich `FileRow` path and
    // its rows jump. One carrying only a count has no jump target and stays on
    // `provider_files`. The split is driven by the SHAPE the server sent rather
    // than by a source string, so a provider that starts sending touches is
    // picked up without a second gate to remember.
    //
    // A file never appears in both. Rendering both arrays for the same file
    // would list it twice, and it is the inert provider row that kept the rich
    // path unreachable for Codex.
    files: richFiles,
    ...(providerFiles.length ? { provider_files: providerFiles } : {}),
    ...(body.subagent_meta ? { subagent_meta: body.subagent_meta } : {}),
    ...(body.subagent_costs ? { subagent_costs: body.subagent_costs } : {}),
    ...(body.task_completion !== undefined
      ? { task_completion: body.task_completion } : {}),
    turns,
    ...(landmarks.length ? { landmarks } : {}),
    positionByKey: buildQualifiedOutlinePositions(body.turns ?? []),
  };
}

// #463 S1 — document position of every addressable key, built over the FULL wire
// turn list BEFORE the navigation filter above removes meta and event-bearing
// turns. Positions are segment-granular, so they match detail order exactly: a
// turn contributes one position per segment, in wire order. A turn's own key and
// each of its folded member keys map to its HEAD segment's position, which is the
// position they already resolved to through the turn-granular outline index.
//
// Segment granularity is essential to the direction decision, not tidiness. A
// turn-granular position would give every segment of one turn the same number,
// so a drain toward segment 2 of a turn whose segment 5 is already the window's
// first item would compute "not above the window" and page the WRONG WAY, away
// from the target.
//
// The index is PROVIDER-NEUTRAL and is published for every qualified
// conversation, Claude included, even though segmentation itself is Codex-only.
// A Claude turn carries no `segment_item_keys`, so it contributes exactly one
// position and the ordering is plain wire order. The navigation filter drops
// meta and event-bearing turns on Claude too, so a Claude jump into one of them
// previously resolved to no turn and `loadToTarget` returned without issuing a
// page; it now drains toward the target like any other. That is a deliberate
// widening of the fix rather than an accident of where the call sits.
export function buildQualifiedOutlinePositions(
  turns: { item_key: string; member_item_keys?: string[]; segment_item_keys?: string[] }[],
): ReadonlyMap<string, number> {
  const positions = new Map<string, number>();
  let position = 0;
  for (const turn of turns) {
    const head = position;
    // A turn with no segment list occupies exactly one position (its own key).
    for (const key of (turn.segment_item_keys?.length ? turn.segment_item_keys : [turn.item_key])) {
      if (!positions.has(key)) positions.set(key, position);
      position += 1;  // advance even on a duplicate key so document order holds
    }
    // A turn's OWN key is set unconditionally, overwriting any entry an earlier
    // turn contributed by naming this key as one of its members. That mirrors
    // `resolveTurnIndex`, which checks the own-key map before the member map for
    // exactly this reason: a turn that lists another turn's key as a member must
    // not shadow the real owner. It is not reachable with today's key
    // derivation, and it is kept because the same defence is pinned on the
    // skeleton index and the two must not disagree.
    positions.set(turn.item_key, head);
    for (const key of turn.member_item_keys ?? []) {
      if (!positions.has(key)) positions.set(key, head);
    }
  }
  return positions;
}

type QualifiedFindWire = {
  status: string;
  conversation_key?: string;
  schema_version?: number;
  semantics?: string;
  query_id?: string;
  selection_stale?: boolean;
  anchors?: { item_key: string; match_kinds: string[] }[];
  total?: number;
  anchors_truncated?: boolean;
  mode?: 'fts' | 'like' | 'regex' | 'literal';
  search_depth?: 'prose-only' | 'full';
  kind?: string;
  page?: {
    start_index?: number;
    previous_cursor?: string | null;
    next_cursor?: string | null;
    occurrences?: Array<{
      occurrence_id?: string;
      item_key?: string;
      block_key?: string;
      container_block_key?: string;
      surface?: string;
      match_kinds?: string[];
      disclosure?: string[];
      fragments?: Array<{ leaf_key?: string; start?: number; end?: number }>;
    }>;
  };
  [key: string]: unknown;
};

export function adaptQualifiedFind(body: QualifiedFindWire): ConversationFindResult {
  if (body.schema_version === 2 && body.semantics === 'occurrence') {
    const occurrences = (body.page?.occurrences ?? []).flatMap((occurrence) => {
      if (
        typeof occurrence.occurrence_id !== 'string'
        || typeof occurrence.item_key !== 'string'
        || typeof occurrence.block_key !== 'string'
        || typeof occurrence.container_block_key !== 'string'
        || !['body', 'call', 'output', 'completion'].includes(occurrence.surface ?? '')
      ) return [];
      const fragments = (occurrence.fragments ?? []).flatMap((fragment) => (
        typeof fragment.leaf_key === 'string'
        && Number.isInteger(fragment.start)
        && Number.isInteger(fragment.end)
        && (fragment.start ?? -1) >= 0
        && (fragment.end ?? -1) > (fragment.start ?? -1)
          ? [{ leaf_key: fragment.leaf_key, start: fragment.start!, end: fragment.end! }]
          : []
      ));
      return [{
        occurrence_id: occurrence.occurrence_id,
        item_key: occurrence.item_key,
        uuid: occurrence.item_key,
        block_key: occurrence.block_key,
        container_block_key: occurrence.container_block_key,
        surface: occurrence.surface as 'body' | 'call' | 'output' | 'completion',
        match_kinds: (occurrence.match_kinds ?? []).filter(
          (kind): kind is 'tool' | 'thinking' => kind === 'tool' || kind === 'thinking',
        ),
        disclosure: (occurrence.disclosure ?? []).filter(
          (key): key is string => typeof key === 'string',
        ),
        fragments,
      }];
    });
    return {
      schema_version: 2,
      semantics: 'occurrence',
      status: body.status === 'indexing' ? 'indexing' : 'ready',
      query_id: typeof body.query_id === 'string' ? body.query_id : '',
      ...(typeof body.total === 'number' ? { total: body.total } : {}),
      selection_stale: body.selection_stale === true,
      mode: body.mode === 'regex' ? 'regex' : 'literal',
      kind: typeof body.kind === 'string' ? body.kind : 'all',
      search_depth: body.search_depth === 'prose-only' ? 'prose-only' : 'full',
      page: {
        start_index: Number.isInteger(body.page?.start_index) ? body.page!.start_index! : 0,
        previous_cursor: typeof body.page?.previous_cursor === 'string' ? body.page.previous_cursor : null,
        next_cursor: typeof body.page?.next_cursor === 'string' ? body.page.next_cursor : null,
        occurrences,
      },
    };
  }
  const anchors: FindAnchor[] = (body.anchors ?? []).map((anchor) => ({
    uuid: anchor.item_key,
    match_kinds: anchor.match_kinds.filter((kind): kind is 'tool' | 'thinking' => kind === 'tool' || kind === 'thinking'),
  }));
  return {
    anchors, total: body.total ?? 0, anchors_truncated: body.anchors_truncated ?? false,
    mode: body.mode === 'fts' || body.mode === 'regex' ? body.mode : 'like',
    search_depth: body.search_depth ?? 'full',
  };
}

export function adaptQualifiedPrompts(body: { status?: string; conversation_key?: string; prompts?: { item_key: string; text: string }[] }): { prompts: { uuid: string; text: string }[] } {
  return { prompts: (body.prompts ?? []).map((prompt) => ({ uuid: prompt.item_key, text: prompt.text })) };
}

export function adaptQualifiedPayload(
  blockKey: string,
  requested: 'input' | 'result' | 'event',
  // #463 S4 remediation round 5 — the `card` key is a union, because the two
  // branches that read it receive different families. The event branch is handed
  // a `NativeToolCard`; the result branch is handed the `terminal_output` card
  // `_reread_codex_full_content` publishes, which is `NativeTerminalOutput` and
  // is NOT a member of that union. Annotating the key as `NativeToolCard` alone
  // made correct usage un-typeable, so every caller constructing a result body
  // had to cast its way past the annotation.
  body: { which?: 'call' | 'output' | 'event'; content?: string; truncated?: boolean; card?: NativeToolCard | NativeTerminalOutput },
): FullPayload {
  if (requested === 'input') {
    const parsed = parseArgs((body.content ?? '').split('\n').slice(1).join('\n')) ?? { raw: body.content ?? '' };
    return { which: 'input', tool_use_id: blockKey, input: parsed, full_length: (body.content ?? '').length, truncated: body.truncated === true };
  }
  if (requested === 'event') {
    // The body annotation is a union because the two branches receive different
    // card families; `FullPayload`'s event arm means an event card specifically,
    // so discriminate rather than widening that arm to something it does not
    // mean. A `terminal_output` card here would be a server publishing the
    // result card on the event branch — same fail-closed discipline as
    // `terminalOutputCard`, which returns undefined for a foreign family instead
    // of reading a verdict off it.
    const card = body.card?.type === 'terminal_output' ? undefined : body.card;
    return {
      which: 'event', tool_use_id: blockKey, text: body.content ?? '',
      full_length: (body.content ?? '').length, truncated: body.truncated === true,
      card,
    };
  }
  // #463 S4 remediation round 4 — `is_error` comes from the card the route
  // publishes, not from a hard-coded `false`. `_reread_codex_full_content`
  // decodes the same `terminal_output` card the paged detail path reads, so the
  // server states a verdict here and this branch discarded it. No consumer reads
  // the field today (`useFullPayload` callers read `text`/`input` only), which
  // is why nothing observed it — but it is the third instance of the same
  // Claude-shaped assumption as the two above, and the one a future consumer
  // would trust. Same disjunction and same `FAILED_STATUSES` as the paged path.
  //
  // The card the route publishes on THIS branch is a `terminal_output` card,
  // which is why the wire-body annotation above is a union rather than
  // `NativeToolCard` alone. `terminalOutputCard` still validates the shape
  // structurally and returns undefined for anything else, so a card of a
  // different family reaches no verdict rather than a wrong one.
  const resultCard = terminalOutputCard(body.card);
  return {
    which: 'result', tool_use_id: blockKey, text: body.content ?? '',
    full_length: (body.content ?? '').length, truncated: body.truncated === true,
    is_error: resultCard?.is_error === true || FAILED_STATUSES.has(resultCard?.status ?? ''),
  };
}
