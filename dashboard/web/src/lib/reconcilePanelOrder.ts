import type { PanelId, GridPanelId } from './panelIds';

// Panel-order schema migrations (spec §2.1; #248 §3). The localStorage
// cursor lives in `prefs.panelOrderSchemaVersion`; this module exports the
// pure migration so the loader (`loadInitial` in store/store.ts) can
// thread it before `reconcilePanelOrder` runs.
//
// Bumping schema version: add a new branch below + bump
// `CURRENT_PANEL_ORDER_SCHEMA_VERSION`. The migration runs once on
// first load and the new cursor is persisted alongside prefs.
//
// Versions:
//   1 → pre-projects schema (9 default panels).
//   2 → 'projects' spliced at canonical index 4.
//   3 → (#248) 'current-week' removed from the grid (it is the HeroStrip).
//   4 → (#254 S8) 'weekly'/'monthly'/'daily' collapsed into one 'history'
//       card at its canonical index.
//   5 → (#264 S2) 'history' renamed to 'daily'; 'weekly'+'monthly' reinstated
//       as an ordered pair right after 'cache-report'.
export const CURRENT_PANEL_ORDER_SCHEMA_VERSION = 5;
// Canonical position of 'projects' in DEFAULT_PANEL_ORDER (spec §2.1).
const PROJECTS_INSERT_INDEX = 4;
// Canonical position of 'history' in DEFAULT_PANEL_ORDER (S8 #254).
const HISTORY_INSERT_INDEX = 5;
// The former period-tile ids collapsed into 'history' by the v3→v4 step.
const S8_COLLAPSED_IDS = new Set<string>(['weekly', 'monthly', 'daily']);

export interface MigrationResult {
  panels: GridPanelId[];
  newVersion: number;
}

/**
 * Apply the CUMULATIVE panel-order migration up to
 * `CURRENT_PANEL_ORDER_SCHEMA_VERSION`. Steps are applied in order so a
 * stale (v1) user is brought all the way forward in a single pass:
 *
 *   • v1→v2: splice 'projects' at canonical index 4 of the saved order
 *     (clamped to the end if saved is shorter), unless already present.
 *     Run BEFORE `reconcilePanelOrder` — the reconcile pass relies on the
 *     canonical set (`DEFAULT_PANEL_ORDER`), so without this splice a v1
 *     user's saved order would just append 'projects' at the END (lossy
 *     w.r.t. spec §2.1's canonical position).
 *   • v2→v3 (#248): drop 'current-week' — it left the grid (it is the
 *     hero now). The loader persists the cleaned order back to
 *     localStorage once, rather than re-filtering in memory every load.
 *   • v3→v4 (#254 S8): collapse 'weekly'/'monthly'/'daily' into ONE
 *     'history' card at its canonical index (not a bare append), so a saved
 *     order carrying the three legacy tiles migrates to the single card in
 *     the right position and the advanced version is written back.
 *   • v4→v5 (#264 S2): un-collapse — rename a saved 'history' back to
 *     'daily' and reinstate the ordered pair 'weekly'+'monthly' right after
 *     'cache-report' (or after 'daily' if cache-report is absent), guarded
 *     like the splices above so neither id is duplicated or reversed.
 *
 * Idempotent: callers already on CURRENT get their input back unchanged
 * (typed to GridPanelId — a current-cursor user can't carry 'current-week'
 * or a period tile).
 */
export function applyPanelOrderMigration(
  saved: PanelId[] | null | undefined,
  currentVersion: number,
): MigrationResult {
  if (currentVersion >= CURRENT_PANEL_ORDER_SCHEMA_VERSION) {
    return { panels: (saved ?? []) as GridPanelId[], newVersion: currentVersion };
  }
  // Widened to string[] internally: legacy ids (current-week, weekly,
  // monthly, daily) are no longer PanelId members but may sit in a stale
  // saved order.
  let panels: string[] = saved ? [...(saved as unknown as string[])] : [];
  // v1 → v2: splice 'projects' at its canonical index if missing.
  if (currentVersion < 2 && panels.length > 0 && !panels.includes('projects')) {
    panels.splice(Math.min(PROJECTS_INSERT_INDEX, panels.length), 0, 'projects');
  }
  // v2 → v3 (#248): drop 'current-week' (removed from the grid).
  if (currentVersion < 3) {
    panels = panels.filter((id) => id !== 'current-week');
  }
  // v3 → v4 (#254 S8): collapse weekly/monthly/daily → one 'history' at its
  // canonical index. Filter the legacy period tiles out, then splice
  // 'history' in (unless already present, e.g. a partially-migrated order).
  if (currentVersion < 4) {
    panels = panels.filter((id) => !S8_COLLAPSED_IDS.has(id));
    // Only position 'history' within a non-empty saved order (mirrors the
    // projects-splice guard). A null/empty order stays empty and the
    // reconcile backstop fills the full canonical default (history included).
    if (panels.length > 0 && !panels.includes('history')) {
      panels.splice(Math.min(HISTORY_INSERT_INDEX, panels.length), 0, 'history');
    }
  }
  // v4 → v5 (#264 S2): un-collapse — rename a saved 'history' back to 'daily'
  // and reinstate 'weekly'+'monthly' as an ORDERED PAIR right after
  // 'cache-report' (or after 'daily' if cache-report is absent). Guarded like
  // the splices above: only within a non-empty saved order, skip any id already
  // present, preserve weekly-before-monthly. reconcilePanelOrder backstops gaps.
  // (A corrupt order carrying BOTH 'history' and 'daily' would rename to two
  // 'daily' — harmless: reconcilePanelOrder dedups keep-first downstream, and a
  // legitimate v4 cursor only ever holds 'history', never 'daily'.)
  if (currentVersion < 5) {
    panels = panels.map((id) => (id === 'history' ? 'daily' : id));
    if (panels.length > 0) {
      const anchor = panels.includes('cache-report')
        ? panels.indexOf('cache-report')
        : panels.indexOf('daily');
      let insertAt = anchor >= 0 ? anchor + 1 : panels.length;
      for (const id of ['weekly', 'monthly'] as const) {
        if (!panels.includes(id)) {
          panels.splice(insertAt, 0, id);
          insertAt += 1;
        }
      }
    }
  }
  return { panels: panels as unknown as GridPanelId[], newVersion: CURRENT_PANEL_ORDER_SCHEMA_VERSION };
}

/**
 * Reconcile a saved panel order with the current canonical set:
 *   1. Drop ids in `saved` that aren't in `canonical` (panel removed).
 *   2. Drop duplicates (keep first occurrence).
 *   3. Append canonical ids missing from saved at the end (panel added),
 *      preserving canonical relative order.
 *
 * If `saved` is null/empty, return a copy of canonical. Generic over the
 * id type so the runtime backstop preserves the caller's narrowing —
 * `DEFAULT_PANEL_ORDER` is `GridPanelId[]`, so the store gets
 * `GridPanelId[]` back (and any 'current-week' in a stale saved order is
 * dropped here too, since it isn't in the canonical grid set).
 */
export function reconcilePanelOrder<T extends PanelId>(
  saved: readonly T[] | null | undefined,
  canonical: readonly T[],
): T[] {
  if (!saved || !Array.isArray(saved) || saved.length === 0) return [...canonical];
  const canonicalSet = new Set<T>(canonical);
  const seen = new Set<T>();
  const filtered: T[] = [];
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
