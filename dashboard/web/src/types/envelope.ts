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
}

// ---- Threshold-actions alerts (T5/T8) --------------------------------

export type AlertAxis = 'weekly' | 'five_hour';

export interface AlertEntry {
  id: string;                    // "axis:window_key:threshold"
  axis: AlertAxis;
  threshold: number;             // integer in [1, 100]
  crossed_at: string;            // ISO-8601 UTC
  alerted_at: string;            // ISO-8601 UTC
  context: {
    week_start_date?: string;
    cumulative_cost_usd?: number;
    dollars_per_percent?: number | null;
    five_hour_window_key?: number;
    block_start_at?: string;
    block_cost_usd?: number;
    primary_model?: string | null;
  };
}

export interface AlertsSettingsEnvelope {
  enabled: boolean;
  weekly_thresholds: number[];
  five_hour_thresholds: number[];
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
}

export interface Milestone {
  percent: number;
  crossed_at_utc: string | null;
  cumulative_usd: number;
  marginal_usd: number | null;
  five_hour_pct_at_cross: number | null;
}

// Snake_case to match the Python envelope; see CLAUDE.md re. snake/camel
// inconsistency between dashboard envelope and CLI --json (intentional).
export interface FiveHourBlockEnvelope {
  block_start_at: string;                    // ISO 8601 with offset
  seven_day_pct_at_block_start: number | null;
  seven_day_pct_delta_pp: number | null;     // null on crossed-reset or missing anchor
  crossed_seven_day_reset: boolean;
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
  cost_usd: number | null;
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

export interface WeeklyEnvelope  { rows: PeriodRow[]; }   // 12 newest-first
export interface MonthlyEnvelope { rows: PeriodRow[]; }   // 12 newest-first

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
}
