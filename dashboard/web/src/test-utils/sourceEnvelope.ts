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
  AccountCard,
  AllSourceData,
  ClaudeSourceData,
  CodexAccountScope,
  CodexQuotaForecast,
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

// ---- #416: the DECORATED (>1 real account) Codex shape ----------------
//
// The three conditional surfaces that appear together under decoration (R8):
// camelCase `accounts[]` cards, mixed-case `hero.cycles[]`, and the fully
// snake_case `account_scopes` children. `ACCOUNT_A` has real evidence,
// `ACCOUNT_B` has different evidence, and `ACCOUNT_EMPTY` is the reported
// symptom — a registered account with nothing in this cycle.
export const ACCOUNT_A = 'a'.repeat(32);
export const ACCOUNT_B = 'b'.repeat(32);
export const ACCOUNT_EMPTY = 'e'.repeat(32);

function accountCard(over: Partial<AccountCard> & { accountKey: string }): AccountCard {
  return {
    accountKey: over.accountKey,
    label: over.label ?? over.accountKey.slice(0, 4),
    plan: over.plan ?? 'pro',
    active: over.active ?? false,
    weeklyPercent: over.weeklyPercent ?? null,
    fiveHourPercent: over.fiveHourPercent ?? null,
    resetsAt: over.resetsAt ?? null,
    spendUsd: over.spendUsd ?? 0,
    inputTokens: over.inputTokens ?? 0,
    cachedInputTokens: over.cachedInputTokens ?? 0,
    outputTokens: over.outputTokens ?? 0,
    reasoningOutputTokens: over.reasoningOutputTokens ?? 0,
    totalTokens: over.totalTokens ?? 0,
    ...(over.unattributed ? { unattributed: true as const } : {}),
  };
}

function accountScope(over: {
  marker: string;
  is_empty?: boolean;
  cost?: number;
  alerts?: CodexAccountScope['alerts']['rows'];
}): CodexAccountScope {
  const cost = over.cost ?? 1;
  const period = {
    rows: over.is_empty ? [] : [{
      label: `${over.marker}-04-24`,
      cost_usd: cost,
      input_tokens: 10,
      cached_input_tokens: 2,
      output_tokens: 4,
      reasoning_output_tokens: 1,
      total_tokens: 17,
      models: ['gpt-5'],
    }],
    total_cost_usd: over.is_empty ? 0 : cost,
    total_tokens: over.is_empty ? 0 : 17,
    display_tz: 'UTC',
  };
  return {
    is_empty: over.is_empty ?? false,
    periods: { daily: period, monthly: period, weekly: period },
    sessions: {
      rows: over.is_empty ? [] : [{
        key: `session:${over.marker}`,
        source: 'codex' as const,
        label: `Session ${over.marker}`,
        last_activity: '2026-04-24T12:30:00Z',
        cost_usd: cost,
        input_tokens: 10,
        cached_input_tokens: 2,
        output_tokens: 4,
        reasoning_output_tokens: 1,
        total_tokens: 17,
        models: ['gpt-5'],
      }],
      total_sessions: over.is_empty ? 0 : 1,
      total_cost_usd: over.is_empty ? 0 : cost,
      total_tokens: over.is_empty ? 0 : 17,
    },
    projects: {
      rows: over.is_empty ? [] : [{
        key: `project:${over.marker}`,
        source: 'codex' as const,
        label: `proj-${over.marker}`,
        session_count: 1,
        first_seen: '2026-04-20T00:00:00Z',
        last_seen: '2026-04-24T12:30:00Z',
        cost_usd: cost,
        input_tokens: 10,
        cached_input_tokens: 2,
        output_tokens: 4,
        reasoning_output_tokens: 1,
        total_tokens: 17,
      }],
      total_cost_usd: over.is_empty ? 0 : cost,
      total_tokens: over.is_empty ? 0 : 17,
    },
    cache_report: null,
    budget: { status: null, milestones: [], projected: [] },
    quota: {
      summary: {
        window_count: over.is_empty ? 0 : 1,
        active_window_count: over.is_empty ? 0 : 1,
        latest_percent: over.is_empty ? null : 30,
        freshness: over.is_empty ? 'unavailable' : 'fresh',
        active: [],
      },
      histories: [],
      milestones: [],
      blocks: [],
      cycle_index: over.is_empty ? [] : [{
        key: `cycle:${over.marker}`,
        start_at_utc: '2026-04-23T00:00:00Z',
        end_at_utc: '2026-04-30T00:00:00Z',
        resets_at_utc: '2026-04-30T00:00:00Z',
        label: `${over.marker} cycle`,
        is_current: true,
        milestone_count: 1,
        block_count: 1,
        detail_stamp: `${over.marker}-stamp`,
      }],
    },
    alerts: { rows: over.alerts ?? [], actual_thresholds: [90], projected_thresholds: [90] },
  };
}

