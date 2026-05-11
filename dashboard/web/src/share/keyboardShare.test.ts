// Task M1.16 — keyboard `S` binding for the share modal (spec §12.1).
//
// The binding is registered in main.tsx alongside the other always-on
// globals. These tests drive it through the same dispatcher: install
// the global keydown handler, register the binding via the helper, then
// fire `keydown { key: 'S' }` on document and assert the resulting
// store state.
//
// Focus resolution is DOM-driven (see keyboardShare.ts): we walk up
// from `document.activeElement` via `closest('[data-panel-kind]')`.
// The `focusPanel` helper below appends a hidden focusable
// `<section data-panel-kind="…">` to document.body and focuses it,
// mirroring the production wiring on every panel (e.g.
// WeeklyPanel.tsx). Tests can also focus child elements inside that
// section to verify the `closest` walk.
//
// Spec §12.1 guards (literal):
//   state.shareModal === null
//   && state.composerModal === null
//   && state.inputMode === 'none'
//   && <focused panel is share-capable>
//
// We resolve two ambiguities from the spec text:
//   1. `inputMode === 'none'` — the store type is `null | 'filter' |
//      'search'` (store.ts:32); the canonical "no input owner" check is
//      `inputMode === null`. The spec text is loose; the store wins.
//   2. We ALSO guard on `state.openModal === null` — spec §6.1 lets the
//      share modal layer above a panel modal, but a free-floating `S`
//      keystroke while a panel modal is open would be ambiguous (which
//      panel's share?). When a panel modal is open the user can already
//      hit the modal-header ShareIcon; the global `S` binding stays
//      inert.
//
// Mobile gate: `window.matchMedia(MOBILE_MEDIA_QUERY).matches` (the same
// query the `useIsMobile` hook reads). Hotkeys are disabled below the
// mobile breakpoint per spec §12.9.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent } from '@testing-library/react';
import {
  _resetForTests as _resetStore,
  dispatch,
  getState,
} from '../store/store';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  registerKeymap,
} from '../store/keymap';
import { buildShareKeyBinding, UNFOCUSED_TOAST_TEXT } from './keyboardShare';
import { openShareModal, openComposer } from '../store/shareSlice';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';

// Track DOM nodes appended by focusPanel() so afterEach can clean them
// up — leftover focused nodes leak across tests and break the
// "unfocused" assertion (activeElement defaults to document.body, but
// only when no other focusable node remains).
const appendedNodes: Element[] = [];

function focusPanel(kind: string): HTMLElement {
  const section = document.createElement('section');
  section.setAttribute('data-panel-kind', kind);
  section.setAttribute('tabindex', '0');
  // Off-screen but focusable; visibility doesn't matter for
  // activeElement.
  section.style.position = 'absolute';
  section.style.left = '-9999px';
  document.body.appendChild(section);
  appendedNodes.push(section);
  section.focus();
  return section;
}

function fireS(): void {
  fireEvent.keyDown(document, { key: 'S' });
}

beforeEach(() => {
  _resetStore();
  _resetKeymap();
  installGlobalKeydown();
  registerKeymap([buildShareKeyBinding()]);
  // Default matchMedia stub: NOT mobile (returns false for the mobile
  // media query). Individual tests can re-stub for mobile coverage.
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false,
    media: q,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
});

afterEach(() => {
  _resetKeymap();
  for (const n of appendedNodes) {
    if (n.parentNode) n.parentNode.removeChild(n);
  }
  appendedNodes.length = 0;
  // Restore activeElement to <body> so the next test starts unfocused.
  if (document.body && typeof (document.body as HTMLElement).focus === 'function') {
    (document.activeElement as HTMLElement | null)?.blur?.();
  }
  vi.restoreAllMocks();
});

