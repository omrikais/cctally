import type { AlertEntry, Envelope, SessionRow } from '../types/envelope';
import {
  applySessionFilter,
  computeSearchMatches,
  ctxFromEnvelope,
  sessionComparator,
} from './selectors';
import { DEFAULT_PANEL_ORDER, type PanelId } from '../lib/panelIds';
import { reconcilePanelOrder } from '../lib/reconcilePanelOrder';
import { applyTableSort, coerceSortOverride, type SortOverride } from '../lib/tableSort';
import { SESSIONS_COLUMNS } from '../lib/sessionsColumns';
import {
  initialShareState,
  shareReducer,
  type ComposerModalState,
  type ShareAction,
  type ShareModalState,
} from './shareSlice';

export type { ShareModalState, ComposerModalState } from './shareSlice';

export type { SortOverride } from '../lib/tableSort';

// localStorage keys retain the legacy `ccusage.*` namespace so the
// cctally rename doesn't reset existing users' panel/filter prefs.
// Renaming would require migration logic; not worth the disruption.
const PREFS_KEY = 'ccusage.dashboard.prefs';
const FILTER_KEY = 'ccusage.dashboard.filter';
const LEGACY_SORT_KEY = 'ccusage.dashboard.sort'; // retired — migrated on first load

export type ModalKind = 'current-week' | 'forecast' | 'trend' | 'session' | 'weekly' | 'monthly' | 'block' | 'daily' | 'alerts' | 'update';
export type InputMode = null | 'filter' | 'search';

// ---------- Update subcommand (spec §6) ----------
//
// `UpdateState` mirrors the on-disk `update-state.json` written by the
// Python `_do_update_check()`. The dashboard endpoint /api/update/status
// returns the raw state + suppress shape (single source of truth on
// disk). The frontend cooks `available` itself from the same predicate
// the Python `_format_update_check_json` uses (semver_gt && !skipped &&
// !in_remind_window) so the badge shows iff the user has not opted out.
//
// `UpdateRunStatus` is the run-time machine: idle → running → success |
// failed. The success state is special: the server execvp's the dashboard
// process, the SSE channel drops, and the existing /api/events reconnect
// logic re-establishes against the new server. The success modal shows
// a spinner + "Restarting…" and auto-closes when refreshState() returns
// `current_version === latest_version`.
//
// `UpdateStreamEvent` matches the SSE event payloads emitted by the
// Python `UpdateWorker.stream(run_id)` generator. The server uses
// `error_event` (not `error`) to avoid clashing with EventSource's
// connection-error semantics — the frontend treats `error_event` as a
// terminal failure event identical to a non-zero `exit`.
export type UpdateMethod = 'brew' | 'npm' | 'unknown';
export type UpdateRunStatus = 'idle' | 'running' | 'success' | 'failed';
export type UpdateCheckStatus =
  | 'ok'
  | 'rate_limited'
  | 'fetch_failed'
  | 'parse_failed'
  | 'unavailable';

export interface UpdateState {
  current_version: string | null;
  latest_version: string | null;
  available: boolean;
  method: UpdateMethod;
  update_command: string | null;
  release_notes_url: string | null;
  check_status: UpdateCheckStatus | null;
  checked_at_utc: string | null;
  prerelease_note: string | null;
}

export interface UpdateRemindAfter {
  version: string;
  until_utc: string;
}

export interface UpdateSuppress {
  skipped_versions: string[];
  remind_after: UpdateRemindAfter | null;
}

export interface UpdateStreamEvent {
  // Mirrors the Python UpdateWorker event types. `done` and `execvp`
  // are terminal; `error_event` is a terminal failure. `step` events
  // render as section headers in the stream viewer; stdout/stderr line
  // by line.
  type: 'stdout' | 'stderr' | 'step' | 'exit' | 'execvp' | 'error_event' | 'done' | 'heartbeat';
  data?: string;
  name?: string;
  rc?: number;
  step?: string;
  message?: string;
  ts?: number;
}

export interface UpdateSlice {
  state: UpdateState | null;
  suppress: UpdateSuppress;
  modalOpen: boolean;
  runId: string | null;
  status: UpdateRunStatus;
  stream: UpdateStreamEvent[];
  errorMessage: string | null;
  startedAt: number | null;
}

function defaultUpdateSlice(): UpdateSlice {
  return {
    state: null,
    suppress: { skipped_versions: [], remind_after: null },
    modalOpen: false,
    runId: null,
    status: 'idle',
    stream: [],
    errorMessage: null,
    startedAt: null,
  };
}

