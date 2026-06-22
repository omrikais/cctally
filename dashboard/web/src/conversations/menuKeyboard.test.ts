import { describe, expect, it } from 'vitest';
import { nextRovingIndex } from './menuKeyboard';

// #224 — pure roving-focus index math shared by the ExportMenu / FocusMoreMenu
// APG menu keyboard pattern. Given a key, the current active index, and the item
// count, return the next active index, or null when the key is not a
// vertical-navigation key the menu handles.
describe('nextRovingIndex', () => {
  it('ArrowDown advances and wraps from last to first', () => {
    expect(nextRovingIndex('ArrowDown', 0, 3)).toBe(1);
    expect(nextRovingIndex('ArrowDown', 1, 3)).toBe(2);
    expect(nextRovingIndex('ArrowDown', 2, 3)).toBe(0);
  });

  it('ArrowUp retreats and wraps from first to last', () => {
    expect(nextRovingIndex('ArrowUp', 2, 3)).toBe(1);
    expect(nextRovingIndex('ArrowUp', 1, 3)).toBe(0);
    expect(nextRovingIndex('ArrowUp', 0, 3)).toBe(2);
  });

  it('Home/End jump to the first/last index', () => {
    expect(nextRovingIndex('Home', 2, 3)).toBe(0);
    expect(nextRovingIndex('End', 0, 3)).toBe(2);
  });

  it('returns null for a non-navigation key', () => {
    expect(nextRovingIndex('Enter', 0, 3)).toBeNull();
    expect(nextRovingIndex('a', 0, 3)).toBeNull();
    expect(nextRovingIndex('ArrowRight', 0, 3)).toBeNull();
  });

  it('returns null when the item count is zero', () => {
    expect(nextRovingIndex('ArrowDown', 0, 0)).toBeNull();
    expect(nextRovingIndex('Home', 0, 0)).toBeNull();
  });
});
