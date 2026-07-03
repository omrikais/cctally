import { useEffect, useState } from 'react';
import { BENTO_MEDIA_QUERY } from '../lib/breakpoints';

// True when the viewport is at/above the desktop bento breakpoint (>=900px),
// where the board is the fixed-height bento. Lockstep with the CSS @media
// (min-width:900px) block via the shared breakpoints module.
export function useIsDesktopBento(): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia(BENTO_MEDIA_QUERY).matches;
  });
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia(BENTO_MEDIA_QUERY);
    const onChange = (e: MediaQueryListEvent | { matches: boolean }) => setMatches(e.matches);
    mql.addEventListener('change', onChange as (e: MediaQueryListEvent) => void);
    setMatches(mql.matches);
    return () => mql.removeEventListener('change', onChange as (e: MediaQueryListEvent) => void);
  }, []);
  return matches;
}
