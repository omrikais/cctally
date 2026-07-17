// SSE envelope — hand-transcribed from snapshot_to_envelope in
// bin/cctally. When Python adds a field, update here.

export type Verdict = 'ok' | 'cap' | 'capped';

export interface Envelope {
  envelope_version: number;
  generated_at: string | null;
  // #300 — an all-inputs change-signal string derived from the DB dispatch
  // signature, surfaced so the lazy detail fetchers (session modal, projects
  // drill, conversation outline) can revalidate on an actual DATA change
  // instead of the 5s `generated_at` heartbeat. Changes iff any DB leg the
  // detail endpoints read changed (session entries, weekly usage/cost, reset
  // events, codex entries, cache generation); flat on an idle tick. Optional /
  // may be "": a Python without the field, or the non-precompute path, leaves
  // it absent or empty, and the client (`revalToken`) falls back to
  // `generated_at`. See `lib/revalToken.ts`.
  data_version?: string;
  // Conversation viewer (spec §5): true only when the transcript GET
  // routes would serve for THIS request (bind gate AND Host allowlist).
  // Absent on envelopes from a Python without the feature → treat as false.
  transcriptsEnabled?: boolean;
  last_sync_at: string | null;
  sync_age_s: number | null;
  last_sync_error: string | null;
  header: HeaderEnvelope;
  current_week: CurrentWeekEnvelope | null;
  forecast: ForecastEnvelope | null;
  trend: TrendEnvelope | null;
  // Per spec §2.7: empty state is `rows === []`, not the parent === null.
  // Python always emits `{rows: [...]}` (possibly empty), so the parent
  // is non-null here.
  weekly:  WeeklyEnvelope;
  monthly: MonthlyEnvelope;
  blocks:  BlocksEnvelope;
  daily:   DailyEnvelope;
  sessions: SessionsEnvelope;
  // Projects panel envelope (spec §5.2). Null when the server-side
  // build returned no usable rows (e.g. empty cache, missing
  // session_files rows for active entries). Non-null parent with empty
  // `current_week.rows` and empty `trend.weeks`/`trend.projects` is the
  // canonical "no project activity yet this week" empty state.
  projects: ProjectsEnvelope | null;
  // Display tz block — per F1 of the localize-datetime-display spec.
  // The server resolves "local" to a concrete IANA zone (using the
  // dashboard process's host tz) BEFORE emitting the envelope; the
  // browser must NEVER call its own Intl resolver. Fields:
  //   tz             — raw config value: "local" | IANA zone (verbatim)
  //   resolved_tz    — concrete IANA zone after server resolution
  //   offset_label   — human suffix shown after datetime ("UTC", "PDT",
  //                    or numeric "+03" / "-04" when no zone abbrev)
  //   offset_seconds — signed offset from UTC in seconds at generated_at
  display: DisplayEnvelope;
  // Threshold-actions (T5+T8): newest-first list of percent-crossing
  // alerts (last 100). Mirrors the Python envelope's `alerts` field.
  alerts: AlertEntry[];
  // Snapshot-mirrored alerts config so SettingsOverlay can seed without
  // a separate GET /api/settings; matches the Python envelope's
  // `alerts_settings` block emitted by snapshot_to_envelope.
  alerts_settings: AlertsSettingsEnvelope;
  // update-subcommand mirror — `{state, suppress}` shape matches GET
  // /api/update/status's payload so coerceUpdateState/Suppress consume
  // both the bootstrap fetch and live SSE ticks uniformly. Optional so
  // a Python without this mirror (older snapshot path or test fixtures
  // built before the field landed) doesn't break the type contract.
  update?: UpdateEnvelope;
  // Doctor aggregate (spec §6). Aggregate-only — full per-check report
  // is fetched lazily via GET /api/doctor by useDoctorReport. Optional
  // so a Python without the mirror keeps the type contract intact.
  // `_error` is present iff the server-side gather raised (Python
  // emits a synthetic-FAIL aggregate so the chip still surfaces).
  doctor?: DoctorEnvelope;
  // Cache Report (spec 2026-05-21). Optional + nullable; matches the
  // additive-field pattern of update? / doctor?. envelope_version stays
  // at 2 — no bump for additive optional fields. The Python serializer
  // at bin/_cctally_dashboard.py :: _cache_report_snapshot_to_dict
  // emits snake_case keys to mirror this interface field-for-field.
  cache_report?: CacheReportEnvelope | null;
  // Dashboard-scoped preferences mirror (cache-failure-markers spec §5).
  // Additive optional, like alerts_settings — a Python without the feature
  // omits it entirely. `cache_failure_markers` is the conversation-viewer
  // cache-rebuild marker opt-out; ABSENCE is treated as ON (default true).
  // `live_tail` is the conversation-viewer live-tail opt-out (live-tail spec
  // §4.2); ABSENCE is likewise treated as ON (default true).
  dashboard_prefs?: { cache_failure_markers?: boolean; live_tail?: boolean };
  // Preview channel marker — set to 'preview' only by the maintainer-local
  // `cctally-preview` wrapper (CCTALLY_CHANNEL=preview); omitted otherwise.
  // Additive-optional, like update?/doctor?/dashboard_prefs? — a Python
  // without the marker leaves it absent. Drives the header PREVIEW pill.
  channel?: 'preview';
  // #278 Theme A first-paint latch: true only while data is still being
  // assembled — the cheap bind-before-build seed and A2's progressive
  // partial republishes. Heavy panels that are still empty render a loading
  // skeleton while this is true; a populated-but-incomplete panel shows its
  // partial data as-is. False/absent on every complete snapshot. Additive-
  // optional — a Python without the field leaves it absent → treat as false.
  hydrating?: boolean;
  // #294 S5 — the S4 source-aware read model. The server's
  // `_source_bundle_to_envelope` (bin/_cctally_dashboard_envelope.py) SPREADS
  // its four fields at the envelope TOP LEVEL via `envelope.update(...)`, so
  // `source_schema_version` / `default_source` / `source_order` are top-level
  // siblings and `sources` is the FLAT per-source map `{claude, codex, all}` —
  // there is NO `env.sources.sources` nesting. All four are additive-optional:
  // a Python that predates S4 (or a fixture built before the fields landed)
  // omits them, and the source-view seam (store/sourceView.ts) degrades Claude
  // to the legacy top-level envelope and Codex/All to a hydrating-like absence.
  source_schema_version?: number;
  default_source?: string;
  source_order?: string[];
  sources?: SourcesMap;
}

