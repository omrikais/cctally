import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ALL_ACCOUNTS } from './accountFocus';
import type { Action, UIState } from './store';
import { installUrlRouting } from './urlRouting';

// Minimal store double: getState returns a mutable snapshot; subscribeStore
// captures the listener so a test can fire it after mutating state.
function makeStore(initial: Partial<UIState>) {
  let s = {
    view: 'dashboard',
    selectedConversationId: null,
    conversationJump: null,
    ...initial,
  } as UIState;
  let listener: () => void = () => {};
  const dispatch = vi.fn<(a: Action) => void>();
  const deps = {
    getState: () => s,
    subscribeStore: (fn: () => void) => {
      listener = fn;
      return () => {};
    },
    dispatch,
  };
  // Test helper: set state then notify the reflect subscriber.
  const set = (patch: Partial<UIState>) => {
    s = { ...s, ...patch } as UIState;
    listener();
  };
  return { deps, dispatch, set };
}

// Seed the URL WITHOUT firing hashchange (jsdom fires hashchange async on
// `location.hash =`, but NOT on replaceState). Per Codex P2.
function seed(hash: string) {
  window.history.replaceState(null, '', hash === '' ? '/' : hash);
}

// #463 S5 (review F5) — the state urlRouting stamps on every entry it writes.
// A popstate carrying it landed on an entry the router wrote (a Back/Forward);
// a popstate with a null state is a FRESH fragment navigation, which Chromium
// also announces with a popstate before its hashchange (probed 2026-08-05).
const ROUTE_STATE = { cctallyRoute: true };

describe('installUrlRouting — read path', () => {
  let dispose: () => void = () => {};

  beforeEach(() => {
    seed('');
    // Spy so a stray store->URL write during boot can't mutate real history;
    // the named handles aren't asserted on in the read-path block.
    vi.spyOn(window.history, 'pushState');
    vi.spyOn(window.history, 'replaceState');
  });
  afterEach(() => {
    dispose();
    vi.restoreAllMocks();
    seed('');
  });

  it('boots a turn route to OPEN_CONVERSATION with a jump', () => {
    seed('#/conversations/A/u1');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenCalledWith({
      type: 'OPEN_CONVERSATION',
      sessionId: 'A',
      jump: { session_id: 'A', uuid: 'u1' },
    });
  });

  it('boots a conversation route (no turn) to OPEN_CONVERSATION without a jump', () => {
    seed('#/conversations/A');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenCalledWith({
      type: 'OPEN_CONVERSATION',
      sessionId: 'A',
      jump: undefined,
    });
  });

  it('F4: boots the singular /conversation/<id> alias to OPEN_CONVERSATION', () => {
    seed('#/conversation/A');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenCalledWith({
      type: 'OPEN_CONVERSATION',
      sessionId: 'A',
      jump: undefined,
    });
  });

  it('F4: boots the singular /conversation/<id>/<turn> alias with a jump', () => {
    seed('#/conversation/A/u1');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenCalledWith({
      type: 'OPEN_CONVERSATION',
      sessionId: 'A',
      jump: { session_id: 'A', uuid: 'u1' },
    });
  });

  it('boots a compare route to OPEN_COMPARE (#217 S7 F10)', () => {
    seed('#/conversations/compare/A/B');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenCalledWith({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
  });

  it('boots a degenerate compare/X/X route to a plain OPEN_CONVERSATION (#217 S7 F10)', () => {
    seed('#/conversations/compare/X/X');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenCalledWith({ type: 'OPEN_CONVERSATION', sessionId: 'X' });
    expect(dispatch).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'OPEN_COMPARE' }),
    );
  });

  it('boots the no-selection route to SET_VIEW conversations + SELECT_CONVERSATION null', () => {
    seed('#/conversations');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(dispatch).toHaveBeenNthCalledWith(1, { type: 'SET_VIEW', view: 'conversations' });
    expect(dispatch).toHaveBeenNthCalledWith(2, { type: 'SELECT_CONVERSATION', sessionId: null });
  });

  it('re-dispatches on hashchange (user Back/Forward)', () => {
    seed('');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    dispatch.mockClear();
    seed('#/conversations/B/u9');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(dispatch).toHaveBeenCalledWith({
      type: 'OPEN_CONVERSATION',
      sessionId: 'B',
      jump: { session_id: 'B', uuid: 'u9' },
    });
  });
});

