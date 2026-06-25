import { describe, it, expect } from 'vitest';
import { modelChipSummary } from './model';

describe('modelChipSummary', () => {
  it('empty models → no chips', () => {
    expect(modelChipSummary([])).toEqual({ classes: [], extra: 0 });
  });
  it('single model maps to its chip class', () => {
    expect(modelChipSummary(['claude-opus-4-8'])).toEqual({ classes: ['opus'], extra: 0 });
  });
  it('dedupes models that share a chip class', () => {
    expect(modelChipSummary(['claude-opus-4-8', 'claude-opus-4-7']))
      .toEqual({ classes: ['opus'], extra: 0 });
  });
  it('caps at 2 distinct classes and reports the overflow, preserving order', () => {
    expect(modelChipSummary(['claude-haiku-4-5', 'claude-opus-4-8', 'claude-sonnet-4-6']))
      .toEqual({ classes: ['haiku', 'opus'], extra: 1 });
  });
});
