import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversationOutline } from './useConversationOutline';

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick signal
// the refetch effect keys on) deterministically. Mirrors useConversation.test.
let mockGeneratedAt = 't0';
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt }),
}));

function outline(session_id: string, over: Record<string, unknown> = {}) {
  return {
    session_id,
    stats: {
      turns: { total: 0, human: 0, assistant: 0, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 0, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
    },
    turns: [],
    ...over,
  };
}

function mockOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: status < 400, status, json: async () => body } as Response);
}
beforeEach(() => { globalThis.fetch = vi.fn(); mockGeneratedAt = 't0'; });
afterEach(() => vi.restoreAllMocks());

function bumpTick(rerender: () => void, tag: string) {
  mockGeneratedAt = tag;
  rerender();
}

describe('useConversationOutline', () => {
  it('fetches the outline on a session id', async () => {
    mockOnce(outline('s'));
    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversation/s/outline');
    expect(result.current.error).toBeNull();
  });

  it('resets to null when the session switches', async () => {
    mockOnce(outline('s1'));
    const { result, rerender } = renderHook(({ sid }) => useConversationOutline(sid), { initialProps: { sid: 's1' as string | null } });
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s1'));

    // Defer s2's fetch so we can observe the synchronous reset to null.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise(() => {}));
    act(() => { rerender({ sid: 's2' }); });
    expect(result.current.outline).toBeNull();
  });

  it('null session id → no fetch, outline null, not loading', async () => {
    const { result } = renderHook(() => useConversationOutline(null));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.outline).toBeNull();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('refetches on a generated_at change', async () => {
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 1 } }));
    const { result, rerender } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(1));

    // Next tick: a fresh outline (cost bumped).
    mockOnce(outline('s', { stats: { ...outline('s').stats, cost_usd: 2 } }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.outline?.stats.cost_usd).toBe(2));
  });

  it('revalidateOnTick:false suppresses the tick-driven refetch (#227)', async () => {
    mockOnce(outline('s'));
    const { result, rerender } = renderHook(() => useConversationOutline('s', { revalidateOnTick: false }));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));
    // Initial load fired once.
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);

    // A generated_at bump must NOT trigger a refetch when opted out.
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1);
    expect(result.current.outline?.session_id).toBe('s');
  });

  it('coalesces a tick that lands mid-fetch into exactly one trailing refetch', async () => {
    let resolveSecond!: (body: unknown) => void;
    let fetchCount = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => {
      fetchCount += 1;
      if (fetchCount === 1) {
        // Initial open resolves immediately.
        return Promise.resolve({ ok: true, status: 200, json: async () => outline('s') } as Response);
      }
      if (fetchCount === 2) {
        // First tick-driven refetch is held pending.
        return new Promise((resolve) => {
          resolveSecond = (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => outline('s') } as Response);
    });

    const { result, rerender } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s'));

    // Tick 1: kicks off refetch #2 (held pending).
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(fetchCount).toBe(2));

    // Ticks 2 and 3 land while #2 is still in flight: coalesced, no new fetch.
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    await act(async () => { bumpTick(rerender, 't3'); await Promise.resolve(); });
    expect(fetchCount).toBe(2);

    // Resolve #2; the finally replays exactly ONE coalesced refetch (#3).
    await act(async () => { resolveSecond(outline('s')); for (let i = 0; i < 6; i++) await Promise.resolve(); });
    await waitFor(() => expect(fetchCount).toBe(3));
    expect(fetchCount).toBe(3);
  });

  it('a fetch error degrades to {outline: null, error} without throwing', async () => {
    mockOnce({}, 500);
    const { result } = renderHook(() => useConversationOutline('s'));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.outline).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('drops a stale-session response when the session switched mid-fetch', async () => {
    // s1's fetch is held pending; we switch to s2 (resolves immediately) and
    // only THEN resolve s1. The stale s1 body must not land under s2.
    let resolveS1!: (body: unknown) => void;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/api/conversation/s1/outline')) {
        return new Promise((resolve) => {
          resolveS1 = (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => outline('s2') } as Response);
    });

    const { result, rerender } = renderHook(({ sid }) => useConversationOutline(sid), { initialProps: { sid: 's1' as string | null } });
    // s1 fetch is pending; switch to s2.
    rerender({ sid: 's2' });
    await waitFor(() => expect(result.current.outline?.session_id).toBe('s2'));

    // Now resolve the stale s1 fetch — it must be dropped.
    await act(async () => { resolveS1(outline('s1')); for (let i = 0; i < 4; i++) await Promise.resolve(); });
    expect(result.current.outline?.session_id).toBe('s2');
  });
});
