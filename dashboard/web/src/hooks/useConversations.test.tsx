import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversations } from './useConversations';

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick
// signal the first-page effect keys on) deterministically between renders.
let mockGeneratedAt = 't0';
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt }),
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

beforeEach(() => { globalThis.fetch = vi.fn(); mockGeneratedAt = 't0'; });
afterEach(() => { vi.restoreAllMocks(); });

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
  it('loads the first page on mount', async () => {
    mockFetchOnce(page1);
    const { result } = renderHook(() => useConversations());
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect(result.current.rows[0].session_id).toBe('a');
    expect(result.current.hasMore).toBe(true);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversations?sort=recent&limit=50&offset=0');
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
});
