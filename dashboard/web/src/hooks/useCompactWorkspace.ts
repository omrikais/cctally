import { useEffect, useState } from 'react';
import { COMPACT_WORKSPACE_MEDIA_QUERY } from '../lib/breakpoints';

// #304 S1 — track the viewport against the conversation workspace's compact
// threshold (880px). Returns true at/below COMPACT_WORKSPACE_PX, where the
// workspace is single-pane (rail OR reader) instead of the two-pane shell.
// STRUCTURALLY different DOM (a single child vs rail+reader), so it must be a JS
// branch, not a pure CSS reflow. Mirrors useIsMobile/useIsWide exactly; stays in
// lockstep with the CSS via the shared breakpoints module.
export function useCompactWorkspace(): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia(COMPACT_WORKSPACE_MEDIA_QUERY).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia(COMPACT_WORKSPACE_MEDIA_QUERY);
    const onChange = (e: MediaQueryListEvent | { matches: boolean }) => setMatches(e.matches);
    mql.addEventListener('change', onChange as (e: MediaQueryListEvent) => void);
    setMatches(mql.matches);
    return () => mql.removeEventListener('change', onChange as (e: MediaQueryListEvent) => void);
  }, []);

  return matches;
}
