import { describe, it, expect, beforeEach, vi } from 'vitest';
import { triggerSync } from './sync';
import { getState, updateSnapshot, _resetForTests } from './store';
import type { Envelope, SyncActivity } from '../types/envelope';
import envelopeFixture from '../../__tests__/fixtures/envelope.json';

const EPOCH = 'a1b2c3d4e5f60718';
const OTHER_EPOCH = '00112233445566aa';

let frameSeq = 0;

// Minimal envelope carrying one activity state. Only `generated_at` (for
// updateSnapshot's out-of-order guard) and `sync_activity` matter here; the
// sequence number keeps every frame strictly newer than the last so no test
// silently loses a frame to that guard.
//
// The counter is the MILLISECOND fraction, zero-padded to three digits, for two
// separate reasons. Padding: `updateSnapshot` compares `generated_at` as a
// STRING, so an unpadded counter makes frame 100 sort before frame 99 and the
// frame is silently dropped as out-of-order. The counter itself never resets and
// climbs across the whole file, but the GUARD resets at every test: `beforeEach`
// calls `_resetForTests()`, which clears the store's `lastGeneratedAt`
// (store.ts:2342). A frame is therefore only ever compared against earlier
// frames of the SAME test, so the hazard is one `it()` whose own calls straddle
// 99 → 100. No test here makes more than two `frame()` calls, so nothing is
// affected yet — the padding is what keeps that true as cases are added. The
// fraction: padding the SECONDS field instead produced `00:00:001Z`, which
// orders correctly but is not a valid instant, so a later test doing
// `new Date(env.generated_at)` would get `Invalid Date`. A millisecond fraction
// keeps both properties.
function frame(activity: Partial<SyncActivity>): Envelope {
  frameSeq += 1;
  return {
    generated_at: `2026-08-16T00:00:00.${String(frameSeq).padStart(3, '0')}Z`,
    sync_activity: {
      server_epoch: EPOCH,
      rebuilding: false,
      requested_id: 0,
      started_id: 0,
      settled_id: 0,
      settled_status: null,
      settled_warnings: [],
      ...activity,
    },
  } as unknown as Envelope;
}

function queuedResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 202,
    headers: { 'Content-Type': 'application/json' },
  });
}

