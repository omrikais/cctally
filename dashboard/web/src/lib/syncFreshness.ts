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

// Escalation order over the buckets, '' being "no bucket at all". Lives here
// rather than at the call site because it orders the type declared above.
const SYNC_BUCKET_RANK: Record<SyncBucket | '', number> = {
  '': 0,
  fresh: 1,
  aging: 2,
  stale: 3,
};

// A caller holding two readings of the same freshness must not prefer one
// unconditionally: either can be the later one. The chip's ticked bucket is
// ahead of the envelope's between frames, and the envelope is ahead of a bucket
// the error floor held back. The more severe reading is the later one in both
// directions, because age only increases within one sync.
export function moreSevereBucket(
  a: SyncBucket | '',
  b: SyncBucket | '',
): SyncBucket | '' {
  return SYNC_BUCKET_RANK[a] >= SYNC_BUCKET_RANK[b] ? a : b;
}

export const SYNC_AGING_S = 5 * 60;    // 300 — aging at/after 5 minutes
export const SYNC_STALE_S = 30 * 60;   // 1800 — stale at/after 30 minutes

function safeAge(ageS: number): number {
  return Number.isFinite(ageS) && ageS > 0 ? Math.floor(ageS) : 0;
}

// The DURATION tiers, with no direction attached (#416 QA P2-A). `humanizeAge`
// is this plus " ago"; a FUTURE interval — a countdown to a reset — is this
// plus nothing. Splitting them is the fix for "resets in 4d 10h ago": the
// caller was appending its own "resets in" prefix to a string that already
// carried a past-tense suffix, and only the `secs === 0` case had been
// special-cased, so every card with a future reset contradicted itself.
export function humanizeDuration(seconds: number): string {
  const s = safeAge(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m === 0 ? `${h}h` : `${h}h ${m}m`;
  }
  // Days tier (#259): raw ">24h" ages read poorly as "27h 9m" — the reported
  // freshness surfaces idle for a full day-plus. Drop to "1d 3h" (minutes
  // elided at this magnitude). Coarser than the hour tier by design. The
  // fresh/aging/stale buckets cap at 30min, so this text-only tier never
  // affects syncFreshness() bucketing.
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  return h === 0 ? `${d}d` : `${d}d ${h}h`;
}

export function humanizeAge(ageS: number): string {
  return `${humanizeDuration(ageS)} ago`;
}

export function syncFreshness(ageS: number): SyncFreshness {
  const s = safeAge(ageS);
  const bucket: SyncBucket =
    s < SYNC_AGING_S ? 'fresh' : s < SYNC_STALE_S ? 'aging' : 'stale';
  return { text: humanizeAge(s), bucket };
}