// Cache Report envelope (spec 2026-05-21).
// Snake_case to match the Python envelope; see CLAUDE.md re.
// snake/camel inconsistency between dashboard envelope and CLI --json.

export type CacheAnomalyReason = 'net_negative' | 'cache_drop';

export interface CacheReportDailyRow {
  date: string;                       // YYYY-MM-DD in display tz
  cache_hit_percent: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  saved_usd: number;
  wasted_usd: number;
  net_usd: number;
  anomaly_triggered: boolean;
  anomaly_reasons: CacheAnomalyReason[];
}

export interface CacheReportBreakdownRow {
  key: string;
  cache_hit_percent: number;
  net_usd: number;
}

export interface CacheReportTodaySpotlight {
  date: string;
  cache_hit_percent: number;
  baseline_median_percent: number | null;
  delta_pp: number | null;
  net_usd: number;
  saved_usd: number;
  wasted_usd: number;
  anomaly_triggered: boolean;
  anomaly_reasons: CacheAnomalyReason[];
  baseline_daily_row_count: number;
}

export interface CacheReportEnvelope {
  window_days: number;
  anomaly_threshold_pp: number;
  anomaly_window_days: number;
  today: CacheReportTodaySpotlight;
  days: CacheReportDailyRow[];        // newest-first
  by_project: CacheReportBreakdownRow[];
  by_model: CacheReportBreakdownRow[];
  seven_day_net_usd: number;
  seven_day_anomaly_count: number;
  fourteen_day_counterfactual_usd: number;
  fourteen_day_efficiency_ratio: number;
  is_empty: boolean;
}

export interface DoctorEnvelope {
  severity: 'ok' | 'warn' | 'fail';
  counts: { ok: number; warn: number; fail: number };
  generated_at: string;
  fingerprint: string;
  _error?: string;
}

export interface UpdateEnvelope {
  // Both fields hold the raw on-disk shape — see _load_update_state /
  // _load_update_suppress in bin/cctally. The client coerces (via
  // coerceUpdateState/Suppress) before storing. Either may be null /
  // {_error: "..."} when the underlying file is missing or invalid;
  // the coercer returns a defensive null-state slice in that case.
  state: unknown;
  suppress: unknown;
}

// ---- Threshold-actions alerts (T5/T8) --------------------------------

export type AlertAxis =
  | 'weekly'
  | 'five_hour'
  | 'budget'
  | 'projected'
  | 'project_budget'
  | 'codex_budget';

// Projected axis (issue #121): the `metric` discriminator selects which
// week-average projection fired (weekly-% against the cap vs budget-$
// against the target). Top-level on the envelope row AND mirrored inside
// `context` so the metric-aware renderer can read either.
export type ProjectedMetric = 'weekly_pct' | 'budget_usd' | 'codex_budget_usd';

export interface AlertEntry {
  id: string;                    // "axis:window_key:…:threshold" — budget/
                                 // codex_budget/projected carry a "period"
                                 // segment (#137: "axis:window_key:period:…");
                                 // opaque React key, never parsed.
  axis: AlertAxis;
  threshold: number;             // integer in [1, 100]
  // Severity tier authority (Phase B): emitted by the Python kernel's
  // `severity_for(threshold)` as a 3-tier token — `info` (<90) / `warn`
  // (90-99) / `critical` (>=100). Optional so a stale envelope (older
  // server) still renders — `alertSeverity` falls back to deriving it from
  // `threshold` when absent, and normalizes the legacy `amber`/`red` tokens
  // a pre-Phase-B backend might still emit. See lib/alertAxis.ts
  // `alertSeverity`.
  severity?: 'info' | 'warn' | 'critical';
  crossed_at: string;            // ISO-8601 UTC
  alerted_at: string;            // ISO-8601 UTC
  // Projected axis (issue #121): top-level metric discriminator. Absent on
  // the weekly/five_hour/budget axes.
  metric?: ProjectedMetric;
  context: {
    week_start_date?: string;
    cumulative_cost_usd?: number;
    dollars_per_percent?: number | null;
    five_hour_window_key?: number;
    block_start_at?: string;
    block_cost_usd?: number;
    primary_model?: string | null;
    // Budget axis (issue #19): equiv-$ budget threshold crossings. The
    // `project_budget` axis (issue #19/#121) reuses budget_usd / spent_usd /
    // consumption_pct and adds the project dimension below.
    week_start_at?: string;
    budget_usd?: number;
    spent_usd?: number;
    consumption_pct?: number;
    // Period generalization (calendar-period-codex-budgets, spec §6): the
    // `budget` axis (and the per-vendor `codex_budget` axis) carry a `period`
    // discriminator — `subscription-week` | `calendar-week` | `calendar-month` —
    // plus the resolved `period_start_at` window start instant. The frontend
    // (RecentAlertsModal / Toast) renders a period-aware label ("Month" /
    // "Calendar week" / "Week") from these. Absent on the other axes; the
    // subscription-week default keeps the legacy "Week" label byte-stable.
    period?: string;
    period_start_at?: string;
    // Project-budget axis (issue #19/#121): `project` is the collision-
    // disambiguated basename (rendered in the chip context), `project_key`
    // the canonical git-root identity. Both absent on the other axes.
    project?: string;
    project_key?: string;
    // Projected axis (issue #121): rendered FROM THE ROW (not live config —
    // Codex P0-4). `projected_value` is the week-average projection (% for
    // weekly_pct, $ for budget_usd); `denominator` is the cap (100.0) or the
    // budget target ($). `metric` repeats the top-level discriminator.
    metric?: ProjectedMetric;
    projected_value?: number;
    denominator?: number;
  };
}

