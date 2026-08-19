import type { Envelope } from '../types/envelope';
import { dispatch, updateSnapshot, resetSnapshotOrdering, getState } from './store';
import { coerceUpdateState, coerceUpdateSuppress } from './update';
import { collectToastAlertRows } from '../lib/alertIdentity';
import {
  createDashboardStreamTransport,
  type DashboardStreamTransport,
} from './dashboardStreamTransport';

let es: DashboardStreamTransport | null = null;
export type ConnectionState = 'connected' | 'suspended' | 'resuming' | 'disconnected';
let currentConnectionState: ConnectionState = 'disconnected';
// B2/B3 (#207): no snapshot has landed from any source yet — i.e. cold-start
// with no data. Distinct from `disconnected` (which is a drop AFTER first
// data).
//
// #583 S3 §7: the condition used to be "the bootstrap fetch rejected"; that
// fetch is gone, so the stream itself raises this when it errors before any
// snapshot has landed.
let bootstrapError = false;
// What raised it, when the raiser knows something the shared banner text does
// not. `null` means the ordinary case — a stream error — for which the banner's
// own wording is right. The watchdog below fills it in, because "couldn't load
// dashboard data" sends the reader to a server that is answering perfectly well.
let bootstrapErrorMsg: string | null = null;
// #583 S3 §7/§8: this no longer guards a late bootstrap rejection. It
// invalidates every callback of a superseded or SUSPENDED EventSource — both
// the update listener and the error handler capture it — so a callback already
// queued when a stream was closed cannot write into the store afterwards.
let startGeneration = 0;
// #583 S3 §7: whether the first snapshot of this start has been accepted. The
// first accepted update IS the bootstrap, so it is what fires `onConnect`;
// afterwards only a reconnect transition does. A resume from a hidden tab does
// NOT re-arm this, because the retained snapshot means the client was never
// without data.
let bootstrapped = false;
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

// #583 S3 §8. A connected client costs the server one projection and the
// browser one full parse on every tick, for as long as the tab is open, whether
// or not anyone is looking at it. A tab hidden beyond this grace disconnects the
// main dashboard delivery for this tab; returning resumes it. The direct
// fallback closes/reopens its EventSource, while the SharedWorker path keeps
// the port and suspends only this subscriber.
//
// Thirty seconds spans two of the server's fifteen-second keep-alive intervals
// and several publish periods, while absorbing an ordinary task switch: hiding
// and returning inside it produces no reconnect at all, and a longer cycle
// produces at most one reconnect per hidden interval.
const HIDDEN_GRACE_MS = 30_000;
let hiddenTimer: ReturnType<typeof setTimeout> | null = null;
let visibilityHandler: (() => void) | null = null;

// #583 S3 §7. The bootstrap `fetch('/api/data')` is gone, so the EventSource is
// the only path to a first paint and `es.onerror` is the only thing that raises
// the cold-start error view. A connection that is QUEUED rather than failed
// never fires `onerror`: the browser's six-connections-per-origin HTTP/1.1
// limit with several tabs open, an intermediary that buffers
// `text/event-stream`, and a server that accepts the socket and then stalls all
// leave a stream that is open and silent. `deriveAppState` renders `loading`
// for as long as that lasts, so the user meets a skeleton grid with no
// diagnostic; before this session the same situations still painted from
// `/api/data` and merely stopped updating.
//
// Ten seconds bounds the wait. `SSEHub.subscribe` seeds a new connection
// immediately from the last publication, so a healthy stream delivers its first
// frame in milliseconds and ten seconds is far outside anything normal.
export const FIRST_FRAME_TIMEOUT_MS = 10_000;
const FIRST_FRAME_TIMEOUT_MESSAGE =
  'The dashboard’s update stream is open but has sent no data. '
  + 'Reload the page, and close other dashboard tabs if you have several open.';
let firstFrameTimer: ReturnType<typeof setTimeout> | null = null;

export interface SSECallbacks {
  onConnect?: () => void;
  onDisconnect?: () => void;
}

export function connectionState(): ConnectionState { return currentConnectionState; }

export function isDisconnected(): boolean {
  return currentConnectionState === 'disconnected';
}

