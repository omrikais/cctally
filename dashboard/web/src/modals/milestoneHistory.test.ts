import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  clearMilestoneHistoryCacheForTests,
  fetchWeekDetail,
  stepWeek,
} from './milestoneHistory';
import type { WeekIndexEntry } from '../types/envelope';

function entry(key: string, opts: Partial<WeekIndexEntry> = {}): WeekIndexEntry {
  return {
    key,
    start_at_utc: null,
    end_at_utc: null,
    label: key,
    is_current: false,
    milestone_count: 0,
    block_count: 0,
    detail_stamp: `stamp-${key}`,
    ...opts,
  };
}

// Newest-first; only index[0] is the current week.
const index: WeekIndexEntry[] = [
  entry('2026-05-15', { is_current: true }),
  entry('2026-05-08'),
  entry('2026-05-01'),
];

afterEach(() => {
  clearMilestoneHistoryCacheForTests();
  vi.restoreAllMocks();
});

describe('stepWeek', () => {
  it('steps older from the current week (null) to the next entry', () => {
    expect(stepWeek(index, null, 1)).toBe('2026-05-08');
  });

  it('steps older again to the oldest entry', () => {
    expect(stepWeek(index, '2026-05-08', 1)).toBe('2026-05-01');
  });

  it('returns null stepping newer back onto the current entry', () => {
    expect(stepWeek(index, '2026-05-08', -1)).toBe(null);
  });

  it('steps newer from the oldest to the middle historic week', () => {
    expect(stepWeek(index, '2026-05-01', -1)).toBe('2026-05-08');
  });

  it('returns null stepping newer from the current week', () => {
    expect(stepWeek(index, null, -1)).toBe(null);
  });

  it('returns null off the old end', () => {
    expect(stepWeek(index, '2026-05-01', 1)).toBe(null);
  });

  it('returns null for an unknown key', () => {
    expect(stepWeek(index, 'nope', 1)).toBe(null);
  });

  it('returns null for an empty index', () => {
    expect(stepWeek([], null, 1)).toBe(null);
  });

  // #373 root cause 2: any index whose NEWEST entry is not the current cycle
  // disabled navigation in both directions, because position 0 was hard-coded
  // as the current selection.
  const foreignFirst: WeekIndexEntry[] = [
    entry('foreign-newer'),
    entry('current', { is_current: true }),
    entry('older'),
  ];

  it('steps older from the current cycle even when a newer entry precedes it', () => {
    expect(stepWeek(foreignFirst, null, 1)).toBe('older');
  });

  it('keeps newer-than-current unreachable', () => {
    // Contract from spec §6 Q4: null selection + newer direction is always null.
    expect(stepWeek(foreignFirst, null, -1)).toBeNull();
  });

  it('falls back to position 0 when no entry is current', () => {
    const historic = foreignFirst.map((e) => ({ ...e, is_current: false }));
    expect(stepWeek(historic, null, 1)).toBe('current');
  });
});

describe('fetchWeekDetail', () => {
  it('caches on an identical stamp (single network call)', async () => {
    const payload = { source: 'claude', key: '2026-05-08', segments: [], dividers: [], blocks: [] };
    global.fetch = vi.fn(async () => ({ ok: true, json: async () => payload })) as unknown as typeof fetch;
    const e = entry('2026-05-08', { detail_stamp: 's1' });
    const a = await fetchWeekDetail('claude', e);
    const b = await fetchWeekDetail('claude', e);
    expect(a).toBe(b);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it('re-fetches when the detail_stamp changes', async () => {
    global.fetch = vi.fn(async () => ({ ok: true, json: async () => ({}) })) as unknown as typeof fetch;
    await fetchWeekDetail('claude', entry('2026-05-08', { detail_stamp: 's1' }));
    await fetchWeekDetail('claude', entry('2026-05-08', { detail_stamp: 's2' }));
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('hits the source-scoped route with the encoded key', async () => {
    const spy = vi.fn(async () => ({ ok: true, json: async () => ({}) }));
    global.fetch = spy as unknown as typeof fetch;
    await fetchWeekDetail('codex', entry('milestone_cycle:a/b+c'));
    expect(spy).toHaveBeenCalledWith(
      `/api/milestones/codex/week/${encodeURIComponent('milestone_cycle:a/b+c')}`,
    );
  });

  it('throws an error carrying status + code on a non-ok response', async () => {
    global.fetch = vi.fn(async () => ({
      ok: false,
      status: 404,
      json: async () => ({ code: 'unknown_key', reason: 'pruned' }),
    })) as unknown as typeof fetch;
    await expect(fetchWeekDetail('codex', entry('gone'))).rejects.toMatchObject({
      status: 404,
      code: 'unknown_key',
      reason: 'pruned',
    });
  });
});