export interface AlertsSettingsEnvelope {
  enabled: boolean;
  weekly_thresholds: number[];
  five_hour_thresholds: number[];
  // Budget is its OWN config block (issue #19), sourced from
  // `_get_budget_config`, not the `alerts` block. `budget_enabled`
  // reflects `_budget_alerts_active` (a budget set AND alerts on).
  budget_thresholds: number[];
  budget_enabled?: boolean;
  // Projected axis (issue #121): the two opt-in toggles, mirrored from
  // `_get_alerts_config(...)["projected_enabled"]` (weekly leg) and
  // `_get_budget_config(...)["projected_enabled"]` (budget leg). Both
  // default false; gated behind their parent axis master switch server-side.
  projected_weekly_enabled?: boolean;
  projected_budget_enabled?: boolean;
  // Per-project budget axis (issue #19/#121): the single opt-in toggle,
  // mirrored from `_get_budget_config(...)["project_alerts_enabled"]`.
  // Defaults false; gates the `project_budget` axis dispatch only (the
  // per-project display section always renders configured projects).
  project_alerts_enabled?: boolean;
  // Codex budget toggles (#134): mirrored from the persisted
  // `budget.codex` block. `codex_budget_configured` is true when a Codex
  // budget exists (gates the two toggles disabled-with-hint when absent);
  // `codex_budget_alerts_enabled` / `codex_projected_enabled` seed the two
  // dashboard-writable sub-leaves. All default false; the nested
  // partial-merge writer only honors the two `*_enabled` leaves.
  codex_budget_configured?: boolean;
  codex_budget_alerts_enabled?: boolean;
  codex_projected_enabled?: boolean;
  // Notifier dispatch backend (Phase B): the configured `alerts.notifier`
  // mirrored from the server so SettingsOverlay can seed the dropdown
  // without a separate GET. Optional so a pre-Phase-B envelope keeps the
  // type contract; SettingsOverlay defaults to 'auto' when absent.
  notifier?: 'auto' | 'osascript' | 'notify-send' | 'command' | 'none';
  // Whether `alerts.command_template` is set on the server. The raw
  // template is NEVER sent to the client (it can carry secrets); only this
  // boolean is mirrored so the UI can enable/disable the "Custom command"
  // option. Optional + defaults false.
  command_configured?: boolean;
}

export interface DisplayEnvelope {
  tz:             string;
  resolved_tz:    string;
  offset_label:   string;
  offset_seconds: number;
  // F3: present (true) when the server was launched with an explicit
  // --tz override, indicating the persisted display.tz is pinned by
  // the running process for the lifetime of the dashboard. Absent
  // (treated as false) when the persisted config is in effect. Used
  // by the React client to surface a read-only Settings state when
  // pinned (POST /api/settings is rejected under pin).
  pinned?:        boolean;
}

export interface HeaderEnvelope {
  week_label: string | null;
  used_pct: number | null;
  five_hour_pct: number | null;
  dollar_per_pct: number | null;
  forecast_pct: number | null;
  forecast_verdict: Verdict | null;
  vs_last_week_delta: number | null;
}

// Freshness envelope for the current_week panel.
// Shape mirrors `cw_freshness` in `bin/cctally :: snapshot_to_envelope`.
// Refs spec §3.4 (OAuth /usage UA bypass design, 2026-04-30).
export type FreshnessLabel = 'fresh' | 'aging' | 'stale';

export interface FreshnessEnvelope {
  label: FreshnessLabel;
  captured_at: string;   // ISO-8601 UTC of the most recent snapshot
  age_seconds: number;   // integer seconds since capture (now - captured_at)
}

export interface CurrentWeekEnvelope {
  used_pct: number | null;
  five_hour_pct: number | null;
  five_hour_resets_in_sec: number | null;
  spent_usd: number | null;
  dollar_per_pct: number | null;
  reset_at_utc: string | null;
  reset_in_sec: number | null;
  last_snapshot_age_sec: number | null;
  milestones: Milestone[];
  // Per spec §3.4: null when cw is absent or has no snapshot timestamp.
  freshness: FreshnessEnvelope | null;
  // null when there's no API-anchored block matching the latest snapshot's
  // five_hour_window_key. Triggers fallback to the legacy single-big-number
  // layout in CurrentWeekPanel. Refs spec §4.1.
  five_hour_block: FiveHourBlockEnvelope | null;
  // Spec §5.3 (Codex r1 finding 3) — NEW key, parallel to ``milestones``
  // (which is the WEEKLY timeline). 5h-block per-percent milestones for
  // the active 5h block, capture-time-ordered, both pre- and
  // post-credit segments included. Optional for backward compat with
  // older envelopes that predate v1.7.x.
  five_hour_milestones?: FiveHourMilestone[];
}

export interface Milestone {
  percent: number;
  crossed_at_utc: string | null;
  cumulative_usd: number;
  marginal_usd: number | null;
  five_hour_pct_at_cross: number | null;
}

// Spec §5.3 — 5h-block milestone row. Snake_case to match the envelope.
// Mirrors the CLI ``five-hour-breakdown --json`` milestone objects but
// with envelope-convention key names.
export interface FiveHourMilestone {
  percent_threshold: number;
  captured_at_utc: string;
  block_cost_usd: number;
  marginal_cost_usd: number | null;
  seven_day_pct_at_crossing: number | null;
  // Segment column (migration 006): ``0`` is the pre-credit / no-credit
  // sentinel; non-zero values reference a ``five_hour_reset_events.id``.
  // React's row key MUST include this to distinguish post-credit
  // threshold repeats from their pre-credit counterparts.
  reset_event_id: number;
}

