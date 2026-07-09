import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useConversationFacets } from './useConversationFacets';

beforeEach(() => { globalThis.fetch = vi.fn(); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

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

// #278 Theme C — the hook exposes the model facets and normalizes on the
// SUCCESS path, not just initial/error state.
it('exposes models from the facets response', async () => {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: true, status: 200,
    json: async () => ({
      projects: [{ project_label: 'projA', count: 4 }],
      models: [{ family: 'opus', count: 3 }, { family: 'sonnet', count: 1 }],
    }),
  } as Response);
  const { result } = renderHook(() => useConversationFacets());
  await waitFor(() => expect(result.current.models).toHaveLength(2));
  expect(result.current.models[0].family).toBe('opus');
  expect(result.current.models[0].count).toBe(3);
});

it('normalizes a legacy {projects}-only response to models: []', async () => {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: true, status: 200,
    // A response carrying NO `models` key (older / mocked server) must not
    // leave models undefined — the success path defaults it to [].
    json: async () => ({ projects: [{ project_label: 'projA', count: 4 }] }),
  } as Response);
  const { result } = renderHook(() => useConversationFacets());
  await waitFor(() => expect(result.current.projects).toHaveLength(1));
  expect(result.current.models).toEqual([]);
});

// #278 Theme C P3 follow-up — a transient facets fetch failure is retried once
// (bounded) instead of failing closed to empty until the popover is reopened.
it('retries once after a transient failure, then recovers', async () => {
  vi.useFakeTimers();
  const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
  fetchMock
    .mockRejectedValueOnce(new Error('transient'))
    .mockResolvedValueOnce({
      ok: true, status: 200,
      json: async () => ({
        projects: [{ project_label: 'projA', count: 4 }],
        models: [{ family: 'opus', count: 3 }],
      }),
    } as Response);

  const { result } = renderHook(() => useConversationFacets());

  // First attempt rejects — the hook must NOT settle to empty; it holds the
  // default while a retry is pending (no second fetch yet).
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(result.current.projects).toEqual([]);
  expect(fetchMock).toHaveBeenCalledTimes(1);

  // After the retry backoff the second (successful) fetch populates the facets.
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(result.current.projects).toHaveLength(1);
  expect(result.current.models).toHaveLength(1);
});

// The retry is bounded to a SINGLE attempt — a persistently-down endpoint must
// not spin forever; after one retry it settles to the empty default.
it('stops after a single retry when the failure persists', async () => {
  vi.useFakeTimers();
  const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
  fetchMock.mockRejectedValue(new Error('down'));

  const { result } = renderHook(() => useConversationFacets());

  await act(async () => { await vi.advanceTimersByTimeAsync(0); });      // attempt 1 fails
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });   // retry fails
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });  // no 3rd attempt ever

  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(result.current.projects).toEqual([]);
  expect(result.current.models).toEqual([]);
});
