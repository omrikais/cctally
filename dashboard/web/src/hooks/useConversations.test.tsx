import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversations } from './useConversations';
import { _resetForTests, dispatch, getState } from '../store/store';
import { clearRailPrefs } from '../store/conversationRailPrefs';

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick
// heartbeat) and `data_version` (the #305 change signal the first-page effect
// now keys on) deterministically between renders. `mockDataVersion` defaults to
// `undefined` so `revalToken` falls back to `generated_at` — keeping the
// pre-#305 tick tests on the every-tick path.
let mockGeneratedAt = 't0';
let mockDataVersion: string | undefined;
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt, data_version: mockDataVersion }),
}));

const page1 = {
  conversations: [{ session_id: 'a', project_label: 'p', git_branch: null, started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T01:00:00Z', msg_count: 3, cost_usd: 1.5, models: ['opus'] }],
  page: { next_offset: 1, has_more: true },
};
const page2 = {
  conversations: [{ session_id: 'b', project_label: 'q', git_branch: 'main', started_utc: '2026-01-02T00:00:00Z', last_activity_utc: '2026-01-02T01:00:00Z', msg_count: 5, cost_usd: 2.0, models: ['sonnet'] }],
  page: { next_offset: null, has_more: false },
};

function mockFetchOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400, status, json: async () => body,
  } as Response);
}

beforeEach(() => { globalThis.fetch = vi.fn(); mockGeneratedAt = 't0'; mockDataVersion = undefined; clearRailPrefs(); _resetForTests(); });
afterEach(() => { vi.restoreAllMocks(); clearRailPrefs(); _resetForTests(); });

// A full first page (exactly PAGE=50 rows) with more to come. Generated
// programmatically so the accumulated tail is unambiguously > PAGE.
function fullPage(prefix: string, nextOffset: number | null) {
  return {
    conversations: Array.from({ length: 50 }, (_, i) => ({
      session_id: `${prefix}-${i}`, project_label: 'p', git_branch: null,
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T01:00:00Z',
      msg_count: 1, cost_usd: 1, models: ['opus'],
    })),
    page: { next_offset: nextOffset, has_more: nextOffset != null },
  };
}

