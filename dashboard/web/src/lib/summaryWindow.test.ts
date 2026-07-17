import { describe, it, expect } from 'vitest';
import { summarize, SUMMARY_WINDOW_CAP } from './summaryWindow';

const rowsOf = (n: number) => Array.from({ length: n }, (_, i) => i);

describe('summarize — stack slices to CAP (newest-first)', () => {
  it('CAP is 3', () => expect(SUMMARY_WINDOW_CAP).toBe(3));
  it('0 rows → empty, hidden 0', () =>
    expect(summarize([], 'stack')).toEqual({ visible: [], hiddenCount: 0 }));
  it('1 row → 1 visible, hidden 0', () =>
    expect(summarize(rowsOf(1), 'stack')).toEqual({ visible: [0], hiddenCount: 0 }));
  it('CAP rows → all visible, hidden 0', () =>
    expect(summarize(rowsOf(3), 'stack')).toEqual({ visible: [0, 1, 2], hiddenCount: 0 }));
  it('CAP+1 → 3 visible, hidden 1, actually drops a row', () => {
    const r = summarize(rowsOf(4), 'stack');
    expect(r.visible).toEqual([0, 1, 2]);
    expect(r.hiddenCount).toBe(1);
    expect(r.visible).not.toContain(3);
  });
  it('large N → 3 visible, hidden N-3', () => {
    const r = summarize(rowsOf(12), 'stack');
    expect(r.visible).toHaveLength(3);
    expect(r.hiddenCount).toBe(9);
  });
});

describe('summarize — non-stack returns ALL rows', () => {
  it('intermediate → all rows, hidden 0', () => {
    const r = summarize(rowsOf(12), 'intermediate');
    expect(r.visible).toHaveLength(12);
    expect(r.hiddenCount).toBe(0);
  });
  it('bento → all rows, hidden 0', () => {
    const r = summarize(rowsOf(12), 'bento');
    expect(r.visible).toHaveLength(12);
    expect(r.hiddenCount).toBe(0);
  });
});