export function isBootstrapError(): boolean { return bootstrapError; }

export function bootstrapErrorMessage(): string | null { return bootstrapErrorMsg; }

// Armed when a stream is opened, disarmed by the first accepted snapshot, by
// `closeSSE()` and by the hidden-tab suspend. The fire path re-checks that no
// snapshot has landed, so it matches `es.onerror`'s condition exactly: the
// error view claims there is nothing to show, and a client holding a retained
// snapshot has something to show.
function armFirstFrameWatchdog(): void {
  disarmFirstFrameWatchdog();
  firstFrameTimer = setTimeout(() => {
    firstFrameTimer = null;
    if (getState().snapshot != null || bootstrapError) return;
    bootstrapError = true;
    bootstrapErrorMsg = FIRST_FRAME_TIMEOUT_MESSAGE;
    emitStatus();
  }, FIRST_FRAME_TIMEOUT_MS);
}

function disarmFirstFrameWatchdog(): void {
  if (firstFrameTimer != null) { clearTimeout(firstFrameTimer); firstFrameTimer = null; }
}

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

function setConnectionState(next: ConnectionState): void {
  if (currentConnectionState === next) return;
  currentConnectionState = next;
  emitStatus();
}

export function startSSE(cb: SSECallbacks = {}): void {
  if (es) { es.close(); es = null; }
  setConnectionState('disconnected');
  // B2/B3: re-arm the bootstrap-error flag on every fresh start, and bump the
  // start-generation token so a callback queued by a superseded EventSource
  // cannot raise the error view after recovery.
  bootstrapError = false;
  bootstrapErrorMsg = null;
  bootstrapped = false;
  startGeneration += 1;
  // Re-arm the cold-start rule on every fresh startSSE — the next
  // INGEST_SNAPSHOT_ALERTS dispatch (from the first update) will populate
  // seenAlertIds without surfacing toasts.
  isFirstTick = true;
  emitStatus();
  resetSnapshotOrdering();

  // #583 S3 §7. There is no bootstrap `fetch('/api/data')` any more. A cold
  // load used to transfer and parse the whole envelope TWICE — once as the
  // fetch response and again as the hub's subscribe seed. `cmd_dashboard`
  // publishes `ref.get()` BEFORE the HTTP server binds, so the hub's `_last`
  // is never empty when a client connects and the seed alone is a sufficient
  // bootstrap. Progressive hydration is unaffected, because the A2 partials
  // travel through the same hub.
  armVisibility(cb);
  // Evaluate the CURRENT visibility, not only future changes. A page restored
  // into a background tab never fires `visibilitychange`, because its
  // visibility never changes.
  //
  // #583 S3 §8 / P3-9: such a tab opens NO stream at all. Opening one and
  // suspending it after the grace downloaded and parsed roughly five full
  // envelopes — thirty seconds against a measured 6.5-second publish period —
  // multiplied by however many tabs the browser restored at once, to display
  // nothing. A hidden tab has nothing to display, so the first transition to
  // visible is what opens the stream. No stream also means no first-frame
  // watchdog: there is no frame to wait for, and arming here would raise the
  // error view against a tab that is deliberately not streaming.
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
  openStream(cb);
}

// #583 S3 §8. Suspension is NOT `closeSSE()`. `closeSSE()` calls
// `resetSnapshotOrdering()`, which would discard the ordering state a returning
// tab is required to retain, and it would clear the retained snapshot's claim on
// the board. Suspension deactivates this transport and keeps everything else.
function suspendForHidden(): void {
  if (!es) return;
  es.suspend();
  setConnectionState('suspended');
  // No stream, no first frame to wait for.
  disarmFirstFrameWatchdog();
  // Re-arm the cold-start rule. The accepted consequence, recorded in §8: an
  // alert that fired while the tab was hidden repaints in its panel but raises
  // no toast, because the client cannot distinguish a genuinely new crossing
  // from one it simply did not receive.
  isFirstTick = true;
}

