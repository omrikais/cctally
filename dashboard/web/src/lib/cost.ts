// Cost classification for table cell coloring (Sessions + Projects modals).
//
// Returns a class name that maps to a color token via index.css:
//   cost-none           -> neutral/dim (unknown cost — NOT "cheap")
//   cost-xs / cost-low  -> green
//   cost-mid            -> amber
//   cost-high           -> red
//
// Bins are tuned for per-session / per-window cost intuition: tiny
// (<$0.25) and small (<$1) read as cheap; medium ($1-$3) reads as
// normal; anything above $3 is "expensive enough to notice."
//
// An unknown (null/undefined) cost must NOT read as cheap-green — it
// gets a neutral `cost-none` so a missing value reads as "unknown"
// rather than confidently low (#207 B4).
export type CostClass = 'cost-none' | 'cost-xs' | 'cost-low' | 'cost-mid' | 'cost-high';

export function costClass(c: number | null | undefined): CostClass {
  if (c == null) return 'cost-none';
  if (c < 0.25) return 'cost-xs';
  if (c < 1.0) return 'cost-low';
  if (c < 3.0) return 'cost-mid';
  return 'cost-high';
}

// #217 S6 F3 — per-turn cost bar intensity: the turn's cost as a fraction of the
// session's heaviest loaded turn, clamped to [0,1]. NOT costClass (whose absolute
// bins are tuned for per-session cost — per-turn costs are sub-cent and would all
// collapse to cost-xs). Returns 0 when there is no positive denominator.
export function costIntensity(turnCost: number, maxTurnCost: number): number {
  if (!(maxTurnCost > 0) || !Number.isFinite(turnCost)) return 0;
  const r = turnCost / maxTurnCost;
  return r < 0 ? 0 : r > 1 ? 1 : r;
}
