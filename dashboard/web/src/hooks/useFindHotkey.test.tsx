import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useFindHotkey } from './useFindHotkey';
import { _resetForTests, dispatch, getState } from '../store/store';

// Dispatch a capture-phase keydown the way a real Cmd/Ctrl+F would arrive, and
// report whether preventDefault was called (the native-find-suppression signal).
function pressFindKey(opts: { meta?: boolean; ctrl?: boolean; shift?: boolean; alt?: boolean } = {}) {
  const ev = new KeyboardEvent('keydown', {
    key: 'f', metaKey: !!opts.meta, ctrlKey: !!opts.ctrl,
    shiftKey: !!opts.shift, altKey: !!opts.alt, bubbles: true, cancelable: true,
  });
  let prevented = false;
  // jsdom sets defaultPrevented after preventDefault; capture it post-dispatch.
  document.dispatchEvent(ev);
  prevented = ev.defaultPrevented;
  return prevented;
}

beforeEach(() => {
  _resetForTests();
  dispatch({ type: 'SET_VIEW', view: 'conversations' });
});
afterEach(() => vi.restoreAllMocks());

describe('useFindHotkey', () => {
  it('opens the find bar with Cmd+F when a conversation is open', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's1' });
    renderHook(() => useFindHotkey());
    const prevented = pressFindKey({ meta: true });
    expect(prevented).toBe(true);
    expect(getState().convFindOpen).toBe(true);
  });

  it('opens the find bar with Ctrl+F too', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's1' });
    renderHook(() => useFindHotkey());
    const prevented = pressFindKey({ ctrl: true });
    expect(prevented).toBe(true);
    expect(getState().convFindOpen).toBe(true);
  });

  it('focuses the rail search input when NO conversation is open', () => {
    // Provide a rail search input to focus.
    const wrap = document.createElement('div');
    wrap.className = 'conv-rail-search';
    const input = document.createElement('input');
    wrap.appendChild(input);
    document.body.appendChild(wrap);
    renderHook(() => useFindHotkey());
    const prevented = pressFindKey({ meta: true });
    expect(prevented).toBe(true);
    expect(document.activeElement).toBe(input);
    expect(getState().convFindOpen).toBe(false);
    document.body.removeChild(wrap);
  });

  it('does NOT preventDefault when _globalKeyGuard is false (a modal is open) — native find allowed', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's1' });
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });  // openModal !== null → guard false
    renderHook(() => useFindHotkey());
    const prevented = pressFindKey({ meta: true });
    expect(prevented).toBe(false);   // native browser find proceeds
  });

  it('does NOT preventDefault outside the conversations workspace', () => {
    dispatch({ type: 'SET_VIEW', view: 'dashboard' });
    renderHook(() => useFindHotkey());
    const prevented = pressFindKey({ meta: true });
    expect(prevented).toBe(false);
  });

  it('ignores Cmd+Shift+F / Cmd+Alt+F (only the bare meta/ctrl chord)', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's1' });
    renderHook(() => useFindHotkey());
    expect(pressFindKey({ meta: true, shift: true })).toBe(false);
    expect(pressFindKey({ meta: true, alt: true })).toBe(false);
    expect(getState().convFindOpen).toBe(false);
  });
});
