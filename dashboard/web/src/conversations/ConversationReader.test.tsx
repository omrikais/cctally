import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationReader, deriveReaderTitle } from './ConversationReader';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import { clearReadingPositions, recordReadingPos } from '../store/readingPosition';
import { stubMobileMedia, stubResponsiveMedia } from '../test-utils/mobileMedia';
import { MOBILE_MEDIA_QUERY, WIDE_MEDIA_QUERY } from '../lib/breakpoints';
import type { ConversationItem, ConversationOutline, OutlineTurn } from '../types/conversation';
import { VIRTUAL_INDEX_BASE } from './virtuosoFirstIndex';

// #232 — render-all react-virtuoso mock (Codex P2-1). Real Virtuoso needs real
// layout (ResizeObserver / offsetHeight / scrollHeight) which JSDOM reports as
// 0, so it would render a degenerate item set and every render/jump/stick
// assertion in this 2,700-line suite would go vacuous. This passthrough renders
// EVERY item (so `itemContent` runs for the whole list), forwards the
// scroller/list/item wrappers exactly as the reader supplies them (so
// `.conv-reader-body` / `.conv-reader-thread` / `[data-uuid]` selectors keep
// resolving), and exposes the imperative handle + the load/at-bottom callbacks
// the migrated tests drive. The `data-index` carries the VIRTUAL index
// (firstItemIndex + array position) so tests can assert the index math.
//
// `virtuosoTestHandle` lets a test reach the live `scrollToIndex` spy and the
// captured callbacks (startReached / endReached / atBottomStateChange) of the
// most-recently-mounted instance, and the IntersectionObserver-free top/bottom
// load triggers (startReached/endReached replace the deleted sentinel
// observers).
// #234 — the reader's jump landing WALKS Virtuoso toward the target via
// `scrollToIndex` (mounted-window steps) then writes the scroller's `scrollTop`
// directly; it no longer calls the library's `scrollIntoView`. So the shared test
// handle only needs `scrollToIndex` (plus the captured props the reader reads back).
const virtuosoTestHandle: {
  scrollToIndex: ReturnType<typeof vi.fn>;
  firstItemIndex: number;
  startReached: (() => void) | null;
  endReached: (() => void) | null;
  atBottomStateChange: ((atBottom: boolean) => void) | null;
  itemsRendered: ((items: unknown[]) => void) | null;
  followOutput: ((atBottom: boolean) => unknown) | null;
} = {
  scrollToIndex: vi.fn(),
  firstItemIndex: 0,
  startReached: null,
  endReached: null,
  atBottomStateChange: null,
  itemsRendered: null,
  followOutput: null,
};
vi.mock('react-virtuoso', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  const Virtuoso = React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
    const scrollToIndex = virtuosoTestHandle.scrollToIndex;
    React.useImperativeHandle(ref, () => ({ scrollToIndex, scrollBy: vi.fn(), scrollTo: vi.fn() }), [scrollToIndex]);
    const data = (props.data as unknown[]) ?? [];
    const itemContent = props.itemContent as (index: number, datum: unknown) => React.ReactNode;
    const computeItemKey = props.computeItemKey as ((index: number, datum: unknown) => React.Key) | undefined;
    const components = (props.components as { List?: unknown; Item?: unknown }) ?? {};
    const firstItemIndex = (props.firstItemIndex as number) ?? 0;
    const scrollerRef = props.scrollerRef as ((el: unknown) => void) | undefined;
    const List = (components.List ?? 'div') as React.ElementType;
    const Item = (components.Item ?? 'div') as React.ElementType;
    const scroller = React.useRef<HTMLDivElement>(null);
    // Mirror the live props onto the shared test handle each render.
    virtuosoTestHandle.firstItemIndex = firstItemIndex;
    virtuosoTestHandle.startReached = (props.startReached as (() => void)) ?? null;
    virtuosoTestHandle.endReached = (props.endReached as (() => void)) ?? null;
    virtuosoTestHandle.atBottomStateChange = (props.atBottomStateChange as ((b: boolean) => void)) ?? null;
    virtuosoTestHandle.itemsRendered = (props.itemsRendered as ((items: unknown[]) => void)) ?? null;
    virtuosoTestHandle.followOutput = (props.followOutput as ((b: boolean) => unknown)) ?? null;
    React.useEffect(() => {
      scrollerRef?.(scroller.current);
      // Emit an itemsRendered range covering the whole (rendered) list so the
      // reader's scroll-sync re-registration fires post-mount, mirroring real
      // Virtuoso's first range callback.
      const rendered = props.itemsRendered as ((items: unknown[]) => void) | undefined;
      rendered?.(data.map((d, i) => ({ index: firstItemIndex + i, data: d })));
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [data.length]);
    return React.createElement(
      'div',
      { ref: scroller, className: props.className as string, role: props.role as string, 'data-virtuoso-scroller': true, onScroll: props.onScroll as React.UIEventHandler },
      React.createElement(
        List,
        {},
        data.map((d, i) =>
          React.createElement(
            Item as React.ElementType,
            { key: computeItemKey ? computeItemKey(firstItemIndex + i, d) : i, 'data-index': firstItemIndex + i },
            itemContent(firstItemIndex + i, d),
          )),
      ),
    );
  });
  return { Virtuoso, VirtuosoMockContext: React.createContext(undefined) };
});

// §6 overlap-upsert keys the live-tail merge off `anchor.id`, so fixtures need
// DISTINCT, STABLE ids (the real server's anchor.id is the cache rowid). A
// per-uuid registry assigns a deterministic monotonic id so the SAME uuid always
// maps to the SAME id (a tail re-return of an existing item replaces it in
// place) while distinct uuids never collide. Reset per test in beforeEach.
const _idByUuid = new Map<string, number>();
let _nextItemId = 1;
function _idFor(uuid: string): number {
  let id = _idByUuid.get(uuid);
  if (id === undefined) { id = _nextItemId++; _idByUuid.set(uuid, id); }
  return id;
}
function makeItem(over: Partial<ConversationItem> & { uuid: string; kind?: ConversationItem['kind']; is_sidechain?: boolean }): ConversationItem {
  const { uuid, kind = 'human', is_sidechain = false, ...rest } = over;
  return {
    kind,
    anchor: { session_id: 's', uuid, id: _idFor(uuid) },
    member_uuids: [uuid],
    ts: 't',
    text: uuid,
    blocks: [],
    is_sidechain,
    subagent_key: is_sidechain ? 'k1' : null,
    parent_uuid: null,
    ...rest,
  } as ConversationItem;
}

function detail(items: ConversationItem[], next_after: number | null = null) {
  return {
    session_id: 's',
    project_label: 'proj',
    git_branch: 'main',
    started_utc: '2026-01-01T00:00:00Z',
    last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 3.5,
    models: ['claude-opus-4'],
    items,
    page: { next_after, has_more: next_after != null },
  };
}

function mockFetchOnce(body: unknown, status = 200) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: status < 400, status, json: async () => body,
  } as Response);
}

// ---- #175 F4 reader-scroll helpers ------------------------------------
// jsdom doesn't lay out, so set the scroll metrics by hand. scrollTop is
// writable on an element; clientHeight/scrollHeight are getters we override.
function setScroll(el: HTMLElement, m: { scrollTop: number; clientHeight: number; scrollHeight: number }) {
  el.scrollTop = m.scrollTop;
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: m.clientHeight });
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: m.scrollHeight });
}
// Drive a live SSE tick: bump the store snapshot's generated_at so the
// hook's tail-poll effect fires.
function bumpSnapshot(tag: string) {
  updateSnapshot({ generated_at: tag } as Envelope);
}
// jsdom doesn't implement Element.prototype.scrollTo, so vi.spyOn can't attach
// to a missing property — define a no-op first, then spy on it.
function spyScrollTo() {
  if (typeof Element.prototype.scrollTo !== 'function') {
    Element.prototype.scrollTo = () => {};
  }
  return vi.spyOn(Element.prototype, 'scrollTo').mockImplementation(() => {});
}

beforeEach(() => {
  _resetForTests();
  _resetKeymapForTests();
  globalThis.fetch = vi.fn();
  installIntersectionObserverStub();
  // #217 S3 E1 — the store now persists reading positions to localStorage on
  // SET_CONV_CURRENT_TURN; clear them per-test so a prior test's saved anchor
  // can't redirect a later session's open-precedence.
  clearReadingPositions();
  _idByUuid.clear();
  _nextItemId = 1;
  // #232 — reset the shared Virtuoso test handle so a prior test's scrollToIndex
  // calls / captured callbacks don't bleed across tests.
  virtuosoTestHandle.scrollToIndex = vi.fn();
  virtuosoTestHandle.firstItemIndex = 0;
  virtuosoTestHandle.startReached = null;
  virtuosoTestHandle.endReached = null;
  virtuosoTestHandle.atBottomStateChange = null;
  virtuosoTestHandle.itemsRendered = null;
  virtuosoTestHandle.followOutput = null;
});
afterEach(() => {
  uninstallGlobalKeydown();
  _resetForTests();
  clearReadingPositions();
  vi.restoreAllMocks();
  // Belt-and-suspenders over `test.unstubGlobals: true` (vite.config.ts):
  // `stubMobileMedia` installs `matchMedia` via vi.stubGlobal, which
  // restoreAllMocks does NOT undo — only unstubAllGlobals does. Without it a
  // mobile test leaves matchMedia stubbed-as-mobile for every later test in the
  // file, so `useIsMobile()` stays true and outline/layout assertions flake
  // under reordering (#221).
  vi.unstubAllGlobals();
});

