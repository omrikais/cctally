import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversation } from './useConversation';
import * as snapMod from './useSnapshot';

function detail(items: unknown[], next_after: number | null) {
  return {
    session_id: 's', project_label: 'p', git_branch: null,
    started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 3, models: ['opus'], items, page: { next_after, has_more: next_after != null },
  };
}
const it1 = { kind: 'human', anchor: { session_id: 's', uuid: 'u1', id: 1 }, member_uuids: ['u1'], ts: 't', text: 'hi', blocks: [], is_sidechain: false };
const it2 = { kind: 'assistant', anchor: { session_id: 's', uuid: 'u2', id: 2 }, member_uuids: ['u2', 'u2b'], ts: 't', text: 'yo', blocks: [], model: 'opus', is_sidechain: false, cost_usd: 1 };

function mockOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: status < 400, status, json: async () => body } as Response);
}
beforeEach(() => { globalThis.fetch = vi.fn(); });
afterEach(() => vi.restoreAllMocks());

describe('useConversation', () => {
  it('loads page 1 for a session', async () => {
    mockOnce(detail([it1], 2));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    expect(result.current.detail?.cost_usd).toBe(3);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversation/s?limit=500');
  });

  it('appends pages via the after cursor', async () => {
    mockOnce(detail([it1], 2));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    mockOnce(detail([it2], null));
    await act(async () => { await result.current.loadMore(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[1][0]).toContain('after=2');
    expect(result.current.hasMore).toBe(false);
  });

  it('never subscribes to the snapshot, and is rerender-stable (no SSE refetch)', async () => {
    // The hook deliberately does NOT live-tail: a past transcript is
    // immutable. Assert it never even subscribes to the snapshot store —
    // this fails the day someone wires in a useSnapshot()-driven refetch.
    const spy = vi.spyOn(snapMod, 'useSnapshot');
    mockOnce(detail([it1], null));
    const { rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(1));
    rerender();                            // simulate a re-render (e.g. SSE tick elsewhere)
    await Promise.resolve();
    expect(spy).not.toHaveBeenCalled();    // no snapshot subscription at all
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);  // and no refetch
  });

  it('surfaces a not-found error on 404 and leaves detail null', async () => {
    mockOnce({}, 404);
    const { result } = renderHook(() => useConversation('missing'));
    await waitFor(() => expect(result.current.error).toBe('Conversation not found.'));
    expect(result.current.detail).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('loadUntil pages until the target uuid is loaded', async () => {
    mockOnce(detail([it1], 2));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    mockOnce(detail([it2], null));         // u2b is a member of it2
    await act(async () => { await result.current.loadUntil('u2b'); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
  });

  it('loadUntil terminates at exhaustion when the target never appears', async () => {
    // Page 1 has a cursor; page 2 exhausts (next_after null). The target
    // uuid is in NEITHER page. loadUntil must resolve (not hang) and stop
    // paging once next_after goes null — never spinning the 20-iter cap.
    mockOnce(detail([it1], 2));            // page 1, more to come
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    mockOnce(detail([it2], null));         // page 2, exhausted
    await act(async () => { await result.current.loadUntil('does-not-exist'); });
    // Exactly 2 fetches total: the page-1 effect + one loadUntil page that
    // exhausted the cursor. The cap-bounded loop cannot fetch 20 times.
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(2);
  });
});
