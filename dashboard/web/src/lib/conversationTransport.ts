import {
  conversationRefKey,
  normalizeConversationRef,
  type ConversationRef,
  type ConversationSource,
} from '../types/conversation';

export type ConversationTransportStatus =
  | 'ok'
  | 'normalization_pending'
  | 'not_found'
  | 'gone'
  | 'capability_unsupported'
  | 'degraded';

// #780 — every conversation read route can answer HTTP 200 with a typed
// degraded envelope instead of opening the transcript store. The body carries
// the route's own EMPTY SHAPE, which for `/api/conversations` is
// `{conversations: [], total: 0}` and for the qualified browse is neither
// `rows` nor `page`, so a client that parses it as an ordinary page throws.
// Before #769 S6 both browse branches did exactly that and the TypeError landed
// in the same `.catch` that prints a load failure, which is how a 200 became
// "Couldn't load conversations." beside a Retry the state could never satisfy.
//
// The reason names a STORE state, not a request failure. It is the sibling of
// `normalization_pending`, which the adapters have always read off the body and
// reported as a pending surface rather than an error.
export interface ConversationDegradedNotice {
  reason: string;
  message: string;
  // Whether re-issuing the same read can plausibly clear the state. False for
  // every reason that needs a process ALLOWED TO DO WORK to open the store:
  // offering Retry there is a dead-end remedy, which is what the browser QA
  // observed on `legacy_bridge_pending` under `dashboard --no-sync`.
  retryable: boolean;
}

const CONVERSATION_DEGRADED_MESSAGES: Record<string, ConversationDegradedNotice> = {
  legacy_bridge_pending: {
    reason: 'legacy_bridge_pending',
    message: 'Transcripts have not finished moving into the conversation store. '
      + 'Run cctally cache-sync to finish the import.',
    retryable: false,
  },
  maintenance: {
    reason: 'maintenance',
    message: 'The conversation store is busy with maintenance. '
      + 'Conversations return once it finishes.',
    retryable: true,
  },
  schema_behind: {
    reason: 'schema_behind',
    message: 'The conversation store is behind this version of cctally. '
      + 'Run cctally cache-sync to bring it up to date.',
    retryable: false,
  },
  schema_ahead: {
    reason: 'schema_ahead',
    message: 'The conversation store was written by a newer version of cctally. '
      + 'Update cctally to read it.',
    retryable: false,
  },
};

const CONVERSATION_DEGRADED_FALLBACK: ConversationDegradedNotice = {
  reason: 'unavailable',
  message: 'The conversation store is not available right now.',
  retryable: true,
};

/** The `degraded_reason` of a typed degraded envelope, or null for any other body.
 *
 * A degraded envelope with no reason still degrades: the status is what says
 * the body carries no page, so parsing it would throw whether or not the server
 * named a cause.
 */
export function conversationDegradedReason(body: unknown): string | null {
  if (body == null || typeof body !== 'object') return null;
  const envelope = body as { status?: unknown; degraded_reason?: unknown };
  if (envelope.status !== 'degraded') return null;
  return typeof envelope.degraded_reason === 'string' && envelope.degraded_reason
    ? envelope.degraded_reason
    : CONVERSATION_DEGRADED_FALLBACK.reason;
}

/** The user-facing sentence and remedy for one degraded reason.
 *
 * An unknown reason falls back to a plain sentence rather than printing the
 * wire code: a new server reason must degrade to something a reader can act on,
 * not to an identifier.
 */
export function conversationDegradedNotice(reason: string): ConversationDegradedNotice {
  return CONVERSATION_DEGRADED_MESSAGES[reason] ?? {
    ...CONVERSATION_DEGRADED_FALLBACK,
    reason: reason || CONVERSATION_DEGRADED_FALLBACK.reason,
  };
}

// Browse cursors are raw opaque conversation keys. Search cursors are the
// S7 external, unpadded-base64url form; neither is decoded by the client.
export type ConversationBrowseCursor = string;
export type ConversationSearchCursor = string;

export interface ConversationStatusEnvelope<S extends ConversationTransportStatus = ConversationTransportStatus> {
  status: S;
  conversation_key?: string;
  source?: ConversationSource;
}

export type QualifiedEntityEnvelope<T extends object> =
  | ({ status: 'ok'; conversation_key: string } & T)
  | { status: 'normalization_pending'; conversation_key: string }
  | { status: 'not_found'; conversation_key: string };

export type QualifiedPayloadEnvelope<T extends object> =
  | QualifiedEntityEnvelope<T>
  | { status: 'gone'; conversation_key: string };

export type ConversationCapabilityEnvelope = {
  status: 'capability_unsupported';
  source: ConversationSource;
};

export interface QualifiedProjectFacet {
  project_key: string;
  project_label: string | null;
  count: number;
}

export interface QualifiedModelFacet {
  model: string;
  count: number;
}

export interface QualifiedConversationFacets {
  projects: QualifiedProjectFacet[];
  models: QualifiedModelFacet[];
}

