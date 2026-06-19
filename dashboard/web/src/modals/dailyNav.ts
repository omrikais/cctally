import type { DailyPanelRow } from '../types/envelope';

/**
 * Step the selected day over the Daily modal's `rows` (which arrive
 * NEWEST-FIRST from the envelope). 'older' moves one day back (idx+1),
 * 'newer' moves one day forward (idx-1) — exactly the index math the
 * DailyModal ↑↓ keymap used inline. Returns the target date, or null at
 * the boundary / when `currentDate` is absent or not present in `rows`.
 *
 * Mirrors ↑↓ deliberately: it does NOT skip zero-cost days (only bar
 * CLICKS skip zero, via the disabled bars in DailyMiniBars). See the M2
 * spec for why this nav-vs-click asymmetry is intentional + pre-existing.
 */
export function stepDay(
  rows: DailyPanelRow[],
  currentDate: string | null,
  dir: 'older' | 'newer',
): string | null {
  if (!currentDate) return null;
  const idx = rows.findIndex((r) => r.date === currentDate);
  if (idx < 0) return null;
  if (dir === 'older') {
    return idx >= rows.length - 1 ? null : rows[idx + 1].date;
  }
  return idx <= 0 ? null : rows[idx - 1].date;
}