// #463 S5 (F24d) — the entry batch. The read path previously dispatched only
// OPEN_CONVERSATION, so a Codex permalink opened while the board sat on Claude
// produced a correct reader over a Claude rail with no row marked current.
//
// The batch's ORDER is the correctness argument, and only a fake dispatch can
// observe a sequence rather than a settled end state — so these assertions read
// `dispatch.mock.calls` through the installed router rather than calling the read
// path directly. That keeps the read path unexported (#463 S5 review F12).
describe('installUrlRouting — the entry batch (#463 S5)', () => {
  const codexHash = '#/conversations/source/codex/v1.KEY';
  let dispose: () => void = () => {};

  beforeEach(() => {
    seed('');
    vi.spyOn(window.history, 'pushState');
    vi.spyOn(window.history, 'replaceState');
  });
  afterEach(() => {
    dispose();
    vi.restoreAllMocks();
    seed('');
  });

  // Applied at BOOT: install with the hash already in place.
  function applied(hash: string): Action[] {
    seed(hash);
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    return dispatch.mock.calls.map((c) => c[0]);
  }

  // Applied on a later navigation: install empty, then move the hash. Chromium
  // announces BOTH a Back/Forward and a fresh `location.hash` assignment with a
  // popstate before the hashchange, so `kind` reproduces that pair faithfully and
  // varies only the entry state, which is the real discriminator. `'no-popstate'`
  // stands for a navigation whose popstate this window never saw.
  function appliedOnNavigation(hash: string, kind: 'traversal' | 'fresh' | 'no-popstate'): Action[] {
    seed('');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    dispatch.mockClear();
    seed(hash);
    if (kind !== 'no-popstate') {
      window.dispatchEvent(new PopStateEvent('popstate', {
        state: kind === 'traversal' ? ROUTE_STATE : null,
      }));
    }
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    return dispatch.mock.calls.map((c) => c[0]);
  }

  it('sets the source before opening', () => {
    const seen = applied(codexHash);
    const source = seen.findIndex((a) => a.type === 'SET_ACTIVE_SOURCE');
    const open = seen.findIndex((a) => a.type === 'OPEN_CONVERSATION');
    expect(source).toBeGreaterThanOrEqual(0);
    expect((seen[source] as { source: string }).source).toBe('codex');
    expect(source).toBeLessThan(open);
  });

  it('sets account focus before opening, because SET_ACCOUNT_FOCUS clears the selection', () => {
    const seen = applied('#/conversations/source/codex/v1.KEY?account=acct-1');
    const focus = seen.findIndex((a) => a.type === 'SET_ACCOUNT_FOCUS');
    const open = seen.findIndex((a) => a.type === 'OPEN_CONVERSATION');
    expect((seen[focus] as { account: string }).account).toBe('acct-1');
    expect(focus).toBeLessThan(open);
  });

  it('falls back to ALL_ACCOUNTS when the link names no account', () => {
    const seen = applied(codexHash);
    const focus = seen.find((a) => a.type === 'SET_ACCOUNT_FOCUS') as { account: string } | undefined;
    expect(focus?.account).toBe(ALL_ACCOUNTS);
  });

  it('clears an active conversation search and filters, so the row is not filtered out', () => {
    const seen = applied(codexHash);
    const clear = seen.findIndex((a) => a.type === 'CLEAR_CONVERSATION_FILTERS');
    const search = seen.findIndex((a) => a.type === 'SET_CONVERSATION_SEARCH');
    const open = seen.findIndex((a) => a.type === 'OPEN_CONVERSATION');
    expect(clear).toBeGreaterThanOrEqual(0);
    expect((seen[search] as { text: string }).text).toBe('');
    expect(Math.max(clear, search)).toBeLessThan(open);
  });

  it('never clears the selection, which is what selectSource does and this path must not copy', () => {
    const seen = applied(codexHash);
    const nulled = seen.some((a) => a.type === 'SELECT_CONVERSATION' && (a as { sessionId: unknown }).sessionId === null);
    expect(nulled).toBe(false);
  });

  it('resolves a cross-source comparison from side A', () => {
    const seen = applied('#/conversations/compare/codex/v1.A/claude/SID-B');
    const source = seen.find((a) => a.type === 'SET_ACTIVE_SOURCE') as { source: string } | undefined;
    expect(source?.source).toBe('codex');
  });

  // #463 S5 (review F5) — the reflector's own pushes make `hashchange` reachable
  // by the Back button, so the clearing above would silently discard a search and
  // a filter the user set moments earlier. A traversal keeps them; a fresh
  // navigation still clears them.
  it('a navigation with no observed popstate clears the rail search and filters', () => {
    const seen = appliedOnNavigation(codexHash, 'no-popstate').map((a) => a.type);
    expect(seen).toContain('CLEAR_CONVERSATION_FILTERS');
    expect(seen).toContain('SET_CONVERSATION_SEARCH');
  });

  // The case a popstate-only test would get WRONG: assigning `location.hash`
  // announces itself with a popstate exactly like a Back does, and it is a fresh
  // navigation. Its entry is unstamped, which is what tells them apart.
  it('a fresh fragment navigation clears them even though it fires a popstate', () => {
    const seen = appliedOnNavigation(codexHash, 'fresh').map((a) => a.type);
    expect(seen).toContain('CLEAR_CONVERSATION_FILTERS');
    expect(seen).toContain('SET_CONVERSATION_SEARCH');
  });

  it('a Back/Forward traversal preserves the rail search and filters', () => {
    const seen = appliedOnNavigation(codexHash, 'traversal').map((a) => a.type);
    expect(seen).not.toContain('CLEAR_CONVERSATION_FILTERS');
    expect(seen).not.toContain('SET_CONVERSATION_SEARCH');
  });

  it('a traversal still resolves source and account focus, so the tab is right', () => {
    const seen = appliedOnNavigation('#/conversations/source/codex/v1.KEY?account=acct-1', 'traversal');
    const source = seen.find((a) => a.type === 'SET_ACTIVE_SOURCE') as { source: string } | undefined;
    const focus = seen.find((a) => a.type === 'SET_ACCOUNT_FOCUS') as { account: string } | undefined;
    expect(source?.source).toBe('codex');
    expect(focus?.account).toBe('acct-1');
  });

  it('the traversal flag is consumed, so the navigation AFTER a Back clears again', () => {
    seed('');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    seed(codexHash);
    window.dispatchEvent(new PopStateEvent('popstate', { state: ROUTE_STATE }));
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    dispatch.mockClear();
    seed('#/conversations/source/claude/SID-B');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(dispatch.mock.calls.map((c) => c[0].type)).toContain('CLEAR_CONVERSATION_FILTERS');
  });

  // #463 S5 — the case every test above steps over: the FIRST Back in a tab
  // opened on a pasted permalink. That tab boots on a browser-created entry, and
  // `writeUrl` early-returns when the hash already matches, so nothing ever
  // stamped it. Returning to it therefore read as a fresh navigation and cleared
  // the rail search, while every LATER Back preserved it — the CHANGELOG claims
  // Back preserves it unconditionally. Install now adopts the boot entry.
  it('the first Back after a cold boot on a permalink preserves the rail search', () => {
    seed(codexHash);
    expect(window.history.state, 'the boot entry must start unstamped, as a new tab does').toBeNull();
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    const bootEntry = window.history.state;
    expect(bootEntry, 'install did not adopt the entry it booted on').toEqual(ROUTE_STATE);

    // Move on to a second conversation (a fresh, unstamped entry), then Back.
    // The Back restores the boot entry's state verbatim, which is the only thing
    // the router reads the traversal from.
    seed('#/conversations/source/claude/SID-B');
    window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    dispatch.mockClear();

    seed(codexHash);
    window.dispatchEvent(new PopStateEvent('popstate', { state: bootEntry }));
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    const seen = dispatch.mock.calls.map((c) => c[0].type);
    expect(seen, 'the Back was applied at all').toContain('OPEN_CONVERSATION');
    expect(seen).not.toContain('CLEAR_CONVERSATION_FILTERS');
    expect(seen).not.toContain('SET_CONVERSATION_SEARCH');
  });

  it('adopting the boot entry leaves the URL, including its fragment, untouched', () => {
    // `replaceState(state, '', '')` would resolve the empty url against the
    // document URL and DROP the fragment — silently discarding the permalink
    // being booted. The adoption passes no url at all.
    seed(codexHash);
    const { deps } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(window.location.hash).toBe(codexHash);
  });

  it('the disposer removes the popstate listener', () => {
    seed('');
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    const d = installUrlRouting(deps);
    d();
    dispose = () => {};
    // A traversal popstate observed after disposal must not arm the next
    // install's read.
    window.dispatchEvent(new PopStateEvent('popstate', { state: ROUTE_STATE }));
    dispatch.mockClear();
    seed(codexHash);
    const { deps: deps2, dispatch: dispatch2 } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps2);
    expect(dispatch2.mock.calls.map((c) => c[0].type)).toContain('CLEAR_CONVERSATION_FILTERS');
  });

  it('the router stamps every entry it writes, which is what a traversal is read from', () => {
    // Non-vacuity for the discriminator: if writeUrl stopped stamping, every
    // Back would read as a fresh navigation and the preserve-on-Back tests above
    // would pass by accident from the other side.
    const { deps, set } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    const push = window.history.pushState as unknown as ReturnType<typeof vi.fn>;
    push.mockClear();
    set({ view: 'conversations', selectedConversationId: 'A' });
    expect(push).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations/source/claude/A');
  });
});

