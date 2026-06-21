import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useConversation } from './useConversation';
import type { OutlineTurn } from '../types/conversation';

// Minimal EventSource mock (copied from __tests__/sse.test.ts). The live-tail
// effect opens one of these per conversation and fires pollTail() on the `tail`
// event and on `open`.
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  closed = false;
  constructor(url: string) { this.url = url; MockEventSource.instances.push(this); }
  addEventListener(name: string, fn: (ev: MessageEvent) => void): void {
    (this.listeners[name] ||= []).push(fn);
  }
  close(): void { this.closed = true; }
  emit(name: string, data: unknown = {}): void {
    (this.listeners[name] || []).forEach((fn) => fn({ data: JSON.stringify(data) } as MessageEvent));
  }
}

// Mock the snapshot store so we can drive `generated_at` (the SSE-tick signal
// the live-tail backstop effect keys on) and `transcriptsEnabled` (the gate on
// the dedicated live-tail EventSource) deterministically between renders.
// Mirrors the useConversations test setup.
let mockGeneratedAt = 't0';
let mockTranscripts = true;
let mockLiveTail = true;
vi.mock('./useSnapshot', () => ({
  useSnapshot: () => ({ generated_at: mockGeneratedAt, transcriptsEnabled: mockTranscripts }),
}));
vi.mock('../store/store', async (orig) => ({
  ...(await orig<typeof import('../store/store')>()),
  selectLiveTailEnabled: () => mockLiveTail,
}));

function detail(items: unknown[], next_after: number | null, over: Record<string, unknown> = {}) {
  return {
    session_id: 's', project_label: 'p', git_branch: null,
    started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 3, models: ['opus'], items, page: { next_after, has_more: next_after != null },
    ...over,
  };
}
const it1 = { kind: 'human', anchor: { session_id: 's', uuid: 'u1', id: 1 }, member_uuids: ['u1'], ts: 't', text: 'hi', blocks: [], is_sidechain: false };
const it2 = { kind: 'assistant', anchor: { session_id: 's', uuid: 'u2', id: 2 }, member_uuids: ['u2', 'u2b'], ts: 't', text: 'yo', blocks: [], model: 'opus', is_sidechain: false, cost_usd: 1 };
const it3 = { kind: 'assistant', anchor: { session_id: 's', uuid: 'u3', id: 3 }, member_uuids: ['u3'], ts: 't', text: 'live', blocks: [], model: 'opus', is_sidechain: false, cost_usd: 1 };
const it4 = { kind: 'human', anchor: { session_id: 's', uuid: 'u4', id: 4 }, member_uuids: ['u4'], ts: 't', text: 'more', blocks: [], is_sidechain: false };

function mockOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: status < 400, status, json: async () => body } as Response);
}
beforeEach(() => {
  globalThis.fetch = vi.fn();
  mockGeneratedAt = 't0';
  mockTranscripts = true;
  mockLiveTail = true;
  (globalThis as unknown as { EventSource: typeof MockEventSource }).EventSource = MockEventSource;
  MockEventSource.instances = [];
});
afterEach(() => vi.restoreAllMocks());

// Bump generated_at + re-render to simulate one SSE tick reaching the hook.
function bumpTick(rerender: () => void, tag: string) {
  mockGeneratedAt = tag;
  rerender();
}

