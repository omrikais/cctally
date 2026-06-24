// #234 §2.2-3 / Codex P0-2 — the quiesce predicate. onItemsRendered deliberately
// ignores same-range measurement ticks, so a range-only waiter can declare
// "settled" while ResizeObserver is still correcting scrollHeight — the measured
// R1 failure mode. The walk + R2 center wait on the full tuple instead. Pure.
import { describe, expect, it } from 'vitest';
import { isLayoutStable, type LayoutSnapshot } from './layoutStable';

const snap = (o: Partial<LayoutSnapshot> = {}): LayoutSnapshot =>
  ({ first: 0, last: 10, scrollHeight: 50000, scrollTop: 1000, anchorTop: 200, ...o });

describe('isLayoutStable', () => {
  it('stable when every field is within tolerance', () => {
    expect(isLayoutStable(snap(), snap({ scrollTop: 1001, anchorTop: 201 }), 2)).toBe(true);
  });
  it('unstable when scrollHeight is still correcting (the R1 failure mode)', () => {
    expect(isLayoutStable(snap(), snap({ scrollHeight: 74000 }), 2)).toBe(false);
  });
  it('unstable when the mounted range moved', () => {
    expect(isLayoutStable(snap(), snap({ last: 14 }), 2)).toBe(false);
  });
  it('treats a null anchorTop (target unmounted) on either side as unstable', () => {
    expect(isLayoutStable(snap(), snap({ anchorTop: null }), 2)).toBe(false);
  });
});
