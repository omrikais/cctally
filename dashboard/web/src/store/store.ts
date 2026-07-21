import type { AlertEntry, DashboardSelection, Envelope, SessionRow, SourceAlertRow, SourceName } from '../types/envelope';
import type { SourceResource } from '../hooks/useSourceDetail';
import { seedFormsForRow, toastAlertId } from '../lib/alertIdentity';
import {
  conversationRefKey,
  isConversationRef,
  legacyClaudeConversationRef,
  sameConversationRef,
  type ConversationFilters,
  type ConversationJump,
  type ConversationRef,
  type RailSortKey,
  type SearchKind,
} from '../types/conversation';
import type { FocusMode } from '../conversations/applyFocusMode';
import { EMPTY_FILTERS } from '../types/conversation';
import { recordReadingPos } from './readingPosition';
import { loadBookmarks, setBookmarkNote, toggleBookmark } from './bookmarks';
import type { SessionBookmarks } from './bookmarks';
import { loadRailPrefs, saveRailPrefs } from './conversationRailPrefs';
import { clampOutlineWidth, loadOutlineWidth, saveOutlineWidth } from './outlineWidth';
import {
  computeSearchMatches,
  ctxFromEnvelope,
  applySessionFilter,
  sessionComparator,
} from './selectors';
import { DEFAULT_PANEL_ORDER, CARD_LAYOUT, type GridPanelId } from '../lib/panelIds';
import {
  applyPanelOrderMigration,
  CURRENT_PANEL_ORDER_SCHEMA_VERSION,
  reconcilePanelOrder,
} from '../lib/reconcilePanelOrder';
import { applyTableSort, coerceSortOverride, type SortOverride } from '../lib/tableSort';
import { ALL_SESSIONS_COLUMNS } from '../lib/sessionsColumns';
import {
  initialShareState,
  shareReducer,
  type ComposerModalState,
  type ShareAction,
  type ShareModalState,
} from './shareSlice';
import {
  basketReducer,
  loadBasketFromStorage,
  saveBasketToStorage,
  type BasketAction,
  type BasketSlice,
} from './basketSlice';
import { loadActiveSource, saveActiveSource } from './sourcePrefs';
import { resolveSourceView } from './sourceView';
import { deriveVisiblePanelOrder, mapVisibleReorderToFull } from '../lib/visiblePanelOrder';
import {
  applySourceSessionFilter,
  adaptClaudeSessionRows,
  collectSourceSessionRows,
  computeSourceSessionMatches,
  type SessionDisplayRow,
} from '../lib/sourceRows';
import {
  sourceRecencyDescCompare,
  sourceSessionsColumns,
} from '../lib/sourceSessionsColumns';

export type { ShareModalState, ComposerModalState } from './shareSlice';

export type { SortOverride } from '../lib/tableSort';

// localStorage keys retain the legacy `ccusage.*` namespace so the
// cctally rename doesn't reset existing users' panel/filter prefs.
// Renaming would require migration logic; not worth the disruption.
const PREFS_KEY = 'ccusage.dashboard.prefs';
const FILTER_KEY = 'ccusage.dashboard.filter';
const LEGACY_SORT_KEY = 'ccusage.dashboard.sort'; // retired — migrated on first load
// #177 S5 — the conversation outline panel's open/closed state. New surface, so
// it adopts the `cctally.*` namespace (NOT the legacy `ccusage.*` panel-prefs
// blob). Read in loadInitial with try/catch, persisted on TOGGLE_CONV_OUTLINE.
const CONV_OUTLINE_OPEN_KEY = 'cctally.conv.outlineOpen';

// S2 (#264): the single 'history' kind is un-collapsed back into the three
// per-period modal kinds 'daily' | 'weekly' | 'monthly' (PeriodModal), each
// opened by its own card. No Day·Week·Month toggle.
export type ModalKind = 'current-week' | 'forecast' | 'trend' | 'session' | 'block' | 'daily' | 'weekly' | 'monthly' | 'alerts' | 'update' | 'projects' | 'cache-report';

// #217 S6 F4 — 'note' added: the per-turn bookmark note editor sets inputMode
// 'note' on focus / null on blur+close so the reader's hotkey guard (which gates
// on inputMode === null) suppresses j/k/i/t/… while the user is typing a note.
export type InputMode = null | 'filter' | 'search' | 'note';

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

// ---------- Doctor subcommand (spec §6 — dashboard chip + modal) -----
//
// `DoctorAggregate` mirrors the SSE envelope's `doctor` block. The
// Python emits this slim payload on every tick (~120 bytes); the full
// per-check report is fetched lazily via `GET /api/doctor` (see
// `useDoctorReport` in hooks/). `fingerprint` is the identity-slice
// hash from the kernel (`_lib_doctor.fingerprint`), so two ticks with
// the same severity/counts/check-ids collapse to one fetch even when
// the rendered summaries (and ages baked into `details`) tick over.
// `_error` is present iff the gather raised inside the snapshot
// pipeline — the Python emits a synthetic-FAIL aggregate with
// counts={ok:0,warn:0,fail:1} in that case so the chip still surfaces
// the failure rather than silently disappearing.
export interface DoctorAggregate {
  severity: 'ok' | 'warn' | 'fail';
  counts: { ok: number; warn: number; fail: number };
  generated_at: string;
  fingerprint: string;
  // Present iff the server-side gather raised. The chip surfaces the
  // synthetic-FAIL aggregate identically; this field is informational.
  _error?: string;
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
  // #248 — grid cards only (GridPanelId). 'current-week' left the grid (it is
  // the HeroStrip); the v2→v3 schema migration filters it out of saved orders.
  panelOrder: GridPanelId[];
  onboardingToastSeen: boolean;
  mobileOnboardingToastSeen: boolean;
  trendSortOverride: SortOverride | null;
  sessionsSortOverride: SortOverride | null;
  // Projects modal prefs (spec §5.2 + §7.3). `projectsWindowWeeks`
  // controls both the trend chart window AND the share artifact window
  // when the modal share affordance fires (ShareModalState.params slot).
  // `projectsTrendYMode` swaps between absolute USD ($/wk) and relative
  // share-of-week (%). `projectsSortOverride` mirrors the trend/sessions
  // pattern for the modal's project table.
  // `panelOrderSchemaVersion` is the localStorage migration cursor: 1 →
  // pre-projects schema (9 default panels), 2 → projects spliced at
  // canonical index 4. Bumped by `applyPanelOrderMigration` on first
  // load post-upgrade; tracked in prefs (not a separate localStorage
  // key) so it persists through `RESET_PREFS` re-seeds with the rest of
  // the prefs blob.
  projectsWindowWeeks: 1 | 4 | 8 | 12;
  projectsTrendYMode: 'share' | 'absolute';
  projectsSortOverride: SortOverride | null;
  panelOrderSchemaVersion: number;
  // S2 (#264): the shared sort override for the Weekly/Monthly period tables
  // (the `table: 'history'` sort key is retained — renaming it is churn for no
  // behavior change). Mirrors the trend/sessions/projects pattern; coerced on
  // load (invalid → null). The former `historyPeriod` toggle pref is gone; a
  // stale key left in a user's saved prefs rides along harmlessly (never read).
  historySortOverride: SortOverride | null;
}

// Toast variant pattern (T8). The `status` shape is the legacy
// transient message; the `alert` shape carries the full AlertEntry so
// the toast can render threshold + axis chip + context-specific body
// without a second store read.
export type ToastState =
  | { kind: 'status'; text: string }
  // #294 S5 §6.7 — the alert toast payload is EITHER a legacy AlertEntry (from
  // SHOW_ALERT_TOAST / the legacy INGEST_SNAPSHOT_ALERTS path) OR a
  // source-qualified row (from the new INGEST_SOURCE_ALERTS pipeline). The Toast
  // normalizes both to a source row at render time and shows a source chip; the
  // legacy reducers keep RAW AlertEntry payloads so their existing store tests
  // stay byte-stable.
  | { kind: 'alert'; payload: AlertEntry | SourceAlertRow }
  | null;

// Mirrors the Python envelope's alerts_settings block; lets
// SettingsOverlay seed without a separate GET.
export interface AlertsConfig {
  enabled: boolean;
  weekly_thresholds: number[];
  five_hour_thresholds: number[];
  // Budget is its own config block (issue #19); the envelope flattens
  // its thresholds + enabled-reflection into alerts_settings.
  budget_thresholds: number[];
  budget_enabled?: boolean;
  // Projected axis (issue #121): the two opt-in toggles mirrored from the
  // envelope's alerts_settings block. Both default false.
  projected_weekly_enabled?: boolean;
  projected_budget_enabled?: boolean;
  // Per-project budget axis (issue #19/#121): single opt-in toggle mirrored
  // from the envelope's alerts_settings block. Defaults false.
  project_alerts_enabled?: boolean;
  // Codex budget toggles (#134): mirrored from the persisted `budget.codex`
  // block. `codex_budget_configured` gates the two dashboard-writable
  // toggles (disabled-with-hint when no Codex budget exists); the two
  // `*_enabled` fields seed them. All default false.
  codex_budget_configured?: boolean;
  codex_budget_alerts_enabled?: boolean;
  codex_projected_enabled?: boolean;
  // Notifier dispatch backend (Phase B): mirrored from the envelope so
  // SettingsOverlay can seed the dropdown. Optional; defaults to 'auto'.
  // The raw `command_template` is NEVER mirrored — only `command_configured`
  // (a boolean) reaches the client.
  notifier?: 'auto' | 'osascript' | 'notify-send' | 'command' | 'none';
  command_configured?: boolean;
}

