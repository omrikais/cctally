import { describe, it, expect } from 'vitest';
import { modelChipClass, modelChipSummary } from './model';

describe('modelChipClass', () => {
  it('maps each known family to its own class', () => {
    expect(modelChipClass('claude-opus-4-8')).toBe('opus');
    expect(modelChipClass('claude-sonnet-4-6')).toBe('sonnet');
    expect(modelChipClass('claude-haiku-4-5-20251001')).toBe('haiku');
    expect(modelChipClass('claude-fable-5')).toBe('fable');
  });
  // #244 — the regression guard: unrecognized / null / empty must land in the
  // neutral `other` bucket, NEVER silently borrow the `sonnet` identity (the
  // pre-fix default).
  it('routes unrecognized + null/empty to other, never sonnet', () => {
    expect(modelChipClass('gpt-5')).toBe('other');
    expect(modelChipClass('<synthetic>')).toBe('other');
    expect(modelChipClass(null)).toBe('other');
    expect(modelChipClass(undefined)).toBe('other');
    expect(modelChipClass('')).toBe('other');
  });
});

describe('modelChipSummary', () => {
  it('empty models → no chips', () => {
    expect(modelChipSummary([])).toEqual({ chips: [], extra: 0 });
  });
  it('a known model → a chip labelled by its family name', () => {
    expect(modelChipSummary(['claude-opus-4-8']))
      .toEqual({ chips: [{ cls: 'opus', label: 'opus' }], extra: 0 });
  });
  it('a fable model → a fable chip (not sonnet)', () => {
    expect(modelChipSummary(['claude-fable-5']))
      .toEqual({ chips: [{ cls: 'fable', label: 'fable' }], extra: 0 });
  });
  it('an unrecognized model → an `other` chip labelled by its abbreviation', () => {
    expect(modelChipSummary(['gpt-5']))
      .toEqual({ chips: [{ cls: 'other', label: 'gpt-5' }], extra: 0 });
    expect(modelChipSummary(['<synthetic>']))
      .toEqual({ chips: [{ cls: 'other', label: '<synthetic>' }], extra: 0 });
  });
  it('dedupes models that share a chip class', () => {
    expect(modelChipSummary(['claude-opus-4-8', 'claude-opus-4-7']))
      .toEqual({ chips: [{ cls: 'opus', label: 'opus' }], extra: 0 });
  });
  it('caps at 2 distinct classes and reports the overflow, preserving order', () => {
    expect(modelChipSummary(['claude-haiku-4-5', 'claude-opus-4-8', 'claude-sonnet-4-6']))
      .toEqual({ chips: [{ cls: 'haiku', label: 'haiku' }, { cls: 'opus', label: 'opus' }], extra: 1 });
  });
});
