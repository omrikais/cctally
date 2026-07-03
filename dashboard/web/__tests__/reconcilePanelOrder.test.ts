import { describe, it, expect } from 'vitest';
import {
  applyPanelOrderMigration,
  reconcilePanelOrder,
  CURRENT_PANEL_ORDER_SCHEMA_VERSION,
} from '../src/lib/reconcilePanelOrder';
import { DEFAULT_PANEL_ORDER, CARD_LAYOUT } from '../src/lib/panelIds';
import type { PanelId } from '../src/lib/panelRegistry';

// Legacy ids ('history' after the #264 S2 un-collapse; 'current-week' after
// #248) may still sit in a stale saved order; the migration must handle them.
// Type such fixtures as string[] and downcast at the call. The generic
// reconcile tests below use 'daily' (a current PanelId) as a representative id.
const asPanelIds = (a: string[]): PanelId[] => a as unknown as PanelId[];

// A valid-current-PanelId canonical for the generic reconcile-algorithm
// tests (they exercise the pure set operations, not the S8 ids specifically).
const DEFAULT: PanelId[] = [
  'current-week', 'forecast', 'trend', 'sessions',
  'projects', 'daily', 'blocks', 'alerts',
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
    const saved: PanelId[] = ['daily', 'blocks', 'alerts', 'projects', 'sessions', 'trend', 'forecast', 'current-week'];
    expect(reconcilePanelOrder(saved, DEFAULT)).toEqual(saved);
  });

  it('drops unknown ids that are not in canonical', () => {
    const saved: PanelId[] = ['current-week', 'unknown' as PanelId, 'forecast', 'trend', 'sessions', 'projects', 'daily', 'blocks', 'alerts'];
    expect(reconcilePanelOrder(saved, DEFAULT)).toEqual(DEFAULT);
  });

  it('appends canonical ids missing from saved at end (preserves saved relative order)', () => {
    const saved: PanelId[] = ['daily', 'blocks', 'forecast'];
    const result = reconcilePanelOrder(saved, DEFAULT);
    expect(result.slice(0, 3)).toEqual(['daily', 'blocks', 'forecast']);
    expect(result.slice(3)).toEqual(['current-week', 'trend', 'sessions', 'projects', 'alerts']);
  });

  it('deduplicates a saved order that contains the same id twice', () => {
    const saved: PanelId[] = ['forecast', 'forecast', 'trend'];
    const result = reconcilePanelOrder(saved, DEFAULT);
    expect(result.slice(0, 2)).toEqual(['forecast', 'trend']);
    expect(result).toHaveLength(DEFAULT.length);
    expect([...result].sort()).toEqual([...DEFAULT].sort());
  });

  it('returns default when saved is not an array (malformed localStorage)', () => {
    // @ts-expect-error - the function must handle bad runtime input safely
    expect(reconcilePanelOrder(42, DEFAULT)).toEqual(DEFAULT);
    // @ts-expect-error
    expect(reconcilePanelOrder({ 0: 'forecast' }, DEFAULT)).toEqual(DEFAULT);
  });
});

// ---- Cumulative panel-order migration (#248 §3 + S8 #254) ----
//
// `applyPanelOrderMigration` is CUMULATIVE: v1→v2 splices 'projects' at
// canonical index 4, v2→v3 (#248) drops 'current-week', and v3→v4 (#254)
// collapses 'weekly'/'monthly'/'daily' into ONE 'history' card at its
// canonical index. The store loader runs this BEFORE `reconcilePanelOrder`.

describe('panel-order migration (cumulative → v6)', () => {
  it('current schema version is 6 (#266 Blocks promotion)', () => {
    expect(CURRENT_PANEL_ORDER_SCHEMA_VERSION).toBe(6);
  });

  it('a v3 order flows through v3→v4 collapse, v4→v5 un-collapse, v5→v6 reseat', () => {
    const OLD_V3 = [
      'forecast', 'trend', 'sessions', 'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts', 'cache-report',
    ];
    const r = applyPanelOrderMigration(asPanelIds(OLD_V3), 3);
    const out = r.panels as unknown as string[];
    expect(r.newVersion).toBe(6);
    // v3→v4 collapses weekly/monthly/daily into 'history' at index 5; v4→v5
    // renames it to 'daily' and re-adds the weekly/monthly pair after
    // cache-report (which sits at the tail of this legacy permutation); v5→v6
    // (#266) reseats 'blocks' immediately before 'forecast' (here at index 0).
    expect(out).not.toContain('history');
    expect(out.filter((p) => p === 'daily')).toHaveLength(1);
    expect(out.filter((p) => p === 'blocks')).toHaveLength(1);
    expect(out).toEqual([
      'blocks', 'forecast', 'trend', 'sessions', 'projects',
      'daily', 'alerts', 'cache-report', 'weekly', 'monthly',
    ]);
  });

  it('does not duplicate daily when a v3 order already contains history', () => {
    const r = applyPanelOrderMigration(asPanelIds(['forecast', 'history', 'trend']), 3);
    const out = r.panels as unknown as string[];
    expect(out.filter((p) => p === 'daily')).toHaveLength(1);
    expect(out).not.toContain('history');
    // weekly/monthly reinstated right after the renamed daily.
    expect(out).toEqual(['forecast', 'daily', 'weekly', 'monthly', 'trend']);
  });

  it('a full v1 user is migrated: projects splice + current-week drop + history un-collapse', () => {
    const v1 = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const r = applyPanelOrderMigration(asPanelIds(v1), 1);
    const out = r.panels as unknown as string[];
    expect(r.newVersion).toBe(6);
    expect(out).not.toContain('current-week');
    expect(out).not.toContain('history');
    expect(out).toContain('projects');
    expect(out).toContain('daily');
    expect(out).toContain('weekly');
    expect(out).toContain('monthly');
  });

  it('is idempotent — no change when version is already 6', () => {
    const saved = [...DEFAULT_PANEL_ORDER];
    const out = applyPanelOrderMigration(saved, 6);
    expect(out.panels).toEqual(DEFAULT_PANEL_ORDER);
    expect(out.newVersion).toBe(6);
  });

  it('returns empty when saved is null/empty (cursor still advances)', () => {
    const out = applyPanelOrderMigration(null, 1);
    expect(out.panels).toEqual([]);
    expect(out.newVersion).toBe(6);
  });

  it('migration → reconcile composition lands a complete default-order set', () => {
    const saved = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const migrated = applyPanelOrderMigration(asPanelIds(saved), 1);
    const final = reconcilePanelOrder(migrated.panels, DEFAULT_PANEL_ORDER);
    expect(final).not.toContain('current-week' as never);
    expect(new Set(final)).toEqual(new Set(DEFAULT_PANEL_ORDER));
    expect(final.length).toBe(DEFAULT_PANEL_ORDER.length);
  });
});

