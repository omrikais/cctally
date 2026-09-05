// #750 S2 review — where focus goes after an alert toast is dismissed.
//
// Dismissing removes the element that had focus, which sends
// `document.activeElement` to <body>: the reader's next Tab restarts at the top
// of the document instead of resuming beside whatever they were reading. The
// toast never takes focus by itself, so the element to restore is the one the
// person came from, and `focusin`'s `relatedTarget` is that element. It is
// captured on the way in because by dismiss time it is no longer recoverable
// from anywhere.
//
// The value lives in module scope because at most one toast is live at a time,
// it is written on every focus, and a `useRef` is unavailable at the second
// call site, which is inside conditional JSX. It lives in THIS module rather
// than in `Toast.tsx` so that `store._resetForTests` can clear it without
// importing the component: a module global no test reset can reach is
// cross-test state that leaks silently, which is what the review found.
let focusOrigin: HTMLElement | null = null;

/** Remember where focus came from — unless it came from inside the toast.
 *
 *  Tab order inside a threshold toast is root, then follow button, so
 *  Shift-Tab from the button back to the root arrives with `relatedTarget`
 *  INSIDE the toast. Recording that element would restore focus onto a node
 *  the same dismissal is about to remove, and React has not flushed the
 *  unmount yet, so `isConnected` is still true and the guard in
 *  `restoreFocusAfterDismiss` cannot see it. Focus would land on the follow
 *  button and then drop to <body> when it unmounts — the exact defect this
 *  restoration exists to prevent. A contained `relatedTarget` therefore leaves
 *  the remembered element alone rather than overwriting it, because the
 *  element the reader actually came from is still the right one to return to.
 */
export function recordFocusOrigin(root: HTMLElement, previous: EventTarget | null): void {
  if (previous instanceof HTMLElement && root.contains(previous)) return;
  focusOrigin = previous instanceof HTMLElement ? previous : null;
}

/** Forget it. Called when focus leaves the toast for somewhere else, so a
 *  later auto-expiry cannot pull the reader out of wherever they moved to, and
 *  on unmount, so nothing holds a detached element for the life of the tab. */
export function clearFocusOrigin(): void {
  focusOrigin = null;
}

/** Put focus back, if the element is still in the document.
 *
 *  Called BEFORE the dismiss dispatch on every path — key, pointer and the 8s
 *  expiry — so focus never passes through <body> on the way, and so the caller
 *  does not depend on whether React has flushed the unmount yet. */
export function restoreFocusAfterDismiss(): void {
  const previous = focusOrigin;
  focusOrigin = null;
  if (previous != null && previous.isConnected) previous.focus();
}

/** Test-only view of the remembered element, so a test can prove the reset
 *  clears it rather than inferring it from a focus outcome. */
export function _peekFocusOriginForTests(): HTMLElement | null {
  return focusOrigin;
}
