// Auto-zoom y-domain for a bounded [0,100] percentage series (CR-1).
// Fits to the data + the +/-band around the median, keeps a minimum span
// so a dead-flat series does not zoom to a hairline, and never leaves
// [0,100]. Every data point and the in-range portion of the band are
// guaranteed on-chart (a near-0/100 median band clips at the valid bound).
export interface ChartDomain {
  lo: number;
  hi: number;
}

export interface AutoZoomOpts {
  minSpan?: number; // smallest allowed hi-lo (default 12)
  pad?: number;     // padding added beyond the data extent (default 1)
}

export function computeAutoZoomDomain(
  points: number[],
  median: number | null,
  bandPp: number,
  opts: AutoZoomOpts = {},
): ChartDomain {
  const minSpan = opts.minSpan ?? 12;
  const pad = opts.pad ?? 1;
  if (points.length === 0) return { lo: 0, hi: 100 };

  const cands = [...points];
  if (median != null && Number.isFinite(median)) {
    cands.push(median - bandPp, median + bandPp);
  }
  let lo = Math.min(...cands) - pad;
  let hi = Math.max(...cands) + pad;

  // Enforce the minimum span about the midpoint.
  if (hi - lo < minSpan) {
    const mid = (lo + hi) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }

  // Clamp into [0,100]; if clamping shrank the span below minSpan (a bound
  // hit an edge), slide the window inward to keep >= minSpan inside [0,100].
  lo = Math.max(0, lo);
  hi = Math.min(100, hi);
  if (hi - lo < minSpan) {
    if (hi >= 100) lo = Math.max(0, 100 - minSpan);
    else hi = Math.min(100, lo + minSpan);
  }
  return { lo, hi };
}