// Snake_case to match the Python envelope; see CLAUDE.md re. snake/camel
// inconsistency between dashboard envelope and CLI --json (intentional).
export interface FiveHourBlockEnvelope {
  block_start_at: string;                    // ISO 8601 with offset
  // Window key threading from server-side ``_select_current_block_for_envelope``.
  // Used by analytics dispatches; optional for backward compat.
  five_hour_window_key?: number;
  seven_day_pct_at_block_start: number | null;
  seven_day_pct_delta_pp: number | null;     // null on crossed-reset or missing anchor
  crossed_seven_day_reset: boolean;
  // Spec §5.3 — in-place credit events for this block. Empty array when
  // no credits detected. Optional for backward compat with envelopes
  // that predate v1.7.x.
  credits?: FiveHourCredit[];
}

export interface FiveHourCredit {
  effective_reset_at_utc: string;
  prior_percent: number;
  post_percent: number;
  delta_pp: number;
}

export interface ForecastEnvelope {
  verdict: Verdict;
  week_avg_projection_pct: number | null;
  recent_24h_projection_pct: number | null;
  budget_100_per_day_usd: number | null;
  budget_90_per_day_usd: number | null;
  confidence: 'high' | 'low' | 'unknown';
  confidence_score: number;
  explain: unknown; // opaque ForecastOutput JSON blob; modal renders it
}

export interface TrendEnvelope {
  weeks: TrendRow[];           // 8 rows — panel sparkline
  spark_heights: number[];     // parallel to weeks[]
  history: TrendRow[];         // up to 12 rows — modal
  // ---- view-model unification additive scalar (Bundle 1) ----
  // 3-sample $/% mean over `weeks`. Null when fewer than 3 valid samples.
  avg_dollars_per_pct?: number | null;
  // ---- issue #59: TrendModal's 4-week-median-non-current scalar ----
  // Pre-computed in `build_trend_view` (sort the last 4 non-current
  // `dollar_per_pct` values, take the midpoint `(s[1]+s[2])/2`).
  // Null when fewer than 4 valid non-current samples. TrendModal.tsx
  // keeps a client-side fallback for envelopes that omit the field
  // (additive contract).
  history_median_dpp?: number | null;
}

export interface TrendRow {
  label: string;
  used_pct: number | null;
  dollar_per_pct: number | null;
  delta: number | null;
  is_current: boolean;
  // S3 (#264): additive-optional weekly cost (USD). Present on both weeks[]
  // (panel sparkline) and history[] (modal); null when the week has no cost
  // row. The Trend modal's new Cost column reads it; the `?` tolerates older
  // envelopes that predate the field.
  cost_usd?: number | null;
}

export interface SessionsEnvelope {
  total: number;
  sort_key: string;
  rows: SessionRow[];
}

export interface SessionRow {
  session_id: string;
  started_utc: string | null;
  duration_min: number;
  model: string;
  project: string;
  // Spec §4.1 — opaque project key for cross-nav routing. Non-null when
  // the row's project IS present in the projects envelope (server
  // guarantees the lookup will resolve); null when project_path is None
  // in the cache (e.g. session_files row not yet ingested). Null rows
  // render as plain text per the stopgap; non-null rows render as a
  // clickable button that dispatches OPEN_MODAL with projectKey set.
  project_key: string | null;
  cost_usd: number | null;
  // S3 (#264): additive-optional. `cache_hit_pct` is a plain number (never
  // gated) — the per-session cache-hit percentage. `title` is transcript
  // content — present only when the request's transcript gate is open, else
  // undefined/null (the Session cell renders a muted em-dash). Consumers
  // already guard null; the `?` tolerates fixture/older envelopes too.
  cache_hit_pct?: number | null;
  title?: string | null;
}

// ---- Projects panel + modal (spec §5.2) ------------------------------
//
// Mirrors `build_projects_view` in `bin/_lib_projects.py`. The envelope
// carries TWO sub-blocks: `current_week` (the live leaderboard the panel
// renders) and `trend` (the 12-week trend the modal renders). The
// per-project detail (sessions + model breakdown) is fetched lazily via
// GET /api/project/<key>?weeks=N and consumed as `ProjectDetail`.

export interface ProjectsCurrentWeekRow {
  key: string;
  bucket_path: string;
  cost_usd: number;
  // Null when the week's total cost is zero (attribution undefined).
  attributed_pct: number | null;
  sessions_count: number;
}

export interface ProjectsCurrentWeekEnvelope {
  week_label: string | null;
  week_start_date: string | null;
  week_start_at: string | null;
  total_cost_usd: number;
  rows: ProjectsCurrentWeekRow[];
}

export interface ProjectsTrendWeek {
  week_start_date: string;
  week_label: string;
  total_cost_usd: number;
  total_pct: number | null;
}

export interface ProjectsTrendProject {
  key: string;
  bucket_path: string;
  // Parallel arrays — index i corresponds to `weeks[i]`.
  weekly_cost: number[];
  weekly_pct: (number | null)[];
  // Per-week distinct session counts / first / last activity within
  // each (project, week) bucket. The modal slices these to the active
  // window pill (1w / 4w / 8w / 12w) so the table's Sessions / First
  // seen / Last seen columns reflect that window (spec §3.4 + issue
  // #71's full fix). Nulls for first/last mean the project had no
  // activity in that week.
  sessions_per_week: number[];
  first_seen_per_week: (string | null)[];
  last_seen_per_week: (string | null)[];
}

export interface ProjectsTrendEnvelope {
  window_weeks: number;
  weeks: ProjectsTrendWeek[];
  projects: ProjectsTrendProject[];
}

export interface ProjectsEnvelope {
  current_week: ProjectsCurrentWeekEnvelope;
  trend: ProjectsTrendEnvelope;
}

// /api/project/<key>?weeks=N response — consumed by ProjectsModal (Task 5).
export interface ProjectDetailModelRow {
  model: string;
  cost_usd: number;
  sessions_count: number;
  tokens_input: number;
  tokens_output: number;
}

export interface ProjectDetailSessionRow {
  session_id: string;
  started_at: string;
  last_activity_at: string;
  primary_model: string;
  cost_usd: number;
}

export interface ProjectDetail {
  key: string;
  bucket_path: string;
  window_weeks: number;
  window_cost_usd: number;
  window_attributed_pct: number | null;
  models: ProjectDetailModelRow[];
  sessions: ProjectDetailSessionRow[];
  models_total: number;
  sessions_total: number;
}