// The `available` cooking predicate + semver comparison live in
// `store/update.ts` so the action helpers (refreshState, skip, remind)
// share one source of truth. Tests import from update.ts directly.

export type SessionSortKey =
  | 'started desc'
  | 'cost desc'
  | 'duration desc'
  | 'model asc'
  | 'project asc';

// Ordered for the sort-pill click cycle (SessionsControls) and the
// Settings radio list. Label is what the Settings dropdown shows; the
// sort pill displays the raw key verbatim for legacy parity.
export const SESSION_SORT_KEYS: ReadonlyArray<{ key: SessionSortKey; label: string }> = [
  { key: 'started desc',  label: 'Started (newest first)' },
  { key: 'cost desc',     label: 'Cost (highest first)' },
  { key: 'duration desc', label: 'Duration (longest first)' },
  { key: 'model asc',     label: 'Model (A→Z)' },
  { key: 'project asc',   label: 'Project (A→Z)' },
];

export interface Prefs {
  sortDefault: SessionSortKey;
  sessionsPerPage: number;
  sessionsCollapsed: boolean;
  blocksCollapsed: boolean;   // NEW — Blocks panel collapsed by default
  dailyCollapsed: boolean;    // NEW — Daily panel collapsed by default
  // Threshold-actions T8: Recent-alerts panel collapsed flag.
  // Default false — alerts are forward-looking and the panel is
  // typically thin; we render it expanded by default. Mirrors the
  // sessions/blocks/daily flags in shape.
  alertsCollapsed: boolean;
  panelOrder: PanelId[];
  onboardingToastSeen: boolean;
  mobileOnboardingToastSeen: boolean;
  trendSortOverride: SortOverride | null;
  sessionsSortOverride: SortOverride | null;
}

// Toast variant pattern (T8). The `status` shape is the legacy
// transient message; the `alert` shape carries the full AlertEntry so
// the toast can render threshold + axis chip + context-specific body
// without a second store read.
export type ToastState =
  | { kind: 'status'; text: string }
  | { kind: 'alert'; payload: AlertEntry }
  | null;

// Mirrors the Python envelope's alerts_settings block; lets
// SettingsOverlay seed without a separate GET.
export interface AlertsConfig {
  enabled: boolean;
  weekly_thresholds: number[];
  five_hour_thresholds: number[];
}

