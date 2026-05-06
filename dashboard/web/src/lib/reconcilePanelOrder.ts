import type { PanelId } from './panelIds';

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
