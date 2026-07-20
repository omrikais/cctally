import type {
  AllSourceData,
  BlocksPanelRow,
  CacheReportDailyRow,
  CodexPeriodBucket,
  CodexQuotaBlockRow,
  CodexSourceData,
  DailyPanelRow,
  DashboardSelection,
  Envelope,
  ModelCostRow,
  PeriodRow,
  SourceName,
  SourceWarning,
  TrendRow,
} from '../types/envelope';
import { modelChipClass } from './model';

// Provider-neutral presentation adapters.  The dashboard cards consume these
// shapes; provider-specific wire vocabulary ends here.  Claude remains the
// visual and semantic baseline, while Codex and All are mapped into the same
// card bodies without inventing quota percentages or accounting rows.

const PERIOD_HISTORY_CAP = { weekly: 12, monthly: 8 } as const;
const DAILY_HISTORY_CAP = 30;

export interface PresentationProviders {
  selection: DashboardSelection;
  claude: AllSourceData['providers']['claude'];
  codex: CodexSourceData | null;
  hydrating: boolean;
  warnings: SourceWarning[];
}

export function presentationProviders(
  env: Envelope | null,
  selection: DashboardSelection,
): PresentationProviders {
  if (selection === 'claude') {
    return {
      selection,
      claude: env?.sources?.claude?.data ?? null,
      codex: null,
      hydrating: env == null || !!env.hydrating,
      warnings: env?.sources?.claude?.warnings ?? [],
    };
  }
  if (selection === 'codex') {
    const entry = env?.sources?.codex;
    return {
      selection,
      claude: null,
      codex: entry?.data ?? null,
      hydrating: env == null || (entry?.data == null && entry?.last_success_at == null && (entry?.warnings.length ?? 0) === 0),
      warnings: entry?.warnings ?? [],
    };
  }
  const entry = env?.sources?.all;
  const data = entry?.data ?? null;
  return {
    selection,
    claude: data?.providers.claude ?? env?.sources?.claude?.data ?? null,
    codex: data?.providers.codex ?? env?.sources?.codex?.data ?? null,
    hydrating: env == null || (data == null && entry?.last_success_at == null && (entry?.warnings.length ?? 0) === 0),
    warnings: entry?.warnings ?? [],
  };
}

function sourceModels(cost: number, source: SourceName): ModelCostRow[] {
  return cost > 0 ? [{
    model: source,
    display: source === 'claude' ? 'Claude' : 'Codex',
    chip: source === 'claude' ? 'opus' : 'other',
    cost_usd: cost,
    cost_pct: 100,
  }] : [];
}

function codexModelRows(
  totalCost: number,
  breakdowns: CodexPeriodBucket['model_breakdowns'],
  compactDisplay = false,
): ModelCostRow[] {
  return (breakdowns ?? []).flatMap((item): ModelCostRow[] => {
      const model = item.modelName?.trim();
      const cost = item.cost;
      if (!model || cost == null || !Number.isFinite(cost)) return [];
      return [{
        model,
        display: compactDisplay ? model.replace(/^gpt-/i, '') : model,
        chip: modelChipClass(model),
        cost_usd: cost,
        cost_pct: totalCost > 0 ? cost / totalCost * 100 : 0,
      }];
    });
}

function codexPeriodRow(row: CodexPeriodBucket, index: number): PeriodRow {
  const breakdownModels = codexModelRows(row.cost_usd, row.model_breakdowns, true);
  const models = breakdownModels.length > 0
    ? breakdownModels
    : sourceModels(row.cost_usd, 'codex');
  return {
    label: row.label,
    cost_usd: row.cost_usd,
    total_tokens: row.total_tokens,
    input_tokens: row.input_tokens,
    output_tokens: row.output_tokens + row.reasoning_output_tokens,
    cache_creation_tokens: 0,
    cache_read_tokens: row.cached_input_tokens,
    used_pct: row.used_pct ?? null,
    dollar_per_pct: row.dollar_per_pct ?? null,
    delta_cost_pct: null,
    is_current: index === 0,
    models,
    week_start_at: row.start_at,
    week_end_at: row.end_at,
  };
}

function recomputeModelPct(models: ModelCostRow[], total: number): ModelCostRow[] {
  return models.map((model) => ({
    ...model,
    cost_pct: total > 0 ? (model.cost_usd / total) * 100 : 0,
  }));
}