describe('useConversations', () => {
  it('uses the qualified Codex browse contract and preserves the opaque key', async () => {
    mockFetchOnce({
      status: 'ok',
      rows: [{ conversation_key: 'v1.root-a', title: 'Codex thread', project_key: 'p1', project_label: 'proj', started_utc: '2026-07-01T00:00:00Z', last_activity_utc: '2026-07-01T01:00:00Z', count: 2, cost_usd: 0.25, models: ['gpt-5.6-codex'], parent: null, is_fork: false }],
      facets: { projects: [], models: [] },
      page: { total: 1, returned: 1, cursor: null },
    });
    const { result } = renderHook(() => useConversations('codex'));
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect(result.current.rows[0].conversation_ref).toEqual({ source: 'codex', key: 'v1.root-a' });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversations?source=codex');
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).not.toContain('offset=');
  });

  it('loads the first page on mount', async () => {
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect(result.current.rows[0].session_id).toBe('a');
    expect(result.current.hasMore).toBe(true);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversations?sort=recent&limit=50&offset=0');
  });

  it('feeds the shared title cache as rows land (#227)', async () => {
    const titled = {
      conversations: [
        { session_id: 'a', title: 'Refactor the store', project_label: 'p', git_branch: null, started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T01:00:00Z', msg_count: 3, cost_usd: 1.5, models: ['opus'] },
        { session_id: 'b', title: 'Fix the dashboard', project_label: 'q', git_branch: null, started_utc: '2026-01-02T00:00:00Z', last_activity_utc: '2026-01-02T01:00:00Z', msg_count: 5, cost_usd: 2.0, models: ['sonnet'] },
      ],
      page: { next_offset: null, has_more: false },
    };
    mockFetchOnce(titled);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(2));
    await waitFor(() => expect(getState().conversationTitles).toEqual({
      '["claude","a"]': 'Refactor the store',
      '["claude","b"]': 'Fix the dashboard',
    }));
  });

  it('appends the next page via loadMore (offset cursor)', async () => {
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    mockFetchOnce(page2);
    await act(async () => { await result.current.loadMore(); });
    await waitFor(() => expect(result.current.rows).toHaveLength(2));
    expect(result.current.rows.map((r) => r.session_id)).toEqual(['a', 'b']);
    expect(result.current.hasMore).toBe(false);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[1][0]).toContain('offset=1');
  });

  it('does not clobber the accumulated tail on an SSE tick after paging', async () => {
    // REGRESSION (tick-clobber gate, useConversations.ts ~L37):
    //   if (loadingMoreRef.current || rows.length > PAGE) return;
    // Once the user has paged past PAGE, an SSE tick (a new generated_at on
    // the snapshot) must NOT re-run the first-page fetch — doing so would
    // reset rows back to page 1 and rewind next_offset. The gate suppresses
    // the tick reload until remount.
    mockFetchOnce(fullPage('p1', 50));     // page 1: a FULL 50-row page, more to come
    const { result, rerender } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(50));
    expect(result.current.hasMore).toBe(true);

    mockFetchOnce(fullPage('p2', 100));    // page 2: 50 more — now rows.length > PAGE
    await act(async () => { await result.current.loadMore(); });
    await waitFor(() => expect(result.current.rows).toHaveLength(100));
    expect(result.current.hasMore).toBe(true);
    const fetchCountAfterPaging = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    // Simulate an SSE tick: bump generated_at so the [generatedAt] effect
    // re-runs. If a page-1 refetch fired here it would mockReject (no queued
    // response) AND, more to the point, clobber the tail. Queue a (wrong)
    // page-1 response to make a regression unmistakable: were the gate gone,
    // rows would snap back to 50 and the cursor rewind to offset=50.
    mockFetchOnce(fullPage('tick', 50));
    mockGeneratedAt = 't1';
    await act(async () => { rerender(); await Promise.resolve(); });

    // The tick was suppressed: the accumulated tail is preserved (still 100,
    // NOT reset to 50), the cursor is not rewound (next page is offset=100),
    // and no extra fetch fired.
    expect(result.current.rows).toHaveLength(100);
    expect(result.current.rows[0].session_id).toBe('p1-0');
    expect(result.current.hasMore).toBe(true);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(fetchCountAfterPaging);

    // And loadMore() still advances from the un-rewound cursor (offset=100).
    mockFetchOnce(fullPage('p3', null));
    await act(async () => { await result.current.loadMore(); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0]).toContain('offset=100');
  });

  it('threads active filters into the request URL', async () => {
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    mockFetchOnce(page1);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { projects: ['projA'], costMin: 1 } }));
    await waitFor(() => {
      const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0] as string);
      expect(urls.some((u) => u.includes('projects=projA') && u.includes('cost_min=1'))).toBe(true);
    });
  });

  it('an SSE tick revalidation carries the active filter params (reads filtersRef)', async () => {
    // Cross-branch review coverage: the per-tick page-1 revalidation must NOT
    // repaint an UNfiltered page 1 while a filter is active. The tick effect reads
    // `filtersRef.current` (not a closed-over value) so a generated_at bump after
    // a filter was set re-issues page 1 WITH the active params. Distinct from the
    // "threads filters into the URL" test, which fires the reset effect on a
    // filter CHANGE — here the filter is unchanged and only the SSE tick fires.
    mockFetchOnce(page1);   // mount load (offset 0, unfiltered)
    const { result, rerender } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));

    // Set a filter; the reset effect fires a page-1 fetch carrying it.
    mockFetchOnce(page1);
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { projects: ['projZ'], rebuildMin: 4 } }));
    await waitFor(() => {
      const urls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0] as string);
      expect(urls.some((u) => u.includes('projects=projZ') && u.includes('rebuild_min=4'))).toBe(true);
    });
    const callsBeforeTick = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    // Now an SSE tick (generated_at bump) with the filter UNCHANGED. rows stay at
    // 1 (< PAGE) so the page-1 revalidation is NOT suppressed. The tick fetch must
    // still carry the active filter params — proving it read filtersRef, not an
    // unfiltered base URL.
    mockFetchOnce(page1);
    mockGeneratedAt = 't1';
    await act(async () => { rerender(); await Promise.resolve(); });
    await waitFor(() => {
      expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsBeforeTick);
    });
    const tickUrl = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string;
    expect(tickUrl).toContain('offset=0');          // a page-1 revalidation
    expect(tickUrl).toContain('projects=projZ');     // …carrying the active filters
    expect(tickUrl).toContain('rebuild_min=4');
  });

  it('resets to offset 0 when filters change', async () => {
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    const callsBefore = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    mockFetchOnce({ conversations: [], page: { next_offset: null, has_more: false } });
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 2 } }));
    // The reset effect fires a NEW page-1 fetch (offset 0) carrying the new param —
    // asserting rebuild_min as well as offset=0 proves this is the post-change
    // refetch (non-vacuous), not the mount fetch which is already offset=0.
    await waitFor(() => {
      const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
      expect(calls.length).toBeGreaterThan(callsBefore);
      const last = calls.at(-1)![0] as string;
      expect(last).toContain('offset=0');
      expect(last).toContain('rebuild_min=2');
    });
  });

  it('ignores an in-flight loadMore result once the filter set has changed', async () => {
    // FINDING 2 regression: if loadMore is in flight when the [filterKey] reset
    // effect runs, the effect empties rows + resets the cursor, but the in-flight
    // loadMore.then would still append the OLD-filter rows onto the emptied list
    // with the OLD cursor — briefly showing wrong-filter rows. A generation token
    // captured at loadMore start must make the stale response a no-op.
    mockFetchOnce(page1);   // mount page-1 load (session 'a')
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));

    // Arm loadMore with a fetch we control: it resolves only when we say so.
    let resolveLoadMore!: (body: unknown) => void;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { resolveLoadMore = (body) => res({ ok: true, status: 200, json: async () => body } as Response); }),
    );
    // Kick off loadMore (do NOT await — it's intentionally still in flight).
    let loadMorePromise!: Promise<void>;
    act(() => { loadMorePromise = result.current.loadMore(); });
    await waitFor(() => expect(resolveLoadMore).toBeTypeOf('function'));

    // Filter changes WHILE loadMore is in flight: the reset effect fires a fresh
    // page-1 load. Queue that (post-change) response — the new-filter page.
    mockFetchOnce({ conversations: [{ session_id: 'new', project_label: 'np', git_branch: null, started_utc: '2026-03-01T00:00:00Z', last_activity_utc: '2026-03-01T01:00:00Z', msg_count: 2, cost_usd: 0.5, models: ['opus'] }], page: { next_offset: 9, has_more: true } });
    act(() => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { costMin: 99 } }));
    await waitFor(() => expect(result.current.rows.map((r) => r.session_id)).toEqual(['new']));
    expect(result.current.hasMore).toBe(true);

    // Now resolve the STALE in-flight loadMore (page2 = session 'b', old filter).
    await act(async () => { resolveLoadMore(page2); await loadMorePromise; });

    // The stale page must NOT have been appended onto the new-filter list, and the
    // cursor must remain the new-filter cursor (next_offset=9 from the reset load),
    // NOT page2's null.
    expect(result.current.rows.map((r) => r.session_id)).toEqual(['new']);
    expect(result.current.hasMore).toBe(true);
  });

  it('surfaces filter_degraded from the page body', async () => {
    mockFetchOnce({ conversations: [], page: { next_offset: null, has_more: false, filter_degraded: true } });
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.filterDegraded).toBe(true));
  });

  // #217 S4 / I-2.3 — rail sort wiring + combined {filters,sort} generation.
  it('threads the active rail sort into the request URL (not hardcoded recent)', async () => {
    act(() => dispatch({ type: 'SET_CONVERSATION_RAIL_SORT', sort: 'cost' }));
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('sort=cost');
  });

  it('resets to offset 0 and refetches when the sort changes', async () => {
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    mockFetchOnce({ conversations: [], page: { next_offset: null, has_more: false } });
    act(() => dispatch({ type: 'SET_CONVERSATION_RAIL_SORT', sort: 'messages' }));
    await waitFor(() => expect(result.current.rows).toHaveLength(0));
    const lastCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0];
    expect(lastCall).toContain('sort=messages');
    expect(lastCall).toContain('offset=0');
  });

  it('ignores an in-flight loadMore result once the sort has changed', async () => {
    mockFetchOnce(page1);   // mount page-1 load
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));

    // Arm loadMore with a controllable fetch (still in flight).
    let resolveLoadMore!: (b: unknown) => void;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { resolveLoadMore = (b) => res({ ok: true, status: 200, json: async () => b } as Response); }),
    );
    let loadMorePromise!: Promise<void>;
    act(() => { loadMorePromise = result.current.loadMore(); });

    // Sort changes mid-flight → reset effect fires a fresh page-1 load.
    mockFetchOnce({ conversations: [{ session_id: 'fresh', project_label: 'np', git_branch: null, started_utc: '2026-03-01T00:00:00Z', last_activity_utc: '2026-03-01T01:00:00Z', msg_count: 2, cost_usd: 0.5, models: ['opus'] }], page: { next_offset: 9, has_more: true } });
    act(() => dispatch({ type: 'SET_CONVERSATION_RAIL_SORT', sort: 'cost' }));
    await waitFor(() => expect(result.current.rows.map((r) => r.session_id)).toEqual(['fresh']));

    // Resolve the STALE in-flight loadMore — its result must be discarded.
    await act(async () => { resolveLoadMore(page2); await loadMorePromise; });
    expect(result.current.rows.map((r) => r.session_id)).toEqual(['fresh']);
    expect(result.current.hasMore).toBe(true);
  });

  it('surfaces sort_degraded from the page body', async () => {
    mockFetchOnce({ conversations: [], page: { next_offset: null, has_more: false, sort_degraded: true } });
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.sortDegraded).toBe(true));
  });

  it('recovers from a failed first load via retry() (#205 S3 F8)', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'));
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.error).toBe("Couldn't load conversations."));
    expect(result.current.rows).toHaveLength(0);

    mockFetchOnce(page1);
    await act(async () => { result.current.retry(); });
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect(result.current.error).toBeNull();
  });

  // #305 — the page-1 revalidation now keys on the change signal (revalToken),
  // not the 5s generated_at heartbeat. A finished/static rail refetches once and
  // stays quiet until the underlying data actually changes.
  it('with data_version present, a generated_at-only tick does NOT refetch page 1 (#305)', async () => {
    mockDataVersion = 'v0';
    mockFetchOnce(page1);                       // mount load only
    const { result, rerender } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    const callsAfterMount = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    // SSE heartbeat: generated_at advances, data_version stays flat → no refetch.
    mockGeneratedAt = 't1';
    await act(async () => { rerender(); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });   // give an erroneous refetch a chance

    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsAfterMount);
  });

  it('a data_version change DOES refetch page 1 (#305)', async () => {
    mockDataVersion = 'v0';
    mockFetchOnce(page1);                        // mount load
    const { result, rerender } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    const callsBefore = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    // A real change signal: data_version advances (generated_at held fixed at 't0').
    mockFetchOnce(page1);
    mockDataVersion = 'v1';
    await act(async () => { rerender(); await Promise.resolve(); });
    await waitFor(() => {
      expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsBefore);
    });
    const url = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string;
    expect(url).toContain('offset=0');           // a page-1 revalidation
  });

  it('a data_version change during the initial fetch aborts req1; only req2 populates (no residual-B, #305)', async () => {
    mockDataVersion = 'v0';
    let req1Aborted = false;
    // Hold the initial page-1 fetch pending; reject with AbortError when aborted
    // (matching a real aborted fetch — fetchJson's .catch treats it via isAbortError).
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementationOnce(
      (_url: string, opts?: { signal?: AbortSignal }) =>
        new Promise((_res, rej) => {
          opts?.signal?.addEventListener('abort', () => { req1Aborted = true; rej(new DOMException('Aborted', 'AbortError')); });
        }),
    );
    const { result, rerender } = renderHook(() => useConversations());
    // req1 is in flight, nothing populated yet.
    await waitFor(() => expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(1));
    expect(result.current.rows).toHaveLength(0);

    // data_version changes WHILE req1 is unresolved → the effect re-fires: its
    // cleanup aborts req1 (via ctlRef) and it issues req2. Queue req2's response.
    const freshPage = { conversations: [{ session_id: 'fresh', project_label: 'np', git_branch: null, started_utc: '2026-03-01T00:00:00Z', last_activity_utc: '2026-03-01T01:00:00Z', msg_count: 2, cost_usd: 0.5, models: ['opus'] }], page: { next_offset: null, has_more: false } };
    mockFetchOnce(freshPage);
    mockDataVersion = 'v1';
    await act(async () => { rerender(); await Promise.resolve(); });

    // req1 was aborted; only req2 (the fresh page) populated the list.
    await waitFor(() => expect(result.current.rows.map((r) => r.session_id)).toEqual(['fresh']));
    expect(req1Aborted).toBe(true);
  });
});
