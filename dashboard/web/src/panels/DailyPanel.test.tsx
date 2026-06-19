import { describe, it, expect } from 'vitest';
import { formatDailyCell } from './DailyPanel';
import { fmt } from '../lib/fmt';

describe('formatDailyCell (#214 M3-3)', () => {
  it('mobile: $-prefixed ceil integer', () => {
    expect(formatDailyCell(527.3, true)).toBe('$528');
    expect(formatDailyCell(50.27, true)).toBe('$51');
    expect(formatDailyCell(1, true)).toBe('$1');
  });
  it('desktop: routes to full usd2 precision', () => {
    expect(formatDailyCell(527.3, false)).toBe(fmt.usd2(527.3));
  });
  it('zero or non-positive renders the em dash', () => {
    expect(formatDailyCell(0, true)).toBe('—');
    expect(formatDailyCell(0, false)).toBe('—');
  });
});
