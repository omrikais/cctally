// #294 S5 — shared source-bundle fixture builders for co-located tests.
//
// These mirror the representative `sources` bundle added to
// `__tests__/fixtures/envelope.json` so both fixture conventions agree (there
// is no single chokepoint — see the plan's Global Constraints). Every builder
// returns a plain object checked with `satisfies` against the transcribed wire
// types in `types/envelope.ts`, so a drift between a builder and the type is a
// compile error.
//
// Defaults: Claude `ok`, Codex `ok` with two quota windows, `all` with a
// non-null `combined`. Pass a shallow override object to any builder to tweak
// one field (mirrors the `{...base, ...over}` convention used by the session /
// basket fixture builders).
import type {
  AllSourceData,
  ClaudeSourceData,
  CodexSourceData,
  SourceEntry,
  SourcesMap,
} from '../types/envelope';

// The four S4 source fields as they appear on the REAL wire: the flat per-source
// `sources` map plus its three top-level sibling scalars. Spread onto an
// `Envelope` (NOT nested under `sources`) — see `_source_bundle_to_envelope` in
// bin/_cctally_dashboard_envelope.py and the guard in
// __tests__/sourceWireShape.test.ts. Fields are REQUIRED here (unlike the
// additive-optional `Envelope` fields) so test bodies can mutate
// `slice.sources.codex` without null-guarding.
export interface SourceEnvelopeSlice {
  source_schema_version: number;
  default_source: string;
  source_order: string[];
  sources: SourcesMap;
}

// ---- Codex ------------------------------------------------------------

