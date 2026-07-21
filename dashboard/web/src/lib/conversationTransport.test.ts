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
});