describe('useConversation', () => {
  it('loads page 1 for a session', async () => {
    mockOnce(detail([it1], 2));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    expect(result.current.detail?.cost_usd).toBe(3);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('/api/conversation/s?limit=500');
  });

  it('appends pages via the after cursor', async () => {
    mockOnce(detail([it1], 2));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    mockOnce(detail([it2], null));
    await act(async () => { await result.current.loadMore(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[1][0]).toContain('after=2');
    expect(result.current.hasMore).toBe(false);
  });

  it('tail-polls after a generated_at tick once fully paged, appending new turns + fresh header (#175 F4)', async () => {
    // Page 1: two items, next_after null -> fully paged (hasMore false).
    mockOnce(detail([it1, it2], null, { cost_usd: 1 }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.hasMore).toBe(false);

    // Next tick: the §6 overlap window re-returns it1+it2 (cursor null, the
    // whole 2-item accumulator fits TAIL_WINDOW) plus one genuinely-new turn +
    // a fresh whole-session cost. Stays fully-paged (next_after null), single fetch.
    mockOnce(detail([it1, it2, it3], null, { cost_usd: 2 }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 2, 3]);

    // The overlap poll keyed off the window cursor — null here (2 items ≤ window),
    // so NO `after=` is sent and the server re-returns the full window.
    expect(String((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0])).not.toContain('after=');
    // Header totals refreshed from the tail response.
    expect(result.current.detail?.cost_usd).toBe(2);
    // Still fully paged after a live append.
    expect(result.current.hasMore).toBe(false);
  });

  it('exposes a monotonic tailRevision bumped on each successful pollTail merge — including an overlap-window mutation that does NOT change items.length (#217 S4 / I-1.6 Codex P1)', async () => {
    // Page 1: it1+it2 fully paged.
    mockOnce(detail([it1, it2], null, { cost_usd: 1 }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    const rev0 = result.current.tailRevision;

    // A live tail re-returns the SAME two ids but with it2 REPLACED in place
    // (a fold/update into an already-delivered turn — its text changed). The
    // length is UNCHANGED (2 → 2), yet the find corpus changed: tailRevision
    // MUST still bump. (items.length as the signal would miss this — the P1 trap.)
    const it2mut = { ...it2, text: 'yo (folded skill body added)', cost_usd: 2 };
    mockOnce(detail([it1, it2mut], null, { cost_usd: 2 }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items[1].text).toBe('yo (folded skill body added)'));
    expect(result.current.detail?.items).toHaveLength(2);             // length unchanged
    expect(result.current.tailRevision).toBeGreaterThan(rev0);        // but revision bumped

    // A genuinely-new append also bumps it (monotonic).
    const rev1 = result.current.tailRevision;
    mockOnce(detail([it1, it2mut, it3], null, { cost_usd: 3 }));
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));
    expect(result.current.tailRevision).toBeGreaterThan(rev1);
  });

  it('refreshes last_anchor when a live tail append adds a new final turn (jump-to-latest staleness)', async () => {
    // REGRESSION (jump-to-latest stale anchor): the "Latest ↓" control jumps to
    // detail.last_anchor. pollTail's partial merge refreshed items/cost/models/
    // title but DROPPED last_anchor, so after a live append the anchor stayed at
    // the page-1 (entry-time) final turn — clicking "Latest" landed on a stale
    // message, never the genuinely-newest one.
    //
    // Page 1: it1+it2 fully paged; last_anchor = it2 (the final turn at load).
    mockOnce(detail([it1, it2], null, { last_anchor: { session_id: 's', uuid: 'u2', id: 2 } }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.detail?.last_anchor?.id).toBe(2);

    // A live tail append adds it3 — now the genuinely-final turn. The server's
    // response carries a FRESH last_anchor (id 3, computed from the whole-session
    // tail). The merge must adopt it so jump-to-latest targets the newest turn.
    mockOnce(detail([it1, it2, it3], null, { last_anchor: { session_id: 's', uuid: 'u3', id: 3 } }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));

    // RED before fix: last_anchor stuck at id 2 (page-1 value). GREEN after: id 3.
    expect(result.current.detail?.last_anchor?.id).toBe(3);
    expect(result.current.detail?.last_anchor?.uuid).toBe('u3');
  });

  it('refreshes last_activity_utc on a live tail append (same dropped-field class as last_anchor)', async () => {
    // last_activity_utc grows with each new turn too; pollTail dropped it for the
    // same reason it dropped last_anchor. Lock it so the merge keeps carrying the
    // whole-session-fresh value forward.
    mockOnce(detail([it1, it2], null, { last_activity_utc: '2026-01-01T02:00:00Z' }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.detail?.last_activity_utc).toBe('2026-01-01T02:00:00Z');

    mockOnce(detail([it1, it2, it3], null, { last_activity_utc: '2026-01-01T03:30:00Z' }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.last_activity_utc).toBe('2026-01-01T03:30:00Z'));
  });

  it('drains a >PAGE tail burst within one tick (#175 F4, §6 overlap)', async () => {
    // Page 1: fully paged with a single item (well inside TAIL_WINDOW=10, so the
    // overlap fetches carry no `after=` cursor — the whole accumulator IS the
    // window and the server re-returns it each page).
    mockOnce(detail([it1], null));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));

    // One tick, two overlap pages. Each page re-returns the current window plus a
    // genuinely-new turn. Page A appends it3 and signals next_after != null so the
    // bounded drain loops again in the SAME tick; page B re-returns the grown
    // window + it4 then exhausts (next_after null).
    mockOnce(detail([it1, it3], 3));       // tail page A: window + it3, more to come
    mockOnce(detail([it1, it3, it4], null)); // tail page B: window + it4, exhausted
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));

    // The two new turns appended in order — no duplication despite the window
    // being re-returned each page.
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 3, 4]);
    // Both overlap fetches keyed off the window cursor (null → no `after=`),
    // never the strict last item.
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(String(calls[calls.length - 2][0])).not.toContain('after=');
    expect(String(calls[calls.length - 1][0])).not.toContain('after=');
    // The stored cursor stays null while live-tailing (still fully paged).
    expect(result.current.hasMore).toBe(false);
  });

  it('replays a tick that arrives mid-fetch exactly once (coalesce, #175 F4)', async () => {
    // Fully paged (hasMore false). The FIRST tail fetch is held pending via a
    // deferred resolver while two more ticks arrive. pollTail sees pollingRef
    // set on each and records pendingTickRef (coalesced — multiple ticks collapse
    // into one pending flag). When the first fetch resolves, the `finally` replays
    // exactly ONE additional `?after=` fetch — not zero, not two.
    // NOTE §6 overlap: page 1 holds 2 items (≤ TAIL_WINDOW=10), so the tail
    // fetches carry NO `after=` cursor — the whole accumulator is the window and
    // the server re-returns it. We distinguish the page-1 load from tail fetches
    // by call ORDER (the first request is page 1), not by an `after=` substring.
    let resolveFirst!: (body: unknown) => void;
    let calls = 0;
    let tailCount = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => {
      calls += 1;
      if (calls === 1) {
        // page-1 load resolves immediately (fully paged).
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1, it2], null) } as Response);
      }
      // Tail fetches. Hold the FIRST one pending; resolve later ones immediately.
      tailCount += 1;
      if (tailCount === 1) {
        return new Promise((resolve) => {
          resolveFirst = (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response);
        });
      }
      // The replay re-returns the (grown) window with nothing new → no append.
      return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1, it2, it3], null) } as Response);
    });

    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.hasMore).toBe(false);

    // Tick 1 kicks off the first tail fetch (held pending via resolveFirst).
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(tailCount).toBe(1));

    // Ticks 2 and 3 arrive while the first fetch is still pending. pollTail sees
    // pollingRef set both times and only records pendingTickRef — no new fetch yet.
    await act(async () => { bumpTick(rerender, 't2'); await Promise.resolve(); });
    await act(async () => { bumpTick(rerender, 't3'); await Promise.resolve(); });
    expect(tailCount).toBe(1); // still only the in-flight fetch

    // Resolve the first fetch with the overlap window + the new it3; the
    // `finally` replays the single coalesced tick.
    await act(async () => { resolveFirst(detail([it1, it2, it3], null)); for (let i = 0; i < 6; i++) await Promise.resolve(); });
    await waitFor(() => expect(tailCount).toBe(2));

    // Exactly ONE replay fetch — the two coalesced ticks did NOT each spawn one.
    expect(tailCount).toBe(2);
    // The first fetch's overlap merge appended it3 exactly once (no dup from the
    // re-returned window); the replay re-returned the same window and appended
    // nothing further.
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 2, 3]);
  });

  it('does not tail-poll while hasMore is true (#175 F4)', async () => {
    mockOnce(detail([it1], 2));            // page 1 has a cursor -> hasMore true
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockClear();
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    // The tick fired but the tail poll is suppressed while still paginating.
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('refreshes the whole-session header even when the tail response is empty (#175 F4)', async () => {
    mockOnce(detail([it1, it2], null, { cost_usd: 1, models: ['opus'] }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    // Empty tail (no new turns) but fresh header totals (e.g. a re-priced turn).
    mockOnce(detail([], null, { cost_usd: 5, models: ['opus', 'sonnet'] }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.cost_usd).toBe(5));

    // No item growth, header merged.
    expect(result.current.detail?.items).toHaveLength(2);
    expect(result.current.detail?.models).toEqual(['opus', 'sonnet']);
  });

  it('#193: live tail updates a rewritten title (P1-4)', async () => {
    // Page 1 carries the initial ai-title.
    mockOnce(detail([it1, it2], null, { title: 'Old' }));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.detail?.title).toBe('Old');

    // Empty tail (no new turns) but the ai-title was rewritten mid-session. The
    // merge must propagate body.title so an OPEN reader's header re-titles.
    mockOnce(detail([], null, { title: 'New' }));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.title).toBe('New'));
    expect(result.current.detail?.items).toHaveLength(2);
  });

  // ── §6 overlap upsert (Bug 1) ──────────────────────────────────────────
  // The tail poll re-fetches a small recent WINDOW (TAIL_WINDOW=10) from the
  // cursor just BEFORE that window, so the window is re-returned and a later
  // fold/update into an already-delivered item reaches the live client. Items
  // outside the window are untouched; in-window items are replaced in place if
  // still returned, deleted if absent; genuinely-new items append.

  it('§6: a fold into a recently-delivered window item updates it in place (no dup)', async () => {
    // Page 1: two items, fully paged. it2 carries an EMPTY blocks list (a Skill
    // chip whose body hasn't folded yet).
    const it2chip = { ...it2, blocks: [{ kind: 'tool_call', tool_use_id: 'tu1', name: 'Skill' }] };
    mockOnce(detail([it1, it2chip], null));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.detail?.items[1].blocks).toHaveLength(1);

    // Tick: the overlap window re-returns it1 + it2 (cursor is null since the
    // total ≤ TAIL_WINDOW). it2's blocks now carry the folded skill body. The
    // server returns the SAME anchor.id for it2 → replace in place, not append.
    const it2folded = { ...it2, blocks: [{ kind: 'tool_call', tool_use_id: 'tu1', name: 'Skill' }, { kind: 'text', text: 'folded skill body' }] };
    mockOnce(detail([it1, it2folded], null));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items[1].blocks).toHaveLength(2));

    // Replaced in place — exactly two items, no duplicate it2.
    expect(result.current.detail?.items).toHaveLength(2);
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 2]);
    // The pre-window page (it1) is byte-untouched.
    expect(result.current.detail?.items[0]).toBe(it1);
    // No `after=` cursor on this fetch — the whole accumulator is the window.
    const last = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string;
    expect(last).not.toContain('after=');
  });

  it('§6 P1-E: an item folded AWAY (absent from the refreshed window) is deleted', async () => {
    // Page 1: three items, fully paged. it2 is a standalone skill body the
    // kernel will later DROP (Phase-4b) once it folds into it1's chip.
    mockOnce(detail([it1, it2, it3], null));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));

    // Tick: the refreshed window omits it2 (id 2) — it folded away. Within the
    // window, the locally-held it2 must be DELETED, not preserved or duplicated.
    mockOnce(detail([it1, it3], null));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 3]);
  });

  it('§6 P1-E: a fold-distance BEYOND the window is NOT picked up (documents the bound)', async () => {
    // Build 12 items; the fold targets item id 1 — that is 11 items back from
    // the last item, beyond TAIL_WINDOW=10. The cursor on the tick is
    // items[splitIdx-1].anchor.id where splitIdx = 12-10 = 2 → after=2. So the
    // server's `after=2` window starts at id 3 and NEVER re-returns id 1; the
    // stale id-1 item is left as-is. This asserts the documented window bound.
    const many = Array.from({ length: 12 }, (_, i) => ({
      kind: 'assistant', anchor: { session_id: 's', uuid: `m${i + 1}`, id: i + 1 },
      member_uuids: [`m${i + 1}`], ts: 't', text: `t${i + 1}`,
      blocks: [{ kind: 'text', text: `orig${i + 1}` }], model: 'opus', is_sidechain: false, cost_usd: 0,
    }));
    mockOnce(detail(many, null));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(12));

    // Tick: the window (after=2) re-returns ids 3..12 only, with id 1's content
    // CHANGED on the server. Because id 1 is outside the window, the client never
    // sees the change.
    const refreshed = many.slice(2).map((m) => ({ ...m }));
    mockOnce(detail(refreshed, null));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    // Give the merge a tick to settle.
    await waitFor(() => {
      const last = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0] as string;
      expect(last).toContain('after=2');
    });

    // id 1's stale content is unchanged (folds beyond the window are not seen).
    const head = result.current.detail?.items[0];
    expect(head?.anchor.id).toBe(1);
    expect(head?.blocks[0]).toMatchObject({ text: 'orig1' });
    expect(result.current.detail?.items).toHaveLength(12);
  });

  it('§6: genuinely-new turns still append past the window', async () => {
    mockOnce(detail([it1, it2], null));
    const { result, rerender } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    // Tick: window re-returns it1+it2 (unchanged) plus a genuinely-new it3.
    mockOnce(detail([it1, it2, it3], null));
    await act(async () => { bumpTick(rerender, 't1'); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 2, 3]);
  });

  it('surfaces a not-found error on 404 and leaves detail null', async () => {
    mockOnce({}, 404);
    const { result } = renderHook(() => useConversation('missing'));
    await waitFor(() => expect(result.current.error).toBe('Conversation not found.'));
    expect(result.current.detail).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  // #217 S3 E2 — loadToTarget (replaces loadUntil + loadToEnd) forward-paging.
  // Outline turns for direction resolution (u1 at index 0, u2 at index 1).
  const fwdOutline = [
    { uuid: 'u1', kind: 'human', member_uuids: ['u1'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
    { uuid: 'u2', kind: 'assistant', member_uuids: ['u2', 'u2b'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
  ] as OutlineTurn[];

  it('loadToTarget pages forward until the target uuid is loaded (replaces loadUntil)', async () => {
    mockOnce(detail([it1], 2));
    const { result } = renderHook(() => useConversation('s', { outlineTurns: fwdOutline }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    mockOnce(detail([it2], null));         // u2b is a member of it2 (outline turn u2, index 1, below)
    await act(async () => { await result.current.loadToTarget('u2b'); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
  });

  it('loadToTarget terminates at exhaustion when the target never appears in the window', async () => {
    // The target IS an outline turn (so direction resolves) but its page never
    // arrives before the forward cursor exhausts — loadToTarget must resolve, not
    // hang, once next_after goes null.
    const exhaustOutline = [
      ...fwdOutline,
      { uuid: 'u9', kind: 'human', member_uuids: ['u9'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
    ] as OutlineTurn[];
    mockOnce(detail([it1], 2));            // page 1, more to come
    const { result } = renderHook(() => useConversation('s', { outlineTurns: exhaustOutline }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    mockOnce(detail([it2], null));         // page 2 exhausts; u9 never arrives
    await act(async () => { await result.current.loadToTarget('u9'); });
    // Exactly 2 fetches: the page-1 effect + one forward page that exhausted.
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(2);
  });

  it('loadToTarget drains an unbounded number of forward pages (no 20-page cap; jump-to-latest spec §5)', async () => {
    // A conversation whose pages exceed the old 20-cap: page 1 + 25 subsequent
    // pages, each carrying ONE item. loadToTarget(last) must drain ALL pages so
    // the final turn lands — no cap (paging strictly advances toward the target).
    const longOutline = Array.from({ length: 26 }, (_, i) => ({
      uuid: `u${i + 1}`, kind: 'human', member_uuids: [`u${i + 1}`],
      subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '',
    })) as OutlineTurn[];
    const page = (id: number, more: boolean) => {
      const item = {
        kind: 'human', anchor: { session_id: 'sess-long', uuid: `u${id}`, id },
        member_uuids: [`u${id}`], ts: 't', text: `m${id}`, blocks: [], is_sidechain: false,
      };
      return detail([item], more ? id + 1 : null, { session_id: 'sess-long' });
    };
    mockOnce(page(1, true));                       // page 1 (effect-loaded)
    const { result } = renderHook(() => useConversation('sess-long', { outlineTurns: longOutline }));
    await waitFor(() => expect(result.current.detail).not.toBeNull());
    for (let id = 2; id <= 26; id++) mockOnce(page(id, id < 26));
    await act(async () => { await result.current.loadToTarget('u26'); });
    expect(result.current.detail!.items.length).toBe(26);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.detail!.items.at(-1)!.anchor.id).toBe(26);
  });

  it('never exposes the previous session\'s detail under the new sid on switch (#183 stale-media 404)', async () => {
    // REGRESSION (cross-session stale-media 404, useConversation.ts derive guard):
    // The fetch effect clears `detail` only in the POST-commit passive phase, so
    // the render right after `sessionId` changes used to return the previous
    // session's `detail` for one commit — while TranscriptContext already carried
    // the new sid, so an auto-fetching MediaFigure built /<newSid>/media?tool_use_id
    // =<oldId> → 404. A setState-in-render reset does NOT fix it (the first render
    // of the new session still returns the stale value); the fix DERIVES the
    // exposed detail in the same render pass (only surface it when the
    // REQUESTED loadedSessionId === sessionId — keyed on the requested sid,
    // not the body's self-reported session_id).
    //
    // We trace EVERY render the hook produces and assert no render that requested
    // s2 ever carried a detail belonging to s1. The new session's fetch is
    // deferred (never resolves) so ONLY a stale-detail leak — not fresh data —
    // could populate detail under s2. RED without the guard: a render with
    // {sid:'s2', detail:<s1>} appears.
    const renders: Array<{ sid: string; detailSid: string | null }> = [];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/api/conversation/s1')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1], null) } as Response);
      }
      return new Promise(() => {}); // s2 page-1 never resolves
    });
    const { result, rerender } = renderHook(
      ({ sid }) => {
        const r = useConversation(sid);
        renders.push({ sid, detailSid: r.detail?.session_id ?? null });
        return r;
      },
      { initialProps: { sid: 's1' } },
    );
    await waitFor(() => expect(result.current.detail?.session_id).toBe('s'));

    // Switch to s2. Across every render the hook produces for s2, detail must be
    // null (loading) — never s1's detail. The fix's derive guard makes this hold
    // in the SAME render pass, not one commit later.
    act(() => { rerender({ sid: 's2' }); });
    const s2Renders = renders.filter((r) => r.sid === 's2');
    expect(s2Renders.length).toBeGreaterThan(0);
    expect(s2Renders.every((r) => r.detailSid === null)).toBe(true);
    expect(result.current.detail).toBeNull();
    expect(result.current.loading).toBe(true);
  });

  it('drops a stale cross-session page that resolves after the session changed', async () => {
    // REGRESSION (cross-session clobber guard, useConversation.ts ~L76):
    //   if (sessionRef.current !== sid) return false;
    // Scenario: s1 page-1 loads (has a cursor). A loadMore() kicks off s1's
    // page-2 fetch but we hold its resolver. We then switch the hook to s2,
    // let s2's page-1 resolve, and ONLY THEN resolve s1's slow page-2. The
    // late s1 page must NOT be appended onto s2's detail — the guard drops it.
    //
    // We drive fetch with per-URL deferred resolvers so the s1 page-2 promise
    // stays pending across the session swap, deterministically.
    const deferred: Record<string, { resolve: (body: unknown) => void }> = {};
    const sN1 = { ...it1, anchor: { ...it1.anchor, session_id: 's2' }, member_uuids: ['s2-u1'] };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      // Page-1 loads (no `after=`) resolve immediately; the s1 page-2 load
      // (`after=`) is deferred so we control exactly when it lands.
      if (url.includes('/api/conversation/s1') && !url.includes('after=')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([it1], 2) } as Response);
      }
      if (url.includes('/api/conversation/s2')) {
        return Promise.resolve({ ok: true, status: 200, json: async () => detail([sN1], null) } as Response);
      }
      // s1 page-2 (the slow one) — return a promise we resolve by hand.
      return new Promise((resolve) => {
        deferred[url] = { resolve: (body: unknown) => resolve({ ok: true, status: 200, json: async () => body } as Response) };
      });
    });

    const { result, rerender } = renderHook(({ sid }) => useConversation(sid), { initialProps: { sid: 's1' } });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));
    expect(result.current.detail?.items[0].anchor.session_id).toBe('s');

    // Kick off s1's page-2 (deferred — promise registered, not yet resolved).
    let morePromise!: Promise<void>;
    act(() => { morePromise = result.current.loadMore(); });
    await waitFor(() => expect(Object.keys(deferred).some((u) => u.includes('after='))).toBe(true));

    // Switch session to s2 while s1 page-2 is still in flight; let s2 page-1 land.
    rerender({ sid: 's2' });
    await waitFor(() => expect(result.current.detail?.items[0]?.anchor.session_id).toBe('s2'));
    expect(result.current.detail?.items).toHaveLength(1);

    // NOW resolve the stale s1 page-2. Without the guard this would append
    // s1's items onto s2's detail; with it, the page is dropped.
    const staleUrl = Object.keys(deferred).find((u) => u.includes('after='))!;
    await act(async () => { deferred[staleUrl].resolve(detail([it2], null)); await morePromise; });

    // s2's detail is untouched: still exactly s2's page-1, no s1 items appended.
    expect(result.current.detail?.items).toHaveLength(1);
    expect(result.current.detail?.items.map((i) => i.anchor.session_id)).toEqual(['s2']);
  });
});

// live-tail spec §3.1 — the dedicated per-conversation EventSource. Opens
// `/api/conversation/<id>/events`, fires the EXISTING pollTail() on a `tail`
// ping (and on `open`, for (re)connect catch-up), gated on transcriptsEnabled
// + selectLiveTailEnabled. The slow generated_at backstop above stays
// untouched.
describe('useConversation live-tail EventSource', () => {
  it('opens a per-conversation EventSource and pollTails on a tail ping', async () => {
    mockOnce(detail([it1, it2], null));
    const { result } = renderHook(() => useConversation('s'));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    const es = MockEventSource.instances.find((e) => e.url.includes('/api/conversation/s/events'));
    expect(es).toBeTruthy();
    // A tail ping triggers pollTail(), which fetches the §6 overlap window
    // (cursor null — 2 items ≤ TAIL_WINDOW) and appends the genuinely-new turn.
    mockOnce(detail([it1, it2, it3], null));
    await act(async () => { es!.emit('tail', { sessionId: 's' }); await Promise.resolve(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));
    expect(result.current.detail?.items.map((i) => i.anchor.id)).toEqual([1, 2, 3]);
    expect(String((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0])).not.toContain('after=');
  });

  it('closes the EventSource when the session changes', async () => {
    mockOnce(detail([], null));
    const { rerender } = renderHook(({ id }) => useConversation(id), { initialProps: { id: 's' } });
    await waitFor(() =>
      expect(MockEventSource.instances.some((e) => e.url.includes('/api/conversation/s/events'))).toBe(true),
    );
    const first = MockEventSource.instances.find((e) => e.url.includes('/api/conversation/s/events'))!;
    mockOnce(detail([], null));
    rerender({ id: 's2' });
    expect(first.closed).toBe(true);
    await waitFor(() =>
      expect(MockEventSource.instances.some((e) => e.url.includes('/api/conversation/s2/events'))).toBe(true),
    );
  });

  it('does NOT open an EventSource when live_tail is off', async () => {
    mockLiveTail = false;
    mockOnce(detail([], null));
    renderHook(() => useConversation('s'));
    await waitFor(() =>
      expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true),
    );
  });

  it('does NOT open an EventSource when transcripts are disabled', async () => {
    mockTranscripts = false;
    mockOnce(detail([], null));
    renderHook(() => useConversation('s'));
    await waitFor(() =>
      expect(MockEventSource.instances.every((e) => !e.url.includes('/events'))).toBe(true),
    );
  });
});

// #217 S3 E2 — the bidirectional windowed pager: open-at-bottom (?tail=1),
// reverse paging (loadPrev → ?before=), a unified loadToTarget(uuid) with
// nearest-edge direction + member-uuid resolution, and jump-to-latest = ?tail=1.
// The hook now takes an `openIntent` (so the FIRST fetch is precedence-correct,
// Codex P1) and `outlineTurns` (for loadToTarget's direction decision).
describe('useConversation — bidirectional windowed pager (#217 S3 E2)', () => {
  // A detail with explicit top + bottom edge keys.
  function pageDetail(
    items: unknown[],
    edges: { next_after?: number | null; has_more?: boolean; prev_before?: number | null; has_prev?: boolean },
    over: Record<string, unknown> = {},
  ) {
    return {
      session_id: 's', project_label: 'p', git_branch: null,
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 3, models: ['opus'], items,
      page: {
        next_after: edges.next_after ?? null,
        has_more: edges.has_more ?? (edges.next_after != null),
        prev_before: edges.prev_before ?? null,
        has_prev: edges.has_prev ?? false,
      },
      ...over,
    };
  }
  // Outline turns the hook uses for loadToTarget direction decisions. Each turn's
  // own uuid + member uuids (it2 folds 'u2b').
  const outlineTurns: OutlineTurn[] = [
    { uuid: 'u1', kind: 'human', member_uuids: ['u1'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
    { uuid: 'u2', kind: 'assistant', member_uuids: ['u2', 'u2b'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
    { uuid: 'u3', kind: 'assistant', member_uuids: ['u3'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
    { uuid: 'u4', kind: 'human', member_uuids: ['u4'], subagent_key: null, parent_uuid: null, is_sidechain: false, ts: null, label: '' },
  ];

  function lastUrl(): string {
    return String((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![0]);
  }

  it('open-at-bottom: a multi-page session fetches ?tail=1 and lands on the tail window (bottom edge)', async () => {
    // tail page: the LAST window. has_prev:true (multi-page) so the top edge is
    // armed; the bottom edge has nothing more (it IS the tail).
    mockOnce(pageDetail([it3, it4], { next_after: null, has_more: false, prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    // FIRST request is ?tail=1 (no head-fetch first).
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('tail=1');
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).not.toContain('after=');
    // Bottom edge: nothing more (live-tail eligible). Top edge: armed.
    expect(result.current.hasMore).toBe(false);
    expect(result.current.hasPrev).toBe(true);
    expect(result.current.prevBefore).toBe(3);
    // Multi-page (has_prev) ⇒ land at the bottom.
    expect(result.current.openScrollIntent).toBe('bottom');
  });

  it('open-at-bottom: a single-page session (?tail=1 → has_prev:false) keeps all items and reports "top"', async () => {
    mockOnce(pageDetail([it1, it2, it3], { next_after: null, has_more: false, prev_before: null, has_prev: false }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(3));
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain('tail=1');
    expect(result.current.hasMore).toBe(false);
    expect(result.current.hasPrev).toBe(false);
    // Single page (everything fits) ⇒ read from the start.
    expect(result.current.openScrollIntent).toBe('top');
  });

  it('loadPrev prepends and never disturbs the bottom edge / live-tail gate (Codex P1)', async () => {
    // tail open: window = [it3, it4]; top edge armed (prev_before 3), bottom edge done.
    mockOnce(pageDetail([it3, it4], { next_after: null, has_more: false, prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    expect(result.current.hasMore).toBe(false);

    // The before-page response carries a NON-NULL next_after / has_more:true for
    // the items AFTER it (already loaded). Storing it wholesale would flip the
    // reader to "not at tail" and kill live-tail. loadPrev must update ONLY the
    // top edge.
    mockOnce(pageDetail([it1, it2], { next_after: 99, has_more: true, prev_before: 1, has_prev: true }));
    await act(async () => { await result.current.loadPrev(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(4));

    // ?before=<prev_before of the tail page> = before=3.
    expect(lastUrl()).toContain('before=3');
    // Prepended in order: [it1, it2, it3, it4].
    expect(result.current.detail?.items.map((i: { anchor: { id: number } }) => i.anchor.id)).toEqual([1, 2, 3, 4]);
    // Bottom edge UNTOUCHED despite the before-page's next_after:99 / has_more:true.
    expect(result.current.hasMore).toBe(false);
    // Top edge advanced to the before-page's prev_before.
    expect(result.current.prevBefore).toBe(1);
    expect(result.current.hasPrev).toBe(true);
  });

  it('loadPrev stops at the top edge when has_prev is false', async () => {
    mockOnce(pageDetail([it3, it4], { prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    // The before-page reaches the head: has_prev:false, prev_before:null.
    mockOnce(pageDetail([it1, it2], { prev_before: null, has_prev: false }));
    await act(async () => { await result.current.loadPrev(); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(4));
    expect(result.current.hasPrev).toBe(false);
    // A further loadPrev is a no-op (no cursor) — no new fetch.
    const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    await act(async () => { await result.current.loadPrev(); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
  });

  it('loadToTarget pages HEAD-WARD (before) for an early target from a tail window', async () => {
    // Open at the tail with only the last 2 items loaded ([it3,it4]); the target
    // is u1 (outline index 0 — earliest), above the window → page via ?before=.
    mockOnce(pageDetail([it3, it4], { prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    mockOnce(pageDetail([it1, it2], { prev_before: null, has_prev: false }));
    await act(async () => { await result.current.loadToTarget('u1'); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(4));
    // The direction was HEAD-WARD: a ?before= request, never ?after=.
    expect(lastUrl()).toContain('before=3');
    expect(result.current.detail?.items.some((i: { anchor: { uuid: string } }) => i.anchor.uuid === 'u1')).toBe(true);
  });

  it('loadToTarget resolves a MEMBER (folded-fragment) uuid to its owning turn (Codex P1)', async () => {
    // u2b is a folded fragment of turn u2 (outline index 1). It is NOT an outline
    // turn's own uuid — only resolvable via member_uuids. Targeting it from the
    // tail must still page head-ward toward turn u2.
    mockOnce(pageDetail([it3, it4], { prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    mockOnce(pageDetail([it1, it2], { prev_before: null, has_prev: false }));
    await act(async () => { await result.current.loadToTarget('u2b'); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(4));
    expect(lastUrl()).toContain('before=');
    // The owning turn u2 is now loaded (its member u2b is reachable).
    expect(result.current.detail?.items.some((i: { member_uuids: string[] }) => i.member_uuids.includes('u2b'))).toBe(true);
  });

  it('loadToTarget is a no-op when the target is already in the window', async () => {
    mockOnce(pageDetail([it3, it4], { prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    // u3 is already loaded → no paging.
    await act(async () => { await result.current.loadToTarget('u3'); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
  });

  it('loadToTarget is a graceful no-op when the uuid resolves to no outline turn', async () => {
    mockOnce(pageDetail([it3, it4], { prev_before: 3, has_prev: true }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'tail' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));
    const before = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    await act(async () => { await result.current.loadToTarget('not-in-outline'); });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before);
  });

  it('loadToTarget pages FORWARD (after) for a late target below the window', async () => {
    // Open at the HEAD (anchor intent on u1) with a forward cursor; the target u4
    // is below the window → page via ?after=.
    mockOnce(pageDetail([it1, it2], { next_after: 2, has_more: true, prev_before: null, has_prev: false }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'anchor', uuid: 'u1' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    mockOnce(pageDetail([it3, it4], { next_after: null, has_more: false, prev_before: null, has_prev: false }));
    await act(async () => { await result.current.loadToTarget('u4'); });
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(4));
    expect(lastUrl()).toContain('after=2');
    expect(lastUrl()).not.toContain('before=');
  });

  it('jump-to-latest issues ?tail=1 (a reset, not a forward drain)', async () => {
    // Open at the head, partially paged (bottom edge has more).
    mockOnce(pageDetail([it1, it2], { next_after: 2, has_more: true, prev_before: null, has_prev: false }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'anchor', uuid: 'u1' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(2));

    // jumpToLatest replaces the window with the tail page in ONE request.
    mockOnce(pageDetail([it3, it4], { next_after: null, has_more: false, prev_before: 3, has_prev: true }));
    await act(async () => { await result.current.jumpToLatest(); });
    await waitFor(() => expect(result.current.detail?.items.map((i: { anchor: { id: number } }) => i.anchor.id)).toEqual([3, 4]));
    // It was a ?tail=1 reset — NOT an ?after= forward drain.
    expect(lastUrl()).toContain('tail=1');
    expect(lastUrl()).not.toContain('after=');
    // Landed at the bottom with live-tail eligible.
    expect(result.current.hasMore).toBe(false);
  });

  it('preserves the overlap-race disambiguation across edges (a loadToTarget mid-load awaits the in-flight load)', async () => {
    // Port of the loadToEnd overlap-race test to the unified pager. A forward
    // loadMore is in flight (loadingMoreRef set) when loadToTarget(lateUuid)
    // fires; loadToTarget must await the in-flight settle, not mistake the early
    // `false` for end-of-conversation.
    mockOnce(pageDetail([it1], { next_after: 1, has_more: true, prev_before: null, has_prev: false }));
    const { result } = renderHook(() => useConversation('s', { outlineTurns, openIntent: { kind: 'anchor', uuid: 'u1' } }));
    await waitFor(() => expect(result.current.detail?.items).toHaveLength(1));

    // Arm a controllable page for the OVERLAPPING loadMore (resolves on command).
    let resolveInflight!: () => void;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementationOnce(
      () => new Promise((res) => { resolveInflight = () => res({ ok: true, status: 200, json: async () => pageDetail([it2], { next_after: 2, has_more: true }) } as Response); }),
    );
    // Queue the pages loadToTarget will drain after the overlap (it3 then it4 exhausts).
    mockOnce(pageDetail([it3], { next_after: 3, has_more: true }));
    mockOnce(pageDetail([it4], { next_after: null, has_more: false }));

    await act(async () => {
      const inflight = result.current.loadMore();          // loadingMoreRef set, not awaited
      const toTarget = result.current.loadToTarget('u4');  // fires mid-load (the race)
      resolveInflight();
      await inflight;
      await toTarget;
    });

    // loadToTarget drained every forward page (2,3,4 appended to 1) despite the
    // overlap — it did NOT conclude "exhausted" at page 1.
    expect(result.current.detail?.items.map((i: { anchor: { id: number } }) => i.anchor.id)).toEqual([1, 2, 3, 4]);
    expect(result.current.hasMore).toBe(false);
  });
});
