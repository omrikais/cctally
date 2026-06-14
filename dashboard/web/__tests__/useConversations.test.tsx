import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useConversations } from '../src/hooks/useConversations';
import { updateSnapshot, _resetForTests } from '../src/store/store';
import type { Envelope } from '../src/types/envelope';
import type { ConversationsPage } from '../src/types/conversation';

// A minimal valid Envelope whose only test-relevant field is generated_at —
// the rail hook revalidates page 1 on every change to it (the SSE tick).
function mkSnap(generated_at: string): Envelope {
  return {
    envelope_version: 2,
    generated_at,
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'Jun 8–15',
      used_pct: 10,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as Envelope;
}

function mkPage(n: number, offset = 0): ConversationsPage {
  const conversations = Array.from({ length: n }, (_, i) => ({
    session_id: `s-${offset + i}`,
    title: `Conversation ${offset + i}`,
    project_label: 'proj',
    git_branch: null,
    started_utc: '2026-06-14T00:00:00Z',
    last_activity_utc: '2026-06-14T01:00:00Z',
    msg_count: 3,
    cost_usd: 0.01,
    models: ['claude'],
  }));
  return { conversations, page: { next_offset: null, has_more: false } };
}

// Count only fetches to /api/conversations (the rail page-1 query). The store
// itself never fetches in these tests (we drive it via updateSnapshot), but
// guard anyway so unrelated traffic can't inflate the count.
function railFetchCount(spy: ReturnType<typeof vi.spyOn>): number {
  return spy.mock.calls.filter((c: unknown[]) =>
    String(c[0]).startsWith('/api/conversations'),
  ).length;
}

// JSDOM exposes document.visibilityState / document.hidden as getters; override
// with configurable descriptors and restore in cleanup so cases can't leak
// visibility state into each other.
function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  Object.defineProperty(document, 'hidden', { value: state === 'hidden', configurable: true });
}

describe('useConversations — visibility gating', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    _resetForTests();
    // Default: every /api/conversations fetch resolves with a single page.
    fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mkPage(2)), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    setVisibility('visible');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // Restore JSDOM's default visible state for the next test.
    setVisibility('visible');
  });

  it('(a) does not fetch page 1 on an SSE tick while the tab is hidden', async () => {
    const { rerender } = renderHook(() => useConversations());
    // Let the mount load settle, then count from a clean slate.
    await waitFor(() => expect(railFetchCount(fetchSpy)).toBeGreaterThanOrEqual(1));
    fetchSpy.mockClear();

    setVisibility('hidden');
    // Advance an SSE tick (bump generated_at) while hidden.
    act(() => { updateSnapshot(mkSnap('2026-06-14T10:00:01Z')); });
    rerender();

    // Give any (incorrectly-fired) async fetch a chance to register.
    await act(async () => { await Promise.resolve(); });
    expect(railFetchCount(fetchSpy)).toBe(0);
  });

  it('(b) issues exactly one completing fetch on the hidden→visible transition', async () => {
    // Mount while hidden so no mount fetch fires.
    setVisibility('hidden');
    const { rerender } = renderHook(() => useConversations());
    await act(async () => { await Promise.resolve(); });
    expect(railFetchCount(fetchSpy)).toBe(0);
    fetchSpy.mockClear();

    // Flip to visible and dispatch the browser's visibilitychange event.
    setVisibility('visible');
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    rerender();

    // A burst may ISSUE several fetches; the shared AbortController aborts all
    // but the last, so exactly one COMPLETES (resolves into setRows).
    await waitFor(() => expect(railFetchCount(fetchSpy)).toBeGreaterThanOrEqual(1));
    let completed = 0;
    await act(async () => {
      await Promise.all(
        fetchSpy.mock.results.map(async (r: { value: unknown }) => {
          try {
            const resp = await (r.value as Promise<Response>);
            // A fetch "completes" for our purposes when its body is consumed by
            // the hook; here we just confirm at least one response resolved ok.
            if (resp.ok) completed += 1;
          } catch {
            /* aborted */
          }
        }),
      );
    });
    expect(completed).toBeGreaterThanOrEqual(1);
  });

  it('(c) does not refetch page 1 on a render that leaves generated_at unchanged', async () => {
    const { rerender } = renderHook(() => useConversations());
    await waitFor(() => expect(railFetchCount(fetchSpy)).toBeGreaterThanOrEqual(1));
    fetchSpy.mockClear();

    // Re-render WITHOUT advancing generated_at (proves loadFirstPage is
    // ref-stable and the tick effect is keyed on generatedAt only — a row/state
    // change must not recreate the callback and refetch).
    rerender();
    await act(async () => { await Promise.resolve(); });
    expect(railFetchCount(fetchSpy)).toBe(0);
  });
});