// /api/session/:id response — consumed by SessionModal.
export interface SessionDetailCostPerModel {
  model: string;
  cost_usd: number | null;
}

export interface SessionDetailModel {
  name: string;
}

export interface SessionDetail {
  session_id?: string | null;
  started_utc?: string | null;
  last_activity_utc?: string | null;
  duration_min?: number | null;
  cost_total_usd?: number | null;
  project_label?: string | null;
  project_path?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_creation_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_hit_pct?: number | null;
  models?: SessionDetailModel[];
  cost_per_model?: SessionDetailCostPerModel[];
  source_paths?: string[];
}

// ---- Weekly / Monthly panels (envelope §1.3) -------------------------

export type ChipKey = 'opus' | 'sonnet' | 'haiku' | 'fable' | 'other';  // #244 — fable joins as a dedicated family

export interface ModelCostRow {
  model: string;        // canonical model id, e.g. "claude-opus-4-5-20251101"
  display: string;      // chip-label-friendly, e.g. "opus-4-5"
  chip: ChipKey;
  cost_usd: number;
  cost_pct: number;     // 0..100
}

export interface PeriodRow {
  label: string;                       // "04-23" | "2026-04"
  cost_usd: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  used_pct: number | null;             // weekly: snapshot value; monthly: null
  dollar_per_pct: number | null;       // weekly only
  delta_cost_pct: number | null;
  is_current: boolean;
  models: ModelCostRow[];
  week_start_at?: string;              // weekly only
  week_end_at?: string;                // weekly only
  cache_hit_pct?: number | null;       // v2.3 — populated by daily variant only for v1
}

export interface WeeklyEnvelope {
  rows: PeriodRow[];                  // 12 newest-first
  // ---- view-model unification additive scalars (Bundle 1) ----
  total_cost_usd?: number;
  total_tokens?:   number;
}
export interface MonthlyEnvelope {
  rows: PeriodRow[];                  // 12 newest-first
  // ---- view-model unification additive scalars (Bundle 1) ----
  total_cost_usd?: number;
  total_tokens?:   number;
}

// ---- Blocks panel (envelope §1.3) ------------------------------------

export type BlockAnchor = 'recorded' | 'heuristic';

export interface BlocksPanelRow {
  start_at: string;            // ISO-8601 UTC
  end_at:   string;            // ISO-8601 UTC, start_at + 5h
  anchor:   BlockAnchor;       // 'recorded' = real reset; 'heuristic' = floor-to-hour
  is_active: boolean;          // now_utc < end_at AND entries_count > 0
  cost_usd: number;
  models:   ModelCostRow[];    // sorted desc by cost
  label:    string;            // pre-formatted "HH:MM MMM DD" in local tz
}

export interface BlocksEnvelope {
  rows: BlocksPanelRow[];
  // ---- view-model unification additive scalars (issue #56) ----
  total_cost_usd?: number;
  total_tokens?:   number;
}

// /api/block/:start_at response — consumed by BlockModal.
export interface BlockDetailSample {
  t:   string;   // ISO-8601 UTC of the entry
  cum: number;   // running cumulative cost in USD
}

export interface BlockDetailBurnRate {
  tokens_per_minute: number;
  cost_per_hour: number;
}

export interface BlockDetailProjection {
  total_tokens: number;
  total_cost_usd: number;
  remaining_minutes: number;
}

export interface BlockDetail {
  start_at:      string;
  end_at:        string;
  actual_end_at: string | null;
  anchor:        BlockAnchor;
  is_active:     boolean;
  label:         string;
  entries_count: number;

  cost_usd:               number;
  total_tokens:           number;
  input_tokens:           number;
  output_tokens:          number;
  cache_creation_tokens:  number;
  cache_read_tokens:      number;
  cache_hit_pct:          number | null;

  models:     ModelCostRow[];
  burn_rate:  BlockDetailBurnRate | null;
  projection: BlockDetailProjection | null;
  samples:    BlockDetailSample[];
}

// ---- Daily panel (envelope §1.3) -------------------------------------

export interface DailyPanelRow {
  date:             string;    // local-tz YYYY-MM-DD
  label:            string;    // "MM-DD" — pre-formatted, mirrors Weekly idiom
  cost_usd:         number;
  is_today:         boolean;
  intensity_bucket: number;    // 0..5 — server-computed quintile bucket
  models:           ModelCostRow[];   // for tooltip text; not rendered as a bar
  // ---- v2.3 additions: Daily modal token + cache rollup ----
  input_tokens:          number;
  output_tokens:         number;
  cache_creation_tokens: number;
  cache_read_tokens:     number;
  total_tokens:          number;
  cache_hit_pct:         number | null;
}

export interface DailyEnvelope {
  rows:                DailyPanelRow[];
  quantile_thresholds: number[];      // length 5 when any non-zero day; [] otherwise.
                                      // Duplicates intentional — do NOT dedup.
  peak:                { date: string; cost_usd: number } | null;
  // ---- view-model unification additive scalars (Bundle 1) ----
  // Pre-computed gap-free total over `build_daily_view`'s rows; lets
  // DailyPanel.tsx swap its `rows.reduce(...)` for a single read.
  total_cost_usd?:     number;
  total_tokens?:       number;
}

// ======================================================================
// #294 S5 — source-aware dashboard read model (the S4 `sources` bundle)
// ======================================================================
//
// These types mirror the S4 snake_case wire contract, transcribed field-by-
// field from the Python builders (source of truth — NOT guessed):
//   - bin/_lib_dashboard_sources.py   — SourceDashboardState / bundle / all-compose
//   - bin/_cctally_dashboard_sources.py — build_codex_source_state + wire helpers
//   - bin/_cctally_tui.py::_tui_project_claude_source_data — Claude projection
//   - bin/_cctally_dashboard_envelope.py::_source_state_to_wire — the serializer
//
// Consumers tolerate unknown keys per the additive-evolution rule: unknown
// capability keys, unknown capability statuses, and unknown warning codes must
// degrade gracefully (generic labels), never crash. Do NOT treat these as
// exhaustive where the wire is additive.

