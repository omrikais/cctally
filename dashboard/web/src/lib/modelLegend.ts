// C6 (#249): the inline model-split legend model. Takes the same per-model
// share array each panel already passes to its bar (envelope orders it by
// share) and returns the leading two items + an overflow count. Pure.
export interface LegendInput { model: string; display: string; chip: string; cost_pct: number }
export interface LegendItem { model: string; display: string; pct: number; chip: string }

export function modelLegend(models: LegendInput[]): { items: LegendItem[]; more: number } {
  const items = models
    .slice(0, 2)
    .map((it) => ({ model: it.model, display: it.display, pct: it.cost_pct, chip: it.chip }));
  return { items, more: Math.max(0, models.length - items.length) };
}