describe('ConversationReader', () => {
  it('renders the header and groups parallel subagents into separate threads', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 'b1', is_sidechain: true, subagent_key: 'B', text: 'Audit B' } as never),
      makeItem({ uuid: 'a2', is_sidechain: true, subagent_key: 'A' } as never),
      makeItem({ uuid: 'b2', is_sidechain: true, subagent_key: 'B' } as never),
    ]));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());

    expect(container.querySelector('.conv-reader-meta')!.textContent).toContain('$3.50');
    const body = container.querySelector('.conv-reader-body')!;
    // #232 — the virtualized list carries role="feed" so off-screen turns stay
    // navigable for a screen reader (Codex P2-4).
    expect(body.getAttribute('role')).toBe('feed');
    expect(body.querySelector('[data-uuid="h1"]')).not.toBeNull();
    // TWO separate subagent disclosures (not one fused group).
    const groups = body.querySelectorAll('details.conv-sidechain');
    expect(groups).toHaveLength(2);
    expect(groups[0].querySelector('summary')!.textContent).toContain('Audit A');
    expect(groups[1].querySelector('summary')!.textContent).toContain('Audit B');
  });

  // #217 S5 F7 — the header completion chip shows ONLY when task_completion is
  // all_done, and clicking it jumps to the anchor (reuses the OPEN_CONVERSATION
  // jump pipeline, asserted via getState().conversationJump).
  it('shows the ✓ Complete chip when all_done and jumps on click', async () => {
    mockFetchOnce(detail([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'a1', kind: 'assistant' })]));
    const outline = {
      session_id: 's',
      stats: {} as never,
      turns: [
        { uuid: 'h1', kind: 'human', ts: 't', label: 'go', member_uuids: ['h1'], subagent_key: null, parent_uuid: null, is_sidechain: false },
        { uuid: 'a1', kind: 'assistant', ts: 't', label: 'done', member_uuids: ['a1'], subagent_key: null, parent_uuid: null, is_sidechain: false },
      ] as OutlineTurn[],
      task_completion: { all_done: true, total: 5, completed: 5, anchor_uuid: 'a1' },
    } as ConversationOutline;
    const { container } = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());
    const chip = screen.getByRole('button', { name: /complete/i });
    expect(chip.textContent).toMatch(/5/);
    act(() => { fireEvent.click(chip); });
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
  });

  it('hides the completion chip when not all_done', async () => {
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })]));
    const outline = {
      session_id: 's',
      stats: {} as never,
      turns: [
        { uuid: 'h1', kind: 'human', ts: 't', label: 'go', member_uuids: ['h1'], subagent_key: null, parent_uuid: null, is_sidechain: false },
      ] as OutlineTurn[],
      task_completion: { all_done: false, total: 5, completed: 2, anchor_uuid: 'h1' },
    } as ConversationOutline;
    const { container } = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());
    expect(screen.queryByRole('button', { name: /complete/i })).toBeNull();
  });

  it('threads subagent_meta from the detail payload into the subagent card (#166)', async () => {
    // A main human + one subagent thread keyed "aaaa1111", with a top-level
    // subagent_meta map. The reader must hand the matching entry to the
    // SidechainGroup, which surfaces the kind in the eyebrow — catching a broken
    // ConversationReader → SidechainGroup hand-off, not just the child unit.
    mockFetchOnce({
      ...detail([
        makeItem({ uuid: 'h1' }),
        makeItem({ uuid: 'a1', is_sidechain: true, subagent_key: 'aaaa1111', text: 'Audit A' } as never),
        makeItem({ uuid: 'a2', is_sidechain: true, subagent_key: 'aaaa1111' } as never),
      ]),
      subagent_meta: { aaaa1111: { kind: 'Explore', total_tokens: 1, status: 'completed' } },
    });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(document.querySelector('.conv-sidechain-kindname')!.textContent).toContain('Explore'));
  });

  it('jumps to a target message: pages until loaded, direct-scrolls the scroller, render-driven flash (#232/#233/#234)', async () => {
    // Page 1 has h1 only, with more to come; page 2 carries the target.
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    mockFetchOnce(detail([makeItem({ uuid: 'target', member_uuids: ['target', 'targetFrag'] } as never)], null));

    // #234 — the landing now writes the Virtuoso SCROLLER's scrollTop directly
    // (scrollNodeIntoView) instead of the library's convergent scrollIntoView,
    // which strands cold far jumps. Spy on the scroller's scrollTo.
    const scrollToSpy = spyScrollTo();

    // Set the jump for this session BEFORE rendering the reader.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'targetFrag' } });

    const { container } = render(<ConversationReader sessionId="s" />);

    // The reader pages until the target's member_uuids include 'targetFrag', then
    // direct-scrolls it into view and flashes it (render-driven, via jumpedUuid).
    await waitFor(() => {
      const el = container.querySelector('[data-uuid="target"]');
      expect(el).not.toBeNull();
    });
    // #234 — the precision landing is a direct scrollTo on the scroller (the
    // bodyRef element), NOT the library's scrollIntoView and NOT a native
    // el.scrollIntoView (inert inside the giant library row). JSDOM has no layout,
    // so we assert the scroller was scrolled at all, not a pixel offset (that's
    // the Playwright gate's job, spec §5).
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    // The flash is render-driven (jumpedUuid), surviving an unmount/remount.
    const target = container.querySelector('[data-uuid="target"]')!;
    await waitFor(() => expect(target.classList.contains('conv-item--jumped')).toBe(true));
    // The jump landed: it cleared and pinned the target (the user-facing outcome).
    await waitFor(() => expect(getState().convPinnedUuid).toBe('targetFrag'));
  });

  it('jump landing never passes scrollToIndex the firstItemIndex-offset virtual index (#232 P1-A / #234 array-index regression)', async () => {
    // ROOT CAUSE (#232 P1-A): react-virtuoso's `scrollToIndex` `{ index }` takes the
    // 0-based DATA (array) index — `firstItemIndex` shifts the `itemContent` index
    // and the prepend bookkeeping, but NOT the scroll input space. Passing the
    // virtual index (= firstItemIndex + arrayIndex, ~1,000,000+) lands outside
    // [0, totalCount] and react-virtuoso clamps + ignores it (measured in-browser:
    // scrollTop did not move). #234's walk-to-mount keeps that rule — every walk
    // step feeds scrollToIndex the ARRAY index. The walk only fires when the target
    // is UNMOUNTED; in this full-render mock the paged-in target is mounted, so the
    // walk short-circuits — but the invariant (NEVER the virtual index) still holds
    // and is pinned here, plus the jump lands (resolving the array space correctly).
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    mockFetchOnce(detail([makeItem({ uuid: 'target', member_uuids: ['target', 'targetFrag'] } as never)], null));

    spyScrollTo();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'targetFrag' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    await waitFor(() => expect(container.querySelector('[data-uuid="target"]')).not.toBeNull());

    // The target row's data-index carries its VIRTUAL index (firstItemIndex + array
    // position); the live firstItemIndex is on the test handle.
    const targetWrap = container.querySelector('[data-uuid="target"]')!.closest('.conv-reader-item') as HTMLElement;
    const targetVirtual = Number(targetWrap.getAttribute('data-index'));
    const targetArrayIndex = targetVirtual - virtuosoTestHandle.firstItemIndex;
    // The base is the real VIRTUAL_INDEX_BASE (1,000,000), so the virtual index is
    // unmistakably different from the array index — a non-vacuous gap.
    expect(targetVirtual).toBeGreaterThanOrEqual(VIRTUAL_INDEX_BASE);
    expect(targetArrayIndex).toBeLessThan(VIRTUAL_INDEX_BASE);

    // The jump resolved + landed (pinned the target) — proof the array space was
    // resolved correctly end-to-end.
    await waitFor(() => expect(getState().convPinnedUuid).toBe('targetFrag'));
    // And the walk (if it ran) NEVER passed scrollToIndex the virtual index (the bug).
    expect(virtuosoTestHandle.scrollToIndex).not.toHaveBeenCalledWith(expect.objectContaining({ index: targetVirtual }));
  });

  it('gives up on a jump when pagination exhausts on an empty terminal page (no infinite loop)', async () => {
    // Page 1 returns h1 + a cursor (has_more true). Page 2 — the after=<id>
    // fetch — is the empty terminal page: items: [], next_after: null. The
    // target uuid never appears anywhere. With Fix 1 the give-up clear
    // fires when hasMore transitions to false even though items.length never
    // grew on the terminal page (the regression this pins).
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    mockFetchOnce({
      session_id: 's',
      project_label: 'proj',
      git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z',
      last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 3.5,
      models: ['claude-opus-4'],
      items: [],
      page: { next_after: null, has_more: false },
    });

    const scrollSpy = vi
      .spyOn(Element.prototype, 'scrollIntoView')
      .mockImplementation(() => {});

    // Jump targets a uuid that never lands in any item.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'never-appears' } });

    render(<ConversationReader sessionId="s" />);

    // The jump clears via the give-up branch once paging is exhausted.
    await waitFor(() => expect(getState().conversationJump).toBeNull());

    // No target was ever found → no scroll, and fetch ran a bounded number
    // of times (page 1 + page 2 = 2; allow a small constant for re-renders).
    expect(scrollSpy).not.toHaveBeenCalled();
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeLessThanOrEqual(4);
  });

  it('does not clear a cross-session jump while detail still belongs to the prior session (cross-session guard)', async () => {
    // The reader is reused across session switches (ConversationsView mounts it
    // at a fixed position), so for one render pass it can hold the PRIOR
    // session's detail while sessionId + jump already point at the NEW session.
    // Modelled here as a stable window: the reader is asked for session 'B'
    // (jump 'B/targetB'), but the loaded detail reports session 's' with no
    // more pages and without targetB. Without the detail.session_id===sessionId
    // guard the jump effect runs against the 's' detail, finds nothing, and
    // (s having no more pages) clears the jump prematurely so 'B' never scrolls.
    // With the guard it short-circuits and leaves the jump set, waiting for 'B'.
    mockFetchOnce({
      session_id: 's', project_label: 'proj', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 1, models: ['claude-opus-4'],
      items: [makeItem({ uuid: 'a1' })],
      page: { next_after: null, has_more: false },   // s: fully loaded, no more pages
    });
    const scrollSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'B', jump: { session_id: 'B', uuid: 'targetB' } });
    render(<ConversationReader sessionId="B" />);
    // Let the loaded-'s' detail land and the jump effect run to completion.
    await act(async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); });

    // The guard kept the jump alive (it would resolve once 'B' itself loads);
    // the give-up branch did NOT fire against the cross-session 's' detail.
    expect(getState().conversationJump).toEqual({ session_id: 'B', uuid: 'targetB' });
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it('resolves a jump into a different session after the new session loads (cross-session guard, full flow)', async () => {
    // Behavior-preservation guard: once the reused reader's detail catches up to
    // the new session 'B' (which carries the jump target), the jump resolves —
    // it scrolls + flashes 'B's target rather than staying stuck.
    mockFetchOnce(detail([makeItem({ uuid: 'a1' })], null));   // session 's': one item, no more
    const scrollToSpy = spyScrollTo();

    const { container, rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull());

    // Queue 'B' page-1 (carries the jump target), then open the hit + switch.
    mockFetchOnce({
      session_id: 'B', project_label: 'proj', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 1, models: ['claude-opus-4'],
      items: [{
        kind: 'human', anchor: { session_id: 'B', uuid: 'targetB', id: 0 },
        member_uuids: ['targetB'], ts: 't', text: 'targetB', blocks: [],
        is_sidechain: false, subagent_key: null, parent_uuid: null,
      }],
      page: { next_after: null, has_more: false },
    });
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'B', jump: { session_id: 'B', uuid: 'targetB' } }); });
    rerender(<ConversationReader sessionId="B" />);

    // 'B' lands; the jump resolves against 'B' (scroll + flash), not cleared early.
    await waitFor(() => expect(container.querySelector('[data-uuid="targetB"]')).not.toBeNull());
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-uuid="targetB"]')!.classList.contains('conv-item--jumped')).toBe(true));
  });

  it('jumps to a FOLDED tool_result uuid: scrolls the owning assistant turn (#160 + #164)', async () => {
    // The kernel folds a tool_result row's uuid ('u1') into its owning turn's
    // member_uuids. A jump targeting 'u1' must resolve to the turn element
    // (data-uuid = the turn's anchor 'a1'), since getItemRef maps every
    // member_uuids entry — including the folded result uuid — to that element.
    mockFetchOnce(detail([
      {
        kind: 'assistant',
        anchor: { session_id: 's', uuid: 'a1', id: 1 },
        member_uuids: ['a1', 'u1'], // u1 = the folded tool_result uuid
        ts: 't',
        text: 'paired turn',
        model: 'claude-opus-4',
        is_sidechain: false,
        subagent_key: null,
        parent_uuid: null,
        cost_usd: 0.01,
        blocks: [
          { kind: 'text', text: 'paired turn' },
          {
            kind: 'tool_call',
            name: 'Read',
            input_summary: '{}',
            preview: '/x.py',
            tool_use_id: 't1',
            result: { text: 'BODY', truncated: false, is_error: false },
          },
        ],
      } as ConversationItem,
    ]));

    const scrollToSpy = spyScrollTo();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'u1' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    const turn = container.querySelector('[data-uuid="a1"]')!;
    expect(turn).not.toBeNull();
    await waitFor(() => expect(turn.classList.contains('conv-item--jumped')).toBe(true));
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });

  it('header leads with the derived title, not project_label', async () => {
    // First human item carries the prompt; project_label is "proj".
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1', text: 'design the conversation reader\nsecond line' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'sure', model: 'claude-opus-4', cost_usd: 0.01 } as never),
    ]));
    render(<ConversationReader sessionId="s" />);
    expect(await screen.findByText('design the conversation reader')).toBeInTheDocument();
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('design the conversation reader'),
    );
    // The project label is demoted into the meta line.
    expect(document.querySelector('.conv-reader-meta')!.textContent).toContain('proj');
  });

  it('header falls back to project_label when the opening human is a system marker', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'm1', text: '<command-name>clear</command-name>' }),
    ]));
    render(<ConversationReader sessionId="s" />);
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('proj'),
    );
  });

  // #186 — belt-and-suspenders: the title skips ANY line wrapped entirely in a
  // command-*/local-command-* family tag, even an UNKNOWN one not in MARKER_TAGS
  // (the strict isSystemMarker would NOT skip `local-command-future`). The title
  // then falls through to the next real prompt, never poisoned by future
  // unrecognized plumbing.
  it('header skips an unknown command-family plumbing line and uses the next real prompt', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'm1', text: '<local-command-future>x</local-command-future>' }),
      makeItem({ uuid: 'h1', text: 'the real first prompt' }),
    ]));
    render(<ConversationReader sessionId="s" />);
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('the real first prompt'),
    );
  });

  it('#193: prefers detail.title over deriveReaderTitle', async () => {
    // The server now derives a `title` (ai-title). It must win over the
    // first-prompt heuristic even when a raw human prompt is present.
    mockFetchOnce({
      ...detail([
        makeItem({ uuid: 'h1', text: 'raw prompt that deriveReaderTitle would pick' }),
      ]),
      title: 'Server AI Title',
    });
    render(<ConversationReader sessionId="s" />);
    await waitFor(() =>
      expect(document.querySelector('.conv-reader-title')!.textContent).toBe('Server AI Title'),
    );
  });

  it('renders a styled selection-empty / loading state, not bare text', async () => {
    // First page never resolves → the loading state shows the styled .conv-state.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader--loading')).not.toBeNull());
    expect(container.querySelector('.conv-state')).not.toBeNull();
    const glyph = container.querySelector('.conv-state-glyph')!;
    expect(glyph).not.toBeNull();
    // C3: the loading state glyph is now an inline SVG (not the ⏳ emoji).
    expect(glyph.querySelector('svg[aria-hidden="true"]')).toBeInTheDocument();
    expect(glyph.textContent).not.toMatch(/[💭🔧📤🖼📄↪⚙⏳⚠💬🧵]/);
    expect(container.querySelector('.conv-state-title')).not.toBeNull();
  });

  it('rise-animates each top-level item once on first appearance, not on re-render (G1 §4b)', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'reply', model: 'claude-opus-4', cost_usd: 0.01 } as never),
    ]));
    const { container, rerender } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    const first = container.querySelector('[data-uuid="h1"]')!;
    expect(first.className).toMatch(/conv-rise/);
    // #231 — the rise decision is FROZEN per uuid: className + style keep a STABLE
    // identity across re-renders (a reverse-page prepend re-renders the reader), so
    // the MessageItem memo holds and the whole loaded window is NOT re-rendered.
    // The entrance animation still runs only once — a stable, un-toggled class does
    // not replay, and `conv-rise` uses animation-fill-mode `both` ending at the
    // natural state, so KEEPING the class is visually inert. So conv-rise REMAINS
    // (it is not stripped on re-render — stripping it was the className flip that
    // defeated the memo for the whole window and froze the cold-load reader).
    const before = first.className;
    rerender(<ConversationReader sessionId="s" />);
    const after = container.querySelector('[data-uuid="h1"]')!;
    expect(after.className).toBe(before);           // stable className → memo holds
    expect(after.className).toMatch(/conv-rise/);    // kept across re-render, not stripped
  });

  it('staggers the first content page with a per-index animationDelay (idx*40ms, G1 §4b)', async () => {
    // The first CONTENT render (seen-Set still empty) must stagger; the loading
    // branch that renders before `detail` resolves must NOT consume "first page".
    // Three top-level items, no active jump → delays 0/40/80ms at indices 0/1/2.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'reply', model: 'claude-opus-4', cost_usd: 0.01 } as never),
      makeItem({ uuid: 'h2' }),
    ]));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    const delayOf = (uuid: string) =>
      (container.querySelector(`[data-uuid="${uuid}"]`) as HTMLElement).style.animationDelay;
    expect(delayOf('h1')).toBe('0ms');
    expect(delayOf('a1')).toBe('40ms');
    expect(delayOf('h2')).toBe('80ms');
  });

  it('the active jump target gets conv-item--jumped WITHOUT conv-rise (Codex P2)', async () => {
    // Jump targets a page-1 uuid set BEFORE first paint; the render-time
    // classifier must deny it conv-rise so only the flash runs on that element.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'target', member_uuids: ['target', 'targetFrag'] } as never),
    ], null));
    const scrollToSpy = spyScrollTo();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'targetFrag' } });

    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    const el = container.querySelector('[data-uuid="target"]')!;
    await waitFor(() => expect(el.classList.contains('conv-item--jumped')).toBe(true));
    // The two animations never run on one element.
    expect(el.className).not.toMatch(/conv-rise/);
  });

  it('auto-expands a collapsed subagent thread, scrolls, and highlights when jumping to a member', async () => {
    // Page 1: a main item + a collapsed subagent thread 'A' (sa1 root, sa2 member).
    // No more pages. The jump targets sa2, which lives inside the collapsed thread.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'sa1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 'sa2', is_sidechain: true, subagent_key: 'A' } as never),
    ]));
    const scrollToSpy = spyScrollTo();

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'sa2' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    // The owning subagent thread auto-expands.
    await waitFor(() => {
      const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement | null;
      expect(det?.open).toBe(true);
    });
    // The target member direct-scrolls into view, flashes, and the jump clears.
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-uuid="sa2"]')!.classList.contains('conv-item--jumped')).toBe(true));
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });

  // #188 B2 — the jump effect pins where it landed. The pin drives the outline's
  // aria-current + the jump-to-next cursor (closes #187).
  it('sets convPinnedUuid to the jump uuid after a jump lands', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'target', member_uuids: ['target', 'targetFrag'] } as never),
    ], null));
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'targetFrag' } });
    render(<ConversationReader sessionId="s" />);
    // The pin records the landing uuid (the jump's uuid), not the scroll cursor.
    await waitFor(() => expect(getState().convPinnedUuid).toBe('targetFrag'));
  });

  // #188 B1/B7 — an outline click on a COLLAPSED subagent jumps to the bucket-root
  // uuid. The jump effect resolves the card via cardRefs (the inner members are
  // ref-less while collapsed) and flashes the <details> CARD without force-opening
  // it (Bug 1: previously this force-opened the thread + flashed an inner member).
  it('a jump to a collapsed subagent bucket-root flashes the CARD without force-opening', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'sa1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 'sa2', is_sidechain: true, subagent_key: 'A' } as never),
    ]));

    // The outline subagent entry's jump anchor is the bucket-root uuid (sa1).
    const scrollToSpy = spyScrollTo();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'sa1' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    // #234 — the card-root jump direct-scrolls the scroller (scrollNodeIntoView on
    // the <details> CARD element, 'start'-aligned — NOT the sticky <summary>), and
    // the flash is render-driven on the card — the thread stays COLLAPSED (no
    // force-open). The card is already mounted (cardRefs holds open+closed cards),
    // so the walk short-circuits straight to the direct landing.
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(det.getAttribute('data-uuid')).toBe('sa1');
    await waitFor(() => expect(det.classList.contains('conv-item--jumped')).toBe(true));
    expect(det.open).toBe(false); // NOT force-opened
    // The pin lands on the bucket-root uuid so the outline subagent entry lights.
    await waitFor(() => expect(getState().convPinnedUuid).toBe('sa1'));
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });

  // §5 (Codex P1-D) — a jump into a GRANDCHILD subagent member force-opens the
  // grandchild AND its parent card (the whole ancestor chain). The integration
  // assertion catches broken reader→SidechainGroup wiring of the recursive tree
  // + ancestor force-open, not just the child unit.
  it('a jump into a nested grandchild force-opens the grandchild AND its parent card', async () => {
    // Topology (s8-shaped): main m1 spawns child C (parent=null, anchor m1);
    // child C spawns grandchild G (parent=C, anchor c1). The jump targets g1
    // (a grandchild member), so BOTH C and G must open for g1's ref to attach.
    mockFetchOnce({
      ...detail([
        makeItem({ uuid: 'm1', kind: 'human', text: 'Run the audit' }),
        makeItem({ uuid: 'c1', is_sidechain: true, subagent_key: 'C', text: 'Sync audit' } as never),
        makeItem({ uuid: 'g1', is_sidechain: true, subagent_key: 'G', text: 'Ground claims' } as never),
      ]),
      subagent_meta: {
        C: { kind: 'code-reviewer', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_c' },
        G: { kind: 'grounding', parent_subagent_key: 'C', spawn_uuid: 'c1', spawn_tool_use_id: 'tu_g' },
      },
    });
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'g1' } });
    const { container } = render(<ConversationReader sessionId="s" />);

    // Both the parent (C, data-uuid c1) and the grandchild (G, data-uuid g1) cards
    // open. The grandchild is nested inside the parent's body, so its card only
    // mounts once the parent's force-open commit renders — one commit AFTER the
    // parent opens. Await BOTH in the same waitFor (a synchronous grandchild
    // check races that trailing commit and flakes under test reordering/load).
    await waitFor(() => {
      const parent = container.querySelector('details.conv-sidechain[data-uuid="c1"]') as HTMLDetailsElement | null;
      const grandchild = container.querySelector('details.conv-sidechain[data-uuid="g1"]') as HTMLDetailsElement | null;
      expect(parent?.open).toBe(true);
      expect(grandchild?.open).toBe(true);
    });
    // #222 — assert the open-state HOLDS at STEADY STATE (after the jump fully
    // clears = the reader's forcedOpenKeys reset has run). The waitFor above can
    // pass transiently while forceOpen is still true; the real regression is the
    // grandchild SILENTLY collapsing once the force resets — the old effect-latch
    // raced that reset, the grandchild's parent transiently collapsed and
    // UNMOUNTED it (discarding its latch), then re-opened while the grandchild
    // re-mounted fresh + collapsed. Without this steady-state check the test
    // missed ~44% of failures under shuffle: it only verified the grandchild was
    // PRESENT (classList), never that it was still OPEN. Assert open=true here.
    await waitFor(() => expect(getState().conversationJump).toBeNull());
    const parentSteady = container.querySelector('details.conv-sidechain[data-uuid="c1"]') as HTMLDetailsElement | null;
    const grandchild = container.querySelector('details.conv-sidechain[data-uuid="g1"]') as HTMLDetailsElement | null;
    expect(parentSteady?.open).toBe(true);    // parent stays open at steady state
    expect(grandchild?.open).toBe(true);      // grandchild STAYS open (the #222 fix)
    // The grandchild card is nested (rendered inside the parent's body).
    expect(grandchild!.classList.contains('conv-sidechain--nested')).toBe(true);
  });

  // #204/#232/#233 — a jump to a nested subagent CARD root force-opens the ancestor
  // chain (so the card mounts), then the primary scroll goes through Virtuoso's
  // convergent scrollIntoView (the top-level node's DATA index) and the WITHIN-ROW
  // re-aim — now run inside scrollIntoView's `done` callback (after convergence
  // settles), no longer a blind rAF — aligns the card HEAD to the top
  // (`block: 'start'` on its <summary>), not center — a tall card centered leaves
  // its head far above the fold.
  it('#204 aligns a nested subagent card head to the top (block:start) via the within-row re-aim', async () => {
    mockFetchOnce({
      ...detail([
        makeItem({ uuid: 'm1', kind: 'human', text: 'Run the audit' }),
        makeItem({ uuid: 'c1', is_sidechain: true, subagent_key: 'C', text: 'Sync audit' } as never),
        makeItem({ uuid: 'g1', is_sidechain: true, subagent_key: 'G', text: 'Ground claims' } as never),
      ]),
      subagent_meta: {
        C: { kind: 'code-reviewer', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_c' },
        G: { kind: 'grounding', parent_subagent_key: 'C', spawn_uuid: 'c1', spawn_tool_use_id: 'tu_g' },
      },
    });
    // #234 — the landing is a direct scrollTop write on the SCROLLER
    // (scrollNodeIntoView), not a native <summary> scrollIntoView. The g1 jump is a
    // NESTED subagent card-root: the reader force-opens its ancestor chain (C + G)
    // so the card mounts, then aligns the g1 <details> CARD element to the top via
    // the scroller (NOT the sticky summary). JSDOM has no layout, so we assert the
    // chain opened + the scroller was scrolled + the jump landed (the start-align
    // pixel precision is the Playwright gate's job).
    const scrollToSpy = spyScrollTo();

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'g1' } });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(getState().conversationJump).toBeNull());

    // The whole ancestor chain force-opened so the nested g1 card mounted.
    const cards = Array.from(container.querySelectorAll('details.conv-sidechain')) as HTMLDetailsElement[];
    const cCard = cards.find((d) => d.getAttribute('data-uuid') === 'c1');
    const gCard = cards.find((d) => d.getAttribute('data-uuid') === 'g1');
    expect(cCard?.open).toBe(true);
    expect(gCard?.open).toBe(true);
    // The landing direct-scrolled the scroller (not a native summary scrollIntoView).
    expect(scrollToSpy).toHaveBeenCalled();
    // The pin lands on the nested card-root uuid (the g1 card).
    await waitFor(() => expect(getState().convPinnedUuid).toBe('g1'));
  });

  // §5 (Codex P1-C) — a resolved spawn's chip is suppressed (its nested subagent
  // card is canonical), but an UNLINKED spawn's chip still renders. The reader
  // builds the suppression set from subagent_meta.spawn_tool_use_id.
  it('suppresses a resolved spawn chip on the main turn but keeps an unlinked one', async () => {
    mockFetchOnce({
      ...detail([
        makeItem({
          uuid: 'm1', kind: 'assistant', text: 'Spawning',
          blocks: [
            { kind: 'tool_call', name: 'Agent', input_summary: '{}', preview: 'linked spawn', tool_use_id: 'tu_c', result: null },
            { kind: 'tool_call', name: 'Agent', input_summary: '{}', preview: 'unlinked spawn', tool_use_id: 'tu_clip', result: null },
          ],
        } as never),
        makeItem({ uuid: 'c1', is_sidechain: true, subagent_key: 'C', text: 'child' } as never),
      ]),
      subagent_meta: {
        // Only the linked spawn carries a spawn_tool_use_id (the kernel emits it
        // only for linked spawns); the >16 KB-clipped one has no entry.
        C: { kind: 'code-reviewer', parent_subagent_key: null, spawn_uuid: 'm1', spawn_tool_use_id: 'tu_c' },
      },
    });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="m1"]')).not.toBeNull());
    // The linked spawn preview is gone (chip suppressed); the unlinked one stays.
    expect(screen.queryByText('linked spawn')).toBeNull();
    expect(screen.getByText('unlinked spawn')).toBeInTheDocument();
    // The child subagent card renders (the canonical representation of the spawn).
    expect(container.querySelector('details.conv-sidechain[data-uuid="c1"]')).not.toBeNull();
  });

  // #188 B3 — explicit user navigation clears the pin. A wheel gesture on the
  // body drops it; a programmatic scroll (the jump's own smooth scroll routes
  // through onBodyScroll) must NOT.
  it('a wheel gesture on the reader body clears the pin; a plain scroll does not', async () => {
    mockFetchOnce(detail([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })], null));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());
    const body = container.querySelector('.conv-reader-body') as HTMLElement;

    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h1' }); });
    // A plain scroll (programmatic, e.g. the jump's smooth scroll) leaves the pin.
    fireEvent.scroll(body);
    expect(getState().convPinnedUuid).toBe('h1');
    // An explicit wheel gesture clears it.
    fireEvent.wheel(body);
    expect(getState().convPinnedUuid).toBeNull();
  });

  it('a scroll-key keydown on the reader body clears the pin', async () => {
    mockFetchOnce(detail([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })], null));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-body')).not.toBeNull());
    const body = container.querySelector('.conv-reader-body') as HTMLElement;

    for (const key of ['ArrowDown', 'PageUp', 'Home', 'End', ' ']) {
      act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h1' }); });
      fireEvent.keyDown(body, { key });
      expect(getState().convPinnedUuid).toBeNull();
    }
    // A non-scroll key leaves the pin.
    act(() => { dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'h1' }); });
    fireEvent.keyDown(body, { key: 'x' });
    expect(getState().convPinnedUuid).toBe('h1');
  });

  it('a find-jump to a focus-mode-hidden turn resets the mode to `all`, then lands the jump (spec §4)', async () => {
    // Focus mode `prompts` keeps human turns and hides assistant turns. A find
    // match on the hidden assistant turn 'a1' must escape the filter the same way
    // jump-to-next does: reset to `all`, re-render, then scroll + flash. Without
    // the jump-effect mode-reset the target never renders (it coalesces into a
    // `hidden_run` marker, ref-less), so the jump silently no-ops and clears once
    // pagination is exhausted (the regression this pins).
    //
    // NOTE: the OPEN_CONVERSATION reducer ONLY blanket-resets the focus mode on a
    // GENUINE session switch (different sessionId). A same-session find-jump (this
    // case) preserves the mode by design — the per-jump hidden check is the
    // caller/effect's job — so the reset proven here can ONLY come from the jump
    // effect's mode-hidden fallback. Hence we select the session FIRST, so the
    // find-jump below is same-session and the reducer does not mask the fix.
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1', kind: 'human', text: 'prompt one' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: '', blocks: [] } as never),
    ]));
    const scrollToSpy = spyScrollTo();

    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' }); });
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());
    // The assistant target is hidden behind a hidden_run marker in prompts mode.
    expect(container.querySelector('[data-uuid="a1"]')).toBeNull();

    // FindBar drives a same-session OPEN_CONVERSATION jump (expand_details set
    // when the match was inside a tool/thinking block).
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'a1', expand_details: false } }); });

    // The mode escapes back to `all` so the hidden assistant turn renders again.
    await waitFor(() => expect(getState().convFocusMode).toBe('all'));
    await waitFor(() => {
      expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull();
    });
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-uuid="a1"]')!.classList.contains('conv-item--jumped')).toBe(true));
    await waitFor(() => expect(getState().conversationJump).toBeNull());
  });

  // ── jump-to-latest control (spec §5) ──────────────────────────────────
  // Parent-level integration (per the modal-level-integration-test lesson):
  // exercise the REAL useConversation hook (#217 S3 E2 ?tail=1 jump-to-latest
  // reset, which replaced the old loadToEnd forward drain) + the store jump
  // pipeline, asserting the dispatched jump lands on last_anchor — not just that
  // a child callback fired.

  // detail head carrying a last_anchor (the conversation's final rendered turn).
  function detailWithAnchor(items: ConversationItem[], next_after: number | null, lastUuid: string | null) {
    const la = lastUuid == null
      ? null
      : { session_id: 's', uuid: lastUuid, id: _idFor(lastUuid) };
    return { ...detail(items, next_after), last_anchor: la };
  }

  it('Jump to latest RESETS to the tail page then dispatches a jump to last_anchor (#217 S3 E2)', async () => {
    // Page 1 (the tail open) holds h1 with MORE above it; last_anchor points at
    // the conversation's final turn. The control must RESET the window via ?tail=1
    // (one request, not a forward drain) THEN jump to last_anchor.uuid — reusing
    // the existing flash/pin jump pipeline.
    mockFetchOnce(detailWithAnchor([makeItem({ uuid: 'h1' })], 2, 'last-uuid'));
    const scrollToSpy = spyScrollTo();

    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());
    // The control is present (last_anchor non-null).
    const btn = screen.getByRole('button', { name: /jump to latest/i });

    // The ?tail=1 reset returns the final turn in one page (it exhausts the bottom).
    mockFetchOnce(detailWithAnchor([makeItem({ uuid: 'last-uuid' })], null, 'last-uuid'));
    await act(async () => { fireEvent.click(btn); for (let i = 0; i < 8; i++) await Promise.resolve(); });

    // The jump-to-latest fetch was a ?tail=1 RESET — NOT an ?after= forward drain.
    await waitFor(() => expect(container.querySelector('[data-uuid="last-uuid"]')).not.toBeNull());
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(String(calls.at(-1)![0])).toContain('tail=1');
    expect(calls.some((c) => String(c[0]).includes('after='))).toBe(false);
    // The jump landed on the final anchor: scrolled, flashed, then cleared.
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-uuid="last-uuid"]')!.classList.contains('conv-item--jumped')).toBe(true));
    await waitFor(() => expect(getState().conversationJump).toBeNull());
    // The landing is pinned on the final anchor (drives the outline + jump-to-next).
    await waitFor(() => expect(getState().convPinnedUuid).toBe('last-uuid'));
  });

  it('hides the Jump to latest control when last_anchor is null (empty conversation)', async () => {
    mockFetchOnce(detailWithAnchor([], null, null));
    render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(document.querySelector('.conv-reader-body')).not.toBeNull());
    expect(screen.queryByRole('button', { name: /jump to latest/i })).toBeNull();
  });

  it('#205 S2 (F3) — the Find button toggles the find bar and reflects aria-pressed', async () => {
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], null));
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    // Role-scoped to 'button' so it never matches the FindBar's same-named
    // textbox (aria-label "Find in conversation") once the bar is open.
    const findBtn = screen.getByRole('button', { name: /find in conversation/i });
    expect(findBtn.getAttribute('aria-pressed')).toBe('false');
    expect(container.querySelector('.conv-findbar')).toBeNull();

    // Open: the gated FindBar mounts (empty needle ⇒ no fetch fired) and
    // aria-pressed flips. Integration-level: proves button → store → gated bar.
    await act(async () => { fireEvent.click(findBtn); await Promise.resolve(); });
    await waitFor(() => expect(container.querySelector('.conv-findbar')).not.toBeNull());
    expect(getState().convFindOpen).toBe(true);
    expect(screen.getByRole('button', { name: /find in conversation/i }).getAttribute('aria-pressed')).toBe('true');

    // Close: a second click unmounts the bar and clears aria-pressed.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /find in conversation/i })); await Promise.resolve(); });
    await waitFor(() => expect(container.querySelector('.conv-findbar')).toBeNull());
    expect(getState().convFindOpen).toBe(false);
  });

  it('End key triggers jump-to-latest but NOT while the filter popover is open (spec §4/§5 guard)', async () => {
    // Single fully-paged page so the target is already loaded; the End key reuses
    // the same handler as the button. The named `End` key is NOT covered by the
    // single-char input-focus guard, so it must gate on convFiltersOpen.
    mockFetchOnce(detailWithAnchor([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'last-uuid' })], null, 'last-uuid'));
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="last-uuid"]')).not.toBeNull());

    // Filter popover OPEN → End is a no-op (no jump dispatched).
    act(() => { dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true }); });
    await act(async () => { fireEvent.keyDown(document, { key: 'End' }); for (let i = 0; i < 4; i++) await Promise.resolve(); });
    expect(getState().conversationJump).toBeNull();
    expect(container.querySelector('[data-uuid="last-uuid"]')!.classList.contains('conv-item--jumped')).toBe(false);

    // Filter popover CLOSED → End jumps to the final anchor (lands + flashes).
    act(() => { dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: false }); });
    await act(async () => { fireEvent.keyDown(document, { key: 'End' }); for (let i = 0; i < 8; i++) await Promise.resolve(); });
    await waitFor(() => expect(container.querySelector('[data-uuid="last-uuid"]')!.classList.contains('conv-item--jumped')).toBe(true));
    expect(getState().convPinnedUuid).toBe('last-uuid');
  });
});

