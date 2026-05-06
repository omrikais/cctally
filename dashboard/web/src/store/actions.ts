// actions.ts — higher-level keymap actions that need access to the
// latest store snapshot at fire time (can't be captured at registration
// time). Separated from main.tsx so tests can exercise them without
// booting the whole app.

import { dispatch, getState } from './store';

// ----- 4-key: open Session modal for the most-recent session -----
// Default sessions sort is `started desc`, so rows[0] is the newest
// session. If the snapshot hasn't arrived or there are no sessions,
// silently no-op — keyboard shortcuts should not surface errors.
export function openMostRecentSessionModal(): void {
  const rows = getState().snapshot?.sessions?.rows ?? [];
  const firstId = rows[0]?.session_id;
  if (!firstId) return;
  dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: firstId });
}

// ----- 7-key: open Block modal for the active block (or newest if none) -----
// blocks.rows is already newest-first by the Python builder. Pick the
// active row when present; otherwise fall back to rows[0]. Silently
// no-op when the snapshot hasn't arrived or the panel is empty.
export function openActiveOrNewestBlockModal(): void {
  const rows = getState().snapshot?.blocks?.rows ?? [];
  if (rows.length === 0) return;
  const target = rows.find((r) => r.is_active) ?? rows[0];
  dispatch({
    type: 'OPEN_MODAL',
    kind: 'block',
    blockStartAt: target.start_at,
  });
}

// ----- n / N: step through searchMatches -----
// Advances searchIndex forward (1) or backward (-1), wrapping at both
// ends. Reads fresh state each fire so `n`/`N` keep working after
// SET_SEARCH changes the matches.
export function stepMatch(delta: 1 | -1): void {
  const { searchMatches, searchIndex } = getState();
  if (searchMatches.length === 0) return;
  const n = searchMatches.length;
  const next = ((searchIndex < 0 ? 0 : searchIndex) + delta + n) % n;
  dispatch({ type: 'SET_SEARCH_MATCHES', matches: searchMatches, index: next });
}

// ----- q: try window.close(), fall back to a toast -----
// window.close() silently refuses on browser-opened tabs, so we check
// `document.hasFocus()` / `window.closed` after a short delay and
// dispatch SHOW_STATUS_TOAST if the close failed. Legacy parity with
// dashboard/static/quit.js.
export const QUIT_TOAST_MS = 100;
export const QUIT_TOAST_MESSAGE =
  "Can't close this tab — use your browser to close it.";

export function tryQuit(): void {
  try {
    window.close();
  } catch {
    // Most browsers silently refuse here; the toast path is the fallback.
  }
  window.setTimeout(() => {
    if (!window.closed) {
      dispatch({ type: 'SHOW_STATUS_TOAST', text: QUIT_TOAST_MESSAGE });
    }
  }, QUIT_TOAST_MS);
}