export interface UIState {
  snapshot: Envelope | null;
  openModal: ModalKind | null;
  openSessionId: string | null;
  openBlockStartAt: string | null;
  openDailyDate: string | null;
  sessionsSort: SessionSortKey;
  filterText: string;
  searchText: string;
  searchMatches: number[];
  searchIndex: number;           // -1 when no matches
  inputMode: InputMode;
  prefs: Prefs;
  // Epoch ms through which the SyncChip should force-render
  // "⚠ sync failed" + .sync-error class, regardless of envelope/tick
  // state. 0 means no active floor. Set by `triggerSync()` on a failed
  // POST /api/sync; expires naturally in 3 s.
  syncErrorFloorUntil: number;
  // True while a POST /api/sync is in flight. <SyncChip /> renders
  // "syncing…" + the .syncing class + aria-busy while set (legacy
  // parity with dashboard/static/sync.js's local `busy` flag).
  syncBusy: boolean;
  // Epoch ms through which the SyncChip should force-render
  // "✓ updated" + .sync-success class. 0 means no active flash.
  // Mirror of syncErrorFloorUntil; set by `triggerSync()` on success.
  syncSuccessFlashUntil: number;
  // Transient toast surfaced by the <Toast /> component. null when no
  // toast active. Tagged-union: `status` (legacy short message; 2.5s
  // auto-dismiss) vs `alert` (rich percent-crossing alert; 8s
  // auto-dismiss + click-to-dismiss). Set by SHOW_STATUS_TOAST /
  // SHOW_ALERT_TOAST / INGEST_SNAPSHOT_ALERTS, cleared by HIDE_TOAST.
  toast: ToastState;
  // Threshold-actions T8: snapshot-mirrored newest-first alerts list.
  // Updated each tick by INGEST_SNAPSHOT_ALERTS (T15 wires the SSE
  // dispatch). Empty until the first envelope arrives.
  alerts: AlertEntry[];
  // Forward-only "already seen" set. Cold-start (isFirstTick=true)
  // unions the entire snapshot list without surfacing a toast (the
  // user has already seen these in a prior session). On subsequent
  // ticks the first alert NOT in this set fires the alert toast.
  // Persists in-memory only; cold-start re-seeds on every page load
  // by design — no toast spam after F5.
  seenAlertIds: Set<string>;
  // Snapshot-mirrored alerts settings so SettingsOverlay can seed
  // without a separate GET /api/settings (T9 will wire the read).
  // Updated each tick alongside `alerts`.
  alertsConfig: AlertsConfig;
  // FIFO queue of fresh alerts that arrived while a toast was already
  // showing (or co-arrived on the same tick as the currently-surfaced
  // one). Drained one entry at a time by HIDE_TOAST: when the dismissed
  // toast was an alert and the queue is non-empty, the head is promoted
  // to `toast` instead of clearing it. The bug this fixes: under
  // `--no-sync` (or any quiet stretch with no further SSE ticks),
  // co-arriving alerts were buried — `state.alerts` showed them in the
  // panel but only the first ever fired a toast because the prior
  // reducer relied on a "next tick will surface the next unseen" loop
  // that never came. Cleared on cold-start (re-seed via INGEST first
  // tick) so a reconnect can't replay alerts that fired pre-drop.
  alertToastQueue: AlertEntry[];
  // UI-only preview of panelOrder during an in-flight drag. While set,
  // App.tsx renders this order (FLIP animates) but prefs.panelOrder
  // remains untouched. Committed to prefs on drop (COMMIT_DRAG_PREVIEW)
  // or discarded on Esc / pointer-cancel / window-resize
  // (CLEAR_DRAG_PREVIEW). Never persisted to localStorage directly.
  dragPreviewOrder: PanelId[] | null;
  // Update subcommand (spec §6). The slice carries both the persisted
  // shape (state + suppress, refreshed via /api/update/status) and the
  // live runtime machine (status, runId, stream, startedAt, errorMessage)
  // for the in-progress upgrade. `modalOpen` is intentionally part of
  // this slice rather than `openModal` so the run state survives the
  // success state's execvp + reconnect: the modal stays mounted with
  // status='success' and auto-closes when refreshState() returns
  // `current_version === latest_version`.
  update: UpdateSlice;
  // Share v2 (spec §6.1). Two named slots, NOT folded into `openModal`,
  // so the share modal can layer on top of a panel modal: the user opens
  // a panel modal, inspects detail, clicks the share affordance, and the
  // share modal appears in front without unmounting the underlying panel
  // modal. `composerModal` is the multi-section editor that supersedes
  // the per-panel `shareModal` when the user clicks "Customize…" inside
  // it. State shape + reducer + action creators live in shareSlice.ts;
  // the master dispatch below forwards OPEN_SHARE / CLOSE_SHARE /
  // OPEN_COMPOSER / CLOSE_COMPOSER through shareReducer.
  shareModal: ShareModalState | null;
  composerModal: ComposerModalState | null;
}

function defaultPrefs(): Prefs {
  return {
    sortDefault: 'started desc',
    sessionsPerPage: 100,
    sessionsCollapsed: true,
    blocksCollapsed: true,
    dailyCollapsed: true,
    alertsCollapsed: false,
    panelOrder: [...DEFAULT_PANEL_ORDER],
    onboardingToastSeen: false,
    mobileOnboardingToastSeen: false,
    trendSortOverride: null,
    sessionsSortOverride: null,
  };
}

function defaultAlertsConfig(): AlertsConfig {
  // Default direction matches the Python source-of-truth at
  // bin/cctally::_validate_alerts_config (`block.get("enabled", False)`)
  // and the Python `_DEFAULT_ALERTS_THRESHOLDS = [90, 95]` list. The
  // store's hardcoded default is only what users see BEFORE the first
  // SSE tick lands; the envelope's `alerts_settings` block then
  // replaces this wholesale (see INGEST_SNAPSHOT_ALERTS reducer). If
  // these don't agree, a brand-new user with no `alerts.*` config keys
  // would briefly see a UI claiming alerts are ON while the server has
  // them OFF (toggle lies for the ~100ms before bootstrap).
  return { enabled: false, weekly_thresholds: [90, 95], five_hour_thresholds: [90, 95] };
}

