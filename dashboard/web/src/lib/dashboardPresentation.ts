import type {
  AllSourceData,
  BlocksPanelRow,
  CacheReportEnvelope,
  CacheReportDailyRow,
  CodexPeriodBucket,
  CodexQuotaBlockRow,
  CodexQuotaDomain,
  CodexSourceData,
  DailyPanelRow,
  DashboardSelection,
  Envelope,
  ModelCostRow,
  PeriodRow,
  SourceName,
  SourceEntry,
  SourceFreshnessDomain,
  SourceWarning,
  TrendRow,
} from '../types/envelope';
import { modelChipClass } from './model';
import { sourceDomainFreshness } from './sourceGating';

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

export type ProviderSectionStatus = 'available' | 'degraded' | 'empty' | 'unavailable';

export interface ProviderPresentationSection<T> {
  source: SourceName;
  label: 'Claude' | 'Codex';
  status: ProviderSectionStatus;
  reason: string | null;
  value: T | null;
}

export interface ProviderPresentationComposition<T> {
  selection: DashboardSelection;
  sections: ProviderPresentationSection<T>[];
}

function providerLabel(source: SourceName): 'Claude' | 'Codex' {
  return source === 'claude' ? 'Claude' : 'Codex';
}

function providerEntry(
  env: Envelope | null,
  source: SourceName,
): SourceEntry<unknown> | null {
  return env?.sources?.[source] ?? null;
}

function providerSection<T>(
  env: Envelope | null,
  source: SourceName,
  value: T | null,
  warningDomains: string[],
  freshnessDomains: SourceFreshnessDomain[],
  unavailableCopy: string,
): ProviderPresentationSection<T> {
  const entry = providerEntry(env, source);
  const relevantWarning = entry?.warnings.find((warning) =>
    warning.domain == null
      || warningDomains.includes(warning.domain)
      || warning.domain === 'ingest'
      || warning.domain === 'read_model',
  );
  const unsupportedDomain = warningDomains.find((domain) => {
    const status = entry?.capabilities[domain]?.status;
    return status === 'unavailable' || status === 'deferred';
  });
  const staleDomain = entry == null
    ? undefined
    : freshnessDomains.find(
      (domain) => sourceDomainFreshness(entry, domain) === 'stale',
    );

  if (value == null) {
    const status: ProviderSectionStatus = entry?.availability === 'empty'
      ? 'empty'
      : 'unavailable';
    return {
      source,
      label: providerLabel(source),
      status,
      reason: relevantWarning?.message
        ?? (unsupportedDomain ? entry?.capabilities[unsupportedDomain]?.semantics : null)
        ?? unavailableCopy,
      value: null,
    };
  }

  if (relevantWarning != null || staleDomain != null || unsupportedDomain != null) {
    return {
      source,
      label: providerLabel(source),
      status: 'degraded',
      reason: relevantWarning?.message
        ?? (staleDomain != null
          ? entry?.domain_freshness == null
            ? `${providerLabel(source)} data is stale.`
            : `${providerLabel(source)} ${staleDomain} data is stale.`
          : entry?.capabilities[unsupportedDomain!]?.semantics ?? unavailableCopy),
      value,
    };
  }

  return {
    source,
    label: providerLabel(source),
    status: 'available',
    reason: null,
    value,
  };
}