describe('S keybinding (share modal)', () => {
  it('opens share modal for the focused panel with triggerId="<panel>-panel"', () => {
    focusPanel('weekly');
    fireS();
    const slot = getState().shareModal;
    expect(slot).not.toBeNull();
    expect(slot?.panel).toBe('weekly');
    expect(slot?.triggerId).toBe('weekly-panel');
  });

  it('opens the share modal for the focused panel regardless of panelOrder (user-reorder safe)', () => {
    // Reorder so `daily` lands at index 0. Under DOM-derived focus,
    // panelOrder is irrelevant — only the focused section's
    // `data-panel-kind` matters. This test guards that invariant.
    dispatch({
      type: 'SAVE_PREFS',
      patch: {
        panelOrder: [
          'daily', 'current-week', 'forecast', 'trend',
          'sessions', 'weekly', 'monthly', 'blocks', 'alerts',
        ],
      },
    });
    focusPanel('daily');
    fireS();
    expect(getState().shareModal?.panel).toBe('daily');
    expect(getState().shareModal?.triggerId).toBe('daily-panel');
  });

  it('resolves the panel via `closest` when focus lands on a nested element', () => {
    // Simulates Tab-traversal landing on a child element inside the
    // panel (e.g. the panel's ShareIcon button). The binding's DOM
    // walk must climb to the parent `[data-panel-kind]` section.
    const section = focusPanel('trend');
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'inner';
    section.appendChild(button);
    button.focus();
    expect(document.activeElement).toBe(button);
    fireS();
    expect(getState().shareModal?.panel).toBe('trend');
    expect(getState().shareModal?.triggerId).toBe('trend-panel');
  });

  it('does nothing when share modal already open', () => {
    focusPanel('current-week');
    // Open via a different panel first; capture state.
    dispatch(openShareModal('trend', 'trend-panel'));
    const before = getState().shareModal;
    fireS();
    // No replace — the existing slot survives.
    expect(getState().shareModal).toBe(before);
    expect(getState().shareModal?.panel).toBe('trend');
  });

  it('does nothing when composer modal is open', () => {
    focusPanel('current-week');
    dispatch(openComposer());
    fireS();
    expect(getState().shareModal).toBeNull();
  });

  it('does nothing when a panel modal is open (openModal !== null)', () => {
    focusPanel('current-week');
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    fireS();
    expect(getState().shareModal).toBeNull();
  });

  it('does nothing when the update modal is open', () => {
    // The update modal uses its own boolean slot (NOT `openModal`), so
    // it needs an explicit guard. Codified by user memory
    // `project_global_hotkeys_modal_guard` — every letter/digit global
    // binding gates against `update.modalOpen`.
    focusPanel('current-week');
    dispatch({ type: 'OPEN_UPDATE_MODAL' });
    fireS();
    expect(getState().shareModal).toBeNull();
  });

  it('does nothing when inputMode is filter', () => {
    focusPanel('current-week');
    dispatch({ type: 'SET_INPUT_MODE', mode: 'filter' });
    fireS();
    expect(getState().shareModal).toBeNull();
  });

  it('does nothing when inputMode is search', () => {
    focusPanel('current-week');
    dispatch({ type: 'SET_INPUT_MODE', mode: 'search' });
    fireS();
    expect(getState().shareModal).toBeNull();
  });

  it('shows the unfocused-toast and does NOT open the modal when no panel ancestor is focused', () => {
    // No focusPanel() call — activeElement defaults to <body>, which
    // has no `[data-panel-kind]` ancestor.
    expect(document.activeElement === document.body || document.activeElement === null).toBe(true);
    fireS();
    expect(getState().shareModal).toBeNull();
    expect(getState().toast).toEqual({
      kind: 'status',
      text: UNFOCUSED_TOAST_TEXT,
    });
    // Sanity: the exact spec-mandated text.
    expect(UNFOCUSED_TOAST_TEXT).toBe(
      'Click a panel first, then press S to share it.',
    );
  });

  it('does nothing when focus is on the alerts panel', () => {
    focusPanel('alerts');
    fireS();
    expect(getState().shareModal).toBeNull();
    // And no toast either — alerts is a quiet "ignored" case, not an
    // unfocused case. The user focused a panel, just not a shareable
    // one.
    expect(getState().toast).toBeNull();
  });

  it('does nothing on mobile (below MOBILE_BREAKPOINT_PX)', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q === MOBILE_MEDIA_QUERY,
      media: q,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }));
    focusPanel('current-week');
    fireS();
    expect(getState().shareModal).toBeNull();
    // No toast either — mobile gate is silent.
    expect(getState().toast).toBeNull();
  });

  it('lowercase `s` does NOT trigger the share modal (Settings owns lowercase)', () => {
    focusPanel('current-week');
    fireEvent.keyDown(document, { key: 's' });
    expect(getState().shareModal).toBeNull();
  });
});
