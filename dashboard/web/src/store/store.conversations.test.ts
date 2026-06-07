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

  it('CLEAR_CONVERSATION_JUMP clears only the jump', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'a', jump: { session_id: 'a', uuid: 'x' } });
    dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
    expect(getState().conversationJump).toBeNull();
    expect(getState().selectedConversationId).toBe('a');
  });

  it('view state does not persist across loadInitial', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    _resetForTests();
    expect(getState().view).toBe('dashboard');
  });
});