function mergePeriodRows(claudeRows: PeriodRow[], codexRows: PeriodRow[]): PeriodRow[] {
  const merged = new Map<string, PeriodRow>();
  for (const row of [...claudeRows, ...codexRows]) {
    const old = merged.get(row.label);
    if (!old) {
      merged.set(row.label, { ...row, models: [...row.models] });
      continue;
    }
    const cost = old.cost_usd + row.cost_usd;
    merged.set(row.label, {
      ...old,
      cost_usd: cost,
      total_tokens: old.total_tokens + row.total_tokens,
      input_tokens: old.input_tokens + row.input_tokens,
      output_tokens: old.output_tokens + row.output_tokens,
      cache_creation_tokens: old.cache_creation_tokens + row.cache_creation_tokens,
      cache_read_tokens: old.cache_read_tokens + row.cache_read_tokens,
      used_pct: null,
      dollar_per_pct: null,
      models: recomputeModelPct([...old.models, ...row.models], cost),
    });
  }
  return [...merged.values()].sort((a, b) => b.label.localeCompare(a.label));
}

export function presentationPeriodRows(
  env: Envelope | null,
  selection: DashboardSelection,
  period: 'weekly' | 'monthly',
): PeriodRow[] {
  const providers = presentationProviders(env, selection);
  const legacy = selection === 'claude'
    ? env?.[period]?.rows ?? []
    : providers.claude?.periods?.[period]?.rows ?? [];
  const codex = [...(providers.codex?.periods?.[period]?.rows ?? [])]
    .sort((a, b) => b.label.localeCompare(a.label))
    .map(codexPeriodRow)
    .map((row, index, allRows) => ({
      ...row,
      delta_cost_pct: allRows[index + 1]?.cost_usd
        ? (row.cost_usd - allRows[index + 1].cost_usd) / allRows[index + 1].cost_usd
        : null,
    }));
  const rows = selection === 'all'
    ? mergePeriodRows(legacy, codex)
    : selection === 'codex' ? codex : legacy;
  return rows.slice(0, PERIOD_HISTORY_CAP[period]);
}

function dailyDate(label: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(label)) return label;
  if (/^\d{2}-\d{2}$/.test(label)) return `${new Date().getFullYear()}-${label}`;
  return label;
}

function intensityRows(rows: DailyPanelRow[]): DailyPanelRow[] {
  const positive = rows.map((row) => row.cost_usd).filter((cost) => cost > 0).sort((a, b) => a - b);
  return rows.map((row) => {
    const rank = positive.length === 0 || row.cost_usd <= 0
      ? 0
      : Math.min(5, Math.max(1, Math.ceil((positive.indexOf(row.cost_usd) + 1) / positive.length * 5)));
    return { ...row, intensity_bucket: rank };
  });
}

function codexDailyRow(row: CodexPeriodBucket): DailyPanelRow {
  const date = dailyDate(row.label);
  const breakdownModels = codexModelRows(row.cost_usd, row.model_breakdowns, true);
  return {
    date,
    label: /^\d{4}-\d{2}-\d{2}$/.test(date) ? date.slice(5) : row.label,
    cost_usd: row.cost_usd,
    is_today: false,
    intensity_bucket: 0,
    models: breakdownModels.length > 0 ? breakdownModels : sourceModels(row.cost_usd, 'codex'),
    input_tokens: row.input_tokens,
    output_tokens: row.output_tokens + row.reasoning_output_tokens,
    cache_creation_tokens: 0,
    cache_read_tokens: row.cached_input_tokens,
    total_tokens: row.total_tokens,
    cache_hit_pct: row.input_tokens > 0 ? row.cached_input_tokens / row.input_tokens * 100 : null,
  };
}

function emptyDailyRow(template: DailyPanelRow): DailyPanelRow {
  return {
    date: template.date,
    label: template.label,
    cost_usd: 0,
    is_today: template.is_today,
    intensity_bucket: 0,
    models: [],
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    total_tokens: 0,
    cache_hit_pct: null,
  };
}

function gapFillDailyRows(
  rows: DailyPanelRow[],
  canonicalShape: DailyPanelRow[],
): DailyPanelRow[] {
  if (canonicalShape.length === 0) return rows;
  const byDate = new Map(rows.map((row) => [row.date, row]));
  const canonicalDates = new Set(canonicalShape.map((row) => row.date));
  const shaped = canonicalShape.map((template) => {
    const row = byDate.get(template.date);
    return row == null
      ? emptyDailyRow(template)
      : { ...row, label: template.label, is_today: template.is_today };
  });
  const extras = rows.filter((row) => !canonicalDates.has(row.date));
  return [...shaped, ...extras].sort((a, b) => b.date.localeCompare(a.date));
}