// cache-failure-markers spec §5 — the dashboard-scoped prefs mirror, a named
// slice following the alertsConfig pattern (there is no generic settings-
// selector convention; settings live as named slices). Replaced wholesale each
// SSE tick by INGEST_DASHBOARD_PREFS (server is the source of truth). The store
// keeps the raw nullable field; `selectMarkersEnabled` does the opt-out
// defaulting so every consumer reads one source instead of re-deriving.
export interface DashboardPrefs {
  // Conversation-viewer cache-rebuild marker opt-out. Undefined when the field
  // is absent on the wire (older server / first tick) — the selector treats
  // absence as ON (opt-out, default true).
  cache_failure_markers?: boolean;
  // Conversation-viewer live-tail opt-out (live-tail spec §4.2). Undefined when
  // absent on the wire — the selector treats absence as ON (opt-out, default
  // true).
  live_tail?: boolean;
}

export interface UIState {
  snapshot: Envelope | null;
  // #294 S5 — the global Claude / Codex / All source selection. Seeded from
  // localStorage (`cctally:dashboard:source`) in loadInitial and persisted on
  // every SET_ACTIVE_SOURCE that changes it. Purely a client re-selection over
  // the already-delivered `sources` bundle: the store NEVER waits for or
  // reconciles this against an envelope (§5.1).
  activeSource: DashboardSelection;
  // Conversation viewer (spec §4). Top-level view mode + the small
  // cross-cutting reader/search state. Fetched list/reader DATA lives in
  // hook state, not here (mirrors useProjectDetail). None of these persist
  // — a reload always lands on the dashboard.
  view: 'dashboard' | 'conversations';
  selectedConversationId: string | null;
  // #321 Task A — authoritative source-qualified selection. The legacy string
  // mirror remains temporarily for stored/test compatibility only; production
  // conversation code consumes this value.
  selectedConversationRef: ConversationRef | null;
  conversationSearch: string;
  // #177 S6 — single-select kind facet for the rail search chips. Resets to
  // 'all' whenever the needle is cleared (SET_CONVERSATION_SEARCH with '').
  conversationSearchKind: SearchKind;
  conversationJump: ConversationJump | null;
  // #177 S5 — outline panel. `convOutlineOpen` is the only persisted bit of
  // conversation UI state (localStorage `cctally.conv.outlineOpen`, default
  // true on desktop); `convFocusMode` + `convCurrentTurnUuid` are transient
  // per-session and reset on every conversation switch.
  convOutlineOpen: boolean;
  // #205 S1 — EPHEMERAL mobile outline-sheet open flag. NOT persisted (unlike
  // convOutlineOpen): a fresh mobile conversation open must never auto-bury the
  // transcript. Reset to false on a genuine conversation switch; toggled only
  // by the reader's ☰ / `o` on mobile.
  convOutlineMobileOpen: boolean;
  // #217 S3 E6(b) — the resizable outline column WIDTH (px), persisted to
  // localStorage `cctally.conv.outlineWidth` (clamped [MIN, MAX]; default 290 =
  // today's track ceiling so an un-resized panel is byte-identical). Drives the
  // 3rd grid track of `.conv-view--outline` via a CSS custom property.
  convOutlineWidth: number;
  // #217 S5 E4 — the focus axis now spans the four primary modes PLUS the "▾
  // More" modes (`edits`/`bash`/`subagent:<key>`); the slice stays one string
  // (single-select). Transient per-session — reset to 'all' on a genuine switch.
  convFocusMode: FocusMode;
  // #217 S5 F2 — the outline panel's [Outline] [Files] tab selection. Transient
  // per-session — reset to 'outline' on a genuine switch (mirrors convFocusMode).
  convOutlineTab: 'outline' | 'files';
  convCurrentTurnUuid: string | null;
  // #188 S2 — the EXPLICIT-selection pin (always a real uuid; the bucket-root
  // uuid for a subagent). Set by the jump effect when a jump lands; cleared by
  // explicit user navigation (wheel/touch/scroll-keys + j/k/g/jump-to-top). It
  // takes precedence over `convCurrentTurnUuid` for aria-current + the
  // jump-to-next cursor, so an outline click selects exactly what was clicked
  // (Bug 2 ≡ #187) rather than the scroll-sync topmost-visible turn (which sits
  // above a centered target). Transient: reset on a genuine session switch.
  convPinnedUuid: string | null;
  // #217 S6 F4 — the CURRENT conversation's bookmarks (uuid → { note, ts }), UI
  // state in the same family as convPinnedUuid / convFocusMode. Hydrated from
  // localStorage (loadBookmarks) on BOTH selection actions (OPEN_CONVERSATION +
  // SELECT_CONVERSATION) and cleared to {} when the selection is null; mutated by
  // TOGGLE_BOOKMARK / SET_BOOKMARK_NOTE (which also write through to localStorage).
  convBookmarks: SessionBookmarks;
  // #177 S6 — the floating in-conversation find bar (Cmd+F style). Transient:
  // opened by '/' over an open reader, closed on Esc / its ✕ / a genuine
  // session switch. Never persists.
  convFindOpen: boolean;
  // Browse-list filters (filters spec §4). `conversationFilters` is the active
  // filter set threaded into the /api/conversations AND /api/conversation/search
  // query strings (#217 S4 / I-2.5 — filters now apply to BOTH browse and
  // search); `convFiltersOpen` is the Filters popover's open flag. #217 S4 / I-2.2
  // flipped the prior session-only behavior: the filters AND the rail sort now
  // PERSIST across reload as one `cctally.conv.railPrefs` blob (seeded in
  // loadInitial, written on SET/CLEAR + SET_CONVERSATION_RAIL_SORT).
  // `convFiltersOpen` is also a named-key guard: the reader's `End`/jump binding
  // (Task 4) gates on it so typing in a filter input never fires reader
  // navigation.
  conversationFilters: ConversationFilters;
  convFiltersOpen: boolean;
  // #217 S4 / I-2 — the rail sort key (Recent/Oldest/Cost/Messages/Project),
  // threaded into the /api/conversations `sort` param. Persisted alongside the
  // filters in the railPrefs blob; default 'recent'.
  conversationRailSort: RailSortKey;
  // #217 S7 F10 — session comparison. `compare` holds the two session ids being
  // diffed side-by-side (null = single-session mode); `comparePick` is the
  // transient rail pick-mode (the user clicked "Compare with…" and is choosing
  // the other side — `anchor` is the session already selected). Neither persists
  // — a reload lands on the dashboard. The reverse-clear discipline (below) wipes
  // both whenever a single-session action runs (OPEN_/SELECT_CONVERSATION,
  // SET_VIEW) so a stale comparison can never linger behind the reader.
  compare: { a: ConversationRef; b: ConversationRef } | null;
  comparePick: { anchor: ConversationRef } | null;
  // #228 S1 (F3) — one-shot: set by CLOSE_COMPARE, consumed+cleared by the
  // reader's focus-on-ready effect to return focus to #conv-compare-with.
  compareCloseFocusPending: boolean;
  // #227 — accumulating session_id → title cache, populated by the rail's browse
  // fetch (useConversations) as pages land. Read by ComparisonView so its header
  // can prefer the real derived title over the `Session <slug>` fallback without
  // fetching the browse list itself (which would worsen the #227 outline-refetch
  // churn). Accumulate-only (never shrinks); not persisted.
  conversationTitles: Record<string, string>;
  openModal: ModalKind | null;
  // Source captured when a legacy panel modal opens. Source switches update the
  // board only; an already-open modal and any share action it launches remain
  // bound to the initiating selection.
  openModalSource: DashboardSelection | null;
  openSessionId: string | null;
  openBlockStartAt: string | null;
  openDailyDate: string | null;
  // Spec §4.1 — when OPEN_MODAL { kind: 'projects' } carries a
  // projectKey, the modal opens with that project's detail pre-expanded.
  // Null on un-targeted opens (panel chrome click / '0' keybinding).
  openProjectKey: string | null;
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
  // cache-failure-markers spec §5 — snapshot-mirrored dashboard prefs (the
  // `dashboard_prefs` envelope block). Replaced wholesale each tick by
  // INGEST_DASHBOARD_PREFS; read via `selectMarkersEnabled`. Seeds before the
  // first tick to `{}` so the selector defaults markers ON.
  dashboardPrefs: DashboardPrefs;
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
  // #294 S5 §6.7 — carries either legacy AlertEntry rows (legacy path) or
  // source-qualified rows (new pipeline) so a Codex toast can queue beside a
  // Claude one; the Toast normalizes both at render.
  alertToastQueue: Array<AlertEntry | SourceAlertRow>;
  // UI-only preview of panelOrder during an in-flight drag. While set,
  // App.tsx renders this order (FLIP animates) but prefs.panelOrder
  // remains untouched. Committed to prefs on drop (COMMIT_DRAG_PREVIEW)
  // or discarded on Esc / pointer-cancel / window-resize
  // (CLEAR_DRAG_PREVIEW). Never persisted to localStorage directly.
  dragPreviewOrder: GridPanelId[] | null;
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
  // Share v2 basket (spec §7). Recipe-only ordered list — never
  // contains rendered bodies. Lifecycle is independent of `shareModal`
  // (basket persists across the share modal closing/opening). Hydrated
  // from localStorage on init; persisted on every mutation by the
  // master dispatch wrapper below. State shape + pure reducer live in
  // basketSlice.ts.
  basket: BasketSlice;
  // Doctor subcommand (spec §6). Aggregate-only block written by the
  // SSE ingest each tick (full per-check report fetched lazily via GET
  // /api/doctor by the DoctorModal). `null` until the first envelope
  // arrives, then a `DoctorAggregate`. `doctorModalOpen` is part of
  // the store (NOT `openModal`) so the `d` keymap's composite guard
  // can read it alongside `update.modalOpen` + `inputMode` per spec
  // §6.4 (Codex M5), and so the doctor modal can layer above panel
  // modals if a future iteration wants that pattern.
  doctor: DoctorAggregate | null;
  doctorModalOpen: boolean;
  // #248 §6 — TRANSIENT mobile sticky-collapse flag. True once the hero block
  // has scrolled out of the viewport (set by HeroStrip's mobile-only
  // IntersectionObserver), which reveals the Header's condensed Used%/reset
  // readout. NOT persisted — a reload always lands with the hero in view.
  heroScrolled: boolean;
  /** Depth counter of open component-local chrome overlays (Settings/Help).
   *  Lets the global/sessions key guards suppress hotkeys under those
   *  overlays, which are NOT in `openModal`/`doctorModalOpen` (#207 D2). */
  chromeOverlayOpen: number;
  // #294 S5 §5.6 — the open qualified source-detail request (Codex/All source
  // rows). Carries {source, resource, key}; the SourceDetailModal fetches the
  // qualified route via useSourceDetail. Null when closed. Separate from the
  // legacy `openSessionId`/`openBlockStartAt` fields (which stay the Claude
  // legacy-route path).
  openSourceDetail: { source: SourceName; resource: SourceResource; key: string } | null;
  // Dashboard selection captured when the qualified detail opens. This can be
  // `all` even though the route itself is provider-qualified, and keeps a
  // modal-launched share action stable across a later board source switch.
  openSourceDetailSelection: DashboardSelection | null;
  // #294 S5 §6.3 — the header-click sort override for the source-aware Sessions
  // grid (Codex + All), over the SOURCE_SESSIONS_COLUMNS set (recency / label /
  // total / cost). TRANSIENT (not persisted — no new config key): defaults to
  // null, which renders the native default sort (last_activity desc). Distinct
  // from the Claude `prefs.sessionsSortOverride` so a switch never cross-binds
  // the two grids' sort state.
  sourceSessionsSort: SortOverride | null;
}

