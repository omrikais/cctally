import { describe, it, expect } from 'vitest';
import {
  applyPanelOrderMigration,
  reconcilePanelOrder,
  CURRENT_PANEL_ORDER_SCHEMA_VERSION,
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

// ---- Tests for the cumulative panel-order migration (#248 §3, spec §2.1) ----
//
// `applyPanelOrderMigration` is CUMULATIVE: v1→v2 splices 'projects' at
// canonical index 4, then v2→v3 (#248) drops 'current-week' (it left the
// grid). The store loader runs this BEFORE `reconcilePanelOrder` so the
// splice lands at the canonical position instead of being shoved to the
// tail by the reconcile "append missing" pass.

describe('panel-order migration (cumulative → v3)', () => {
  it('current schema version is 3 (#248)', () => {
    expect(CURRENT_PANEL_ORDER_SCHEMA_VERSION).toBe(3);
  });

  it('v3 migration drops current-week from a saved order (#248)', () => {
    const r = applyPanelOrderMigration(
      ['current-week', 'forecast', 'projects', 'weekly'] as PanelId[], 2);
    expect(r.panels).not.toContain('current-week');
    expect(r.newVersion).toBe(3);
  });

  it('v1 user is migrated through projects-splice AND current-week drop (#248)', () => {
    const r = applyPanelOrderMigration(
      ['current-week', 'forecast', 'trend', 'sessions', 'weekly'] as PanelId[], 1);
    expect(r.panels).not.toContain('current-week');
    expect(r.panels).toContain('projects');
    expect(r.newVersion).toBe(3);
  });

  it('splices projects at canonical index 4 AND drops current-week for v1 users', () => {
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 1);
    // projects spliced at index 4 of the saved order, THEN current-week dropped.
    expect(out.panels).toEqual([
      'forecast', 'trend', 'sessions',
      'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
    expect(out.newVersion).toBe(3);
  });

  it('is idempotent — no change when version is already 3', () => {
    const saved = [...DEFAULT_PANEL_ORDER];  // has projects, no current-week
    const out = applyPanelOrderMigration(saved, 3);
    expect(out.panels).toEqual(DEFAULT_PANEL_ORDER);
    expect(out.newVersion).toBe(3);
  });

  it('a v2 user (projects present, current-week present) only drops current-week', () => {
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 2);
    expect(out.panels).toEqual([
      'forecast', 'trend', 'sessions',
      'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
    expect(out.newVersion).toBe(3);
  });

  it('handles saved order with custom user reordering', () => {
    const saved: PanelId[] = [
      'weekly', 'current-week', 'sessions',  // user reorder
      'forecast', 'trend',
      'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 1);
    // 'projects' inserted at index 4 of the saved order (between 'trend' and
    // 'monthly'), THEN 'current-week' dropped.
    expect(out.panels).toEqual([
      'weekly', 'sessions', 'forecast',
      'projects',  // index 4 of the pre-drop array
      'trend', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
    expect(out.newVersion).toBe(3);
  });

  it('appends projects if saved order is shorter than 4 (and drops current-week)', () => {
    const saved: PanelId[] = ['current-week', 'forecast', 'trend'];  // 3 panels
    const out = applyPanelOrderMigration(saved, 1);
    expect(out.panels).toContain('projects');
    expect(out.panels).not.toContain('current-week');
    expect(out.newVersion).toBe(3);
  });

  it('passes through projects when already present, dropping current-week', () => {
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'projects',  // user manually added (or migrated by another tab)
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const out = applyPanelOrderMigration(saved, 1);
    expect(out.panels).toEqual([
      'forecast', 'trend', 'sessions',
      'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ]);
    expect(out.newVersion).toBe(3);
  });

  it('returns empty when saved is null/empty', () => {
    const out = applyPanelOrderMigration(null, 1);
    expect(out.panels).toEqual([]);
    expect(out.newVersion).toBe(3);
  });

  it('reconcilePanelOrder backstop drops current-week against the grid canonical', () => {
    // Even if a stale order slips past the migration (cursor already ≥3 but
    // the array still has current-week), the runtime reconcile against the
    // grid-only DEFAULT_PANEL_ORDER drops it.
    const final = reconcilePanelOrder(
      ['current-week', 'forecast', 'trend'] as PanelId[],
      DEFAULT_PANEL_ORDER as PanelId[],
    );
    expect(final).not.toContain('current-week');
  });

  it('migration → reconcile composition lands a complete default-order tail', () => {
    // The real wiring runs reconcile() AFTER the migration. A 9-panel
    // saved order from a v1 user should, after migration + reconcile,
    // contain every panel in DEFAULT_PANEL_ORDER (and only those) — and
    // never 'current-week'.
    const saved: PanelId[] = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const migrated = applyPanelOrderMigration(saved, 1);
    const final = reconcilePanelOrder(migrated.panels, DEFAULT_PANEL_ORDER);
    expect(final).not.toContain('current-week');
    expect(new Set(final)).toEqual(new Set(DEFAULT_PANEL_ORDER));
    expect(final.length).toBe(DEFAULT_PANEL_ORDER.length);
  });
});
