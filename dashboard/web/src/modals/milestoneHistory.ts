// Hero-modal historical-milestone navigation: week/cycle stepping + on-demand
// week-detail fetch with a session cache keyed by (source, key, detail_stamp).
// See docs/superpowers/specs/2026-07-22-hero-milestone-history-design.md (§2/§4).

import type { WeekDetailPayload, WeekIndexEntry } from '../types/envelope';

// key: `${source}|${key}|${detail_stamp}` — a moved stamp (Claude recompute /
// Codex projection rebuild / current-week growth) invalidates the entry, which
// is exactly the gap the envelope's data_version legs don't track.
//
// #1050 spec 4.6 (G2): bounded, least recently used. A stamp-keyed entry is
// never reused once its stamp moves, and two streams move often — an untrusted
// store re-stamps every cycle on every index, and a busy current cycle on every
// crossing — so an unbounded Map grew for the life of the tab. A `Map` keeps
// insertion order: a hit is re-inserted at the newest end, and an insert past
// the cap drops the oldest key.
const CACHE_CAPACITY = 32;
const cache = new Map<string, WeekDetailPayload>();

function cacheGet(key: string): WeekDetailPayload | undefined {
  const hit = cache.get(key);
  if (hit !== undefined) {
    cache.delete(key);
    cache.set(key, hit);
  }
  return hit;
}

function cacheSet(key: string, payload: WeekDetailPayload): void {
  cache.delete(key);
  cache.set(key, payload);
  while (cache.size > CACHE_CAPACITY) {
    const oldest = cache.keys().next().value as string;
    cache.delete(oldest);
  }
}

/**
 * Step from `currentKey` to an older (dir=+1) or newer (dir=-1) navigable
 * week. `index` is newest-first; `currentKey === null` means the CURRENT
 * entry — found by its `is_current` flag, NOT assumed to be index[0]. Returns
 * the target key, or `null` when the step lands on the current entry or runs
 * off either end.
 *
 * #373 root cause 2: index[0] is not always current. A foreign quota pool (or
 * any window the provider re-anchors ahead of the live one) sorts newest-first
 * ahead of the current cycle, and hard-coding position 0 then made the older
 * step land on the current entry and return null — while `canNewer` was
 * already false — disabling BOTH nav buttons and the ArrowUp/ArrowDown
 * handlers that route through here.
 */
export function stepWeek(
  index: WeekIndexEntry[],
  currentKey: string | null,
  dir: 1 | -1,
): string | null {
  if (!index.length) return null;
  // Newer than the current cycle is unreachable by contract (spec §6 Q4). This
  // must be explicit rather than falling through: deriving it from currentPos
  // would return index[0] — the foreign newer entry — and break that rule.
  if (currentKey == null && dir === -1) return null;
  const currentPos = () => {
    const i = index.findIndex((e) => e.is_current);
    // Load-bearing fallback: an index with NO current entry would otherwise
    // make pos -1 and kill navigation on a state that works today.
    return i === -1 ? 0 : i;
  };
  const pos = currentKey == null ? currentPos() : index.findIndex((e) => e.key === currentKey);
  if (pos === -1) return null;
  const next = pos + dir; // dir +1 = older (later in array), -1 = newer
  if (next < 0 || next >= index.length) return null;
  return index[next].is_current ? null : index[next].key;
}

/**
 * Fetch one cycle's milestone detail.
 *
 * `accountKey` (#416) is the `?account=` route qualifier. It MUST be the
 * account whose `cycle_index` produced `entry.key` — a correctness requirement,
 * not a filter, because a key minted from one account's index is not guaranteed
 * to resolve against the merged enumeration. Pass `null` iff "All accounts" is
 * selected; the server then answers with the shipped merged body. A mismatched
 * or omitted-under-focus qualifier is a 404 `unknown cycle`, never a
 * wrong-account body; `*`, blank, uppercase or malformed values are a 400.
 *
 * #836 (U1): Claude qualifies the same way. On a decorated store the Current
 * Week modal passes the displayed account (`current_week.account_key`), whose
 * scoped index produced the key; the server answers only with that account's
 * cycle and rejects a qualifier on an undecorated store, where the modal
 * passes `null` and the request stays the unqualified route.
 *
 * The cache key carries the account for the same reason: two accounts can hold
 * distinct cycles — or the SAME cycle key and stamp over different rows, when
 * they share subscription-week boundaries — and must never share a payload.
 */
export async function fetchWeekDetail(
  source: 'claude' | 'codex',
  entry: WeekIndexEntry,
  accountKey: string | null = null,
): Promise<WeekDetailPayload> {
  const ck = `${source}|${accountKey ?? ''}|${entry.key}|${entry.detail_stamp}`;
  const hit = cacheGet(ck);
  if (hit) return hit;
  const qs = accountKey == null ? '' : `?account=${encodeURIComponent(accountKey)}`;
  const res = await fetch(`/api/milestones/${source}/week/${encodeURIComponent(entry.key)}${qs}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error('week fetch failed'), {
      status: res.status,
      code: (body as { code?: string }).code,
      reason: (body as { reason?: string }).reason,
    });
  }
  const payload = (await res.json()) as WeekDetailPayload;
  cacheSet(ck, payload);
  return payload;
}

export function clearMilestoneHistoryCacheForTests(): void {
  cache.clear();
}

export function milestoneHistoryCacheSizeForTests(): number {
  return cache.size;
}