export function defaultPrefs(): Prefs {
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
    projectsWindowWeeks: 4,
    projectsTrendYMode: 'absolute',
    projectsSortOverride: null,
    // Pre-migration baseline — bumps to 2 in `loadInitial` once
    // `applyPanelOrderMigration` runs on first load post-upgrade.
    panelOrderSchemaVersion: 1,
    // S8 (#254): first-open period is Day (matches the heatmap card +
    // its per-day deep-link); no table sort override by default.
    historySortOverride: null,
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
  return {
    enabled: false,
    weekly_thresholds: [90, 95],
    five_hour_thresholds: [90, 95],
    budget_thresholds: [90, 100],
    budget_enabled: false,
    projected_weekly_enabled: false,
    projected_budget_enabled: false,
    project_alerts_enabled: false,
    codex_budget_configured: false,
    codex_budget_alerts_enabled: false,
    codex_projected_enabled: false,
  };
}

function loadInitial(): UIState {
  let prefs = defaultPrefs();
  const rawPrefs = localStorage.getItem(PREFS_KEY);
  const legacySort = localStorage.getItem(LEGACY_SORT_KEY);
  if (rawPrefs) {
    try {
      const parsed = JSON.parse(rawPrefs) as Partial<Prefs>;
      prefs = { ...defaultPrefs(), ...parsed };
      // Panel-order migration runs BEFORE reconcile — see
      // applyPanelOrderMigration's doc for the ordering rationale.
      // A first-time v1 user has parsed.panelOrderSchemaVersion === undefined,
      // which coerces to NaN through Number() — clamp to 1.
      const rawVersion = typeof parsed.panelOrderSchemaVersion === 'number'
        ? parsed.panelOrderSchemaVersion
        : 1;
      const migrated = applyPanelOrderMigration(parsed.panelOrder ?? null, rawVersion);
      prefs.panelOrderSchemaVersion = migrated.newVersion;
      prefs.panelOrder = reconcilePanelOrder(migrated.panels, DEFAULT_PANEL_ORDER);
      prefs.trendSortOverride = coerceSortOverride(prefs.trendSortOverride ?? null);
      prefs.sessionsSortOverride = coerceSortOverride(prefs.sessionsSortOverride ?? null);
      prefs.projectsSortOverride = coerceSortOverride(prefs.projectsSortOverride ?? null);
      // S2 (#264): coerce the shared Weekly/Monthly table sort override
      // defensively (invalid persisted value → null). A retired `historyPeriod`
      // key left in saved prefs is tolerated — never read, no strip pass.
      prefs.historySortOverride = coerceSortOverride(prefs.historySortOverride ?? null);
      // Persist the cursor advancement immediately so a tab refresh
      // doesn't re-run the migration on every load.
      if (rawVersion !== migrated.newVersion) {
        localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      }
    } catch {
      prefs = defaultPrefs();
      // Brand-new user (or corrupt prefs) — schema is at CURRENT.
      prefs.panelOrderSchemaVersion = CURRENT_PANEL_ORDER_SCHEMA_VERSION;
    }
    // Retire the legacy key unconditionally when prefs exists — prefs wins.
    if (legacySort != null) localStorage.removeItem(LEGACY_SORT_KEY);
  } else if (legacySort) {
    // One-time migration: no prefs yet, legacy sort exists → write prefs
    // with that sortDefault and delete the legacy key. No panel-order
    // migration to run (no saved order yet); schema cursor lands at
    // CURRENT directly so a future v3+ migration sees a sane starting
    // point.
    prefs = {
      ...defaultPrefs(),
      sortDefault: legacySort as SessionSortKey,
      panelOrderSchemaVersion: CURRENT_PANEL_ORDER_SCHEMA_VERSION,
    };
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    localStorage.removeItem(LEGACY_SORT_KEY);
  } else {
    // Brand-new user, no prefs, no legacy sort — schema is at CURRENT.
    prefs.panelOrderSchemaVersion = CURRENT_PANEL_ORDER_SCHEMA_VERSION;
  }
  // #177 S5 — read the persisted outline-open pref; default true (desktop
  // open). try/catch mirrors the prefs initializer above: a throwing or
  // corrupt localStorage falls back to the default rather than crashing init.
  let convOutlineOpen = true;
  try {
    const rawOutline = localStorage.getItem(CONV_OUTLINE_OPEN_KEY);
    if (rawOutline === 'false') convOutlineOpen = false;
    else if (rawOutline === 'true') convOutlineOpen = true;
  } catch {
    convOutlineOpen = true;
  }
  // #217 S4 / I-2.2 — seed the persisted rail discovery prefs (filters + sort).
  // loadRailPrefs is itself try/catch-guarded and falls back to EMPTY_FILTERS +
  // 'recent' on a corrupt/absent blob, so this never throws during init.
  const railPrefs = loadRailPrefs();
  return {
    snapshot: null,
    // #294 S5 — seed the persisted source selection (invalid/missing → claude).
    activeSource: loadActiveSource(),
    view: 'dashboard',
    selectedConversationId: null,
    selectedConversationRef: null,
    conversationSearch: '',
    conversationSearchKind: 'all',
    conversationJump: null,
    convOutlineOpen,
    convOutlineMobileOpen: false,
    // #217 S3 E6(b) — seed the persisted outline width (clamped; default 290).
    convOutlineWidth: loadOutlineWidth(),
    convFocusMode: 'all',
    convOutlineTab: 'outline',
    convCurrentTurnUuid: null,
    convPinnedUuid: null,
    // #217 S6 F4 — no conversation selected at init → no bookmarks hydrated.
    convBookmarks: {},
    convFindOpen: false,
    conversationFilters: railPrefs.filters,
    convFiltersOpen: false,
    conversationRailSort: railPrefs.sort,
    // #217 S7 F10 — no comparison / pick in flight at init.
    compare: null,
    comparePick: null,
    // #228 S1 (F3) — no focus-return pending at init.
    compareCloseFocusPending: false,
    // #227 — empty title cache; filled lazily as the rail browse list loads.
    conversationTitles: {},
    openModal: null,
    openModalSource: null,
    openSessionId: null,
    openBlockStartAt: null,
    openDailyDate: null,
    openProjectKey: null,
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
    // Empty before the first tick → markers default ON (opt-out).
    dashboardPrefs: {},
    alertToastQueue: [],
    dragPreviewOrder: null,
    update: defaultUpdateSlice(),
    ...initialShareState,
    basket: { items: loadBasketFromStorage(), rejectedReason: null },
    doctor: null,
    doctorModalOpen: false,
    // #248 §6 — transient; starts with the hero in view (not scrolled).
    heroScrolled: false,
    chromeOverlayOpen: 0,
    openSourceDetail: null,
    openSourceDetailSelection: null,
    // #294 S5 — transient; the source-grid sort defaults to native recency desc.
    sourceSessionsSort: null,
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

// cache-failure-markers spec §5 — the single defaulting path for the
// conversation-viewer cache-rebuild marker opt-out. Reads the snapshot-mirrored
// `dashboardPrefs` slice and treats an UNDEFINED `cache_failure_markers` as ON
// (opt-out, default true): an older server / a first tick before bootstrap
// reads as markers-on. MessageItem, OutlinePanel, and deriveOutline all read
// THIS so none of them re-invents the defaulting.
export function selectMarkersEnabled(s: UIState = state): boolean {
  return s.dashboardPrefs.cache_failure_markers !== false;
}

// live-tail spec §4.2 — the single defaulting path for the conversation-viewer
// live-tail opt-out. Reads the snapshot-mirrored `dashboardPrefs` slice and
// treats an UNDEFINED `live_tail` as ON (opt-out, default true): an older
// server / a first tick before bootstrap reads as live-tail-on. useConversation
// reads THIS to decide whether to open the dedicated per-conversation
// EventSource, so the defaulting lives in one place.
export function selectLiveTailEnabled(s: UIState = state): boolean {
  return s.dashboardPrefs.live_tail !== false;
}

// a11y focus management (#207 A1) — the highest STORE-TRACKED open focus layer,
// by fixed priority. Drives `trapEnabled` for `useModalFocus`: a panel modal
// suspends its Tab-trap when Share/Composer/Update opens on top of it (panel
// modals stay mounted under the share layer — `ModalRoot` and `ShareModalRoot`
// are siblings — so a trap keyed only on "is a panel modal open" would keep
// yanking focus back into the underlying panel). `SettingsOverlay`/`HelpOverlay`
// track their open-state in component-local React state and are intentionally
// NOT seen here — the `useModalFocus` contains-guard covers them (Settings is
// mutually exclusive with a panel modal, and Help moves focus into itself).
export type StoreFocusLayer = 'composer' | 'share' | 'update' | 'doctor' | 'source-detail' | 'panel' | null;

export function topmostStoreFocusLayer(s: UIState): StoreFocusLayer {
  if (s.composerModal) return 'composer';
  if (s.shareModal) return 'share';
  if (s.update.modalOpen) return 'update';
  if (s.doctorModalOpen) return 'doctor';
  if (s.openSourceDetail != null) return 'source-detail';
  if (s.openModal != null) return 'panel';
  return null;
}

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
    ? applyTableSort(filtered, ALL_SESSIONS_COLUMNS, override)
    : filtered.slice().sort(sessionComparator(s.sessionsSort));
  return sorted.slice(0, s.prefs.sessionsPerPage);
}

// #294 S5 §6.3 — the source-aware parallel of getRenderedRows: the currently-
// visible Sessions display-row list for Codex / All. Codex → the source-native
// rows; All → the two providers' rows CONCATENATED and interleaved by the shared
// recency comparator (each row keeps its own `source` — no merge). filter →
// sort → slice-to-perPage, exactly matching how the source Sessions grid paints,
// so search-match indices align with rendered positions (n/N). Claude mode uses
// the legacy `getRenderedRows` path instead and this returns []. Byte-identical
// Claude output is preserved because `_recomputeSearch` routes Claude away from
// this path entirely.
export function getRenderedSourceRows(s: UIState = state): SessionDisplayRow[] {
  if (s.activeSource === 'claude') {
    return adaptClaudeSessionRows(getRenderedRows(s).map((row) => ({
      key: row.session_id,
      source: 'claude',
      started_utc: row.started_utc,
      duration_min: row.duration_min,
      model: row.model,
      project: row.project,
      project_key: row.project_key,
      cost_usd: row.cost_usd,
      cache_hit_pct: row.cache_hit_pct,
      title: row.title,
    })));
  }
  const view = resolveSourceView(s.snapshot, s.activeSource);
  const rows = collectSourceSessionRows(view);
  const filtered = applySourceSessionFilter(rows, s.filterText);
  const override = s.sourceSessionsSort;
  const sorted = override
    ? applyTableSort(filtered, sourceSessionsColumns({ includeSource: s.activeSource === 'all', oneModel: false }), override)
    : filtered.slice().sort(sourceRecencyDescCompare);
  return sorted.slice(0, s.prefs.sessionsPerPage);
}

// Recompute searchMatches + searchIndex from the currently-visible row
// list. Every reducer branch that changes what's visible (snapshot,
// filter, sort, perPage) — or the needle itself — calls this so the
// match set never goes stale.
function _recomputeSearch(s: UIState): Pick<UIState, 'searchMatches' | 'searchIndex'> {
  if (!s.searchText) return { searchMatches: [], searchIndex: -1 };
  // #294 S5 — route the search haystack + rendered-row list by active source so
  // n/N and the in-cell highlight align with what the visible grid paints.
  // Claude keeps the legacy row+haystack path verbatim (byte-identical); Codex /
  // All match over the source display rows (haystack = label + models).
  const matches = s.activeSource === 'claude'
    ? computeSearchMatches(getRenderedRows(s), s.searchText, ctxFromEnvelope(s.snapshot))
    : computeSourceSessionMatches(getRenderedSourceRows(s), s.searchText);
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
  | { type: 'OPEN_MODAL'; kind: ModalKind; sessionId?: string; blockStartAt?: string; dailyDate?: string; projectKey?: string }
  | { type: 'CLOSE_MODAL' }
  // #294 S5 — the global source selection. Persists to localStorage only when
  // it actually changes (identity-gated, like the basket / outline-width
  // persistence); a same-value dispatch is a no-op (no emit, no write).
  | { type: 'SET_ACTIVE_SOURCE'; source: DashboardSelection }
  // #294 S5 §5.6 — open / close the qualified source-detail modal (Codex/All
  // source rows). The modal fetches `/api/source/<source>/<resource>/<key>`.
  | { type: 'OPEN_SOURCE_DETAIL'; source: SourceName; resource: SourceResource; key: string }
  | { type: 'CLOSE_SOURCE_DETAIL' }
  // #294 S5 §6.3 — header-click sort override for the source Sessions grid
  // (Codex / All). Transient; null restores the native recency-desc default.
  | { type: 'SET_SOURCE_SESSIONS_SORT'; override: SortOverride | null }
  // Conversation viewer (spec §4). View-mode + reader/search cross-cutting
  // state. None persist to localStorage (a reload lands on the dashboard).
  | { type: 'SET_VIEW'; view: 'dashboard' | 'conversations' }
  | { type: 'OPEN_CONVERSATION'; conversationRef?: ConversationRef; sessionId?: string; jump?: ConversationJump }
  | { type: 'SELECT_CONVERSATION'; conversationRef?: ConversationRef | null; sessionId?: string | null }
  | { type: 'SET_CONVERSATION_SEARCH'; text: string }
  // #177 S6 — rail search kind facet (chips). Clearing the needle resets it.
  | { type: 'SET_CONVERSATION_SEARCH_KIND'; kind: SearchKind }
  | { type: 'CLEAR_CONVERSATION_JUMP' }
  // #177 S5 — outline panel. TOGGLE_CONV_OUTLINE flips + persists the open
  // flag; SET_CONV_FOCUS_MODE sets the transient per-session focus mode (Task 4
  // consumer); SET_CONV_CURRENT_TURN is the scroll-sync cursor written by the
  // reader's IntersectionObserver.
  | { type: 'TOGGLE_CONV_OUTLINE' }
  // #217 S3 E6(b) — set + persist the resizable outline column width (clamped).
  | { type: 'SET_CONV_OUTLINE_WIDTH'; px: number }
  // #205 S1 — the ☰ button + `o` key on mobile flip the ephemeral mobile flag
  // (no persistence); the ✕ button + backdrop force it closed.
  | { type: 'TOGGLE_CONV_OUTLINE_MOBILE' }
  | { type: 'CLOSE_CONV_OUTLINE_MOBILE' }
  | { type: 'SET_CONV_FOCUS_MODE'; mode: UIState['convFocusMode'] }
  // #217 S5 F2 — select the outline panel's [Outline] [Files] tab (transient).
  | { type: 'SET_CONV_OUTLINE_TAB'; tab: UIState['convOutlineTab'] }
  | { type: 'SET_CONV_CURRENT_TURN'; uuid: string | null }
  // #188 S2 — the explicit-selection pin. SET_CONV_PINNED_TURN is dispatched by
  // the reader's jump effect when a jump lands; CLEAR_CONV_PIN by explicit user
  // navigation. The pin takes precedence over the scroll-sync cursor for
  // aria-current + jump-to-next (closes #187).
  | { type: 'SET_CONV_PINNED_TURN'; uuid: string }
  | { type: 'CLEAR_CONV_PIN' }
  // #217 S6 F4 — bookmark mutations on the current conversation. Both reducers
  // write through to localStorage (the recordReadingPos write-through pattern)
  // and re-hydrate convBookmarks from the saved map.
  // #217 S6 F4 (review) — optional sessionId targets a specific conversation;
  // when absent the reducer falls back to state.selectedConversationId (the
  // default in-reader path), so existing callers are unchanged.
  | { type: 'TOGGLE_BOOKMARK'; uuid: string; conversationRef?: ConversationRef; sessionId?: string }
  | { type: 'SET_BOOKMARK_NOTE'; uuid: string; note: string; conversationRef?: ConversationRef; sessionId?: string }
  // #177 S6 — the in-conversation find bar open flag.
  | { type: 'OPEN_CONV_FIND' }
  | { type: 'CLOSE_CONV_FIND' }
  // Browse-list filters (filters spec §4). SET merges a partial patch (live-apply
  // of one axis at a time); CLEAR resets to EMPTY_FILTERS; TOGGLE flips the popover
  // open flag; SET_CONV_FILTERS_OPEN sets it explicitly (the popover's "Done"
  // closes it). #217 S4 / I-2.2 — SET/CLEAR now PERSIST the railPrefs blob.
  | { type: 'SET_CONVERSATION_FILTERS'; patch: Partial<ConversationFilters> }
  | { type: 'CLEAR_CONVERSATION_FILTERS' }
  | { type: 'TOGGLE_CONV_FILTERS' }
  | { type: 'SET_CONV_FILTERS_OPEN'; open: boolean }
  // #217 S4 / I-2 — the persisted rail sort key.
  | { type: 'SET_CONVERSATION_RAIL_SORT'; sort: RailSortKey }
  // #217 S7 F10 — session comparison. START/CANCEL drive the rail pick-mode;
  // OPEN_COMPARE enters the comparison (sets the anchor selection + view);
  // SWAP flips the two sides; CLOSE returns to the single-session reader on the
  // anchor. OPEN_COMPARE is a no-op when a===b (a session never compares to
  // itself — the URL boot path routes that to a plain OPEN_CONVERSATION).
  | { type: 'START_COMPARE_PICK'; anchorRef?: ConversationRef; anchor?: string }
  | { type: 'CANCEL_COMPARE_PICK' }
  | { type: 'OPEN_COMPARE'; aRef?: ConversationRef; bRef?: ConversationRef; a?: string; b?: string }
  | { type: 'SWAP_COMPARE' }
  | { type: 'CLOSE_COMPARE' }
  // #228 S1 (F3) — clear the transient focus-return flag once the reader has
  // returned focus to the compare trigger.
  | { type: 'CLEAR_COMPARE_CLOSE_FOCUS' }
  // #227 — merge a batch of [session_id, title] pairs into the title cache. The
  // rail's useConversations dispatches it as pages land; non-empty titles only.
  | { type: 'CACHE_CONVERSATION_TITLES'; titles: Array<[string | ConversationRef, string]> }
  | { type: 'SET_FILTER'; text: string }
  | { type: 'SET_SEARCH'; text: string }
  | { type: 'SET_SEARCH_MATCHES'; matches: number[]; index: number }
  | { type: 'SET_SORT'; key: SessionSortKey }
  | { type: 'SET_INPUT_MODE'; mode: InputMode }
  | { type: 'SAVE_PREFS'; patch: Partial<Prefs> }
  | { type: 'RESET_PREFS' }
  | { type: 'RESET_PANEL_ORDER' }
  | { type: 'REORDER_PANELS'; from: number; to: number }
  | { type: 'SET_DRAG_PREVIEW'; order: GridPanelId[] }
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
  // #294 S5 §6.7 — the source-aware toast pipeline. `rows` is the union of the
  // two provider projections (`sources.claude` + `sources.codex` data.alerts),
  // NOT the legacy top-level array, so a codex_budget row can't double-toast.
  // Seen-state keys off the normalized `toastAlertId` (§6.7), seeding both the
  // normalized and bare legacy forms for continuity. Toasts fire for rows of
  // every source regardless of `activeSource` (an alert is a notification).
  | {
      type: 'INGEST_SOURCE_ALERTS';
      rows: SourceAlertRow[];
      alertsSettings: AlertsConfig;
      isFirstTick: boolean;
    }
  // cache-failure-markers spec §5 — mirror the snapshot's `dashboard_prefs`
  // block into the named slice each tick (the SSE handler dispatches this
  // from ingestDashboardPrefs). Replaced wholesale: the server is the source
  // of truth, so a flip in `dashboard.cache_failure_markers` (CLI or another
  // tab's Save) takes effect on the next tick.
  | { type: 'INGEST_DASHBOARD_PREFS'; prefs: DashboardPrefs }
  | { type: 'SET_TABLE_SORT'; table: 'trend' | 'sessions' | 'projects' | 'history'; override: SortOverride | null }
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
  | ShareAction
  // Share v2 basket (spec §7). BASKET_ADD/REMOVE/REORDER/CLEAR are
  // forwarded to basketReducer (basketSlice.ts); the master dispatch
  // case persists every mutation to localStorage and surfaces the
  // capacity-rejection toast.
  | BasketAction
  // Doctor subcommand (spec §6).
  // SET_DOCTOR_AGGREGATE writes the SSE-mirrored aggregate slice (the
  //   ingest in sse.ts dispatches this each tick when snap.doctor is
  //   present). Missing field → no dispatch (slice stays as last
  //   known good, mirroring the UPDATE slice's tolerance).
  // OPEN_DOCTOR_MODAL / CLOSE_DOCTOR_MODAL mirror the UPDATE_MODAL
  //   precedent — flag-only, no reset of any other slice.
  | { type: 'SET_DOCTOR_AGGREGATE'; doctor: DoctorAggregate | null }
  | { type: 'OPEN_DOCTOR_MODAL' }
  | { type: 'CLOSE_DOCTOR_MODAL' }
  // #248 §6 — mobile sticky-collapse: HeroStrip's IntersectionObserver sets
  // this as the hero block enters/leaves the viewport (transient, not persisted).
  | { type: 'SET_HERO_SCROLLED'; scrolled: boolean }
  | { type: 'INCREMENT_CHROME_OVERLAY' }
  | { type: 'DECREMENT_CHROME_OVERLAY' };

// Transient-modal slots dismissed whenever the top-level workspace switches
// (#158), so no panel modal floats over the destination body. Covers the panel
// modal + its selection fields (mirroring CLOSE_MODAL) and both share v2 slots.
// Both view-entry paths spread this in: SET_VIEW (ViewSwitcher) and
// OPEN_CONVERSATION (the row/rail "open conversation" affordance, which sets
// view='conversations' directly, bypassing SET_VIEW). The keymap's modal/overlay
// Esc staying view-agnostic (#156) is the belt; clearing here is the suspenders.
const DISMISSED_ON_VIEW_SWITCH = {
  openModal: null,
  openModalSource: null,
  openSessionId: null,
  openBlockStartAt: null,
  openDailyDate: null,
  openProjectKey: null,
  shareModal: null,
  composerModal: null,
  // #294 S5 — the qualified source-detail modal is a transient overlay too.
  openSourceDetail: null,
  openSourceDetailSelection: null,
} satisfies Partial<UIState>;

function actionConversationRef(
  ref: ConversationRef | null | undefined,
  legacyId: string | null | undefined,
): ConversationRef | null {
  if (ref !== undefined) return ref;
  return typeof legacyId === 'string' && legacyId ? legacyClaudeConversationRef(legacyId) : null;
}

export function dispatch(action: Action): void {
  switch (action.type) {
    case 'OPEN_MODAL':
      state = {
        ...state,
        openModal: action.kind,
        openModalSource: state.activeSource,
        openSessionId: action.sessionId ?? null,
        openBlockStartAt: action.blockStartAt ?? null,
        openDailyDate: action.dailyDate ?? null,
        openProjectKey: action.projectKey ?? null,
      };
      break;
    case 'CLOSE_MODAL':
      state = {
        ...state,
        openModal: null,
        openModalSource: null,
        openSessionId: null,
        openBlockStartAt: null,
        openDailyDate: null,
        openProjectKey: null,
      };
      break;
    case 'SET_ACTIVE_SOURCE':
      // No-op same-value dispatch FIRST (skip both the persist AND the state
      // reassignment) so a re-click of the active segment never churns the
      // store or hits synchronous localStorage. Persist only on a real change.
      if (state.activeSource === action.source) break;
      saveActiveSource(action.source);
      {
        // Qualified details carry their physical source and remain bound while
        // the board switches behind them. Recompute only the board search
        // indices; the open modal continues to read its captured key/source.
        const next = { ...state, activeSource: action.source };
        state = { ...next, ..._recomputeSearch(next) };
      }
      break;
    case 'OPEN_SOURCE_DETAIL':
      state = {
        ...state,
        openSourceDetail: { source: action.source, resource: action.resource, key: action.key },
        openSourceDetailSelection: state.activeSource,
      };
      break;
    case 'CLOSE_SOURCE_DETAIL':
      if (state.openSourceDetail == null) break;
      state = { ...state, openSourceDetail: null, openSourceDetailSelection: null };
      break;
    case 'SET_SOURCE_SESSIONS_SORT': {
      // Header-click sort reorders the source grid's rendered rows; search
      // indices must follow. Transient — no localStorage write.
      const next = { ...state, sourceSessionsSort: action.override };
      state = { ...next, ..._recomputeSearch(next) };
      break;
    }
    case 'SET_VIEW':
      // Leaving the view clears the active selection AND the rail search so
      // re-entry starts clean; entering preserves any selection set by
      // OPEN_CONVERSATION. Switching the workspace also dismisses any transient
      // modal (#158) — see DISMISSED_ON_VIEW_SWITCH.
      state = {
        ...state,
        view: action.view,
        ...DISMISSED_ON_VIEW_SWITCH,
        // #217 S7 F10 — reverse-clear: a view switch (incl. into the dashboard)
        // wipes any in-flight comparison + pick-mode so a stale compare never
        // lingers behind the next view.
        compare: null,
        comparePick: null,
        // #289 (Codex P2-D, belt-and-suspenders) — also drop the pending
        // compare-close focus so leaving the workspace (the second Escape → the
        // dashboard) never strands the flag for the next reader.
        compareCloseFocusPending: false,
        ...(action.view === 'dashboard'
          ? { selectedConversationId: null, selectedConversationRef: null, conversationJump: null, conversationSearch: '', conversationSearchKind: 'all' as const }
          : {}),
      };
      break;
    case 'OPEN_CONVERSATION': {
      const conversationRef = actionConversationRef(action.conversationRef, action.sessionId);
      if (!conversationRef) break;
      // A direct workspace switch into the conversations view (bypasses
      // SET_VIEW), so it dismisses transient modals the same way (#158) before
      // applying the selection it carries.
      //
      // #177 S5 — focus-mode reset is GENUINE-SWITCH-ONLY. Every in-session
      // jump (jump-to-next keys, outline-entry click, glyph cluster,
      // hidden-run click) dispatches OPEN_CONVERSATION with the SAME
      // sessionId. A blanket reset here would drop the user out of their
      // active focus mode on every such jump (e.g. pressing `e` to walk to
      // the next error while in Errors mode). Per spec §5 the reset-to-All
      // belongs to the jump callers, who already run the precise
      // per-jump hidden-target check (ConversationReader.jumpNext via
      // nodeVisible; OutlinePanel via outlineTurnVisible; the hidden-run
      // marker dispatches SET_CONV_FOCUS_MODE 'all' explicitly). So a
      // same-session OPEN_CONVERSATION MUST preserve convFocusMode +
      // convCurrentTurnUuid — the caller is the authority. Only a genuine
      // session switch (different sessionId) resets the transient outline
      // state; the persisted open flag is left alone in both cases.
      const switched = !sameConversationRef(conversationRef, state.selectedConversationRef);
      state = {
        ...state,
        view: 'conversations',
        ...DISMISSED_ON_VIEW_SWITCH,
        // #217 S7 F10 — reverse-clear: opening a single conversation (incl. the
        // "open in reader →" affordance inside a comparison) leaves comparison
        // mode, so the reader replaces the comparison view.
        compare: null,
        comparePick: null,
        // #304 S2 (Codex F8) — a direct open makes any pending compare focus
        // return moot (mirrors SELECT_CONVERSATION's unconditional clear).
        compareCloseFocusPending: false,
        selectedConversationId: conversationRef.source === 'claude' ? conversationRef.key : null,
        selectedConversationRef: conversationRef,
        conversationJump: action.jump ?? null,
        // #177 S6 — a GENUINE session switch closes the find bar (its anchor
        // list is session-scoped + point-in-time, so it's stale for the new
        // conversation). A same-session OPEN_CONVERSATION (an in-session find
        // step dispatches one with a jump) leaves it open so the cursor lives.
        // #188 S2 — a genuine session switch also drops the explicit pin (the
        // bucket-root uuids it could hold are session-scoped). A same-session
        // OPEN_CONVERSATION (an in-session jump) leaves it — the jump effect
        // re-sets it after the jump lands.
        // #205 S1 — a genuine switch also closes the ephemeral mobile outline
        // sheet so the new conversation's transcript is never auto-buried; a
        // same-session OPEN_CONVERSATION (in-session jump) leaves it open.
        // #217 S6 F4 — a GENUINE session switch re-hydrates convBookmarks from
        // localStorage for the new session; a same-session OPEN (an in-session
        // jump) keeps the live map (which may carry an unsaved-to-state mutation
        // mid-flight). action.sessionId is non-null for OPEN_CONVERSATION.
        ...(switched
          ? { convFocusMode: 'all' as const, convOutlineTab: 'outline' as const, convCurrentTurnUuid: null, convPinnedUuid: null, convFindOpen: false, convOutlineMobileOpen: false, convBookmarks: loadBookmarks(conversationRef) }
          : {}),
      };
      break;
    }
    case 'SELECT_CONVERSATION': {
      const conversationRef = actionConversationRef(action.conversationRef, action.sessionId);
      const changed = !sameConversationRef(conversationRef, state.selectedConversationRef);
      state = {
        ...state,
        // #205 S1 — close the mobile outline sheet on a genuine conversation
        // change (incl. Back → null); a same-id re-select leaves it. #205 S2 —
        // also close find on a genuine change: its anchor list is session-scoped
        // + point-in-time, so it's stale for a new conversation (symmetric with
        // the OPEN_CONVERSATION switch-cleanup). Without this, the new mobile
        // Find button lets open-find → Back → reselect auto-reopen the bar and
        // pop the keyboard. Read the PRIOR selectedConversationId (state, not
        // action) before the overwrite.
        // #217 S6 F4 — a genuine change re-hydrates convBookmarks from
        // localStorage for the new session (or clears to {} on a select-to-null /
        // Back). Rail-row clicks dispatch SELECT_CONVERSATION, so hydrating only
        // OPEN_CONVERSATION would leave the prior session's bookmarks showing
        // (Codex P1). A same-id reselect leaves the live map untouched.
        ...(changed ? { convOutlineMobileOpen: false, convFindOpen: false, convBookmarks: conversationRef ? loadBookmarks(conversationRef) : {} } : {}),
        // #217 S7 F10 — reverse-clear: selecting a rail row (single-session)
        // leaves any in-flight comparison + pick-mode.
        compare: null,
        comparePick: null,
        // #289 (Codex P2-D) — clear the pending compare-close focus too. The new
        // Escape peel can run CLOSE_COMPARE (arms it) → SELECT_CONVERSATION null
        // (unmounts the reader that would consume it), which would otherwise
        // strand the flag so the NEXT reader steals focus to #conv-compare-with.
        // A deselect-to-null OR select-to-other both make a pending compare-focus
        // moot; the intended in-reader compare-close return does no SELECT.
        compareCloseFocusPending: false,
        selectedConversationId: conversationRef?.source === 'claude' ? conversationRef.key : null,
        selectedConversationRef: conversationRef,
        conversationJump: null,
        // #177 S5 — same transient reset as OPEN_CONVERSATION (convOutlineOpen
        // is NOT touched). #217 S5 — convOutlineTab resets alongside.
        convFocusMode: 'all',
        convOutlineTab: 'outline',
        convCurrentTurnUuid: null,
        // #188 S2 — drop the explicit pin on a select too.
        convPinnedUuid: null,
      };
      break;
    }
    case 'SET_CONVERSATION_SEARCH':
      // #177 S6 — clearing the needle snaps the kind facet back to 'all' so
      // re-opening search starts on the default facet (a non-empty edit leaves
      // the active facet alone — the user keeps Tools/Thinking across keystrokes).
      // #217 S4 / I-2.5 — filters now apply to BOTH browse and search, so a
      // non-empty needle MUST NOT force-close `convFiltersOpen` (the prior
      // cross-branch behavior): the rail renders the Filters popover in search
      // mode too, and the reader nav guards still gate correctly on
      // `convFiltersOpen` regardless of mode (an open filter popover always means
      // "typing in a filter"). The search needle no longer touches the popover
      // flag at all; only the empty (clear) path resets the kind facet.
      state = {
        ...state,
        conversationSearch: action.text,
        ...(action.text === ''
          ? { conversationSearchKind: 'all' as const }
          : {}),
      };
      break;
    case 'SET_CONVERSATION_SEARCH_KIND':
      state = { ...state, conversationSearchKind: action.kind };
      break;
    case 'CLEAR_CONVERSATION_JUMP':
      state = { ...state, conversationJump: null };
      break;
    case 'TOGGLE_CONV_OUTLINE': {
      const next = !state.convOutlineOpen;
      try {
        localStorage.setItem(CONV_OUTLINE_OPEN_KEY, next ? 'true' : 'false');
      } catch {
        // localStorage unavailable (private mode / quota) — keep the in-memory
        // toggle; the pref just won't survive a reload.
      }
      state = { ...state, convOutlineOpen: next };
      break;
    }
    case 'SET_CONV_OUTLINE_WIDTH': {
      // #217 S3 E6(b) — clamp + persist the outline width. No-op (no re-render)
      // when the clamped value is unchanged, so a held arrow at the clamp edge
      // or an identical pointer-move doesn't churn the store.
      const px = clampOutlineWidth(action.px);
      if (px === state.convOutlineWidth) break;
      saveOutlineWidth(px);
      state = { ...state, convOutlineWidth: px };
      break;
    }
    case 'TOGGLE_CONV_OUTLINE_MOBILE':
      // #205 S1 — flip the ephemeral mobile flag only; never touches the
      // persisted desktop convOutlineOpen or localStorage.
      state = { ...state, convOutlineMobileOpen: !state.convOutlineMobileOpen };
      break;
    case 'CLOSE_CONV_OUTLINE_MOBILE':
      state = { ...state, convOutlineMobileOpen: false };
      break;
    case 'SET_CONV_FOCUS_MODE':
      state = { ...state, convFocusMode: action.mode };
      break;
    case 'SET_CONV_OUTLINE_TAB':
      state = { ...state, convOutlineTab: action.tab };
      break;
    case 'OPEN_CONV_FIND':
      state = { ...state, convFindOpen: true };
      break;
    case 'CLOSE_CONV_FIND':
      state = { ...state, convFindOpen: false };
      break;
    case 'SET_CONVERSATION_FILTERS': {
      // #217 S4 / I-2.2 — persist the filters+sort blob on every filter edit.
      const conversationFilters = { ...state.conversationFilters, ...action.patch };
      saveRailPrefs({ filters: conversationFilters, sort: state.conversationRailSort });
      state = { ...state, conversationFilters };
      break;
    }
    case 'CLEAR_CONVERSATION_FILTERS':
      saveRailPrefs({ filters: EMPTY_FILTERS, sort: state.conversationRailSort });
      state = { ...state, conversationFilters: EMPTY_FILTERS };
      break;
    case 'TOGGLE_CONV_FILTERS':
      state = { ...state, convFiltersOpen: !state.convFiltersOpen };
      break;
    case 'SET_CONV_FILTERS_OPEN':
      state = { ...state, convFiltersOpen: action.open };
      break;
    case 'SET_CONVERSATION_RAIL_SORT':
      // #217 S4 / I-2 — persist the filters+sort blob on a sort change too.
      saveRailPrefs({ filters: state.conversationFilters, sort: action.sort });
      state = { ...state, conversationRailSort: action.sort };
      break;
    // #217 S7 F10 — session comparison.
    case 'START_COMPARE_PICK':
      // #304 S2 (Codex F2) — enforce the caller invariant `comparePick ⇒
      // selectedConversationId === anchor` at the reducer: both entries
      // dispatch from the open reader, and the compact view-layer gate relies
      // on the anchor selection surviving pick-mode (cancel returns to the
      // anchor reader). (Codex F7) — also close the ephemeral outline sheet:
      // restoring it after cancel would bury the remounted reader and obscure
      // the focus-return target behind the sheet backdrop.
      {
        const anchor = actionConversationRef(action.anchorRef, action.anchor);
        if (!anchor || !sameConversationRef(anchor, state.selectedConversationRef)) break;
        state = { ...state, comparePick: { anchor }, convOutlineMobileOpen: false };
      }
      break;
    case 'CANCEL_COMPARE_PICK':
      // #304 S2 — cancel returns to the anchor reader; arm the SAME focus-
      // return flag CLOSE_COMPARE uses (its meaning generalizes to
      // "compare-flow focus return pending"), and normalize convFiltersOpen
      // (mirrors OPEN_COMPARE) so a banner-Cancel click with the popover open
      // can't strand the flag through a compact rail unmount (Codex F1).
      state = { ...state, comparePick: null, convFiltersOpen: false, compareCloseFocusPending: true };
      break;
    case 'OPEN_COMPARE': {
      const a = actionConversationRef(action.aRef, action.a);
      const b = actionConversationRef(action.bRef, action.b);
      if (!a || !b || sameConversationRef(a, b)) break; // guard: never A===B
      state = {
        ...state,
        ...DISMISSED_ON_VIEW_SWITCH,
        view: 'conversations',
        // A is the anchor: initial selectedConversationId is null, so without
        // this a cold-boot OPEN_COMPARE (e.g. from a pasted compare URL) would
        // leave CLOSE_COMPARE with nothing to fall back to. Anchoring on A keeps
        // close → single-session-reader-on-A correct.
        selectedConversationId: a.source === 'claude' ? a.key : null,
        selectedConversationRef: a,
        conversationJump: null,
        comparePick: null,
        // C2 (#238 S3, Codex gate #1) — entering a comparison must present a
        // clean, Escape-reachable state. The conversations Escape binding gates
        // on inView (which excludes convFiltersOpen); a filters popover left open
        // when the comparison opens would otherwise suppress Esc-to-close. Scoped
        // here, NOT in DISMISSED_ON_VIEW_SWITCH, to leave other transitions alone.
        convFiltersOpen: false,
        compare: { a, b },
      };
      break;
    }
    case 'SWAP_COMPARE':
      state = { ...state, compare: state.compare ? { a: state.compare.b, b: state.compare.a } : null };
      break;
    case 'CLOSE_COMPARE':
      // #228 S1 (F3) — keep selectedConversationId = anchor AND arm the
      // focus-return flag so the reader returns focus to #conv-compare-with
      // once its detail re-renders. ONLY CLOSE_COMPARE arms it — the
      // reverse-clear sites (SET_VIEW/OPEN_/SELECT_CONVERSATION) clear compare
      // for a different reason and must NOT request a focus return.
      state = { ...state, compare: null, compareCloseFocusPending: true };
      break;
    case 'CLEAR_COMPARE_CLOSE_FOCUS':
      state = { ...state, compareCloseFocusPending: false };
      break;
    case 'CACHE_CONVERSATION_TITLES': {
      // #227 — merge non-empty titles into the accumulating cache. Skip the
      // emit entirely when nothing actually changes (the rail re-dispatches the
      // same rows on every SSE-tick revalidation) so a no-op doesn't churn
      // subscribers.
      let changed = false;
      const next = { ...state.conversationTitles };
      for (const [ref, title] of action.titles) {
        if (!ref || !title) continue;
        const key = isConversationRef(ref) ? conversationRefKey(ref) : ref;
        if (next[key] === title) continue;
        next[key] = title;
        changed = true;
      }
      if (changed) state = { ...state, conversationTitles: next };
      break;
    }
    case 'SET_CONV_CURRENT_TURN':
      // No-op same-uuid ticks FIRST (the scroll-sync observer re-fires the same
      // topmost-visible uuid repeatedly): skip both the emit AND the persistence
      // so an unchanged position never hits localStorage (Codex P2 throttle —
      // dedupe-then-throttle). The position for this uuid was already written on
      // the tick that first set it.
      if (state.convCurrentTurnUuid === action.uuid) break;
      // #217 S3 E1 — persist the reading position for the CURRENTLY-OPEN session
      // on the throttled scroll-sync write (persist-before-reset, Codex P2): a
      // genuine switch resets convCurrentTurnUuid in the OPEN_CONVERSATION /
      // SELECT_CONVERSATION reducers, so persisting "on switch" would save a
      // just-cleared null. Only a real uuid (not the cross-session null reset)
      // for an actually-selected session is recorded. recordReadingPos throttles
      // per session, so rapid distinct-uuid ticks don't hammer synchronous
      // localStorage.
      if (action.uuid != null && state.selectedConversationRef != null) {
        recordReadingPos(state.selectedConversationRef, action.uuid);
      }
      state = { ...state, convCurrentTurnUuid: action.uuid };
      break;
    case 'SET_CONV_PINNED_TURN':
      if (state.convPinnedUuid === action.uuid) break; // no-op: avoid a needless emit
      state = { ...state, convPinnedUuid: action.uuid };
      break;
    case 'CLEAR_CONV_PIN':
      if (state.convPinnedUuid === null) break; // already clear — no emit
      state = { ...state, convPinnedUuid: null };
      break;
    case 'TOGGLE_BOOKMARK': {
      // #217 S6 F4 — write through to localStorage, then re-read the saved map so
      // convBookmarks reflects the canonical persisted shape (mirrors the
      // recordReadingPos write-through). The action's sessionId (when set) wins
      // over the selected conversation so the bookmark always lands on THIS
      // button's session; both absent → no-op.
      const conversationRef = actionConversationRef(action.conversationRef, action.sessionId)
        ?? state.selectedConversationRef;
      if (!conversationRef) break;
      toggleBookmark(conversationRef, action.uuid);
      // Only re-hydrate the in-view convBookmarks when the target IS the open
      // conversation; a write to some other session must not clobber it.
      if (sameConversationRef(conversationRef, state.selectedConversationRef)) {
        state = { ...state, convBookmarks: loadBookmarks(conversationRef) };
      }
      break;
    }
    case 'SET_BOOKMARK_NOTE': {
      const conversationRef = actionConversationRef(action.conversationRef, action.sessionId)
        ?? state.selectedConversationRef;
      if (!conversationRef) break;
      setBookmarkNote(conversationRef, action.uuid, action.note);
      if (sameConversationRef(conversationRef, state.selectedConversationRef)) {
        state = { ...state, convBookmarks: loadBookmarks(conversationRef) };
      }
      break;
    }
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
      // Pin the schema cursor to CURRENT (mirrors RESET_PANEL_ORDER above).
      // defaultPrefs() ships the current canonical panelOrder but a v1 baseline
      // cursor, so without this the next reload would re-run the v1→vN migration
      // over the already-current order and scramble it — post-#264-S2 the v3→v4
      // step re-collapses the fresh daily/weekly/monthly ids into 'history'.
      fresh.panelOrderSchemaVersion = CURRENT_PANEL_ORDER_SCHEMA_VERSION;
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
      // Defensively pin the schema-version cursor to CURRENT here so a
      // future bug that allowed the cursor to drift backwards (e.g.
      // corrupt JSON in localStorage parsed as v1) does not re-trigger
      // migrations on the next reload after a user-initiated reset.
      const prefs = {
        ...state.prefs,
        panelOrder: [...DEFAULT_PANEL_ORDER],
        panelOrderSchemaVersion: CURRENT_PANEL_ORDER_SCHEMA_VERSION,
      };
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
      state = { ...state, prefs };
      break;
    }
    case 'REORDER_PANELS': {
      // #294 S5 §6.11 — from/to index the VISIBLE (source-filtered) list; the
      // reorder maps back into the full order via mapVisibleReorderToFull so
      // hidden panels keep their absolute positions. When every panel is visible
      // (visible === full) this is byte-identical to the prior splice-move.
      const { from, to } = action;
      const full = state.prefs.panelOrder;
      const visible = deriveVisiblePanelOrder(
        full,
        resolveSourceView(state.snapshot, state.activeSource),
      );
      if (from === to) break;
      if (from < 0 || from >= visible.length) break;
      if (to < 0 || to >= visible.length) break;
      const after = visible.slice();
      const [moved] = after.splice(from, 1);
      after.splice(to, 0, moved);
      const next = mapVisibleReorderToFull(full, visible, after);
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
      // #264 S1 — height-class-aware keyboard reorder. Shift+Arrow (PanelHost)
      // dispatches this with the card's GLOBAL panelOrder index; the swap
      // target is the previous/next id sharing the dragged card's
      // CARD_LAYOUT row (tall/medium/short), skipping any intervening
      // other-class ids. This keeps the bento coherent: a keyboard reorder can
      // never cross height classes (matching the pointer path, where each row
      // is its own dnd context). Empty/one-item rows and the class boundaries
      // are no-ops.
      // #294 S5 §6.11 — `index` is the position in the VISIBLE (source-filtered)
      // list; the row-aware swap walks the VISIBLE list and writes back into the
      // full order (hidden panels hold their positions). visible === full →
      // byte-identical to the prior full-order swap.
      const { index, direction } = action;
      const full = state.prefs.panelOrder;
      const visible = deriveVisiblePanelOrder(
        full,
        resolveSourceView(state.snapshot, state.activeSource),
      );
      if (index < 0 || index >= visible.length) break;
      const row = CARD_LAYOUT[visible[index]].row;
      let target = index + direction;
      while (target >= 0 && target < visible.length && CARD_LAYOUT[visible[target]].row !== row) {
        target += direction;
      }
      if (target < 0 || target >= visible.length) break;
      const after = visible.slice();
      [after[index], after[target]] = [after[target], after[index]];
      const next = mapVisibleReorderToFull(full, visible, after);
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
      // The alerts-test button dispatches a Claude AlertEntry; the Toast
      // normalizes it to a Claude source row at render (raw here so the store
      // test's exact-payload assertion stays byte-stable).
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
    case 'INGEST_SOURCE_ALERTS': {
      // #294 S5 §6.7 — the source-aware toast pipeline. Mirrors the legacy
      // INGEST_SNAPSHOT_ALERTS forward-only rule, but keyed on the normalized
      // `toastAlertId` and fed ONLY the provider projections (no legacy
      // top-level array → no codex_budget double-toast). Toasts fire for rows
      // of every source; the panel (not the store) filters by active source.
      const seen = new Set(state.seenAlertIds);
      if (action.isFirstTick) {
        // Cold-start / reconnect: seed every row as seen (both the normalized
        // and bare legacy forms, for one release of continuity) without
        // surfacing a toast, and clear the queue so a reconnect can't replay.
        for (const r of action.rows) for (const f of seedFormsForRow(r)) seen.add(f);
        state = {
          ...state,
          seenAlertIds: seen,
          alertsConfig: action.alertsSettings,
          alertToastQueue: [],
        };
        break;
      }
      const fresh = action.rows.filter((r) => !seen.has(toastAlertId(r)));
      for (const r of fresh) for (const f of seedFormsForRow(r)) seen.add(f);

      let toast = state.toast;
      let queue = state.alertToastQueue;
      if (fresh.length > 0) {
        if (!toast || toast.kind !== 'alert') {
          toast = { kind: 'alert', payload: fresh[0] };
          queue = [...queue, ...fresh.slice(1)];
        } else {
          queue = [...queue, ...fresh];
        }
      }
      state = {
        ...state,
        seenAlertIds: seen,
        alertsConfig: action.alertsSettings,
        alertToastQueue: queue,
        toast,
      };
      break;
    }
    case 'INGEST_DASHBOARD_PREFS':
      // Wholesale replace (server is the source of truth) — same posture as
      // alertsConfig under INGEST_SNAPSHOT_ALERTS.
      state = { ...state, dashboardPrefs: action.prefs };
      break;
    case 'SET_TABLE_SORT': {
      const key =
        action.table === 'trend'
          ? 'trendSortOverride'
          : action.table === 'projects'
            ? 'projectsSortOverride'
            : action.table === 'history'
              ? 'historySortOverride'
              : 'sessionsSortOverride';
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
        projectsSortOverride: null,
        // S8 (#254): the History table sort is part of the reset surface.
        historySortOverride: null,
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
      // #294 S5 §7 — stamp the share flow's source from the CURRENT
      // activeSource at OPEN_SHARE. A later SET_ACTIVE_SOURCE mutates only
      // state.activeSource (not shareModal), so the captured source is frozen
      // for the flow's lifetime — no restamp mid-flow.
      const enriched: ShareAction =
        action.type === 'OPEN_SHARE'
          ? { ...action, source: action.source ?? (state.openModal != null ? state.openModalSource : null) ?? state.activeSource }
          : action;
      const slice = shareReducer(
        { shareModal: state.shareModal, composerModal: state.composerModal },
        enriched,
      );
      state = { ...state, ...slice };
      break;
    }
    // Share v2 basket (spec §7). Forward to the pure reducer, then
    // persist on every mutation AND surface a status toast on
    // capacity-rejection. The toast is fired here (not in the
    // reducer) so the reducer stays pure and tests don't need a
    // master-store boot to exercise the rejection branch — the
    // master-store integration test owns the toast-surfacing
    // contract; the reducer test owns the rejectedReason sentinel.
    case 'BASKET_ADD':
    case 'BASKET_REMOVE':
    case 'BASKET_REORDER':
    case 'BASKET_CLEAR':
    case 'BASKET_CLEAR_REJECTED':
    case 'BASKET_HYDRATE': {
      const next = basketReducer(state.basket, action);
      if (next === state.basket) break;
      // items array identity drives the localStorage write — we
      // don't want to re-serialize 20 items every time the reducer
      // toggles rejectedReason only.
      const itemsChanged = next.items !== state.basket.items;
      state = { ...state, basket: next };
      if (itemsChanged) saveBasketToStorage(next.items);
      if (action.type === 'BASKET_ADD' && next.rejectedReason === 'capacity') {
        state = {
          ...state,
          toast: {
            kind: 'status',
            text: 'Basket is full (20 sections). Remove one to add another.',
          },
        };
      }
      break;
    }
    // Doctor subcommand (spec §6). The aggregate slice is replaced
    // wholesale every tick; OPEN/CLOSE flip a boolean modal flag that
    // the composite `d` keymap guard in main.tsx reads alongside
    // openModal + update.modalOpen + inputMode per spec §6.4.
    case 'SET_DOCTOR_AGGREGATE':
      state = { ...state, doctor: action.doctor };
      break;
    case 'OPEN_DOCTOR_MODAL':
      state = { ...state, doctorModalOpen: true };
      break;
    case 'CLOSE_DOCTOR_MODAL':
      state = { ...state, doctorModalOpen: false };
      break;
    case 'SET_HERO_SCROLLED':
      // No-op short-circuit so a stream of identical IntersectionObserver
      // callbacks doesn't emit() a fresh render every frame.
      if (state.heroScrolled !== action.scrolled) {
        state = { ...state, heroScrolled: action.scrolled };
      }
      break;
    case 'INCREMENT_CHROME_OVERLAY':
      state = { ...state, chromeOverlayOpen: state.chromeOverlayOpen + 1 };
      break;
    case 'DECREMENT_CHROME_OVERLAY':
      state = { ...state, chromeOverlayOpen: Math.max(0, state.chromeOverlayOpen - 1) };
      break;
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
