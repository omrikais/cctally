import { afterEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';

afterEach(() => _resetForTests());

describe('conversation view state', () => {
  it('defaults to dashboard view with no selection/search/jump', () => {
    const s = getState();
    expect(s.view).toBe('dashboard');
    expect(s.selectedConversationId).toBeNull();
    expect(s.conversationSearch).toBe('');
    expect(s.conversationJump).toBeNull();
  });

  it('SET_VIEW switches the view', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    expect(getState().view).toBe('conversations');
    dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    expect(getState().view).toBe('dashboard');
  });

  it('SET_VIEW dismisses any open panel/share/composer modal (#158)', () => {
    // A panel modal open on the dashboard, with the layered share + composer
    // slots stacked on top — the exact state that would otherwise render a
    // dashboard modal over the conversations body after the view switch.
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: 's1' });
    dispatch({ type: 'OPEN_SHARE', panel: 'sessions', triggerId: 'btn-share' });
    dispatch({ type: 'OPEN_COMPOSER' });
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('s1');
    expect(getState().shareModal).not.toBeNull();
    expect(getState().composerModal).not.toBeNull();

    dispatch({ type: 'SET_VIEW', view: 'conversations' });

    const s = getState();
    expect(s.view).toBe('conversations');
    expect(s.openModal).toBeNull();
    expect(s.openSessionId).toBeNull();
    expect(s.shareModal).toBeNull();
    expect(s.composerModal).toBeNull();
  });

  it('SET_VIEW back to dashboard also clears a stray modal', () => {
    // Symmetric: the reducer dismisses transient modals on every view switch,
    // not just dashboard -> conversations.
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    dispatch({ type: 'OPEN_MODAL', kind: 'forecast' });
    dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    expect(getState().openModal).toBeNull();
  });

  it('OPEN_CONVERSATION enters the view, selects, and stores the jump', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc', jump: { session_id: 'abc', uuid: 'u1' } });
    const s = getState();
    expect(s.view).toBe('conversations');
    expect(s.selectedConversationId).toBe('abc');
    expect(s.conversationJump).toEqual({ session_id: 'abc', uuid: 'u1' });
  });

  it('OPEN_CONVERSATION also dismisses any open panel/share/composer modal (#158)', () => {
    // OPEN_CONVERSATION is the second workspace-entry path (it sets
    // view='conversations' directly, bypassing SET_VIEW). It must enforce the
    // same "switching the workspace dismisses transient modals" invariant so a
    // future in-modal "open conversation" link can't strand a dashboard modal.
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: 's1' });
    dispatch({ type: 'OPEN_SHARE', panel: 'sessions', triggerId: 'btn-share' });
    dispatch({ type: 'OPEN_COMPOSER' });

    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'conv-1', jump: { session_id: 'conv-1', uuid: 'u1' } });

    const s = getState();
    // The conversation selection it sets survives...
    expect(s.view).toBe('conversations');
    expect(s.selectedConversationId).toBe('conv-1');
    expect(s.conversationJump).toEqual({ session_id: 'conv-1', uuid: 'u1' });
    // ...while every transient modal slot is cleared.
    expect(s.openModal).toBeNull();
    expect(s.openSessionId).toBeNull();
    expect(s.shareModal).toBeNull();
    expect(s.composerModal).toBeNull();
  });

  it('OPEN_CONVERSATION without jump clears any prior jump', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'a', jump: { session_id: 'a', uuid: 'x' } });
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'b' });
    expect(getState().conversationJump).toBeNull();
    expect(getState().selectedConversationId).toBe('b');
  });

  it('SELECT_CONVERSATION sets selection without a jump and without leaving the view', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 's9' });
    expect(getState().view).toBe('conversations');
    expect(getState().selectedConversationId).toBe('s9');
    expect(getState().conversationJump).toBeNull();
  });

  it('SELECT_CONVERSATION with null clears the selection (mobile back)', () => {
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 's9' });
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: null });
    expect(getState().selectedConversationId).toBeNull();
  });

  it('SET_CONVERSATION_SEARCH updates the needle', () => {
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'flock' });
    expect(getState().conversationSearch).toBe('flock');
  });

  // #177 S6 — kind facet for the rail chips.
  it('conversationSearchKind defaults to all', () => {
    expect(getState().conversationSearchKind).toBe('all');
  });

  it('SET_CONVERSATION_SEARCH_KIND updates the facet', () => {
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'tools' });
    expect(getState().conversationSearchKind).toBe('tools');
  });

  it('clearing the search needle resets the kind to all', () => {
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'npm' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'thinking' });
    expect(getState().conversationSearchKind).toBe('thinking');
    // An empty-text SET_CONVERSATION_SEARCH (the clear path) snaps kind back.
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: '' });
    expect(getState().conversationSearch).toBe('');
    expect(getState().conversationSearchKind).toBe('all');
  });

  it('a non-empty SET_CONVERSATION_SEARCH leaves the kind untouched', () => {
    dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: 'assistant' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'rerun' });
    expect(getState().conversationSearchKind).toBe('assistant');
  });

  it('CLEAR_CONVERSATION_JUMP clears only the jump', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'a', jump: { session_id: 'a', uuid: 'x' } });
    dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
    expect(getState().conversationJump).toBeNull();
    expect(getState().selectedConversationId).toBe('a');
  });

  // #177 S6 — in-conversation find bar open flag.
  it('convFindOpen defaults to false', () => {
    expect(getState().convFindOpen).toBe(false);
  });

  it('OPEN_CONV_FIND / CLOSE_CONV_FIND toggle the flag', () => {
    dispatch({ type: 'OPEN_CONV_FIND' });
    expect(getState().convFindOpen).toBe(true);
    dispatch({ type: 'CLOSE_CONV_FIND' });
    expect(getState().convFindOpen).toBe(false);
  });

  it('a genuine session switch via OPEN_CONVERSATION closes an open find bar', () => {
    _resetForTests();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    dispatch({ type: 'OPEN_CONV_FIND' });
    expect(getState().convFindOpen).toBe(true);
    // Switching to a DIFFERENT session closes find (its anchors are stale).
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'def' });
    expect(getState().convFindOpen).toBe(false);
  });

  it('a same-session OPEN_CONVERSATION (in-session jump) leaves find open', () => {
    _resetForTests();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    dispatch({ type: 'OPEN_CONV_FIND' });
    // An in-session find step dispatches a same-session OPEN_CONVERSATION with a
    // jump — find must stay open so the next/prev cursor survives.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc', jump: { session_id: 'abc', uuid: 'u9' } });
    expect(getState().convFindOpen).toBe(true);
  });

  it('view state does not persist across loadInitial', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    _resetForTests();
    expect(getState().view).toBe('dashboard');
  });
});