// The visibility state machine. It governs ONLY the main dashboard stream. The
// conversation live-tail (`hooks/useConversationLiveTail.ts`) and update
// progress (`components/UpdateRunningModal.tsx`) own their own EventSources and
// are deliberately untouched — an update running while the tab is hidden keeps
// streaming to completion.
function onVisibilityChange(cb: SSECallbacks): void {
  if (typeof document === 'undefined') return;
  if (document.visibilityState === 'hidden') {
    if (hiddenTimer != null) return;       // already armed; do not restart it
    hiddenTimer = setTimeout(() => {
      hiddenTimer = null;
      suspendForHidden();
    }, HIDDEN_GRACE_MS);
    return;
  }
  if (hiddenTimer != null) { clearTimeout(hiddenTimer); hiddenTimer = null; }
  // Stream only. There is no bootstrap fetch to repeat: the returning tab
  // renders from the hub's subscribe seed exactly as a cold load does.
  if (currentConnectionState === 'suspended' && es != null) {
    setConnectionState('resuming');
    es.resume();
  } else if (es == null) {
    openStream(cb);
  }
}

function armVisibility(cb: SSECallbacks): void {
  if (typeof document === 'undefined') return;
  disarmVisibility();
  visibilityHandler = () => onVisibilityChange(cb);
  document.addEventListener('visibilitychange', visibilityHandler);
}

// Removing the listener is what makes `closeSSE()` a real teardown: left armed,
// a later return to visible would reopen a stream the caller deliberately shut.
function disarmVisibility(): void {
  if (hiddenTimer != null) { clearTimeout(hiddenTimer); hiddenTimer = null; }
  if (visibilityHandler != null && typeof document !== 'undefined') {
    document.removeEventListener('visibilitychange', visibilityHandler);
  }
  visibilityHandler = null;
}

// The transport and its callbacks are factored out so a resume from a hidden
// tab (§8) reactivates delivery WITHOUT re-running the cold-start reset above:
// the retained snapshot and the ordering state are exactly what a returning tab
// must keep.
function openStream(cb: SSECallbacks): void {
  // Captured, not read live: this top-level transport's callbacks are valid
  // until closeSSE()/startSSE() replaces it. Hidden-tab staleness is guarded by
  // the transport-local per-port generation, while a deliberate top-level
  // teardown advances startGeneration.
  const myGen = startGeneration;
  let errorReported = false;
  es = createDashboardStreamTransport({
    onReady: () => {
      if (myGen !== startGeneration) return;
      armFirstFrameWatchdog();
    },
    onSnapshot: (snap) => {
      if (myGen !== startGeneration || !updateSnapshot(snap)) return false;
      errorReported = false;
      // The stream delivered an accepted current-generation snapshot.
      disarmFirstFrameWatchdog();
      if (bootstrapError) {
        bootstrapError = false;
        bootstrapErrorMsg = null;
        emitStatus();
      }
      ingestAlerts(snap);
      ingestUpdate(snap);
      ingestDoctor(snap);
      ingestDashboardPrefs(snap);
      let connectFired = false;
      if (!bootstrapped) {
        bootstrapped = true;
        cb.onConnect?.();
        connectFired = true;
      }
      if (currentConnectionState !== 'connected') {
        setConnectionState('connected');
        if (!connectFired) cb.onConnect?.();
      }
      return true;
    },
    onError: () => {
    if (myGen !== startGeneration) return;
    // #583 S3 §7: with the bootstrap fetch gone, a stream error before any
    // snapshot has landed IS the cold-start failure the error view exists for.
    // Distinct from `disconnected`, which is a drop after first data.
    if (getState().snapshot == null && !bootstrapError) {
      bootstrapError = true;
      emitStatus();
    }
    if (currentConnectionState !== 'disconnected') {
      setConnectionState('disconnected');
      // Re-arm cold-start: the next successful update (post-reconnect)
      // should populate seenAlertIds without surfacing toasts, so a
      // network drop doesn't replay every alert that fired meanwhile.
      // Spec §4.3 / §8.7 ("post-reconnect after a drop").
      isFirstTick = true;
    }
    if (!errorReported) {
      errorReported = true;
      isFirstTick = true;
      cb.onDisconnect?.();
    }
    },
  });
}

