import { describe, it, expect } from 'vitest';
import { historyColumns, type HistoryTableRow } from './historyColumns';
import { applyTableSort } from './tableSort';

const rows: HistoryTableRow[] = [
  { key: 'a', label: 'Wk A', cost_usd: 5, used_pct: 2, dollar_per_pct: 2.5, delta_cost_pct: 10, models: [] },
  { key: 'b', label: 'Wk B', cost_usd: 20, used_pct: 8, dollar_per_pct: 2.5, delta_cost_pct: -5, models: [] },
];

describe('historyColumns', () => {
  it('weekly variant includes Used % and $/1%; monthly omits them', () => {
    const wk = historyColumns('week').map((c) => c.id);
    const mo = historyColumns('month').map((c) => c.id);
    expect(wk).toContain('used_pct');
    expect(wk).toContain('dollar_per_pct');
    expect(mo).not.toContain('used_pct');
    expect(mo).not.toContain('dollar_per_pct');
  });
  it('the delta column is labeled "Δ cost"', () => {
    expect(historyColumns('week').find((c) => c.id === 'delta_cost_pct')?.label).toBe('Δ cost');
  });
  it('the first column label is variant-aware (Week vs Month)', () => {
    expect(historyColumns('week')[0].label).toBe('Week');
    expect(historyColumns('month')[0].label).toBe('Month');
  });
  it('accepts provider-native and neutral weekly column vocabulary', () => {
    expect(historyColumns('week', false, 'Cycle')[0].label).toBe('Cycle');
    expect(historyColumns('week', true, 'Provider period')[0].label).toBe('Provider period');
  });
  it('sorts by cost descending', () => {
    const cols = historyColumns('week');
    const sorted = applyTableSort(rows, cols, { column: 'cost_usd', direction: 'desc' });
    expect(sorted.map((r) => r.key)).toEqual(['b', 'a']);
  });
  it('parks null delta rows last regardless of direction', () => {
    const cols = historyColumns('week');
    const withNull: HistoryTableRow[] = [
      { key: 'x', label: 'x', cost_usd: 1, used_pct: null, dollar_per_pct: null, delta_cost_pct: null, models: [] },
      ...rows,
    ];
    const asc = applyTableSort(withNull, cols, { column: 'delta_cost_pct', direction: 'asc' });
    expect(asc[asc.length - 1].key).toBe('x');
    const desc = applyTableSort(withNull, cols, { column: 'delta_cost_pct', direction: 'desc' });
    expect(desc[desc.length - 1].key).toBe('x');
  });
});
