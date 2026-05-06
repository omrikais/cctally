// POST /api/sync with sync-chip decoration.
//
// Response semantics (consumer of T4's /api/sync contract):
//   204 → sync started; refresh succeeded; snapshot republished. Silent.
//   200 + JSON {status:"ok", warnings:[...]} → sync started; rebuild
//        succeeded; refresh-usage produced one or more warnings.
//        - `rate_limited` alone is silent — matches the server's
//          "exit 0 on 429" precedent (cctally refresh-usage returns
//          exit 0 on HTTP 429 because UA changes are the only fix and
//          treating it as an error trains users to ignore real
//          failures) and the 503 cooperative-no-op pattern below.
//        - Any other warning code (`fetch_failed`, `parse_failed`,
//          `no_oauth_token`, `record_failed`, …) flashes the 3 s
//          error floor on the chip — these represent user-actionable
//          failures the user should see briefly even though the
//          rebuild succeeded.
//        - Mixed: any non-rate_limited warning fires the floor; the
//          presence of rate_limited alongside doesn't suppress it.
//   503 → another sync already in flight; SILENT cooperative no-op.
//        Common when the periodic sync thread fires while the user
//        also clicks the chip — NOT a failure.
//   other non-2xx or throw → 3 s error floor.

import { dispatch, getState } from './store';

const SYNC_ERROR_FLOOR_MS = 3000;
// Affirmative success indicator on the chip after a clean /api/sync.
// 1.2s is long enough to read "✓ updated" without dwelling, short
// enough that the chip returns to "synced 0s ago" before the next tick.
const SYNC_SUCCESS_FLASH_MS = 1200;
// Perception floor for the spinner. Without this, a fast resolution
// (e.g. /api/sync returning 200 in 50 ms because OAuth was warm-cached)
// flips busy=true → busy=false in one frame and the user sees no
// progress signal at all. 300 ms is the threshold above which a state
// change is reliably perceived.
const SYNC_SPINNER_MIN_MS = 300;

interface SyncWarning {
  code?: string;
}
interface SyncOkBody {
  status?: string;
  warnings?: SyncWarning[];
}

export async function triggerSync(): Promise<void> {
  if (getState().syncBusy) return;
  const clickAt = Date.now();
  dispatch({ type: 'SET_SYNC_BUSY', busy: true });
  // success tracks whether to fire the green flash in finally. 503 and
  // failure paths leave it false; only a clean 204 / 200-with-no-fatal
  // flips it true.
  let success = false;
  try {
    const r = await fetch('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    // Check 503 BEFORE r.ok so the cooperative no-op stays silent
    // (r.ok is false for 503 — would otherwise fall into the error
    // branch below).
    if (r.status === 503) {
      // Another sync in flight — stay quiet, the next SSE frame will repaint.
      return;
    }
    if (r.ok) {
      if (r.status === 200) {
        // Defensive: malformed body should not throw — fall back to
        // empty warnings (treat as success).
        const body: SyncOkBody = await r.json().catch(() => ({}));
        const warnings = Array.isArray(body.warnings) ? body.warnings : [];
        const fatal = warnings.filter(
          (w) => w?.code && w.code !== 'rate_limited',
        );
        if (fatal.length > 0) {
          // eslint-disable-next-line no-console
          console.error(
            '/api/sync warnings:',
            fatal.map((w) => w.code).join(', '),
          );
          dispatch({
            type: 'SET_SYNC_ERROR_FLOOR',
            untilMs: Date.now() + SYNC_ERROR_FLOOR_MS,
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
    // failure visible.
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
