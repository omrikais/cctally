import type { PeriodRow } from '../types/envelope';

// #556 S2 §5.2 — what a combined period footer may truthfully say.
//
// Under All, Weekly and Monthly both foot ONE combined cost across two
// providers' independent histories. That figure stays: it is a USD sum, which
// the anti-blend contract permits. What was missing beside it is the range it
// covers and the attribution it is made of — the Weekly footer stated a cost
// spanning twelve Claude subscription weeks plus twelve Codex native cycles
// with neither.
//
// The span is the UNION OF THE TWO DISPLAYED PROVIDER SECTIONS after their
// independent caps, not one provider's extent: the footer describes the rows
// on screen.

export interface ProviderLeg {
  source: 'claude' | 'codex';
  label: 'Claude' | 'Codex';
  cost: number;
}

export function providerLegs(rows: PeriodRow[]): ProviderLeg[] {
  return (['claude', 'codex'] as const).map((source) => ({
    source,
    label: source === 'claude' ? 'Claude' as const : 'Codex' as const,
    cost: rows
      .filter((row) => row.source === source)
      .reduce((sum, row) => sum + row.cost_usd, 0),
  }));
}

/**
 * The exact-date span of a WEEKLY set, from the per-row bounds both providers
 * already carry — Claude's `week_start_at` / `week_end_at` and Codex's
 * `start_at` / `end_at`, which `codexPeriodRow` maps onto the same two fields.
 * Rendered today only in `PeriodDetailCard`.
 *
 * Null when no displayed row carries both bounds, so a caller states no span
 * rather than half of one.
 */
export function weeklySpan(rows: PeriodRow[]): { startAt: string; endAt: string } | null {
  let start: number | null = null;
  let end: number | null = null;
  let startIso = '';
  let endIso = '';
  for (const row of rows) {
    const from = row.week_start_at == null ? NaN : Date.parse(row.week_start_at);
    const to = row.week_end_at == null ? NaN : Date.parse(row.week_end_at);
    if (!Number.isNaN(from) && (start == null || from < start)) {
      start = from;
      startIso = row.week_start_at!;
    }
    if (!Number.isNaN(to) && (end == null || to > end)) {
      end = to;
      endIso = row.week_end_at!;
    }
  }
  return start == null || end == null ? null : { startAt: startIso, endAt: endIso };
}

/**
 * The MONTH-LABEL span of a monthly set — deliberately not exact dates.
 *
 * Monthly rows carry no bounds, and the server's current-month read ends at
 * `now_utc` rather than at the end of the labelled month, so month labels
 * cannot distinguish a partial current month from a complete one. Naming exact
 * dates would assert a boundary the data does not carry. The difference from
 * `weeklySpan` is deliberate and recorded in spec §5.2 so it does not read as
 * an oversight.
 */
export function monthLabelSpan(rows: PeriodRow[]): string | null {
  const labels = rows
    .map((row) => row.label)
    .filter((label): label is string => typeof label === 'string' && label !== '')
    .sort();
  if (labels.length === 0) return null;
  const first = labels[0];
  const last = labels[labels.length - 1];
  return first === last ? first : `${first} – ${last}`;
}
