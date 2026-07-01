// Sync-chip freshness (S7 SYNC-1). Humanizes `sync_age_s` and buckets it
// for color escalation. NOTE the thresholds are intentionally coarser than
// the server's OAuth-usage `_freshness_label` (30s/90s) — that bucket is
// tuned for how recent a rate-limit read is; a "synced N ago" chip on those
// thresholds would sit red whenever the dashboard idles. See spec §Design
// decisions (1).

export type SyncBucket = 'fresh' | 'aging' | 'stale';
export interface SyncFreshness {
  text: string;      // humanized, e.g. "8m ago"
  bucket: SyncBucket;
}

export const SYNC_AGING_S = 5 * 60;    // 300 — aging at/after 5 minutes
export const SYNC_STALE_S = 30 * 60;   // 1800 — stale at/after 30 minutes

function safeAge(ageS: number): number {
  return Number.isFinite(ageS) && ageS > 0 ? Math.floor(ageS) : 0;
}

export function humanizeAge(ageS: number): string {
  const s = safeAge(ageS);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m === 0 ? `${h}h ago` : `${h}h ${m}m ago`;
}

export function syncFreshness(ageS: number): SyncFreshness {
  const s = safeAge(ageS);
  const bucket: SyncBucket =
    s < SYNC_AGING_S ? 'fresh' : s < SYNC_STALE_S ? 'aging' : 'stale';
  return { text: humanizeAge(s), bucket };
}