export function presentationDailyRows(env: Envelope | null, selection: DashboardSelection): DailyPanelRow[] {
  const providers = presentationProviders(env, selection);
  const claudeRows = selection === 'claude'
    ? env?.daily?.rows ?? []
    : providers.claude?.periods.daily.rows ?? [];
  const codexRows = [...(providers.codex?.periods.daily.rows ?? [])]
    .sort((a, b) => b.label.localeCompare(a.label))
    .map(codexDailyRow);
  if (selection === 'claude') return claudeRows.slice(0, DAILY_HISTORY_CAP);
  const canonicalShape = env?.daily?.rows ?? [];
  if (selection === 'codex') {
    return intensityRows(gapFillDailyRows(codexRows, canonicalShape)).slice(0, DAILY_HISTORY_CAP);
  }
  const merged = new Map<string, DailyPanelRow>();
  for (const row of [...claudeRows, ...codexRows]) {
    const old = merged.get(row.date);
    if (!old) {
      merged.set(row.date, { ...row, models: [...row.models] });
      continue;
    }
    const cost = old.cost_usd + row.cost_usd;
    // Claude input excludes cache reads while Codex input is cache-inclusive.
    // All rows are merged Claude-first, then Codex, so combine the two native
    // denominators without counting either provider's cached input twice.
    const cacheEligibleInput = old.input_tokens + old.cache_read_tokens + row.input_tokens;
    merged.set(row.date, {
      ...old,
      cost_usd: cost,
      input_tokens: old.input_tokens + row.input_tokens,
      output_tokens: old.output_tokens + row.output_tokens,
      cache_creation_tokens: old.cache_creation_tokens + row.cache_creation_tokens,
      cache_read_tokens: old.cache_read_tokens + row.cache_read_tokens,
      total_tokens: old.total_tokens + row.total_tokens,
      cache_hit_pct: cacheEligibleInput > 0
        ? (old.cache_read_tokens + row.cache_read_tokens) / cacheEligibleInput * 100
        : null,
      models: recomputeModelPct([...old.models, ...row.models], cost),
    });
  }
  const combined = [...merged.values()].sort((a, b) => b.date.localeCompare(a.date));
  return intensityRows(gapFillDailyRows(combined, canonicalShape)).slice(0, DAILY_HISTORY_CAP);
}

export interface TrendPresentation {
  rows: TrendRow[];
  title: string;
  chartLabel: string;
  valueLabel: string;
  source: DashboardSelection;
}

export function presentationTrend(env: Envelope | null, selection: DashboardSelection): TrendPresentation {
  if (selection === 'claude') {
    return { rows: env?.trend?.weeks ?? [], title: '$/1% Trend', chartLabel: '$/1% trend:', valueLabel: '$/1%', source: selection };
  }
  const rows = presentationPeriodRows(env, selection, 'weekly').slice().reverse();
  return {
    rows: rows.map((row, index) => ({
      label: row.label,
      used_pct: row.used_pct,
      dollar_per_pct: row.dollar_per_pct,
      delta: row.dollar_per_pct != null && rows[index - 1]?.dollar_per_pct != null
        ? row.dollar_per_pct - rows[index - 1].dollar_per_pct!
        : null,
      is_current: row.is_current,
      cost_usd: row.cost_usd,
    })),
    title: '$/1% Trend',
    chartLabel: '$/1% trend:',
    valueLabel: '$/1%',
    source: selection,
  };
}

export interface ForecastPresentation {
  projected: number | null;
  recent: number | null;
  primaryLabel: string;
  recentLabel: string;
  foot: Array<{ label: string; value: string }>;
  verdict: 'ok' | 'cap' | 'capped' | null;
}

export function presentationForecast(env: Envelope | null, selection: DashboardSelection): ForecastPresentation {
  if (selection === 'claude') {
    const fc = env?.forecast ?? null;
    return {
      projected: fc?.week_avg_projection_pct ?? null,
      recent: fc?.recent_24h_projection_pct ?? null,
      primaryLabel: 'Projected @ reset',
      recentLabel: 'Recent-24h',
      foot: [
        { label: 'Budget ≤100%', value: fc?.budget_100_per_day_usd == null ? '—' : `$${fc.budget_100_per_day_usd.toFixed(2)}/day` },
        { label: 'Budget ≤90%', value: fc?.budget_90_per_day_usd == null ? '—' : `$${fc.budget_90_per_day_usd.toFixed(2)}/day` },
      ],
      verdict: fc?.verdict ?? null,
    };
  }
  const codex = presentationProviders(env, selection).codex;
  const weekly = codex?.quota.histories.find((row) => row.window_minutes === 10_080)
    ?? codex?.quota.histories[0];
  const forecast = weekly?.forecast;
  const projected = forecast?.status === 'ok' ? forecast.projected_percent : null;
  const budget = codex?.budget.status;
  return {
    projected,
    recent: forecast?.current_percent ?? null,
    primaryLabel: 'Projected @ reset',
    recentLabel: weekly?.label ?? 'Current quota',
    foot: [
      { label: 'Confidence', value: forecast?.confidence ?? 'unavailable' },
      { label: 'Budget pace', value: budget?.pace.daily_usd == null ? '—' : `$${budget.pace.daily_usd.toFixed(2)}/day` },
    ],
    verdict: projected == null ? null : projected >= 100 ? 'capped' : projected >= 90 ? 'cap' : 'ok',
  };
}

