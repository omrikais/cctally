import { useEffect } from 'react';

// Shared scroll lock for overlay surfaces. A module-level depth counter keeps
// the page locked while ANY overlay is open, so stacked overlays (panel modal
// -> share -> composer) don't prematurely restore scroll when an inner one
// closes. The saved values are captured only at the 0 -> 1 edge, so a second
// concurrent lock never records 'hidden' as the "original". Strict-Mode's
// setup -> cleanup -> setup double-invoke is balanced (+1, -1, +1) and re-saves
// the same values, so it's inert.
//
// We lock BOTH <html> (documentElement) and <body>. On the current stylesheet
// `html, body` set no `overflow` (both resolve to the initial `visible`), and CSS
// overflow PROPAGATION governs: when the root <html>'s overflow is `visible`, the
// UA applies the FIRST <body>'s overflow to the viewport instead. So on this
// stylesheet `overflow: hidden` on <body> ALONE already locks the page — the
// propagated value is what stops the viewport scrolling. Locking documentElement
// too is belt-and-suspenders, NOT load-bearing here; it's kept for a future
// stylesheet that gives <html> its own non-`visible` overflow (which would cancel
// the propagation and make <html> the scroller). (#281 S5 B4 — corrects the prior
// inverted claim; body-only overflow:hidden is sufficient on this stylesheet.)
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
