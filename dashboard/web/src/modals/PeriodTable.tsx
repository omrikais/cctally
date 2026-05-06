import { fmt } from '../lib/fmt';
import type { PeriodRow } from '../types/envelope';

interface Props {
  rows: PeriodRow[];
  variant: 'weekly' | 'monthly';
  accentClass: 'accent-cyan' | 'accent-pink';
  selectedIndex: number;
  onSelect: (i: number) => void;
}

function deltaCellCls(d: number | null): string {
  if (d == null || d === 0) return 'num';
  return d > 0 ? 'num delta-up' : 'num delta-down';
}

// Dedup by chip family so a row with `opus-4-7` + `opus-4-6` shows ONE
// `opus` chip, not two. Order is preserved (cost-desc upstream).
function uniqueChipKeys(row: PeriodRow): string[] {
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const m of row.models) {
    if (!seen.has(m.chip)) {
      seen.add(m.chip);
      keys.push(m.chip);
    }
  }
  return keys;
}

function ModelsCell({ row }: { row: PeriodRow }) {
  const keys = uniqueChipKeys(row);
  const top = keys.slice(0, 3);
  const extra = keys.length > 3 ? keys.length - 3 : 0;
  return (
    <span className="models-chips">
      {top.map((k) => (
        <span key={k} className={`chip ${k}`}>{k}</span>
      ))}
      {extra > 0 && <span className="models-chips-more">…+{extra}</span>}
    </span>
  );
}

export function PeriodTable({ rows, variant, accentClass, selectedIndex, onSelect }: Props) {
  return (
    <table
      className={`history-table history-table--${variant} ${accentClass}`}
      role="grid"
      aria-rowcount={rows.length}
    >
      <thead>
        <tr>
          <th>{variant === 'weekly' ? 'Week' : 'Month'}</th>
          <th>Models</th>
          <th className="num">Cost (USD)</th>
          {variant === 'weekly' && <th className="num">Used %</th>}
          {variant === 'weekly' && <th className="num">$/1%</th>}
          <th className="num">Δ</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr
            key={r.label}
            className={i === selectedIndex ? 'selected' : undefined}
            aria-rowindex={i + 1}
            aria-selected={i === selectedIndex}
            onClick={() => onSelect(i)}
          >
            <td>{r.label}{i === selectedIndex ? ' ▶' : ''}</td>
            <td><ModelsCell row={r} /></td>
            <td className="num">{fmt.usd2(r.cost_usd)}</td>
            {variant === 'weekly' && <td className="num">{fmt.pct0(r.used_pct)}</td>}
            {variant === 'weekly' && <td className="num">{fmt.usd2(r.dollar_per_pct)}</td>}
            <td className={deltaCellCls(r.delta_cost_pct)}>{fmt.deltaPct(r.delta_cost_pct)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
