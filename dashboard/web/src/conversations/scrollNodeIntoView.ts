// #234 §2.1 — the single landing primitive. Both R1 (cold far jump) and R2
// (nested find-hit centering) land precisely by writing the Virtuoso scroller's
// scrollTop directly to a MEASURED element offset, rather than the library's
// estimate-based convergence (R1) or a native scrollIntoView that is inert
// inside a giant absolute-positioned library row (R2, measured 0px).
export interface AlignArgs {
  elTop: number;          // el.getBoundingClientRect().top
  elHeight: number;       // el.getBoundingClientRect().height
  scrollerTop: number;    // scroller.getBoundingClientRect().top
  scrollTop: number;      // scroller.scrollTop
  viewportHeight: number; // scroller.clientHeight
  maxScrollTop: number;   // scroller.scrollHeight - scroller.clientHeight
  align: 'start' | 'center';
}

/** Pure: the scrollTop that aligns `el` to `start` (top) or `center` of the scroller. */
export function computeAlignScrollTop(a: AlignArgs): number {
  const relTop = a.elTop - a.scrollerTop + a.scrollTop;
  const raw = a.align === 'center' ? relTop - (a.viewportHeight - a.elHeight) / 2 : relTop;
  return Math.max(0, Math.min(raw, a.maxScrollTop));
}

/**
 * Measure `el` + `scroller` and return the scrollTop that aligns `el` to
 * `start`/`center` — the exact math `scrollNodeIntoView` writes. Split out (#237)
 * so the convergent re-center loop can read the live desired offset each frame
 * WITHOUT writing, sharing ONE measurement+compute path with the writer.
 */
export function alignScrollTop(scroller: HTMLElement, el: HTMLElement, align: 'start' | 'center'): number {
  const er = el.getBoundingClientRect();
  const sr = scroller.getBoundingClientRect();
  return computeAlignScrollTop({
    elTop: er.top, elHeight: er.height, scrollerTop: sr.top, scrollTop: scroller.scrollTop,
    viewportHeight: scroller.clientHeight, maxScrollTop: scroller.scrollHeight - scroller.clientHeight, align,
  });
}

/**
 * Land `el` precisely inside the Virtuoso scroller by writing scrollTop directly
 * (spec §2.1). NOT `el.scrollIntoView` — that is inert inside the library-managed
 * absolute-positioned row (measured 0px). Jump/find landings pass behavior:'auto'
 * (Codex P1-1: 'smooth' reintroduces async motion during measurement — the exact
 * bug #233 fixed). Callers MUST pass the card <details>/item element, never a
 * position:sticky <summary> (its rect reports the viewport top, not the card's
 * natural top — Codex P2-1).
 */
export function scrollNodeIntoView(
  scroller: HTMLElement, el: HTMLElement,
  align: 'start' | 'center', behavior: ScrollBehavior = 'auto',
): void {
  const top = alignScrollTop(scroller, el, align);
  // JSDOM (the vitest env) does not implement Element.prototype.scrollTo; guard
  // it so the jump effect can run end-to-end under test, falling back to a direct
  // scrollTop write. Real browsers always have scrollTo (this is the precision path).
  if (typeof scroller.scrollTo === 'function') scroller.scrollTo({ top, behavior });
  else scroller.scrollTop = top;
}