export type SourceName = 'claude' | 'codex';
export type DashboardSelection = SourceName | 'all';
export type SourceAvailability = 'ok' | 'empty' | 'partial' | 'unavailable';
export type SourceFreshness = 'fresh' | 'stale';
export type CapabilityStatus =
  | 'supported'
  | 'derived'
  | 'unavailable'
  | 'deferred'
  | 'not_applicable';

// Per-capability support record. `status` is one of the five literals above,
// but an unknown status arriving on the wire must degrade generically, not
// throw — consumers read `status` as a plain string when gating.
export interface CapabilityRecord {
  status: CapabilityStatus;
  semantics?: string;
}

export interface SourceWarning {
  code: string;
  message: string;
  domain?: string;
}

// One atomically-published provider read model. `TData` is the provider's own
// `data` payload (ClaudeSourceData | CodexSourceData | AllSourceData); it is
// null before the first coherent generation (unavailable / hydrating).
export interface SourceEntry<TData> {
  availability: SourceAvailability;
  freshness: SourceFreshness;
  warnings: SourceWarning[];
  data_version: string;
  last_success_at: string | null;
  capabilities: Record<string, CapabilityRecord>;
  data: TData | null;
}

// ---- Codex provider vocabulary (build_codex_source_state) -------------

// Freshness inside a quota history/active row carries the physical-evidence
// state, which has MORE states than the top-level SourceFreshness (it can be
// 'future'/'unavailable' too). Kept as its own union so those extra states
// don't leak into SourceFreshness.
export type QuotaEvidenceFreshness = 'fresh' | 'stale' | 'future' | 'unavailable';
export type QuotaForecastStatus =
  | 'ok'
  | 'insufficient-history'
  | 'unavailable'
  | 'stale'
  | 'future';

export interface CodexQuotaForecast {
  status: QuotaForecastStatus;
  current_percent: number | null;
  rate_percent_per_hour: number | null;
  projected_percent: number | null;
  resets_at: string | null;
  remaining_seconds: number | null;
  sample_count: number;
  sample_span_seconds: number | null;
  confidence: 'high' | 'medium' | 'low' | null;
}

// A currently-active quota window (`quota.summary.active` + also surfaced in
// `hero.quota.active`). Carries the opaque `key`, percentages, reset, and
// freshness — but NO label/duration (those live on the matching history row,
// joined by key per §6.1).
export interface CodexQuotaActiveRow {
  key: string;
  current_percent: number;
  captured_at: string;
  resets_at: string;
  freshness: QuotaEvidenceFreshness;
  stale_after_seconds: number | null;
}

export interface CodexQuotaSummary {
  window_count: number;
  active_window_count: number;
  latest_percent: number | null;
  freshness: QuotaEvidenceFreshness;
  active: CodexQuotaActiveRow[];
}

// A retained quota history row — carries the `label` + `window_minutes` that the
// §6.1 join attaches to the active rows by matching `key`.
export interface CodexQuotaHistoryRow {
  key: string;
  source: 'codex';
  label: string;
  observed_slot: number;
  window_minutes: number | null;
  current_percent: number | null;
  captured_at: string | null;
  freshness: QuotaEvidenceFreshness;
  stale_after_seconds: number | null;
  forecast: CodexQuotaForecast;
}

export interface CodexQuotaMilestoneRow {
  key: string;
  source: 'codex';
  block_key: string;
  percent: number;
  captured_at: string;
}

// A durable quota-window block (`data.quota.blocks`, from _quota_wire). Distinct
// `block:` key namespace — the §6.1 quota-history join does NOT apply here.
export interface CodexQuotaBlockRow {
  key: string;
  source: 'codex';
  label: string;
  resets_at: string;
  current_percent: number | null;
  orphaned: boolean;
}

export interface CodexQuotaDomain {
  summary: CodexQuotaSummary;
  histories: CodexQuotaHistoryRow[];
  milestones: CodexQuotaMilestoneRow[];
  blocks: CodexQuotaBlockRow[];
}

export interface CodexBudgetPace {
  daily_usd: number | null;
  projected_low_usd: number | null;
  projected_high_usd: number | null;
  week_avg_projection_usd: number | null;
}

// The live configured-budget status (_configured_codex_budget_status). Uses the
// existing ok/warn/over verdict vocabulary — NOT the Verdict alias (which is
// ok/cap/capped for the Claude weekly ceiling).
export interface CodexBudgetStatus {
  period: string;
  budget_usd: number;
  spent_usd: number;
  remaining_usd: number;
  consumption_pct: number;
  verdict: 'ok' | 'warn' | 'over';
  low_confidence: boolean;
  window_start_at: string;
  window_end_at: string;
  recent_24h_usd: number;
  alert_thresholds: number[];
  pace: CodexBudgetPace;
}

// Durable budget-milestone history row (_budget_wire) — alert history, distinct
// from the live `status` above.
export interface CodexBudgetMilestoneRow {
  period_start_at: string;
  period: string;
  threshold: number;
  budget_usd: number;
  spent_usd: number;
  consumption_pct: number;
}

export interface CodexProjectedBudgetRow {
  period: string;
  threshold: number;
  projected_value: number;
  denominator: number;
  crossed_at: string | null;
  alerted_at: string | null;
}

export interface CodexBudgetDomain {
  status: CodexBudgetStatus | null;
  milestones: CodexBudgetMilestoneRow[];
  projected: CodexProjectedBudgetRow[];
}

// Codex hero counters — the five native token counters + cost, the quota
// summary, the configured budget, and the alert count.
export interface CodexHero {
  cost_usd: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  quota: CodexQuotaSummary;
  budget: CodexBudgetStatus | null;
  alerts: { count: number };
}

export interface CodexPeriodBucket {
  label: string;
  cost_usd: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  models: string[];
}

