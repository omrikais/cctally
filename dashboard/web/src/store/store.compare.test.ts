import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';
import { clearRailPrefs } from './conversationRailPrefs';

beforeEach(() => { clearRailPrefs(); _resetForTests(); });
afterEach(() => { clearRailPrefs(); _resetForTests(); });

describe('compare slice', () => {
  it('OPEN_COMPARE sets the anchor + view, clears pick', () => {
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    const s = getState();
    expect(s.view).toBe('conversations');
    expect(s.compare).toEqual({ a: 'A', b: 'B' });
    expect(s.selectedConversationId).toBe('A');      // anchor set (cold-boot safe)
    expect(s.comparePick).toBeNull();
  });

  it('OPEN_COMPARE with a===b is a no-op on compare', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'X', b: 'X' });
    expect(getState().compare).toBeNull();
  });

  it('SWAP_COMPARE flips sides', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'SWAP_COMPARE' });
    expect(getState().compare).toEqual({ a: 'B', b: 'A' });
  });

  it('CLOSE_COMPARE clears compare but keeps the anchor selection', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'CLOSE_COMPARE' });
    expect(getState().compare).toBeNull();
    expect(getState().selectedConversationId).toBe('A');
  });

  it('single-session actions clear a lingering compare (reverse-clear)', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'C' });
    expect(getState().compare).toBeNull();

    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'C' });
    expect(getState().compare).toBeNull();

    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    expect(getState().compare).toBeNull();
  });

  it('CANCEL_COMPARE_PICK clears the pick anchor', () => {
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    expect(getState().comparePick).toEqual({ anchor: 'A' });
    dispatch({ type: 'CANCEL_COMPARE_PICK' });
    expect(getState().comparePick).toBeNull();
  });
});