function loadInitial(): UIState {
  let prefs = defaultPrefs();
  const rawPrefs = localStorage.getItem(PREFS_KEY);
  const legacySort = localStorage.getItem(LEGACY_SORT_KEY);
  if (rawPrefs) {
    try {
      const parsed = JSON.parse(rawPrefs) as Partial<Prefs>;
      prefs = { ...defaultPrefs(), ...parsed };
      prefs.panelOrder = reconcilePanelOrder(parsed.panelOrder ?? null, DEFAULT_PANEL_ORDER);
      prefs.trendSortOverride = coerceSortOverride(prefs.trendSortOverride ?? null);
      prefs.sessionsSortOverride = coerceSortOverride(prefs.sessionsSortOverride ?? null);
    } catch {
      prefs = defaultPrefs();
    }
    // Retire the legacy key unconditionally when prefs exists — prefs wins.
    if (legacySort != null) localStorage.removeItem(LEGACY_SORT_KEY);
  } else if (legacySort) {
    // One-time migration: no prefs yet, legacy sort exists → write prefs
    // with that sortDefault and delete the legacy key.
    prefs = { ...defaultPrefs(), sortDefault: legacySort as SessionSortKey };
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    localStorage.removeItem(LEGACY_SORT_KEY);
  }
  return {
    snapshot: null,
    openModal: null,
    openSessionId: null,
    openBlockStartAt: null,
    openDailyDate: null,
    sessionsSort: prefs.sortDefault,
    filterText: localStorage.getItem(FILTER_KEY) ?? '',
    searchText: '',
    searchMatches: [],
    searchIndex: -1,
    inputMode: null,
    prefs,
    syncErrorFloorUntil: 0,
    syncBusy: false,
    syncSuccessFlashUntil: 0,
    toast: null,
    alerts: [],
    seenAlertIds: new Set<string>(),
    alertsConfig: defaultAlertsConfig(),
    alertToastQueue: [],
    dragPreviewOrder: null,
    update: defaultUpdateSlice(),
    ...initialShareState,
  };
}

let state: UIState = loadInitial();
const subscribers = new Set<() => void>();
let lastGeneratedAt = '';

function emit(): void {
  subscribers.forEach((fn) => {
    try { fn(); }
    catch (err) { console.error('subscriber error:', err); }
  });
}

export function getState(): UIState { return state; }

export function subscribeStore(fn: () => void): () => void {
  subscribers.add(fn);
  return () => { subscribers.delete(fn); };
}

// getRenderedRows is the single source of truth for the currently-visible
// Sessions row list: filter → sort → slice-to-perPage, exactly matching
// how SessionsPanel paints the table. The search-matches helper below and
// the panel both read from it so match indices always line up with DOM
// positions (indices that the user navigates via n/N).
export function getRenderedRows(s: UIState = state): SessionRow[] {
  const rows = s.snapshot?.sessions?.rows ?? [];
  const filtered = applySessionFilter(rows, s.filterText);
  const override = s.prefs.sessionsSortOverride;
  const sorted = override
    ? applyTableSort(filtered, SESSIONS_COLUMNS, override)
    : filtered.slice().sort(sessionComparator(s.sessionsSort));
  return sorted.slice(0, s.prefs.sessionsPerPage);
}

// Recompute searchMatches + searchIndex from the currently-visible row
// list. Every reducer branch that changes what's visible (snapshot,
// filter, sort, perPage) — or the needle itself — calls this so the
// match set never goes stale.
function _recomputeSearch(s: UIState): Pick<UIState, 'searchMatches' | 'searchIndex'> {
  if (!s.searchText) return { searchMatches: [], searchIndex: -1 };
  const matches = computeSearchMatches(
    getRenderedRows(s),
    s.searchText,
    ctxFromEnvelope(s.snapshot),
  );
  if (matches.length === 0) return { searchMatches: [], searchIndex: -1 };
  const idx =
    s.searchIndex < 0 || s.searchIndex >= matches.length ? 0 : s.searchIndex;
  return { searchMatches: matches, searchIndex: idx };
}

// Returns `true` when the snapshot was applied, `false` when it was
// rejected as out-of-order (older `generated_at` than the last accepted
// one). Callers that want to keep adjacent state (alerts, alertsConfig,
// seenAlertIds) in sync with the snapshot — see sse.ts's `ingestAlerts`
// — gate their follow-up dispatches on this return value so a late
// bootstrap can't replace fresh post-update state with stale rows.
export function updateSnapshot(snap: Envelope | null): boolean {
  const ga = snap?.generated_at ?? '';
  if (ga && lastGeneratedAt && ga < lastGeneratedAt) return false;
  if (ga) lastGeneratedAt = ga;
  const next = { ...state, snapshot: snap };
  state = { ...next, ..._recomputeSearch(next) };
  emit();
  return true;
}

