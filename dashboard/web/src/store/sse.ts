import type { Envelope } from '../types/envelope';
import { dispatch, updateSnapshot, resetSnapshotOrdering, getState } from './store';
import { coerceUpdateState, coerceUpdateSuppress } from './update';
import { collectToastAlertRows } from '../lib/alertIdentity';

let es: EventSource | null = null;
let disconnected = false;
// B2/B3 (#207): the bootstrap fetch failed AND no snapshot has landed from
// any source yet — i.e. cold-start with no data. Distinct from `disconnected`
// (which is a drop AFTER first data). `startGeneration` guards against a late
// reject from a superseded startSSE() raising the error view after recovery.
let bootstrapError = false;
let startGeneration = 0;
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

export function isBootstrapError(): boolean { return bootstrapError; }

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
  // B2/B3: re-arm the bootstrap-error flag on every fresh start, and bump
  // the start-generation token. The bootstrap closure captures `myGen` so a
  // late reject from a SUPERSEDED start (a second startSSE only closes the
  // old EventSource, not the in-flight old fetch) can't raise the error.
  bootstrapError = false;
  const myGen = ++startGeneration;
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
        // A snapshot landed — clear any bootstrap-error view (defensive;
        // normally false on the success path).
        if (bootstrapError) { bootstrapError = false; emitStatus(); }
        ingestAlerts(snap);
        ingestUpdate(snap);
        ingestDoctor(snap);
        ingestDashboardPrefs(snap);
      }
      cb.onConnect?.();
    } catch (err) {
      console.error('initial snapshot failed:', err);
      // Only raise the error view if THIS start is still current AND no
      // snapshot has landed from any source (an SSE update can beat the
      // bootstrap fetch's reject). A late reject after recovery / after a
      // newer startSSE is then a no-op (Codex P2 race guard).
      if (myGen === startGeneration && getState().snapshot == null && !bootstrapError) {
        bootstrapError = true;
        emitStatus();
      }
    }
  })();

  es = new EventSource('/api/events');
  es.addEventListener('update', (ev: MessageEvent) => {
    try {
      const snap = JSON.parse(ev.data) as Envelope;
      if (updateSnapshot(snap)) {
        // A snapshot landed via SSE — clear any bootstrap-error view so a
        // cold start that recovers through the stream self-heals (B2/B3).
        if (bootstrapError) { bootstrapError = false; emitStatus(); }
        ingestAlerts(snap);
        ingestUpdate(snap);
        ingestDoctor(snap);
        ingestDashboardPrefs(snap);
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
  // Budget axis (issue #19) — a stale Python without the budget leg
  // won't carry these; default to "no thresholds / disabled".
  budget_thresholds: [] as number[],
  budget_enabled: false,
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
  if (es) { es.close(); es = null; }
  // disconnected=false here models a clean teardown, not a retry-in-progress.
  disconnected = false;
  // Re-arm the bootstrap-error flag too (B2/B3) — a clean teardown clears
  // any cold-start error so the next startSSE begins fresh.
  bootstrapError = false;
  // Re-arm cold-start so the next startSSE begins in cold-start mode
  // (matches startSSE's own re-arm on entry; defensive).
  isFirstTick = true;
  emitStatus();
  resetSnapshotOrdering();
}

export function _resetForTests(): void {
  if (es) { es.close(); es = null; }
  disconnected = false;
  bootstrapError = false;
  startGeneration = 0;
  isFirstTick = true;
  statusSubs.clear();
}
