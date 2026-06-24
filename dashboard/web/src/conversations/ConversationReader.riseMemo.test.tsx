// #231 regression — a reverse-page PREPEND must NOT re-render the turns already
// on screen. Before the fix, `riseFor` returned `conv-rise` (+ a fresh style
// object) for a not-yet-seen turn and `['', undefined]` once it was marked seen;
// because the seen-marking is a ref mutation (no re-render), the flip was deferred
// to the next commit — a prepend — at which point EVERY retained turn's className
// AND style changed at once, defeating the MessageItem React.memo for the whole
// window (an O(n²) re-render cascade = the cold-load freeze). The fix freezes each
// turn's rise decision per uuid so its className/style identity is stable across
// commits. This test drives a real prepend (#232: via Virtuoso's startReached)
// and asserts the already-mounted turns are NOT re-rendered.
import { act, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { _resetForTests, dispatch } from '../store/store';
import { clearReadingPositions } from '../store/readingPosition';
import type { ConversationItem, ConversationOutline, OutlineTurn } from '../types/conversation';

// #232 — render-all react-virtuoso mock (mirrors ConversationReader.test.tsx):
// real Virtuoso renders nothing in JSDOM (zero layout), so this passthrough
// renders EVERY item — keeping the memo-defeat render-count assertions valid —
// and exposes `startReached` (the reverse-paging trigger that replaces the old
// top-sentinel IntersectionObserver).
const virtuosoTestHandle: { firstItemIndex: number; startReached: (() => void) | null; atBottomStateChange: ((b: boolean) => void) | null } = {
  firstItemIndex: 0, startReached: null, atBottomStateChange: null,
};
vi.mock('react-virtuoso', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  const Virtuoso = React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
    React.useImperativeHandle(ref, () => ({ scrollToIndex: vi.fn(), scrollIntoView: vi.fn(), scrollBy: vi.fn(), scrollTo: vi.fn() }), []);
    const data = (props.data as unknown[]) ?? [];
    const itemContent = props.itemContent as (index: number, datum: unknown) => React.ReactNode;
    const computeItemKey = props.computeItemKey as ((index: number, datum: unknown) => React.Key) | undefined;
    const components = (props.components as { List?: unknown; Item?: unknown }) ?? {};
    const firstItemIndex = (props.firstItemIndex as number) ?? 0;
    const scrollerRef = props.scrollerRef as ((el: unknown) => void) | undefined;
    const List = (components.List ?? 'div') as React.ElementType;
    const Item = (components.Item ?? 'div') as React.ElementType;
    const scroller = React.useRef<HTMLDivElement>(null);
    virtuosoTestHandle.firstItemIndex = firstItemIndex;
    virtuosoTestHandle.startReached = (props.startReached as (() => void)) ?? null;
    virtuosoTestHandle.atBottomStateChange = (props.atBottomStateChange as ((b: boolean) => void)) ?? null;
    React.useEffect(() => {
      scrollerRef?.(scroller.current);
      (props.itemsRendered as ((items: unknown[]) => void) | undefined)?.(data.map((d, i) => ({ index: firstItemIndex + i, data: d })));
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [data.length]);
    return React.createElement(
      'div',
      { ref: scroller, className: props.className as string, role: props.role as string, onScroll: props.onScroll as React.UIEventHandler },
      React.createElement(List, {}, data.map((d, i) =>
        React.createElement(Item as React.ElementType,
          { key: computeItemKey ? computeItemKey(firstItemIndex + i, d) : i, 'data-index': firstItemIndex + i },
          itemContent(firstItemIndex + i, d)))),
    );
  });
  return { Virtuoso, VirtuosoMockContext: React.createContext(undefined) };
});

// Count every render of each MessageItem by uuid, INSIDE a memo boundary that
// mirrors the real component's default shallow-prop memo — so a count only ticks
// when the turn's props actually change (i.e. the memo is defeated).
const h = vi.hoisted(() => ({ renders: new Map<string, number>() }));
vi.mock('./MessageItem', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./MessageItem')>();
  const React = await import('react');
  const Counting = React.memo(
    React.forwardRef<HTMLDivElement, { item?: { anchor?: { uuid?: string } } }>((props, ref) => {
      const uuid = props?.item?.anchor?.uuid ?? '?';
      h.renders.set(uuid, (h.renders.get(uuid) ?? 0) + 1);
      return React.createElement(actual.MessageItem, { ...(props as object), ref } as never);
    }),
  );
  return { ...actual, MessageItem: Counting };
});

// A controllable IntersectionObserver: records each observer's callback + targets
// so a test can fire the top "Load earlier" sentinel's intersection deterministically
// (jsdom has no real layout/scroll).
const observers: { cb: IntersectionObserverCallback; targets: Element[] }[] = [];
class ControllableIO {
  cb: IntersectionObserverCallback;
  targets: Element[] = [];
  constructor(cb: IntersectionObserverCallback) { this.cb = cb; observers.push(this); }
  observe(el: Element): void { this.targets.push(el); }
  unobserve(el: Element): void { this.targets = this.targets.filter((t) => t !== el); }
  disconnect(): void { this.targets = []; }
  takeRecords(): IntersectionObserverEntry[] { return []; }
}
let _id = 1;
function turn(uuid: string): ConversationItem {
  return {
    kind: uuid.startsWith('a') ? 'assistant' : 'human',
    anchor: { session_id: 's', uuid, id: _id++ },
    member_uuids: [uuid], ts: 't', text: uuid, blocks: [],
    is_sidechain: false, subagent_key: null, parent_uuid: null,
  } as ConversationItem;
}
function pageBody(items: ConversationItem[], prev_before: number | null) {
  return {
    session_id: 's', project_label: 'p', git_branch: 'main',
    started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 1, models: ['claude-opus-4'], items,
    page: { next_after: null, has_more: false, prev_before, has_prev: prev_before != null },
  };
}
function mockFetchOnce(body: unknown) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: true, status: 200, json: async () => body } as Response);
}
const flush = async () => { await act(async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); }); };