function compositionSources(selection: DashboardSelection): SourceName[] {
  return selection === 'all' ? ['claude', 'codex'] : [selection];
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
    source: 'codex',
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
    codex_tokens: {
      input_tokens: row.input_tokens,
      cached_input_tokens: row.cached_input_tokens,
      output_tokens: row.output_tokens,
      reasoning_output_tokens: row.reasoning_output_tokens,
      total_tokens: row.total_tokens,
    },
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
      merged.set(row.label, { ...row, source: 'all', models: [...row.models] });
      continue;
    }
    const cost = old.cost_usd + row.cost_usd;
    merged.set(row.label, {
      ...old,
      source: 'all',
      cost_usd: cost,
      total_tokens: old.total_tokens + row.total_tokens,
      input_tokens: old.input_tokens + row.input_tokens,
      output_tokens: old.output_tokens + row.output_tokens,
      cache_creation_tokens: old.cache_creation_tokens + row.cache_creation_tokens,
      cache_read_tokens: old.cache_read_tokens + row.cache_read_tokens,
      codex_tokens: undefined,
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
  const legacy = (selection === 'claude'
    ? env?.[period]?.rows ?? []
    : providers.claude?.periods?.[period]?.rows ?? [])
    .map((row) => ({ ...row, source: 'claude' as const }));
  const codex = [...(providers.codex?.periods?.[period]?.rows ?? [])]
    .sort((a, b) => b.label.localeCompare(a.label))
    .map(codexPeriodRow)
    .map((row, index, allRows) => ({
      ...row,
      delta_cost_pct: allRows[index + 1]?.cost_usd
        ? (row.cost_usd - allRows[index + 1].cost_usd) / allRows[index + 1].cost_usd
        : null,
    }));
  if (selection === 'all' && period === 'weekly') {
    const cap = PERIOD_HISTORY_CAP.weekly;
    // Independent reset axes do not share a join key. Keep each provider's
    // history intact and grouped; source-qualified keys carry identity through
    // selection and sorting even when visible labels collide.
    return [...legacy.slice(0, cap), ...codex.slice(0, cap)];
  }
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
    source: 'codex',
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
    codex_tokens: {
      input_tokens: row.input_tokens,
      cached_input_tokens: row.cached_input_tokens,
      output_tokens: row.output_tokens,
      reasoning_output_tokens: row.reasoning_output_tokens,
      total_tokens: row.total_tokens,
    },
  };
}

function emptyDailyRow(template: DailyPanelRow, source = template.source): DailyPanelRow {
  return {
    source,
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
    codex_tokens: source === 'codex' ? {
      input_tokens: 0, cached_input_tokens: 0, output_tokens: 0,
      reasoning_output_tokens: 0, total_tokens: 0,
    } : undefined,
  };
}

function gapFillDailyRows(
  rows: DailyPanelRow[],
  canonicalShape: DailyPanelRow[],
  emptySource?: DashboardSelection,
): DailyPanelRow[] {
  if (canonicalShape.length === 0) return rows;
  const byDate = new Map(rows.map((row) => [row.date, row]));
  const canonicalDates = new Set(canonicalShape.map((row) => row.date));
  const shaped = canonicalShape.map((template) => {
    const row = byDate.get(template.date);
    return row == null
      ? emptyDailyRow(template, emptySource)
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
    return intensityRows(gapFillDailyRows(codexRows, canonicalShape, 'codex')).slice(0, DAILY_HISTORY_CAP);
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
      source: 'all',
      cost_usd: cost,
      input_tokens: old.input_tokens + row.input_tokens,
      output_tokens: old.output_tokens + row.output_tokens,
      cache_creation_tokens: old.cache_creation_tokens + row.cache_creation_tokens,
      cache_read_tokens: old.cache_read_tokens + row.cache_read_tokens,
      total_tokens: old.total_tokens + row.total_tokens,
      cache_hit_pct: cacheEligibleInput > 0
        ? (old.cache_read_tokens + row.cache_read_tokens) / cacheEligibleInput * 100
        : null,
      codex_tokens: undefined,
      models: recomputeModelPct([...old.models, ...row.models], cost),
    });
  }
  const combined = [...merged.values()].sort((a, b) => b.date.localeCompare(a.date));
  return intensityRows(gapFillDailyRows(combined, canonicalShape)).slice(0, DAILY_HISTORY_CAP);
}

export interface TrendPresentation {
  rows: TrendRow[];
  sections: TrendProviderSection[];
  title: string;
  chartLabel: string;
  valueLabel: string;
  source: DashboardSelection;
}

export interface TrendProviderSection {
  source: SourceName;
  label: 'Claude' | 'Codex';
  rows: TrendRow[];
  historyRows: TrendRow[];
}

function periodRowsToTrend(rows: PeriodRow[], source: SourceName): TrendRow[] {
  const chronological = rows.slice().reverse();
  return chronological.map((row, index) => ({
    source,
    label: row.label,
    used_pct: row.used_pct,
    dollar_per_pct: row.dollar_per_pct,
    delta: row.dollar_per_pct != null && chronological[index - 1]?.dollar_per_pct != null
      ? row.dollar_per_pct - chronological[index - 1].dollar_per_pct!
      : null,
    is_current: row.is_current,
    cost_usd: row.cost_usd,
  }));
}