describe('ConversationReader live-tail scroll (#175 F4)', () => {
  // §6 overlap: the live-tail poll re-fetches the recent WINDOW from the cursor
  // BEFORE it, so a real tail response re-returns the existing window + any new
  // turns (the seed sets here are all ≤ TAIL_WINDOW=10, so the cursor is null and
  // the whole accumulator is re-returned). `_liveWindow` tracks the running set
  // so the append helpers mock a faithful overlap response (prior window + new),
  // not the old strict-after-last delta.
  let _liveWindow: ConversationItem[] = [];

  // Render a fully-paged conversation (next_after null), then drive a live tail
  // append by bumping the snapshot + queueing a tail fetch. Returns the body.
  async function renderFullyPaged(items: ConversationItem[]) {
    _liveWindow = [...items];
    mockFetchOnce(detail(items, null));
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(utils.container.querySelectorAll('.conv-reader-thread > *').length).toBe(items.length));
    const body = utils.container.querySelector('.conv-reader-body') as HTMLElement;
    return { ...utils, body };
  }

  // Append one new turn via the live tail. The overlap tail response re-returns
  // the running window + the new turn (stays fully paged).
  async function appendLiveItem(newUuid: string) {
    _liveWindow = [..._liveWindow, makeItem({ uuid: newUuid })];
    mockFetchOnce(detail(_liveWindow, null));
    await act(async () => {
      bumpSnapshot(`t-${newUuid}`);
      // let the tail poll fetch + setState flush
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
  }

  it('live append sticks to bottom when already at bottom (#232 followOutput)', async () => {
    await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // #232 — the at-bottom signal is Virtuoso's atBottomStateChange (not the old
    // manual scrollTop math), and the stick is `followOutput`. Drive at-bottom.
    act(() => { virtuosoTestHandle.atBottomStateChange?.(true); });

    await appendLiveItem('live1');
    // followOutput requests a stick (a truthy behavior) while at bottom...
    expect(virtuosoTestHandle.followOutput?.(true)).toBeTruthy();
    // ...and returns false when scrolled up (no auto-stick).
    expect(virtuosoTestHandle.followOutput?.(false)).toBe(false);
    // No pill while stuck to the bottom (the count path is gated on !atBottom).
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('live append while scrolled up preserves position and shows the pill', async () => {
    await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // #232 — scrolled up = atBottomStateChange(false). The pill bump path is gated
    // on !atBottomRef, set here through Virtuoso's signal.
    act(() => { virtuosoTestHandle.atBottomStateChange?.(false); });

    await appendLiveItem('live1');
    // Surfaced the pill with the visibleAdded count (no auto-stick while scrolled up).
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill).toBeInTheDocument();
    expect(pill.textContent).toMatch(/1 new/);

    // #232 — clicking the pill scrolls to the LAST node via Virtuoso's
    // scrollToIndex (align 'end'), not a raw body.scrollTo, and clears the pill.
    fireEvent.click(pill);
    expect(virtuosoTestHandle.scrollToIndex).toHaveBeenCalledWith(expect.objectContaining({ align: 'end' }));
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  // #228 S1 (§6c) — the "↓ N new" pill is conditionally mounted, so an aria-live
  // ON it can't announce. A persistent, always-rendered .sr-only polite region
  // mirrors newCount so screen readers hear live-tail arrivals.
  it('announces newly-arrived live-tail messages via a persistent polite region', async () => {
    const { body, container } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // The region is ALWAYS present (even at zero) with aria-live="polite".
    const live = container.querySelector('[data-testid="conv-newcount-live"]');
    expect(live).toBeTruthy();
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live?.textContent).toBe('');

    // Scroll up so a live append raises newCount instead of sticking.
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    spyScrollTo();

    await appendLiveItem('live1');
    // The region's text now mentions the count (the pill also shows).
    await waitFor(() => expect(live?.textContent).toMatch(/1 new/));
  });

  it('the final PAGINATION append (was hasMore) shows no pill and no stick (P0 discriminator)', async () => {
    // Page 1 has a cursor (hasMore true). Render in conversations view with the
    // keymap installed so `j` at the last item triggers loadMore -> the FINAL
    // pagination page (next_after null). prevHasMore was TRUE on that append, so
    // it must NOT be treated as a live append.
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());

    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 }); // scrolled up
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    // The final page lands via loadMore (j at the single, last item).
    mockFetchOnce(detail([makeItem({ uuid: 'h2' })], null));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'j' });
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());

    // A pagination append must neither stick nor raise a pill.
    expect(scrollToSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('clears a stale "↓ N new" pill on a session switch (#175 P1)', async () => {
    // Render convo A fully-paged, scroll up, drive a live append so the pill
    // appears. Then switch the reused reader to convo B (new sessionId + B's
    // detail). Without the per-session pill reset the stale pill survives the
    // switch until the user scrolls B to the bottom; with it the pill is gone
    // the moment B loads.
    const { body, rerender, container } =
      await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 }); // scrolled up
    fireEvent.scroll(body);
    spyScrollTo();

    await appendLiveItem('live1');
    // Pill is up on convo A.
    expect(await screen.findByRole('button', { name: /new/i })).toBeInTheDocument();

    // Switch the reused reader to convo B; queue B's page-1 detail.
    mockFetchOnce({
      session_id: 'B', project_label: 'projB', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 2, models: ['claude-opus-4'],
      items: [makeItem({ uuid: 'b1' })],
      page: { next_after: null, has_more: false },
    });
    await act(async () => {
      rerender(<ConversationReader sessionId="B" />);
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="b1"]')).not.toBeNull());

    // The stale pill must be gone on B (per-session reset cleared newCount).
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('a pagination append followed by a live tail append shows the pill (sequence guard)', async () => {
    // After the final pagination page lands (hasMore flips false), the NEXT
    // growth — a live tail append — must be treated as live (pill appears).
    mockFetchOnce(detail([makeItem({ uuid: 'h1' })], 2));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h1"]')).not.toBeNull());
    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    // Final pagination page → hasMore false; no pill yet.
    mockFetchOnce(detail([makeItem({ uuid: 'h2' })], null));
    await act(async () => {
      fireEvent.keyDown(document, { key: 'j' });
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();

    // Now a live tail append — prevHasMore is false → pill. The §6 overlap tail
    // response re-returns the window (h1 + h2) + the genuinely-new live1.
    mockFetchOnce(detail([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' }), makeItem({ uuid: 'live1' })], null));
    // Re-pin the scroll metrics (jsdom append doesn't recompute them) so the
    // layout effect still reads "scrolled up".
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    await act(async () => {
      bumpSnapshot('t-live1');
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill).toBeInTheDocument();
    expect(scrollToSpy).not.toHaveBeenCalled();
  });
});

describe('ConversationReader visible-only "↓ N new" count (#188 S4/C2, Bug 5)', () => {
  // §6 overlap: like the scroll suite above, track the running window so the
  // append helper mocks a faithful overlap response (prior window + new turns),
  // not the old strict-after-last delta. A session switch reseeds the window.
  let _liveWindow: ConversationItem[] = [];

  // Render fully-paged, scroll UP (so live appends raise the pill rather than
  // sticking to bottom), and return the body + container.
  async function renderScrolledUp(items: ConversationItem[]) {
    _liveWindow = [...items];
    mockFetchOnce(detail(items, null));
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(utils.container.querySelectorAll('.conv-reader-thread > *').length).toBeGreaterThan(0));
    const body = utils.container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 }); // scrolled up
    fireEvent.scroll(body);
    return { ...utils, body };
  }

  // Append a live-tail delta of `items` via the §6 overlap poll: the tail
  // response re-returns the running window + these new turns (stays fully paged).
  // Re-pins the scroll metrics first because a jsdom append doesn't recompute
  // them, so the layout effect still reads "scrolled up".
  async function appendLive(body: HTMLElement, items: ConversationItem[], tag: string) {
    _liveWindow = [..._liveWindow, ...items];
    mockFetchOnce(detail(_liveWindow, null));
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    await act(async () => {
      bumpSnapshot(tag);
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
  }

  const sc = (uuid: string, key: string, over: Partial<ConversationItem> = {}) =>
    makeItem({ uuid, is_sidechain: true, subagent_key: key, text: uuid, ...over } as never);

  it('an append into a COLLAPSED known subagent thread does NOT bump the pill', async () => {
    // Page 1: a top-level item + a (collapsed) subagent thread A (root sa1).
    const { body, container } = await renderScrolledUp([
      makeItem({ uuid: 'h1' }),
      sc('sa1', 'A', { text: 'Audit A' }),
    ]);
    // The thread renders collapsed; its key 'A' is in the known-set, but it is
    // NOT open.
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(det.open).toBe(false);

    // A live append of a NEW member into the collapsed known thread A — nothing
    // is visible below the fold, so the pill must stay hidden (Bug 5).
    await appendLive(body, [sc('sa2', 'A')], 't-sa2');
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  it('an append into an EXPANDED known subagent thread bumps the pill (+1)', async () => {
    const { body, container } = await renderScrolledUp([
      makeItem({ uuid: 'h1' }),
      sc('sa1', 'A', { text: 'Audit A' }),
    ]);
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    // Expand the thread: the SidechainGroup's onToggle fires the reader's
    // handleSubagentOpenChange so the key 'A' enters openKeysRef.
    await act(async () => {
      det.open = true;
      fireEvent(det, new Event('toggle', { bubbles: false }));
      await Promise.resolve();
    });

    // Now an append into the EXPANDED thread A IS visible → +1.
    await appendLive(body, [sc('sa2', 'A')], 't-sa2');
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill.textContent).toMatch(/1 new/);
  });

  it('a top-level live append bumps the pill (+1)', async () => {
    const { body } = await renderScrolledUp([
      makeItem({ uuid: 'h1' }),
      sc('sa1', 'A', { text: 'Audit A' }),
    ]);
    await appendLive(body, [makeItem({ uuid: 'h2' })], 't-h2');
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill.textContent).toMatch(/1 new/);
  });

  it('the FIRST item of a brand-new subagent group bumps the pill once (+1)', async () => {
    const { body } = await renderScrolledUp([makeItem({ uuid: 'h1' })]);
    // Two members of a NEW subagent thread B arrive in one tick. The group's
    // FIRST item is visible (the card appears); the second is buried under the
    // (collapsed-by-default) fold → net +1, deduped per key per tick.
    await appendLive(body, [sc('sb1', 'B', { text: 'Audit B' }), sc('sb2', 'B')], 't-B');
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill.textContent).toMatch(/1 new/);
  });

  it('a session switch resets the open/known subagent sets (collapsed append on B does not over-count)', async () => {
    // Convo A: a known subagent thread A (collapsed).
    const { container, rerender } = await renderScrolledUp([
      makeItem({ uuid: 'h1' }),
      sc('sa1', 'A', { text: 'Audit A' }),
    ]);
    // Expand A on convo A (puts 'A' in openKeysRef) so we can prove the reset:
    // if openKeysRef survived the switch, a collapsed thread reusing key 'A' on
    // convo B would wrongly count.
    const det = container.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    await act(async () => {
      det.open = true;
      fireEvent(det, new Event('toggle', { bubbles: false }));
      await Promise.resolve();
    });

    // Switch the reused reader to convo B (different session, B's page-1 detail
    // carries a COLLAPSED subagent thread that happens to reuse key 'A'). Reseed
    // the §6 overlap window to B's items so the subsequent appendLive re-returns
    // B's window (not the stale A window) + the new turn.
    // NOTE: these B-items hard-code anchor.id (0/1) instead of going through the
    // _idFor registry (the only place in this file the two id schemes coexist).
    // Intentional — convo B is a separate session so its ids needn't align with
    // the _idFor sequence. If you add B-items that must dedup against _idFor-keyed
    // ones, route them through _idFor instead of literal ids.
    const bItems = [
      { ...makeItem({ uuid: 'b1' }), anchor: { session_id: 'B', uuid: 'b1', id: 0 } },
      { ...sc('ba1', 'A', { text: 'B Audit' }), anchor: { session_id: 'B', uuid: 'ba1', id: 1 } },
    ] as ConversationItem[];
    _liveWindow = [...bItems];
    mockFetchOnce({
      session_id: 'B', project_label: 'projB', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 2, models: ['claude-opus-4'],
      items: bItems,
      page: { next_after: null, has_more: false },
    });
    await act(async () => {
      rerender(<ConversationReader sessionId="B" />);
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="b1"]')).not.toBeNull());
    const bodyB = container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(bodyB, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(bodyB);

    // A live append into the COLLAPSED thread 'A' on convo B. If the reset
    // cleared openKeysRef (it must), this append is invisible → no pill. If the
    // stale open-set survived, it would wrongly count (the regression this pins).
    await appendLive(bodyB, [
      { ...sc('ba2', 'A'), anchor: { session_id: 'B', uuid: 'ba2', id: 2 } } as ConversationItem,
    ], 't-ba2');
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });
});

describe('ConversationReader floating "↑ Top of turn" button (#176)', () => {
  // jsdom never lays out, so getBoundingClientRect returns all-zeros. The
  // visibility decision keys on rects, so stub each element's rect by hand:
  // the body (the scroller) plus each top-level thread child (the turns). The
  // helper installs a single prototype spy that dispatches on element identity.
  function stubRects(
    map: Map<Element, { top: number; bottom: number }>,
  ) {
    return vi
      .spyOn(Element.prototype, 'getBoundingClientRect')
      .mockImplementation(function (this: Element) {
        const r = map.get(this) ?? { top: 0, bottom: 0 };
        return {
          top: r.top, bottom: r.bottom, left: 0, right: 0,
          width: 0, height: r.bottom - r.top, x: 0, y: r.top, toJSON() {},
        } as DOMRect;
      });
  }

  async function renderFullyPaged(items: ConversationItem[]) {
    mockFetchOnce(detail(items, null));
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-body')).not.toBeNull());
    await waitFor(() =>
      expect(utils.container.querySelectorAll('.conv-reader-thread > *').length).toBe(items.length));
    const body = utils.container.querySelector('.conv-reader-body') as HTMLElement;
    const thread = utils.container.querySelector('.conv-reader-thread') as HTMLElement;
    return { ...utils, body, thread };
  }

  it('shows no button when the first turn is at the top of the viewport', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // Body top at 0; the first turn's top is flush with it (not scrolled past).
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: 0, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });

  it('shows the button when scrolled deep into a tall first turn (top off by > 160px)', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // Body top at 0; the first turn's top is 300px above the body top, so it's
    // the block under the viewport top AND scrolled past the 160px threshold.
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -300, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(await screen.findByRole('button', { name: /jump to the start of this turn/i })).toBeInTheDocument();
  });

  it('does NOT show the button when the current turn is barely scrolled (under the threshold)', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    // First turn's top only 100px above the body top — under the 160px floor.
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -100, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });

  it('clicking the button scrolls the current turn back to its start and hides it', async () => {
    const { body, thread } = await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -300, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    const btn = await screen.findByRole('button', { name: /jump to the start of this turn/i });

    const scrollIntoViewSpy = vi
      .spyOn(thread.children[0], 'scrollIntoView')
      .mockImplementation(() => {});
    fireEvent.click(btn);
    // Scrolls the turn under the viewport top back to its start.
    expect(scrollIntoViewSpy).toHaveBeenCalledWith(expect.objectContaining({ block: 'start' }));
    // And the button hides immediately.
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });

  it('hides the button on a session switch (#176 reset)', async () => {
    const { body, thread, rerender, container } =
      await renderFullyPaged([makeItem({ uuid: 'h1' }), makeItem({ uuid: 'h2' })]);
    stubRects(new Map<Element, { top: number; bottom: number }>([
      [body, { top: 0, bottom: 600 }],
      [thread.children[0], { top: -300, bottom: 1000 }],
      [thread.children[1], { top: 1000, bottom: 1100 }],
    ]));
    fireEvent.scroll(body);
    expect(await screen.findByRole('button', { name: /jump to the start of this turn/i })).toBeInTheDocument();

    // Switch the reused reader to convo B.
    mockFetchOnce({
      session_id: 'B', project_label: 'projB', git_branch: 'main',
      started_utc: '2026-01-01T00:00:00Z', last_activity_utc: '2026-01-01T02:00:00Z',
      cost_usd: 2, models: ['claude-opus-4'],
      items: [makeItem({ uuid: 'b1' })],
      page: { next_after: null, has_more: false },
    });
    await act(async () => {
      rerender(<ConversationReader sessionId="B" />);
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="b1"]')).not.toBeNull());

    // The stale jump-top button must be gone on B (per-session reset).
    expect(screen.queryByRole('button', { name: /jump to the start of this turn/i })).toBeNull();
  });
});

describe('ConversationReader keyboard navigation (G3)', () => {
  // The reader's keymap is `view:'conversations'`-scoped; the conversations
  // view is entered via OPEN_CONVERSATION (sets view + selection).
  async function renderInConversations(items: ConversationItem[], next_after: number | null = null) {
    mockFetchOnce(detail(items, next_after));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    const thread = utils.container.querySelector('.conv-reader-thread') as HTMLElement;
    return { ...utils, thread };
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });
  // #232 — Virtuoso wraps each node in a `.conv-reader-item`, and the focus ring
  // class lands on the INNER node element, so resolve it for the cursor asserts.
  const row = (thread: HTMLElement, i: number): Element => thread.children[i].firstElementChild ?? thread.children[i];

  it('j/k move the focused-turn cursor and clamp at both ends', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'a1', kind: 'assistant', text: 'r', model: 'm', cost_usd: 0.01 } as never),
      makeItem({ uuid: 'h2' }),
    ]);
    // Starts on the first turn (render-driven ring, keyed on the cursor uuid).
    expect(row(thread, 0)).toHaveClass('conv-item--focused');
    press('j');
    expect(row(thread, 1)).toHaveClass('conv-item--focused');
    expect(row(thread, 0)).not.toHaveClass('conv-item--focused');
    // #232 — a step brings the cursor's node into view via Virtuoso's scrollToIndex.
    expect(virtuosoTestHandle.scrollToIndex).toHaveBeenCalled();
    press('j');
    expect(row(thread, 2)).toHaveClass('conv-item--focused');
    press('j'); // clamp at the last child
    expect(row(thread, 2)).toHaveClass('conv-item--focused');
    press('k');
    expect(row(thread, 1)).toHaveClass('conv-item--focused');
    press('k');
    press('k'); // clamp at 0
    expect(row(thread, 0)).toHaveClass('conv-item--focused');
  });

  it('[ collapses and ] expands every sidechain via the DATA MODEL (#232 Codex P1-1)', async () => {
    // #232 — the bulk sweep now flips the SidechainGroup open-state through the
    // data model (a rev+open prop adopted in render), NOT querySelectorAll, so it
    // reaches off-screen sidechains under virtualization. The subagent <details
    // open> reflects the swept state.
    const { thread } = await renderInConversations([
      makeItem({ uuid: 's1', is_sidechain: true, subagent_key: 'A', text: 'Audit A' } as never),
      makeItem({ uuid: 's2', is_sidechain: true, subagent_key: 'A' } as never),
    ]);
    const sidechain = () => thread.querySelector('details.conv-sidechain') as HTMLDetailsElement;
    expect(sidechain()).not.toBeNull();
    expect(sidechain().open).toBe(false); // collapsed by default
    press(']');
    expect(sidechain().open).toBe(true);  // expand-all via the data model
    press('[');
    expect(sidechain().open).toBe(false); // collapse-all via the data model
  });

  it('g scrolls the reader to the top via Virtuoso and resets the cursor to 0 (#232)', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
      makeItem({ uuid: 'h3' }),
    ]);
    press('j');
    press('j');
    expect(row(thread, 2)).toHaveClass('conv-item--focused');
    virtuosoTestHandle.scrollToIndex.mockClear();
    press('g');
    // #232 — the top jump routes through Virtuoso's scrollToIndex (not a raw
    // body.scrollTo — the mounted window may not include the first item). #232 fix:
    // scrollToIndex takes the 0-based DATA (array) index, NOT the firstItemIndex-
    // offset virtual index (which the library clamps + ignores — see the dedicated
    // regression test). The first loaded node is array index 0.
    expect(virtuosoTestHandle.scrollToIndex).toHaveBeenCalledWith(
      expect.objectContaining({ index: 0, align: 'start' }),
    );
    expect(row(thread, 0)).toHaveClass('conv-item--focused');
  });

  it('bindings are inert while a modal is open', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
    ]);
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'session' }); });
    press('j');
    // No move from index 0 while a modal owns the keys.
    expect(row(thread, 0)).toHaveClass('conv-item--focused');
    expect(row(thread, 1)).not.toHaveClass('conv-item--focused');
  });

  it('bindings are inert while input-mode is active', async () => {
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
    ]);
    act(() => { dispatch({ type: 'SET_INPUT_MODE', mode: 'search' }); });
    press('j');
    expect(row(thread, 0)).toHaveClass('conv-item--focused');
    expect(row(thread, 1)).not.toHaveClass('conv-item--focused');
  });

  it('does not fire on the dashboard view (view-scoped binding)', async () => {
    // Mount in conversations, then leave to the dashboard: the binding is
    // view:'conversations'-gated so j must not move the cursor.
    const { thread } = await renderInConversations([
      makeItem({ uuid: 'h1' }),
      makeItem({ uuid: 'h2' }),
    ]);
    act(() => { dispatch({ type: 'SET_VIEW', view: 'dashboard' }); });
    press('j');
    expect(row(thread, 0)).toHaveClass('conv-item--focused');
    expect(row(thread, 1)).not.toHaveClass('conv-item--focused');
  });

  // #177 S5 — the `o` key toggles the outline open flag; the toggle button in
  // the reader head mirrors the flag via aria-pressed and dispatches the same.
  it('o toggles the outline open flag', async () => {
    await renderInConversations([makeItem({ uuid: 'h1' })]);
    const before = getState().convOutlineOpen;
    press('o');
    expect(getState().convOutlineOpen).toBe(!before);
    press('o');
    expect(getState().convOutlineOpen).toBe(before);
  });

  it('o is inert while a modal is open (modal guard)', async () => {
    await renderInConversations([makeItem({ uuid: 'h1' })]);
    const before = getState().convOutlineOpen;
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'session' }); });
    press('o');
    expect(getState().convOutlineOpen).toBe(before); // unchanged
  });

  it('the reader-head outline toggle reflects + flips the open flag', async () => {
    const { container } = await renderInConversations([makeItem({ uuid: 'h1' })]);
    const btn = container.querySelector<HTMLButtonElement>('.conv-outline-toggle')!;
    expect(btn).not.toBeNull();
    const before = getState().convOutlineOpen;
    expect(btn.getAttribute('aria-pressed')).toBe(String(before));
    fireEvent.click(btn);
    expect(getState().convOutlineOpen).toBe(!before);
    await waitFor(() => expect(btn.getAttribute('aria-pressed')).toBe(String(!before)));
  });
});

