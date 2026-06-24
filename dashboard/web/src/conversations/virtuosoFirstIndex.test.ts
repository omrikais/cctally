import { describe, it, expect } from 'vitest';
import { VIRTUAL_INDEX_BASE, applyFirstItemDelta } from './virtuosoFirstIndex';

describe('applyFirstItemDelta', () => {
  it('decrements by addedTop on a prepend', () => {
    expect(applyFirstItemDelta(VIRTUAL_INDEX_BASE, { addedTop: 5, droppedTop: 0 }))
      .toBe(VIRTUAL_INDEX_BASE - 5);
  });
  it('increments by droppedTop on a head trim', () => {
    expect(applyFirstItemDelta(VIRTUAL_INDEX_BASE, { addedTop: 0, droppedTop: 3 }))
      .toBe(VIRTUAL_INDEX_BASE + 3);
  });
  it('is unchanged on a tail-only op (append / bottom-trim)', () => {
    expect(applyFirstItemDelta(42, { addedTop: 0, droppedTop: 0 })).toBe(42);
  });
  it('nets a simultaneous head add + head trim', () => {
    expect(applyFirstItemDelta(100, { addedTop: 5, droppedTop: 2 })).toBe(97);
  });
  it('clamps at 0 (never goes negative)', () => {
    expect(applyFirstItemDelta(3, { addedTop: 10, droppedTop: 0 })).toBe(0);
  });
  it('is pure (same inputs → same output, no shared state)', () => {
    const a = applyFirstItemDelta(50, { addedTop: 1, droppedTop: 0 });
    const b = applyFirstItemDelta(50, { addedTop: 1, droppedTop: 0 });
    expect(a).toBe(b);
    expect(a).toBe(49);
  });
});