function okResponse(warnings: Array<{ code: string }>): Response {
  return new Response(JSON.stringify({ status: 'ok', warnings }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Disposition matrix for triggerSync — the consumer of T4's /api/sync
// contract. The store field name is `syncErrorFloorUntil` (epoch ms; 0
// when no floor active). We assert "floor set / not set" rather than a
// specific timestamp because Date.now() is non-deterministic in tests.

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  vi.restoreAllMocks();
});

// #583 S2 Task 13 — the committed web fixture must carry the same
// `sync_activity` object the server publishes unconditionally, because the
// generator's header claims the fixture mirrors the emitted Envelope. The
// fixture pins the IDLE object; queued and in-flight states are temporal
// transitions covered by the `triggerSync` cases below.
describe('envelope fixture sync_activity', () => {
  it('carries a well-formed idle sync_activity object', () => {
    const activity = (envelopeFixture as { sync_activity?: unknown })
      .sync_activity as Record<string, unknown> | undefined;
    expect(activity).toBeDefined();
    // 16 lowercase hex characters, fixed length so the envelope's byte count
    // stays deterministic for bin/cctally-snapshot-measure.
    expect(activity!.server_epoch).toMatch(/^[0-9a-f]{16}$/);
    expect(activity!.rebuilding).toBe(false);
    expect(activity!.requested_id).toBe(0);
    expect(activity!.started_id).toBe(0);
    expect(activity!.settled_id).toBe(0);
    expect(activity!.settled_status).toBeNull();
    expect(activity!.settled_warnings).toEqual([]);
  });
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

  // #583 S2 §4 retired `503` from POST /api/sync — contention now answers
  // `202` and queues. The silent early return that used to sit BEFORE the
  // `r.ok` check is therefore dead code, and this test pins its removal: a
  // `503` from anything else is an ordinary failure, not a cooperative no-op.
  it('503 → error floor (the cooperative no-op branch is retired)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('sync in progress', { status: 503 }),
      ),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  // `sync_busy` is the `--no-sync` bounded-acquire expiry: neither a refresh
  // nor a rebuild ran. It is informational rather than an error, but silence
  // makes a wedged refresh indistinguishable from an ordinary no-op.
  it('200 with sync_busy → bounded informational notice', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse([{ code: 'sync_busy' }])));
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBe(0);
    expect(getState().syncBusyNoticeUntil).toBeGreaterThan(Date.now());
  });

  // A manual refresh under `--no-sync`: the rebuild ran, only the OAuth leg was
  // skipped by the operator's own launch flag. Not a failure.
  it('200 with refresh_skipped_no_sync → success flash, no error floor', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(okResponse([{ code: 'refresh_skipped_no_sync' }])),
    );
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('200 with record_failed → error floor (unknown-to-benign never defaults)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(okResponse([{ code: 'record_failed' }])));
    await triggerSync();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('a second click while a POST is in flight issues no second request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    const first = triggerSync();
    await triggerSync();
    await first;
    expect(fetchMock).toHaveBeenCalledTimes(1);
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

// ---------------------------------------------------------------------------
// #583 S2 §6.2 — the queued contract.
//
// `202` is the CONTENDED branch only: a request that failed to take the lock.
// The lock-free path still rebuilds synchronously and answers 204/200, so none
// of the disposition cases above change.
// ---------------------------------------------------------------------------
describe('triggerSync queued contract (202)', () => {
  it('registers the request and holds it outstanding until a settlement', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 7, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBe(7);
    expect(getState().syncEpoch).toBe(EPOCH);
    // Acceptance is not completion: neither overlay fires yet.
    expect(getState().syncSuccessFlashUntil).toBe(0);
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncBusy).toBe(false);
  });

  // THE RACE §6.2 names. Acceptance is published BEFORE the handler returns
  // `202`, so the settlement frame can be applied before triggerSync has
  // parsed its response. With a latest-wins hub there is no guarantee of any
  // later frame, so registering an already-settled identifier and waiting
  // would hold `queued…` forever. Registration must therefore store the
  // identifier AND reconcile it in one operation.
  //
  // The ordering is forced rather than hoped for: the settlement frame is
  // applied from inside `json()`, which resolves strictly before triggerSync
  // sees the body.
  it('settles immediately when the settlement frame arrived before the 202 was parsed', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    const response = {
      status: 202,
      ok: true,
      json: async () => {
        updateSnapshot(frame({
          requested_id: 7, started_id: 7, settled_id: 7, settled_status: 'ok',
        }));
        return { status: 'queued', request_id: 7, server_epoch: EPOCH };
      },
    } as unknown as Response;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));

    await triggerSync();

    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncEpoch).toBeNull();
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('settles on a later frame when the settlement arrives after registration', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 4, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBe(4);

    // The acceptance frame is DROPPED by the latest-wins hub; the next frame
    // the client sees is the settlement itself.
    updateSnapshot(frame({
      requested_id: 4, started_id: 4, settled_id: 4, settled_status: 'ok',
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('settles from a high-water settled_id even when its own settlement frame was dropped', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 4, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    // Frame 4's settlement never arrives; an ordinary later frame carries the
    // retained high-water mark, which is the whole point of counters over a
    // boolean.
    updateSnapshot(frame({
      requested_id: 9, started_id: 9, settled_id: 9, settled_status: 'ok',
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  // Spec §6.2 point 4: `rebuilding` is true while ANY rebuilder holds a claim,
  // so under contention the very frame that settles this batch can still carry
  // it — one rebuilder's terminal publish while another is mid-rebuild. Every
  // other settlement case in this file leaves `rebuilding` at the helper's
  // `false` default, so without this case nothing pins that the client settles
  // on `settled_id` alone and never infers "still queued" from the flag.
  it('settles on a settlement frame that still reports rebuilding', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 4, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBe(4);

    updateSnapshot(frame({
      requested_id: 4,
      started_id: 4,
      settled_id: 4,
      settled_status: 'ok',
      rebuilding: true,
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
  });

  it('a frame whose only delta is rebuilding leaves the queued request outstanding', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 3, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    // A batchless rebuild: `rebuilding` flips and no counter moves.
    updateSnapshot(frame({ rebuilding: true }));
    expect(getState().outstandingSyncId).toBe(3);
    expect(getState().syncSuccessFlashUntil).toBe(0);
    expect(getState().syncErrorFloorUntil).toBe(0);
  });

  it('a failed settlement fires the error floor instead of the success flash', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 2, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    updateSnapshot(frame({
      requested_id: 2, started_id: 2, settled_id: 2, settled_status: 'failed',
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  // Deferred warnings: §4 removed the HTTP response that used to carry them,
  // so the SAME triage the 200 path applies must run over settled_warnings.
  it('deferred warnings are triaged exactly as HTTP warnings are', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 2, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    updateSnapshot(frame({
      requested_id: 2,
      started_id: 2,
      settled_id: 2,
      settled_status: 'ok',
      settled_warnings: [{ code: 'rate_limited' }, { code: 'fetch_failed' }],
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncErrorFloorUntil).toBeGreaterThan(Date.now());
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('a rate_limited-only settlement still counts as success', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 2, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    updateSnapshot(frame({
      requested_id: 2,
      started_id: 2,
      settled_id: 2,
      settled_status: 'ok',
      settled_warnings: [{ code: 'rate_limited' }],
    }));
    expect(getState().syncSuccessFlashUntil).toBeGreaterThan(Date.now());
    expect(getState().syncErrorFloorUntil).toBe(0);
  });

  it('a sync_busy settlement surfaces the same informational notice', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 2, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    updateSnapshot(frame({
      requested_id: 2,
      started_id: 2,
      settled_id: 2,
      settled_status: 'ok',
      settled_warnings: [{ code: 'sync_busy' }],
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncBusyNoticeUntil).toBeGreaterThan(Date.now());
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('a malformed 202 body registers nothing and stays silent', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('not json', {
          status: 202,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncEpoch).toBeNull();
    expect(getState().syncErrorFloorUntil).toBe(0);
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('a 202 missing its epoch registers nothing', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(queuedResponse({ status: 'queued', request_id: 5 })),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncEpoch).toBeNull();
  });

  // The other half of the same guard. Registering an epoch with no identifier
  // would leave `syncEpoch` set and `outstandingSyncId` null, which
  // `reconcileOutstandingSync` reads as "nothing outstanding" and never clears.
  it('a 202 missing its identifier registers nothing', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(queuedResponse({ status: 'queued', server_epoch: EPOCH })),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncEpoch).toBeNull();
  });
});

describe('sync epoch handling', () => {
  it('discards an outstanding identifier when the server epoch changes', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 6, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    expect(getState().outstandingSyncId).toBe(6);

    // A restart: a new process, new epoch, counters back at zero. Comparing
    // `settled_id` across epochs would mistake a fresh counter for progress —
    // and a zeroed counter would strand the request forever. Discard instead.
    updateSnapshot(frame({
      server_epoch: OTHER_EPOCH, requested_id: 0, started_id: 0, settled_id: 0,
    }));
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncEpoch).toBeNull();
    // Discarding is not settling: no success flash for work that never ran.
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('keeps the outstanding identifier across a reconnect that returns the same epoch', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 6, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    // Same epoch, counters intact — a transient EventSource drop, not a restart.
    updateSnapshot(frame({ requested_id: 6, started_id: 6, settled_id: 5 }));
    expect(getState().outstandingSyncId).toBe(6);
  });

  it('treats an empty server_epoch as non-authoritative rather than as a restart', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 6, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    // A snapshot that never passed through `_SnapshotRef` publishes "" — it
    // says nothing about our identifier, so it must neither settle nor discard.
    updateSnapshot(frame({ server_epoch: '', settled_id: 99 }));
    expect(getState().outstandingSyncId).toBe(6);
    expect(getState().syncSuccessFlashUntil).toBe(0);
  });

  it('an envelope without sync_activity at all leaves the request outstanding', async () => {
    updateSnapshot(frame({ requested_id: 0 }));
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        queuedResponse({ status: 'queued', request_id: 6, server_epoch: EPOCH }),
      ),
    );
    await triggerSync();
    frameSeq += 1;
    updateSnapshot({ generated_at: '2026-08-16T00:01:00Z' } as unknown as Envelope);
    expect(getState().outstandingSyncId).toBe(6);
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