export interface QualifiedBrowseRow {
  conversation_key: string;
  title: string | null;
  project_key: string | null;
  project_label: string | null;
  started_utc: string | null;
  last_activity_utc: string | null;
  count: number;
  cost_usd: number;
  models: string[];
  parent: { conversation_key: string; title: string | null } | null;
  is_fork: boolean;
}

export type QualifiedBrowseEnvelope =
  | {
      status: 'ok';
      rows: QualifiedBrowseRow[];
      selected?: QualifiedBrowseRow;
      facets: QualifiedConversationFacets;
      page: { total: number; returned: number; cursor?: ConversationBrowseCursor | null };
    }
  | {
      status: 'normalization_pending';
      rows: [];
      facets: QualifiedConversationFacets;
      page: { total: 0 };
    };

export type QualifiedFacetsEnvelope =
  | { status: 'ok'; facets: QualifiedConversationFacets; filter_degraded?: boolean }
  | { status: 'normalization_pending'; facets: QualifiedConversationFacets; filter_degraded?: boolean };

export interface QualifiedSearchHit {
  conversation_key: string;
  item_key: string | null;
  title: string | null;
  snippet: string;
  badges: string[];
  last_activity_utc: string | null;
  project_label: string | null;
}

export type QualifiedSearchEnvelope =
  | {
      status: 'ok';
      query: string;
      hits: QualifiedSearchHit[];
      total: number;
      mode: 'fts' | 'like';
      depth: 'full' | 'prose-only';
      page?: { returned: number; cursor?: ConversationSearchCursor | null };
    }
  | {
      status: 'normalization_pending';
      query: string;
      hits: [];
      total: 0;
      mode: 'fts' | 'like';
      depth: 'full' | 'prose-only';
    };

export interface QualifiedBrowseOptions {
  accountKey?: string;
  projectKey?: string;
  model?: string;
  limit?: number;
  cursor?: ConversationBrowseCursor;
  selected?: string;
}

export interface QualifiedSearchOptions {
  accountKey?: string;
  query: string;
  kind?: 'all' | 'prompts' | 'assistant' | 'tools' | 'thinking' | 'title' | 'files';
  limit?: number;
  cursor?: ConversationSearchCursor;
}

function append(params: URLSearchParams, key: string, value: string | number | undefined): void {
  if (value !== undefined) params.append(key, String(value));
}

export function qualifiedBrowseUrl(
  source: ConversationSource,
  options: QualifiedBrowseOptions = {},
): string {
  const params = new URLSearchParams();
  params.append('source', source);
  append(params, 'account', options.accountKey);
  append(params, 'project_key', options.projectKey);
  append(params, 'model', options.model);
  append(params, 'limit', options.limit);
  append(params, 'cursor', options.cursor);
  append(params, 'selected', options.selected);
  return `/api/conversations?${params.toString()}`;
}

export function qualifiedFacetsUrl(source: ConversationSource, accountKey?: string): string {
  const params = new URLSearchParams({ source });
  append(params, 'account', accountKey);
  return `/api/conversations/facets?${params.toString()}`;
}

export function qualifiedSearchUrl(
  source: ConversationSource,
  options: QualifiedSearchOptions,
): string {
  const params = new URLSearchParams();
  params.append('source', source);
  append(params, 'account', options.accountKey);
  params.append('q', options.query);
  append(params, 'kind', options.kind);
  append(params, 'limit', options.limit);
  append(params, 'cursor', options.cursor);
  return `/api/conversation/search?${params.toString().replace(/\+/g, '%20')}`;
}

export type ConversationEntityOperation =
  | 'detail'
  | 'outline'
  | 'find'
  | 'prompts'
  | 'payload'
  | 'export'
  | 'anon-map'
  | 'events'
  | 'media';

const ENTITY_SUFFIX: Record<ConversationEntityOperation, string> = {
  detail: '',
  outline: '/outline',
  find: '/find',
  prompts: '/prompts',
  payload: '/payload',
  export: '/export',
  'anon-map': '/anon-map',
  events: '/events',
  media: '/media',
};

export function conversationEntityUrl(
  rawRef: ConversationRef,
  operation: ConversationEntityOperation,
  params?: Record<string, string | number | boolean | undefined>,
): string {
  const ref = normalizeConversationRef(rawRef);
  if (ref.source === 'codex' && !ref.key.startsWith('v1.')) {
    throw new Error('Codex conversation keys must be qualified');
  }
  const query = new URLSearchParams();
  append(query, 'account', ref.account_key);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined) query.append(key, typeof value === 'boolean' ? (value ? '1' : '0') : String(value));
  }
  // URLSearchParams spells spaces as '+'. Preserve the existing conversation
  // route byte shape (`encodeURIComponent` => `%20`) while centralizing it.
  const suffix = query.size ? `?${query.toString().replace(/\+/g, '%20')}` : '';
  return `/api/conversation/${encodeURIComponent(ref.key)}${ENTITY_SUFFIX[operation]}${suffix}`;
}

export function conversationRequestKey(
  ref: ConversationRef,
  operation: string,
  params?: Record<string, string>,
): string {
  const normalizedParams = Object.entries(params ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return JSON.stringify([conversationRefKey(ref), operation, normalizedParams]);
}
