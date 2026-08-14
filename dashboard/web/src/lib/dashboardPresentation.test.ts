import { describe, expect, it } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import {
  presentationCacheReportComposition,
  presentationBlocks,
  presentationDailyRows,
  presentationForecastComposition,
  presentationPeriodRows,
  presentationProjects,
  presentationProviders,
  presentationTrend,
} from './dashboardPresentation';
import type {
  CodexPeriodBucket,
  DailyPanelRow,
  DashboardSelection,
  Envelope,
  PeriodRow,
} from '../types/envelope';

function cloneFixture(): Envelope {
  return structuredClone(fixture) as unknown as Envelope;
}

// #556 S2 §3.7 — the two aggregate adapters now return a discriminated
// outcome, so a withheld state can never collapse into an empty list. These
// unwrap an `available` result and fail loudly on anything else, which is what
// keeps a test from silently asserting over `[]`.
function dailyRowsOf(result: ReturnType<typeof presentationDailyRows>): DailyPanelRow[] {
  if (result.state !== 'available') {
    throw new Error(`expected an available daily outcome, got ${JSON.stringify(result)}`);
  }
  return result.rows;
}

function projectRowsOf(result: ReturnType<typeof presentationProjects>) {
  if (result.state !== 'available') {
    throw new Error(`expected an available projects outcome, got ${JSON.stringify(result)}`);
  }
  return result.rows;
}

// Give an envelope the v6 All contract: the rows-only Claude siblings plus the
// single public `aggregates` object. `sources.all.data.providers.*` stays null
// in the fixture, so the siblings go where `presentationProviders`' documented
// fallback resolves them.
function withV6Aggregates(env: Envelope, claudeDaily: DailyPanelRow[]): Envelope {
  const claude = env.sources!.claude.data!;
  claude.periods.daily_aggregate = { rows: claudeDaily.map((row) => ({ ...row })) };
  claude.projects.aggregate = claude.projects.aggregate ?? { rows: [] };
  env.sources!.all.data = {
    ...(env.sources!.all.data ?? { combined: null, alerts: { rows: [] }, providers: { claude: null, codex: null } }),
    aggregates: {
      range: {
        kind: 'absolute_range', label: 'Shared range',
        start_at: '2026-03-26T00:00:00Z', end_at: '2026-04-24T13:07:00Z',
      },
      projects: { state: 'available' },
      daily: { state: 'available' },
    },
  };
  return env;
}

function periodRow(label: string): PeriodRow {
  return {
    label, cost_usd: 1, total_tokens: 1, input_tokens: 1,
    output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0,
    used_pct: null, dollar_per_pct: null, delta_cost_pct: null,
    is_current: false, models: [],
  };
}

function codexPeriodRow(label: string): CodexPeriodBucket {
  return {
    label, cost_usd: 1, input_tokens: 1, cached_input_tokens: 0,
    output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 1,
    models: [],
  };
}

function dailyRow(day: number): DailyPanelRow {
  const suffix = String(day).padStart(2, '0');
  return {
    date: `2026-07-${suffix}`, label: `07-${suffix}`, cost_usd: day,
    is_today: day === 31, intensity_bucket: 1, models: [],
    input_tokens: 1, output_tokens: 0, cache_creation_tokens: 0,
    cache_read_tokens: 0, total_tokens: 1, cache_hit_pct: null,
  };
}

