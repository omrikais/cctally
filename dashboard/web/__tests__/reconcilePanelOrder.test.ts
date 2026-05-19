import { describe, it, expect } from 'vitest';
import {
  applyPanelOrderMigration,
  reconcilePanelOrder,
} from '../src/lib/reconcilePanelOrder';
import { DEFAULT_PANEL_ORDER } from '../src/lib/panelIds';
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

// ---- Tests for panelOrderSchemaVersion=2 migration (plan §3.1, spec §2.1) ----
//
// `applyPanelOrderMigration` splices 'projects' into a saved order from
// a user on the pre-projects schema. The store loader runs this BEFORE
// `reconcilePanelOrder` so the splice lands at canonical index 4 instead
// of being shoved to the tail by the reconcile "append missing" pass.

describe('panelOrderSchemaVersion=2 migration', () => {
  it('splices projects at canonical index 4 for upgraded users', () => {
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 1);
    expect(out.panels).toEqual([
      'current-week', 'forecast', 'trend', 'sessions',
      'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
    expect(out.newVersion).toBe(2);
  });

  it('is idempotent — does not re-splice if version is already 2', () => {
    const saved = [...DEFAULT_PANEL_ORDER];  // already has projects
    const out = applyPanelOrderMigration(saved, 2);
    expect(out.panels).toEqual(DEFAULT_PANEL_ORDER);
    expect(out.newVersion).toBe(2);
  });

  it('handles saved order with custom user reordering', () => {
    const saved: PanelId[] = [
      'weekly', 'current-week', 'sessions',  // user reorder
      'forecast', 'trend',
      'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 1);
    // 'projects' inserted at index 4 of the saved order (between 'trend' and 'monthly')
    expect(out.panels).toEqual([
      'weekly', 'current-week', 'sessions', 'forecast',
      'projects',  // index 4
      'trend', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
    expect(out.newVersion).toBe(2);
  });

  it('appends projects if saved order is shorter than 4', () => {
    const saved: PanelId[] = ['current-week', 'forecast', 'trend'];  // 3 panels
    const out = applyPanelOrderMigration(saved, 1);
    expect(out.panels).toContain('projects');
    expect(out.newVersion).toBe(2);
  });

  it('passes through when saved already includes projects (manual edit)', () => {
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'projects',  // user manually added (or migrated by another tab)
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 1);
    expect(out.panels).toEqual(saved);
    expect(out.newVersion).toBe(2);
  });

  it('returns empty when saved is null/empty', () => {
    const out = applyPanelOrderMigration(null, 1);
    expect(out.panels).toEqual([]);
    expect(out.newVersion).toBe(2);
  });

  it('migration → reconcile composition lands a complete default-order tail', () => {
    // The real wiring runs reconcile() AFTER the migration. A 9-panel
    // saved order from a v1 user should, after migration + reconcile,
    // contain every panel in DEFAULT_PANEL_ORDER (and only those).
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const migrated = applyPanelOrderMigration(saved, 1);
    const final = reconcilePanelOrder(migrated.panels, DEFAULT_PANEL_ORDER);
    expect(new Set(final)).toEqual(new Set(DEFAULT_PANEL_ORDER));
    expect(final.length).toBe(DEFAULT_PANEL_ORDER.length);
  });
});
