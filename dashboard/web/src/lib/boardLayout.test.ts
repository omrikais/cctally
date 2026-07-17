import { describe, it, expect } from 'vitest';
import { boardMode, boardSpan, type BoardMode } from './boardLayout';
import { MOBILE_BREAKPOINT_PX, BENTO_BREAKPOINT_PX, WIDE_BREAKPOINT_PX, BOARD_WIDE_PX } from './breakpoints';

describe('boardMode boundaries', () => {
  const cases: Array<[number, BoardMode]> = [
    [639, 'stack'], [640, 'stack'], [899, 'stack'],
    [900, 'intermediate'], [1024, 'intermediate'], [1100, 'intermediate'], [1199, 'intermediate'],
    [1200, 'bento'], [1440, 'bento'], [1920, 'bento'],
  ];
  it.each(cases)('width %i → %s', (w, expected) => {
    expect(boardMode(w)).toBe(expected);
  });
});

describe('boardSpan policy', () => {
  // stack / intermediate / bento
  const tall: Array<['sessions'|'trend'|'projects', [number, number, number]]> = [
    ['sessions', [6, 12, 6]],
    ['trend', [3, 6, 3]],
    ['projects', [3, 6, 3]],
  ];
  it.each(tall)('%s spans', (id, [s, i, b]) => {
    expect(boardSpan(id, 'stack')).toBe(s);
    expect(boardSpan(id, 'intermediate')).toBe(i);
    expect(boardSpan(id, 'bento')).toBe(b);
  });
  it('medium/alerts are mode-invariant', () => {
    for (const m of ['stack', 'intermediate', 'bento'] as BoardMode[]) {
      expect(boardSpan('weekly', m)).toBe(6);
      expect(boardSpan('alerts', m)).toBe(12);
    }
  });
});

describe('breakpoint ordering invariant', () => {
  it('640 < 900 < 1100 < 1200', () => {
    expect(MOBILE_BREAKPOINT_PX).toBeLessThan(BENTO_BREAKPOINT_PX);
    expect(BENTO_BREAKPOINT_PX).toBeLessThan(WIDE_BREAKPOINT_PX);
    expect(WIDE_BREAKPOINT_PX).toBeLessThan(BOARD_WIDE_PX);
  });
});
