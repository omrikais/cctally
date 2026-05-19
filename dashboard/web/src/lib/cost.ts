// Cost classification for table cell coloring (Sessions + Projects modals).
//
// Returns a class name that maps to a color token via index.css:
//   cost-xs / cost-low  -> green
//   cost-mid            -> amber
//   cost-high           -> red
//
// Bins are tuned for per-session / per-window cost intuition: tiny
// (<$0.25) and small (<$1) read as cheap; medium ($1-$3) reads as
// normal; anything above $3 is "expensive enough to notice."
export type CostClass = 'cost-xs' | 'cost-low' | 'cost-mid' | 'cost-high';

export function costClass(c: number | null | undefined): CostClass {
  if (c == null) return 'cost-low';
  if (c < 0.25) return 'cost-xs';
  if (c < 1.0) return 'cost-low';
  if (c < 3.0) return 'cost-mid';
  return 'cost-high';
}
