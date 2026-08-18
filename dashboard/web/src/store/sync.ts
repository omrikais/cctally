// POST /api/sync with sync-chip decoration.
//
// Response semantics (#583 S2 §4 — the refresh contract):
//   204 → the lock was free, the rebuild ran synchronously, the refresh
//        succeeded, the snapshot was republished. Silent success.
//   200 + JSON {status:"ok", warnings:[...]} → same synchronous path, but
//        `refresh-usage` produced one or more warnings. `lib/syncActivity`
//        owns the triage; see the code sets there for which warnings are
//        non-fatal, which are silent, and why an unknown code is fatal.
//   202 + JSON {status:"queued", request_id:N, server_epoch:E} → the
//        CONTENDED branch, and ONLY that: this request failed to take
//        `sync_lock`, so the server enqueued it and will coalesce it into the
//        next rebuild. There is no later HTTP response, so the outcome
//        arrives on a published frame as `sync_activity.settled_*`. The
//        identifier is registered and reconciled in ONE store operation —
//        see REGISTER_SYNC_REQUEST.
//   other non-2xx or throw → 3 s error floor.
//
// `503` is GONE from this endpoint. It used to mean "another sync is already
// in flight", and it was answered with a silent early return placed BEFORE the
// `r.ok` check. Contention now answers 202, so that branch was dead code and
// removing it makes a 503 from anything else an ordinary, visible failure.

import { dispatch, getState } from './store';
import {
  isFatalSyncWarning,
  syncWarningCodes,
  BUSY_SYNC_WARNINGS,
  SYNC_BUSY_NOTICE_MS,
  SYNC_ERROR_FLOOR_MS,
  SYNC_SUCCESS_FLASH_MS,
  SYNC_SPINNER_MIN_MS,
} from '../lib/syncActivity';

interface SyncWarning {
  code?: string;
}
interface SyncOkBody {
  status?: string;
  warnings?: SyncWarning[];
}
interface SyncQueuedBody {
  status?: string;
  request_id?: number;
  server_epoch?: string;
}

export async function triggerSync(): Promise<void> {
  if (getState().syncBusy) return;
  const clickAt = Date.now();
  dispatch({ type: 'SET_SYNC_BUSY', busy: true });
  // success tracks whether to fire the green flash in finally. The queued and
  // failure paths leave it false; only a clean 204 / 200-with-no-fatal flips it
  // true. A queued request's flash fires on SETTLEMENT instead, from the store,
  // because acceptance is not completion.
  let success = false;
  try {
    const r = await fetch('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    if (r.status === 202) {
      // Defensive parse: a malformed body must not throw, and must not
      // register a request the client cannot later recognise.
      const body: SyncQueuedBody = await r.json().catch(() => ({} as SyncQueuedBody));
      if (
        typeof body.request_id === 'number'
        && Number.isFinite(body.request_id)
        && body.request_id > 0
        && typeof body.server_epoch === 'string'
        && body.server_epoch !== ''
      ) {
        // ONE atomic operation: register AND reconcile against the envelope
        // already in the store. Acceptance is published before the handler
        // returns this 202, so the settlement frame may have landed and been
        // applied before this line runs, and a latest-wins hub guarantees no
        // later frame. Registering without reconciling would hold `queued…`
        // forever.
        dispatch({
          type: 'REGISTER_SYNC_REQUEST',
          id: body.request_id,
          epoch: body.server_epoch,
        });
      }
      return;
    }
    if (r.ok) {
      if (r.status === 200) {
        // Defensive: malformed body should not throw — fall back to
        // empty warnings (treat as success).
        const body: SyncOkBody = await r.json().catch(() => ({}));
        const codes = syncWarningCodes(body.warnings);
        const fatal = codes.filter(isFatalSyncWarning);
        if (fatal.length > 0) {
          // eslint-disable-next-line no-console
          console.error('/api/sync warnings:', fatal.join(', '));
          dispatch({
            type: 'SET_SYNC_ERROR_FLOOR',
            untilMs: Date.now() + SYNC_ERROR_FLOOR_MS,
          });
          return;
        }
        // A busy refusal reports that NOTHING ran. It is neither success nor
        // failure, but it must stay visible long enough to distinguish a
        // wedged rebuild from an ordinary refresh no-op.
        if (codes.some((c) => BUSY_SYNC_WARNINGS.has(c))) {
          dispatch({
            type: 'SET_SYNC_BUSY_NOTICE',
            untilMs: Date.now() + SYNC_BUSY_NOTICE_MS,
          });
          return;
        }
      }
      success = true;
      return;
    }
    // eslint-disable-next-line no-console
    console.error(`/api/sync failed: ${r.status}`);
    dispatch({
      type: 'SET_SYNC_ERROR_FLOOR',
      untilMs: Date.now() + SYNC_ERROR_FLOOR_MS,
    });
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error('/api/sync failed:', err);
    dispatch({
      type: 'SET_SYNC_ERROR_FLOOR',
      untilMs: Date.now() + SYNC_ERROR_FLOOR_MS,
    });
  } finally {
    // Minimum-perceivable spinner: a sub-300ms resolution would flip
    // busy in one frame and the user sees no signal. Hold the spinner
    // until at least 300ms total has elapsed since the click. On the
    // common 5-7s OAuth-bound success path this is a no-op (elapsed
    // dwarfs the floor); on instant-throw network errors it makes the
    // failure visible. A 202 also resolves in milliseconds — the queue
    // answers immediately — and the chip hands over to `queued…` after
    // this floor rather than blinking.
    const elapsed = Date.now() - clickAt;
    if (elapsed < SYNC_SPINNER_MIN_MS) {
      await new Promise((r) => setTimeout(r, SYNC_SPINNER_MIN_MS - elapsed));
    }
    if (success) {
      dispatch({
        type: 'SET_SYNC_SUCCESS_FLASH',
        untilMs: Date.now() + SYNC_SUCCESS_FLASH_MS,
      });
    }
    dispatch({ type: 'SET_SYNC_BUSY', busy: false });
  }
}
