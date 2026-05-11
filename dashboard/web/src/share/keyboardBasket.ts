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
// Case-sensitivity: uppercase-only, mirroring the M1.16 `S` precedent.
// The keymap dispatcher matches `key` literally; we register only
// 'B' (Shift+B). Lowercase 'b' is NOT bound — leaving it free for
// future per-panel use, and matching the `S`-vs-`s` divergence (where
// lowercase `s` is owned by Settings).
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
    key: 'B',
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

// Returned as an array so main.tsx can spread into `registerKeymap`
// uniformly with the other `build*KeyBindings()` helpers. The list
// currently has a single entry (uppercase B); kept array-shaped so
// future related bindings can land here without churning the caller.
export function buildBasketKeyBindings(): Binding[] {
  return [buildBasketKeyBinding()];
}
