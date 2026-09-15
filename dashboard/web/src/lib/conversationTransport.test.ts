import { describe, expect, it } from 'vitest';

const modulePath = './conversationTransport';

async function loadTransport(): Promise<Record<string, unknown> | null> {
  try {
    return await import(/* @vite-ignore */ modulePath) as Record<string, unknown>;
  } catch {
    return null;
  }
}

describe('conversation transport adapters', () => {
  it('builds strict qualified collection URLs with raw browse cursors', async () => {
    const transport = await loadTransport();
    expect(transport).not.toBeNull();
    const qualifiedBrowseUrl = transport?.qualifiedBrowseUrl as undefined | ((
      source: 'claude' | 'codex',
      options: { limit: number; cursor: string },
    ) => string);
    expect(qualifiedBrowseUrl?.('codex', { limit: 50, cursor: 'v1.root-a' })).toBe(
      '/api/conversations?source=codex&limit=50&cursor=v1.root-a',
    );

    const qualifiedFacetsUrl = transport?.qualifiedFacetsUrl as undefined | ((source: 'claude' | 'codex') => string);
    expect(qualifiedFacetsUrl?.('claude')).toBe('/api/conversations/facets?source=claude');

    const qualifiedSearchUrl = transport?.qualifiedSearchUrl as undefined | ((
      source: 'claude' | 'codex',
      options: { query: string; kind: string; cursor: string },
    ) => string);
    expect(qualifiedSearchUrl?.('codex', { query: 'same id', kind: 'tools', cursor: 'ZXh0ZXJuYWw' })).toBe(
      '/api/conversation/search?source=codex&q=same%20id&kind=tools&cursor=ZXh0ZXJuYWw',
    );
  });

  it('never silently downgrades a Codex entity to a legacy Claude route', async () => {
    const transport = await loadTransport();
    expect(transport).not.toBeNull();
    const entityUrl = transport?.conversationEntityUrl as undefined | ((
      ref: { source: 'claude' | 'codex'; key: string },
      operation: 'detail',
    ) => string);
    expect(() => entityUrl?.({ source: 'codex', key: 'shared-native-id' }, 'detail')).toThrow(
      'Codex conversation keys must be qualified',
    );
    expect(entityUrl?.({ source: 'claude', key: 'legacy-session' }, 'detail')).toBe(
      '/api/conversation/legacy-session',
    );
  });

  it('keeps request-cache keys source-qualified', async () => {
    const transport = await loadTransport();
    expect(transport).not.toBeNull();
    const requestKey = transport?.conversationRequestKey as undefined | ((
      ref: { source: 'claude' | 'codex'; key: string },
      operation: string,
      params?: Record<string, string>,
    ) => string);
    const claude = requestKey?.({ source: 'claude', key: 'same' }, 'detail');
    const codex = requestKey?.({ source: 'codex', key: 'same' }, 'detail');
    expect(claude).not.toBe(codex);
  });

  // #769 S6 / #802 browser QA P1 — every read route can answer HTTP 200 with a
  // typed degraded envelope, and the client used to parse it as an ordinary
  // page. `conversations`/`rows` and `page` are all absent from that body, so
  // the parse threw and the rail printed a generic load failure beside a Retry
  // button. These two helpers are what the hooks read instead.
  it('reads the degraded reason off a conversation envelope', async () => {
    const transport = await loadTransport();
    const reasonOf = transport?.conversationDegradedReason as (body: unknown) => string | null;
    expect(reasonOf({
      conversations: [], total: 0, status: 'degraded',
      degraded_reason: 'legacy_bridge_pending',
    })).toBe('legacy_bridge_pending');
    expect(reasonOf({ status: 'ok', rows: [], facets: { projects: [], models: [] }, page: { total: 0 } })).toBeNull();
    expect(reasonOf({ status: 'normalization_pending', rows: [] })).toBeNull();
    expect(reasonOf(null)).toBeNull();
    // A degraded envelope with no reason still degrades rather than parsing.
    expect(reasonOf({ status: 'degraded' })).toBe('unavailable');
  });

  it('names the legacy bridge state and the command that clears it', async () => {
    const transport = await loadTransport();
    const notice = transport?.conversationDegradedNotice as (
      reason: string,
    ) => { reason: string; message: string; retryable: boolean };
    const bridge = notice('legacy_bridge_pending');
    expect(bridge.message).toContain('cctally cache-sync');
    // Retry re-issues the same read against the same suppressed derivation, so
    // it can never clear this state. Offering it is a dead-end remedy.
    expect(bridge.retryable).toBe(false);
    // Maintenance ends on its own, so a re-read is a real remedy there.
    expect(notice('maintenance').retryable).toBe(true);
    expect(notice('schema_behind').message).toContain('cctally cache-sync');
    expect(notice('schema_ahead').message).toContain('newer version');
    // An unknown reason must still produce a sentence rather than the raw code.
    expect(notice('something_new').message).not.toContain('something_new');
  });

  it('carries account scope through collection and entity URLs', async () => {
    const transport = await loadTransport();
    const browse = transport?.qualifiedBrowseUrl as (source: 'codex', options: Record<string, unknown>) => string;
    const entity = transport?.conversationEntityUrl as (
      ref: { source: 'codex'; key: string; account_key: string }, operation: 'outline',
    ) => string;
    expect(browse('codex', { accountKey: 'account-a', limit: 50 })).toBe(
      '/api/conversations?source=codex&account=account-a&limit=50',
    );
    expect(entity(
      { source: 'codex', key: 'v1.root-a', account_key: 'account-a' }, 'outline',
    )).toBe('/api/conversation/v1.root-a/outline?account=account-a');
  });
});
