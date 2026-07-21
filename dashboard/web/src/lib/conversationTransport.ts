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
  | 'capability_unsupported';

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
  | { status: 'ok'; facets: QualifiedConversationFacets }
  | { status: 'normalization_pending'; facets: QualifiedConversationFacets };

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
  projectKey?: string;
  model?: string;
  limit?: number;
  cursor?: ConversationBrowseCursor;
}

export interface QualifiedSearchOptions {
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
  append(params, 'project_key', options.projectKey);
  append(params, 'model', options.model);
  append(params, 'limit', options.limit);
  append(params, 'cursor', options.cursor);
  return `/api/conversations?${params.toString()}`;
}

export function qualifiedFacetsUrl(source: ConversationSource): string {
  return `/api/conversations/facets?source=${source}`;
}

export function qualifiedSearchUrl(
  source: ConversationSource,
  options: QualifiedSearchOptions,
): string {
  const params = new URLSearchParams();
  params.append('source', source);
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
