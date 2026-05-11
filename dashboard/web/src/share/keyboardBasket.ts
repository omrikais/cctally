// Task M3.8 — keyboard `B` opens the composer (spec §12.1).
//
// Same guard surface as the M1.16 `S` binding except the composer is
// global (not panel-scoped), so we don't walk the DOM for a focused
// `[data-panel-kind]`. Guards:
//   1. !mobile (spec §12.9: hotkeys disabled below the mobile breakpoint).
//   2. shareModal slot is empty (don't replace an open share modal).
//   3. composerModal slot is empty (idempotent: re-pressing B while the
//      composer is up is a no-op, not a re-open).
//   4. !state.update.modalOpen — the update modal owns its own boolean
//      slot (NOT `openModal`); every letter/digit global gates on it
//      per the project's global-hotkeys-modal-guard convention.
//   5. inputMode === null — no input owner (filter `f` or search `/`).
//
// Spec §12.1 reads the binding as a single literal `B`, but the
// keymap dispatcher's match is case-sensitive on `e.key`. To stay
// robust against shift-state quirks we register both 'b' and 'B':
// `buildBasketKeyBindings()` returns the pair, and main.tsx spreads it
// into `registerKeymap`. Note: NOT case-sensitive on purpose — the
// composer hotkey is the primary action; spec doesn't reserve
// lowercase for anything else here. (The `S` binding diverges: it is
// uppercase-only because lowercase `s` is reserved for Settings.)
import type { Binding } from '../store/keymap';
import { dispatch, getState } from '../store/store';
import { openComposer } from '../store/shareSlice';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';

function isMobileViewport(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
}

export function buildBasketKeyBinding(): Binding {
  return {
    key: 'b',
    scope: 'global',
    when: () => {
      const s = getState();
      if (isMobileViewport()) return false;
      if (s.shareModal !== null) return false;
      if (s.composerModal !== null) return false;
      if (s.update.modalOpen) return false;
      if (s.inputMode !== null) return false;
      return true;
    },
    action: () => dispatch(openComposer()),
  };
}

// keymap dispatcher matches `key` literally; register the uppercase
// twin so Shift-B works the same as plain `b`. Both share the same
// guard predicate and action.
export function buildBasketKeyBindings(): Binding[] {
  const base = buildBasketKeyBinding();
  return [base, { ...base, key: 'B' }];
}
