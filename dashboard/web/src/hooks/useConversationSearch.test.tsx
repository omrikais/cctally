import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversationSearch } from './useConversationSearch';
import type { SearchKind } from '../types/conversation';

const result1 = {
  query: 'flock', mode: 'fts',
  hits: [{ session_id: 'a', uuid: 'u1', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: 'the [flock] x', cost_usd: 0.1 }],
  total: 1,
};

function mockFetchOnce(body: unknown) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: true, status: 200, json: async () => body } as Response);
}

beforeEach(() => { globalThis.fetch = vi.fn(); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('useConversationSearch', () => {
  it('does not fetch for an empty query', () => {
    const { result } = renderHook(() => useConversationSearch(''));
    act(() => { vi.advanceTimersByTime(500); });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.hits).toEqual([]);
  });

  it('debounces then fetches, exposing hits + mode + total', async () => {
    mockFetchOnce(result1);
    const { result } = renderHook(() => useConversationSearch('flock'));
    expect(globalThis.fetch).not.toHaveBeenCalled();        // pre-debounce
    // Step over the debounce, then flush the resolved-fetch microtasks.
    // (waitFor would deadlock under fake timers — see PreviewPane.test.tsx
    // for this repo's fake-timer flush idiom.)
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.hits).toHaveLength(1);
    expect(result.current.mode).toBe('fts');
    expect(result.current.total).toBe(1);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversation/search?q=flock');
  });

  it('a newer needle wins over an older in-flight response (abort path)', async () => {
    // The first needle's debounce fires and its fetch is in flight when the
    // needle changes; the effect cleanup aborts it (AbortError, swallowed),
    // so the older response must NOT commit over the newer needle's hits.
    const oldResult = {
      query: 'old', mode: 'fts',
      hits: [{ session_id: 'a', uuid: 'old', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: 'old', cost_usd: 0.1 }],
      total: 1,
    };
    const newResult = {
      query: 'new', mode: 'like',
      hits: [{ session_id: 'b', uuid: 'new', project_label: 'q', ts: '2026-01-02T00:00:00Z', snippet: 'new', cost_usd: 0.2 }],
      total: 1,
    };
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    // First (old) fetch never resolves until we let it — it'll be aborted.
    let resolveOld: (v: unknown) => void = () => {};
    fetchMock.mockImplementationOnce((_url: string, opts: { signal: AbortSignal }) =>
      new Promise((res, rej) => {
        opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        resolveOld = res;
      }),
    );
    mockFetchOnce(newResult);

    const { result, rerender } = renderHook(({ q }) => useConversationSearch(q), { initialProps: { q: 'old' } });
    await act(async () => { vi.advanceTimersByTime(250); });   // fire old debounce → old fetch in flight
    rerender({ q: 'new' });                                    // needle changes → cleanup aborts old fetch
    await act(async () => { vi.advanceTimersByTime(250); });   // fire new debounce → new fetch
    await act(async () => { resolveOld({ ok: true, status: 200, json: async () => oldResult }); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(result.current.mode).toBe('like');                 // newer needle won
    expect(result.current.hits[0].uuid).toBe('new');
  });

  it('aborts the in-flight fetch the instant the needle changes — a late prior-needle response cannot commit (ctlRef)', async () => {
    // 'old' fetch is in flight when the needle advances to 'older'; effect (1)'s
    // cleanup aborts 'old' IMMEDIATELY. Resolving the (now-aborted) 'old'
    // response DURING the debounce window (before 'older' settles) must be
    // ignored. Without the ctlRef abort, 'old' would still be in flight and its
    // response would commit stale hits here.
    const oldResult = {
      query: 'old', mode: 'fts',
      hits: [{ session_id: 'a', uuid: 'old', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: 'old', cost_usd: 0.1 }],
      total: 1,
    };
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    let resolveOld: (v: unknown) => void = () => {};
    fetchMock.mockImplementationOnce((_url: string, opts: { signal: AbortSignal }) =>
      new Promise((res, rej) => {
        opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        resolveOld = res;
      }),
    );

    const { result, rerender } = renderHook(({ q }) => useConversationSearch(q), { initialProps: { q: 'old' } });
    await act(async () => { vi.advanceTimersByTime(250); });   // 'old' debounce fires -> 'old' fetch in flight
    rerender({ q: 'older' });                                  // raw-q change -> effect (1) cleanup aborts 'old' now
    // Resolve 'old' DURING the window (before 'older' settles): must be ignored.
    await act(async () => {
      resolveOld({ ok: true, status: 200, json: async () => oldResult });
      await Promise.resolve(); await Promise.resolve();
    });

    expect(result.current.hits).toEqual([]);                   // stale 'old' never committed
    expect(result.current.mode).toBeNull();
  });

  // ---- #177 S6: kind facet + load-more pagination ----

  it('threads the kind facet into the fetch URL', async () => {
    mockFetchOnce({ ...result1, kind: 'tools', search_depth: 'full' });
    renderHook(() => useConversationSearch('npm', 'tools'));
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain('q=npm');
    expect(url).toContain('offset=0');
    expect(url).toContain('kind=tools');
  });

  it('exposes searchDepth from the response', async () => {
    mockFetchOnce({ ...result1, kind: 'all', search_depth: 'prose-only' });
    const { result } = renderHook(() => useConversationSearch('flock', 'all'));
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.searchDepth).toBe('prose-only');
  });

  it('a kind change aborts the in-flight fetch and refetches from offset 0, REPLACING hits', async () => {
    const allResult = {
      query: 'npm', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'a', uuid: 'A', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: 'a', cost_usd: 0.1 }],
      total: 5,
    };
    const toolsResult = {
      query: 'npm', mode: 'fts', kind: 'tools', search_depth: 'full',
      hits: [{ session_id: 'b', uuid: 'B', project_label: 'q', ts: '2026-01-02T00:00:00Z', snippet: 'b', cost_usd: 0.2 }],
      total: 1,
    };
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    let resolveAll: (v: unknown) => void = () => {};
    fetchMock.mockImplementationOnce((_url: string, opts: { signal: AbortSignal }) =>
      new Promise((res, rej) => {
        opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        resolveAll = res;
      }),
    );
    mockFetchOnce(toolsResult);

    const { result, rerender } = renderHook(
      ({ k }: { k: SearchKind }) => useConversationSearch('npm', k),
      { initialProps: { k: 'all' as SearchKind } },
    );
    await act(async () => { vi.advanceTimersByTime(250); });   // fire 'all' fetch (in flight)
    rerender({ k: 'tools' as const });                         // kind change → aborts, refetch
    await act(async () => { vi.advanceTimersByTime(250); });   // fire 'tools' fetch
    await act(async () => { resolveAll({ ok: true, status: 200, json: async () => allResult }); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // The stale 'all' response must NOT commit over the 'tools' hits.
    expect(result.current.hits.map((h) => h.uuid)).toEqual(['B']);
    expect(result.current.total).toBe(1);
    const lastUrl = fetchMock.mock.calls[fetchMock.mock.calls.length - 1][0] as string;
    expect(lastUrl).toContain('kind=tools');
    expect(lastUrl).toContain('offset=0');
  });

  it('loadMore fetches offset=hits.length and APPENDS', async () => {
    const page1 = {
      query: 'x', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'a', uuid: 'p1', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: '1', cost_usd: 0.1 }],
      total: 3,
    };
    const page2 = {
      query: 'x', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'a', uuid: 'p2', project_label: 'p', ts: '2026-01-02T00:00:00Z', snippet: '2', cost_usd: 0.1 }],
      total: 3,
    };
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversationSearch('x', 'all'));
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.hits.map((h) => h.uuid)).toEqual(['p1']);

    mockFetchOnce(page2);
    await act(async () => { result.current.loadMore(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(result.current.hits.map((h) => h.uuid)).toEqual(['p1', 'p2']);  // appended
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    const moreUrl = fetchMock.mock.calls[1][0] as string;
    expect(moreUrl).toContain('offset=1');   // offset = prior hits.length
  });

  it('discards a stale loadMore append when the needle changes mid-flight', async () => {
    const page1 = {
      query: 'x', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'a', uuid: 'p1', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: '1', cost_usd: 0.1 }],
      total: 3,
    };
    const stalePage2 = {
      query: 'x', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'a', uuid: 'STALE', project_label: 'p', ts: '2026-01-02T00:00:00Z', snippet: '2', cost_usd: 0.1 }],
      total: 3,
    };
    const fresh = {
      query: 'y', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'b', uuid: 'FRESH', project_label: 'q', ts: '2026-01-03T00:00:00Z', snippet: 'y', cost_usd: 0.2 }],
      total: 1,
    };
    mockFetchOnce(page1);
    const { result, rerender } = renderHook(
      ({ q }) => useConversationSearch(q, 'all'),
      { initialProps: { q: 'x' } },
    );
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // loadMore in flight (never resolves until we say so) — it'll be aborted.
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    let resolveMore: (v: unknown) => void = () => {};
    fetchMock.mockImplementationOnce((_url: string, opts: { signal: AbortSignal }) =>
      new Promise((res, rej) => {
        opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        resolveMore = res;
      }),
    );
    await act(async () => { result.current.loadMore(); });   // page-2 fetch in flight
    mockFetchOnce(fresh);
    rerender({ q: 'y' });                                    // needle change → abort everything
    await act(async () => { vi.advanceTimersByTime(250); }); // fresh 'y' fetch
    await act(async () => { resolveMore({ ok: true, status: 200, json: async () => stalePage2 }); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // The stale append must be discarded; only the fresh needle's REPLACE stands.
    expect(result.current.hits.map((h) => h.uuid)).toEqual(['FRESH']);
  });

  it('loadMore is a no-op once all hits are loaded', async () => {
    mockFetchOnce({
      query: 'x', mode: 'fts', kind: 'all', search_depth: 'full',
      hits: [{ session_id: 'a', uuid: 'p1', project_label: 'p', ts: '2026-01-01T00:00:00Z', snippet: '1', cost_usd: 0.1 }],
      total: 1,   // already complete
    });
    const { result } = renderHook(() => useConversationSearch('x', 'all'));
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    const beforeCalls = fetchMock.mock.calls.length;
    await act(async () => { result.current.loadMore(); });
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock.mock.calls.length).toBe(beforeCalls);   // guarded, no fetch
  });

  it('does not get stuck loading when the needle oscillates back to a settled value within the debounce window', async () => {
    // 'a' settles + fetches (empty result) -> loading false. Then 'a' -> 'ab' -> 'a'
    // within the window: debouncedQ never leaves 'a', so no re-fetch fires and
    // loading must return to false (not stick true on the perpetual-spinner path).
    mockFetchOnce({ query: 'a', mode: 'fts', hits: [], total: 0 });
    const { result, rerender } = renderHook(({ q }) => useConversationSearch(q), { initialProps: { q: 'a' } });
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.loading).toBe(false);              // settled + idle

    rerender({ q: 'ab' });
    await act(async () => { vi.advanceTimersByTime(50); });   // within the window
    rerender({ q: 'a' });
    await act(async () => { vi.advanceTimersByTime(250); });  // debounce elapses; debouncedQ stays 'a'
    await act(async () => { await Promise.resolve(); });

    expect(result.current.loading).toBe(false);              // NOT stuck true
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);  // no redundant re-fetch
  });
});
