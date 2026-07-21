// Pure data adapters for the period modals (S8 #254; #264 S2 split the History
// modal into the shared PeriodModal). Extracted so PeriodModal and PeriodTable
// share ONE decoration recipe (their ordered key lists must agree — the ↑/↓
// keymap steps over the same sorted order the table renders).
import type { DailyPanelRow, PeriodRow } from '../types/envelope';
import type { HistoryTableRow, HistoryVariant } from '../lib/historyColumns';
import { keyOf } from './periodNav';

/**
 * Adapt a DailyPanelRow into a PeriodRow shape for the detail card.
 * Computes Δ% vs the prior (older) day inline. used_pct / dollar_per_pct
 * stay null (the detail card gates that stats row to the weekly variant).
 * Moved here from the former Daily modal (removed in S8 Milestone B).
 */
export function dailyToPeriodRow(row: DailyPanelRow, prior?: DailyPanelRow): PeriodRow {
  const delta =
    prior && prior.cost_usd > 0
      ? (row.cost_usd - prior.cost_usd) / prior.cost_usd
      : null;
  return {
    source: row.source,
    label: row.label,
    cost_usd: row.cost_usd,
    total_tokens: row.total_tokens,
    input_tokens: row.input_tokens,
    output_tokens: row.output_tokens,
    cache_creation_tokens: row.cache_creation_tokens,
    cache_read_tokens: row.cache_read_tokens,
    used_pct: null,
    dollar_per_pct: null,
    delta_cost_pct: delta,
    is_current: row.is_today,
    models: row.models,
    cache_hit_pct: row.cache_hit_pct,
    codex_tokens: row.codex_tokens,
  };
}

/**
 * Decorate PeriodRow[] → HistoryTableRow[] (keyed via keyOf) for the
 * sortable Weekly/Monthly table. A null sort override leaves rows in
 * envelope order. Both PeriodTable (render) and PeriodModal (the ↑/↓
 * ordered key list) route through this so the visible order and the
 * keyboard-step order can never drift.
 */
export function decorateHistoryRows(rows: PeriodRow[], variant: HistoryVariant): HistoryTableRow[] {
  return rows.map((r) => ({
    key: keyOf(r, variant),
    source: r.source,
    label: r.label,
    cost_usd: r.cost_usd,
    used_pct: r.used_pct,
    dollar_per_pct: r.dollar_per_pct,
    delta_cost_pct: r.delta_cost_pct,
    models: r.models,
  }));
}
