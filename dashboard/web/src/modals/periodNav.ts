import type { PeriodRow, DailyPanelRow } from '../types/envelope';

export type PeriodVariant = 'day' | 'week' | 'month';

/** Stable per-period selection key. day→date, week→week_start_at, month→label
 *  (monthly rows carry no start field — Codex finding 4). The week fallback
 *  `?? label` guards a null/absent week_start_at. */
export function keyOf(row: DailyPanelRow | PeriodRow, variant: PeriodVariant): string {
  if (variant === 'day') return (row as DailyPanelRow).date;
  if (variant === 'week') return (row as PeriodRow).week_start_at ?? (row as PeriodRow).label;
  return (row as PeriodRow).label;
}

/** rows are newest-first. 'older' → next index, 'newer' → prev index.
 *  Returns null at either boundary, unknown key, or null current.
 *  Deliberately does NOT skip zero-cost periods (parity with the old stepDay,
 *  which mirrored the ↑/↓ keymap — only bar CLICKS skip zero-cost periods). */
export function stepPeriod<T extends { key: string }>(
  rows: T[], currentKey: string | null, dir: 'older' | 'newer',
): string | null {
  if (currentKey == null) return null;
  const idx = rows.findIndex((r) => r.key === currentKey);
  if (idx < 0) return null;
  const next = dir === 'older' ? idx + 1 : idx - 1;
  if (next < 0 || next >= rows.length) return null;
  return rows[next].key;
}
