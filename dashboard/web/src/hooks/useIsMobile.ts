import { useEffect, useState } from 'react';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';

// Track viewport against the project's mobile breakpoint. Returns true
// when the viewport is at or below MOBILE_BREAKPOINT_PX. Used by the
// HelpOverlay and OnboardingToast for runtime branches that CSS alone
// cannot express (different DOM, not just different style). Stays in
// lockstep with the CSS @media block via the shared breakpoints module.
export function useIsMobile(): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia(MOBILE_MEDIA_QUERY);
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
