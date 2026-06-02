// SSE envelope — hand-transcribed from snapshot_to_envelope in
// bin/cctally. When Python adds a field, update here.

export type Verdict = 'ok' | 'cap' | 'capped';

export interface Envelope {
  envelope_version: number;
  generated_at: string | null;
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

export type AlertAxis = 'weekly' | 'five_hour' | 'budget' | 'projected';

// Projected axis (issue #121): the `metric` discriminator selects which
// week-average projection fired (weekly-% against the cap vs budget-$
// against the target). Top-level on the envelope row AND mirrored inside
// `context` so the metric-aware renderer can read either.
export type ProjectedMetric = 'weekly_pct' | 'budget_usd';

export interface AlertEntry {
  id: string;                    // "axis:window_key:threshold"
  axis: AlertAxis;
  threshold: number;             // integer in [1, 100]
  // Severity color authority (Task F): emitted by the Python kernel's
  // `severity_for(threshold)` (amber <95 / red >=95). Optional so a stale
  // envelope (older server) still renders — consumers fall back to deriving
  // it from `threshold` when absent. See lib/alertAxis.ts `alertSeverity`.
  severity?: 'amber' | 'red';
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
    // Budget axis (issue #19): equiv-$ budget threshold crossings.
    week_start_at?: string;
    budget_usd?: number;
    spent_usd?: number;
    consumption_pct?: number;
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

export type ChipKey = 'opus' | 'sonnet' | 'haiku' | 'other';

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
