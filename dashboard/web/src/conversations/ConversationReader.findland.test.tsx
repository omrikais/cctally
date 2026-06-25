import { act, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationReader } from './ConversationReader';
import { _resetForTests, dispatch, getState } from '../store/store';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import { clearReadingPositions } from '../store/readingPosition';
import type { ConversationItem } from '../types/conversation';

// #236 — the find/jump member-turn CENTER branch lands on the turn's first
// LANDABLE <mark> (via firstLandableMark) when find is open, and on the turn
// root when find is closed. JSDOM has no layout, so the pixel-exact landing is
// the Playwright ui-qa gate's job; this test pins the CALL TARGET of the
// `scrollNodeIntoView` center calls (which element the reader hands the scroller
// to center) by mocking both `./scrollNodeIntoView` (to capture it) and
// `./findMark` (so `firstLandableMark` returns a deterministic sentinel,
// independent of layout).

const scrollCalls: HTMLElement[] = [];
vi.mock('./scrollNodeIntoView', () => ({
  scrollNodeIntoView: (_scroller: HTMLElement, el: HTMLElement) => { scrollCalls.push(el); },
  computeAlignScrollTop: () => 0,
}));

const sentinelMark = document.createElement('mark');
vi.mock('./findMark', async (orig) => ({
  ...(await orig<typeof import('./findMark')>()),
  firstLandableMark: vi.fn(() => sentinelMark),
}));

// Render-all react-virtuoso mock (mirrors ConversationReader.test.tsx): mounts
// EVERY item so the jump target is always present (a warm jump → the reader's
// `alreadyMounted()` is true → result === 'mounted' → the member-turn center
// branch runs with the target element in hand, no walk).
const virtuosoTestHandle = { scrollToIndex: vi.fn() };
vi.mock('react-virtuoso', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  const Virtuoso = React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
    React.useImperativeHandle(ref, () => ({
      scrollToIndex: virtuosoTestHandle.scrollToIndex, scrollBy: vi.fn(), scrollTo: vi.fn(),
    }), []);
    const data = (props.data as unknown[]) ?? [];
    const itemContent = props.itemContent as (index: number, datum: unknown) => React.ReactNode;
    const computeItemKey = props.computeItemKey as ((index: number, datum: unknown) => React.Key) | undefined;
    const components = (props.components as { List?: unknown; Item?: unknown }) ?? {};
    const firstItemIndex = (props.firstItemIndex as number) ?? 0;
    const scrollerRef = props.scrollerRef as ((el: unknown) => void) | undefined;
    const List = (components.List ?? 'div') as React.ElementType;
    const Item = (components.Item ?? 'div') as React.ElementType;
    const scroller = React.useRef<HTMLDivElement>(null);
    React.useEffect(() => {
      scrollerRef?.(scroller.current);
      const rendered = props.itemsRendered as ((items: unknown[]) => void) | undefined;
      rendered?.(data.map((d, i) => ({ index: firstItemIndex + i, data: d })));
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [data.length]);
    return React.createElement(
      'div',
      { ref: scroller, className: props.className as string, role: props.role as string, onScroll: props.onScroll as React.UIEventHandler },
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

function detail(items: ConversationItem[]) {
  return {
    session_id: 's',
    project_label: 'proj',
    git_branch: 'main',
    started_utc: '2026-01-01T00:00:00Z',
    last_activity_utc: '2026-01-01T02:00:00Z',
    cost_usd: 1,
    models: ['claude-opus-4'],
    items,
    page: { next_after: null, has_more: false },
  };
}

function mockFetchOnce(body: unknown) {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    ok: true, status: 200, json: async () => body,
  } as Response);
}

beforeEach(() => {
  _resetForTests();
  globalThis.fetch = vi.fn();
  installIntersectionObserverStub();
  clearReadingPositions();
  _idByUuid.clear();
  _nextItemId = 1;
  scrollCalls.length = 0;
  if (typeof Element.prototype.scrollTo !== 'function') {
    Element.prototype.scrollTo = () => {};
  }
});
afterEach(() => {
  _resetForTests();
  clearReadingPositions();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// A plain member turn (NOT a card root) so the jump resolves to the center
// branch. 'm1' is the head; 'targetMember' is a later plain human turn.
const items = () => [
  makeItem('m1', { text: 'kick off the work' }),
  makeItem('targetMember', { text: 'the matched member turn' }),
  makeItem('m3', { text: 'trailing turn' }),
];

describe('#236 find-jump lands on the matched word', () => {
  it('centers the first landable <mark> when find is open', async () => {
    mockFetchOnce(detail(items()));
    // Open the conversation, THEN open find on the SAME session (a new
    // OPEN_CONVERSATION for the same id preserves convFindOpen), then jump.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's' });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="m1"]')).not.toBeNull());

    act(() => { dispatch({ type: 'OPEN_CONV_FIND' }); });
    await waitFor(() => expect(getState().convFindOpen).toBe(true));

    act(() => {
      dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's',
        jump: { session_id: 's', uuid: 'targetMember' } });
    });

    await waitFor(() => expect(scrollCalls.length).toBeGreaterThan(0));
    await waitFor(() => expect(getState().conversationJump).toBeNull());

    // Every center call targeted the sentinel mark, not the turn root.
    expect(scrollCalls.every((el) => el === sentinelMark)).toBe(true);
  });

  it('centers the turn root when find is CLOSED (firstLandableMark not consulted)', async () => {
    mockFetchOnce(detail(items()));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's',
      jump: { session_id: 's', uuid: 'targetMember' } });
    const { container } = render(<ConversationReader sessionId="s" />);
    await waitFor(() => expect(container.querySelector('[data-uuid="m1"]')).not.toBeNull());

    await waitFor(() => expect(scrollCalls.length).toBeGreaterThan(0));
    await waitFor(() => expect(getState().conversationJump).toBeNull());

    // The sentinel mark is NEVER a center target while find is closed; the turn
    // root (data-uuid="targetMember") is.
    expect(scrollCalls.some((el) => el === sentinelMark)).toBe(false);
    const turn = container.querySelector('[data-uuid="targetMember"]') as HTMLElement;
    expect(scrollCalls).toContain(turn);
  });
});