describe('provider-neutral dashboard presentation adapters', () => {
  it('composes distinct provider-labelled Forecast values in All mode', () => {
    const env = cloneFixture();
    env.forecast!.week_avg_projection_pct = 68.5;
    env.sources!.codex.data!.quota.histories.find(
      (row) => row.window_minutes === 10_080,
    )!.forecast.projected_percent = 74;

    const composition = presentationForecastComposition(env, 'all');

    expect(composition.selection).toBe('all');
    expect(composition.sections).toHaveLength(2);
    expect(composition.sections.map((section) => section.source)).toEqual(['claude', 'codex']);
    expect(composition.sections[0]).toMatchObject({
      label: 'Claude',
      status: 'available',
      value: { projected: 68.5, verdict: 'ok' },
    });
    expect(composition.sections[1]).toMatchObject({
      label: 'Codex',
      status: 'available',
      value: { projected: 74 },
    });
    expect('verdict' in composition).toBe(false);
  });

  it('composes distinct provider-native Cache reports without blending their facts', () => {
    const env = cloneFixture();
    const codexReport = structuredClone(env.cache_report!);
    codexReport.today.cache_hit_percent = 42;
    codexReport.today.net_usd = 12.5;
    codexReport.days[0].cache_hit_percent = 42;
    codexReport.days[0].net_usd = 12.5;
    env.sources!.codex.data!.cache_report = codexReport;

    const composition = presentationCacheReportComposition(env, 'all');

    expect(composition.sections).toHaveLength(2);
    expect(composition.sections[0]).toMatchObject({
      source: 'claude',
      label: 'Claude',
      status: 'available',
      value: { today: { cache_hit_percent: 87.3, net_usd: 3.1 } },
    });
    expect(composition.sections[1]).toMatchObject({
      source: 'codex',
      label: 'Codex',
      status: 'available',
      value: { today: { cache_hit_percent: 42, net_usd: 12.5 } },
    });
    expect(composition.sections[0].value).not.toBe(composition.sections[1].value);
  });

  it('keeps a missing provider section explicit instead of relabelling its sibling as All', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.cache_report = null;
    env.sources!.codex.capabilities.forensics = {
      status: 'unavailable',
      semantics: 'native cache report unavailable',
    };
    env.sources!.codex.warnings = [{
      code: 'codex_cache_unavailable',
      domain: 'forensics',
      message: 'Codex cache counters are unavailable.',
    }];

    const composition = presentationCacheReportComposition(env, 'all');

    expect(composition.sections[0]).toMatchObject({ source: 'claude', status: 'available' });
    expect(composition.sections[1]).toMatchObject({
      source: 'codex',
      status: 'unavailable',
      value: null,
      reason: 'Codex cache counters are unavailable.',
    });
  });

  it('maps every pooled Codex weekly owner key to its account label', () => {
    const env = cloneFixture();
    const codex = env.sources!.codex.data!;
    codex.accounts = [
      {
        accountKey: 'a'.repeat(32), label: 'work@example.com', plan: 'pro',
        active: true, weeklyPercent: 40, fiveHourPercent: null,
        resetsAt: '2026-04-30T00:00:00Z', spendUsd: 8,
        inputTokens: 1, cachedInputTokens: 0, outputTokens: 0,
        reasoningOutputTokens: 0, totalTokens: 1,
      },
      {
        accountKey: 'b'.repeat(32), label: 'personal@example.com', plan: 'pro',
        active: true, weeklyPercent: 97, fiveHourPercent: null,
        resetsAt: '2026-04-30T00:00:00Z', spendUsd: 12.1,
        inputTokens: 1, cachedInputTokens: 0, outputTokens: 0,
        reasoningOutputTokens: 0, totalTokens: 1,
      },
    ];
    const row = codex.periods.weekly.rows[0] as CodexPeriodBucket & {
      account_keys?: string[];
    };
    row.account_keys = ['a'.repeat(32), 'b'.repeat(32)];

    expect(presentationPeriodRows(env, 'codex', 'weekly')[0].account_labels)
      .toEqual(['work@example.com', 'personal@example.com']);
  });

  it('labels stale native Forecast evidence as degraded with its provider reason', () => {
    const env = cloneFixture();
    const weekly = env.sources!.codex.data!.quota.histories.find(
      (row) => row.window_minutes === 10_080,
    )!;
    weekly.forecast.status = 'stale';

    const codex = presentationForecastComposition(env, 'all').sections[1];

    expect(codex).toMatchObject({
      source: 'codex',
      status: 'degraded',
      reason: 'Codex forecast is stale.',
      value: { recent: weekly.current_percent },
    });
  });

  it('degrades Forecast from quota freshness while provider freshness stays coherent', () => {
    const env = cloneFixture();
    env.sources!.codex.domain_freshness = {
      hero: 'fresh',
      quota: 'stale',
      sessions: 'fresh',
    };

    expect(presentationForecastComposition(env, 'codex').sections[0]).toMatchObject({
      source: 'codex',
      status: 'degraded',
      reason: 'Codex quota data is stale.',
    });
  });

  it('names QUOTA, not hero, when a Claude percent observation goes stale', () => {
    // #556 S1 §4.7 — a consequence of repointing the axes, taken by S1 because
    // S1 causes it. `hero` now means current-cycle accounting resolvability and
    // `quota` means percent-observation age, so the Forecast section still
    // degrades on the same evidence but names the axis that actually described
    // it. `freshnessDomains` is `['hero', 'quota']` and `find` returns the
    // FIRST stale one, so before the repointing this read "Claude hero data is
    // stale."
    const env = cloneFixture();
    env.sources!.claude.domain_freshness = {
      hero: 'fresh',
      quota: 'stale',
      sessions: 'fresh',
    };

    expect(presentationForecastComposition(env, 'claude').sections[0]).toMatchObject({
      source: 'claude',
      status: 'degraded',
      reason: 'Claude quota data is stale.',
    });
  });

  it('keeps the accounting-backed Cache report available when Sessions is fresh', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.cache_report = structuredClone(env.cache_report!);
    env.sources!.codex.freshness = 'stale';
    env.sources!.codex.domain_freshness = {
      hero: 'stale',
      quota: 'stale',
      sessions: 'fresh',
    };

    expect(presentationCacheReportComposition(env, 'codex').sections[0]).toMatchObject({
      source: 'codex',
      status: 'available',
      reason: null,
    });
  });

  it('preserves provider-freshness fallback for legacy presentation entries', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.cache_report = structuredClone(env.cache_report!);
    env.sources!.codex.freshness = 'stale';
    delete env.sources!.codex.domain_freshness;

    expect(presentationCacheReportComposition(env, 'codex').sections[0]).toMatchObject({
      source: 'codex',
      status: 'degraded',
      reason: 'Codex data is stale.',
    });
  });

  it('labels an empty provider-native Cache report without hiding the other provider', () => {
    const env = cloneFixture();
    const codexReport = structuredClone(env.cache_report!);
    codexReport.is_empty = true;
    env.sources!.codex.data!.cache_report = codexReport;

    const composition = presentationCacheReportComposition(env, 'all');

    expect(composition.sections[0]).toMatchObject({ source: 'claude', status: 'available' });
    expect(composition.sections[1]).toMatchObject({
      source: 'codex',
      status: 'empty',
      reason: 'No Codex cache activity is available for this window.',
      // #443 F7 — the value is dropped alongside the status flip, so the
      // populated branch cannot render KPIs beside the empty chip.
      value: null,
    });
  });

  it('nulls the value when a report is empty so no KPI branch can render', () => {
    const env = cloneFixture();
    const claudeReport = structuredClone(env.cache_report!);
    claudeReport.is_empty = true;
    env.cache_report = claudeReport;

    const composition = presentationCacheReportComposition(env, 'all');
    const claude = composition.sections.find((s) => s.source === 'claude')!;
    expect(claude.status).toBe('empty');
    expect(claude.value).toBeNull();
  });

  it('keeps the value for a degraded section', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.cache_report = structuredClone(env.cache_report!);
    env.sources!.codex.freshness = 'stale';
    delete env.sources!.codex.domain_freshness;

    const composition = presentationCacheReportComposition(env, 'all');
    const codex = composition.sections.find((s) => s.source === 'codex')!;
    expect(codex.status).toBe('degraded');
    expect(codex.value).not.toBeNull();
  });

  it('does not degrade Forecast or Cache for an unrelated Projects warning', () => {
    const env = cloneFixture();
    const codexReport = structuredClone(env.cache_report!);
    env.sources!.codex.data!.cache_report = codexReport;
    env.sources!.codex.availability = 'partial';
    env.sources!.codex.warnings = [{
      code: 'codex_metadata_incomplete',
      domain: 'projects',
      message: 'Project metadata is incomplete.',
    }];

    expect(presentationForecastComposition(env, 'all').sections[1].status).toBe('available');
    expect(presentationCacheReportComposition(env, 'all').sections[1].status).toBe('available');
  });

  it.each(['claude', 'codex', 'all'] as DashboardSelection[])(
    'caps %s period history to the canonical 12-week / 8-month windows',
    (selection) => {
      const env = cloneFixture();
      const claudeWeekly = Array.from({ length: 14 }, (_, i) => periodRow(`2026-W${String(30 - i).padStart(2, '0')}`));
      const claudeMonthly = Array.from({ length: 10 }, (_, i) => periodRow(`2026-${String(10 - i).padStart(2, '0')}`));
      const codexWeekly = Array.from({ length: 18 }, (_, i) => codexPeriodRow(`2026-W${String(30 - i).padStart(2, '0')}`));
      const codexMonthly = Array.from({ length: 10 }, (_, i) => codexPeriodRow(`2026-${String(10 - i).padStart(2, '0')}`));
      env.weekly = { rows: claudeWeekly };
      env.monthly = { rows: claudeMonthly };
      env.sources!.claude.data!.periods.weekly.rows = claudeWeekly;
      env.sources!.claude.data!.periods.monthly.rows = claudeMonthly;
      env.sources!.codex.data!.periods.weekly.rows = codexWeekly;
      env.sources!.codex.data!.periods.monthly.rows = codexMonthly;
      env.sources!.all.data = null;

      expect(presentationPeriodRows(env, selection, 'weekly')).toHaveLength(
        selection === 'all' ? 24 : 12,
      );
      // #556 S2 §5.1 — the All monthly cap is now per PROVIDER, exactly as
      // weekly's already was, because the merge that used to collapse two
      // providers' months into one row is deleted.
      expect(presentationPeriodRows(env, selection, 'monthly')).toHaveLength(
        selection === 'all' ? 16 : 8,
      );
    },
  );

  it.each(['claude', 'codex', 'all'] as DashboardSelection[])(
    'caps %s Daily history at 30 newest rows with canonical compact labels',
    (selection) => {
      const env = cloneFixture();
      const claudeRows = Array.from({ length: 31 }, (_, i) => dailyRow(31 - i));
      const codexRows = Array.from({ length: 31 }, (_, i) => codexPeriodRow(`2026-07-${String(31 - i).padStart(2, '0')}`));
      env.daily = { rows: claudeRows, quantile_thresholds: [], peak: null };
      env.sources!.claude.data!.periods.daily.rows = claudeRows;
      env.sources!.codex.data!.periods.daily.rows = codexRows;
      // Under All the canonical shape is the BOUNDED sibling, never the
      // legacy top-level panel (§6.3a).
      withV6Aggregates(env, claudeRows);

      const rows = dailyRowsOf(presentationDailyRows(env, selection));
      expect(rows).toHaveLength(30);
      expect(rows[0].date).toBe('2026-07-31');
      expect(rows[29].date).toBe('2026-07-02');
      expect(rows.every((row) => /^\d{2}-\d{2}$/.test(row.label))).toBe(true);
    },
  );

  it('All combines compatible daily accounting rows exactly once', () => {
    const env = cloneFixture();
    const claude = env.sources!.claude.data!;
    const codex = env.sources!.codex.data!;
    claude.periods.daily.rows = [{
      date: '2026-04-24', label: '04-24', cost_usd: 8.4, is_today: false,
      intensity_bucket: 3, models: [], input_tokens: 10, output_tokens: 5,
      cache_creation_tokens: 2, cache_read_tokens: 3, total_tokens: 20,
      cache_hit_pct: 20,
    }];
    codex.periods.daily.rows = [{
      label: '2026-04-24', cost_usd: 12.3, input_tokens: 30,
      cached_input_tokens: 7, output_tokens: 8, reasoning_output_tokens: 2,
      total_tokens: 40, models: ['gpt-5'],
    }];
    // The All outcome owns its canonical shape, so the bounded sibling is
    // what the merge reads — never the legacy top-level panel (§6.3a).
    withV6Aggregates(env, claude.periods.daily.rows);

    const rows = dailyRowsOf(presentationDailyRows(env, 'all'));
    expect(rows).toHaveLength(claude.periods.daily.rows.length);
    const combined = rows.find((row) => row.date === '2026-04-24');
    expect(combined).toMatchObject({
      date: '2026-04-24', input_tokens: 40,
      cache_read_tokens: 10, output_tokens: 15, total_tokens: 60,
    });
    expect(combined!.cost_usd).toBeCloseTo(20.7, 9);
    // #556 S2 §6.1 — the combined ratio is WITHHELD, not recomputed. Claude's
    // ratio is cache-read over input plus cache creation and read; Codex's
    // input is already cache-inclusive, so no ratio over their sum describes
    // anything. The `cacheEligibleInput` compensation this assertion used to
    // pin went with it.
    expect(combined!.cache_hit_pct).toBeNull();
  });

  it('All falls back to sibling provider entries when nested providers are absent', () => {
    const env = cloneFixture();
    env.sources!.all.data = null;
    const providers = presentationProviders(env, 'all');
    expect(providers.claude).toBe(env.sources!.claude.data);
    expect(providers.codex).toBe(env.sources!.codex.data);
  });

  it('Codex weekly periods preserve provider-native quota usage and $/1%', () => {
    const env = cloneFixture();
    Object.assign(env.sources!.codex.data!.periods.weekly.rows[0], {
      start_at: '2026-07-13T00:00:00Z',
      end_at: '2026-07-20T00:00:00Z',
      used_pct: 25,
      dollar_per_pct: 0.4,
    });
    const rows = presentationPeriodRows(env, 'codex', 'weekly');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows[0]).toMatchObject({ used_pct: 25, dollar_per_pct: 0.4 });
    expect(rows[0].models[0]).toMatchObject({ display: 'Codex', cost_pct: 100 });
  });

  it('retains Codex native token categories without changing the reconciled total', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.periods.weekly.rows = [{
      label: 'Native cycle', cost_usd: 12, input_tokens: 1_200,
      cached_input_tokens: 300, output_tokens: 400,
      reasoning_output_tokens: 100, total_tokens: 1_600, models: ['gpt-5'],
    }];
    env.sources!.codex.data!.periods.daily.rows = [{
      label: '2026-07-20', cost_usd: 12, input_tokens: 1_200,
      cached_input_tokens: 300, output_tokens: 400,
      reasoning_output_tokens: 100, total_tokens: 1_600, models: ['gpt-5'],
    }];

    const weekly = presentationPeriodRows(env, 'codex', 'weekly')[0];
    const daily = dailyRowsOf(presentationDailyRows(env, 'codex')).find((row) => row.cost_usd === 12);

    expect(weekly.codex_tokens).toEqual({
      input_tokens: 1_200, cached_input_tokens: 300,
      output_tokens: 400, reasoning_output_tokens: 100, total_tokens: 1_600,
    });
    expect(daily?.codex_tokens).toEqual(weekly.codex_tokens);
    expect(weekly.total_tokens).toBe(1_600);
    expect(daily?.total_tokens).toBe(1_600);
  });

  it('All keeps non-colliding weekly quota rows provider-attributed', () => {
    const env = cloneFixture();
    const claude = env.weekly.rows.map((row, index) => ({
      ...row,
      label: `Claude week ${index + 1}`,
      used_pct: 60 + index,
      dollar_per_pct: 1.4 + index / 10,
    }));
    const template = env.sources!.codex.data!.periods.weekly.rows[0];
    env.sources!.claude.data!.periods.weekly.rows = claude;
    env.sources!.codex.data!.periods.weekly.rows = [{
      ...template,
      label: 'Codex cycle A',
      used_pct: 31,
      dollar_per_pct: 0.75,
    }];
    env.sources!.all.data = null;

    const rows = presentationPeriodRows(env, 'all', 'weekly') as Array<PeriodRow & {
      source?: 'claude' | 'codex';
    }>;

    expect(rows).toHaveLength(claude.length + 1);
    expect(rows.map((row) => [row.source, row.label, row.used_pct, row.dollar_per_pct])).toEqual([
      ['claude', 'Claude week 1', 60, 1.4],
      ['claude', 'Claude week 2', 61, 1.5],
      ['codex', 'Codex cycle A', 31, 0.75],
    ]);
  });

  it('Codex period cost deltas keep the shared fractional ratio contract', () => {
    const env = cloneFixture();
    const template = env.sources!.codex.data!.periods.weekly.rows[0];
    env.sources!.codex.data!.periods.weekly.rows = [
      { ...template, label: '07-18 06:24', cost_usd: 639.31 },
      { ...template, label: '07-16 07:16', cost_usd: 418.35 },
    ];

    const rows = presentationPeriodRows(env, 'codex', 'weekly');

    expect(rows[0].delta_cost_pct).toBeCloseTo((639.31 - 418.35) / 418.35);
    expect(rows[1].delta_cost_pct).toBeNull();
  });

  it('keeps the canonical $/1% Trend title and values for Codex', () => {
    const env = cloneFixture();
    Object.assign(env.sources!.codex.data!.periods.weekly.rows[0], {
      used_pct: 20,
      dollar_per_pct: 0.5,
    });

    const trend = presentationTrend(env, 'codex');
    expect(trend.title).toBe('$/1% Trend');
    expect(trend.chartLabel).toBe('$/1% trend:');
    expect(trend.valueLabel).toBe('$/1%');
    expect(trend.rows[0]).toMatchObject({ used_pct: 20, dollar_per_pct: 0.5 });
  });

  it('All exposes separate Claude and Codex trend series instead of one quota series', () => {
    const env = cloneFixture();
    Object.assign(env.sources!.codex.data!.periods.weekly.rows[0], {
      used_pct: 20,
      dollar_per_pct: 0.5,
    });

    const trend = presentationTrend(env, 'all') as ReturnType<typeof presentationTrend> & {
      sections?: Array<{ source: 'claude' | 'codex'; rows: unknown[]; historyRows: unknown[] }>;
    };

    expect(trend.rows).toEqual([]);
    expect(trend.sections?.map((section) => section.source)).toEqual(['claude', 'codex']);
    expect(trend.sections?.[0].rows).toEqual(
      env.trend!.weeks.map((row) => ({ ...row, source: 'claude' })),
    );
    expect(trend.sections?.[0].historyRows).toEqual(
      env.trend!.history.map((row) => ({ ...row, source: 'claude' })),
    );
    expect(trend.sections?.[1].rows).toHaveLength(1);
  });

  it('maps real Codex per-model costs into canonical model segments', () => {
    const env = cloneFixture();
    const bucket = env.sources!.codex.data!.periods.monthly.rows[0];
    bucket.cost_usd = 10;
    bucket.model_breakdowns = [
      { modelName: 'gpt-5.6-sol', cost: 7 },
      { modelName: 'gpt-5.6-terra', cost: 3 },
    ];

    expect(presentationPeriodRows(env, 'codex', 'monthly')[0].models).toMatchObject([
      { model: 'gpt-5.6-sol', display: '5.6-sol', cost_pct: 70 },
      { model: 'gpt-5.6-terra', display: '5.6-terra', cost_pct: 30 },
    ]);
  });

  it('maps real Codex daily model breakdowns instead of a synthetic source row', () => {
    const env = cloneFixture();
    const bucket = env.sources!.codex.data!.periods.daily.rows[0];
    bucket.cost_usd = 10;
    bucket.model_breakdowns = [
      { modelName: 'gpt-5.6-sol', cost: 7 },
      { modelName: 'gpt-5.6-terra', cost: 3 },
    ];

    const row = dailyRowsOf(presentationDailyRows(env, 'codex')).find((item) => item.cost_usd === 10);
    expect(row?.models).toMatchObject([
      { model: 'gpt-5.6-sol', cost_pct: 70 },
      { model: 'gpt-5.6-terra', cost_pct: 30 },
    ]);
  });

  it('normalizes Codex periods to the same newest-first order as Claude', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.periods.monthly.rows = [
      { label: '2026-06', cost_usd: 1, input_tokens: 1, cached_input_tokens: 0, output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 1, models: [] },
      { label: '2026-07', cost_usd: 2, input_tokens: 2, cached_input_tokens: 0, output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 2, models: [] },
    ];
    expect(presentationPeriodRows(env, 'codex', 'monthly').map((row) => row.label)).toEqual(['2026-07', '2026-06']);
  });

  it('gap-fills Codex daily rows to the canonical Claude calendar shape', () => {
    const env = cloneFixture();
    env.daily.rows = env.daily.rows.slice(0, 3);
    env.sources!.codex.data!.periods.daily.rows = [{
      label: env.daily.rows[1].date,
      cost_usd: 4.5,
      input_tokens: 10,
      cached_input_tokens: 8,
      output_tokens: 2,
      reasoning_output_tokens: 1,
      total_tokens: 12,
      models: ['gpt-5.6-sol'],
    }];

    const rows = dailyRowsOf(presentationDailyRows(env, 'codex'));
    expect(rows.map((row) => row.date)).toEqual(
      env.daily.rows.map((row) => row.date),
    );
    expect(rows[1]).toMatchObject({ cost_usd: 4.5, total_tokens: 12 });
    expect(rows[0]).toMatchObject({
      cost_usd: 0,
      intensity_bucket: 0,
      models: [],
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_tokens: 0,
      cache_read_tokens: 0,
      total_tokens: 0,
      cache_hit_pct: null,
    });
  });

  it('shows only real Codex 5-hour activity blocks with model splits', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.quota.blocks = [
      {
        key: 'block:weekly', source: 'codex', label: '7-day limit',
        window_minutes: 10_080, start_at: '2026-07-13T00:00:00Z',
        end_at: '2026-07-20T00:00:00Z', resets_at: '2026-07-20T00:00:00Z',
        current_percent: 15, orphaned: false, is_active: true,
        cost_usd: 0, model_breakdowns: [],
      },
      {
        key: 'block:five-hour', source: 'codex', label: '10:00 Jul 18 UTC',
        window_minutes: 300, start_at: '2026-07-18T10:00:00Z',
        end_at: '2026-07-18T15:00:00Z', resets_at: '2026-07-18T15:00:00Z',
        current_percent: 30, orphaned: false, is_active: true, cost_usd: 10,
        model_breakdowns: [
          { modelName: 'gpt-5.6-sol', cost: 7 },
          { modelName: 'gpt-5.6-terra', cost: 3 },
        ],
      },
    ];

    const rows = presentationBlocks(env, 'codex');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      key: 'block:five-hour', value: 10, valueLabel: '$10.00',
      start_at: '2026-07-18T10:00:00Z', end_at: '2026-07-18T15:00:00Z',
    });
    expect(rows[0].models).toMatchObject([
      { model: 'gpt-5.6-sol', cost_pct: 70 },
      { model: 'gpt-5.6-terra', cost_pct: 30 },
    ]);
  });

  it('preserves the server-issued Claude block key in All mode', () => {
    const env = cloneFixture();
    env.sources!.claude.data!.quota.blocks = [{
      ...env.blocks!.rows[0],
      key: 'block:opaque-server-issued',
      source: 'claude',
    }];

    const row = presentationBlocks(env, 'all').find((item) => item.source === 'claude');
    expect(row?.key).toBe('block:opaque-server-issued');
  });

  // #556 S2 §5.1 — Monthly is UNMERGED.
  //
  // #294 S5 §6.2 states that daily and monthly rows render per active source
  // and that buckets are never merged. #312 §7.4, written one day later,
  // explicitly authorises the DAILY aggregation and supersedes it there.
  // Nothing authorises the monthly merge, and the merge was never safe: it
  // nulled `used_pct` and `dollar_per_pct` only when two rows collided on a
  // label, and it recomputed one model split across two providers' families.
  it('keeps each provider its own monthly rows under All', () => {
    const env = cloneFixture();
    env.sources!.claude.data!.periods.monthly.rows = [
      { label: '2026-04', cost_usd: 10, total_tokens: 4, input_tokens: 2,
        output_tokens: 1, cache_creation_tokens: 1, cache_read_tokens: 0,
        used_pct: 40, dollar_per_pct: 0.25, delta_cost_pct: null,
        is_current: true, models: [] },
    ];
    env.sources!.codex.data!.periods.monthly.rows = [
      { label: '2026-04', cost_usd: 7, input_tokens: 3, cached_input_tokens: 1,
        output_tokens: 1, reasoning_output_tokens: 0, total_tokens: 5,
        models: ['gpt-5'] },
    ];

    const rows = presentationPeriodRows(env, 'all', 'monthly');

    // Two rows sharing one label, each keeping its own source — not one row
    // labelled `all`.
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.source)).toEqual(['claude', 'codex']);
    expect(rows.every((row) => row.label === '2026-04')).toBe(true);
    expect(rows.some((row) => row.source === 'all')).toBe(false);
    // The Claude row keeps the two quota fields the merge used to null.
    expect(rows[0].used_pct).toBe(40);
    expect(rows[0].dollar_per_pct).toBe(0.25);
  });

  it('caps each provider independently under All monthly, as weekly already does', () => {
    const env = cloneFixture();
    env.sources!.claude.data!.periods.monthly.rows = Array.from(
      { length: 12 },
      (_, i) => ({
        label: `2026-${String(12 - i).padStart(2, '0')}`, cost_usd: 1,
        total_tokens: 1, input_tokens: 1, output_tokens: 0,
        cache_creation_tokens: 0, cache_read_tokens: 0, used_pct: null,
        dollar_per_pct: null, delta_cost_pct: null, is_current: false,
        models: [],
      }),
    );
    env.sources!.codex.data!.periods.monthly.rows = Array.from(
      { length: 12 },
      (_, i) => codexPeriodRow(`2025-${String(12 - i).padStart(2, '0')}`),
    );

    const rows = presentationPeriodRows(env, 'all', 'monthly');
    expect(rows.filter((row) => row.source === 'claude')).toHaveLength(8);
    expect(rows.filter((row) => row.source === 'codex')).toHaveLength(8);
  });

  it('reads the published label off the bounded ranking instead of searching for it', () => {
    // #556 S2 §4.3 — the deleted alternative joined the opaque provider rows to
    // the legacy envelope by their `(cost_usd, sessions_count)` tuple and fell
    // back to `row.key` whenever that search missed, which printed an opaque
    // key as visible copy. The bounded sibling publishes the label, so there is
    // nothing left to search for and nothing left to miss.
    const env = cloneFixture();
    env.sources!.claude.data!.projects.aggregate = {
      rows: [{
        key: 'project:opaque-qualified-key',
        label: 'cctally-dev',
        source: 'claude',
        cost_usd: 8,
        sessions_count: 2,
        drillable: true,
      }],
    };
    // Two identical accounting tuples in the legacy collection, which is what
    // made the old reverse search ambiguous. Nothing consults them now.
    env.projects = {
      current_week: {
        week_label: null, week_start_date: null, week_start_at: null,
        total_cost_usd: 16,
        rows: [
          { key: 'other-a', bucket_path: '/w/a', cost_usd: 8, attributed_pct: null, sessions_count: 2 },
          { key: 'other-b', bucket_path: '/w/b', cost_usd: 8, attributed_pct: null, sessions_count: 2 },
        ],
      },
      trend: { window_weeks: 0, weeks: [], projects: [] },
    };
    withV6Aggregates(env, env.daily!.rows);

    const row = projectRowsOf(presentationProjects(env, 'all'))
      .find((item) => item.source === 'claude')!;
    expect(row.key).toBe('project:opaque-qualified-key');
    expect(row.label).toBe('cctally-dev');
    expect(row.drillable).toBe(true);
  });
});
