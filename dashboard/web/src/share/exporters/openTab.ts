// #503 S3 §5 — the `Open` export's tab lifecycle, in the order that makes a
// blocked popup DETECTABLE.
//
// The previous shape was `await fetch(...)` and then
// `window.open(url, '_blank', 'noopener,noreferrer')` with the return value
// discarded. Two things were wrong with it. A popup opened after an `await`
// is exactly what browsers block, because the call no longer sits in the
// user-gesture task; and passing `noopener` makes `window.open` return null
// by specification, so the handle could never have been tested even if the
// call had succeeded.
//
// So the tab is RESERVED SYNCHRONOUSLY at the start of the click, with no
// feature string, and its `opener` is cleared by hand — which gives the same
// isolation `noopener` would have, while leaving a handle to test, to
// navigate once the body arrives, and to close when the render fails.
export interface ReservedTab {
  /** Point the reserved tab at `blob`. Throws if the tab refuses. */
  navigate: (blob: Blob) => void;
  /** Abandon the reservation: revoke any unclaimed URL and close the tab. */
  close: () => void;
}

/**
 * Open a blank tab with NO feature string and detach it by hand.
 *
 * Every caller in this directory must go through here. `window.open` returns
 * null whenever `noopener` is present, so a call that passes `noopener` can
 * never tell a blocked popup from a successful one — it reports every success
 * as a block. Clearing `opener` afterwards gives the same isolation while
 * leaving a handle to test, to navigate and to close.
 *
 * Returns `null` only when the browser genuinely blocked the tab.
 */
export function openDetachedTab(): Window | null {
  const win = window.open('', '_blank');
  if (!win) return null;
  try {
    win.opener = null;
  } catch {
    /* already detached, or a stub that disallows the write */
  }
  return win;
}

/** Reserve a tab, or `null` when the browser blocked it. */
export function reserveExportTab(): ReservedTab | null {
  const win = openDetachedTab();
  if (!win) return null;
  return {
    navigate(blob: Blob) {
      if (win.closed) {
        // The user closed the reserved tab while the export fetch was in
        // flight. Assigning `location.href` on a closed window is a silent
        // no-op, so without this check the caller would report "Opened" and
        // record a share-history row for an export nobody can see. Tested
        // before the URL is minted, so there is nothing to revoke.
        throw new Error(TAB_CLOSED_MESSAGE);
      }
      const url = URL.createObjectURL(blob);
      try {
        win.location.href = url;
      } catch (err) {
        // No tab took ownership, so this URL would leak.
        URL.revokeObjectURL(url);
        throw err;
      }
      // The tab owns the URL for its lifetime now; revoking it here would
      // blank the document the user just opened. Browsers reclaim a blob
      // URL when its document unloads.
    },
    close() {
      try {
        win.close();
      } catch {
        /* already closed */
      }
    },
  };
}

/** The message every blocked-popup path states, in one place. */
export const POPUP_BLOCKED_MESSAGE =
  'the browser blocked the new tab — allow pop-ups for this page and retry';

/** The message a reservation that lost its tab mid-export states. */
export const TAB_CLOSED_MESSAGE =
  'the new tab was closed before the export finished';
