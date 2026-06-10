import { describe, it, expect } from 'vitest';
import { fmt } from './fmt';

describe('fmt.durationMs', () => {
  it('formats sub-minute as X.Xs', () => {
    expect(fmt.durationMs(10668)).toBe('10.7s');
    expect(fmt.durationMs(4200)).toBe('4.2s');
  });
  it('formats >= 60s as Xm Ys, dropping a trailing 0s', () => {
    expect(fmt.durationMs(125000)).toBe('2m 5s');
    expect(fmt.durationMs(120000)).toBe('2m');
  });
  it('handles null/undefined', () => {
    expect(fmt.durationMs(null)).toBe('—');
    expect(fmt.durationMs(undefined)).toBe('—');
  });
});
