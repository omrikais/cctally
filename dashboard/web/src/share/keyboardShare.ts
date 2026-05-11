// Task M1.16 — keyboard `S` binding for the share modal (spec §12.1).
//
// Factored into its own module so main.tsx doesn't import the entire
// share/ subtree at boot (the share modal still lazy-mounts via
// ShareModalRoot; this binding is a pure callback that only touches the
// store + dispatch). The binding is registered as `scope: 'global'`
// alongside the existing digit/letter globals.
//
// Focus resolution is DOM-driven: we walk up from
// `document.activeElement` via `closest('[data-panel-kind]')` to find
// the focused panel. Every panel `<section>` carries
// `data-panel-kind="<panel-id>"` (see e.g. WeeklyPanel.tsx, the
// AlertsPanel, etc.). This sidesteps the store's `focusIndex` slice
// (which is dead in production — nothing dispatches `SET_FOCUS` outside
// tests) and is more honest besides: "the focused panel" is literally
// where the keyboard cursor sits, and `closest` correctly resolves the
// parent panel even when Tab-traversal lands the user on a child
// element (e.g. the panel's ShareIcon button).
//
// Guards (spec §12.1, with two resolved ambiguities — see test file):
//   1. shareModal slot is empty (don't replace an already-open modal).
//   2. composerModal slot is empty.
//   3. openModal === null — no panel modal is up (mid-task decision,
//      documented in test file: free-floating `S` while a panel modal
//      is open is ambiguous; user can click the modal's ShareIcon).
//   4. !state.update.modalOpen — the update modal uses its own boolean
//      slot (NOT `openModal`); per the project's
//      global-hotkeys-modal-guard convention every letter/digit global
//      gates on this. Without it `S` would layer the share modal
//      above an open update modal.
//   5. inputMode === null (the canonical "no input owner" check;
//      spec's 'none' string maps to TypeScript `null`).
//   6. !mobile — spec §12.9 disables hotkeys below the mobile
//      breakpoint. Same media query as `useIsMobile`.
//
// In the action (NOT the `when:` guard, so the keystroke still
// surfaces a help toast on unfocused presses):
//   - If no ancestor `[data-panel-kind]` exists, fire the unfocused
//     toast pointing the user at click-to-focus.
//   - If the resolved kind is not in SHARE_CAPABLE_PANELS (e.g.
//     'alerts'), silently ignore.
//
// The triggerId is derived as `<panel>-panel` so that closing the
// modal via <ShareModalRoot> restores focus to the panel's ShareIcon
// (matches the id wired up by M1.10).
import type { Binding } from '../store/keymap';
import { dispatch, getState } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { SHARE_CAPABLE_PANELS, type PanelId } from '../lib/panelIds';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';
import type { SharePanelId } from './types';

export const UNFOCUSED_TOAST_TEXT =
  'Click a panel first, then press S to share it.';

function isMobileViewport(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
}

function resolveFocusedPanelKind(): string | null {
  if (typeof document === 'undefined') return null;
  const active = document.activeElement;
  if (!active || !(active instanceof Element)) return null;
  const section = active.closest('[data-panel-kind]');
  if (!section) return null;
  return section.getAttribute('data-panel-kind');
}

export function buildShareKeyBinding(): Binding {
  return {
    key: 'S',
    scope: 'global',
    when: () => {
      const s = getState();
      if (isMobileViewport()) return false;
      if (s.shareModal !== null) return false;
      if (s.composerModal !== null) return false;
      if (s.openModal !== null) return false;
      if (s.update.modalOpen) return false;
      if (s.inputMode !== null) return false;
      return true;
    },
    action: () => {
      const kind = resolveFocusedPanelKind();
      // Unfocused: no panel ancestor of activeElement. Surface the
      // spec-mandated toast and bail. We check this in the action (not
      // in `when:`) so the keystroke still fires the help message —
      // moving it to `when:` would silently swallow it.
      if (kind === null) {
        dispatch({ type: 'SHOW_STATUS_TOAST', text: UNFOCUSED_TOAST_TEXT });
        return;
      }
      // Not a share-capable kind (e.g. 'alerts'): silent ignore. The
      // user clicked a panel, just not a shareable one — no toast.
      if (!SHARE_CAPABLE_PANELS.has(kind as PanelId)) return;
      // The membership check above narrows `kind` to the share-capable
      // subset, which is exactly the SharePanelId literal union.
      const sharePanel = kind as SharePanelId;
      dispatch(openShareModal(sharePanel, `${sharePanel}-panel`));
    },
  };
}
