import { describe, it, expect } from 'vitest';
import { costClass, costIntensity } from './cost';

describe('costClass', () => {
  it('returns a neutral cost-none for unknown (null/undefined) cost', () => {
    expect(costClass(null)).toBe('cost-none');
    expect(costClass(undefined)).toBe('cost-none');
  });

  it('keeps the numeric bins unchanged', () => {
    expect(costClass(0)).toBe('cost-xs');      // < 0.25
    expect(costClass(0.24)).toBe('cost-xs');
    expect(costClass(0.25)).toBe('cost-low');  // [0.25, 1)
    expect(costClass(0.99)).toBe('cost-low');
    expect(costClass(1.0)).toBe('cost-mid');   // [1, 3)
    expect(costClass(2.99)).toBe('cost-mid');
    expect(costClass(3.0)).toBe('cost-high');  // >= 3
    expect(costClass(99)).toBe('cost-high');
  });
});

describe('costIntensity', () => {
  it('is the ratio of turn cost to the session max, clamped to [0,1]', () => {
    expect(costIntensity(0, 4)).toBe(0);
    expect(costIntensity(1, 4)).toBe(0.25);
    expect(costIntensity(4, 4)).toBe(1);
    // over-max (a later-loaded heavier turn before the max updates) clamps to 1
    expect(costIntensity(8, 4)).toBe(1);
  });
  it('returns 0 when the max is 0 or non-finite (no positive-cost turn loaded)', () => {
    expect(costIntensity(0, 0)).toBe(0);
    expect(costIntensity(2, 0)).toBe(0);
    expect(costIntensity(2, Number.NaN)).toBe(0);
  });
});