describe('installUrlRouting — scroll restoration (#241)', () => {
  // The deep-link jump pipeline lands by writing the reader scroller's scrollTop;
  // the browser's default 'auto' scroll-restoration would write a stale scrollTop
  // on reload that defeats that landing, so install must switch it to 'manual'.
  let dispose: () => void = () => {};
  let prev: ScrollRestoration;

  beforeEach(() => {
    seed('');
    prev = window.history.scrollRestoration;
    // Known prior mode so the restore-on-dispose assertion is deterministic
    // (jsdom leaves scrollRestoration undefined by default).
    window.history.scrollRestoration = 'auto';
    vi.spyOn(window.history, 'pushState');
    vi.spyOn(window.history, 'replaceState');
  });
  afterEach(() => {
    dispose();
    vi.restoreAllMocks();
    window.history.scrollRestoration = prev;
    seed('');
  });

  it('switches history.scrollRestoration to manual on boot', () => {
    const { deps } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    expect(window.history.scrollRestoration).toBe('manual');
  });

  it('restores the prior scroll-restoration mode on dispose', () => {
    const { deps } = makeStore({ view: 'dashboard' });
    const d = installUrlRouting(deps);
    expect(window.history.scrollRestoration).toBe('manual');
    d();
    dispose = () => {};
    expect(window.history.scrollRestoration).toBe('auto');
  });
});