// #205 S1 / #228 S3 F1 — the ☰ outline toggle is viewport-aware, now keyed on
// the WIDE breakpoint (≥1101px), not mobile (≤640px): the persistent COLUMN
// pref (convOutlineOpen) flips only when WIDE; the ephemeral SHEET flag
// (convOutlineMobileOpen) flips across the whole no-column band (≤1100px =
// mobile AND the 641–1100 tablet band — so the tablet-band ☰ stops lying).
// `aria-pressed` tracks the EFFECTIVE state. The matchMedia stub must be
// installed BEFORE the reader renders so useIsWide's initial state reads it.
// The per-query stub keeps the 640 vs 1100 breakpoints distinct (the old
// stubMobileMedia returned ONE value for both queries, conflating them).
describe('ConversationReader outline toggle is viewport-aware (#205 S1 / #228 S3 F1)', () => {
  // Per-band resolver: tablet = NOT mobile AND NOT wide.
  function bandResolver(band: 'mobile' | 'tablet' | 'wide') {
    return (q: string): boolean => {
      if (q === MOBILE_MEDIA_QUERY) return band === 'mobile';
      if (q === WIDE_MEDIA_QUERY) return band === 'wide';
      return false;
    };
  }
  async function renderInConversations(items: ConversationItem[]) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-A' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="sess-A" />);
    await waitFor(() => expect(utils.container.querySelector('.conv-outline-toggle')).not.toBeNull());
    return utils;
  }

  it('wide (≥1101px): ☰ toggles the persisted COLUMN flag, sheet flag untouched', async () => {
    stubResponsiveMedia(bandResolver('wide'));
    _resetForTests();
    const { container } = await renderInConversations([makeItem({ uuid: 'h1' })]);
    const btn = container.querySelector<HTMLButtonElement>('.conv-outline-toggle')!;
    expect(getState().convOutlineMobileOpen).toBe(false);
    const before = getState().convOutlineOpen;
    fireEvent.click(btn);
    expect(getState().convOutlineOpen).toBe(!before);       // column flag flips
    expect(getState().convOutlineMobileOpen).toBe(false);    // sheet flag untouched
  });

  it('tablet band (641–1100): ☰ toggles the ephemeral SHEET flag, never the persisted pref', async () => {
    // F1's new behavior: the tablet-band ☰ is now LIVE — it opens the sheet
    // (the column is hidden ≤1100px), keyed on !isWide while isMobile is FALSE.
    stubResponsiveMedia(bandResolver('tablet'));
    _resetForTests();
    localStorage.removeItem('cctally.conv.outlineOpen');
    const { container } = await renderInConversations([makeItem({ uuid: 'h1' })]);
    const btn = container.querySelector<HTMLButtonElement>('.conv-outline-toggle')!;
    fireEvent.click(btn);
    expect(getState().convOutlineMobileOpen).toBe(true);     // sheet flag flips
    expect(localStorage.getItem('cctally.conv.outlineOpen')).toBeNull(); // pref untouched
    await waitFor(() => expect(btn.getAttribute('aria-pressed')).toBe('true')); // EFFECTIVE state
  });

  it('mobile (≤640px): ☰ toggles the ephemeral SHEET flag and never mutates the persisted pref', async () => {
    stubResponsiveMedia(bandResolver('mobile'));
    _resetForTests();
    localStorage.removeItem('cctally.conv.outlineOpen');
    const { container } = await renderInConversations([makeItem({ uuid: 'h1' })]);
    const btn = container.querySelector<HTMLButtonElement>('.conv-outline-toggle')!;
    fireEvent.click(btn);
    expect(getState().convOutlineMobileOpen).toBe(true);
    expect(localStorage.getItem('cctally.conv.outlineOpen')).toBeNull();
    await waitFor(() => expect(btn.getAttribute('aria-pressed')).toBe('true')); // reflects EFFECTIVE state
  });
});

