import { useSyncExternalStore } from 'react';
import { fmt } from '../lib/fmt';
import { SortableHeader } from '../components/SortableHeader';
import { applyTableSort } from '../lib/tableSort';
import { historyColumns } from '../lib/historyColumns';
import { decorateHistoryRows } from './historyData';
import { dispatch, getState, subscribeStore } from '../store/store';
import type { ModelCostRow, PeriodRow } from '../types/envelope';

interface Props {
  rows: PeriodRow[];
  variant: 'weekly' | 'monthly';
  // #264 S2: the Weekly modal passes cyan, Monthly pink (Daily renders no
  // table). 'accent-indigo' stays in the union but is unused on the table now.
  accentClass: 'accent-cyan' | 'accent-pink' | 'accent-indigo';
  // WM-1 / S8: selection is key-based (keyOf) so it survives header
  // re-sorting and SSE churn; the row order in the DOM is the sorted
  // order, not the envelope order.
  selectedKey: string | null;
  onSelect: (key: string) => void;
  showSource?: boolean;
  periodLabel?: string;
}

function deltaCellCls(d: number | null): string {
  if (d == null || d === 0) return 'num';
  return d > 0 ? 'num delta-up' : 'num delta-down';
}

interface ModelChip {
  key: string;
  cls: string;
  label: string;
}

// Known Claude families remain deduped (`opus-4-7` + `opus-4-6` → one Opus
// chip). Provider models in the neutral `other` class retain their own model
// identity, otherwise every Codex split collapses into a useless "other".
function uniqueModelChips(models: ModelCostRow[]): ModelChip[] {
  const seen = new Set<string>();
  const chips: ModelChip[] = [];
  for (const m of models) {
    const key = m.chip === 'other' ? `other:${m.model}` : m.chip;
    if (!seen.has(key)) {
      seen.add(key);
      chips.push({ key, cls: m.chip, label: m.chip === 'other' ? m.display : m.chip });
    }
  }
  return chips;
}

function ModelsCell({ models }: { models: ModelCostRow[] }) {
  const chips = uniqueModelChips(models);
  const top = chips.slice(0, 3);
  const extra = chips.length > 3 ? chips.length - 3 : 0;
  return (
    <span className="models-chips">
      {top.map((chip) => (
        <span key={chip.key} className={`chip ${chip.cls}`}>{chip.label}</span>
      ))}
      {extra > 0 && <span className="models-chips-more">…+{extra}</span>}
    </span>
  );
}

export function PeriodTable({
  rows, variant, accentClass, selectedKey, onSelect, showSource = false, periodLabel,
}: Props) {
  const hv = variant === 'weekly' ? 'week' : 'month';
  const columns = historyColumns(hv, showSource, periodLabel);
  const sortOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.historySortOverride,
  );

  // Decorate PeriodRow[] → HistoryTableRow[] (keyed via keyOf), then apply
  // the persisted sort. A null override leaves rows in envelope order
  // (today's default) — applyTableSort returns the input unchanged. Shared
  // with PeriodModal via decorateHistoryRows so the ↑/↓ ordered key list
  // and the rendered row order never drift.
  const decorated = decorateHistoryRows(rows, hv);
  const sorted = applyTableSort(decorated, columns, sortOverride);

  return (
    <table
      className={`history-table history-table--${variant} ${accentClass}`}
      role="grid"
      aria-rowcount={sorted.length}
    >
      <SortableHeader
        columns={columns}
        override={sortOverride}
        onChange={(next) =>
          dispatch({ type: 'SET_TABLE_SORT', table: 'history', override: next })
        }
        accentVar={`--${accentClass}`}
      />
      <tbody>
        {sorted.map((r, i) => {
          const isSelected = r.key === selectedKey;
          return (
            <tr
              key={r.key}
              className={isSelected ? 'selected' : undefined}
              aria-rowindex={i + 1}
              aria-selected={isSelected}
              tabIndex={0}
              onClick={() => onSelect(r.key)}
              onKeyDown={(e) => {
                // SH-3: keep the native table `row` role but make the row
                // operable — Enter/Space selects, same as a click.
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  e.stopPropagation();
                  onSelect(r.key);
                }
              }}
            >
              <td>{r.label}{isSelected ? ' ▶' : ''}</td>
              {showSource && (
                <td>
                  <span className={`source-chip source-chip--${r.source ?? 'all'}`}>
                    {r.source === 'claude' ? 'Claude' : r.source === 'codex' ? 'Codex' : 'Combined'}
                  </span>
                </td>
              )}
              <td><ModelsCell models={r.models} /></td>
              <td className="num">{fmt.usd2(r.cost_usd)}</td>
              {variant === 'weekly' && <td className="num">{fmt.pct0(r.used_pct)}</td>}
              {variant === 'weekly' && <td className="num">{fmt.usd2(r.dollar_per_pct)}</td>}
              <td className={deltaCellCls(r.delta_cost_pct)}>{fmt.deltaPct(r.delta_cost_pct)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
