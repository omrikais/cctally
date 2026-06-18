import { describe, it, expect } from 'vitest';
import { costClass } from './cost';

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