export function makeCodexSourceData(): CodexSourceData {
  const activeA = {
    key: 'quota:codex-5h',
    current_percent: 42.0,
    captured_at: '2026-04-24T13:00:00Z',
    resets_at: '2026-04-24T18:00:00Z',
    freshness: 'fresh' as const,
    stale_after_seconds: 900,
  };
  const activeB = {
    key: 'quota:codex-weekly',
    current_percent: 61.0,
    captured_at: '2026-04-24T13:00:00Z',
    resets_at: '2026-04-30T00:00:00Z',
    freshness: 'fresh' as const,
    stale_after_seconds: 3600,
  };
  const summary = {
    window_count: 2,
    active_window_count: 2,
    latest_percent: 61.0,
    freshness: 'fresh' as const,
    active: [activeA, activeB],
  };
  const budgetStatus = {
    period: 'calendar-month',
    budget_usd: 100.0,
    spent_usd: 38.5,
    remaining_usd: 61.5,
    consumption_pct: 38.5,
    verdict: 'ok' as const,
    low_confidence: false,
    window_start_at: '2026-04-01T00:00:00Z',
    window_end_at: '2026-05-01T00:00:00Z',
    recent_24h_usd: 4.2,
    alert_thresholds: [90, 100],
    pace: {
      daily_usd: 1.6,
      projected_low_usd: 48.0,
      projected_high_usd: 55.0,
      week_avg_projection_usd: 50.0,
    },
  };
  return {
    hero: {
      cost_usd: 12.3,
      input_tokens: 480000,
      cached_input_tokens: 120000,
      output_tokens: 64000,
      reasoning_output_tokens: 8000,
      total_tokens: 552000,
      cycle: {
        window_minutes: 10080,
        start_at: '2026-04-23T00:00:00Z',
        resets_at: '2026-04-30T00:00:00Z',
      },
      quota: summary,
      budget: budgetStatus,
      alerts: { count: 1 },
    },
    periods: {
      daily: {
        rows: [
          {
            label: '04-24',
            cost_usd: 12.3,
            input_tokens: 480000,
            cached_input_tokens: 120000,
            output_tokens: 64000,
            reasoning_output_tokens: 8000,
            total_tokens: 552000,
            models: ['gpt-5'],
          },
        ],
        total_cost_usd: 12.3,
        total_tokens: 552000,
        display_tz: 'UTC',
      },
      monthly: {
        rows: [
          {
            label: '2026-04',
            cost_usd: 38.5,
            input_tokens: 1500000,
            cached_input_tokens: 400000,
            output_tokens: 200000,
            reasoning_output_tokens: 25000,
            total_tokens: 1700000,
            models: ['gpt-5'],
          },
        ],
        total_cost_usd: 38.5,
        total_tokens: 1700000,
        display_tz: 'UTC',
      },
      weekly: {
        rows: [
          {
            label: '04-20',
            cost_usd: 20.1,
            input_tokens: 800000,
            cached_input_tokens: 200000,
            output_tokens: 100000,
            reasoning_output_tokens: 12000,
            total_tokens: 900000,
            models: ['gpt-5'],
          },
        ],
        total_cost_usd: 20.1,
        total_tokens: 900000,
        display_tz: 'UTC',
      },
    },
    sessions: {
      rows: [
        {
          key: 'session:codex-a',
          source: 'codex',
          label: 'Session 1',
          last_activity: '2026-04-24T12:30:00Z',
          cost_usd: 6.4,
          input_tokens: 240000,
          cached_input_tokens: 60000,
          output_tokens: 32000,
          reasoning_output_tokens: 4000,
          total_tokens: 276000,
          models: ['gpt-5'],
        },
        {
          key: 'session:codex-b',
          source: 'codex',
          label: 'Session 2',
          last_activity: '2026-04-24T11:00:00Z',
          cost_usd: 5.9,
          input_tokens: 240000,
          cached_input_tokens: 60000,
          output_tokens: 32000,
          reasoning_output_tokens: 4000,
          total_tokens: 276000,
          models: ['gpt-5-codex'],
        },
      ],
      total_sessions: 2,
      total_cost_usd: 12.3,
      total_tokens: 552000,
    },
    quota: {
      summary,
      histories: [
        {
          key: 'quota:codex-5h',
          source: 'codex',
          label: '5-hour limit',
          observed_slot: 0,
          window_minutes: 300,
          current_percent: 42.0,
          captured_at: '2026-04-24T13:00:00Z',
          freshness: 'fresh',
          stale_after_seconds: 900,
          forecast: {
            status: 'ok',
            current_percent: 42.0,
            rate_percent_per_hour: 8.0,
            projected_percent: 74.0,
            resets_at: '2026-04-24T18:00:00Z',
            remaining_seconds: 18000,
            sample_count: 6,
            sample_span_seconds: 3600,
            confidence: 'high',
          },
        },
        {
          key: 'quota:codex-weekly',
          source: 'codex',
          label: 'Weekly limit',
          observed_slot: 0,
          window_minutes: 10080,
          current_percent: 61.0,
          captured_at: '2026-04-24T13:00:00Z',
          freshness: 'fresh',
          stale_after_seconds: 3600,
          forecast: {
            status: 'ok',
            current_percent: 61.0,
            rate_percent_per_hour: 1.5,
            projected_percent: 80.0,
            resets_at: '2026-04-30T00:00:00Z',
            remaining_seconds: 216000,
            sample_count: 10,
            sample_span_seconds: 86400,
            confidence: 'medium',
          },
        },
      ],
      milestones: [
        {
          key: 'quota_milestone:codex-a',
          source: 'codex',
          block_key: 'block:codex-5h',
          percent: 40,
          captured_at: '2026-04-24T12:45:00Z',
        },
      ],
      blocks: [
        {
          key: 'block:codex-5h',
          source: 'codex',
          label: '13:00 Apr 24 UTC',
          window_minutes: 300,
          start_at: '2026-04-24T13:00:00Z',
          end_at: '2026-04-24T18:00:00Z',
          resets_at: '2026-04-24T18:00:00Z',
          current_percent: 42.0,
          orphaned: false,
          is_active: true,
          cost_usd: 12.3,
          model_breakdowns: [
            { modelName: 'gpt-5', cost: 8.0 },
            { modelName: 'gpt-5-codex', cost: 4.3 },
          ],
        },
      ],
    },
    budget: {
      status: budgetStatus,
      milestones: [
        {
          period_start_at: '2026-04-01T00:00:00Z',
          period: 'calendar-month',
          threshold: 90,
          budget_usd: 100.0,
          spent_usd: 90.5,
          consumption_pct: 90.5,
        },
      ],
      projected: [
        {
          period: 'calendar-month',
          threshold: 90,
          projected_value: 92.0,
          denominator: 100.0,
          crossed_at: '2026-04-20T00:00:00Z',
          alerted_at: '2026-04-20T00:00:05Z',
        },
      ],
    },
    projects: {
      rows: [
        {
          key: 'project:codex-alpha',
          source: 'codex',
          label: 'alpha',
          session_count: 3,
          first_seen: '2026-04-20T00:00:00Z',
          last_seen: '2026-04-24T12:30:00Z',
          cost_usd: 8.0,
          input_tokens: 320000,
          cached_input_tokens: 80000,
          output_tokens: 40000,
          reasoning_output_tokens: 5000,
          total_tokens: 360000,
        },
      ],
      total_cost_usd: 12.3,
      total_tokens: 552000,
    },
    alerts: {
      rows: [
        {
          key: 'alert:codex-budget-90',
          source: 'codex',
          axis: 'codex_budget',
          period: 'calendar-month',
          threshold: 90,
          value: 90.5,
          created_at: '2026-04-20T00:00:00Z',
        },
      ],
    },
  } satisfies CodexSourceData;
}

