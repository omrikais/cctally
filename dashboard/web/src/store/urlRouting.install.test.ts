import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
    expect(push).toHaveBeenCalledWith(null, '', '#/conversations/A');
  });

  it('pushes #/conversations when mobile-Back clears the selection (Codex P1)', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    push.mockClear();
    set({ selectedConversationId: null });
    expect(push).toHaveBeenCalledWith(null, '', '#/conversations');
  });

  it('pushes the bare path when leaving to the dashboard', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    push.mockClear();
    set({ view: 'dashboard', selectedConversationId: null });
    expect(push).toHaveBeenCalledWith(null, '', '/');
  });

  it('replaces with the turn when a jump lands within the same conversation', () => {
    const { deps, set } = makeStore({ view: 'conversations', selectedConversationId: 'A' });
    dispose = installUrlRouting(deps);
    seed('#/conversations/A');
    replace.mockClear();
    set({ conversationJump: { session_id: 'A', uuid: 'u1' } });
    expect(replace).toHaveBeenCalledWith(null, '', '#/conversations/A/u1');
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
    expect(replace).toHaveBeenCalledWith(null, '', '#/conversations/A/u2');
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
    seed('#/conversations/A');
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
