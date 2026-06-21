import { renderHook, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversationFind } from './useConversationFind';
import type { ConversationFindResult } from '../types/conversation';

// useConversationFind debounces 200ms (seeded '') before fetching. Drive it by
// stubbing fetch + advancing the fake timers, mirroring FindBar.test.tsx.
function okResponse(body: Partial<ConversationFindResult>): Response {
  const full: ConversationFindResult = {
    anchors: [], total: 0, anchors_truncated: false, mode: 'fts', search_depth: 'full', ...body,
  };
  return { ok: true, status: 200, json: async () => full } as Response;
}

// Advance past the debounce + drain the fetch microtasks.
async function settle() {
  await act(async () => { vi.advanceTimersByTime(250); });
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  globalThis.fetch = vi.fn();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useConversationFind', () => {
  it('populates anchors/total/mode/truncated on a successful fetch', async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      okResponse({ anchors: [{ uuid: 'u1', match_kinds: ['tool'] }], total: 1, anchors_truncated: false, mode: 'fts' }),
    );
    const { result, rerender } = renderHook(
      ({ q }: { q: string }) => useConversationFind('s1', q),
      { initialProps: { q: '' } },
    );
    rerender({ q: 'needle' });
    await settle();
    expect(result.current.anchors.map((a) => a.uuid)).toEqual(['u1']);
    expect(result.current.total).toBe(1);
    expect(result.current.mode).toBe('fts');
    expect(result.current.error).toBeNull();
  });

  it('clears anchors/total/truncated/mode on a non-abort fetch failure (M5)', async () => {
    // First a successful query lands real results.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      okResponse({
        anchors: [{ uuid: 'u1', match_kinds: [] }, { uuid: 'u2', match_kinds: [] }],
        total: 700, anchors_truncated: true, mode: 'like',
      }),
    );
    const { result, rerender } = renderHook(
      ({ q }: { q: string }) => useConversationFind('s1', q),
      { initialProps: { q: '' } },
    );
    rerender({ q: 'first' });
    await settle();
    expect(result.current.anchors.length).toBe(2);
    expect(result.current.total).toBe(700);
    expect(result.current.truncated).toBe(true);
    expect(result.current.mode).toBe('like');

    // A subsequent refetch FAILS (500 -> HttpError, non-abort). The stale result
    // MUST be cleared so the find bar can't navigate matches that no longer hold.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      { ok: false, status: 500, json: async () => ({}) } as Response,
    );
    rerender({ q: 'second' });
    await settle();
    // #217 S4 / I-1 fixup — the generic-failure string is rendered verbatim by
    // FindBar (no re-derivation), so it must match the UI wording: lowercase
    // 'find failed', not the prior 'Find failed.' (which FindBar discarded).
    expect(result.current.error).toBe('find failed');
    expect(result.current.anchors).toEqual([]);
    expect(result.current.total).toBe(0);
    expect(result.current.truncated).toBe(false);
    expect(result.current.mode).toBeNull();
  });
});
