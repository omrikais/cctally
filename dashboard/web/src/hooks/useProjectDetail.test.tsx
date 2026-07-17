import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectDetail } from './useProjectDetail';

// Mock the snapshot store so we can drive the SSE-tick signals deterministically.
// `generated_at` is the 5s heartbeat; `data_version` (#300) is the real change
// signal. `undefined` (the default) makes the hook fall back to `generated_at`,
// preserving the pre-#300 every-tick behavior for the fallback test.
let mockGeneratedAt = 't0';
let mockDataVersion: string | undefined = undefined;
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt, data_version: mockDataVersion }),
}));

function fetchCalls(): number {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
}
function lastUrl(): string {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  return String(calls[calls.length - 1][0]);
}
function alwaysResolve(key = 'p', cost = 1) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
    { ok: true, status: 200, json: async () => ({ key, cost_usd: cost }) } as Response);
}

beforeEach(() => { globalThis.fetch = vi.fn(); mockGeneratedAt = 't0'; mockDataVersion = undefined; });
afterEach(() => vi.restoreAllMocks());

describe('useProjectDetail', () => {
  it('fetches the drill on (projectKey, windowWeeks)', async () => {
    alwaysResolve('p');
    const { result } = renderHook(() => useProjectDetail('p', 12));
    await waitFor(() => expect((result.current.data as { key?: string } | null)?.key).toBe('p'));
    expect(lastUrl()).toContain('/api/project/p?weeks=12');
    expect(fetchCalls()).toBe(1);
    expect(result.current.error).toBeNull();
  });

  it('null projectKey → no fetch, data null, not loading', async () => {
    const { result } = renderHook(() => useProjectDetail(null, 12));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  // #300 — the drill must gate on the change signal (data_version), not the 5s
  // `generated_at` heartbeat: a finished/static project drill fetches once and
  // is not re-GET every tick while the panel is open.
  it('with data_version present, a generated_at-only tick does NOT refetch (#300)', async () => {
    mockDataVersion = 'v1';
    alwaysResolve('p');
    const { result, rerender } = renderHook(() => useProjectDetail('p', 12));
    await waitFor(() => expect((result.current.data as { key?: string } | null)?.key).toBe('p'));
    expect(fetchCalls()).toBe(1);
    // generated_at advances twice, data_version stays flat → no refetch.
    await act(async () => { mockGeneratedAt = 't1'; rerender(); await Promise.resolve(); });
    await act(async () => { mockGeneratedAt = 't2'; rerender(); await Promise.resolve(); });
    expect(fetchCalls()).toBe(1);
  });

  it('a data_version change refetches even when generated_at is unchanged (#300)', async () => {
    mockDataVersion = 'v1';
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ key: 'p', cost_usd: 1 }) } as Response);
    const { result, rerender } = renderHook(() => useProjectDetail('p', 12));
    await waitFor(() => expect((result.current.data as { cost_usd?: number } | null)?.cost_usd).toBe(1));
    // generated_at left at 't0'; only the change signal advances → refetch.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ key: 'p', cost_usd: 2 }) } as Response);
    await act(async () => { mockDataVersion = 'v2'; rerender(); await Promise.resolve(); });
    await waitFor(() => expect((result.current.data as { cost_usd?: number } | null)?.cost_usd).toBe(2));
  });

  it('fallback: with data_version absent, a generated_at tick still refetches', async () => {
    // mockDataVersion stays undefined → token falls back to generated_at.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ key: 'p', cost_usd: 1 }) } as Response);
    const { result, rerender } = renderHook(() => useProjectDetail('p', 12));
    await waitFor(() => expect((result.current.data as { cost_usd?: number } | null)?.cost_usd).toBe(1));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      { ok: true, status: 200, json: async () => ({ key: 'p', cost_usd: 2 }) } as Response);
    await act(async () => { mockGeneratedAt = 't1'; rerender(); await Promise.resolve(); });
    await waitFor(() => expect((result.current.data as { cost_usd?: number } | null)?.cost_usd).toBe(2));
  });

  // #300 P1-b — the initial fetch must NEVER be aborted/stranded by a change
  // signal that lands while it is still in flight. Regression guard: the stub
  // RESPECTS the abort signal (rejects with AbortError on abort), so if the
  // per-effect AbortController + cleanup-abort were ever reintroduced, the
  // held fetch would reject and strand on "Loading…" and this test would fail.
  it('a data_version change mid-initial-fetch does not strand it (P1-b, #300)', async () => {
    mockDataVersion = 'v1';
    let resolveFetch: ((body: unknown) => void) | null = null;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (_url: string, opts?: { signal?: AbortSignal }) =>
        new Promise<Response>((res, rej) => {
          resolveFetch = (body: unknown) =>
            res({ ok: true, status: 200, json: async () => body } as Response);
          opts?.signal?.addEventListener('abort', () =>
            rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        }));

    const { result, rerender } = renderHook(() => useProjectDetail('p', 12));
    await waitFor(() => expect(fetchCalls()).toBe(1));
    expect(result.current.loading).toBe(true);

    // A change signal lands while the initial fetch is still in flight. The
    // guard must let the in-flight fetch resolve (no restart, no abort).
    await act(async () => { mockDataVersion = 'v2'; rerender(); await Promise.resolve(); });
    expect(fetchCalls()).toBe(1); // no restart

    // Resolve the still-in-flight initial fetch → it must commit, not be lost.
    await act(async () => { resolveFetch!({ key: 'p', cost_usd: 9 }); for (let i = 0; i < 4; i++) await Promise.resolve(); });
    await waitFor(() => expect((result.current.data as { cost_usd?: number } | null)?.cost_usd).toBe(9));
    expect(result.current.loading).toBe(false);
  });

  // #300 — a key change supersedes the in-flight fetch: a late response for the
  // OLD key must be dropped (the reqId stale-guard), never committed over the new.
  it('drops a stale-key response when projectKey changes mid-fetch', async () => {
    let resolveP1: ((body: unknown) => void) | null = null;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (String(url).includes('/api/project/p1')) {
        return new Promise<Response>((res) => {
          resolveP1 = (body: unknown) =>
            res({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      return Promise.resolve(
        { ok: true, status: 200, json: async () => ({ key: 'p2', cost_usd: 2 }) } as Response);
    });

    const { result, rerender } = renderHook(
      ({ k }) => useProjectDetail(k, 12), { initialProps: { k: 'p1' as string | null } });
    // p1 fetch pending; switch to p2 (resolves immediately).
    rerender({ k: 'p2' });
    await waitFor(() => expect((result.current.data as { key?: string } | null)?.key).toBe('p2'));

    // Now resolve the stale p1 fetch — it must be dropped, not overwrite p2.
    await act(async () => { resolveP1!({ key: 'p1', cost_usd: 1 }); for (let i = 0; i < 4; i++) await Promise.resolve(); });
    expect((result.current.data as { key?: string } | null)?.key).toBe('p2');
  });
});