export function resetSnapshotOrdering(): void { lastGeneratedAt = ''; }

// ----- Actions -----
export type Action =
  | { type: 'OPEN_MODAL'; kind: ModalKind; sessionId?: string; blockStartAt?: string; dailyDate?: string }
  | { type: 'CLOSE_MODAL' }
  | { type: 'SET_FILTER'; text: string }
  | { type: 'SET_SEARCH'; text: string }
  | { type: 'SET_SEARCH_MATCHES'; matches: number[]; index: number }
  | { type: 'SET_SORT'; key: SessionSortKey }
  | { type: 'SET_INPUT_MODE'; mode: InputMode }
  | { type: 'SAVE_PREFS'; patch: Partial<Prefs> }
  | { type: 'RESET_PREFS' }
  | { type: 'RESET_PANEL_ORDER' }
  | { type: 'REORDER_PANELS'; from: number; to: number }
  | { type: 'SET_DRAG_PREVIEW'; order: PanelId[] }
  | { type: 'COMMIT_DRAG_PREVIEW' }
  | { type: 'CLEAR_DRAG_PREVIEW' }
  | { type: 'SWAP_PANELS'; index: number; direction: -1 | 1 }
  | { type: 'MARK_ONBOARDING_TOAST_SEEN' }
  | { type: 'MARK_MOBILE_ONBOARDING_TOAST_SEEN' }
  | { type: 'SET_SYNC_ERROR_FLOOR'; untilMs: number }
  | { type: 'SET_SYNC_BUSY'; busy: boolean }
  | { type: 'SET_SYNC_SUCCESS_FLASH'; untilMs: number }
  // Toast variant pattern (T8). SHOW_STATUS_TOAST replaces the legacy
  // SHOW_TOAST (string-message); SHOW_ALERT_TOAST surfaces a rich
  // AlertEntry. HIDE_TOAST clears either kind. INGEST_SNAPSHOT_ALERTS
  // mirrors the envelope's `alerts` list into store state and, on
  // non-cold-start ticks, queues a toast for the first unseen alert
  // (cold-start populates seenAlertIds without surfacing). T15 wires
  // the SSE handler to dispatch INGEST_SNAPSHOT_ALERTS each tick.
  | { type: 'SHOW_STATUS_TOAST'; text: string }
  | { type: 'SHOW_ALERT_TOAST'; alert: AlertEntry }
  | { type: 'HIDE_TOAST' }
  | {
      type: 'INGEST_SNAPSHOT_ALERTS';
      alerts: AlertEntry[];
      // Wholesale-replace payload for state.alertsConfig — sourced from
      // the same snapshot's `alerts_settings` block. Server is the
      // source of truth; the reducer assigns this directly (NOT a
      // shallow merge), so a server-side flip from enabled=true→false
      // takes effect immediately on the next tick. SSE handler
      // synthesizes a sensible default if the field is missing
      // (back-compat for envelopes from a Python without T5).
      alertsSettings: AlertsConfig;
      isFirstTick: boolean;
    }
  | { type: 'SET_TABLE_SORT'; table: 'trend' | 'sessions'; override: SortOverride | null }
  | { type: 'CLEAR_TABLE_SORTS' }
  // ---------- Update subcommand actions (spec §6) ----------
  // OPEN_UPDATE_MODAL / CLOSE_UPDATE_MODAL: badge click + Esc / X.
  //   Only sets modalOpen; does NOT reset status/stream so closing a
  //   running-state modal and reopening shows the same in-progress run.
  // SET_UPDATE_STATE: replace state from /api/update/status. The
  //   `available` boolean is cooked client-side from current/latest +
  //   suppress (mirrors Python `_format_update_check_json`). Server
  //   returns raw state + suppress; the cooking lives in store.ts so
  //   tests can pin the predicate without spinning a server.
  // SET_UPDATE_STATUS: state-machine transition (idle → running →
  //   success | failed). errorMessage is set on failed transitions
  //   only.
  // SET_UPDATE_RUN_ID: track the run_id returned by POST /api/update so
  //   the SSE stream URL can be built (/api/update/stream/<run_id>).
  // APPEND_UPDATE_STREAM: push one SSE event onto state.update.stream.
  //   The stream viewer auto-scrolls; the success/failed modals read
  //   the stream tail for the diagnostic preview.
  // RESET_UPDATE_RUN: drop runtime fields back to idle defaults
  //   without touching state/suppress. Called by Retry (after a
  //   failed run) before issuing a fresh POST /api/update.
  | { type: 'OPEN_UPDATE_MODAL' }
  | { type: 'CLOSE_UPDATE_MODAL' }
  | { type: 'SET_UPDATE_STATE'; state: UpdateState | null; suppress: UpdateSuppress }
  | { type: 'SET_UPDATE_STATUS'; status: UpdateRunStatus; errorMessage?: string | null }
  | { type: 'SET_UPDATE_RUN_ID'; runId: string | null; startedAt?: number | null }
  | { type: 'APPEND_UPDATE_STREAM'; event: UpdateStreamEvent }
  | { type: 'RESET_UPDATE_RUN' }
  // Share v2 (spec §6.1). OPEN_SHARE / CLOSE_SHARE / OPEN_COMPOSER /
  // CLOSE_COMPOSER are forwarded to shareReducer (shareSlice.ts) and
  // emit() — the reducer is exported pure so the unit tests can drive
  // it without booting the master store.
  | ShareAction;