export function makeCodexSourceEntry(
  overrides?: Partial<SourceEntry<CodexSourceData>>,
): SourceEntry<CodexSourceData> {
  return {
    availability: 'ok',
    freshness: 'fresh',
    warnings: [],
    data_version: 'codex:v1',
    last_success_at: '2026-04-24T13:07:00Z',
    capabilities: {
      hero: { status: 'supported', semantics: 'native-reset-cycle' },
      daily: { status: 'supported', semantics: 'calendar-day' },
      monthly: { status: 'supported', semantics: 'calendar-month' },
      weekly: { status: 'supported', semantics: 'calendar-week' },
      sessions: { status: 'supported', semantics: 'inclusive-input-tokens' },
      forensics: { status: 'supported', semantics: 'inclusive-input-token-reuse' },
      quota: { status: 'derived', semantics: 'native-windows' },
      budget: { status: 'supported', semantics: 'calendar-period' },
      projects: { status: 'supported', semantics: 'qualified-attribution' },
      alerts: { status: 'supported', semantics: 'provider-native' },
    },
    data: makeCodexSourceData(),
    ...overrides,
  } satisfies SourceEntry<CodexSourceData>;
}

// ---- Claude -----------------------------------------------------------

export function makeClaudeSourceData(): ClaudeSourceData {
  return {
    hero: {
      cost_usd: 8.4,
      total_tokens: 9950400,
      header: null,
      current_week: null,
      forecast: null,
      trend: null,
    },
    periods: {
      daily: { rows: [], quantile_thresholds: [], peak: null },
      monthly: { rows: [] },
      weekly: { rows: [] },
    },
    sessions: {
      total: 1,
      sort_key: 'started_desc',
      rows: [
        {
          key: 'session:claude-a',
          source: 'claude',
          started_utc: '2026-04-24T10:00:00Z',
          duration_min: 15,
          model: 'claude-opus-4-8',
          project: 'project-00',
          cost_usd: 1.5,
        },
      ],
    },
    projects: {
      current_week: {
        week_label: 'Apr 21–28',
        week_start_date: '2026-04-21',
        week_start_at: '2026-04-21T00:00:00Z',
        total_cost_usd: 8.0,
        rows: [
          {
            key: 'project:claude-alpha',
            source: 'claude',
            cost_usd: 8.0,
            attributed_pct: 100.0,
            sessions_count: 1,
          },
        ],
      },
      trend: { window_weeks: 4, weeks: [], projects: [] },
      rows: [
        {
          key: 'project:claude-alpha',
          source: 'claude',
          cost_usd: 8.0,
          attributed_pct: 100.0,
          sessions_count: 1,
        },
      ],
    },
    quota: {
      current_week: { used_pct: 17.4 },
      blocks: [],
      milestones: [],
      five_hour_milestones: [],
    },
    budget: { forecast: null, settings: {} },
    alerts: { rows: [] },
  } satisfies ClaudeSourceData;
}

export function makeClaudeSourceEntry(
  overrides?: Partial<SourceEntry<ClaudeSourceData>>,
): SourceEntry<ClaudeSourceData> {
  return {
    availability: 'ok',
    freshness: 'fresh',
    warnings: [],
    data_version: 'claude:v1',
    last_success_at: '2026-04-24T13:07:00Z',
    capabilities: {
      hero: { status: 'supported', semantics: 'subscription-week' },
      daily: { status: 'supported', semantics: 'calendar-day' },
      monthly: { status: 'supported', semantics: 'calendar-month' },
      weekly: { status: 'supported', semantics: 'subscription-week' },
      sessions: { status: 'supported', semantics: 'legacy-session-rollup' },
      forensics: { status: 'supported', semantics: 'legacy-projection' },
      quota: { status: 'supported', semantics: 'subscription-week' },
      budget: { status: 'supported', semantics: 'subscription-week' },
      projects: { status: 'supported', semantics: 'legacy-projection' },
      alerts: { status: 'supported', semantics: 'provider-native' },
    },
    data: makeClaudeSourceData(),
    ...overrides,
  } satisfies SourceEntry<ClaudeSourceData>;
}

