import type { PanelId } from './panelIds';

// Panel-order schema migrations (spec §2.1). The localStorage cursor
// lives in `prefs.panelOrderSchemaVersion`; this module exports the
// pure migration so the loader (`loadInitial` in store/store.ts) can
// thread it before `reconcilePanelOrder` runs.
//
// Bumping schema version: add a new branch below + bump
// `CURRENT_PANEL_ORDER_SCHEMA_VERSION`. The migration runs once on
// first load and the new cursor is persisted alongside prefs.
export const CURRENT_PANEL_ORDER_SCHEMA_VERSION = 2;
// Canonical position of 'projects' in DEFAULT_PANEL_ORDER (spec §2.1).
const PROJECTS_INSERT_INDEX = 4;

export interface MigrationResult {
  panels: PanelId[];
  newVersion: number;
}

/**
 * Apply the v1→v2 migration: splice 'projects' at canonical index 4 of
 * the saved order (clamped to the end if saved is shorter). Idempotent:
 * callers on v2+ get their input back unchanged. The 'projects'
 * already-present branch covers users who manually edited their saved
 * order (or had a concurrent tab migrate them first).
 *
 * Run BEFORE `reconcilePanelOrder` — the reconcile pass relies on the
 * canonical set (`DEFAULT_PANEL_ORDER`) which now includes 'projects',
 * so without this splice a v1 user's saved order would just append
 * 'projects' at the END (lossy w.r.t. spec §2.1's canonical position).
 */
export function applyPanelOrderMigration(
  saved: PanelId[] | null | undefined,
  currentVersion: number,
): MigrationResult {
  if (currentVersion >= CURRENT_PANEL_ORDER_SCHEMA_VERSION) {
    return { panels: saved ?? [], newVersion: currentVersion };
  }
  if (!saved || saved.length === 0) {
    return { panels: [], newVersion: CURRENT_PANEL_ORDER_SCHEMA_VERSION };
  }
  if (saved.includes('projects')) {
    // Already-present branch — user (or another tab) is ahead of the
    // cursor; just advance the version. Don't re-splice or dedup.
    return { panels: saved, newVersion: CURRENT_PANEL_ORDER_SCHEMA_VERSION };
  }
  const out = [...saved];
  const insertAt = Math.min(PROJECTS_INSERT_INDEX, out.length);
  out.splice(insertAt, 0, 'projects');
  return { panels: out, newVersion: CURRENT_PANEL_ORDER_SCHEMA_VERSION };
}

/**
 * Reconcile a saved panel order with the current canonical set:
 *   1. Drop ids in `saved` that aren't in `canonical` (panel removed).
 *   2. Drop duplicates (keep first occurrence).
 *   3. Append canonical ids missing from saved at the end (panel added),
 *      preserving canonical relative order.
 *
 * If `saved` is null/empty, return a copy of canonical.
 */
export function reconcilePanelOrder(
  saved: PanelId[] | null | undefined,
  canonical: PanelId[],
): PanelId[] {
  if (!saved || !Array.isArray(saved) || saved.length === 0) return [...canonical];
  const canonicalSet = new Set(canonical);
  const seen = new Set<PanelId>();
  const filtered: PanelId[] = [];
  for (const id of saved) {
    if (!canonicalSet.has(id)) continue;
    if (seen.has(id)) continue;
    seen.add(id);
    filtered.push(id);
  }
  for (const id of canonical) {
    if (!seen.has(id)) filtered.push(id);
  }
  return filtered;
}
