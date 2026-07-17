import { useCallback, useEffect, useRef, useState } from 'react';

// #304 S3 — the desktop reader header's third responsive axis: an ELEMENT-width
// resolver (not a window media query — the squeeze case is the ≥1101 outline
// column narrowing the reader while the viewport stays wide). NORMATIVE
// threshold; revise via spec, not silently. Measurement contract (spec §1 /
// Codex F6): observe the .conv-reader ROOT; one metric everywhere —
// getBoundingClientRect().width — read synchronously in the ref callback (no
// first-paint flash) and RE-READ inside the ResizeObserver callback (observer
// entries only signal WHEN to re-measure; their box rects are ignored so
// border/content-box drift cannot split the 719/720/721 boundary). Width 0 or
// unmeasurable (JSDOM) → 'full'.
export const COMPACT_READER_CONTROLS_PX = 720;

export type ReaderControlsDensity = 'full' | 'compact';

export function useReaderControlsDensity(): {
  density: ReaderControlsDensity;
  readerRef: (el: HTMLElement | null) => void;
} {
  const [density, setDensity] = useState<ReaderControlsDensity>('full');
  const roRef = useRef<ResizeObserver | null>(null);
  const measure = useCallback((el: HTMLElement) => {
    const w = el.getBoundingClientRect().width;
    setDensity(w > 0 && w < COMPACT_READER_CONTROLS_PX ? 'compact' : 'full');
  }, []);
  const readerRef = useCallback((el: HTMLElement | null) => {
    roRef.current?.disconnect();
    roRef.current = null;
    if (!el) return;
    measure(el);
    if (typeof ResizeObserver !== 'undefined') {
      const ro = new ResizeObserver(() => measure(el));
      ro.observe(el);
      roRef.current = ro;
    }
  }, [measure]);
  useEffect(() => () => { roRef.current?.disconnect(); }, []);
  return { density, readerRef };
}