// ---- #177 S5 §5 — focus modes + jump-to-next ------------------------------
function oTurn(over: Partial<OutlineTurn> & { uuid: string; kind: OutlineTurn['kind'] }): OutlineTurn {
  return {
    ts: null, label: over.uuid, member_uuids: [over.uuid], subagent_key: null,
    parent_uuid: null, is_sidechain: false, ...over,
  };
}

describe('ConversationReader focus modes (#177 S5 §5)', () => {
  async function renderWithOutline(items: ConversationItem[], outline: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    const thread = utils.container.querySelector('.conv-reader-thread') as HTMLElement;
    return { ...utils, thread };
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });
  // #232 — the focus ring class lands on the INNER node inside Virtuoso's
  // `.conv-reader-item` wrapper; resolve it for the cursor asserts.
  const row = (thread: HTMLElement, i: number): Element => thread.children[i].firstElementChild ?? thread.children[i];

  const baseOutline = (turns: OutlineTurn[], errorCount = 0): ConversationOutline => ({
    session_id: 's',
    stats: {
      turns: { total: turns.length, human: 0, assistant: 0, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: errorCount, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
      cache_saved_usd: 0,
    },
    turns,
  });

  it('the segmented control renders a labeled radiogroup with four modes + an error badge', async () => {
    // #217 S3 E10#2 — the badge counts error TURNS (== the jump chip), so build
    // three turns each carrying an error tool. (stats.error_count is set to a
    // DIFFERENT value to prove the badge no longer reads from it.)
    const { container } = await renderWithOutline(
      [makeItem({ uuid: 'h1' })],
      baseOutline(
        [
          oTurn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
          oTurn({ uuid: 'a2', kind: 'assistant', tools: [{ name: 'Read', is_error: true }] }),
          oTurn({ uuid: 'a3', kind: 'assistant', tools: [{ name: 'Edit', is_error: true }] }),
        ],
        99, // server error_count is intentionally NOT 3
      ),
    );
    const seg = container.querySelector('[role="radiogroup"][aria-label="Focus mode"]')!;
    expect(seg).not.toBeNull();
    const radios = seg.querySelectorAll('[role="radio"]');
    expect(radios).toHaveLength(4);
    // All is active by default.
    expect(seg.querySelector('[aria-checked="true"]')!.textContent).toContain('All');
    // Errors carries the error-TURN count badge (3), not stats.error_count (99).
    expect(container.querySelector('.conv-focus-seg-badge')!.textContent).toBe('3');
  });

  // #217 S3 E10#2 — the Errors badge shows the error-TURN count (== the jump
  // cluster chip == what clicking the filter navigates), NOT the server's total
  // error-EVENT count (stats.error_count). Here the server reports 5 error events
  // but they all live on ONE turn, so the badge must read 1.
  it('the Errors badge reflects the error-turn count, not the server error_count', async () => {
    const { container } = await renderWithOutline(
      [makeItem({ uuid: 'h1' }), makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0 } as never)],
      baseOutline(
        [
          oTurn({ uuid: 'h1', kind: 'human' }),
          // ONE error turn carrying TWO error tools (and more events counted
          // server-side) — the turn count is 1 regardless of event multiplicity.
          oTurn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }, { name: 'Read', is_error: true }] }),
        ],
        5, // server total error events
      ),
    );
    // Badge == error-turn count (1), NOT the server total (5).
    expect(container.querySelector('.conv-focus-seg-badge')!.textContent).toBe('1');
  });

  // #217 S3 E10#3 — the Find control leads with a ConvIcons SVG glyph, matching
  // every other reader control, instead of the inline 🔍 emoji.
  it('the Find toggle renders an SVG icon, not the 🔍 emoji', async () => {
    const { container } = await renderWithOutline(
      [makeItem({ uuid: 'h1' })],
      baseOutline([oTurn({ uuid: 'h1', kind: 'human' })]),
    );
    const find = container.querySelector('.conv-find-toggle')!;
    expect(find.querySelector('svg.conv-ico')).not.toBeNull();
    expect(find.textContent).not.toContain('🔍');
  });

  it('v cycles the focus mode all → chat → prompts → errors → all', async () => {
    await renderWithOutline([makeItem({ uuid: 'h1' })], baseOutline([oTurn({ uuid: 'h1', kind: 'human' })]));
    expect(getState().convFocusMode).toBe('all');
    press('v'); expect(getState().convFocusMode).toBe('chat');
    press('v'); expect(getState().convFocusMode).toBe('prompts');
    press('v'); expect(getState().convFocusMode).toBe('errors');
    press('v'); expect(getState().convFocusMode).toBe('all');
  });

  it('prompts mode hides non-human turns behind a hidden-run marker', async () => {
    const { container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0,
          blocks: [{ kind: 'tool_call', name: 'Read', input_summary: '{}', preview: '/a',
            tool_use_id: 't', result: { text: 'ok', truncated: false, is_error: false } }] } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => expect(container.querySelector('.conv-hidden-run')).not.toBeNull());
    const marker = container.querySelector('.conv-hidden-run')!;
    expect(marker.textContent).toContain('1 hidden');
    // The marker carries data-conv-marker so j/k never land on it.
    expect((marker as HTMLElement).dataset.convMarker).toBe('');
  });

  // #217 S3 E10#1 — the `· N hidden ·` pill is icon-like prose; give it an
  // aria-label so a screen reader announces the action, not just the glyph run.
  it('the hidden-run pill carries an aria-label naming the count', async () => {
    const { container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0 } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    const marker = await waitFor(() => container.querySelector('.conv-hidden-run')!);
    expect(marker.getAttribute('aria-label')).toMatch(/1 hidden turn/i);
  });

  it('clicking a hidden-run marker resets to all and jumps to the first hidden turn', async () => {
    const { container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0 } as never),
      ],
      baseOutline([oTurn({ uuid: 'h1', kind: 'human' }), oTurn({ uuid: 'a1', kind: 'assistant' })]),
    );
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    const marker = await waitFor(() => container.querySelector('.conv-hidden-run')!);
    fireEvent.click(marker);
    expect(getState().convFocusMode).toBe('all');
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
  });

  it('switching to a mode that hides the focused turn remaps focus to the nearest visible turn, and j/k skip the marker', async () => {
    const { thread } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0 } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    // Focus the assistant turn (index 1).
    press('j');
    expect(row(thread, 1)).toHaveClass('conv-item--focused');
    // Switch to prompts: a1 is hidden (a hidden_run marker takes its slot). The
    // remap must move focus onto a real, visible human turn — never the marker.
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => expect(thread.querySelector('.conv-hidden-run')).not.toBeNull());
    const focused = thread.querySelector('.conv-item--focused')!;
    expect(focused.classList.contains('conv-hidden-run')).toBe(false);
    expect(focused.getAttribute('data-uuid')).toMatch(/h[12]/);
    // j/k still navigate and never settle on the marker.
    press('j');
    expect(thread.querySelector('.conv-item--focused')!.classList.contains('conv-hidden-run')).toBe(false);
    press('k');
    expect(thread.querySelector('.conv-item--focused')!.classList.contains('conv-hidden-run')).toBe(false);
  });

  // Regression (cross-branch P2): the remap target must resolve in RENDERED-NODE
  // space (`nodes` = what the thread actually renders, time markers AND
  // hidden_run markers included), NOT the marker-less `visible` space. The
  // focused cursor (`focusedIndex`) indexes thread.children = nodes-space, so
  // both the prev-list it reads AND the target it computes must live in
  // nodes-space too. When time markers precede the focused turn in the PRIOR
  // render, the old visible-space `prevVisibleRef[focusedIndex]` reads the wrong
  // slot (or undefined → the remap bails and leaves focusedIndex dangling past
  // the new child count → focus blanks entirely).
  it('remaps focus in rendered-node space when time markers precede the target', async () => {
    // h1 @14:00 (human), a1 @14:20 (tool-only assistant → hidden in prompts),
    // h2 @14:40 (human). ≥10-min gaps mean a time marker precedes BOTH a1 and h2.
    //   ALL nodes:    [h1, marker, a1, marker, h2]            (h2 at index 4)
    //   ALL visible:  [h1, a1, h2]                            (h2 at index 2)
    //   PROMPTS nodes:[h1, hidden_run, marker(h1→h2 40min), h2] (h2 at index 3)
    // The cursor on h2 is nodes-index 4; a visible-space prev list (length 3)
    // has no [4], so the buggy remap bails and focus is lost on the switch.
    const { thread, container } = await renderWithOutline(
      [
        makeItem({ uuid: 'h1', kind: 'human', text: 'hi', ts: '2026-06-12T14:00:00Z' } as never),
        makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0,
          ts: '2026-06-12T14:20:00Z',
          blocks: [{ kind: 'tool_call', name: 'Read', input_summary: '{}', preview: '/a',
            tool_use_id: 't', result: { text: 'ok', truncated: false, is_error: false } }] } as never),
        makeItem({ uuid: 'h2', kind: 'human', text: 'bye', ts: '2026-06-12T14:40:00Z' } as never),
      ],
      baseOutline([
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ]),
    );
    // ALL mode renders a time marker before a1 AND before h2.
    await waitFor(() => expect(container.querySelectorAll('.conv-time-marker')).toHaveLength(2));
    // Focus h2 (the later turn). stepFocus skips markers: h1→a1→(skip)→h2.
    press('j'); // a1
    press('j'); // h2 (marker skipped)
    expect(thread.querySelector('.conv-item--focused')!.getAttribute('data-uuid')).toBe('h2');
    // Switch to prompts: a1 collapses into a hidden_run; the 40-min h1→h2 gap
    // inserts a time marker BEFORE h2. The remap must still land focus on h2 —
    // resolved in nodes-space — never a marker, never blank.
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => expect(thread.querySelector('.conv-hidden-run')).not.toBeNull());
    const focused = thread.querySelector('.conv-item--focused');
    expect(focused).not.toBeNull();
    expect((focused as HTMLElement).dataset.convMarker).toBeUndefined();
    expect(focused!.classList.contains('conv-time-marker')).toBe(false);
    expect(focused!.classList.contains('conv-hidden-run')).toBe(false);
    expect(focused!.getAttribute('data-uuid')).toBe('h2');
  });
});