export interface ProjectPresentationRow {
  key: string;
  source: SourceName;
  label: string;
  cost: number;
  pct: number | null;
  sessionsCount: number;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
}

export function presentationProjects(env: Envelope | null, selection: DashboardSelection): ProjectPresentationRow[] | null {
  const providers = presentationProviders(env, selection);
  const legacyClaudeRows = env?.projects?.current_week.rows ?? [];
  const sourceClaudeRows = selection === 'claude'
    ? legacyClaudeRows
    : providers.claude?.projects.current_week.rows ?? [];
  const claudeRows = sourceClaudeRows.map((row) => {
    // The provider bundle keeps an opaque qualified key for drill-down, while
    // the canonical Claude envelope owns the display label. Join the two
    // projections by their identical accounting tuple so All never exposes an
    // opaque project key as visible copy.
    const display = selection === 'claude' ? row : legacyClaudeRows.find(
      (candidate) => candidate.cost_usd === row.cost_usd
        && candidate.sessions_count === row.sessions_count,
    );
    return {
      key: row.key,
      source: 'claude' as const,
      label: display?.key ?? row.key,
      cost: row.cost_usd ?? 0,
      pct: row.attributed_pct ?? null,
      sessionsCount: row.sessions_count ?? 0,
      firstSeenAt: null,
      lastSeenAt: null,
    };
  });
  if (selection === 'claude') return env?.projects == null ? null : claudeRows;
  const codexRows = providers.codex?.projects.rows.map((row) => ({
    key: row.key,
    source: 'codex' as const,
    label: row.label,
    cost: row.cost_usd,
    pct: null,
    sessionsCount: row.session_count,
    firstSeenAt: row.first_seen,
    lastSeenAt: row.last_seen,
  })) ?? [];
  if (selection === 'codex' && providers.codex?.projects == null) return null;
  const rows = selection === 'all' ? [...claudeRows, ...codexRows] : codexRows;
  const total = rows.reduce((sum, row) => sum + row.cost, 0);
  return rows.sort((a, b) => b.cost - a.cost).map((row) => ({ ...row, pct: total > 0 ? row.cost / total * 100 : null }));
}

export interface BlockPresentationRow extends BlocksPanelRow {
  key: string;
  source: SourceName;
  value: number;
  valueLabel: string;
}

function codexBlock(row: CodexQuotaBlockRow): BlockPresentationRow {
  return {
    key: row.key,
    source: 'codex',
    start_at: row.start_at,
    end_at: row.end_at,
    anchor: 'recorded',
    is_active: row.is_active,
    cost_usd: row.cost_usd,
    models: codexModelRows(row.cost_usd, row.model_breakdowns),
    label: row.label,
    value: row.cost_usd,
    valueLabel: `$${row.cost_usd.toFixed(2)}`,
  };
}

export function presentationBlocks(env: Envelope | null, selection: DashboardSelection): BlockPresentationRow[] {
  const providers = presentationProviders(env, selection);
  const claudeRows = (selection === 'claude' ? env?.blocks?.rows : providers.claude?.quota.blocks) ?? [];
  const claude = claudeRows.map((row, index) => ({ ...row, key: 'key' in row && typeof row.key === 'string' ? row.key : `claude:${row.start_at}:${index}`, source: 'claude' as const, value: row.cost_usd, valueLabel: `$${row.cost_usd.toFixed(2)}` }));
  const codex = (providers.codex?.quota.blocks ?? [])
    .filter((row) => row.window_minutes === 300)
    .map(codexBlock);
  return selection === 'claude' ? claude : selection === 'codex' ? codex : [...claude, ...codex];
}

export function presentationCacheDays(env: Envelope | null, selection: DashboardSelection): CacheReportDailyRow[] | null {
  if (selection === 'claude') return env?.cache_report?.days ?? null;
  const native = presentationProviders(env, selection).codex?.cache_report;
  if (native != null) return native.days;
  const rows = presentationDailyRows(env, selection);
  return rows.map((row) => {
    // Codex input_tokens is cache-inclusive. codexDailyRow already normalizes
    // the provider-native ratio, so adding cached input to the denominator a
    // second time would force heavily cached days toward the implausible 50%
    // seen in the parity screenshots.
    const pct = row.cache_hit_pct ?? 0;
    return {
      date: row.date,
      cache_hit_percent: pct,
      input_tokens: row.input_tokens,
      output_tokens: row.output_tokens,
      cache_creation_tokens: row.cache_creation_tokens,
      cache_read_tokens: row.cache_read_tokens,
      saved_usd: 0,
      wasted_usd: 0,
      net_usd: 0,
      anomaly_triggered: false,
      anomaly_reasons: [],
    };
  });
}