beforeEach(() => {
  _resetForTests();
  globalThis.fetch = vi.fn();
  (globalThis as unknown as { IntersectionObserver: typeof ControllableIO }).IntersectionObserver = ControllableIO;
  clearReadingPositions();
  observers.length = 0;
  h.renders.clear();
  _id = 1;
  virtuosoTestHandle.firstItemIndex = 0;
  virtuosoTestHandle.startReached = null;
  virtuosoTestHandle.atBottomStateChange = null;
});
afterEach(() => { _resetForTests(); clearReadingPositions(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('#231 — a reverse-page prepend does not re-render already-mounted turns', () => {
  it('keeps every retained turn out of the re-render when older turns prepend', async () => {
    const { ConversationReader } = await import('./ConversationReader');
    const tail = Array.from({ length: 20 }, (_, i) => turn(`${i % 2 ? 'a' : 'h'}${i}`));
    mockFetchOnce(pageBody(tail, 1000));            // has_prev → reverse cursor armed + top sentinel rendered
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelectorAll('[data-uuid]').length).toBe(20));
    await flush();

    // Snapshot render counts of the mounted (tail) turns AFTER the initial settle.
    const before = new Map(h.renders);
    const tailUuids = tail.map((t) => t.anchor.uuid);
    expect(tailUuids.every((u) => before.has(u))).toBe(true);

    // Prepend an older page by firing Virtuoso's startReached (#232 — replaces
    // the old top "Load earlier" sentinel intersection).
    const older = Array.from({ length: 15 }, (_, i) => turn(`o${i}`));
    mockFetchOnce(pageBody(older, 500));
    // #232 — arm paging (the open settles → atBottomStateChange) before firing
    // startReached; the freeze guard no-ops paging until the open has settled.
    await act(async () => { virtuosoTestHandle.atBottomStateChange?.(true); virtuosoTestHandle.startReached?.(); for (let i = 0; i < 16; i++) await Promise.resolve(); });
    await flush();

    // The prepend really happened: older turns mounted, total grew, head changed.
    expect(container.querySelectorAll('[data-uuid]').length).toBe(35);
    expect(container.querySelector('[data-uuid]')?.getAttribute('data-uuid')?.startsWith('o')).toBe(true);
    expect(older.every((o) => h.renders.has(o.anchor.uuid))).toBe(true);

    // THE REGRESSION GUARD: not one retained turn re-rendered because of the prepend.
    const rerendered = tailUuids.filter((u) => (h.renders.get(u) ?? 0) > (before.get(u) ?? 0));
    expect(rerendered).toEqual([]);
  });

  it('cold deep-link to a head-ward turn drains backward, lands the target, and keeps renders bounded', async () => {
    const { ConversationReader } = await import('./ConversationReader');
    // Full-session outline: 15 older turns (o0..o14) then 20 tail turns (t0..t19).
    // The deep-link target o5 sits ABOVE the tail window → loadToTarget must drain
    // BACKWARD (the cold deep-link path, #231) rather than no-op or page forward.
    const older = Array.from({ length: 15 }, (_, i) => turn(`o${i}`));
    const tail = Array.from({ length: 20 }, (_, i) => turn(`t${i}`));
    const toOutlineTurn = (it: ConversationItem): OutlineTurn => ({
      uuid: it.anchor.uuid, kind: it.kind, ts: 't', label: it.anchor.uuid,
      member_uuids: [it.anchor.uuid], subagent_key: null, parent_uuid: null, is_sidechain: false,
    } as OutlineTurn);
    const outline = {
      session_id: 's', stats: {} as never,
      turns: [...older, ...tail].map(toOutlineTurn),
      task_completion: { all_done: false, total: 35, completed: 0, anchor_uuid: 't19' },
    } as ConversationOutline;

    mockFetchOnce(pageBody(tail, 1000));     // initial tail open, has_prev → backward cursor armed
    mockFetchOnce(pageBody(older, null));    // the ?before= drain page carrying the target o5
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'o5' } });
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    const { container } = render(<ConversationReader sessionId="s" outline={outline} />);
    // The backward drain (?before=) pages the head-ward older page in and MOUNTS
    // the deep-link target — directly refuting the "drain never fires / target
    // never lands" failure mode. (The scroll/flash on the resolved ref is covered
    // by the forward-jump test; it isn't re-asserted here because the render-count
    // mock wrapper this file installs doesn't forward the item ref.)
    await waitFor(() => expect(container.querySelector('[data-uuid="o5"]')).not.toBeNull());
    await flush();

    // The whole session is now in-window (35 < cap → no trim).
    expect(container.querySelectorAll('[data-uuid]').length).toBe(35);

    // Renders stayed bounded: the backward drain did NOT re-render the tail turns
    // for every page it pulled in (the #231 cascade). Each turn renders a small,
    // constant number of times regardless of window size.
    const worst = Math.max(...tail.map((t) => h.renders.get(t.anchor.uuid) ?? 0));
    expect(worst).toBeLessThanOrEqual(2);
  });
});