describe('ConversationReader jump-to-next keys (#177 S5 §4)', () => {
  async function renderWithOutline(items: ConversationItem[], outline: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    return utils;
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });

  const outline: ConversationOutline = {
    session_id: 's',
    stats: {
      turns: { total: 4, human: 2, assistant: 2, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 1, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
      cache_saved_usd: 0,
    },
    turns: [
      oTurn({ uuid: 'h1', kind: 'human' }),
      oTurn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
      oTurn({ uuid: 'h2', kind: 'human' }),
      oTurn({ uuid: 'a2', kind: 'assistant', tools: [{ name: 'ExitPlanMode', is_error: false }] }),
    ],
  };
  const items = [
    makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
    makeItem({ uuid: 'a1', kind: 'assistant', text: 'oops', model: 'm', cost_usd: 0 } as never),
    makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
    makeItem({ uuid: 'a2', kind: 'assistant', text: 'plan', model: 'm', cost_usd: 0 } as never),
  ];

  // The cursor resolves from convCurrentTurnUuid first, else the focused child's
  // data-uuid (focus starts on the first turn h1, index 0), else -1. Tests pin
  // the scroll-sync cursor where the jump origin matters.
  it('e jumps to the next error turn (cursor before the start)', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('e'); // first error strictly after h1 (idx0) → a1 (idx1)
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
  });

  it('u jumps to the next prompt after the cursor', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('u'); // next human after h1 (idx0) → h2 (idx2)
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'h2' });
  });

  it('U jumps to the previous prompt relative to the scroll-sync cursor', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h2' }); });
    press('U'); // previous prompt before h2 (idx2) → h1 (idx0)
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'h1' });
  });

  it('p jumps to the next plan/question turn', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('p');
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a2' });
  });

  it('a no-op jump (no target ahead) leaves the jump untouched', async () => {
    await renderWithOutline(items, outline);
    // Park the cursor past the only error (a1) so `e` forward finds nothing.
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'a2' }); });
    press('e');
    expect(getState().conversationJump).toBeNull();
  });
});

// #177 S5 §5 — the store reducer no longer blanket-resets focus mode on a
// same-session OPEN_CONVERSATION, so jumpNext's precise nodeVisible check is
// the sole authority for resetting to `all`. It must reset ONLY when the jump
// target is hidden by the current mode (e.g. an error target in Prompts mode),
// and leave the mode untouched when the target is already visible (e.g. an
// error target in Errors mode). This is the behavior the blanket reset masked.
describe('ConversationReader jump-to-next focus-mode reset (#177 S5 §5)', () => {
  async function renderWithOutline(items: ConversationItem[], outline: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    return utils;
  }
  const press = (key: string) => fireEvent.keyDown(document, { key });

  const errBlock = {
    kind: 'tool_call', name: 'Bash', input_summary: '{}', preview: 'x',
    tool_use_id: 'te', result: { text: 'boom', truncated: false, is_error: true },
  };
  const outline: ConversationOutline = {
    session_id: 's',
    stats: {
      turns: { total: 3, human: 1, assistant: 2, tool_result: 0, meta: 0 },
      tool_counts: {}, error_count: 1, models: {}, duration_seconds: null,
      tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
      cache_saved_usd: 0,
    },
    turns: [
      oTurn({ uuid: 'h1', kind: 'human' }),
      oTurn({ uuid: 'a1', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
      oTurn({ uuid: 'h2', kind: 'human' }),
    ],
  };
  const items = [
    makeItem({ uuid: 'h1', kind: 'human', text: 'hi' }),
    makeItem({ uuid: 'a1', kind: 'assistant', text: 'oops', model: 'm', cost_usd: 0,
      blocks: [errBlock] } as never),
    makeItem({ uuid: 'h2', kind: 'human', text: 'bye' }),
  ];

  it('Errors mode: e-jump to a VISIBLE error target does NOT reset the mode', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'errors' }); });
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('e'); // → a1, which IS visible in errors mode → no reset
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
    expect(getState().convFocusMode).toBe('errors');
  });

  it('Prompts mode: e-jump to an error target (hidden) DOES reset to all', async () => {
    await renderWithOutline(items, outline);
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    act(() => { dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'h1' }); });
    press('e'); // → a1, hidden in prompts mode → reset to all before jumping
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1' });
    expect(getState().convFocusMode).toBe('all');
  });
});

// ---- #177 S5 §6 — inter-turn time markers in the reader -------------------
describe('ConversationReader time markers (#177 S5 §6)', () => {
  async function renderInConversations(items: ConversationItem[], outline?: ConversationOutline) {
    mockFetchOnce(detail(items));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-thread')).not.toBeNull());
    return utils;
  }

  it('inserts one gap marker between two turns 42 minutes apart', async () => {
    const { container } = await renderInConversations([
      makeItem({ uuid: 'h1', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'h2', ts: '2026-06-12T14:42:00Z' } as never),
    ]);
    await waitFor(() => expect(container.querySelector('.conv-time-marker')).not.toBeNull());
    const markers = container.querySelectorAll('.conv-time-marker');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toContain('42 min later');
    // role="separator" + data-conv-marker → not a keyboard stop.
    expect(markers[0].getAttribute('role')).toBe('separator');
    expect((markers[0] as HTMLElement).dataset.convMarker).toBe('');
  });

  it('emits no marker when adjacent turns are under 10 minutes apart', async () => {
    const { container } = await renderInConversations([
      makeItem({ uuid: 'h1', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'h2', ts: '2026-06-12T14:05:00Z' } as never),
    ]);
    // Let the render settle, then assert no marker.
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());
    expect(container.querySelector('.conv-time-marker')).toBeNull();
  });

  it('recomputes markers over the visible sequence when the focus mode hides the middle turn', async () => {
    // h1 @14:00, a1 (tool-only assistant, hidden in prompts) @14:05, h2 @14:50.
    // ALL mode: h1→a1 = 5 min (no marker), a1→h2 = 45 min (one "45 min later").
    // PROMPTS mode: a1 is hidden, so h1→h2 spans 50 min → one "50 min later".
    const outline: ConversationOutline = {
      session_id: 's',
      stats: {
        turns: { total: 3, human: 2, assistant: 1, tool_result: 0, meta: 0 },
        tool_counts: {}, error_count: 0, models: {}, duration_seconds: null,
        tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
        cache_saved_usd: 0,
      },
      turns: [
        oTurn({ uuid: 'h1', kind: 'human' }),
        oTurn({ uuid: 'a1', kind: 'assistant' }),
        oTurn({ uuid: 'h2', kind: 'human' }),
      ],
    };
    const { container } = await renderInConversations([
      makeItem({ uuid: 'h1', kind: 'human', text: 'hi', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'a1', kind: 'assistant', text: '', model: 'm', cost_usd: 0,
        ts: '2026-06-12T14:05:00Z',
        blocks: [{ kind: 'tool_call', name: 'Read', input_summary: '{}', preview: '/a',
          tool_use_id: 't', result: { text: 'ok', truncated: false, is_error: false } }] } as never),
      makeItem({ uuid: 'h2', kind: 'human', text: 'bye', ts: '2026-06-12T14:50:00Z' } as never),
    ], outline);

    // ALL mode: a single "45 min later" marker between a1 and h2.
    await waitFor(() => expect(container.querySelector('.conv-time-marker')).not.toBeNull());
    let markers = container.querySelectorAll('.conv-time-marker');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toContain('45 min later');

    // Switch to prompts: a1 vanishes (hidden_run takes its place); the gap now
    // spans h1→h2 = 50 min, recomputed over the visible sequence.
    act(() => { dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' }); });
    await waitFor(() => {
      const m = container.querySelectorAll('.conv-time-marker');
      return expect(m[0]?.textContent).toContain('50 min later');
    });
    markers = container.querySelectorAll('.conv-time-marker');
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toContain('50 min later');
  });
});

// #184 — scroll-sync PRODUCER coverage. The reader registers an
// IntersectionObserver over its rendered turns; on a change it dispatches the
// topmost-visible turn's data-uuid to convCurrentTurnUuid. jsdom never lays out
// and the default IO stub is a no-op, so this drives the producer directly: a
// capturing stub records every observer callback, then the test invokes the one
// the reader observed turns with — feeding synthetic intersecting entries whose
// targets carry data-uuid + a mocked getBoundingClientRect top — and asserts the
// store cursor becomes the topmost uuid.
describe('ConversationReader scroll-sync producer (#177 S5 §3 / #184)', () => {
  // A capturing IntersectionObserver: each instance records its callback and the
  // elements it observed, so a test can replay the callback by hand.
  type CapturedObs = { cb: IntersectionObserverCallback; targets: Element[] };
  let observers: CapturedObs[] = [];
  function installCapturingObserver() {
    class Capturing {
      cb: IntersectionObserverCallback;
      targets: Element[] = [];
      constructor(cb: IntersectionObserverCallback) {
        this.cb = cb;
        observers.push(this as unknown as CapturedObs);
      }
      observe(el: Element): void { this.targets.push(el); }
      unobserve(): void {}
      disconnect(): void {}
      takeRecords(): IntersectionObserverEntry[] { return []; }
    }
    (globalThis as unknown as { IntersectionObserver: typeof Capturing }).IntersectionObserver = Capturing;
  }

  beforeEach(() => { observers = []; installCapturingObserver(); });

  it('dispatches the topmost intersecting turn uuid to the store', async () => {
    mockFetchOnce(detail([
      makeItem({ uuid: 'h1', ts: '2026-06-12T14:00:00Z' } as never),
      makeItem({ uuid: 'h2', ts: '2026-06-12T14:01:00Z' } as never),
    ]));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="h2"]')).not.toBeNull());

    const elH1 = container.querySelector('[data-uuid="h1"]') as HTMLElement;
    const elH2 = container.querySelector('[data-uuid="h2"]') as HTMLElement;
    // h2 sits ABOVE h1 in viewport space (smaller top) — it must win.
    vi.spyOn(elH1, 'getBoundingClientRect').mockReturnValue({ top: 200 } as DOMRect);
    vi.spyOn(elH2, 'getBoundingClientRect').mockReturnValue({ top: 40 } as DOMRect);

    // The scroll-sync observer is the one that observed the rendered turn
    // elements (the lazy-load observer observes the sentinel, not these). It
    // registers in an effect keyed on the rendered set, so its observe() can lag
    // the turn render by a tick under test reordering — wait for it rather than
    // grabbing it synchronously (a bare find() flaked ~1/40 under shuffle). The
    // turn nodes are memoized + keyed, so elH1/elH2 identity is stable across
    // polls and the rect spies above stay attached to the observed elements.
    let obs: CapturedObs | undefined;
    await waitFor(() => {
      obs = observers.find((o) => o.targets.includes(elH1) || o.targets.includes(elH2));
      expect(obs).toBeDefined();
    });

    act(() => {
      obs!.cb(
        [
          { target: elH1, isIntersecting: true } as unknown as IntersectionObserverEntry,
          { target: elH2, isIntersecting: true } as unknown as IntersectionObserverEntry,
        ],
        obs as unknown as IntersectionObserver,
      );
    });
    expect(getState().convCurrentTurnUuid).toBe('h2');

    // When h2 scrolls out (no longer intersecting), the topmost falls back to h1.
    act(() => {
      obs!.cb(
        [{ target: elH2, isIntersecting: false } as unknown as IntersectionObserverEntry],
        obs as unknown as IntersectionObserver,
      );
    });
    expect(getState().convCurrentTurnUuid).toBe('h1');
  });
});

// #177 S6 — reader-level wiring of the in-conversation find bar: find →
// jump → disclosure-expand. Parent-level integration (the modal-integration
// precedent): a mocked /find response whose anchor matched in a tool block,
// the target turn carrying a COLLAPSED <details>; opening find, typing, and
// pressing Enter must (a) dispatch the jump with expand_details, (b) open the
// turn's <details>, (c) flash it with conv-item--jumped.
describe('ConversationReader in-conversation find', () => {
  // A tool_call block renders a CLOSED <details className="conv-chip--tool">.
  function detailWithTool() {
    const assistant: ConversationItem = {
      kind: 'assistant',
      anchor: { session_id: 's', uuid: 'a1', id: 2 },
      member_uuids: ['a1'],
      ts: 't',
      text: '',
      blocks: [
        {
          kind: 'tool_call', name: 'Bash', input_summary: 'rg needle',
          preview: 'rg needle', tool_use_id: 'tu1',
          result: { text: 'found needle', truncated: false, is_error: false },
        },
      ],
      model: 'claude-opus-4',
      is_sidechain: false,
      subagent_key: null,
      parent_uuid: null,
      cost_usd: 0.01,
    } as ConversationItem;
    return detail([makeItem({ uuid: 'h1', text: 'opening prompt' }), assistant]);
  }

  function installFindRoutedFetch(findBody: unknown) {
    globalThis.fetch = vi.fn(async (url: string | URL) => {
      const u = String(url);
      const body = u.includes('/find') ? findBody : detailWithTool();
      return { ok: true, status: 200, json: async () => body } as Response;
    }) as unknown as typeof fetch;
  }

  it('typing + Enter jumps to the matched turn, opens its collapsed details, and flashes it', async () => {
    const scrollToSpy = spyScrollTo();
    installFindRoutedFetch({
      anchors: [{ uuid: 'a1', match_kinds: ['tool'] }],
      total: 1, anchors_truncated: false, mode: 'fts', search_depth: 'full',
    });
    installGlobalKeydown();
    // Land on the session so OPEN_CONVERSATION jumps are same-session (find stays open).
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });

    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull());

    // The matched turn's tool disclosure starts CLOSED.
    const det = container.querySelector('[data-uuid="a1"] details.conv-chip--tool') as HTMLDetailsElement;
    expect(det).not.toBeNull();
    expect(det.open).toBe(false);

    // Open the find bar (the '/' rebind dispatches this; here we drive the store
    // directly to keep the test reader-scoped).
    act(() => { dispatch({ type: 'OPEN_CONV_FIND' }); });
    const input = await waitFor(() => {
      const el = container.querySelector<HTMLInputElement>('.conv-findbar-input');
      expect(el).not.toBeNull();
      return el!;
    });

    // Type a needle → debounced find fetch → one anchor.
    fireEvent.change(input, { target: { value: 'needle' } });
    await waitFor(() => expect(container.querySelector('.conv-findbar-count')!.textContent).toContain('1 / 1'));

    // Enter steps to the (only) anchor and jumps with expand_details (tool match).
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }); });
    expect(getState().conversationJump).toEqual({ session_id: 's', uuid: 'a1', expand_details: true });

    // The jump effect opens the disclosure, direct-scrolls the scroller, and flashes the turn.
    await waitFor(() => expect((container.querySelector('[data-uuid="a1"] details.conv-chip--tool') as HTMLDetailsElement).open).toBe(true));
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    await waitFor(() => {
      const target = container.querySelector('[data-uuid="a1"]')!;
      expect(target.classList.contains('conv-item--jumped')).toBe(true);
    });
  });
});

// #188 — deriveReaderTitle picks a promoted slash-command turn. A promoted
// command is kind='human' with text=args (NOT a marker), so the existing
// first-real-human-line logic selects it; the title is the args, not the
// project label.
describe('deriveReaderTitle (#188 promoted command)', () => {
  it('uses the args of a promoted slash-command first turn as the title', () => {
    const title = deriveReaderTitle({
      project_label: 'cctally-dev',
      session_id: 's',
      items: [
        makeItem({
          uuid: 'pc1',
          kind: 'human',
          text: 'Audit the reader UI and file issues.',
          command_name: '/frontend-design',
          blocks: [
            {
              kind: 'text',
              text:
                '<command-name>/frontend-design</command-name>' +
                '<command-args>Audit the reader UI and file issues.</command-args>',
            },
          ],
        } as never),
      ],
    });
    expect(title).toBe('Audit the reader UI and file issues.');
  });

  it('still falls back to the project label when the first turn is a hidden /clear', () => {
    const title = deriveReaderTitle({
      project_label: 'cctally-dev',
      session_id: 's',
      items: [
        makeItem({
          uuid: 'm1',
          kind: 'meta',
          text: '<command-name>/clear</command-name><command-args></command-args>',
          meta_kind: 'command',
          skill_name: null,
        } as never),
      ],
    });
    expect(title).toBe('cctally-dev');
  });
});