describe('installUrlRouting — reflect path (store -> URL)', () => {
  let push: ReturnType<typeof vi.spyOn>;
  let replace: ReturnType<typeof vi.spyOn>;
  let dispose: () => void = () => {};

  beforeEach(() => {
    seed('');
    push = vi.spyOn(window.history, 'pushState');
    replace = vi.spyOn(window.history, 'replaceState');
  });
  afterEach(() => {
    dispose();
    vi.restoreAllMocks();
    seed('');
  });

  it('pushes #/conversations/<sid> when a conversation is selected', () => {
    const { deps, set } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    push.mockClear();
    set({ view: 'conversations', selectedConversationId: 'A' });
    expect(push).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations/source/claude/A');
  });

  it('pushes #/conversations when mobile-Back clears the selection (Codex P1)', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    push.mockClear();
    set({ selectedConversationId: null });
    expect(push).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations');
  });

  it('pushes the bare path when leaving to the dashboard', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    push.mockClear();
    set({ view: 'dashboard', selectedConversationId: null });
    expect(push).toHaveBeenCalledWith(ROUTE_STATE, '', '/');
  });

  it('pushes the compare hash when a comparison opens (#217 S7 F10)', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    push.mockClear();
    set({ compare: { a: 'A', b: 'B' } as never });
    expect(push).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations/compare/claude/A/claude/B');
  });

  it('does NOT overwrite the compare hash on a sibling tick (compare unchanged) (#217 S7 F10)', () => {
    const { deps, set } = makeStore({
      view: 'conversations',
      selectedConversationId: 'A',
      compare: { a: 'A', b: 'B' } as never,
    });
    dispose = installUrlRouting(deps);
    seed('#/conversations/compare/A/B');
    push.mockClear();
    replace.mockClear();
    set({ conversationJump: { session_id: 'A', uuid: 'u1' } }); // sibling tick, compare unchanged
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it('writes the single-session hash when CLOSE_COMPARE clears compare with sid/view unchanged (#217 S7 F10)', () => {
    // The load-bearing P1 regression: CLOSE_COMPARE sets ONLY compare=null and
    // leaves the anchor sid='A' + view='conversations' intact, so the sid/view
    // branch can't fire — without the explicit clear-write the URL would strand
    // on #/conversations/compare/A/B while the single reader is shown.
    const { deps, set } = makeStore({
      view: 'conversations',
      selectedConversationId: 'A',
      compare: { a: 'A', b: 'B' } as never,
    });
    dispose = installUrlRouting(deps);
    seed('#/conversations/compare/A/B');
    push.mockClear();
    set({ compare: null }); // CLOSE_COMPARE
    expect(push).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations/source/claude/A');
  });

  it('replaces with the turn when a jump lands within the same conversation', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    replace.mockClear();
    set({ conversationJump: { session_id: 'A', uuid: 'u1' } });
    expect(replace).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations/source/claude/A/u1');
  });

  it('replaces u1 -> u2 for a same-session jump before the first clears (Codex P2)', () => {
    const { deps, set } = makeStore({
      view: 'conversations',
      selectedConversationId: 'A',
      conversationJump: { session_id: 'A', uuid: 'u1' },
    });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A/u1');
    replace.mockClear();
    set({ conversationJump: { session_id: 'A', uuid: 'u2' } });
    expect(replace).toHaveBeenCalledWith(ROUTE_STATE, '', '#/conversations/source/claude/A/u2');
  });

  it('does NOT strip the turn when the jump clears (load-bearing)', () => {
    const { deps, set } = makeStore({
      view: 'conversations',
      selectedConversationId: 'A',
      conversationJump: { session_id: 'A', uuid: 'u1' },
    });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A/u1');
    push.mockClear();
    replace.mockClear();
    set({ conversationJump: null }); // CLEAR_CONVERSATION_JUMP
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it('is idempotent — no write when the desired hash already matches', () => {
    const { deps, set } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/source/claude/A');
    push.mockClear();
    set({ view: 'conversations', selectedConversationId: 'A' });
    expect(push).not.toHaveBeenCalled();
  });

  it('disposer removes the hashchange listener', () => {
    const { deps, dispatch } = makeStore({ view: 'dashboard' });
    dispose = installUrlRouting(deps);
    dispose();
    dispatch.mockClear();
    seed('#/conversations/Z');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    expect(dispatch).not.toHaveBeenCalled();
  });
});
