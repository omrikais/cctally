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
});
