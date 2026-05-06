import { describe, it, expect, beforeEach, vi } from 'vitest';
import { triggerSync } from '../src/store/sync';
import { getState, _resetForTests } from '../src/store/store';

// Disposition matrix for triggerSync — the consumer of T4's /api/sync
// contract. The store field name is `syncErrorFloorUntil` (epoch ms; 0
// when no floor active). We assert "floor set / not set" rather than a
// specific timestamp because Date.now() is non-deterministic in tests.

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.restoreAllMocks();
});

describe('triggerSync disposition', () => {
  it('204 → no error floor + success flash set', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(null, { status: 204 })),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('200 with empty warnings → no error floor + success flash set', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: 'ok', warnings: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('200 with rate_limited warning → no error floor (silent) + success flash set', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: 'ok',
            warnings: [{ code: 'rate_limited' }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('200 with fetch_failed warning → error floor (no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: 'ok',
            warnings: [{ code: 'fetch_failed' }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('200 with parse_failed warning → error floor (no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: 'ok',
            warnings: [{ code: 'parse_failed' }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('200 with no_oauth_token warning → error floor (no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: 'ok',
            warnings: [{ code: 'no_oauth_token' }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('200 with mixed warnings (rate_limited + fetch_failed) → error floor (no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            status: 'ok',
            warnings: [{ code: 'rate_limited' }, { code: 'fetch_failed' }],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('503 → silent (no error floor, no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('sync in progress', { status: 503 }),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('500 → error floor (no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('boom', { status: 500 })),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('Network throw → error floor (no success flash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new Error('offline')),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });
});

describe('triggerSync minimum-spinner timing', () => {
  it('busy stays true ≥ 300 ms even when fetch resolves immediately', async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue(new Response(null, { status: 204 })),
      );
      const p = triggerSync();
      // Drain the microtask queue so the awaited fetch resolves and
      // execution lands inside the finally block's setTimeout. The
      // setTimeout is now armed for ~300 ms but real time hasn't moved.
      await Promise.resolve();
      await Promise.resolve();
      expect(getState().syncBusy).toBe(true);
      // Mid-floor: still busy.
      vi.advanceTimersByTime(150);
      await Promise.resolve();
      expect(getState().syncBusy).toBe(true);
      // Past the 300 ms floor: busy must clear.
      vi.advanceTimersByTime(200);
      await p;
      expect(getState().syncBusy).toBe(false);
      expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
    } finally {
      vi.useRealTimers();
    }
  });
});