function trendSection(
  source: SourceName,
  rows: TrendRow[],
  historyRows: TrendRow[],
): TrendProviderSection {
  return {
    source,
    label: providerLabel(source),
    rows: rows.map((row) => ({ ...row, source })),
    historyRows: historyRows.map((row) => ({ ...row, source })),
  };
}

export function presentationTrend(env: Envelope | null, selection: DashboardSelection): TrendPresentation {
  if (selection === 'claude') {
    const rows = env?.trend?.weeks ?? [];
    const historyRows = env?.trend?.history ?? rows;
    return {
      rows,
      sections: [trendSection('claude', rows, historyRows)],
      title: '$/1% Trend', chartLabel: '$/1% trend:', valueLabel: '$/1%', source: selection,
    };
  }
  if (selection === 'all') {
    const claudeRows = env?.trend?.weeks
      ?? periodRowsToTrend(presentationPeriodRows(env, 'claude', 'weekly'), 'claude');
    const claudeHistory = env?.trend?.history ?? claudeRows;
    const codexRows = periodRowsToTrend(
      presentationPeriodRows(env, 'codex', 'weekly'),
      'codex',
    );
    return {
      rows: [],
      sections: [
        trendSection('claude', claudeRows, claudeHistory),
        trendSection('codex', codexRows, codexRows),
      ],
      title: '$/1% Trend', chartLabel: '$/1% trend:', valueLabel: '$/1%', source: selection,
    };
  }
  const rows = periodRowsToTrend(
    presentationPeriodRows(env, 'codex', 'weekly'),
    'codex',
  );
  return {
    rows,
    sections: [trendSection('codex', rows, rows)],
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

// #416 QA P1 — the ONE liveness question every Codex forecast read must ask
// before it publishes a "current quota".
//
// A retained history row keeps a window's last observed percentage and its
// forecast as EVIDENCE, long after the window itself has reset — the reported
// case had a weekly window captured 2026-07-13 whose `resets_at` was already
// 2026-07-19, nine days dead by the snapshot's clock, still carrying
// `current_percent: 41` and `confidence: 'medium'`.
//
// The server already answers the question and publishes the answer:
// `_quota_read_model` appends a `quota.summary.active[]` row, keyed identically
// to the history row, only when `baseline.resets_at > now`. That set is exactly
// this predicate, per WINDOW. Reading it keeps the decision in the one place
// that owns it — build time — instead of re-deriving cycle validity on the
// client from a view #350 documented as lossy for precisely that purpose.
//
// PASS THE RIGHT SUBTREE (#416 QA P2-1). The active row's key is
// `dashboard_resource_key("quota", "codex", source_root_key, logical_limit_key,
// observed_slot, window_minutes)` and carries NO account — nor does
// `logical_limit_key`, whose `limitId` is one literal for the whole provider. So
// two accounts sharing one `$CODEX_HOME` root — the shape
// `adopt_unidentified_observations` and `_codex_account_scopes_wire` are both
// written for — produce two history rows under ONE key, and the merged parent
// lists that key once, contributed by whichever account is live. A liveness
// lookup for ONE account must therefore come from THAT account's
// `account_scopes` child, whose `quota` is built by `_quota_read_model` over
// that account's own observations. The parent's set answers only the
// provider-wide question "is any window with this key live", which is not the
// question a per-account row is asking.
//
// NOT `forecast.status === 'ok'`: that is derived from freshness and sample
// count alone (`forecast_quota`), so it says nothing about whether the window is
// still running, and gating on it would blank a LIVE window whose forecast is
// merely stale or low-confidence — a worse defect than the one this fixes.
//
// NOT `accounts[].weeklyPercent`: that is the per-ACCOUNT cycle decision, and it
// is additionally forced to null for the unattributed bucket (dimmed, totals
// only) whose window may still be live.
export function codexLiveQuotaKeys(
  quota: CodexQuotaDomain | null | undefined,
): Set<string> {
  return new Set((quota?.summary.active ?? []).map((row) => row.key));
}

export function presentationForecast(env: Envelope | null, selection: SourceName): ForecastPresentation {
  if (selection === 'claude') {
    const fc = env?.forecast ?? env?.sources?.claude?.data?.hero.forecast ?? null;
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
  // #373: exclude windows outside account-level standard quota from BOTH the
  // primary lookup and the fallback — a separate model pool must never become
  // the account's forecast.
  const accountHistories = (codex?.quota.histories ?? [])
    .filter((row) => !row.model_scoped);
  const weekly = accountHistories.find((row) => row.window_minutes === 10_080)
    ?? accountHistories[0];
  const forecast = weekly?.forecast;
  // #416 QA P1: an already-reset window is not a current quota. `status` is a
  // forecast-QUALITY axis, so it gates the projection independently.
  //
  // `codex` here is ALREADY the right subtree for the shared-root collision:
  // under focus `composeScopedData` replaces `quota` wholesale with the
  // account's child, so the histories and the active set come from the SAME
  // child; unfocused-and-decorated, this branch is not the surface that
  // publishes a per-account number (`presentationCodexAccountForecasts` is);
  // undecorated, the parent has exactly one account.
  const live = weekly != null && codexLiveQuotaKeys(codex?.quota).has(weekly.key);
  const projected = live && forecast?.status === 'ok' ? forecast.projected_percent : null;
  const budget = codex?.budget.status;
  return {
    projected,
    recent: live ? forecast?.current_percent ?? null : null,
    primaryLabel: 'Projected @ reset',
    recentLabel: weekly?.label ?? 'Current quota',
    foot: [
      { label: 'Confidence', value: (live && forecast?.confidence) || 'unavailable' },
      { label: 'Budget pace', value: budget?.pace.daily_usd == null ? '—' : `$${budget.pace.daily_usd.toFixed(2)}/day` },
    ],
    verdict: projected == null ? null : projected >= 100 ? 'capped' : projected >= 90 ? 'cap' : 'ok',
  };
}

// #416 QA P0 — the per-account Codex forecast disclosure.
//
// `presentationForecast` resolves ONE weekly window, and under decoration
// `quota.histories` carries one weekly row PER ACCOUNT. Every "All accounts"
// forecast surface therefore published whichever account sorted first as the
// provider's forecast, unlabelled, while a sibling could be sixty points away
// and carry the opposite verdict.
//
// D6 forbids blending independent allowances and no summary statistic over them
// (a max, a mean, "the most urgent") is the quantity a forecast slot claims to
// hold — so the surfaces blank. Where a disclosure beats a blank, this selector
// supplies it: one row per `accounts[]` card, each carrying that account's OWN
// server-emitted projection. Nothing is summed, averaged or re-derived here,
// exactly as `CodexPerAccountCycleTable` and the per-account hero strip
// established.
//
// Returns `null` when the provider is UNDECORATED (`accounts[]` absent), which
// is the byte-stable single-account shape — callers branch on presence.
export interface CodexAccountForecastRow {
  accountKey: string;
  label: string;
  unattributed: boolean;
  projected: number | null;
  current: number | null;
  confidence: string | null;
  status: string | null;
  verdict: 'ok' | 'cap' | 'capped' | null;
}

export function presentationCodexAccountForecasts(
  env: Envelope | null,
): CodexAccountForecastRow[] | null {
  const codex = (env?.sources?.codex?.data ?? null) as CodexSourceData | null;
  const cards = codex?.accounts;
  if (cards == null || cards.length === 0) return null;
  // #373: a window outside account-level standard quota is never the account's
  // forecast — the same exclusion every other forecast read applies.
  const histories = (codex?.quota.histories ?? []).filter((row) => !row.model_scoped);
  // #416 QA P1: the same server-side liveness decision the focused panel reads.
  // Without it this table published a nine-day-dead window's `41.0% / medium`
  // under CURRENT QUOTA while the sibling per-account Current Cycle table, the
  // account card, the hero and the alerts gauge all rendered `—` for the same
  // account, from the same envelope.
  //
  // #416 QA P2-1: taken from the CARD'S OWN child, never the merged parent. The
  // window key excludes the account, so two accounts under one `$CODEX_HOME`
  // root collide on it and the parent's set would let the live sibling revive
  // the dead account — the original defect, unfixed, on the exact shape
  // `_codex_account_scopes_wire` exists to serve. A missing child degrades to
  // the empty set (blank), matching `accountScope.ts`'s law that a focused read
  // NEVER falls back to the parent; `accounts[]` and `account_scopes` ship
  // together under one `_codex_decorated` gate, and every card key is passed to
  // `_codex_account_scopes_wire`, so a missing child is drift, not a shape.
  const scopes = codex?.account_scopes;
  return cards.map((card) => {
    const own = histories.filter((row) => row.account_key === card.accountKey);
    const weekly = own.find((row) => row.window_minutes === 10_080) ?? own[0];
    const forecast = weekly?.forecast;
    const live = weekly != null
      && codexLiveQuotaKeys(scopes?.[card.accountKey]?.quota).has(weekly.key);
    const projected = live && forecast?.status === 'ok' ? forecast.projected_percent : null;
    return {
      accountKey: card.accountKey,
      label: card.label,
      unattributed: card.unattributed === true,
      projected,
      current: live ? forecast?.current_percent ?? weekly?.current_percent ?? null : null,
      confidence: live ? forecast?.confidence ?? null : null,
      // #416 QA P3-1: `status` is a forecast-QUALITY axis, so on a dead window
      // it describes a projection for a cycle that no longer exists. Nothing
      // renders it today, but it is on the exported row contract.
      status: live ? forecast?.status ?? null : null,
      verdict: projected == null
        ? null
        : projected >= 100 ? 'capped' : projected >= 90 ? 'cap' : 'ok',
    };
  });
}

export function presentationForecastComposition(
  env: Envelope | null,
  selection: DashboardSelection,
): ProviderPresentationComposition<ForecastPresentation> {
  return {
    selection,
    sections: compositionSources(selection).map((source) => {
      const value = presentationForecast(env, source);
      const codex = source === 'codex' ? presentationProviders(env, source).codex : null;
      // #373: the same account-level exclusion `presentationForecast` applies,
      // over the SAME three reads — the primary lookup, the `[0]` fallback and
      // the capability probe. A foreign pool's stale forecast must not degrade
      // the account's section, and its presence must not stand in for the
      // account having a forecast at all.
      const accountHistories = (codex?.quota.histories ?? [])
        .filter((row) => !row.model_scoped);
      const nativeForecast = accountHistories.find(
        (row) => row.window_minutes === 10_080,
      )?.forecast ?? accountHistories[0]?.forecast;
      const hasForecast = source === 'claude'
        ? (env?.forecast ?? env?.sources?.claude?.data?.hero.forecast) != null
        : accountHistories.some((row) => row.forecast != null);
      const section = providerSection(
        env,
        source,
        hasForecast ? value : null,
        source === 'claude' ? ['hero', 'quota', 'budget'] : ['quota', 'budget'],
        source === 'claude' ? ['hero', 'quota'] : ['quota'],
        `${providerLabel(source)} forecast is unavailable.`,
      );
      if (source === 'codex' && section.value != null && nativeForecast?.status !== 'ok') {
        const statusCopy = nativeForecast?.status === 'stale'
          ? 'Codex forecast is stale.'
          : nativeForecast?.status === 'insufficient-history'
            ? 'Codex forecast needs more history.'
            : 'Codex forecast is unavailable.';
        return { ...section, status: 'degraded' as const, reason: statusCopy };
      }
      return section;
    }),
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
  // Present only on a decorated Codex row (#416); `null` everywhere else.
  accountKey?: string | null;
}

function codexBlock(row: CodexQuotaBlockRow): BlockPresentationRow {
  return {
    key: row.key,
    source: 'codex',
    // #416 QA P1-A — the merged list is now the union of every account's
    // windows, so a row must be able to name its owner. Omitted below two real
    // accounts (the server does not stamp it), which is what keeps the
    // single-account panel unlabelled.
    accountKey: row.account_key ?? null,
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

export function presentationCacheReportComposition(
  env: Envelope | null,
  selection: DashboardSelection,
): ProviderPresentationComposition<CacheReportEnvelope> {
  return {
    selection,
    sections: compositionSources(selection).map((source) => {
      const value = source === 'claude'
        ? env?.cache_report ?? null
        : presentationProviders(env, source).codex?.cache_report ?? null;
      const section = providerSection(
        env,
        source,
        value,
        ['forensics'],
        ['sessions'],
        `${providerLabel(source)} cache report is unavailable.`,
      );
      if (section.value?.is_empty && section.status === 'available') {
        return {
          ...section,
          status: 'empty' as const,
          reason: `No ${providerLabel(source)} cache activity is available for this window.`,
        };
      }
      return section;
    }),
  };
}
