import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversation } from './useConversation';

// Minimal EventSource mock (copied from __tests__/sse.test.ts). The live-tail
// effect opens one of these per conversation and fires pollTail() on the `tail`
// event and on `open`.
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  closed = false;
  constructor(url: string) { this.url = url; MockEventSource.instances.push(this); }
  addEventListener(name: string, fn: (ev: MessageEvent) => void): void {
    (this.listeners[name] ||= []).push(fn);
  }
  close(): void { this.closed = true; }
  emit(name: string, data: unknown = {}): void {
    (this.listeners[name] || []).forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent));
  }
}

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick signal
// the live-tail backstop effect keys on) and `transcriptsEnabled` (the gate on
// the dedicated live-tail EventSource) deterministically between renders.
// Mirrors the useConversations test setup.
let mockGeneratedAt = 't0';
let mockTranscripts = true;
let mockLiveTail = true;
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt, transcriptsEnabled: mockTranscripts }),
}));
vi.mock('../store/store', async (orig) => ({
  ...(await orig<typeof import('../store/store')>()),
  selectLiveTailEnabled: () => mockLiveTail,
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
beforeEach(() => {
  globalThis.fetch = vi.fn();
  mockGeneratedAt = 't0';
  mockTranscripts = true;
  mockLiveTail = true;
  (globalThis as unknown as { EventSource: typeof MockEventSource }).EventSource = MockEventSource;
  MockEventSource.instances = [];
});
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

  it('replays a tick that arrives mid-fetch exactly once (coalesce, #175 F4)', async () => {
    // Fully paged (hasMore false). The FIRST tail fetch is held pending via a
    // deferred resolver while two more ticks arrive. pollTail sees pollingRef
    // set on each and records pendingTickRef (coalesced — multiple ticks collapse
    // into one pending flag). When the first fetch resolves, the `finally` replays
    // exactly ONE additional `?after=` fetch — not zero, not two.
    let resolveFirst!: (body: unknown) => void;
    let tailCount = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (!url.includes('after=')) {
        // page-1 load resolves immediately (fully paged).
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1, it2], null) } as Response);
      }
      // Tail fetches. Hold the FIRST one pending; resolve later ones immediately.
      tailCount += 1;
      if (tailCount === 1) {
        return new Promise((resolve) => {
          resolveFirst = (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      return Promise.resolve({ ok: true, status: 200, json: async () => detail([], null) } as Response);
    });

    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.hasMore).toBe(false);

    // Tick 1 kicks off the first tail fetch (held pending via resolveFirst).
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(tailCount).toBe(1));

    // Ticks 2 and 3 arrive while the first fetch is still pending. pollTail sees
    // pollingRef set both times and only records pendingTickRef — no new fetch yet.
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    await act(async () => { bumpTick(rerender, 't3'); await Promise.resolve(); });
    expect(tailCount).toBe(1); // still only the in-flight fetch

    // Resolve the first fetch; the `finally` replays the single coalesced tick.
    await act(async () => { resolveFirst(detail([it3], null)); for (let i = 0; i < 6; i++) await Promise.resolve(); });
    await waitFor(() => expect(tailCount).toBe(2));

    // Exactly ONE replay fetch — the two coalesced ticks did NOT each spawn one.
    expect(tailCount).toBe(2);
    // The replay's after= cursor advanced to the newly-appended it3 (id 3), since
    // the first fetch's append landed before the replay reads the last item.
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => String(c[0]).includes('after='));
    expect(calls[calls.length - 1][0]).toContain('after=3');
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

  it('#193: live tail updates a rewritten title (P1-4)', async () => {
    // Page 1 carries the initial ai-title.
    mockOnce(detail([it1, it2], null, { title: 'Old' }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.detail?.title).toBe('Old');

    // Empty tail (no new turns) but the ai-title was rewritten mid-session. The
    // merge must propagate body.title so an OPEN reader's header re-titles.
    mockOnce(detail([], null, { title: 'New' }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.title).toBe('New'));
    expect(result.current.detail?.items).toHaveLength(2);
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

  it('never exposes the previous session\'s detail under the new sid on switch (#183 stale-media 404)', async () => {
    // REGRESSION (cross-session stale-media 404, useConversation.ts derive guard):
    // The fetch effect clears `detail` only in the POST-commit passive phase, so
    // the render right after `sessionId` changes used to return the previous
    // session's `detail` for one commit — while TranscriptContext already carried
    // the new sid, so an auto-fetching MediaFigure built /<newSid>/media?tool_use_id
    // =<oldId> → 404. A setState-in-render reset does NOT fix it (the first render
    // of the new session still returns the stale value); the fix DERIVES the
    // exposed detail in the same render pass (only surface it when the
    // REQUESTED loadedSessionId === sessionId — keyed on the requested sid,
    // not the body's self-reported session_id).
    //
    // We trace EVERY render the hook produces and assert no render that requested
    // s2 ever carried a detail belonging to s1. The new session's fetch is
    // deferred (never resolves) so ONLY a stale-detail leak — not fresh data —
    // could populate detail under s2. RED without the guard: a render with
    // {sid:'s2', detail:<s1>} appears.
    const renders: Array<{ sid: string; detailSid: string | null }> = [];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/api/conversation/s1')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1], null) } as Response);
      }
      return new Promise(() => {}); // s2 page-1 never resolves
    });
    const { result, rerender } = renderHook(
      ({ sid }) => {
        const r = useConversation(sid);
        renders.push({ sid, detailSid: r.detail?.session_id ?? null });
        return r;
      },
      { initialProps: { sid: 's1' } },
    );
    await waitFor(() => expect(result.current.detail?.session_id).toBe('s'));

    // Switch to s2. Across every render the hook produces for s2, detail must be
    // null (loading) — never s1's detail. The fix's derive guard makes this hold
    // in the SAME render pass, not one commit later.
    act(() => { rerender({ sid: 's2' }); });
    const s2Renders = renders.filter((r) => r.sid === 's2');
    expect(s2Renders.length).toBeGreaterThan(0);
    expect(s2Renders.every((r) => r.detailSid === null)).toBe(true);
    expect(result.current.detail).toBeNull();
    expect(result.current.loading).toBe(true);
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

// live-tail spec §3.1 — the dedicated per-conversation EventSource. Opens
// `/api/conversation/<id>/events`, fires the EXISTING pollTail() on a `tail`
// ping (and on `open`, for (re)connect catch-up), gated on transcriptsEnabled
// + selectLiveTailEnabled. The slow generated_at backstop above stays
// untouched.
describe('useConversation live-tail EventSource', () => {
  it('opens a per-conversation EventSource and pollTails on a tail ping', async () => {
    mockOnce(detail([it1, it2], null));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    const es = MockEventSource.instances.find((e) => e.url.includes('/api/conversation/s/events'));
    expect(es).toBeTruthy();
    // A tail ping triggers pollTail(), which fetches `?after=<lastId>` and
    // appends the new turn.
    mockOnce(detail([it3], null));
    await act(async () => { es!.emit('tail', { sessionId: 's' }); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0]).toContain('after=2');
  });

  it('closes the EventSource when the session changes', async () => {
    mockOnce(detail([], null));
    const { rerender } = renderHook(({ id }) => useConversation(id), { initialProps: { id: 's' } });
    await waitFor(() =>
      expect(MockEventSource.instances.some((e) => e.url.includes('/api/conversation/s/events'))).toBe(true),
    );
    const first = MockEventSource.instances.find((e) => e.url.includes('/api/conversation/s/events'))!;
    mockOnce(detail([], null));
    rerender({ id: 's2' });
    expect(first.closed).toBe(true);
    await waitFor(() =>
      expect(MockEventSource.instances.some((e) => e.url.includes('/api/conversation/s2/events'))).toBe(true),
    );
  });

  it('does NOT open an EventSource when live_tail is off', async () => {
    mockLiveTail = false;
    mockOnce(detail([], null));
    renderHook(() => useConversation('s'));
    await waitFor(() =>
      expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true),
    );
  });

  it('does NOT open an EventSource when transcripts are disabled', async () => {
    mockTranscripts = false;
    mockOnce(detail([], null));
    renderHook(() => useConversation('s'));
    await waitFor(() =>
      expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true),
    );
  });
});