// #556 S3 §7 — this said "Dispatches INGEST_SNAPSHOT_ALERTS for the
// just-applied snapshot." It does not: `ingestAlerts` dispatches
// INGEST_SOURCE_ALERTS, from the two provider projections. No production
// caller dispatches INGEST_SNAPSHOT_ALERTS at all, so the legacy top-level
// `state.alerts` array is empty in normal operation. This comment is what made
// a false claim about production behaviour plausible during the S3 design.
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
  // Budget axis (issue #19) — a stale Python without the budget leg
  // won't carry these; default to "no thresholds / disabled".
  budget_thresholds: [] as number[],
  budget_enabled: false,
  // The Claude weekly budget amount (#513 S2 §5.1) — a server without the
  // mirror carries no amount, and "no budget configured" is exactly `null`.
  weekly_usd: null as number | null,
  // Projected axis (issue #121) — a stale Python without the projected leg
  // won't carry these; default to "disabled".
  projected_weekly_enabled: false,
  projected_budget_enabled: false,
  // Per-project budget axis (issue #19/#121) — a stale Python without the
  // per-project leg won't carry this; default to "disabled".
  project_alerts_enabled: false,
  // Codex budget toggles (#134) — a stale Python without the Codex leg
  // won't carry these; default to "no Codex budget / disabled" so a
  // disconnected/initial UI never sees `undefined` toggle state (R7).
  codex_budget_configured: false,
  codex_budget_alerts_enabled: false,
  codex_projected_enabled: false,
};

function ingestAlerts(snap: Envelope): void {
  // #294 S5 §6.7 — feed the source-aware toast pipeline from the two provider
  // projections (`sources.claude` + `sources.codex` data.alerts) ONLY. The
  // legacy top-level `alerts` array is deliberately NOT consumed here, so a
  // codex_budget row present in both feeds can't double-toast. The panel/modal
  // read the active source's projection directly through the seam. On a pre-S4
  // envelope (no `sources` bundle) the union is empty and no toast fires —
  // matching the seam's pre-S4 Claude legacy-compatible view.
  dispatch({
    type: 'INGEST_SOURCE_ALERTS',
    rows: collectToastAlertRows(snap),
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
  const state = coerceUpdateState(
    snap.update.state, suppress, snap.update.configured_channel,
  );
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

// cache-failure-markers spec §5 — mirror the envelope's `dashboard_prefs`
// block into the named store slice. Unlike ingestUpdate/ingestDoctor (which
// no-op on an absent field to keep their last-known-good slice), we ALWAYS
// dispatch — `dashboard_prefs ?? {}` — so an older Python that omits the field
// (or a server flip to the default) resolves to the opt-out default (markers
// ON) via `selectMarkersEnabled`, never a stale prior value.
function ingestDashboardPrefs(snap: Envelope): void {
  dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: snap.dashboard_prefs ?? {} });
}

export function closeSSE(): void {
  disarmVisibility();
  disarmFirstFrameWatchdog();
  // #583 S3 §7/§8: invalidate every callback of the transport being torn
  // down. `closeSSE()` is a DELIBERATE top-level teardown; hidden suspension
  // instead advances the transport-local per-port generation and keeps this
  // callback generation intact. The ordering guard does not cover teardown:
  // `resetSnapshotOrdering()` below clears `lastGeneratedAt`, so any late frame
  // would otherwise be accepted.
  startGeneration += 1;
  if (es) { es.close(); es = null; }
  // disconnected=false here models a clean teardown, not a retry-in-progress.
  setConnectionState('disconnected');
  // Re-arm the bootstrap-error flag too (B2/B3) — a clean teardown clears
  // any cold-start error so the next startSSE begins fresh.
  bootstrapError = false;
  bootstrapErrorMsg = null;
  bootstrapped = false;
  // Re-arm cold-start so the next startSSE begins in cold-start mode
  // (matches startSSE's own re-arm on entry; defensive).
  isFirstTick = true;
  emitStatus();
  resetSnapshotOrdering();
}

export function _resetForTests(): void {
  disarmVisibility();
  disarmFirstFrameWatchdog();
  if (es) { es.close(); es = null; }
  currentConnectionState = 'disconnected';
  bootstrapError = false;
  bootstrapErrorMsg = null;
  bootstrapped = false;
  startGeneration = 0;
  isFirstTick = true;
  statusSubs.clear();
}
