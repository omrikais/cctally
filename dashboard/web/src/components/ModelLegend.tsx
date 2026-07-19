import { modelLegend, type LegendInput } from '../lib/modelLegend';
import { modelChipStyle } from '../lib/model';

// C6 (#249): a compact legend rendered BENEATH each panel's existing
// model-split bar (the bars are untouched — Blocks is a cost-scaled gauge,
// not a .model-stack). Shows the leading two models (dot + short name +
// rounded %) and a "+N" overflow. The percent is rounded to match the bar
// segments' own title tooltip (`cost_pct.toFixed(0)%`).
export function ModelLegend({ models }: { models: LegendInput[] }) {
  const { items, more } = modelLegend(models);
  if (items.length === 0) return null;
  return (
    <div className="model-legend" role="presentation">
      {items.map((it) => (
        <span className="ms-leg" key={it.model}>
          <span className={`ms-dot ${it.chip}`} style={modelChipStyle(it.model)} aria-hidden="true" />
          {it.display} {Math.round(it.pct)}%
        </span>
      ))}
      {more > 0 && <span className="ms-leg ms-more">+{more}</span>}
    </div>
  );
}
