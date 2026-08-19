import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Envelope } from '../types/envelope';
import {
  _resetForTests, getState, updateSnapshot,
} from './store';
import * as storeModule from './store';

function envelope(generatedAt: string, ageSeconds: number): Envelope {
  return {
    envelope_version: 2,
    generated_at: generatedAt,
    sync_age_s: ageSeconds,
    sessions: { total: 0, rows: [], sort_key: 'started_desc' },
  } as unknown as Envelope;
}

describe('#611 snapshot observation clock', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    vi.useFakeTimers();
  });

  afterEach(() => { vi.useRealTimers(); });

  it('records the local wall-clock instant only when a snapshot is accepted', () => {
    vi.setSystemTime(new Date('2026-08-18T10:00:00Z'));
    expect(updateSnapshot(envelope('2026-08-18T10:00:00Z', 60))).toBe(true);

    const observedAt = (
      getState() as ReturnType<typeof getState> & { snapshotObservedAtMs?: number }
    ).snapshotObservedAtMs;
    expect(observedAt).toBe(Date.parse('2026-08-18T10:00:00Z'));

    vi.setSystemTime(new Date('2026-08-18T11:00:00Z'));
    expect(updateSnapshot(envelope('2026-08-18T09:59:59Z', 1))).toBe(false);
    expect((
      getState() as ReturnType<typeof getState> & { snapshotObservedAtMs?: number }
    ).snapshotObservedAtMs).toBe(observedAt);
  });

  it('derives age from elapsed wall time and clamps backward clock movement', () => {
    const effectiveSnapshotAge = (
      storeModule as typeof storeModule & {
        effectiveSnapshotAge?: (
          baseAge: number | null, observedAtMs: number | null, nowMs: number,
        ) => number | null;
      }
    ).effectiveSnapshotAge;
    const t0 = Date.parse('2026-08-18T10:00:00Z');

    expect(effectiveSnapshotAge?.(60, t0, t0 + 3_600_000)).toBe(3_660);
    expect(effectiveSnapshotAge?.(60, t0, t0 - 5_000)).toBe(60);
    expect(effectiveSnapshotAge?.(null, t0, t0 + 3_600_000)).toBeNull();
  });
});