export interface CodexPeriodView {
  rows: CodexPeriodBucket[];
  total_cost_usd: number;
  total_tokens: number;
  display_tz: string;
}

export interface CodexPeriodsDomain {
  daily: CodexPeriodView;
  monthly: CodexPeriodView;
  weekly: CodexPeriodView;
}

export interface CodexSessionRow {
  key: string;
  source: 'codex';
  label: string;
  last_activity: string;
  cost_usd: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  models: string[];
}

export interface CodexSessionsDomain {
  rows: CodexSessionRow[];
  total_sessions: number;
  total_cost_usd: number;
  total_tokens: number;
}

export interface CodexProjectRow {
  key: string;
  source: 'codex';
  label: string;
  session_count: number;
  first_seen: string;
  last_seen: string;
  cost_usd: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}

export interface CodexProjectsDomain {
  rows: CodexProjectRow[];
  total_cost_usd: number;
  total_tokens: number;
}

// Codex source-owned alert rows (_alerts_wire) — a discriminated union on
// `axis`. The heterogeneous toast-pipeline `SourceAlertRow` union that spans
// Claude+Codex lands in Stage 2 (§6.7); these are the Codex `data.alerts.rows`.
export interface CodexBudgetAlertRow {
  key: string;
  source: 'codex';
  axis: 'codex_budget';
  period: string;
  threshold: number;
  value: number;
  created_at: string;
}
export interface CodexProjectedAlertRow {
  key: string;
  source: 'codex';
  axis: 'projected';
  period: string;
  threshold: number;
  value: number;
  created_at: string;
}
export interface CodexQuotaAlertRow {
  key: string;
  source: 'codex';
  axis: 'quota';
  threshold: number;
  severity: string;
  created_at: string;
}
export type CodexAlertRow =
  | CodexBudgetAlertRow
  | CodexProjectedAlertRow
  | CodexQuotaAlertRow;

export interface CodexSourceData {
  hero: CodexHero;
  periods: CodexPeriodsDomain;
  sessions: CodexSessionsDomain;
  quota: CodexQuotaDomain;
  budget: CodexBudgetDomain;
  projects: CodexProjectsDomain;
  alerts: { rows: CodexAlertRow[] };
}

// ---- Claude provider projection (_tui_project_claude_source_data) -----
//
// The Claude source data is the legacy dashboard envelope's already-rendered
// values re-placed under the S4 source contract, with native route identities
// replaced by opaque provider-qualified resource keys. Blobs that are direct
// copies of legacy envelope objects reuse the legacy interfaces above.

// A Claude session source row = the legacy SessionRow minus its raw
// `session_id`/`project_key` identities, plus the opaque `key` + `source`.
export interface ClaudeSessionSourceRow {
  key: string;
  source: 'claude';
  started_utc: string | null;
  duration_min: number;
  model: string;
  project: string;
  cost_usd: number | null;
  cache_hit_pct?: number | null;
  title?: string | null;
}

// A Claude project source row = the legacy projects current-week/trend row
// minus its raw `bucket_path`, plus the opaque `key` + `source`.
export interface ClaudeProjectSourceRow {
  key: string;
  source: 'claude';
  cost_usd?: number;
  attributed_pct?: number | null;
  sessions_count?: number;
  // trend rows carry the parallel weekly arrays instead
  weekly_cost?: number[];
  weekly_pct?: (number | null)[];
  sessions_per_week?: number[];
  first_seen_per_week?: (string | null)[];
  last_seen_per_week?: (string | null)[];
}

export interface ClaudeHero {
  cost_usd: number;
  total_tokens: number;
  header: HeaderEnvelope | null;
  current_week: CurrentWeekEnvelope | null;
  forecast: ForecastEnvelope | null;
  trend: TrendEnvelope | null;
}

export interface ClaudePeriodsDomain {
  daily: DailyEnvelope;
  monthly: MonthlyEnvelope;
  weekly: WeeklyEnvelope;
}

export interface ClaudeSessionsDomain {
  total?: number;
  sort_key?: string;
  rows: ClaudeSessionSourceRow[];
}

// The Claude projects domain keeps the legacy current_week/trend sub-shapes
// (with re-keyed rows) plus a flat `rows` route-lookup collection.
export interface ClaudeProjectsDomain {
  current_week: {
    week_label?: string | null;
    week_start_date?: string | null;
    week_start_at?: string | null;
    total_cost_usd?: number;
    rows: ClaudeProjectSourceRow[];
  };
  trend: {
    window_weeks?: number;
    weeks?: ProjectsTrendWeek[];
    projects: ClaudeProjectSourceRow[];
  };
  rows: ClaudeProjectSourceRow[];
}

// A Claude 5h-block source row = the legacy BlocksPanelRow with the opaque
// `key` + `source` added (no raw identity removed beyond `key`).
export interface ClaudeBlockSourceRow {
  key: string;
  source: 'claude';
  start_at: string;
  end_at: string;
  anchor: BlockAnchor;
  is_active: boolean;
  cost_usd: number;
  models: ModelCostRow[];
  label: string;
}

export interface ClaudeQuotaDomain {
  current_week: Record<string, unknown>;   // legacy current_week minus milestones
  blocks: ClaudeBlockSourceRow[];
  milestones: Array<Record<string, unknown>>;
  five_hour_milestones: Array<Record<string, unknown>>;
}

export interface ClaudeSourceData {
  hero: ClaudeHero;
  periods: ClaudePeriodsDomain;
  sessions: ClaudeSessionsDomain;
  projects: ClaudeProjectsDomain;
  quota: ClaudeQuotaDomain;
  budget: { forecast: ForecastEnvelope | null; settings: Record<string, unknown> | null };
  alerts: { rows: Array<Record<string, unknown>> };
}

// ---- The `all` composition (compose_all_state) ------------------------

export interface AllCombined {
  cost_usd: number;
  total_tokens: number;
}