// ---- All --------------------------------------------------------------

// Compose an `all` entry from a Claude + Codex entry (mirrors the Python
// compose_all_state): combined = provider hero cost/total-token sums; the
// providers block references each source's own `data`.
export function makeAllSourceEntry(
  claude: SourceEntry<ClaudeSourceData> = makeClaudeSourceEntry(),
  codex: SourceEntry<CodexSourceData> = makeCodexSourceEntry(),
  overrides?: Partial<SourceEntry<AllSourceData>>,
): SourceEntry<AllSourceData> {
  const codexHero = codex.data?.hero;
  const combined =
    claude.data && codexHero
    && codexHero.cost_usd != null
    && codexHero.total_tokens != null
      ? {
          cost_usd: claude.data.hero.cost_usd + codexHero.cost_usd,
          total_tokens: claude.data.hero.total_tokens + codexHero.total_tokens,
        }
      : null;
  // The `all` alert union mirrors the Python `_combined_alert_rows`: each
  // provider's OWN rows (filtered to `source === provider`) concatenated in
  // declared source order, then sorted by `created_at` desc (rows without a
  // `created_at`, e.g. the Claude legacy-field rows, sink last). #294 S5 Task 7.
  const claudeAlertRows = (claude.data?.alerts.rows ?? []).filter(
    (r) => (r as { source?: string }).source === 'claude',
  );
  const codexAlertRows = (codex.data?.alerts.rows ?? []).filter(
    (r) => (r as { source?: string }).source === 'codex',
  );
  const unionAlertRows: unknown[] = [...claudeAlertRows, ...codexAlertRows].sort((a, b) =>
    String((b as { created_at?: string }).created_at ?? '').localeCompare(
      String((a as { created_at?: string }).created_at ?? ''),
    ),
  );
  return {
    availability: 'ok',
    freshness: 'fresh',
    warnings: [],
    data_version: 'all:v1',
    last_success_at: '2026-04-24T13:07:00Z',
    capabilities: {
      hero: { status: 'derived', semantics: 'compatible-provider-totals' },
      quota: { status: 'not_applicable', semantics: 'provider-native' },
      budget: { status: 'not_applicable', semantics: 'provider-native' },
      alerts: { status: 'derived', semantics: 'provider-native-union' },
    },
    data: {
      combined,
      alerts: { rows: unionAlertRows },
      providers: { claude: claude.data, codex: codex.data },
    },
    ...overrides,
  } satisfies SourceEntry<AllSourceData>;
}

// ---- Hydrating (§5.2 bootstrap detection) -----------------------------

// The honest no-ingest state: capabilities `{}`, `data: null`, `warnings: []`,
// `last_success_at: null`. The seam's `isHydratingEntry` keys off exactly this
// shape (NOT on `availability`, which the server publishes as `partial`).
export function makeHydratingEntry(): SourceEntry<never> {
  return {
    availability: 'partial',
    freshness: 'stale',
    warnings: [],
    data_version: 'hydrating',
    last_success_at: null,
    capabilities: {},
    data: null,
  } satisfies SourceEntry<never>;
}

// ---- Sources map + envelope slice -------------------------------------

// The FLAT per-source map that lands at `env.sources` on the wire.
export function makeSourcesMap(overrides?: Partial<SourcesMap>): SourcesMap {
  const claude = makeClaudeSourceEntry();
  const codex = makeCodexSourceEntry();
  return {
    claude,
    codex,
    all: makeAllSourceEntry(claude, codex),
    ...overrides,
  } satisfies SourcesMap;
}

// The four source fields SPREAD at the envelope top level — the shape the server
// actually emits. Compose an `Envelope` as `{ ...base, ...makeSourceEnvelope() }`
// (or feed it directly to `updateSnapshot` when only the source fields matter).
export function makeSourceEnvelope(
  overrides?: Partial<SourceEnvelopeSlice>,
): SourceEnvelopeSlice {
  return {
    source_schema_version: 1,
    default_source: 'claude',
    source_order: ['claude', 'codex', 'all'],
    sources: makeSourcesMap(),
    ...overrides,
  };
}
