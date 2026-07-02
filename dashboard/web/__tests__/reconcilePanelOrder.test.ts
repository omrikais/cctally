import { describe, it, expect } from 'vitest';
import {
  applyPanelOrderMigration,
  reconcilePanelOrder,
  CURRENT_PANEL_ORDER_SCHEMA_VERSION,
} from '../src/lib/reconcilePanelOrder';
import { DEFAULT_PANEL_ORDER } from '../src/lib/panelIds';
import type { PanelId } from '../src/lib/panelRegistry';

// Legacy ids (current-week / weekly / monthly / daily) are no longer PanelId
// members but may sit in a stale saved order; the migration must still
// handle them. Type such fixtures as string[] and downcast at the call.
const asPanelIds = (a: string[]): PanelId[] => a as unknown as PanelId[];

// A valid-current-PanelId canonical for the generic reconcile-algorithm
// tests (they exercise the pure set operations, not the S8 ids specifically).
const DEFAULT: PanelId[] = [
  'current-week', 'forecast', 'trend', 'sessions',
  'projects', 'history', 'blocks', 'alerts',
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
    const saved: PanelId[] = ['history', 'blocks', 'alerts', 'projects', 'sessions', 'trend', 'forecast', 'current-week'];
    expect(reconcilePanelOrder(saved, DEFAULT)).toEqual(saved);
  });

  it('drops unknown ids that are not in canonical', () => {
    const saved: PanelId[] = ['current-week', 'unknown' as PanelId, 'forecast', 'trend', 'sessions', 'projects', 'history', 'blocks', 'alerts'];
    expect(reconcilePanelOrder(saved, DEFAULT)).toEqual(DEFAULT);
  });

  it('appends canonical ids missing from saved at end (preserves saved relative order)', () => {
    const saved: PanelId[] = ['history', 'blocks', 'forecast'];
    const result = reconcilePanelOrder(saved, DEFAULT);
    expect(result.slice(0, 3)).toEqual(['history', 'blocks', 'forecast']);
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

describe('panel-order migration (cumulative → v4)', () => {
  it('current schema version is 4 (S8 #254)', () => {
    expect(CURRENT_PANEL_ORDER_SCHEMA_VERSION).toBe(4);
  });

  it('v3→v4 collapses weekly/monthly/daily into ONE history at its canonical index', () => {
    const OLD_V3 = [
      'forecast', 'trend', 'sessions', 'projects',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts', 'cache-report',
    ];
    const r = applyPanelOrderMigration(asPanelIds(OLD_V3), 3);
    const out = r.panels as unknown as string[];
    expect(r.newVersion).toBe(4);
    expect(out).not.toContain('weekly');
    expect(out).not.toContain('monthly');
    expect(out).not.toContain('daily');
    expect(out.filter((p) => p === 'history')).toHaveLength(1);
    // The migration preserves the saved within-class order and splices
    // 'history' at HISTORY_INSERT_INDEX (5). Since #264 S1 the bento is
    // order-independent (each card's row + span is static per id), so the
    // migration deliberately does NOT reproduce the new bento
    // DEFAULT_PANEL_ORDER — reconcile keeps this legacy permutation and the
    // board still renders every card in its correct row at its correct span.
    expect(out).toEqual([
      'forecast', 'trend', 'sessions', 'projects',
      'blocks', 'history', 'alerts', 'cache-report',
    ]);
  });

  it('does not duplicate history when a v3 order already contains it', () => {
    const r = applyPanelOrderMigration(asPanelIds(['forecast', 'history', 'trend']), 3);
    expect((r.panels as unknown as string[]).filter((p) => p === 'history')).toHaveLength(1);
  });

  it('a full v1 user is migrated: projects splice + current-week drop + history collapse', () => {
    const v1 = [
      'current-week', 'forecast', 'trend', 'sessions',
      'weekly', 'monthly', 'blocks', 'daily', 'alerts',
    ];
    const r = applyPanelOrderMigration(asPanelIds(v1), 1);
    const out = r.panels as unknown as string[];
    expect(r.newVersion).toBe(4);
    expect(out).not.toContain('current-week');
    expect(out).not.toContain('weekly');
    expect(out).not.toContain('monthly');
    expect(out).not.toContain('daily');
    expect(out).toContain('projects');
    expect(out).toContain('history');
  });

  it('is idempotent — no change when version is already 4', () => {
    const saved = [...DEFAULT_PANEL_ORDER];
    const out = applyPanelOrderMigration(saved, 4);
    expect(out.panels).toEqual(DEFAULT_PANEL_ORDER);
    expect(out.newVersion).toBe(4);
  });

  it('returns empty when saved is null/empty (cursor still advances)', () => {
    const out = applyPanelOrderMigration(null, 1);
    expect(out.panels).toEqual([]);
    expect(out.newVersion).toBe(4);
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
