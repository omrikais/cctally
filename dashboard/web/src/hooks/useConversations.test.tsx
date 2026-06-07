import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversations } from './useConversations';

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

beforeEach(() => { globalThis.fetch = vi.fn(); });
afterEach(() => { vi.restoreAllMocks(); });

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
});
