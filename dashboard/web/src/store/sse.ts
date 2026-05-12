import type { Envelope } from '../types/envelope';
import { dispatch, updateSnapshot, resetSnapshotOrdering } from './store';
import { coerceUpdateState, coerceUpdateSuppress } from './update';

let es: EventSource | null = null;
let disconnected = false;
const statusSubs = new Set<() => void>();

// Threshold-actions T15: cold-start re-arm flag (spec §4.3, §8.7).
// `true` means the very next snapshot should be treated as a cold-start
// tick — INGEST_SNAPSHOT_ALERTS will populate seenAlertIds without
// surfacing toasts. Reset to `true` on every fresh `startSSE` and again
// on `onerror` (the next successful update after reconnect re-arms the
// rule, so a network blip doesn't bombard the user with toasts for
// alerts that fired during the drop). Module-scoped so it survives
// React StrictMode double-mounts (matches the SSE singleton lifecycle).
let isFirstTick = true;

export interface SSECallbacks {
  onConnect?: () => void;
  onDisconnect?: () => void;
}

export function isDisconnected(): boolean { return disconnected; }

export function subscribeConnectionStatus(fn: () => void): () => void {
  statusSubs.add(fn);
  return () => { statusSubs.delete(fn); };
}

function emitStatus(): void {
  statusSubs.forEach((fn) => {
    try { fn(); }
    catch (err) { console.error('status subscriber error:', err); }
  });
}

export function startSSE(cb: SSECallbacks = {}): void {
  if (es) { es.close(); es = null; }
  disconnected = false;
  // Re-arm the cold-start rule on every fresh startSSE — the next
  // INGEST_SNAPSHOT_ALERTS dispatch (from bootstrap or first update)
  // will populate seenAlertIds without surfacing toasts.
  isFirstTick = true;
  emitStatus();
  resetSnapshotOrdering();

  // Initial one-shot snapshot.
  // async/await (vs .then chain) keeps the microtask depth shallow enough
  // that tests awaiting two microticks observe the bootstrap applied.
  // ingestAlerts / ingestUpdate are gated on updateSnapshot's accept/reject
  // return so a late-arriving bootstrap (older generated_at than an SSE
  // update that already landed) can't replace fresh state.alerts /
  // state.alertsConfig / state.update or pollute state.seenAlertIds with
  // stale ids.
  (async () => {
    try {
      const r = await fetch('/api/data');
      const snap = (await r.json()) as Envelope;
      if (updateSnapshot(snap)) {
        ingestAlerts(snap);
        ingestUpdate(snap);
        ingestDoctor(snap);
      }
      cb.onConnect?.();
    } catch (err) {
      console.error('initial snapshot failed:', err);
    }
  })();

  es = new EventSource('/api/events');
  es.addEventListener('update', (ev: MessageEvent) => {
    try {
      const snap = JSON.parse(ev.data) as Envelope;
      if (updateSnapshot(snap)) {
        ingestAlerts(snap);
        ingestUpdate(snap);
        ingestDoctor(snap);
      }
      if (disconnected) {
        disconnected = false;
        emitStatus();
        cb.onConnect?.();  // only on reconnect transition
      }
    } catch (err) {
      console.error('SSE parse failed:', err);
    }
  });
  es.onerror = () => {
    if (!disconnected) {
      disconnected = true;
      // Re-arm cold-start: the next successful update (post-reconnect)
      // should populate seenAlertIds without surfacing toasts, so a
      // network drop doesn't replay every alert that fired meanwhile.
      // Spec §4.3 / §8.7 ("post-reconnect after a drop").
      isFirstTick = true;
      emitStatus();
      cb.onDisconnect?.();
    }
  };
}

// Dispatches INGEST_SNAPSHOT_ALERTS for the just-applied snapshot.
// `alerts ?? []` defends against backends without T5 that omit the
// field entirely (graceful degradation; the reducer still runs and
// the panel just stays empty). `alerts_settings` is similarly
// fall-back-defaulted: a stale Python without T5 (or a partial
// envelope) won't have the block, so we synthesize a "disabled +
// canonical thresholds" default that matches what the Python
// validator would produce for a missing config. After the first
// dispatch per connect-cycle, isFirstTick flips false so subsequent
// ticks compute fresh = alerts \ seenAlertIds and surface a toast
// for the first unseen entry. Re-armed by `onerror` on disconnect.
const FALLBACK_ALERTS_SETTINGS = {
  enabled: false,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
};

function ingestAlerts(snap: Envelope): void {
  dispatch({
    type: 'INGEST_SNAPSHOT_ALERTS',
    alerts: snap.alerts ?? [],
    alertsSettings: snap.alerts_settings ?? FALLBACK_ALERTS_SETTINGS,
    isFirstTick,
  });
  isFirstTick = false;
}

// Mirror of the envelope's `update` block (added alongside
// `alerts_settings`). Pre-mirror Python builds omit the field entirely;
// in that case we leave the slice untouched — the boot-time
// `refreshUpdateState()` fallback in main.tsx still seeds initial state
// against /api/update/status. Once Python emits the field, every tick
// repaints the badge with no extra fetch.
function ingestUpdate(snap: Envelope): void {
  if (!snap.update) return;
  const suppress = coerceUpdateSuppress(snap.update.suppress);
  const state = coerceUpdateState(snap.update.state, suppress);
  dispatch({ type: 'SET_UPDATE_STATE', state, suppress });
}

// Mirror of the envelope's `doctor` block (spec §6). Pre-mirror Python
// builds omit the field entirely; in that case the slice stays at its
// previous value (the chip just doesn't repaint until a backend with
// the field arrives — same posture as ingestUpdate). The Python
// emits a synthetic-FAIL aggregate with `_error` when the gather
// raised, so absent-field and gather-failure are distinct cases:
// absent = no dispatch, failure = dispatch a payload whose severity
// is "fail".
function ingestDoctor(snap: Envelope): void {
  if (!snap.doctor) return;
  // Trust the server's shape — coerceDoctor would be overkill since
  // the field is small and Python writes it via a typed dict. Cast
  // straight through the import type.
  dispatch({ type: 'SET_DOCTOR_AGGREGATE', doctor: snap.doctor });
}

export function closeSSE(): void {
  if (es) { es.close(); es = null; }
  // disconnected=false here models a clean teardown, not a retry-in-progress.
  disconnected = false;
  // Re-arm cold-start so the next startSSE begins in cold-start mode
  // (matches startSSE's own re-arm on entry; defensive).
  isFirstTick = true;
  emitStatus();
  resetSnapshotOrdering();
}

export function _resetForTests(): void {
  if (es) { es.close(); es = null; }
  disconnected = false;
  isFirstTick = true;
  statusSubs.clear();
}
