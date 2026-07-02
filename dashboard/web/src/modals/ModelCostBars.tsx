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
// unchanged.
import type { CSSProperties } from 'react';
import { fmt } from '../lib/fmt';
import { modelChipClass } from '../lib/model';
import { costClass } from '../lib/cost';

export interface ModelCostBarRow {
  model: string;
  cost_usd: number;
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
            <span className={`chip ${modelChipClass(m.model)}`}>{m.model}</span>
            <div className="drill-bar" style={style} />
            <span className={`cost ${costClass(m.cost_usd)}`}>{fmt.usd2(m.cost_usd)}</span>
          </div>
        );
      })}
    </>
  );
}
