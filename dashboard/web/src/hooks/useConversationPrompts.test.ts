import { renderHook, waitFor } from '@testing-library/react';
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
});
