import { describe, it, expect } from 'vitest';
import { shouldShowMilestoneTicks } from './milestoneTicks';

describe('shouldShowMilestoneTicks', () => {
  it('false below the threshold (early week)', () => {
    expect(shouldShowMilestoneTicks(11)).toBe(false);
  });
  it('true at/above the threshold', () => {
    expect(shouldShowMilestoneTicks(15)).toBe(true);
    expect(shouldShowMilestoneTicks(40)).toBe(true);
  });
  it('treats null/undefined as 0 → false', () => {
    expect(shouldShowMilestoneTicks(null)).toBe(false);
    expect(shouldShowMilestoneTicks(undefined)).toBe(false);
  });
});
