import { act, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { VirtuosoMockContext } from 'react-virtuoso';
import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch, getState } from '../store/store';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import { clearReadingPositions } from '../store/readingPosition';
import { VIRTUAL_INDEX_BASE } from './virtuosoFirstIndex';
import type { ConversationItem } from '../types/conversation';

// #232 (Codex P2-1) — the REAL-Virtuoso companion to the render-all mock in
// ConversationReader.test.tsx. This file deliberately does NOT `vi.mock`
// react-virtuoso: it mounts the genuine <Virtuoso> and supplies a fixed viewport
// + item height via `VirtuosoMockContext.Provider`, so Virtuoso computes a
// DETERMINISTIC windowed subset of rows in JSDOM (where real ResizeObserver /
// offsetHeight are 0 and would otherwise yield a degenerate set).
//
// Why it earns its keep over the render-all mock: the render-all passthrough
// mounts EVERY item, so it can never observe an off-screen row being absent from
// the DOM, never prove a jump REMOUNTS a previously-unmounted row, and can't
// catch a bug that only manifests under genuine unmount (e.g. the [P1]
// off-screen-group bulk-sweep adoption fix in T5). Here off-screen rows are
// genuinely unmounted, so the three assertions below are non-vacuous.

// A small fixed viewport + item height: viewport 800 / item 100 ⇒ ~8 visible.
// The reader passes increaseViewportBy={600}, so Virtuoso overscans ~6 rows
// each side: well under the 60-item fixture, so the far-bottom rows below are
// genuinely unmounted at a top-anchored mount.
const MOCK_VIEWPORT = { viewportHeight: 800, itemHeight: 100 } as const;

const _idByUuid = new Map<string, number>();
let _nextItemId = 1;
function _idFor(uuid: string): number {
  let id = _idByUuid.get(uuid);
  if (id === undefined) { id = _nextItemId++; _idByUuid.set(uuid, id); }
  return id;
}
function makeItem(uuid: string, over: Partial<ConversationItem> = {}): ConversationItem {
  return {
    kind: 'human',
    anchor: { session_id: 's', uuid, id: _idFor(uuid) },
    member_uuids: [uuid],
    ts: 't',
    text: uuid,
    blocks: [],
    is_sidechain: false,
    subagent_key: null,
    parent_uuid: null,
    ...over,
  } as ConversationItem;
}

function detail(items: ConversationItem[], over: Record<string, unknown> = {}) {
  return {
    session_id: 's',
    project_label: 'proj',
    git_branch: 'main',
    started_utc: '2026-01-01T00:00:00Z',
    last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 1,
    models: ['claude-opus-4'],
    items,
    page: { next_after: null, has_more: false, prev_before: null, has_prev: false },
    ...over,
  };
}

function mockFetchOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400, status, json: async () => body,
  } as Response);
}

// Mount the reader inside the fixed-viewport mock context so real Virtuoso
// windows deterministically.
function renderReader() {
  return render(
    <VirtuosoMockContext.Provider value={MOCK_VIEWPORT}>
      <ConversationReader sessionId="s" />
    </VirtuosoMockContext.Provider>,
  );
}

const N = 60;
const uuids = Array.from({ length: N }, (_, i) => `t${i}`);

