import { useEffect, useState } from 'react';
import { BENTO_MEDIA_QUERY, BOARD_WIDE_MEDIA_QUERY } from '../lib/breakpoints';
import type { BoardMode } from '../lib/boardLayout';

// Resolve the current board mode from the two board breakpoints. When
// matchMedia is absent (JSDOM/SSR-less) default to 'bento' so tests + the
// first paint see today's spans.
function resolveMode(): BoardMode {
  if (typeof window === 'undefined' || !window.matchMedia) return 'bento';
  if (window.matchMedia(BOARD_WIDE_MEDIA_QUERY).matches) return 'bento';
  if (window.matchMedia(BENTO_MEDIA_QUERY).matches) return 'intermediate';
  return 'stack';
}

// True board mode (>=1200 bento / 900–1199 intermediate / <900 stack).
// Lockstep with the CSS @media(min-width:900px) grid + the JS boardSpan policy.
export function useBoardMode(): BoardMode {
  const [mode, setMode] = useState<BoardMode>(resolveMode);
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const wide = window.matchMedia(BOARD_WIDE_MEDIA_QUERY);
    const nonStack = window.matchMedia(BENTO_MEDIA_QUERY);
    const onChange = () => setMode(resolveMode());
    wide.addEventListener('change', onChange);
    nonStack.addEventListener('change', onChange);
    onChange();
    return () => {
      wide.removeEventListener('change', onChange);
      nonStack.removeEventListener('change', onChange);
    };
  }, []);
  return mode;
}
