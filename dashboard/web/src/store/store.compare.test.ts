import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';
import { clearRailPrefs } from './conversationRailPrefs';

beforeEach(() => { clearRailPrefs(); _resetForTests(); });
afterEach(() => { clearRailPrefs(); _resetForTests(); });

describe('compare slice', () => {
  it('treats same opaque key under different sources as different conversations', () => {
    const claude = { source: 'claude', key: 'same' } as const;
    const codex = { source: 'codex', key: 'same' } as const;
    dispatch({ type: 'OPEN_COMPARE', aRef: claude, bRef: codex } as never);
    expect((getState() as unknown as { selectedConversationRef?: unknown }).selectedConversationRef).toEqual(claude);
    expect(getState().compare).toEqual({ a: claude, b: codex });
  });

  it('resets source-scoped pin and jump state when the opaque key is unchanged', () => {
    const claude = { source: 'claude', key: 'same' } as const;
    const codex = { source: 'codex', key: 'same' } as const;
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef: claude,
      jump: { conversation_ref: claude, session_id: 'same', uuid: 'claude-turn' },
    });
    dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: 'claude-turn' });

    dispatch({ type: 'OPEN_CONVERSATION', conversationRef: codex });

    expect(getState().selectedConversationRef).toEqual(codex);
    expect(getState().conversationJump).toBeNull();
    expect(getState().convPinnedUuid).toBeNull();
  });

  it('rejects comparison of the same qualified reference', () => {
    const rootA = { source: 'codex', key: 'v1.root-a-same' } as const;
    dispatch({ type: 'OPEN_COMPARE', aRef: rootA, bRef: { ...rootA } } as never);
    expect(getState().compare).toBeNull();
  });

  it('OPEN_COMPARE sets the anchor + view, clears pick', () => {
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    dispatch({ type: 'OPEN_COMPARE', a: 'A', b: 'B' });
    const s = getState();
    expect(s.view).toBe('conversations');
    expect(s.compare).toEqual({
      a: { source: 'claude', key: 'A' },
      b: { source: 'claude', key: 'B' },
    });
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
    expect(getState().compare).toEqual({
      a: { source: 'claude', key: 'B' },
      b: { source: 'claude', key: 'A' },
    });
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
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'A' });   // NEW: satisfy the F2 precondition
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    expect(getState().comparePick).toEqual({ anchor: { source: 'claude', key: 'A' } });
    dispatch({ type: 'CANCEL_COMPARE_PICK' });
    expect(getState().comparePick).toBeNull();
  });

  // #304 S2 (Codex F2) — the reducer enforces the caller invariant
  // `comparePick ⇒ selectedConversationId === anchor`: both real entries
  // dispatch from the open reader, and the compact view-layer gate relies on
  // the anchor selection surviving pick-mode.
  it('START_COMPARE_PICK is a no-op when the anchor is not the current selection', () => {
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });        // nothing selected
    expect(getState().comparePick).toBeNull();
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'B' });
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });        // wrong anchor
    expect(getState().comparePick).toBeNull();
  });

  // #304 S2 (Codex F7) — entering pick closes the ephemeral outline sheet so a
  // restored sheet can't bury the reader / obscure the focus target on cancel.
  it('START_COMPARE_PICK closes the mobile outline sheet', () => {
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'A' });
    dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' });
    expect(getState().convOutlineMobileOpen).toBe(true);
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    expect(getState().convOutlineMobileOpen).toBe(false);
  });

  // #304 S2 — cancel returns to the anchor reader and requests the same focus
  // return CLOSE_COMPARE does (the flag generalizes to "compare-flow focus
  // return pending").
  it('CANCEL_COMPARE_PICK arms the compare focus-return flag', () => {
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'A' });
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    dispatch({ type: 'CANCEL_COMPARE_PICK' });
    expect(getState().compareCloseFocusPending).toBe(true);
  });

  // #304 S2 (Codex F1 belt-and-suspenders) — a banner-Cancel click with the
  // filters popover open must not strand convFiltersOpen through the compact
  // rail unmount (inView would deaden the view keymap).
  it('CANCEL_COMPARE_PICK clears convFiltersOpen', () => {
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'A' });
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true });
    dispatch({ type: 'CANCEL_COMPARE_PICK' });
    expect(getState().convFiltersOpen).toBe(false);
  });

  // #304 S2 (Codex F8) — OPEN_CONVERSATION reverse-clears comparison state but
  // previously left the focus flag; now that cancel arms it more often, a
  // direct open (URL boot / search-hit nav) before consumption must clear it.
  it('OPEN_CONVERSATION clears a pending compare focus return', () => {
    dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'A' });
    dispatch({ type: 'START_COMPARE_PICK', anchor: 'A' });
    dispatch({ type: 'CANCEL_COMPARE_PICK' });
    expect(getState().compareCloseFocusPending).toBe(true);
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'C' });
    expect(getState().compareCloseFocusPending).toBe(false);
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
  it('does not collide same-key Claude and Codex titles', () => {
    const claude = { source: 'claude', key: 'same' } as const;
    const codex = { source: 'codex', key: 'same' } as const;
    dispatch({
      type: 'CACHE_CONVERSATION_TITLES',
      titles: [[claude, 'Claude title'], [codex, 'Codex title']],
    } as never);
    expect(getState().conversationTitles).toEqual({
      '["claude","same"]': 'Claude title',
      '["codex","same"]': 'Codex title',
    });
  });

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
