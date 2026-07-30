import { describe, it, expect } from 'vitest';
import { modelChipClass, modelChipStyle, modelChipSummary } from './model';

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

describe('modelChipStyle', () => {
  it('gives Opus 4.8 and Opus 5 exact-model colors despite one family class', () => {
    expect(modelChipClass('claude-opus-4-8')).toBe('opus');
    expect(modelChipClass('claude-opus-5')).toBe('opus');
    expect(modelChipStyle('claude-opus-4-8'))
      .not.toEqual(modelChipStyle('claude-opus-5'));
  });
});

describe('modelChipSummary', () => {
  it('empty models → no chips', () => {
    expect(modelChipSummary([])).toEqual({ chips: [], extra: 0 });
  });
  it('a known model → a chip labelled by its logical release', () => {
    expect(modelChipSummary(['claude-opus-4-8']))
      .toEqual({
        chips: [{
          cls: 'opus', label: 'opus-4-8', full: 'opus-4-8',
          model: 'claude-opus-4-8',
        }],
        extra: 0,
      });
  });
  it('a fable model → a fable chip (not sonnet)', () => {
    expect(modelChipSummary(['claude-fable-5']))
      .toEqual({
        chips: [{
          cls: 'fable', label: 'fable-5', full: 'fable-5',
          model: 'claude-fable-5',
        }],
        extra: 0,
      });
  });
  it('an unrecognized model → an `other` chip labelled by its abbreviation', () => {
    expect(modelChipSummary(['gpt-5']))
      .toEqual({
        chips: [{
          cls: 'other', label: 'gpt-5', full: 'gpt-5', model: 'gpt-5',
        }],
        extra: 0,
      });
    expect(modelChipSummary(['<synthetic>']))
      .toEqual({
        chips: [{
          cls: 'other', label: '<synthetic>', full: '<synthetic>',
          model: '<synthetic>',
        }],
        extra: 0,
      });
  });
  it('dedupes aliases of one logical release', () => {
    expect(modelChipSummary([
      'claude-opus-4-8-20260701',
      'claude-opus-4-8[1m]',
    ])).toEqual({
      chips: [{
        cls: 'opus', label: 'opus-4-8', full: 'opus-4-8',
        model: 'claude-opus-4-8-20260701',
      }],
      extra: 0,
    });
  });
  it('keeps distinct same-family releases as separate chips', () => {
    expect(modelChipSummary(['claude-opus-4-8', 'claude-opus-5']))
      .toEqual({
        chips: [
          {
            cls: 'opus', label: 'opus-4-8', full: 'opus-4-8',
            model: 'claude-opus-4-8',
          },
          {
            cls: 'opus', label: 'opus-5', full: 'opus-5',
            model: 'claude-opus-5',
          },
        ],
        extra: 0,
      });
  });
  it('caps at 2 logical models and reports the overflow, preserving order', () => {
    expect(modelChipSummary(['claude-haiku-4-5', 'claude-opus-4-8', 'claude-sonnet-4-6']))
      .toEqual({
        chips: [
          {
            cls: 'haiku', label: 'haiku-4-5', full: 'haiku-4-5',
            model: 'claude-haiku-4-5',
          },
          {
            cls: 'opus', label: 'opus-4-8', full: 'opus-4-8',
            model: 'claude-opus-4-8',
          },
        ],
        extra: 1,
      });
  });
  // #304 S3 (Codex F4) — the rigid two-line rail stats line must not grow with
  // arbitrary model-id length. An `other` chip's DISPLAY label is bounded to
  // OTHER_CHIP_LABEL_MAX (12) chars + ellipsis; the untruncated label rides on
  // `full` for the chip's title / accessible name. Known families are already
  // short and stay unchanged.
  it('caps an unbounded other-model label at 12 chars + ellipsis, keeping the full label', () => {
    const s = modelChipSummary(['internal-experimental-model-v2-preview-20260701'], 1);
    expect(s.chips).toHaveLength(1);
    expect(s.chips[0].cls).toBe('other');
    // abbreviateModel strips the -YYYYMMDD stamp first
    expect(s.chips[0].full).toBe('internal-experimental-model-v2-preview');
    expect(s.chips[0].label).toBe('internal-exp…');
    expect(s.chips[0].label.length).toBe(13); // 12 + ellipsis
  });

  it('leaves short model labels unbounded/unchanged', () => {
    const s = modelChipSummary(['gpt-5', 'claude-opus-4-8'], 2);
    expect(s.chips.map((c) => c.label)).toEqual(['gpt-5', 'opus-4-8']);
    expect(s.chips.map((c) => c.full)).toEqual(['gpt-5', 'opus-4-8']);
  });
});
