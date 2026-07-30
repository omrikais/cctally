import { describe, expect, it } from 'vitest';
import {
  createModelColorAllocator,
  logicalModelKey,
  oklabDistance,
} from './modelColor';

const CURRENT_MODELS = [
  'claude-opus-4-8',
  'claude-opus-5',
  'claude-fable-5',
  'gpt-5.6-sol',
  'gpt-5.6-terra',
  'gpt-5.5',
  'gpt-5.6-luna',
  'gpt-5.3-codex-spark',
];

function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => {
    const channel = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return channel <= 0.04045
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrastRatio(a: string, b: string): number {
  const [lighter, darker] = [relativeLuminance(a), relativeLuminance(b)]
    .sort((x, y) => y - x);
  return (lighter + 0.05) / (darker + 0.05);
}

describe('logicalModelKey', () => {
  it('collapses dated and capacity-qualified aliases only', () => {
    expect(logicalModelKey(' Claude-Opus-4-8-20260701[1m] '))
      .toBe('claude-opus-4-8');
    expect(logicalModelKey('claude-opus-4-8')).toBe('claude-opus-4-8');
    expect(logicalModelKey('claude-opus-5')).toBe('claude-opus-5');
    expect(logicalModelKey('gpt-5.6-sol')).toBe('gpt-5.6-sol');
  });

  it('returns an empty key for absent model identities', () => {
    expect(logicalModelKey(null)).toBe('');
    expect(logicalModelKey(undefined)).toBe('');
    expect(logicalModelKey('   ')).toBe('');
  });
});

describe('createModelColorAllocator', () => {
  it('shares aliases and separates distinct releases', () => {
    const colors = createModelColorAllocator();
    expect(colors.styleFor('claude-opus-4-8-20260701'))
      .toEqual(colors.styleFor('claude-opus-4-8'));
    expect(colors.styleFor('claude-opus-5'))
      .not.toEqual(colors.styleFor('claude-opus-4-8'));
  });

  it('preserves existing assignments when a future model is added', () => {
    const colors = createModelColorAllocator();
    const before = new Map(
      CURRENT_MODELS.map((model) => [model, colors.styleFor(model)]),
    );

    colors.styleFor('future-provider-nova-7');

    for (const [model, style] of before) {
      expect(colors.styleFor(model)).toEqual(style);
    }
  });

  it('keeps the current logical models perceptually separated', () => {
    const colors = createModelColorAllocator();
    const backgrounds = CURRENT_MODELS.map(
      (model) => colors.styleFor(model)!.backgroundColor,
    );
    for (let i = 0; i < backgrounds.length; i += 1) {
      for (let j = i + 1; j < backgrounds.length; j += 1) {
        expect(oklabDistance(backgrounds[i], backgrounds[j]))
          .toBeGreaterThanOrEqual(0.08);
      }
    }
  });

  it('chooses chip text with at least 4.5:1 contrast', () => {
    const colors = createModelColorAllocator();
    for (const model of CURRENT_MODELS) {
      const style = colors.styleFor(model)!;
      expect(contrastRatio(style.backgroundColor, style.color))
        .toBeGreaterThanOrEqual(4.5);
    }
  });

  it('returns no style for an absent model identity', () => {
    const colors = createModelColorAllocator();
    expect(colors.styleFor(null)).toBeUndefined();
    expect(colors.styleFor('')).toBeUndefined();
  });
});
