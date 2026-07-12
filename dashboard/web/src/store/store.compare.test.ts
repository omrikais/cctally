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

  // #289 (Codex P2-D) — the new Escape peel adds a reader-deselect step, so the
  // sequence Escape(close compare) → Escape(deselect reader) unmounts the reader
  // (the only consumer of compareCloseFocusPending) with the flag still armed.
  // Left uncleared, the NEXT reader opened would steal focus to #conv-compare-with.
  // SELECT_CONVERSATION's reverse-clear must drop it (deselect-to-null AND
  // select-to-other both make a pending compare-focus moot).
  it('SELECT_CONVERSATION reverse-clear clears a pending compare-close focus', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'CLOSE_COMPARE' });
    expect(getState().compareCloseFocusPending).toBe(true); // armed by CLOSE_COMPARE
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: null });
    expect(getState().compareCloseFocusPending).toBe(false); // must be cleared
  });

  // Belt-and-suspenders: the pre-existing SET_VIEW → dashboard reverse-clear must
  // also drop the flag so the second-Escape-to-dashboard path leaves it clean.
  it('SET_VIEW dashboard reverse-clear also clears a pending compare-close focus', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    dispatch({ type: 'CLOSE_COMPARE' });
    expect(getState().compareCloseFocusPending).toBe(true);
    dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    expect(getState().compareCloseFocusPending).toBe(false);
  });
});

// #227 — the shared session_id → title cache the rail feeds and the comparison
// header reads.
describe('CACHE_CONVERSATION_TITLES', () => {
  it('starts empty and merges non-empty titles, accumulating across dispatches', () => {
    expect(getState().conversationTitles).toEqual({});
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['a', 'First run'], ['b', 'Second run']] });
    expect(getState().conversationTitles).toEqual({ a: 'First run', b: 'Second run' });
    // A later batch merges in new ids without dropping prior ones.
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['c', 'Third run']] });
    expect(getState().conversationTitles).toEqual({ a: 'First run', b: 'Second run', c: 'Third run' });
  });

  it('skips empty session ids and empty titles', () => {
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['', 'x'], ['z', ''], ['ok', 'kept']] });
    expect(getState().conversationTitles).toEqual({ ok: 'kept' });
  });

  it('preserves the object reference on a no-op (same titles re-dispatched)', () => {
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['a', 'First run']] });
    const before = getState().conversationTitles;
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['a', 'First run']] });
    // Identity is preserved so useSyncExternalStore subscribers don't re-render
    // on the rail's per-tick re-dispatch of unchanged rows.
    expect(getState().conversationTitles).toBe(before);
  });

  it('updates a changed title for an existing id', () => {
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['a', 'Old']] });
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: [['a', 'New']] });
    expect(getState().conversationTitles).toEqual({ a: 'New' });
  });
});
