// Hero-modal historical-milestone navigation: week/cycle stepping + on-demand
// week-detail fetch with a session cache keyed by (source, key, detail_stamp).
// See docs/superpowers/specs/2026-07-22-hero-milestone-history-design.md (§2/§4).

import type { WeekDetailPayload, WeekIndexEntry } from '../types/envelope';

// key: `${source}|${key}|${detail_stamp}` — a moved stamp (Claude recompute /
// Codex projection rebuild / current-week growth) invalidates the entry, which
// is exactly the gap the envelope's data_version legs don't track.
const cache = new Map<string, WeekDetailPayload>();

/**
 * Step from `currentKey` to an older (dir=+1) or newer (dir=-1) navigable
 * week. `index` is newest-first; `currentKey === null` means the current week
 * (index[0] when it is the current entry). Returns the target key, or `null`
 * when the step lands on the current entry or runs off either end.
 */
export function stepWeek(
  index: WeekIndexEntry[],
  currentKey: string | null,
  dir: 1 | -1,
): string | null {
  if (!index.length) return null;
  const pos = currentKey == null ? 0 : index.findIndex((e) => e.key === currentKey);
  if (pos === -1) return null;
  const next = pos + dir; // dir +1 = older (later in array), -1 = newer
  if (next < 0 || next >= index.length) return null;
  return index[next].is_current ? null : index[next].key;
}

export async function fetchWeekDetail(
  source: 'claude' | 'codex',
  entry: WeekIndexEntry,
): Promise<WeekDetailPayload> {
  const ck = `${source}|${entry.key}|${entry.detail_stamp}`;
  const hit = cache.get(ck);
  if (hit) return hit;
  const res = await fetch(`/api/milestones/${source}/week/${encodeURIComponent(entry.key)}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error('week fetch failed'), {
      status: res.status,
      code: (body as { code?: string }).code,
      reason: (body as { reason?: string }).reason,
    });
  }
  const payload = (await res.json()) as WeekDetailPayload;
  cache.set(ck, payload);
  return payload;
}

export function clearMilestoneHistoryCacheForTests(): void {
  cache.clear();
}
