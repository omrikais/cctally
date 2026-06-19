import { useEffect } from 'react';

// Shared body-scroll lock for overlay surfaces. A module-level depth
// counter keeps the body locked while ANY overlay is open, so stacked
// overlays (panel modal -> share -> composer) don't prematurely restore
// scroll when an inner one closes. `savedOverflow` is captured only at the
// 0 -> 1 edge, so a second concurrent lock never records 'hidden' as the
// "original" value. Strict-Mode's setup -> cleanup -> setup double-invoke
// is balanced (+1, -1, +1) and re-saves the same value, so it's inert.
let lockCount = 0;
let savedOverflow = '';

export function useScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    if (lockCount === 0) {
      savedOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
    }
    lockCount += 1;
    return () => {
      lockCount -= 1;
      if (lockCount === 0) {
        document.body.style.overflow = savedOverflow;
      }
    };
  }, [active]);
}