// The parent's per-account weekly quota rows. Under decoration the server
// stamps `account_key` on every `quota.histories` row, so the merged subtree
// carries ONE weekly history PER ACCOUNT — which is exactly why picking
// `histories.find(w => w.window_minutes === 10080)` publishes whichever account
// happens to sort first as "the" forecast (#416 QA P0).
function accountWeeklyHistory(
  accountKey: string,
  over: {
    percent: number;
    projected: number;
    confidence: CodexQuotaForecast['confidence'];
    marker: string;
  },
): CodexSourceData['quota']['histories'][number] {
  return {
    key: `quota:codex-weekly-${over.marker}`,
    source: 'codex',
    account_key: accountKey,
    label: '7-day limit',
    observed_slot: 0,
    window_minutes: 10080,
    current_percent: over.percent,
    captured_at: '2026-04-24T13:00:00Z',
    freshness: 'fresh',
    stale_after_seconds: 3600,
    forecast: {
      status: 'ok',
      current_percent: over.percent,
      rate_percent_per_hour: 1.5,
      projected_percent: over.projected,
      resets_at: '2026-04-30T00:00:00Z',
      remaining_seconds: 216000,
      sample_count: 10,
      sample_span_seconds: 86400,
      confidence: over.confidence,
    },
  } as CodexSourceData['quota']['histories'][number];
}

// #416 QA P2-1: the children are mirrored HERE rather than left to an opt-in
// second call. A decorated provider builds both halves from the SAME
// observations — `_quota_read_model` over the merged set for the parent, over
// each account's partition for its child — so a parent weekly row stamped
// `account_key: A` always implies A's child holds that row. The split builder
// let a test construct the opposite (a stamped parent over empty children),
// which is not a wire shape, and any selector that consults a child then reads
// as broken against a fixture rather than against the server.
export function makeDecoratedCodexSourceData(): CodexSourceData {
  return withAccountScopedQuotaHistories(makeDecoratedCodexParentOnly());
}

