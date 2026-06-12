import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversationFind } from './useConversationFind';

const result1 = {
  anchors: [
    { uuid: 'u1', match_kinds: ['tool'] },
    { uuid: 'u2', match_kinds: [] },
  ],
  total: 2,
  anchors_truncated: false,
  mode: 'fts',
  search_depth: 'full',
};

function mockFetchOnce(body: unknown) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: true, status: 200, json: async () => body } as Response);
}

beforeEach(() => { globalThis.fetch = vi.fn(); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('useConversationFind', () => {
  it('does not fetch for an empty needle', () => {
    const { result } = renderHook(() => useConversationFind('s1', ''));
    act(() => { vi.advanceTimersByTime(500); });
    expect(globalThis.fetch).not.toHaveBeenCalled();
    expect(result.current.anchors).toEqual([]);
    expect(result.current.total).toBe(0);
  });

  it('debounces then fetches, exposing anchors + total + mode + truncated', async () => {
    mockFetchOnce(result1);
    const { result } = renderHook(() => useConversationFind('s1', 'needle'));
    expect(globalThis.fetch).not.toHaveBeenCalled();          // pre-debounce
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.anchors).toHaveLength(2);
    expect(result.current.total).toBe(2);
    expect(result.current.mode).toBe('fts');
    expect(result.current.truncated).toBe(false);
  });

  it('builds the find URL from the session id + needle (encoded)', async () => {
    mockFetchOnce(result1);
    renderHook(() => useConversationFind('s 1/x', 'a b'));
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain('/api/conversation/s%201%2Fx/find');
    expect(url).toContain('q=a%20b');
  });

  it('a newer needle wins over an older in-flight response (abort path)', async () => {
    const oldResult = { anchors: [{ uuid: 'OLD', match_kinds: [] }], total: 1, anchors_truncated: false, mode: 'fts', search_depth: 'full' };
    const newResult = { anchors: [{ uuid: 'NEW', match_kinds: [] }], total: 1, anchors_truncated: false, mode: 'like', search_depth: 'full' };
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    let resolveOld: (v: unknown) => void = () => {};
    fetchMock.mockImplementationOnce((_url: string, opts: { signal: AbortSignal }) =>
      new Promise((res, rej) => {
        opts.signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        resolveOld = res;
      }),
    );
    mockFetchOnce(newResult);

    const { result, rerender } = renderHook(({ q }) => useConversationFind('s1', q), { initialProps: { q: 'old' } });
    await act(async () => { vi.advanceTimersByTime(250); });   // old fetch in flight
    rerender({ q: 'new' });                                    // needle changes → cleanup aborts old
    await act(async () => { vi.advanceTimersByTime(250); });   // new fetch
    await act(async () => { resolveOld({ ok: true, status: 200, json: async () => oldResult }); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(result.current.mode).toBe('like');                  // newer needle won
    expect(result.current.anchors[0].uuid).toBe('NEW');
  });

  it('resets to empty when the needle is cleared', async () => {
    mockFetchOnce(result1);
    const { result, rerender } = renderHook(({ q }) => useConversationFind('s1', q), { initialProps: { q: 'needle' } });
    await act(async () => { vi.advanceTimersByTime(250); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.anchors).toHaveLength(2);
    rerender({ q: '' });
    await act(async () => { vi.advanceTimersByTime(250); });
    expect(result.current.anchors).toEqual([]);
    expect(result.current.total).toBe(0);
  });
});