describe('v4→v5 period un-collapse (#264 S2)', () => {
  it('CURRENT_PANEL_ORDER_SCHEMA_VERSION is 6', () => {
    expect(CURRENT_PANEL_ORDER_SCHEMA_VERSION).toBe(6);
  });

  it('renames a saved history→daily, inserts weekly,monthly after cache-report, reseats blocks before forecast', () => {
    const saved = ['sessions', 'trend', 'projects', 'history', 'cache-report', 'forecast', 'blocks', 'alerts'];
    const { panels, newVersion } = applyPanelOrderMigration(saved as any, 4);
    expect(newVersion).toBe(6);
    // v5→v6 (#266) additionally reseats blocks left of forecast — the result is
    // the canonical v6 DEFAULT_PANEL_ORDER.
    expect(panels).toEqual([
      'sessions', 'trend', 'projects',
      'daily', 'cache-report', 'weekly', 'monthly', 'blocks', 'forecast',
      'alerts',
    ]);
  });

  it('inserts after daily when cache-report is absent', () => {
    const { panels } = applyPanelOrderMigration(['history', 'forecast'] as any, 4);
    expect(panels).toEqual(['daily', 'weekly', 'monthly', 'forecast']);
  });

  it('does not duplicate weekly/monthly already present, keeps weekly before monthly', () => {
    const { panels } = applyPanelOrderMigration(['daily', 'cache-report', 'monthly'] as any, 4);
    // weekly missing → inserted after cache-report; monthly already present → not re-added
    expect(panels.filter((p) => p === 'monthly')).toHaveLength(1);
    expect(panels.filter((p) => p === 'weekly')).toHaveLength(1);
    expect(panels.indexOf('weekly')).toBeLessThan(panels.indexOf('monthly'));
  });

  it('migrates a stale v3 order (weekly/monthly/daily tiles) all the way to v5', () => {
    const saved = ['sessions', 'weekly', 'monthly', 'daily', 'cache-report'];
    const { panels, newVersion } = applyPanelOrderMigration(saved as any, 3);
    expect(newVersion).toBe(6);
    // v3→v4 collapses the three tiles into 'history' at index 5 (clamped), v4→v5
    // renames it to 'daily' and re-adds weekly/monthly after cache-report.
    expect(panels).toContain('daily');
    expect(panels).toContain('weekly');
    expect(panels).toContain('monthly');
    expect(panels).not.toContain('history');
  });

  it('advances a v5 order to cursor 6 without reordering (no blocks/forecast pair)', () => {
    const saved = ['daily', 'cache-report', 'weekly', 'monthly'];
    const { panels, newVersion } = applyPanelOrderMigration(saved as any, 5);
    expect(newVersion).toBe(6);
    // No blocks+forecast pair present → the v5→v6 reseat is a no-op.
    expect(panels).toEqual(saved);
  });
});

// ---- v5→v6 Blocks promotion (#266) ----
describe('v5→v6 Blocks promotion (#266)', () => {
  it('reseats blocks immediately before forecast for a v5 user (pure move)', () => {
    // The #264 (v5) default: blocks + forecast still in the short tier, forecast first.
    const v5 = [
      'sessions', 'trend', 'projects',
      'daily', 'cache-report', 'weekly', 'monthly',
      'forecast', 'blocks', 'alerts',
    ];
    const { panels, newVersion } = applyPanelOrderMigration(v5 as any, 5);
    expect(newVersion).toBe(6);
    expect(panels).toEqual([
      'sessions', 'trend', 'projects',
      'daily', 'cache-report', 'weekly', 'monthly', 'blocks', 'forecast',
      'alerts',
    ]);
    expect((panels as unknown as string[]).filter((p) => p === 'blocks')).toHaveLength(1);
  });

  it('is a no-op reseat when forecast is absent (still advances the cursor)', () => {
    const saved = ['sessions', 'daily', 'blocks', 'alerts'];
    const { panels, newVersion } = applyPanelOrderMigration(saved as any, 5);
    expect(newVersion).toBe(6);
    expect(panels).toEqual(saved);
  });

  it('lands blocks left of forecast in the medium row after reconcile', () => {
    const v5 = [
      'sessions', 'trend', 'projects',
      'daily', 'cache-report', 'weekly', 'monthly',
      'forecast', 'blocks', 'alerts',
    ];
    const { panels } = applyPanelOrderMigration(v5 as any, 5);
    const reconciled = reconcilePanelOrder(panels, DEFAULT_PANEL_ORDER);
    const medium = reconciled.filter((id) => CARD_LAYOUT[id].row === 'medium');
    expect(medium.indexOf('blocks')).toBeLessThan(medium.indexOf('forecast'));
  });
});
