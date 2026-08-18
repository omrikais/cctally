// #583 S2 — the shared refresh-contract vocabulary: the chip's three timing
// floors, the warning triage, and the idle fallback for `sync_activity`.
//
// This module holds only types and constants so both `store/store.ts` (which
// reconciles a queued request against every published frame) and
// `store/sync.ts` (which triages the HTTP response) can import it without a
// cycle. The two paths MUST apply the same triage: since the queued contract
// removed the HTTP response that used to carry warnings, the very same codes
// arrive over HTTP on the synchronous path and on the frame's
// `settled_warnings` on the queued path, and a user must not see one outcome
// treated as a failure and the other as a success.

import type { Envelope, SyncActivity } from '../types/envelope';

export const SYNC_ERROR_FLOOR_MS = 3000;
// Affirmative success indicator on the chip after a clean /api/sync.
// 1.2s is long enough to read "✓ updated" without dwelling, short
// enough that the chip returns to "synced 0s ago" before the next tick.
export const SYNC_SUCCESS_FLASH_MS = 1200;
// A refused refresh is informational, but it still needs enough time on the
// chip to be read after the spinner hands off. Match the error floor's 3 s
// visibility without borrowing its failure semantics or colour.
export const SYNC_BUSY_NOTICE_MS = 3000;
// Perception floor for the spinner. Without this, a fast resolution
// (e.g. /api/sync returning 200 in 50 ms because OAuth was warm-cached)
// flips busy=true → busy=false in one frame and the user sees no
// progress signal at all. 300 ms is the threshold above which a state
// change is reliably perceived.
export const SYNC_SPINNER_MIN_MS = 300;

// Warnings that describe a real sync which nonetheless completed. They flash no
// failure, because neither is user-actionable in the way the error floor exists
// for:
//   rate_limited            — matches the server's "exit 0 on HTTP 429"
//                             precedent; only a User-Agent change fixes it, and
//                             treating it as an error trains users to ignore
//                             real failures.
//   refresh_skipped_no_sync — the documented `--no-sync` freeze. The rebuild
//                             ran; only the OAuth leg was skipped, by the
//                             operator's own launch flag.
export const NON_FATAL_SYNC_WARNINGS: ReadonlySet<string> = new Set([
  'rate_limited',
  'refresh_skipped_no_sync',
]);

// `sync_busy` means NOTHING happened: the `--no-sync` bounded lock acquire
// expired, so neither a refresh nor a rebuild ran. It is neither a success nor
// a failure. It receives a bounded informational notice; reporting "✓ updated"
// for it would be a lie about work that never ran.
export const BUSY_SYNC_WARNINGS: ReadonlySet<string> = new Set(['sync_busy']);

// Any code that is neither non-fatal nor the busy refusal fires the error floor —
// `no_oauth_token`, `fetch_failed`, `parse_failed`, `record_failed`, and any
// code a later server adds. Defaulting an UNKNOWN code to fatal is deliberate:
// a new warning the client has never heard of is more likely to be a real
// degradation than a benign one, and a visible 3-second floor is the cheap
// direction to be wrong in.
export function isFatalSyncWarning(code: string): boolean {
  return !NON_FATAL_SYNC_WARNINGS.has(code) && !BUSY_SYNC_WARNINGS.has(code);
}

// Collect the non-empty `code` strings out of either warning carrier (the HTTP
// body's `warnings` or a frame's `settled_warnings`), tolerating a malformed
// entry rather than throwing on a published frame.
export function syncWarningCodes(
  warnings: ReadonlyArray<{ code?: string } | null | undefined> | null | undefined,
): string[] {
  if (!Array.isArray(warnings)) return [];
  return warnings
    .map((w) => w?.code)
    .filter((c): c is string => typeof c === 'string' && c !== '');
}

// The defined fallback §8 requires: an absent object reads as idle, so an older
// server or a fixture built before the field landed does not break the client.
export const IDLE_SYNC_ACTIVITY: SyncActivity = Object.freeze({
  server_epoch: '',
  rebuilding: false,
  requested_id: 0,
  started_id: 0,
  settled_id: 0,
  settled_status: null,
  settled_warnings: [],
}) as SyncActivity;

export function syncActivityOrIdle(
  env: Envelope | null | undefined,
): SyncActivity {
  return env?.sync_activity ?? IDLE_SYNC_ACTIVITY;
}