// #177 S5 — outline panel store plumbing.
describe('conversation outline / focus-mode state', () => {
  it('convOutlineOpen defaults to true with no localStorage pref', () => {
    localStorage.removeItem('cctally.conv.outlineOpen');
    _resetForTests();
    expect(getState().convOutlineOpen).toBe(true);
  });

  it('convOutlineOpen reads the persisted localStorage pref on init', () => {
    localStorage.setItem('cctally.conv.outlineOpen', 'false');
    _resetForTests();
    expect(getState().convOutlineOpen).toBe(false);
    localStorage.setItem('cctally.conv.outlineOpen', 'true');
    _resetForTests();
    expect(getState().convOutlineOpen).toBe(true);
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('TOGGLE_CONV_OUTLINE flips the flag and persists it', () => {
    localStorage.removeItem('cctally.conv.outlineOpen');
    _resetForTests();
    expect(getState().convOutlineOpen).toBe(true);
    dispatch({ type: 'TOGGLE_CONV_OUTLINE' });
    expect(getState().convOutlineOpen).toBe(false);
    expect(localStorage.getItem('cctally.conv.outlineOpen')).toBe('false');
    dispatch({ type: 'TOGGLE_CONV_OUTLINE' });
    expect(getState().convOutlineOpen).toBe(true);
    expect(localStorage.getItem('cctally.conv.outlineOpen')).toBe('true');
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('convFocusMode defaults to "all" and SET_CONV_FOCUS_MODE sets it', () => {
    _resetForTests();
    expect(getState().convFocusMode).toBe('all');
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'errors' });
    expect(getState().convFocusMode).toBe('errors');
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' });
    expect(getState().convFocusMode).toBe('prompts');
  });

  it('convCurrentTurnUuid defaults to null and SET_CONV_CURRENT_TURN sets it', () => {
    _resetForTests();
    expect(getState().convCurrentTurnUuid).toBeNull();
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'u7' });
    expect(getState().convCurrentTurnUuid).toBe('u7');
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: null });
    expect(getState().convCurrentTurnUuid).toBeNull();
  });

  it('OPEN_CONVERSATION resets focus mode + current turn but NOT outlineOpen', () => {
    localStorage.setItem('cctally.conv.outlineOpen', 'false');
    _resetForTests();
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'errors' });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'u3' });
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    const s = getState();
    expect(s.convFocusMode).toBe('all');
    expect(s.convCurrentTurnUuid).toBeNull();
    expect(s.convOutlineOpen).toBe(false); // persisted pref untouched
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('SELECT_CONVERSATION resets focus mode + current turn but NOT outlineOpen', () => {
    _resetForTests();
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'chat' });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'u9' });
    dispatch({ type: 'TOGGLE_CONV_OUTLINE' }); // → false
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 's5' });
    const s = getState();
    expect(s.convFocusMode).toBe('all');
    expect(s.convCurrentTurnUuid).toBeNull();
    expect(s.convOutlineOpen).toBe(false); // NOT reset by the switch
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  // #177 S5 §5 — the reset-to-All belongs to the jump callers (each runs the
  // precise hidden-target check). A SAME-session OPEN_CONVERSATION (an
  // in-session jump) MUST preserve the focus mode + scroll cursor; only a
  // genuine session switch resets them.
  it('same-session OPEN_CONVERSATION preserves convFocusMode + convCurrentTurnUuid', () => {
    _resetForTests();
    // Land on a session first (genuine switch from null → 'abc').
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'errors' });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'u3' });
    // Same-session jump (e.g. `e` to the next visible error) — must NOT reset.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc', jump: { session_id: 'abc', uuid: 'u9' } });
    const s = getState();
    expect(s.convFocusMode).toBe('errors');
    expect(s.convCurrentTurnUuid).toBe('u3');
    expect(s.conversationJump).toEqual({ session_id: 'abc', uuid: 'u9' });
  });

  it('cross-session OPEN_CONVERSATION resets convFocusMode + convCurrentTurnUuid', () => {
    _resetForTests();
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'prompts' });
    dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid: 'u3' });
    // Switching to a DIFFERENT session resets the transient outline state.
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'def' });
    const s = getState();
    expect(s.convFocusMode).toBe('all');
    expect(s.convCurrentTurnUuid).toBeNull();
  });
});