export interface AllSourceData {
  combined: AllCombined | null;
  // The provider-native union (Claude + Codex source-owned rows). The toast
  // pipeline's discriminated `SourceAlertRow` union lands in Stage 2 (§6.7);
  // until then the rows stay `unknown` (each is one provider's own alert row
  // carrying `source`) so a typed provider-row array assigns without an
  // index-signature mismatch.
  alerts: { rows: unknown[] };
  providers: {
    claude: ClaudeSourceData | null;
    codex: CodexSourceData | null;
  };
}

// ---- Source-aware alert rows (§6.7) -----------------------------------
//
// The heterogeneous toast-pipeline union that spans both providers. Claude
// source alert rows are the legacy `AlertEntry` (id, axis, threshold, context,
// alerted_at…) with `source: 'claude'` and an opaque `key` added by the
// projection (`_tui_claude_resource_row`). That `key` embeds the row ORDINAL,
// so it is NOT stable across newer-row insertion and must never be used for
// dedup/identity — the stable Claude identity is the preserved `id`. Codex
// source alert rows are the lean `_alerts_wire` shapes (budget/projected carry
// `value`; quota carries `severity`, no `value`) whose `key` IS a stable native
// identity. Transcribed from bin/_cctally_tui.py (Claude rows ~2297-2320) and
// bin/_cctally_dashboard_sources.py::_alerts_wire (Codex rows).
export type ClaudeAlertSourceRow = AlertEntry & { source: 'claude'; key: string };

export type SourceAlertRow = ClaudeAlertSourceRow | CodexAlertRow;

// ---- The flat source map ----------------------------------------------
//
// `env.sources` on the wire — the FLAT per-source map. The three sibling bundle
// fields (`source_schema_version`, `default_source`, `source_order`) live at the
// envelope TOP LEVEL, NOT inside this object (see the `Envelope` fields above and
// the server's `_source_bundle_to_envelope`). There is deliberately no
// `SourcesBundle` wrapper type — that was the phantom nested shape (#294 S5 QA).
export interface SourcesMap {
  claude: SourceEntry<ClaudeSourceData>;
  codex: SourceEntry<CodexSourceData>;
  all: SourceEntry<AllSourceData>;
}

// ---- Qualified detail routes (§5.6) -----------------------------------
//
// `/api/source/<source>/<resource>/<key>` → `{source, resource, data}` where
// `data` is one of six adapter bodies discriminated by `detail_kind`. All six
// are transcribed from the qualified-route builders in bin/_cctally_dashboard.py
// (the Claude bodies are adapters that reshape/remove legacy fields — NOT the
// legacy route payloads). The two stable error envelopes render as friendly
// non-fatal messages.

export interface QualifiedDetailEnvelope<T> {
  source: SourceName;
  resource: 'session' | 'project' | 'block';
  data: T;
}

// Claude bodies (_source_safe_claude_*_detail).
export interface ClaudeSessionDetailBody {
  detail_kind: 'claude_session';
  key: string;
  started_utc: string | null;
  last_activity_utc: string | null;
  duration_min: number | null;
  models: SessionDetailModel[];
  input_tokens: number | null;
  cache_creation_tokens: number | null;
  cache_read_tokens: number | null;
  output_tokens: number | null;
  cache_hit_pct: number | null;
  cost_per_model: SessionDetailCostPerModel[];
  cost_total_usd: number | null;
}
export interface ClaudeProjectDetailBody {
  detail_kind: 'claude_project';
  key: string;
  window_weeks: number;
  window_cost_usd: number;
  window_attributed_pct: number | null;
  models: ProjectDetailModelRow[];
  sessions: Array<{
    started_at: string;
    last_activity_at: string;
    primary_model: string;
    cost_usd: number;
  }>;
  models_total: number;
  sessions_total: number;
}
export interface ClaudeBlockDetailBody {
  detail_kind: 'claude_block';
  key: string;
  start_at: string;
  end_at: string;
  actual_end_at: string | null;
  anchor: BlockAnchor;
  is_active: boolean;
  entries_count: number;
  cost_usd: number;
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  cache_hit_pct: number | null;
  models: ModelCostRow[];
  burn_rate: BlockDetailBurnRate | null;
  projection: BlockDetailProjection | null;
  samples: BlockDetailSample[];
}

// Codex bodies (_build_codex_*_detail).
export interface CodexModelBreakdown {
  modelName?: string;
  inputTokens?: number;
  cachedInputTokens?: number;
  outputTokens?: number;
  reasoningOutputTokens?: number;
  totalTokens?: number;
  cost?: number;
  isFallback?: boolean;
}
export interface CodexTokenTotals {
  cost_usd: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
}
export interface CodexSessionDetailBody extends CodexTokenTotals {
  detail_kind: 'codex_session';
  key: string;
  last_activity: string;
  models: string[];
  model_breakdowns: CodexModelBreakdown[];
}
export interface CodexProjectDetailBody extends CodexTokenTotals {
  detail_kind: 'codex_project';
  key: string;
  range_start: string;
  range_end: string;
  first_seen: string;
  last_seen: string;
  session_count: number;
  models: Array<{ model: string } & CodexTokenTotals>;
  sessions: Array<{ label: string; last_activity: string } & CodexTokenTotals>;
}
export interface CodexBlockDetailBody {
  detail_kind: 'codex_block';
  key: string;
  label: string;
  observed_slot: number;
  window_minutes: number | null;
  resets_at: string;
  current_percent: number | null;
  orphaned: boolean;
  freshness: string;
  observations: Array<{ captured_at: string; used_percent: number; resets_at: string }>;
  milestones: Array<{ percent: number; captured_at: string }>;
  forecast: {
    status: string;
    current_percent: number | null;
    projected_percent: number | null;
    resets_at: string | null;
  };
}

export type SourceDetailBody =
  | ClaudeSessionDetailBody
  | ClaudeProjectDetailBody
  | ClaudeBlockDetailBody
  | CodexSessionDetailBody
  | CodexProjectDetailBody
  | CodexBlockDetailBody;

// The two stable error envelopes (§5.6).
export interface SourceDetailErrorEnvelope {
  code: 'source_capability_unavailable' | 'source_resource_not_found';
  error: string;
}