function makeDecoratedCodexParentOnly(): CodexSourceData {
  const base = makeCodexSourceData();
  // The representative account's weekly window is the one `hero.quota.active`
  // joins against, so it keeps its shipped key and simply gains the stamp. It
  // projects OVER the cap; the sibling is comfortably OK — the two verdicts the
  // reported defect showed as one.
  const siblingWeekly = accountWeeklyHistory(ACCOUNT_B, {
    percent: 12, projected: 31, confidence: 'low', marker: 'b',
  });
  // #416 QA P1 — the sibling's window is LIVE, so the server lists it in
  // `quota.summary.active` exactly like the representative account's. The
  // builder used to stamp a second weekly HISTORY without its matching active
  // row, which no wire ever emits: `_quota_read_model` appends an active row for
  // every non-model-scoped history whose `baseline.resets_at > now`. A gate that
  // reads that server-side liveness decision would have blanked a live window
  // against the old fixture, so the fidelity gap had to close first.
  const summary = {
    ...base.quota.summary,
    window_count: base.quota.summary.window_count + 1,
    active_window_count: base.quota.summary.active_window_count + 1,
    active: [
      ...base.quota.summary.active,
      {
        key: siblingWeekly.key,
        current_percent: 12.0,
        captured_at: '2026-04-24T13:00:00Z',
        resets_at: '2026-04-29T00:00:00Z',
        freshness: 'fresh' as const,
        stale_after_seconds: 3600,
      },
    ],
  };
  return {
    ...base,
    quota: {
      ...base.quota,
      summary,
      histories: [
        ...base.quota.histories.map((row) => ({
          ...row,
          account_key: ACCOUNT_A,
          ...(row.window_minutes === 10080
            ? {
              forecast: {
                ...row.forecast,
                projected_percent: 104,
                confidence: 'high' as const,
              },
            }
            : {}),
        })),
        siblingWeekly,
      ],
    },
    hero: {
      ...base.hero,
      // `hero.quota` is BUILT FROM `quota.summary`: the source builder passes
      // `quota["summary"]` straight into the hero, so the two agree at the
      // point of construction. They are not guaranteed equal in a DELIVERED
      // envelope — a captured production snapshot carried older `captured_at`
      // values in `hero.quota.active[]` than in `quota.summary.active[]` for
      // two rows, meaning the hero it shipped came from an earlier generation.
      // Keeping them in step here is still what a coherent single-generation
      // wire looks like, which is what a fixture should model.
      quota: summary,
      cycles: [
        {
          accountKey: ACCOUNT_A,
          window_minutes: 10080,
          start_at: '2026-04-23T00:00:00Z',
          resets_at: '2026-04-30T00:00:00Z',
          used_percent: 61,
          cost_usd: 8.0,
          total_tokens: 400000,
        },
        {
          accountKey: ACCOUNT_B,
          window_minutes: 10080,
          start_at: '2026-04-22T00:00:00Z',
          resets_at: '2026-04-29T00:00:00Z',
          used_percent: 12,
          cost_usd: 4.3,
          total_tokens: 152000,
        },
      ],
    },
    accounts: [
      accountCard({
        accountKey: ACCOUNT_A, label: 'work@example.com', weeklyPercent: 61,
        spendUsd: 8.0, inputTokens: 320000, totalTokens: 400000,
      }),
      accountCard({
        accountKey: ACCOUNT_B, label: 'personal@example.com', weeklyPercent: 12,
        spendUsd: 4.3, inputTokens: 120000, totalTokens: 152000,
      }),
      accountCard({ accountKey: ACCOUNT_EMPTY, label: 'quiet@example.com' }),
    ],
    account_scopes: {
      [ACCOUNT_A]: accountScope({
        marker: 'A',
        cost: 8.0,
        alerts: [
          {
            key: 'alert:codex-quota-a', source: 'codex', axis: 'quota',
            threshold: 90, severity: 'warn', created_at: '2026-04-24T09:00:00Z',
            account_key: ACCOUNT_A,
          },
          // A vendor-wide crossing: visible under focus, LABELLED vendor-wide,
          // never attributed to the focused account.
          {
            key: 'alert:codex-budget-90', source: 'codex', axis: 'codex_budget',
            period: 'calendar-month', threshold: 90, value: 90.5,
            created_at: '2026-04-20T00:00:00Z', account_key: '*',
          },
        ],
      }),
      [ACCOUNT_B]: accountScope({ marker: 'B', cost: 4.3 }),
      [ACCOUNT_EMPTY]: accountScope({ marker: 'E', is_empty: true }),
    },
  } satisfies CodexSourceData;
}

