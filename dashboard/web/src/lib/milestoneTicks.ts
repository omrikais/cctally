// CW-1 (#249): at low early-week usage the per-percent crossing ticks crowd
// into a sliver. Below the threshold, omit the tick overlay and rely on the
// milestone table below. Pure.
export function shouldShowMilestoneTicks(pct: number | null | undefined, threshold = 15): boolean {
  return (pct ?? 0) >= threshold;
}
