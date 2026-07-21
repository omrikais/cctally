import { useEffect } from 'react';
import { dispatch, getState } from '../store/store';
import { _globalKeyGuard } from '../store/globalBindings';

// #217 S4 / I-1.5 — the Cmd/Ctrl+F intercept for the Conversations workspace.
// The central keymap dispatcher bails on `metaKey || ctrlKey` before any
// binding (store/keymap.ts), so this needs a SEPARATE capture-phase keydown
// listener to beat the browser's native find bar to `preventDefault`.
//
// Because it bypasses the central keymap precedence it carries the FULLER guard
// (Codex P1): reuse `_globalKeyGuard()` — which covers openModal, the
// update/doctor modals, chrome overlays, AND input mode — NOT the narrower
// `/`-open predicate (which only checks openModal/inputMode/convFiltersOpen).
// It is ALSO scoped to the conversations workspace (`view === 'conversations'`)
// so the native browser find still works on the dashboard panels — we don't
// hijack it globally.
//
//  - conversation open → preventDefault + OPEN_CONV_FIND (the find bar mounts /
//    auto-focuses).
//  - no conversation open → preventDefault + focus the rail search input
//    (mirrors the `/` behavior).
//  - guard false OR off-workspace → no preventDefault (native find proceeds).
//
// Mounted by ConversationsView (so it exists only while that view is active).
export function useFindHotkey(): void {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // The bare meta/ctrl + F chord only (Shift/Alt variants are the browser's
      // own / other shortcuts — leave them alone).
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
      if (e.key.toLowerCase() !== 'f') return;
      const s = getState();
      // Conversations-workspace scope + the fuller modal/overlay/input guard.
      if (s.view !== 'conversations') return;
      if (!_globalKeyGuard()) return;
      e.preventDefault();
      if (s.selectedConversationRef) {
        dispatch({ type: 'OPEN_CONV_FIND' });
        // The find bar auto-focuses its input on mount; if it is ALREADY open,
        // re-focus the existing input so a repeat press lands the caret there.
        requestAnimationFrame(() => {
          document.querySelector<HTMLInputElement>('.conv-findbar-input')?.focus();
        });
      } else {
        const el = document.querySelector<HTMLInputElement>('.conv-rail-search input');
        el?.focus();
        el?.select();
      }
    };
    // Capture phase so we beat both the document-level keymap dispatcher AND the
    // browser's native find shortcut.
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, []);
}
