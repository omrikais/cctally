import { useEffect, useState } from 'react';
import { WIDE_MEDIA_QUERY } from '../lib/breakpoints';

// #217 S7 F10 — track the viewport against the comparison view's 1100px
// breakpoint. Returns true when there is room for the two-column side-by-side
// prompt diff; false selects the unified single-column renderer. Mirrors
// useIsMobile exactly but with WIDE_MEDIA_QUERY — the two-column and unified
// layouts are STRUCTURALLY different DOM (not a pure CSS reflow), so the choice
// must be a JS branch. Stays in lockstep with the index.css @media block via the
// shared breakpoints module.
export function useIsWide(): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return true;
    return window.matchMedia(WIDE_MEDIA_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia(WIDE_MEDIA_QUERY);
    const onChange = (e: MediaQueryListEvent | { matches: boolean }) => {
      setMatches(e.matches);
    };
    mql.addEventListener('change', onChange as (e: MediaQueryListEvent) => void);
    setMatches(mql.matches);
    return () => {
      mql.removeEventListener('change', onChange as (e: MediaQueryListEvent) => void);
    };
  }, []);

  return matches;
}