// ---- #205 S3 (F6) — reader meta model abbreviation on mobile ----------
describe('reader meta model abbreviation (#205 S3 F6)', () => {
  function multiModelDetail() {
    const d = detail([makeItem({ uuid: 'h1' })]);
    d.models = ['claude-haiku-4-5-20251001', 'claude-opus-4-8'];
    return d;
  }

  it('abbreviates the model list on mobile', async () => {
    stubMobileMedia(true);
    _resetForTests();
    mockFetchOnce(multiModelDetail());
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-meta')).not.toBeNull());
    const meta = container.querySelector('.conv-reader-meta')!.textContent!;
    expect(meta).toContain('haiku-4-5');
    expect(meta).toContain('opus-4-8');
    expect(meta).not.toContain('claude-haiku-4-5-20251001');
  });

  it('renders the full ids on desktop', async () => {
    stubMobileMedia(false);
    _resetForTests();
    mockFetchOnce(multiModelDetail());
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('.conv-reader-meta')).not.toBeNull());
    const meta = container.querySelector('.conv-reader-meta')!.textContent!;
    expect(meta).toContain('claude-haiku-4-5-20251001');
  });
});

// ---- #228 S3 C2 — two-row mobile reader header (≤640px only) -----------
// At ≤640px the reader header collapses: Row 1 (Back · title · ⋯), Row 2 (the
// compact Focus dropdown · Find · Outline). The secondary actions (Export,
// Compare with…, Latest ↓, Expand-all, Collapse-all) move INTO the ⋯ overflow
// menu, and the 4-button focus segment becomes the compact Focus dropdown.
// Desktop/tablet keep the full inline controls unchanged. matchMedia-mocked
// (per-query) so the 640 vs 1100 breakpoints stay distinct; the visual
// first-paint reclaim + 44px targets are the ui-qa gate.
describe('ConversationReader two-row mobile header (#228 S3 C2)', () => {
  function bandResolver(band: 'mobile' | 'wide') {
    return (q: string): boolean => {
      if (q === MOBILE_MEDIA_QUERY) return band === 'mobile';
      if (q === WIDE_MEDIA_QUERY) return band === 'wide';
      return false;
    };
  }
  async function renderHeader(band: 'mobile' | 'wide') {
    stubResponsiveMedia(bandResolver(band));
    _resetForTests();
    // last_anchor present so the "Latest ↓" control is reachable (it gates on it,
    // on desktop inline AND inside the mobile ⋯ menu).
    const d = detail([makeItem({ uuid: 'h1' })]);
    (d as { last_anchor?: unknown }).last_anchor = { session_id: 's', uuid: 'h1' };
    mockFetchOnce(d);
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const utils = render(<ConversationReader sessionId="s" mobileBack />);
    await waitFor(() => expect(utils.container.querySelector('.conv-reader-head')).not.toBeNull());
    return utils;
  }

  it('mobile (≤640): secondary actions live in the ⋯ menu, not inline; two slim rows render', async () => {
    const { container } = await renderHeader('mobile');
    // The ⋯ overflow trigger is present.
    const overflow = container.querySelector('.conv-overflow-toggle');
    expect(overflow).not.toBeNull();
    // The secondary actions are NOT inline in the header controls (they moved
    // into the ⋯ menu, which is closed). The inline Compare / Latest / Export
    // header buttons are absent.
    expect(container.querySelector('.conv-reader-controls .conv-compare-with')).toBeNull();
    expect(container.querySelector('.conv-reader-controls .conv-jump-latest')).toBeNull();
    expect(container.querySelector('.conv-reader-controls .conv-export')).toBeNull();
    // The 4-button focus segment is replaced by the compact Focus dropdown.
    expect(container.querySelector('.conv-focus-seg')).toBeNull();
    expect(container.querySelector('.conv-focus-compact-toggle')).not.toBeNull();
    // Row 2 keeps Find + Outline inline (the primary on-screen affordances).
    expect(container.querySelector('.conv-find-toggle')).not.toBeNull();
    expect(container.querySelector('.conv-outline-toggle')).not.toBeNull();
    // Two slim rows: the structural row wrappers.
    expect(container.querySelector('.conv-reader-head--mobile')).not.toBeNull();
    expect(container.querySelector('.conv-reader-row1')).not.toBeNull();
    expect(container.querySelector('.conv-reader-row2')).not.toBeNull();

    // Opening the ⋯ menu reveals the moved actions.
    fireEvent.click(overflow as HTMLButtonElement);
    const menu = screen.getByRole('menu', { name: /more actions/i });
    expect(within(menu).getByRole('menuitem', { name: /compare with/i })).not.toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /latest/i })).not.toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /expand all/i })).not.toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /collapse all/i })).not.toBeNull();
  });

  it('desktop (≥1101): the full inline controls render unchanged; no ⋯ menu', async () => {
    const { container } = await renderHeader('wide');
    // The full inline desktop controls are present.
    expect(container.querySelector('.conv-focus-seg')).not.toBeNull();      // 4-button segment
    expect(container.querySelector('.conv-export-toggle')).not.toBeNull();   // Export menu
    expect(container.querySelector('.conv-compare-with')).not.toBeNull();    // Compare inline
    expect(container.querySelector('.conv-jump-latest')).not.toBeNull();     // Latest inline
    expect(container.querySelector('.conv-find-toggle')).not.toBeNull();
    expect(container.querySelector('.conv-outline-toggle')).not.toBeNull();
    // The mobile collapse did NOT happen: no ⋯ menu, no compact focus dropdown,
    // no two-row wrappers.
    expect(container.querySelector('.conv-overflow-toggle')).toBeNull();
    expect(container.querySelector('.conv-focus-compact-toggle')).toBeNull();
    expect(container.querySelector('.conv-reader-head--mobile')).toBeNull();
  });
});

