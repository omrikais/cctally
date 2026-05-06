import { describe, it, expect } from 'vitest';
import { reconcilePanelOrder } from '../src/lib/reconcilePanelOrder';
import type { PanelId } from '../src/lib/panelRegistry';

const DEFAULT: PanelId[] = [
  'current-week', 'forecast', 'trend', 'sessions',
  'weekly', 'monthly', 'blocks', 'daily',
];

describe('reconcilePanelOrder', () => {
  it('returns default when saved is null', () => {
    expect(reconcilePanelOrder(null, DEFAULT)).toEqual(DEFAULT);
  });

  it('returns default when saved is empty', () => {
    expect(reconcilePanelOrder([], DEFAULT)).toEqual(DEFAULT);
  });

  it('returns saved unchanged when it equals default', () => {
    expect(reconcilePanelOrder(DEFAULT, DEFAULT)).toEqual(DEFAULT);
  });

  it('preserves a non-default-but-complete saved order', () => {
    const saved: PanelId[] = ['daily', 'blocks', 'monthly', 'weekly', 'sessions', 'trend', 'forecast', 'current-week'];
    expect(reconcilePanelOrder(saved, DEFAULT)).toEqual(saved);
  });

  it('drops unknown ids that are not in canonical', () => {
    const saved: PanelId[] = ['current-week', 'unknown' as PanelId, 'forecast', 'trend', 'sessions', 'weekly', 'monthly', 'blocks', 'daily'];
    expect(reconcilePanelOrder(saved, DEFAULT)).toEqual(DEFAULT);
  });

  it('appends canonical ids missing from saved at end (preserves saved relative order)', () => {
    const saved: PanelId[] = ['daily', 'blocks', 'forecast'];
    const result = reconcilePanelOrder(saved, DEFAULT);
    expect(result.slice(0, 3)).toEqual(['daily', 'blocks', 'forecast']);
    expect(result.slice(3)).toEqual(['current-week', 'trend', 'sessions', 'weekly', 'monthly']);
  });

  it('deduplicates a saved order that contains the same id twice', () => {
    const saved: PanelId[] = ['forecast', 'forecast', 'trend'];
    const result = reconcilePanelOrder(saved, DEFAULT);
    expect(result.slice(0, 2)).toEqual(['forecast', 'trend']);
    expect(result).toHaveLength(DEFAULT.length);
    expect([...result].sort()).toEqual([...DEFAULT].sort());
  });

  it('returns default when saved is not an array (malformed localStorage)', () => {
    // Simulate hand-edited localStorage giving us a non-array.
    // @ts-expect-error - the function must handle bad runtime input safely
    expect(reconcilePanelOrder(42, DEFAULT)).toEqual(DEFAULT);
    // @ts-expect-error
    expect(reconcilePanelOrder({ 0: 'forecast' }, DEFAULT)).toEqual(DEFAULT);
  });
});
