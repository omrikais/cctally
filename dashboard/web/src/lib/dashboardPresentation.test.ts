import { describe, expect, it } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import {
  presentationCacheReportComposition,
  presentationCacheDays,
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
      value: codexReport,
    });
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
      expect(presentationPeriodRows(env, selection, 'monthly')).toHaveLength(8);
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
      env.sources!.all.data = null;

      const rows = presentationDailyRows(env, selection);
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
    env.sources!.all.data = null;

    const rows = presentationDailyRows(env, 'all');
    expect(rows).toHaveLength(env.daily.rows.length);
    const combined = rows.find((row) => row.date === '2026-04-24');
    expect(combined).toMatchObject({
      date: '2026-04-24', input_tokens: 40,
      cache_read_tokens: 10, output_tokens: 15, total_tokens: 60,
    });
    expect(combined!.cost_usd).toBeCloseTo(20.7, 9);
    expect(combined!.cache_hit_pct).toBeCloseTo(10 / 43 * 100, 9);
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
    const daily = presentationDailyRows(env, 'codex').find((row) => row.cost_usd === 12);

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

    const row = presentationDailyRows(env, 'codex').find((item) => item.cost_usd === 10);
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

  it('uses inclusive Codex input as the cache-hit denominator exactly once', () => {
    const env = cloneFixture();
    env.sources!.codex.data!.periods.daily.rows = [{
      label: '2026-07-18', cost_usd: 1, input_tokens: 100,
      cached_input_tokens: 98, output_tokens: 1, reasoning_output_tokens: 0,
      total_tokens: 101, models: ['gpt-5.6-sol'],
    }];

    expect(presentationCacheDays(env, 'codex')?.[0].cache_hit_percent).toBe(98);
  });

  it('uses the provider-computed Codex cache report instead of zero-dollar synthesis', () => {
    const env = cloneFixture();
    const report = structuredClone(env.cache_report!);
    report.days[0].saved_usd = 12.5;
    report.days[0].net_usd = 12.5;
    (env.sources!.codex.data! as unknown as { cache_report: typeof report }).cache_report = report;

    expect(presentationCacheDays(env, 'codex')?.[0]).toMatchObject({
      saved_usd: 12.5,
      net_usd: 12.5,
    });
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

    const rows = presentationDailyRows(env, 'codex');
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

  it('keeps the qualified Claude project key but renders its canonical label in All', () => {
    const env = cloneFixture();
    const projected = env.sources!.claude.data!.projects.current_week.rows[0];
    const cost = projected.cost_usd ?? 8;
    const sessions = projected.sessions_count ?? 1;
    env.projects = {
      current_week: {
        week_label: null, week_start_date: null, week_start_at: null,
        total_cost_usd: cost,
        rows: [{ key: 'cctally-dev', bucket_path: '/workspace/cctally-dev', cost_usd: cost, attributed_pct: projected.attributed_pct ?? null, sessions_count: sessions }],
      },
      trend: { window_weeks: 0, weeks: [], projects: [] },
    };
    const legacy = env.projects!.current_week.rows[0];
    projected.key = 'project:opaque-qualified-key';
    projected.cost_usd = cost;
    projected.sessions_count = sessions;
    env.sources!.all.data = null;
    const row = presentationProjects(env, 'all')!.find((item) => item.source === 'claude')!;
    expect(row.key).toBe('project:opaque-qualified-key');
    expect(row.label).toBe(legacy.key);
  });
});
