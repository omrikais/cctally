// #264 S1 (FRESH-1/HERO-4): de-alarm the hero freshness reading. The server
// `freshness.label` uses OAuth-tuned 30s/90s thresholds (shared with the TUI +
// refresh-usage) that mark a benign 8-minute dashboard snapshot "stale". The
// hero instead derives its tint from the already-shipped age_seconds with
// dashboard-appropriate thresholds. The server label is untouched (so the TUI
// and refresh-usage keep their behavior); only the hero stops trusting it.
export type HeroFreshnessLabel = 'fresh' | 'aging' | 'stale';

const HERO_FRESH_S = 15 * 60; // ≤15 min reads calm
const HERO_AGING_S = 60 * 60; // ≤60 min reads aging; older escalates to stale

export function heroFreshnessLabel(
  ageSeconds: number | null | undefined,
): HeroFreshnessLabel {
  // Missing age → calm; a null age is "unknown", not "old", and must not
  // alarm the hero.
  if (ageSeconds == null || ageSeconds <= HERO_FRESH_S) return 'fresh';
  if (ageSeconds <= HERO_AGING_S) return 'aging';
  return 'stale';
}