// The child scopes mirror the parent's shape, so each account's own weekly
// history lives in its own subtree — that is what a FOCUSED read renders.
// `makeDecoratedCodexSourceData` applies it, so the two halves can never
// disagree about which account owns which projection; it is exported because a
// transform that rewrites the PARENT (`withExpiredWeekly`,
// `withStaleButLiveWeekly`) must re-derive the children afterwards. Idempotent —
// every call rebuilds the children from the parent.
//
// Assumes each account's window carries a DISTINCT key, which is what the
// key-join below needs to attribute an active row. Two accounts sharing one
// `$CODEX_HOME` root collide on that key; `withSharedRootWeeklyWindows` writes
// those children out directly instead.
export function withAccountScopedQuotaHistories(
  data: CodexSourceData,
): CodexSourceData {
  const byAccount = new Map(
    (data.quota.histories ?? [])
      .filter((row) => row.account_key != null && row.window_minutes === 10080)
      .map((row) => [row.account_key as string, row] as const),
  );
  const scopes = { ...(data.account_scopes ?? {}) };
  const activeByKey = new Map(
    (data.quota.summary.active ?? []).map((row) => [row.key, row] as const),
  );
  for (const [key, scope] of Object.entries(scopes)) {
    const own = byAccount.get(key);
    // A child's `quota.summary.active` is built from the CHILD's observations,
    // so it holds exactly this account's live windows — the same rows the parent
    // lists, narrowed. Mirroring it here keeps the scoped fixture honest about
    // which of its windows is still running (#416 QA P1).
    const ownActive = own == null ? undefined : activeByKey.get(own.key);
    // The other three summary fields are DERIVED from the same active rows by
    // `_quota_read_model`, so they move together or the child contradicts
    // itself: `latest_percent` is a MAX over the active rows (`None` when there
    // are none), and `freshness` is `fresh` when every active row is fresh,
    // `unavailable` when there are no active rows at all, `stale` otherwise.
    // Leaving `accountScope()`'s `30` / `fresh` in place while zeroing
    // `active_window_count` produced a child that reported a latest percent and
    // fresh quota with no active window — a shape no wire emits, and one
    // `focusedHero` would have carried straight onto the hero.
    scopes[key] = {
      ...scope,
      quota: {
        ...scope.quota,
        histories: own == null ? [] : [own],
        summary: {
          ...scope.quota.summary,
          active_window_count: ownActive == null ? 0 : 1,
          latest_percent: ownActive?.current_percent ?? null,
          freshness: ownActive == null
            ? 'unavailable'
            : ownActive.freshness === 'fresh' ? 'fresh' : 'stale',
          active: ownActive == null ? [] : [ownActive],
        },
      },
    };
  }
  return { ...data, account_scopes: scopes };
}

// ---- #416 QA P2-1: two accounts sharing ONE `$CODEX_HOME` root ---------
//
// The quota row key is `dashboard_resource_key("quota", "codex",
// source_root_key, logical_limit_key, observed_slot, window_minutes)` and
// carries NO account; `_codex_logical_limit_key` carries none either (its
// `limitId` is the same literal for every account of one provider). Under a
// single root, two accounts' weekly windows are therefore TWO history rows with
// ONE key — `build_history` partitions by the full `QuotaWindowIdentity`
// (account included) while `dashboard_resource_key` does not, and
// `adopt_unidentified_observations` states the rule outright: "two identified
// accounts sharing an identical physical window key stay separate windows".
// `_codex_account_scopes_wire`'s own docstring names this the shape the
// per-account reads exist for ("`quota_window_blocks` and
// `quota_window_snapshots` both carry two rows when two accounts share one
// physical root"), so it is designed for, not hypothetical.
//
// `ACCOUNT_A`'s window is LIVE; `ACCOUNT_B`'s reset nine days ago. The parent's
// `quota.summary.active` consequently lists that ONE key ONCE, contributed by
// A — which is precisely why a key-only liveness lookup taken from the PARENT
// revives B.
//
// The children are written out here rather than mirrored through
// `withAccountScopedQuotaHistories`: that helper attributes an active row to a
// child BY KEY, and under a shared root the key no longer identifies the
// account. The server has no such problem — each child comes from
// `_quota_read_model` over that ACCOUNT's own observations — so constructing
// them directly is the faithful emulation.
export const SHARED_ROOT_WEEKLY_KEY = 'quota:codex-weekly';

