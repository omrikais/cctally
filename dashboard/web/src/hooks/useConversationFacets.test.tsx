import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useConversationFacets } from './useConversationFacets';

beforeEach(() => { globalThis.fetch = vi.fn(); });
afterEach(() => vi.restoreAllMocks());

it('fetches and exposes projects', async () => {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: true, status: 200,
    json: async () => ({ projects: [{ project_label: 'projA', count: 4 }] }),
  } as Response);
  const { result } = renderHook(() => useConversationFacets());
  await waitFor(() => expect(result.current.projects).toHaveLength(1));
  expect(result.current.projects[0].project_label).toBe('projA');
  expect(result.current.projects[0].count).toBe(4);
  expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversations/facets');
});

it('falls back to an empty list on a fetch error', async () => {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'));
  const { result } = renderHook(() => useConversationFacets());
  // Stays the empty default — never throws.
  await waitFor(() => expect(result.current.projects).toEqual([]));
});
