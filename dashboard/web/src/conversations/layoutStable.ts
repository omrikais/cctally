// #234 §2.2-3 / Codex P0-2 — the shared quiesce predicate for both R1's walk and
// R2's post-force-open center. The walk/center must wait until the WHOLE tuple
// (mounted array-range, scrollHeight, scrollTop, target-anchor rect) is stable
// across several rAFs — not the range alone. react-virtuoso's onItemsRendered
// ignores same-range measurement ticks, so a range-only waiter can declare
// settled while ResizeObserver is still correcting scrollHeight (15705→~74266 in
// the measured R1 failure), which is exactly what strands the landing.
export interface LayoutSnapshot {
  first: number; last: number;          // mounted array-range
  scrollHeight: number; scrollTop: number;
  anchorTop: number | null;             // target/anchor rect.top rel. scroller, or null if unmounted
}

/** Pure: two snapshots represent a settled layout (spec §2.2-3 / Codex P0-2). */
export function isLayoutStable(a: LayoutSnapshot, b: LayoutSnapshot, tol = 1): boolean {
  if (a.first !== b.first || a.last !== b.last) return false;
  if (Math.abs(a.scrollHeight - b.scrollHeight) > tol) return false;
  if (Math.abs(a.scrollTop - b.scrollTop) > tol) return false;
  if (a.anchorTop == null || b.anchorTop == null) return false;
  return Math.abs(a.anchorTop - b.anchorTop) <= tol;
}