export function dispatch(action: Action): void {
  switch (action.type) {
    case 'OPEN_MODAL':
      state = {
        ...state,
        openModal: action.kind,
        openSessionId: action.sessionId ?? null,
        openBlockStartAt: action.blockStartAt ?? null,
        openDailyDate: action.dailyDate ?? null,
      };
      break;
    case 'CLOSE_MODAL':
      state = {
        ...state,
        openModal: null,
        openSessionId: null,
        openBlockStartAt: null,
        openDailyDate: null,
      };
      break;
    case 'SET_FILTER': {
      if (action.text) localStorage.setItem(FILTER_KEY, action.text);
      else localStorage.removeItem(FILTER_KEY);
      const next = { ...state, filterText: action.text };
      // Filter change re-partitions the visible rows → matches recompute.
      state = { ...next, ..._recomputeSearch(next) };
      break;
    }
    case 'SET_SEARCH': {
      // Recompute against the currently-visible (filtered+sorted+sliced)
      // rows so `/` input and `n`/`N` never point off-screen or at rows
      // the user can't see. Index seeds to 0 on non-empty needle, -1 when
      // no matches.
      const withText = { ...state, searchText: action.text, searchIndex: 0 };
      state = { ...withText, ..._recomputeSearch(withText) };
      break;
    }
    case 'SET_SEARCH_MATCHES':
      state = { ...state, searchMatches: action.matches, searchIndex: action.index };
      break;
    case 'SET_SORT': {
      const next = { ...state, sessionsSort: action.key };
      // Sort reorders rendered rows; indices must follow.
      state = { ...next, ..._recomputeSearch(next) };
      break;
    }
    case 'SET_INPUT_MODE':
      state = { ...state, inputMode: action.mode };
      break;
    case 'SAVE_PREFS': {
      const prefs = { ...state.prefs, ...action.patch };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      const next = { ...state, prefs };
      // sessionsPerPage widens/narrows the slice; re-derive matches.
      state = { ...next, ..._recomputeSearch(next) };
      break;
    }
    case 'RESET_PREFS': {
      // Preserve onboardingToastSeen + mobileOnboardingToastSeen — both
      // flags track UX state, not user preference; clearing either would
      // re-show the toast on next load.
      const seen = state.prefs.onboardingToastSeen;
      const mobileSeen = state.prefs.mobileOnboardingToastSeen;
      localStorage.removeItem(PREFS_KEY);
      localStorage.removeItem(FILTER_KEY);
      const fresh = defaultPrefs();
      fresh.onboardingToastSeen = seen;
      fresh.mobileOnboardingToastSeen = mobileSeen;
      // Persist immediately so the preserved flag survives the next reload.
      localStorage.setItem(PREFS_KEY, JSON.stringify(fresh));
      state = {
        ...state,
        prefs: fresh,
        filterText: '',
        searchText: '',
        searchMatches: [],
        searchIndex: -1,
        sessionsSort: fresh.sortDefault,
      };
      break;
    }
    case 'RESET_PANEL_ORDER': {
      const prefs = { ...state.prefs, panelOrder: [...DEFAULT_PANEL_ORDER] };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs };
      break;
    }
    case 'REORDER_PANELS': {
      const { from, to } = action;
      const order = state.prefs.panelOrder;
      if (from === to) break;
      if (from < 0 || from >= order.length) break;
      if (to < 0 || to >= order.length) break;
      const next = order.slice();
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      const prefs = { ...state.prefs, panelOrder: next };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs };
      break;
    }
    case 'SET_DRAG_PREVIEW': {
      // UI-only state; do NOT persist to localStorage. The actual prefs.panelOrder
      // stays untouched until COMMIT_DRAG_PREVIEW (or is discarded via CLEAR).
      state = { ...state, dragPreviewOrder: action.order };
      break;
    }
    case 'COMMIT_DRAG_PREVIEW': {
      const preview = state.dragPreviewOrder;
      if (!preview) break;
      const prefs = { ...state.prefs, panelOrder: preview };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs, dragPreviewOrder: null };
      break;
    }
    case 'CLEAR_DRAG_PREVIEW': {
      if (state.dragPreviewOrder == null) break;
      state = { ...state, dragPreviewOrder: null };
      break;
    }
    case 'SWAP_PANELS': {
      const { index, direction } = action;
      const order = state.prefs.panelOrder;
      const target = index + direction;
      if (target < 0 || target >= order.length) break;
      if (index < 0 || index >= order.length) break;
      const next = order.slice();
      [next[index], next[target]] = [next[target], next[index]];
      const prefs = { ...state.prefs, panelOrder: next };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs };
      break;
    }
    case 'MARK_ONBOARDING_TOAST_SEEN': {
      if (state.prefs.onboardingToastSeen) break;
      const prefs = { ...state.prefs, onboardingToastSeen: true };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs };
      break;
    }
    case 'MARK_MOBILE_ONBOARDING_TOAST_SEEN': {
      if (state.prefs.mobileOnboardingToastSeen) break;
      const prefs = { ...state.prefs, mobileOnboardingToastSeen: true };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs };
      break;
    }
    case 'SET_SYNC_ERROR_FLOOR':
      state = { ...state, syncErrorFloorUntil: action.untilMs };
      break;
    case 'SET_SYNC_BUSY':
      state = { ...state, syncBusy: action.busy };
      break;
    case 'SET_SYNC_SUCCESS_FLASH':
      state = { ...state, syncSuccessFlashUntil: action.untilMs };
      break;
    case 'SHOW_STATUS_TOAST':
      state = { ...state, toast: { kind: 'status', text: action.text } };
      break;
    case 'SHOW_ALERT_TOAST':
      state = { ...state, toast: { kind: 'alert', payload: action.alert } };
      break;
    case 'HIDE_TOAST': {
      // If we just hid an alert toast and the queue has more, promote
      // the next one. Otherwise clear toast and leave queue alone (it
      // may already be empty for status toasts; defensive in any case).
      const wasAlert = state.toast?.kind === 'alert';
      if (wasAlert && state.alertToastQueue.length > 0) {
        const [next, ...rest] = state.alertToastQueue;
        state = {
          ...state,
          toast: { kind: 'alert', payload: next },
          alertToastQueue: rest,
        };
      } else {
        state = { ...state, toast: null };
      }
      break;
    }
    case 'INGEST_SNAPSHOT_ALERTS': {
      // Forward-only "already seen" rule (spec §4.3): cold-start unions
      // every alert id into seenAlertIds without surfacing a toast (the
      // user has already seen these in a prior session). The cold-start
      // branch ALSO clears alertToastQueue so a reconnect after a drop
      // can't replay an old queue's worth of alerts that already fired
      // pre-drop.
      //
      // Steady-state: every alert NOT in seenAlertIds is "fresh." All
      // fresh alerts are marked seen this tick AND queued/surfaced —
      // the head of `fresh` becomes the toast (or extends the existing
      // toast's queue if a toast is already showing), and the rest pile
      // onto `alertToastQueue`. HIDE_TOAST drains the queue head-first.
      //
      // Why a queue (rather than the prior "surface one, leave the
      // rest unseen for next tick"): under `--no-sync` (or any quiet
      // stretch with no further SSE ticks), the "next tick" never
      // arrives, so co-arriving alerts on a multi-threshold-jump tick
      // (e.g. 88→96 crossing 90 and 95 in one snapshot) were buried —
      // visible in the panel but never flashed as toasts.
      //
      // alertsSettings is replaced wholesale every tick — server is
      // the source of truth (spec §3.3); a flip in the user's config
      // (cctally config set alerts.enabled false, or another tab's
      // Save) flows through this reducer, NOT through SettingsOverlay
      // local state.
      const seen = new Set(state.seenAlertIds);
      if (action.isFirstTick) {
        for (const a of action.alerts) seen.add(a.id);
        state = {
          ...state,
          alerts: action.alerts,
          seenAlertIds: seen,
          alertsConfig: action.alertsSettings,
          alertToastQueue: [],
        };
        break;
      }
      const allFresh = action.alerts.filter((a) => !seen.has(a.id));
      for (const a of allFresh) seen.add(a.id);

      let toast = state.toast;
      let queue = state.alertToastQueue;
      if (allFresh.length > 0) {
        if (!toast || toast.kind !== 'alert') {
          // No alert currently showing (no toast or a status toast):
          // surface the head and queue the rest. A status toast in
          // flight is replaced by the alert (preserving the legacy
          // behavior where a fresh alert preempts status messages).
          toast = { kind: 'alert', payload: allFresh[0] };
          queue = [...queue, ...allFresh.slice(1)];
        } else {
          // An alert is already on screen — append every fresh entry
          // to the tail; HIDE_TOAST will promote them one at a time.
          queue = [...queue, ...allFresh];
        }
      }
      state = {
        ...state,
        alerts: action.alerts,
        seenAlertIds: seen,
        alertsConfig: action.alertsSettings,
        alertToastQueue: queue,
        toast,
      };
      break;
    }
    case 'SET_TABLE_SORT': {
      const key = action.table === 'trend' ? 'trendSortOverride' : 'sessionsSortOverride';
      const prefs = { ...state.prefs, [key]: action.override };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      const next = { ...state, prefs };
      // Header-click sort reorders rendered rows; indices must follow.
      state = { ...next, ..._recomputeSearch(next) };
      break;
    }
    case 'CLEAR_TABLE_SORTS': {
      const prefs = {
        ...state.prefs,
        trendSortOverride: null,
        sessionsSortOverride: null,
      };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      const next = { ...state, prefs };
      // Clearing sessionsSortOverride may reorder rendered rows; recompute matches.
      state = { ...next, ..._recomputeSearch(next) };
      break;
    }
    case 'OPEN_UPDATE_MODAL':
      state = { ...state, update: { ...state.update, modalOpen: true } };
      break;
    case 'CLOSE_UPDATE_MODAL':
      // Closing while running is non-aborting (server keeps installing
      // and execvp's regardless of whether anyone is watching). The
      // modalOpen flag flips false but status/stream/runId persist so a
      // re-open finds the in-flight run.
      state = { ...state, update: { ...state.update, modalOpen: false } };
      break;
    case 'SET_UPDATE_STATE':
      state = {
        ...state,
        update: {
          ...state.update,
          state: action.state,
          suppress: action.suppress,
        },
      };
      break;
    case 'SET_UPDATE_STATUS':
      state = {
        ...state,
        update: {
          ...state.update,
          status: action.status,
          errorMessage:
            action.errorMessage !== undefined
              ? action.errorMessage
              : state.update.errorMessage,
        },
      };
      break;
    case 'SET_UPDATE_RUN_ID':
      state = {
        ...state,
        update: {
          ...state.update,
          runId: action.runId,
          startedAt:
            action.startedAt !== undefined
              ? action.startedAt
              : state.update.startedAt,
        },
      };
      break;
    case 'APPEND_UPDATE_STREAM':
      state = {
        ...state,
        update: {
          ...state.update,
          stream: [...state.update.stream, action.event],
        },
      };
      break;
    case 'RESET_UPDATE_RUN':
      state = {
        ...state,
        update: {
          ...state.update,
          status: 'idle',
          runId: null,
          stream: [],
          errorMessage: null,
          startedAt: null,
        },
      };
      break;
    // Share v2 (spec §6.1). The four cases delegate the {shareModal,
    // composerModal} slot updates to shareReducer (a pure function in
    // shareSlice.ts) and lift the returned subset onto the master
    // state. Mirrors the OPEN_MODAL / CLOSE_MODAL flow above: emit() at
    // the bottom of dispatch picks up the new state for subscribers.
    case 'OPEN_SHARE':
    case 'CLOSE_SHARE':
    case 'OPEN_COMPOSER':
    case 'CLOSE_COMPOSER': {
      const slice = shareReducer(
        { shareModal: state.shareModal, composerModal: state.composerModal },
        action,
      );
      state = { ...state, ...slice };
      break;
    }
  }
  emit();
}

// ----- Test-only exports -----
export function _resetForTests(): void {
  state = loadInitial();
  lastGeneratedAt = '';
  subscribers.clear();
}
export function loadInitialForTests(): UIState { return loadInitial(); }
