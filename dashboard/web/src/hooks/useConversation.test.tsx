import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversation } from './useConversation';

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick signal
// the live-tail effect keys on) deterministically between renders. Mirrors the
// useConversations test setup.
let mockGeneratedAt = 't0';
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt }),
}));

function detail(items: unknown[], next_after: number | null, over: Record<string, unknown> = {}) {
  return {
    session_id: 's', project_label: 'p', git_branch: null,
    started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 3, models: ['opus'], items, page: { next_after, has_more: next_after != null },
    ...over,
  };
}
const it1 = { kind: 'human', anchor: { session_id: 's', uuid: 'u1', id: 1 }, member_uuids: ['u1'], ts: 't', text: 'hi', blocks: [], is_sidechain: false };
const it2 = { kind: 'assistant', anchor: { session_id: 's', uuid: 'u2', id: 2 }, member_uuids: ['u2', 'u2b'], ts: 't', text: 'yo', blocks: [], model: 'opus', is_sidechain: false, cost_usd: 1 };
const it3 = { kind: 'assistant', anchor: { session_id: 's', uuid: 'u3', id: 3 }, member_uuids: ['u3'], ts: 't', text: 'live', blocks: [], model: 'opus', is_sidechain: false, cost_usd: 1 };
const it4 = { kind: 'human', anchor: { session_id: 's', uuid: 'u4', id: 4 }, member_uuids: ['u4'], ts: 't', text: 'more', blocks: [], is_sidechain: false };

function mockOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: status < 400, status, json: async () => body } as Response);
}
beforeEach(() => { globalThis.fetch = vi.fn(); mockGeneratedAt = 't0'; });
afterEach(() => vi.restoreAllMocks());

// Bump generated_at + re-render to simulate one SSE tick reaching the hook.
function bumpTick(rerender: () => void, tag: string) {
  mockGeneratedAt = tag;
  rerender();
}

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

  it('tail-polls after a generated_at tick once fully paged, appending new turns + fresh header (#175 F4)', async () => {
    // Page 1: two items, next_after null -> fully paged (hasMore false).
    mockOnce(detail([it1, it2], null, { cost_usd: 1 }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.hasMore).toBe(false);

    // Next tick: the tail returns one new turn + a fresh whole-session cost.
    // The tail response stays fully-paged (next_after null), so a single fetch
    // drains it.
    mockOnce(detail([it3], null, { cost_usd: 2 }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));

    // The poll keyed off the LAST loaded item's anchor id (it2 -> id 2).
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0]).toContain('after=2');
    // Header totals refreshed from the tail response.
    expect(result.current.detail?.cost_usd).toBe(2);
    // Still fully paged after a live append.
    expect(result.current.hasMore).toBe(false);
  });

  it('drains a >PAGE tail burst within one tick (#175 F4)', async () => {
    // Page 1: fully paged.
    mockOnce(detail([it1], null));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));

    // One tick, two tail pages: the first carries a non-null next_after, so the
    // bounded drain loop fetches again in the SAME tick; the second exhausts it.
    mockOnce(detail([it3], 3));            // tail page A: more to come (next_after=3)
    mockOnce(detail([it4], null));         // tail page B: exhausted
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));

    // Two tail fetches in one tick: after=<it1.id=1> then after=<it3.id=3>.
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[calls.length - 2][0]).toContain('after=1');
    expect(calls[calls.length - 1][0]).toContain('after=3');
    // The stored cursor stays null while live-tailing (still fully paged).
    expect(result.current.hasMore).toBe(false);
  });

  it('does not tail-poll while hasMore is true (#175 F4)', async () => {
    mockOnce(detail([it1], 2));            // page 1 has a cursor -> hasMore true
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    // The tick fired but the tail poll is suppressed while still paginating.
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('refreshes the whole-session header even when the tail response is empty (#175 F4)', async () => {
    mockOnce(detail([it1, it2], null, { cost_usd: 1, models: ['opus'] }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    // Empty tail (no new turns) but fresh header totals (e.g. a re-priced turn).
    mockOnce(detail([], null, { cost_usd: 5, models: ['opus', 'sonnet'] }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.cost_usd).toBe(5));

    // No item growth, header merged.
    expect(result.current.detail?.items).toHaveLength(2);
    expect(result.current.detail?.models).toEqual(['opus', 'sonnet']);
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

  it('drops a stale cross-session page that resolves after the session changed', async () => {
    // REGRESSION (cross-session clobber guard, useConversation.ts ~L76):
    //   if (sessionRef.current !== sid) return false;
    // Scenario: s1 page-1 loads (has a cursor). A loadMore() kicks off s1's
    // page-2 fetch but we hold its resolver. We then switch the hook to s2,
    // let s2's page-1 resolve, and ONLY THEN resolve s1's slow page-2. The
    // late s1 page must NOT be appended onto s2's detail — the guard drops it.
    //
    // We drive fetch with per-URL deferred resolvers so the s1 page-2 promise
    // stays pending across the session swap, deterministically.
    const deferred: Record<string, { resolve: (body: unknown) => void }> = {};
    const sN1 = { ...it1, anchor: { ...it1.anchor, session_id: 's2' }, member_uuids: ['s2-u1'] };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      // Page-1 loads (no `after=`) resolve immediately; the s1 page-2 load
      // (`after=`) is deferred so we control exactly when it lands.
      if (url.includes('/api/conversation/s1') && !url.includes('after=')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1], 2) } as Response);
      }
      if (url.includes('/api/conversation/s2')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([sN1], null) } as Response);
      }
      // s1 page-2 (the slow one) — return a promise we resolve by hand.
      return new Promise((resolve) => {
        deferred[url] = { resolve: (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response) };
      });
    });

    const { result, rerender } = renderHook(({ sid }) => useConversation(sid), { initialProps: { sid: 's1' } });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    expect(result.current.detail?.items[0].anchor.session_id).toBe('s');

    // Kick off s1's page-2 (deferred — promise registered, not yet resolved).
    let morePromise!: Promise<void>;
    act(() => { morePromise = result.current.loadMore(); });
    await waitFor(() => expect(Object.keys(deferred).some((u) => u.includes('after='))).toBe(true));

    // Switch session to s2 while s1 page-2 is still in flight; let s2 page-1 land.
    rerender({ sid: 's2' });
    await waitFor(() => expect(result.current.detail?.items[0]?.anchor.session_id).toBe('s2'));
    expect(result.current.detail?.items).toHaveLength(1);

    // NOW resolve the stale s1 page-2. Without the guard this would append
    // s1's items onto s2's detail; with it, the page is dropped.
    const staleUrl = Object.keys(deferred).find((u) => u.includes('after='))!;
    await act(async () => { deferred[staleUrl].resolve(detail([it2], null)); await morePromise; });

    // s2's detail is untouched: still exactly s2's page-1, no s1 items appended.
    expect(result.current.detail?.items).toHaveLength(1);
    expect(result.current.detail?.items.map((i) => i.anchor.session_id)).toEqual(['s2']);
  });
});
