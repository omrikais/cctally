import { useEffect } from 'react';

// Shared scroll lock for overlay surfaces. A module-level depth counter keeps
// the page locked while ANY overlay is open, so stacked overlays (panel modal
// -> share -> composer) don't prematurely restore scroll when an inner one
// closes. The saved values are captured only at the 0 -> 1 edge, so a second
// concurrent lock never records 'hidden' as the "original". Strict-Mode's
// setup -> cleanup -> setup double-invoke is balanced (+1, -1, +1) and re-saves
// the same values, so it's inert.
//
// We lock BOTH <html> (documentElement) and <body>. In standards mode <html>
// is the viewport scroller (cctally's `html, body` set no overflow/height), so
// `overflow:hidden` on <body> alone is inert — the page keeps scrolling behind
// the overlay. Locking documentElement is the load-bearing part; body is kept
// for setups where <body> is the scroller. (Verified in Chromium at 390px —
// #214 M1 QA caught the body-only version scrolling behind every modal.)
let lockCount = 0;
let savedHtmlOverflow = '';
let savedBodyOverflow = '';

export function useScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    if (lockCount === 0) {
      const html = document.documentElement;
      savedHtmlOverflow = html.style.overflow;
      savedBodyOverflow = document.body.style.overflow;
      html.style.overflow = 'hidden';
      document.body.style.overflow = 'hidden';
    }
    lockCount += 1;
    return () => {
      lockCount -= 1;
      if (lockCount === 0) {
        document.documentElement.style.overflow = savedHtmlOverflow;
        document.body.style.overflow = savedBodyOverflow;
      }
    };
  }, [active]);
}

/** Test-only: reset the module-level lock state between tests so a test that
 *  forgets to unmount an open overlay can't poison `lockCount` for later
 *  tests in the same file. Never imported by production code (tree-shaken
 *  out of the bundle). Mirrors the `_resetForTests` convention in
 *  `store/store.ts` and `store/keymap.ts`. */
export function _resetForTests(): void {
  lockCount = 0;
  savedHtmlOverflow = '';
  savedBodyOverflow = '';
}
