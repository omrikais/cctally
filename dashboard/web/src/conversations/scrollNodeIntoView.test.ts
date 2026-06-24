// #234 — pure landing-math tests for computeAlignScrollTop. JSDOM has NO layout,
// so the DOM wrapper scrollNodeIntoView and pixel-exact landing are verified by
// the Playwright ui-qa gate, not here; only the offset/clamp math is unit-tested.
import { describe, expect, it } from 'vitest';
import { computeAlignScrollTop } from './scrollNodeIntoView';

describe('computeAlignScrollTop', () => {
  // el is 100px tall, currently 500px below the scroller's top edge; scroller is
  // scrolled to 1000 and 800px tall. relTop = 500 - 0 + 1000 = 1500.
  const base = { elTop: 500, elHeight: 100, scrollerTop: 0, scrollTop: 1000, viewportHeight: 800, maxScrollTop: 100000 };

  it('start aligns the element top to the scroller top', () => {
    expect(computeAlignScrollTop({ ...base, align: 'start' })).toBe(1500);
  });

  it('center puts the element mid-viewport', () => {
    // 1500 - (800 - 100)/2 = 1500 - 350 = 1150
    expect(computeAlignScrollTop({ ...base, align: 'center' })).toBe(1150);
  });

  it('clamps to [0, maxScrollTop]', () => {
    expect(computeAlignScrollTop({ ...base, scrollTop: 0, elTop: -2000, align: 'start' })).toBe(0);
    expect(computeAlignScrollTop({ ...base, maxScrollTop: 1200, align: 'start' })).toBe(1200);
  });

  it('honors a non-zero scrollerTop (scroller not at viewport origin)', () => {
    // relTop = 500 - 200 + 1000 = 1300
    expect(computeAlignScrollTop({ ...base, scrollerTop: 200, align: 'start' })).toBe(1300);
  });
});
