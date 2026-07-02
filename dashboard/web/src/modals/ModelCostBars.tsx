// ModelCostBars — per-model horizontal cost bars, extracted verbatim
// from the inline block in ProjectsDrillPanel so both the Projects drill
// and the History detail card share ONE recipe (S8, issue #254). Each
// row renders a `modelChipClass` chip, a `.drill-bar` whose width is the
// model's cost relative to the top model (via the `--w` CSS custom
// property), and a `costClass`-tinted `fmt.usd2` cost.
//
// Callers MUST pass rows in cost-DESCENDING order — `rows[0]` is assumed
// to be the max, matching the drill's data-layer sort. The CSS classes
// (`.drill-bar-row` / `.drill-bar` / `.chip` / `.cost`) are byte-identical
// to the former inline block so `index.css`'s `.drill-bar*` rules apply
// unchanged (the chip cell is a fixed 110px column).
//
// `label` is the friendly chip TEXT: the Projects drill (whose row type
// carries only the canonical `model` id) omits it and falls back to the
// raw id, matching its prior behavior; PeriodDetailCard passes the
// server's chip-friendly `display` (e.g. "opus-4-5") so a dated canonical
// id like "claude-opus-4-5-20251101" doesn't overflow the 110px chip
// column and stays consistent with the table's short family key. The chip
// COLOR always derives from `modelChipClass(model)` (drill parity + the
// #244 six-surface classifier convention), independent of `label`.
import type { CSSProperties } from 'react';
import { fmt } from '../lib/fmt';
import { modelChipClass } from '../lib/model';
import { costClass } from '../lib/cost';

export interface ModelCostBarRow {
  model: string;
  cost_usd: number;
  label?: string;
}

export function ModelCostBars({ rows }: { rows: ModelCostBarRow[] }) {
  const topModelCost = rows[0]?.cost_usd ?? 0;
  const denom = topModelCost > 0 ? topModelCost : 1;
  return (
    <>
      {rows.map((m) => {
        const widthPct = (m.cost_usd / denom) * 100;
        const style = { '--w': `${widthPct}%` } as CSSProperties;
        return (
          <div className="drill-bar-row" key={m.model}>
            <span className={`chip ${modelChipClass(m.model)}`}>{m.label ?? m.model}</span>
            <div className="drill-bar" style={style} />
            <span className={`cost ${costClass(m.cost_usd)}`}>{fmt.usd2(m.cost_usd)}</span>
          </div>
        );
      })}
    </>
  );
}