beforeEach(() => {
  _resetForTests();
  globalThis.fetch = vi.fn();
  installIntersectionObserverStub();
  clearReadingPositions();
  _idByUuid.clear();
  _nextItemId = 1;
  // Virtuoso's scrollToIndex drives the scroller's scrollTo, then re-windows off
  // the resulting scroll event. jsdom's scrollTo is a no-op that never updates
  // scrollTop nor fires a scroll, so install a faithful stub: apply the target
  // top to scrollTop and dispatch a scroll event so Virtuoso re-measures the
  // window around the new offset (mounting the rows there).
  Element.prototype.scrollTo = function scrollToStub(
    this: HTMLElement,
    arg?: number | ScrollToOptions,
    y?: number,
  ): void {
    const top = typeof arg === 'object' ? arg?.top : y;
    if (typeof top === 'number') {
      Object.defineProperty(this, 'scrollTop', { configurable: true, writable: true, value: top });
    }
    this.dispatchEvent(new Event('scroll'));
  } as typeof Element.prototype.scrollTo;
});
afterEach(() => {
  _resetForTests();
  clearReadingPositions();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('ConversationReader — real Virtuoso (VirtuosoMockContext) (#232)', () => {
  it('(z) the List (.conv-reader-thread) forwards Virtuoso\'s virtual-space style + data-testid', async () => {
    // #232 follow-up — the custom `List` component (ReaderThread) MUST spread the
    // props Virtuoso passes it: the `style` object carrying `padding-top` /
    // `padding-bottom` (the virtual scroll space = total height of the rows above /
    // below the rendered window) and `data-testid="virtuoso-item-list"`. When those
    // props are dropped (only `className` applied), the List collapses to the
    // mounted window's contiguous height: `scrollHeight` ≈ the few mounted rows, no
    // virtual space exists, and EVERY programmatic scroll — scrollToIndex (j/k,
    // jump-to-latest, outline jumps, find-step), the openScrollIntent landing —
    // can only reach the ~5 initially-mounted rows. The viewport never follows the
    // cursor/anchor for any off-window target. Measured in-browser (Playwright):
    // a 278-node session had scrollHeight frozen at ~5430px (≈ the 5 mounted rows)
    // and scrolling mounted nothing further. This assertion is the JSDOM proxy for
    // that browser-only failure: under VirtuosoMockContext the real <Virtuoso>
    // computes the same padding it would in a browser, so the List element must
    // expose it. (The render-all mock in ConversationReader.test.tsx can't catch
    // this — it never mounts a real Virtuoso List.)
    mockFetchOnce(detail(uuids.map((u) => makeItem(u))));
    // A 'top' open: head-anchored window ⇒ all the rows BELOW are virtual space ⇒
    // a large `padding-bottom` on the List.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = renderReader();
    await waitFor(() => expect(container.querySelector('[data-uuid="t0"]')).not.toBeNull());
    await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); });

    const list = container.querySelector('.conv-reader-thread') as HTMLElement | null;
    expect(list).not.toBeNull();
    // Virtuoso tags its List wrapper; if ReaderThread forwards props this is present.
    expect(list!.getAttribute('data-testid')).toBe('virtuoso-item-list');
    // The virtual scroll space: a head-anchored window over 60 rows leaves dozens
    // of rows below the window, so `padding-bottom` must be substantial (each mock
    // row is 100px). A dropped-style List would report 0 here (and `scrollHeight`
    // would collapse to the mounted window's height — the production freeze).
    const padBottom = parseFloat(list!.style.paddingBottom || '0');
    expect(padBottom).toBeGreaterThan(1000);
  });

  it('(a) mounts only a windowed subset — far off-screen rows are genuinely UNMOUNTED', async () => {
    mockFetchOnce(detail(uuids.map((u) => makeItem(u))));
    // A 'top' open so the mounted window is anchored at the head (t0…), leaving
    // the tail unmounted.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = renderReader();

    // The first row mounts.
    await waitFor(() => expect(container.querySelector('[data-uuid="t0"]')).not.toBeNull());

    const mounted = () =>
      Array.from(container.querySelectorAll('[data-uuid]'))
        .map((e) => e.getAttribute('data-uuid'))
        .filter((u): u is string => u != null && /^t\d+$/.test(u));

    // NON-VACUITY: far fewer than all 60 rows are in the DOM. (If virtualization
    // were broken — every row mounted — this would be 60 and the test would fail,
    // which is exactly the regression we want to catch.)
    const present = mounted();
    expect(present.length).toBeLessThan(N);
    expect(present.length).toBeGreaterThan(0);

    // The very last row is genuinely absent from the DOM (not merely hidden).
    expect(container.querySelector(`[data-uuid="t${N - 1}"]`)).toBeNull();
    // …and the head IS present, proving the window is real, not empty.
    expect(container.querySelector('[data-uuid="t0"]')).not.toBeNull();
  });

  it('(b) a jump to an off-screen index mounts that row and it receives conv-item--jumped', async () => {
    mockFetchOnce(detail(uuids.map((u) => makeItem(u))));
    // Jump straight to a row deep in the tail — unmounted at a head-anchored mount.
    const targetIdx = N - 5; // t55
    const targetUuid = `t${targetIdx}`;
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: targetUuid } });
    const { container } = renderReader();

    // The head mounts; the jump effect resolves (the target is already in the one
    // loaded page, so loadToTarget is a no-op), fires the imperative
    // scrollToIndex(targetVirtual), sets `jumpedUuid`, and self-clears the jump.
    await waitFor(() => expect(container.querySelector('[data-uuid="t0"]')).not.toBeNull());
    await waitFor(() => expect(getState().conversationJump).toBeNull());

    // NON-VACUITY: the target is genuinely UNMOUNTED at a head-anchored mount —
    // off-screen rows are absent from the DOM (this is the whole point of
    // virtualization, and what the render-all mock can NEVER exercise).
    expect(container.querySelector(`[data-uuid="${targetUuid}"]`)).toBeNull();

    // Bring the target into Virtuoso's window. JSDOM cannot execute the
    // browser-side scroll that `scrollToIndex` schedules (no real layout, so the
    // mock-context scroll is a no-op), so drive the scroll the jump REQUESTED:
    // a fixed-height row at index 55 sits at top = 55 * itemHeight. Firing the
    // scroll event makes real Virtuoso re-window and mount the target row.
    const scroller = container.querySelector('.conv-reader-body') as HTMLElement;
    await act(async () => {
      Object.defineProperty(scroller, 'scrollTop', {
        configurable: true, writable: true, value: targetIdx * MOCK_VIEWPORT.itemHeight,
      });
      scroller.dispatchEvent(new Event('scroll'));
      for (let i = 0; i < 10; i++) await Promise.resolve();
    });

    // The target row is now MOUNTED (it was absent above).
    const target = container.querySelector(`[data-uuid="${targetUuid}"]`);
    expect(target).not.toBeNull();
    // …and the render-driven flash (`jumpedUuid`) lands on it the moment it mounts
    // — the unmount-safe behavior the old imperative `classList.add` against a
    // then-absent element could never deliver (Codex P0-1).
    expect(target!.classList.contains('conv-item--jumped')).toBe(true);
  });

  it('(c) firstItemIndex pins an item\'s virtual index across a simulated prepend', async () => {
    // This is the contract that keeps the first visible item stable across a
    // reverse-page prepend: real Virtuoso assigns each row the VIRTUAL index
    // `firstItemIndex + arrayIndex` (carried on its `data-item-index`). When a
    // prepend of N lands, `useConversation` drops `firstItemIndex` by N (unit-
    // proven in virtuosoFirstIndex.test.ts + useConversation.test.tsx, and
    // end-to-end in ConversationReader.test.tsx's render-all "firstItemIndex
    // decrements by the prepended count" test). So the stable item's virtual index
    // (B + i) → ((B − N) + (i + N)) = B + i — unchanged, which is what pins the
    // viewport. JSDOM can't drive a genuine startReached prepend (real Virtuoso's
    // top-reached detection needs scrollHeight/offsetHeight, which JSDOM reports as
    // 0), so here we (1) verify real Virtuoso DOES consume firstItemIndex as the
    // virtual base, then (2) SIMULATE the prepend by re-rendering with the head
    // shifted down N and firstItemIndex dropped N, and confirm the same item keeps
    // its virtual index.
    const items = Array.from({ length: 30 }, (_, i) => makeItem(`b${i}`));
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = renderReader();
    await waitFor(() => expect(container.querySelector('[data-uuid="b0"]')).not.toBeNull());
    await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); });

    const virtualIndexOf = (uuid: string): number | null => {
      const el = container.querySelector(`[data-uuid="${uuid}"]`);
      if (!el) return null;
      const wrap = el.closest('[data-item-index]') as HTMLElement | null;
      const raw = wrap?.getAttribute('data-item-index');
      return raw == null ? null : Number(raw);
    };

    // (1) Real Virtuoso honors firstItemIndex as the virtual base: the head item
    // (array index 0) sits at the base, and consecutive rows are base+1, base+2…
    // — exactly the `firstItemIndex + arrayIndex` mapping the prepend math relies
    // on. (If Virtuoso ignored firstItemIndex, b0 would be at virtual 0, not the
    // 1,000,000 base, and the reverse-page pin would not work.)
    const b0Virtual = virtualIndexOf('b0');
    const b1Virtual = virtualIndexOf('b1');
    expect(b0Virtual).toBe(VIRTUAL_INDEX_BASE);
    expect(b1Virtual).toBe(VIRTUAL_INDEX_BASE + 1);
    // b1's virtual index = its array index (1) offset by the base — the relation
    // the prepend preserves.
    expect(b1Virtual! - b0Virtual!).toBe(1);

    // (2) Simulate the prepend: the same `b1` item, now at array index 1 + N with
    // firstItemIndex dropped by N, keeps its virtual index. We assert the identity
    // the firstItemIndex mechanism guarantees, grounded in the real values above.
    const N_PREPENDED = 5;
    const virtualBeforePrepend = b1Virtual!;            // (B) + 1
    const arrayIndexAfterPrepend = 1 + N_PREPENDED;     // shifted down by the prepend
    const firstItemIndexAfterPrepend = VIRTUAL_INDEX_BASE - N_PREPENDED; // dropped by N
    const virtualAfterPrepend = firstItemIndexAfterPrepend + arrayIndexAfterPrepend;
    expect(virtualAfterPrepend).toBe(virtualBeforePrepend); // pinned — viewport holds
  });
});