export function withSharedRootWeeklyWindows(data: CodexSourceData): CodexSourceData {
  const weeklyOf = (accountKey: string) => (data.quota.histories ?? []).find(
    (row) => row.account_key === accountKey && row.window_minutes === 10080,
  )!;
  const liveWeekly = {
    ...weeklyOf(ACCOUNT_A),
    key: SHARED_ROOT_WEEKLY_KEY,
    current_percent: 78.2,
    captured_at: '2026-04-24T13:00:00Z',
    freshness: 'fresh' as const,
    forecast: {
      ...weeklyOf(ACCOUNT_A).forecast,
      status: 'ok' as const,
      current_percent: 78.2,
      projected_percent: 91.0,
      resets_at: '2026-04-30T00:00:00Z',
      confidence: 'high' as const,
    },
  };
  // Captured 2026-04-13 with a `resets_at` of 2026-04-19 — already past at
  // capture time, and nine days dead by the fixture clock (2026-04-24T13:07Z).
  const deadWeekly = {
    ...weeklyOf(ACCOUNT_B),
    key: SHARED_ROOT_WEEKLY_KEY,
    current_percent: 41.0,
    captured_at: '2026-04-13T09:00:00Z',
    freshness: 'stale' as const,
    forecast: {
      ...weeklyOf(ACCOUNT_B).forecast,
      status: 'stale' as const,
      current_percent: 41.0,
      projected_percent: null,
      resets_at: '2026-04-19T00:00:00Z',
      remaining_seconds: 0,
      confidence: 'medium' as const,
    },
  };
  const liveActive = {
    key: SHARED_ROOT_WEEKLY_KEY,
    current_percent: 78.2,
    captured_at: '2026-04-24T13:00:00Z',
    resets_at: '2026-04-30T00:00:00Z',
    freshness: 'fresh' as const,
    stale_after_seconds: 3600,
  };
  const others = (data.quota.histories ?? []).filter(
    (row) => row.window_minutes !== 10080,
  );
  // The parent's active list: every non-weekly window it already had, plus the
  // shared weekly key exactly ONCE. A's row contributed it; B's is absent.
  const active = [
    ...data.quota.summary.active.filter(
      (row) => !(data.quota.histories ?? []).some(
        (h) => h.key === row.key && h.window_minutes === 10080,
      ),
    ),
    liveActive,
  ];
  const summary = {
    ...data.quota.summary,
    window_count: others.length + 2,
    active_window_count: active.length,
    latest_percent: Math.max(...active.map((row) => row.current_percent)),
    freshness: 'fresh' as const,
    active,
  };
  const scopes = { ...(data.account_scopes ?? {}) };
  const childQuota = (
    histories: CodexSourceData['quota']['histories'],
    ownActive: typeof liveActive | null,
  ) => ({
    summary: {
      window_count: histories.length,
      active_window_count: ownActive == null ? 0 : 1,
      latest_percent: ownActive?.current_percent ?? null,
      freshness: (ownActive == null ? 'unavailable' : 'fresh') as 'unavailable' | 'fresh',
      active: ownActive == null ? [] : [ownActive],
    },
    histories,
    milestones: [],
    blocks: [],
    cycle_index: [],
  });
  scopes[ACCOUNT_A] = {
    ...scopes[ACCOUNT_A],
    quota: childQuota([liveWeekly], liveActive),
  } as CodexAccountScope;
  scopes[ACCOUNT_B] = {
    ...scopes[ACCOUNT_B],
    quota: childQuota([deadWeekly], null),
  } as CodexAccountScope;
  scopes[ACCOUNT_EMPTY] = {
    ...scopes[ACCOUNT_EMPTY],
    quota: childQuota([], null),
  } as CodexAccountScope;
  return {
    ...data,
    quota: { ...data.quota, summary, histories: [...others, liveWeekly, deadWeekly] },
    hero: {
      ...data.hero,
      quota: summary,
      // B has no live cycle, exactly as `hero_cycles_wire` emits it.
      cycles: (data.hero.cycles ?? []).filter((c) => c.accountKey !== ACCOUNT_B),
    },
    accounts: (data.accounts ?? []).map((card) => (
      card.accountKey === ACCOUNT_A
        ? { ...card, weeklyPercent: 78.2 }
        : card.accountKey === ACCOUNT_B
          ? { ...card, weeklyPercent: null, resetsAt: null }
          : card
    )),
    account_scopes: scopes,
  } as CodexSourceData;
}

export function makeCodexSourceEntry(
  overrides?: Partial<SourceEntry<CodexSourceData>>,
): SourceEntry<CodexSourceData> {
  return {
    availability: 'ok',
    freshness: 'fresh',
    domain_freshness: {
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    },
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
    domain_freshness: {
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    },
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
    domain_freshness: {
      hero: 'fresh',
      quota: 'fresh',
      sessions: 'fresh',
    },
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
    domain_freshness: {
      hero: 'stale',
      quota: 'stale',
      sessions: 'stale',
    },
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
