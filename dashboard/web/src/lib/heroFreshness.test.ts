import { describe, it, expect } from 'vitest';
import { heroFreshnessLabel } from './heroFreshness';

describe('heroFreshnessLabel', () => {
  it('null/undefined → fresh (calm; do not alarm on missing age)', () => {
    expect(heroFreshnessLabel(null)).toBe('fresh');
    expect(heroFreshnessLabel(undefined)).toBe('fresh');
  });
  it('a benign 8-minute snapshot reads fresh (FRESH-1)', () => {
    expect(heroFreshnessLabel(8 * 60)).toBe('fresh');
  });
  it('escalates by age: fresh ≤15m, aging ≤60m, stale >60m', () => {
    expect(heroFreshnessLabel(0)).toBe('fresh');
    expect(heroFreshnessLabel(15 * 60 - 1)).toBe('fresh');
    expect(heroFreshnessLabel(15 * 60)).toBe('fresh');
    expect(heroFreshnessLabel(15 * 60 + 1)).toBe('aging');
    expect(heroFreshnessLabel(60 * 60)).toBe('aging');
    expect(heroFreshnessLabel(60 * 60 + 1)).toBe('stale');
  });
});