// #217 S3 E2 — open precedence + the top sentinel (bidirectional pager) +
// E1 reading-position restore + E8 last-prompt/last-error jumps. Parent-level
// integration: drive the REAL hook + store pipeline, asserting on content /
// cursor state — NOT pixel scrollTop or SSE frame counts (the JSDOM limits).
describe('ConversationReader open precedence + reverse pager (#217 S3 E2/E1/E8)', () => {
  // A detail with explicit edge keys (the bidirectional page envelope).
  function edged(
    items: ConversationItem[],
    edges: { next_after?: number | null; has_more?: boolean; prev_before?: number | null; has_prev?: boolean },
    lastUuid: string | null = null,
  ) {
    const base = detail(items, edges.next_after ?? null);
    return {
      ...base,
      page: {
        next_after: edges.next_after ?? null,
        has_more: edges.has_more ?? (edges.next_after != null),
        prev_before: edges.prev_before ?? null,
        has_prev: edges.has_prev ?? false,
      },
      last_anchor: lastUuid == null ? null : { session_id: 's', uuid: lastUuid, id: _idFor(lastUuid) },
    };
  }
  function outlineFor(turns: OutlineTurn[], errorCount = 0): ConversationOutline {
    return {
      session_id: 's',
      stats: {
        turns: { total: turns.length, human: 0, assistant: 0, tool_result: 0, meta: 0 },
        tool_counts: {}, error_count: errorCount, models: {}, duration_seconds: null,
        tokens: { input: 0, output: 0, cache_creation: 0, cache_read: 0 }, cost_usd: 0,
        cache_saved_usd: 0,
      },
      turns,
    };
  }

  it('a multi-page session opens at the BOTTOM via ?tail=1 (no head-fetch first)', async () => {
    // tail page: last window, has_prev:true (more above) ⇒ land at the bottom.
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());
    // The FIRST request was ?tail=1 — not the legacy head page.
    expect(String((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0])).toContain('tail=1');
    // #232 — reverse paging is now driven by Virtuoso's `startReached`, not a top
    // sentinel. Firing it (has_prev:true) issues the ?before= reverse fetch — but
    // only once paging is ARMED. A tail open settles at the bottom, so Virtuoso's
    // atBottomStateChange(true) fires and arms both edges (the cold-load freeze
    // guard: the gate stays disarmed through the open's transient edge hits).
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.atBottomStateChange?.(true);   // settle → arm paging
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some((c) => String(c[0]).includes('before=3'))).toBe(true);
  });

  it('a single-page session opens (?tail=1 → has_prev:false) — startReached is a no-op', async () => {
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { prev_before: null, has_prev: false }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t2"]')).not.toBeNull());
    expect(String((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0])).toContain('tail=1');
    // #232 — single page: nothing above, so a startReached fires no reverse fetch.
    const callsBefore = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    await act(async () => {
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore);
  });

  it('startReached triggers loadPrev → a ?before= prepend (#232)', async () => {
    // #232 — reverse paging is driven by Virtuoso's `startReached` (the head
    // scrolling into view), replacing the deleted top-sentinel IntersectionObserver.
    // tail open with a top edge armed (prev_before 3).
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());

    // The before-page (the earlier window). Its envelope carries a bottom edge
    // for the already-loaded items — the prepend must ignore it.
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));

    // Arm paging (the tail open settles at the bottom → atBottomStateChange(true)),
    // then fire Virtuoso's startReached — the reader runs doLoadPrev. The arming
    // step is the #232 freeze guard: startReached no-ops until the open settles.
    await act(async () => {
      virtuosoTestHandle.atBottomStateChange?.(true);
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });

    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());
    // The reverse fetch went to ?before=3 (the tail page's top cursor).
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.some((c) => String(c[0]).includes('before=3'))).toBe(true);
    // The window now holds the prepended items in order (t1,t2,t3,t4).
    const order = Array.from(container.querySelectorAll('[data-uuid]')).map((e) => e.getAttribute('data-uuid'));
    expect(order.filter((u) => /^t[1-4]$/.test(u ?? ''))).toEqual(['t1', 't2', 't3', 't4']);
  });

  it('#232 freeze guard: startReached does NOT page while paging is UNARMED, and DOES once armed', async () => {
    // The cold-deep-link freeze (P0): on a cold open Virtuoso fires startReached as
    // it settles the initial position, BEFORE any user scroll. Paging on that
    // transient edge hit re-enters the very drain positioning the window, and (with
    // the jump effect re-firing on every prepend it causes) spawns concurrent
    // loadToTarget drains that LIVELOCK on the overlap flags — pinning the main
    // thread forever. The fix gates startReached/endReached behind an arming flag
    // that flips only once the open has SETTLED (first atBottomStateChange / jump
    // landing / 750ms fallback). This proves the gate: an UNARMED startReached
    // issues NO ?before= fetch; an ARMED one does.
    //
    // NON-VACUITY: against the pre-fix code (startReached → doLoadPrev unguarded)
    // the first assertion FAILS — the unarmed startReached would already have
    // issued before=3. The arming gate is exactly what makes the unarmed fire a
    // no-op while preserving the armed fire.
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());

    const beforeCalls = () =>
      (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.filter((c) => String(c[0]).includes('before=3')).length;

    // (1) UNARMED — fire startReached repeatedly: NO reverse fetch is issued and
    // NO earlier page mounts. This is the cold-mount transient the freeze rode.
    await act(async () => {
      virtuosoTestHandle.startReached?.();
      virtuosoTestHandle.startReached?.();
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    expect(beforeCalls()).toBe(0);
    expect(container.querySelector('[data-uuid="t1"]')).toBeNull();

    // (2) ARM via the settle signal (atBottomStateChange), then fire startReached:
    // now the genuine reverse page loads — real user reverse-paging is preserved.
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.atBottomStateChange?.(true);   // open settled → arm paging
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());
    expect(beforeCalls()).toBe(1);
  });

  it('#232: firstItemIndex decrements by the prepended count, pinning the virtual index of the old head', async () => {
    // The Virtuoso firstItemIndex (owned in useConversation, T2) must DROP by the
    // count prepended so data[0]'s virtual index stays stable across a reverse
    // page — the mechanism that pins the viewport. We read it off the render-all
    // mock's exposed handle + the VIRTUAL data-index on the rendered rows.
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());
    const before = virtuosoTestHandle.firstItemIndex;
    // t3 is the head node here — capture its current virtual index.
    const t3Wrap = (container.querySelector('[data-uuid="t3"]')!.closest('.conv-reader-item')) as HTMLElement;
    const t3VirtualBefore = Number(t3Wrap.getAttribute('data-index'));

    // Prepend a 2-item reverse page (arm paging first — #232 freeze guard).
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.atBottomStateChange?.(true);
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());

    // firstItemIndex dropped by exactly the prepended count (2).
    expect(virtuosoTestHandle.firstItemIndex).toBe(before - 2);
    // t3's VIRTUAL index is unchanged — its viewport position is pinned even
    // though its ARRAY position moved from 0 to 2.
    const t3WrapAfter = (container.querySelector('[data-uuid="t3"]')!.closest('.conv-reader-item')) as HTMLElement;
    expect(Number(t3WrapAfter.getAttribute('data-index'))).toBe(t3VirtualBefore);
  });

  it('#232: the keyboard cursor ring stays on the same turn across a reverse-page prepend', async () => {
    // The render-driven cursor ring is keyed on the cursor's TURN UUID (not the
    // array index — #231 memo invariant), so a prepend that shifts indices must
    // NOT move the ring to a different turn. The default cursor lands on the first
    // turn (t3); after prepending t1,t2 the ring must STILL be on t3.
    installGlobalKeydown();
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());
    // The default cursor ring is on t3 (the first turn).
    await waitFor(() => expect(container.querySelector('[data-uuid="t3"]')!.classList.contains('conv-item--focused')).toBe(true));

    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.atBottomStateChange?.(true);   // arm paging (#232 freeze guard)
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());

    // The ring is STILL on t3 (same turn, now at a later index) — not on the new
    // head t1, and not lost.
    expect(container.querySelector('[data-uuid="t3"]')!.classList.contains('conv-item--focused')).toBe(true);
    expect(container.querySelector('[data-uuid="t1"]')!.classList.contains('conv-item--focused')).toBe(false);
  });

  it('#228 S3 B3: a tail-open prepend is recognised via op metadata — no stick, no "↓ N new" mis-fire', async () => {
    // This proves the reader keys its live-append/stick + "↓ N new" pill paths on
    // the hook's explicit WindowOp (op==='prepend' / addedBottom), NOT on
    // items.length / firstId. The TRAP this fixes: a prepend at a TAIL open
    // (hasMore===false) grows items.length AND satisfies the old live
    // discriminator, so a count/firstId inference would mis-count the prepended
    // turns as "↓ N new" and could wrongly stick-to-bottom. (The pixel-level
    // scroll-anchor restore itself is a layout concern verified at the ui-qa gate,
    // not in JSDOM, which never lays out — here we assert the LOGIC: the op routes
    // the prepend away from the live-append path.) This is also the count-flat
    // case the windowed-cap trim will hit (a prepend+far-trim keeps len flat).
    // #232 — the prepend is now triggered via Virtuoso's startReached.

    // Tail open at the bottom (hasMore===false, has_prev:true).
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { next_after: null, has_more: false, prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());

    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    setScroll(body, { scrollTop: 500, clientHeight: 100, scrollHeight: 1000 });
    fireEvent.scroll(body);
    const scrollToSpy = spyScrollTo();

    // The before-page (the earlier window). The prepend grows items.length while
    // hasMore stays false — the exact discriminator trap.
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.atBottomStateChange?.(true);   // arm paging (#232 freeze guard)
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());

    // The op routed the prepend AWAY from the live-append path: no stick-to-bottom
    // and no "↓ N new" pill, even though items.length grew and hasMore was false.
    expect(scrollToSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
    // And the prepended items landed in order (the prepend itself succeeded).
    const order = Array.from(container.querySelectorAll('[data-uuid]')).map((e) => e.getAttribute('data-uuid'));
    expect(order.filter((u) => /^t[1-4]$/.test(u ?? ''))).toEqual(['t1', 't2', 't3', 't4']);
  });

  it('open precedence slot 1: a deep-link anchor overrides a saved reading position', async () => {
    // A saved reading position for the session exists, AND a deep-link jump is set
    // — the deep-link wins (it pages to the anchor). We assert the jump uuid is
    // honored (the anchor's turn is reached + flashed), not the saved one.
    clearReadingPositions();
    recordReadingPos('s', 'saved-turn', 1000);
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    // Open at the tail (first fetch) for the anchor intent; the anchor target is
    // already in the tail window so loadToTarget is a no-op + the jump scrolls it.
    mockFetchOnce(edged([makeItem({ uuid: 'anchor-turn', member_uuids: ['anchor-turn'] } as never), makeItem({ uuid: 'last' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 'anchor-turn' } });
    const outline = outlineFor([oTurn({ uuid: 'anchor-turn', kind: 'human' }), oTurn({ uuid: 'last', kind: 'human' })]);
    const { container } = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(container.querySelector('[data-uuid="anchor-turn"]')).not.toBeNull());
    // The deep-link target flashed (the anchor won precedence).
    await waitFor(() => expect(container.querySelector('[data-uuid="anchor-turn"]')!.classList.contains('conv-item--jumped')).toBe(true));
    clearReadingPositions();
  });

  it('open precedence slot 2: a saved reading position is restored when there is no deep-link', async () => {
    // No deep-link jump; a saved reading position exists → the reader restores it
    // (loadToTarget(savedUuid) + flash). The first fetch is still ?tail=1 (the
    // natural resting place), then loadToTarget walks to the saved turn (already
    // in the tail window here) and the restore jump flashes it.
    clearReadingPositions();
    recordReadingPos('s', 'saved-turn', 1000);
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    mockFetchOnce(edged([makeItem({ uuid: 'saved-turn', member_uuids: ['saved-turn'] } as never), makeItem({ uuid: 'last' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const outline = outlineFor([oTurn({ uuid: 'saved-turn', kind: 'human' }), oTurn({ uuid: 'last', kind: 'human' })]);
    const { container } = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(container.querySelector('[data-uuid="saved-turn"]')).not.toBeNull());
    // The restore dispatched a jump → the saved turn landed + flashed + pinned.
    // The transient jump clears post-land, so assert on the persistent pin/flash.
    await waitFor(() => expect(getState().convPinnedUuid).toBe('saved-turn'));
    expect(container.querySelector('[data-uuid="saved-turn"]')!.classList.contains('conv-item--jumped')).toBe(true);
    clearReadingPositions();
  });

  it('E8: pressing `a` jumps to the LAST prompt; `L` jumps to the LAST error', async () => {
    clearReadingPositions();
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    // A session with two prompts (h1,h3) and two errors (a2,a4). The outline
    // drives the target lists; the items are all loaded (single page).
    const turns = [
      oTurn({ uuid: 'h1', kind: 'human' }),
      oTurn({ uuid: 'a2', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
      oTurn({ uuid: 'h3', kind: 'human' }),
      oTurn({ uuid: 'a4', kind: 'assistant', tools: [{ name: 'Bash', is_error: true }] }),
    ];
    mockFetchOnce(edged([
      makeItem({ uuid: 'h1', kind: 'human', text: 'one' }),
      makeItem({ uuid: 'a2', kind: 'assistant', text: 'oops', model: 'm', cost_usd: 0 } as never),
      makeItem({ uuid: 'h3', kind: 'human', text: 'two' }),
      makeItem({ uuid: 'a4', kind: 'assistant', text: 'boom', model: 'm', cost_usd: 0 } as never),
    ], { prev_before: null, has_prev: false }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" outline={outlineFor(turns, 2)} />);
    await waitFor(() => expect(container.querySelector('[data-uuid="a4"]')).not.toBeNull());

    // `a` → the LAST prompt (h3, the most-recent human turn). The jump pipeline
    // lands + PINS the target then clears the transient jump, so assert on the
    // landing pin (which persists) — not the jump (already cleared post-land).
    await act(async () => { fireEvent.keyDown(document, { key: 'a' }); for (let i = 0; i < 8; i++) await Promise.resolve(); });
    await waitFor(() => expect(getState().convPinnedUuid).toBe('h3'));
    expect(container.querySelector('[data-uuid="h3"]')!.classList.contains('conv-item--jumped')).toBe(true);

    // `L` → the LAST error (a4, the most-recent error turn).
    await act(async () => { fireEvent.keyDown(document, { key: 'L' }); for (let i = 0; i < 8; i++) await Promise.resolve(); });
    await waitFor(() => expect(getState().convPinnedUuid).toBe('a4'));
    clearReadingPositions();
  });

  it('E8: `a`/`L` are inert while a modal is open (shared guard) and a no-op when the family is empty', async () => {
    clearReadingPositions();
    // An outline with NO prompts and NO errors → both jumps are no-ops.
    const turns = [oTurn({ uuid: 'a1', kind: 'assistant', label: 'plain' })];
    mockFetchOnce(edged([makeItem({ uuid: 'a1', kind: 'assistant', text: 'plain', model: 'm', cost_usd: 0 } as never)], { prev_before: null, has_prev: false }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    installGlobalKeydown();
    const { container } = render(<ConversationReader sessionId="s" outline={outlineFor(turns)} />);
    await waitFor(() => expect(container.querySelector('[data-uuid="a1"]')).not.toBeNull());

    // Empty families → no jump dispatched.
    await act(async () => { fireEvent.keyDown(document, { key: 'a' }); fireEvent.keyDown(document, { key: 'L' }); await Promise.resolve(); });
    expect(getState().conversationJump).toBeNull();

    // Modal open → the keys are inert (the shared guard). Even with targets this
    // would not fire; here it asserts the guard path explicitly.
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'session' }); });
    await act(async () => { fireEvent.keyDown(document, { key: 'a' }); await Promise.resolve(); });
    expect(getState().conversationJump).toBeNull();
    clearReadingPositions();
  });

  // ── A controllable IntersectionObserver that captures every observed element
  //    + its callback so a sentinel can be fired into view deterministically
  //    (JSDOM has no real IO; the default test stub is a no-op). ───────────────
  function installCapturingIO() {
    const observed: { el: Element; cb: IntersectionObserverCallback; obs: IntersectionObserver }[] = [];
    class CapturingIO {
      cb: IntersectionObserverCallback;
      constructor(cb: IntersectionObserverCallback) { this.cb = cb; }
      observe(el: Element): void { observed.push({ el, cb: this.cb, obs: this as unknown as IntersectionObserver }); }
      unobserve(): void {}
      disconnect(): void {}
      takeRecords(): IntersectionObserverEntry[] { return []; }
    }
    (globalThis as unknown as { IntersectionObserver: typeof CapturingIO }).IntersectionObserver = CapturingIO;
    return observed;
  }

  // #217 S3 E2 — P0 regression at the REF/STATE level (the load-bearing one).
  // After a tail open lands 'bottom', the user scrolls UP (atBottomRef := false).
  // A reverse-page prepend must NOT re-force atBottomRef = true. We observe the
  // ref through its only public consequence: a subsequent live append shows the
  // "↓ N new" pill IFF atBottomRef === false. With the P0 bug the prepend
  // re-fires the open-intent effect (atBottomRef := true), so the live append
  // sticks-to-bottom and the pill never appears — RED. (Revert the one-shot guard
  // to reproduce.)
  it('P0: a reverse prepend does NOT re-arm atBottomRef (a later live append still raises the pill)', async () => {
    installCapturingIO();
    // tail open at the bottom; bottom edge fully paged (has_more:false) so the
    // live-tail poll path is eligible.
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());

    // #232 — the user scrolls UP, away from the bottom → Virtuoso reports
    // atBottomStateChange(false), which sets atBottomRef false (the old manual
    // scrollTop computation is gone).
    act(() => { virtuosoTestHandle.atBottomStateChange?.(false); });

    // Reverse-page prepend (t1,t2 above t3,t4). Its envelope carries a bottom
    // edge for the already-loaded items — the prepend must ignore it AND must not
    // re-arm atBottomRef. #232 — triggered via Virtuoso's startReached.
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());

    // The prepend did not re-arm atBottomRef (it's still false), so a live append
    // surfaces the "↓ N new" pill instead of sticking to bottom.
    // Live-tail overlap response: the running window (t1..t4) + a new turn t5.
    mockFetchOnce(detail([
      makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' }),
      makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' }), makeItem({ uuid: 't5' }),
    ], null));
    await act(async () => {
      bumpSnapshot('tick-1');
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    const pill = await screen.findByRole('button', { name: /new/i });
    expect(pill.textContent).toMatch(/1 new/);
  });

  // #217 S3 E2 — P1 regression: a reverse prepend must NOT be miscounted as a
  // live append. The stick-to-bottom layout effect's live discriminator
  // (added>0 && prevHasMoreRef===false && prevLen>0) is TRUE on a tail open's
  // first reverse prepend, and `tail = items.slice(prevLen)` then reads the OLD
  // (back) turns as "new", bumping the "↓ N new" pill by the prior window size.
  // The fix is an early guard keyed on prependPendingRef. We scroll UP (so a real
  // live append WOULD raise the pill), fire a prepend, and assert the pill stays
  // hidden (the prepend contributed 0). Remove the prependPendingRef guard to
  // reproduce RED (the pill would read "↓ 2 new").
  it('P1: a reverse prepend does NOT bump the "↓ N new" pill', async () => {
    installCapturingIO();
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t4"]')).not.toBeNull());

    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    // Scroll UP so a real live append would surface the pill (atBottomRef false).
    // The bottom-state edge (scrolled away from bottom) also ARMS paging — the
    // #232 freeze guard — which is what lets the subsequent startReached page.
    act(() => { virtuosoTestHandle.atBottomStateChange?.(false); });
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);

    // Reverse-page prepend of TWO turns (t1,t2). A miscount would read "2 new".
    // #232 — triggered via Virtuoso's startReached.
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    await act(async () => {
      virtuosoTestHandle.startReached?.();
      for (let i = 0; i < 8; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());

    // The prepend is reverse paging, not new live content → the pill stays hidden.
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
  });

  // Cross-branch review P1 — a JUMP to an early target prepends INSIDE the hook
  // (loadToTarget's backward branch → fetchPrev), so it NEVER sets the
  // reader-owned prependPendingRef the older P1 guard keyed on. On a tail-opened
  // multi-page session (has_more:false) that prepend satisfies the live
  // discriminator (added>0 && prevHasMoreRef===false && prevLen>0), so
  // `tail = items.slice(prevLen)` reads the OLD back-of-window turns (t3,t4) as
  // "new" → the "↓ N new" pill bumps by the prior window size, and if atBottomRef
  // were still true the viewport would scroll-flash to bottom mid-jump. The fix
  // detects a prepend by a TOP-EDGE ADVANCE (the first item's anchor uuid changed),
  // which catches the hook-driven prepend the prependPendingRef-only guard misses.
  // NON-VACUITY: with the first-item-id discriminator reverted (back to the
  // prependPendingRef-only guard) this jump path is NOT recognised as a prepend,
  // the pill reads "↓ 2 new" (the t3,t4 back-of-window turns) → RED.
  it('P1 (cross-branch): a JUMP-driven backward prepend does NOT bump the "↓ N new" pill', async () => {
    // The jump pipeline scrolls the target into view — stub scrollIntoView and
    // capture scrollTo so we can assert the viewport was NOT forced to bottom.
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    const scrollToSpy = spyScrollTo();
    // Full-session outline (t1..t4 in order) so loadToTarget can decide the
    // nearest-edge DIRECTION — without it the hook falls back to a forward drain
    // and never exercises the backward (prepend) branch this P1 lives in.
    const outline = outlineFor([
      oTurn({ uuid: 't1', kind: 'human' }), oTurn({ uuid: 't2', kind: 'human' }),
      oTurn({ uuid: 't3', kind: 'human' }), oTurn({ uuid: 't4', kind: 'human' }),
    ]);
    // Tail open: window holds t3,t4; bottom fully paged (has_more:false) so the
    // live discriminator is eligible; top edge armed (prev_before:3, has_prev).
    mockFetchOnce(edged([makeItem({ uuid: 't3' }), makeItem({ uuid: 't4' })], { prev_before: 3, has_prev: true }));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" outline={outline} />);
    await waitFor(() => expect(container.querySelector('[data-uuid="t3"]')).not.toBeNull());

    const body = container.querySelector('.conv-reader-body') as HTMLElement;
    // User scrolls UP, away from the bottom → atBottomRef becomes false (so a real
    // live append WOULD raise the pill; this isolates the prepend miscount).
    setScroll(body, { scrollTop: 100, clientHeight: 10, scrollHeight: 1000 });
    fireEvent.scroll(body);
    scrollToSpy.mockClear();

    // The before-page returned by loadToTarget's backward fetchPrev: t1,t2 above
    // the window, top edge exhausted (has_prev:false). The envelope carries a
    // bottom edge for the already-loaded items — the prepend must ignore it.
    mockFetchOnce(edged([makeItem({ uuid: 't1' }), makeItem({ uuid: 't2' })], { next_after: 99, has_more: true, prev_before: null, has_prev: false }));
    // Jump to the EARLY target t1 (above the loaded window) → loadToTarget takes
    // the backward branch and prepends INSIDE the hook (no prependPendingRef).
    await act(async () => {
      dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's', jump: { session_id: 's', uuid: 't1' } });
      for (let i = 0; i < 12; i++) await Promise.resolve();
    });
    await waitFor(() => expect(container.querySelector('[data-uuid="t1"]')).not.toBeNull());

    // The prepend is jump-driven reverse paging, not new live content → no pill.
    expect(screen.queryByRole('button', { name: /new/i })).toBeNull();
    // And the stick-to-bottom path must NOT have force-scrolled the body to the
    // bottom (b.scrollTo({ top: scrollHeight }) with no behavior) mid-jump.
    expect(scrollToSpy.mock.calls.some((c) => {
      const arg = c[0] as { top?: number; behavior?: string } | undefined;
      return arg != null && arg.behavior === undefined && arg.top === 1000;
    })).toBe(false);
  });

  // #217 S3 E1 — P2 regression: A→B→A reopen restores the reading position on the
  // RETURN visit. The restore latch must re-arm on a session switch (the reader
  // is mounted persistently — no key={sessionId}). The masking subtlety: B is a
  // NON-restore (tail) open — no saved position — so the restore effect early-
  // returns for B WITHOUT touching the latch. With a value-keyed latch
  // (`latch === sessionId`) the ref stays 'a' across B, so returning to A wrongly
  // skips the restore. The fix re-arms the latch on every genuinely-new open, so
  // A's restore fires again on return even though B never restored.
  it('P2: A→B→A reopen re-fires the reading-position restore on the return visit', async () => {
    // The makeItem/detail helpers hardcode session_id 's', and the jump effect
    // gates on detail.session_id === sessionId, so a per-session detail must
    // carry the matching session_id (and items their matching anchor.session_id).
    const sItem = (sid: string, uuid: string) => makeItem({
      uuid, member_uuids: [uuid], anchor: { session_id: sid, uuid, id: _idFor(uuid) },
    } as never);
    const sDetail = (sid: string, items: ConversationItem[]) => ({
      ...edged(items, { prev_before: 3, has_prev: true }),
      session_id: sid,
    });
    clearReadingPositions();
    recordReadingPos('a', 'a-saved', 1000);
    // B has NO saved reading position → it opens at the tail (a NON-restore open).
    // This is the case that masks/unmasks the bug: B never sets the latch.
    vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {});
    const outlineA = outlineFor([oTurn({ uuid: 'a-saved', kind: 'human' }), oTurn({ uuid: 'a-last', kind: 'human' })]);
    const outlineB = outlineFor([oTurn({ uuid: 'b-1', kind: 'human' }), oTurn({ uuid: 'b-2', kind: 'human' })]);

    // Open A (restore slot 2 → loadToTarget(a-saved) + flash + pin).
    mockFetchOnce(sDetail('a', [sItem('a', 'a-saved'), sItem('a', 'a-last')]));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'a' });
    const { container, rerender } = render(<ConversationReader sessionId="a" outline={outlineA} />);
    await waitFor(() => expect(getState().convPinnedUuid).toBe('a-saved'));

    // Switch to B (genuine switch, NON-restore tail open). No restore, no pin.
    mockFetchOnce(sDetail('b', [sItem('b', 'b-1'), sItem('b', 'b-2')]));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'b' });
    rerender(<ConversationReader sessionId="b" outline={outlineB} />);
    await waitFor(() => expect(container.querySelector('[data-uuid="b-1"]')).not.toBeNull());
    await waitFor(() => expect(getState().convPinnedUuid).toBeNull()); // B did not restore

    // Return to A. With the bug (value-keyed latch never re-armed because B never
    // touched it), the restore is skipped and the pin stays null. Fixed: the
    // latch re-arms on the new open, the restore re-fires, and the pin lands back
    // on 'a-saved'.
    mockFetchOnce(sDetail('a', [sItem('a', 'a-saved'), sItem('a', 'a-last')]));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'a' });
    rerender(<ConversationReader sessionId="a" outline={outlineA} />);
    await waitFor(() => expect(getState().convPinnedUuid).toBe('a-saved'));
    expect(container.querySelector('[data-uuid="a-saved"]')!.classList.contains('conv-item--jumped')).toBe(true);
    clearReadingPositions();
  });
});
