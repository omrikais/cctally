import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useConversationPrompts } from './useConversationPrompts';

afterEach(() => vi.restoreAllMocks());

describe('useConversationPrompts', () => {
  it('does not fetch until active, then fetches once and caches by uuid', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ session_id: 's1', prompts: [{ uuid: 'u1', text: 'hello' }] }),
        { status: 200 },
      ),
    );
    const { result, rerender } = renderHook(
      ({ active }) => useConversationPrompts('s1', active),
      { initialProps: { active: false } },
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    rerender({ active: true });
    await waitFor(() => expect(result.current.byUuid?.u1).toBe('hello'));
    rerender({ active: true }); // no refetch
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('reports an error string on a non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('nope', { status: 404 }));
    const { result } = renderHook(() => useConversationPrompts('s2', true));
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.byUuid).toBeNull();
  });

  it.each([
    { reason: 'maintenance', ref: 's1', shape: 'bare' },
    { reason: 'maintenance', ref: { source: 'codex' as const, key: 'v1.codex-prompts' }, shape: 'qualified' },
    { reason: 'schema_behind', ref: 's1', shape: 'bare' },
    { reason: 'schema_behind', ref: { source: 'codex' as const, key: 'v1.codex-prompts' }, shape: 'qualified' },
    { reason: 'legacy_bridge_pending', ref: 's1', shape: 'bare' },
    { reason: 'legacy_bridge_pending', ref: { source: 'codex' as const, key: 'v1.codex-prompts' }, shape: 'qualified' },
  ] as const)('surfaces a 200 $reason envelope as degraded for $shape prompts reads', async ({ reason, ref }) => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ status: 'degraded', degraded_reason: reason, prompts: [], total: 0 }),
      { status: 200 },
    ));
    const { result } = renderHook(() => useConversationPrompts(ref, true));

    await waitFor(() => expect(result.current.degraded?.reason).toBe(reason));
    expect(result.current.byUuid).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it('keeps qualified normalization-pending prompts behavior distinct from degraded', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ status: 'normalization_pending', conversation_key: 'v1.codex-pending', prompts: [] }),
      { status: 200 },
    ));
    const { result } = renderHook(() => useConversationPrompts(
      { source: 'codex', key: 'v1.codex-pending' }, true,
    ));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.degraded).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.byUuid).toEqual({});
  });

  it('resets the cache when the session id changes', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((url) => {
      const u = String(url);
      const sid = u.includes('s-a') ? 'a' : 'b';
      return Promise.resolve(
        new Response(
          JSON.stringify({ session_id: sid, prompts: [{ uuid: `u-${sid}`, text: sid }] }),
          { status: 200 },
        ),
      );
    });
    const { result, rerender } = renderHook(
      ({ sid }) => useConversationPrompts(sid, true),
      { initialProps: { sid: 's-a' } },
    );
    await waitFor(() => expect(result.current.byUuid?.['u-a']).toBe('a'));
    rerender({ sid: 's-b' });
    await waitFor(() => expect(result.current.byUuid?.['u-b']).toBe('b'));
    // the stale session's map is gone, not merged
    expect(result.current.byUuid?.['u-a']).toBeUndefined();
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('ignores a late successful response for a session whose request was aborted', async () => {
    let resolveOld!: (response: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockImplementation((url) => String(url).includes('old')
      ? new Promise<Response>((resolve) => { resolveOld = resolve; })
      : Promise.resolve(new Response(JSON.stringify({ prompts: [{ uuid: 'current', text: 'new' }] }), { status: 200 })));
    const { result, rerender } = renderHook(({ sid }) => useConversationPrompts(sid, true), {
      initialProps: { sid: 'old' },
    });
    rerender({ sid: 'new' });
    await waitFor(() => expect(result.current.byUuid?.current).toBe('new'));
    await act(async () => {
      resolveOld(new Response(JSON.stringify({ status: 'degraded', degraded_reason: 'maintenance', prompts: [] }), { status: 200 }));
      await Promise.resolve();
    });
    expect(result.current.byUuid).toEqual({ current: 'new' });
    expect(result.current.degraded).toBeNull();
  });
});
