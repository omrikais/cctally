import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversationSearch } from './useConversationSearch';

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
});
