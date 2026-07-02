import type { TableColumn } from './tableSort';
import type { ModelCostRow } from '../types/envelope';

// Decorated row type for the History (Weekly/Monthly) sortable table.
// Mirrors the fields PeriodTable renders — reusing PeriodRow's field
// names — plus a stable `key` (from keyOf) so selection survives sorting.
// Analogous to lib/projectsColumns.ts. Weekly-only metrics (used_pct /
// dollar_per_pct) are null for monthly rows.
export interface HistoryTableRow {
  key: string;
  label: string;
  cost_usd: number;
  used_pct: number | null;
  dollar_per_pct: number | null;
  delta_cost_pct: number | null;
  models: ModelCostRow[];
}

export type HistoryVariant = 'week' | 'month';

// Null-last comparators park null rows at the END regardless of asc/desc
// (via TableColumn.nullKey — see lib/tableSort.ts), so `compare` only ever
// runs on two non-null rows and can safely `!`-assert.
const cmpStr = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

// Column set mirrors the current PeriodTable header, variant-aware:
//   <variant label> · Models · Cost (USD) · [Used % · $/1%] · Δ cost
// The label column sorts by label (text asc); Models is a stable no-op
// sort (the header stays clickable via SortableHeader, which ignores the
// `sortable` flag, but rows keep envelope order). Monthly drops the two
// weekly-only percent columns (WM-1: Monthly correctly omits Used%/$/1%).
export function historyColumns(variant: HistoryVariant): TableColumn<HistoryTableRow>[] {
  const cols: TableColumn<HistoryTableRow>[] = [
    {
      id: 'label',
      label: variant === 'week' ? 'Week' : 'Month',
      defaultDirection: 'asc',
      compare: (a, b) => cmpStr(a.label, b.label),
    },
    {
      id: 'models',
      label: 'Models',
      defaultDirection: 'desc',
      // No meaningful ordering for the chip cluster — keep rows stable.
      compare: () => 0,
    },
    {
      id: 'cost_usd',
      label: 'Cost (USD)',
      defaultDirection: 'desc',
      numeric: true,
      compare: (a, b) => a.cost_usd - b.cost_usd,
    },
  ];
  if (variant === 'week') {
    cols.push(
      {
        id: 'used_pct',
        label: 'Used %',
        defaultDirection: 'desc',
        numeric: true,
        nullKey: (r) => r.used_pct,
        compare: (a, b) => a.used_pct! - b.used_pct!,
      },
      {
        id: 'dollar_per_pct',
        label: '$/1%',
        defaultDirection: 'desc',
        numeric: true,
        nullKey: (r) => r.dollar_per_pct,
        compare: (a, b) => a.dollar_per_pct! - b.dollar_per_pct!,
      },
    );
  }
  cols.push({
    id: 'delta_cost_pct',
    label: 'Δ cost',
    defaultDirection: 'desc',
    numeric: true,
    nullKey: (r) => r.delta_cost_pct,
    compare: (a, b) => a.delta_cost_pct! - b.delta_cost_pct!,
  });
  return cols;
}
