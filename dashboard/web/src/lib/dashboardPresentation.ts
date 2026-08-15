import type {
  AggregateOutcome,
  BudgetPresentation,
  AggregateQualification,
  AggregateRange,
  AllSourceData,
  BlocksPanelRow,
  CacheReportEnvelope,
  ClaudeBudgetDomain,
  CodexBudgetDomain,
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

// #556 S2 §3.7 — the discriminated aggregate result.
//
// An adapter that can only return rows has no way to say "this could not be
// computed", so a range problem reached the user as an empty table ("No usage
// history yet") or as a broken instance ("restart the dashboard"). Neither is
// true, and during a v5-server/v6-client overlap both are EXPECTED, because
// the in-place update path deliberately leaves an old client talking to a
// restarted server without reloading the JavaScript.
//
// `unavailable` is today's single-provider null-envelope state, kept exactly
// as it is so the Claude and Codex tabs are untouched. `withheld` is the new
// All-only state.
export interface AggregateAvailable<T> {
  state: 'available';
  rows: T;
  // Null on the two single-provider tabs, which are not ranked over a shared
  // range at all, and on an All payload whose range did not resolve.
  range: AggregateRange | null;
  qualifications: AggregateQualification[];
}

export interface AggregateWithheld {
  state: 'withheld';
  // NOT a closed union on the client. The rendering switch needs a required
  // fallback branch so an unknown code from a newer server renders generic
  // copy instead of nothing.
  code: string;
  provider?: SourceName;
}

export type ProjectsPresentation =
  | AggregateAvailable<ProjectPresentationRow[]>
  | AggregateWithheld
  | { state: 'unavailable' };

export type DailyPresentation =
  | AggregateAvailable<DailyPanelRow[]>
  | AggregateWithheld;

const ROWS_ABSENT: AggregateWithheld = { state: 'withheld', code: 'rows_absent' };

function withheldFrom(outcome: AggregateOutcome & { state: 'withheld' }): AggregateWithheld {
  // `provider` is omitted rather than set to undefined: a provider-scoped code
  // names its provider and a non-provider code carries no such key at all, and
  // the copy layer branches on presence.
  return outcome.provider == null
    ? { state: 'withheld', code: outcome.code }
    : { state: 'withheld', code: outcome.code, provider: outcome.provider };
}

/**
 * Resolve one aggregate's outcome, ahead of composing its rows.
 *
 * Returns the withheld result to publish, or `null` when the caller should go
 * on to compose rows. Both `rows_absent` synthesis rules live here: a missing
 * `aggregates` object (a v5 server, which cannot emit a code for a field it
 * does not know about) and a malformed outcome slot.
 */
function aggregateOutcomeFor(
  env: Envelope | null,
  domain: 'projects' | 'daily',
): { withheld: AggregateWithheld } | { available: AggregateAvailable<never>['qualifications'] } {
  const aggregates = env?.sources?.all?.data?.aggregates;
  const outcome = aggregates?.[domain];
  if (aggregates == null || outcome == null || typeof outcome.state !== 'string') {
    return { withheld: ROWS_ABSENT };
  }
  if (outcome.state === 'withheld') return { withheld: withheldFrom(outcome) };
  return { available: outcome.qualifications ?? [] };
}

function aggregateRange(env: Envelope | null): AggregateRange | null {
  return env?.sources?.all?.data?.aggregates?.range ?? null;
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

function codexPeriodRow(
  row: CodexPeriodBucket,
  index: number,
  accountLabels: ReadonlyMap<string, string>,
): PeriodRow {
  const breakdownModels = codexModelRows(row.cost_usd, row.model_breakdowns, true);
  const models = breakdownModels.length > 0
    ? breakdownModels
    : sourceModels(row.cost_usd, 'codex');
  const ownerLabels = [...new Set(
    (row.account_keys ?? []).flatMap((key) => {
      const label = accountLabels.get(key);
      return label == null ? [] : [label];
    }),
  )];
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
    ...(ownerLabels.length > 0 ? { account_labels: ownerLabels } : {}),
  };
}


export function presentationPeriodRows(
  env: Envelope | null,
  selection: DashboardSelection,
  period: 'weekly' | 'monthly',
): PeriodRow[] {
  const providers = presentationProviders(env, selection);
  const codexAccountLabels = new Map(
    (providers.codex?.accounts ?? []).map((card) => [card.accountKey, card.label]),
  );
  const legacy = (selection === 'claude'
    ? env?.[period]?.rows ?? []
    : providers.claude?.periods?.[period]?.rows ?? [])
    .map((row) => ({ ...row, source: 'claude' as const }));
  const codex = [...(providers.codex?.periods?.[period]?.rows ?? [])]
    .sort((a, b) => b.label.localeCompare(a.label))
    .map((row, index) => codexPeriodRow(row, index, codexAccountLabels))
    .map((row, index, allRows) => ({
      ...row,
      delta_cost_pct: allRows[index + 1]?.cost_usd
        ? (row.cost_usd - allRows[index + 1].cost_usd) / allRows[index + 1].cost_usd
        : null,
    }));
  if (selection === 'all') {
    // #556 S2 §5.1 — MONTHLY IS UNMERGED, so both period kinds take this
    // branch. Independent reset axes do not share a join key. Keep each
    // provider's history intact and grouped; source-qualified keys carry
    // identity through selection and sorting even when visible labels collide.
    //
    // The deleted `mergePeriodRows` was the only production caller of a
    // function with two behaviours that made the merge unsafe: it nulled
    // `used_pct` and `dollar_per_pct` only when two rows COLLIDED on a label,
    // so the same row kept or lost its quota figures depending on whether the
    // other provider happened to have that month; and it recomputed one model
    // split across two providers' families. #294 S5 §6.2 says buckets are
    // never merged, and #312 §7.4 supersedes that for DAILY only.
    const cap = PERIOD_HISTORY_CAP[period];
    return [...legacy.slice(0, cap), ...codex.slice(0, cap)];
  }
  const rows = selection === 'codex' ? codex : legacy;
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

export function presentationDailyRows(
  env: Envelope | null,
  selection: DashboardSelection,
): DailyPresentation {
  const providers = presentationProviders(env, selection);
  // The `?? []` stays for the two single-provider tabs, whose output is
  // byte-frozen. The All branch below requires the sibling to be PRESENT.
  const codexRows = [...(providers.codex?.periods?.daily?.rows ?? [])]
    .sort((a, b) => b.label.localeCompare(a.label))
    .map(codexDailyRow);
  if (selection === 'claude') {
    return {
      state: 'available',
      range: null,
      qualifications: [],
      rows: (env?.daily?.rows ?? []).slice(0, DAILY_HISTORY_CAP),
    };
  }
  if (selection === 'codex') {
    // The Codex tab keeps reading the legacy top-level panel for its canonical
    // shape (§6.3a leaves this branch alone); only the All path stops.
    return {
      state: 'available',
      range: null,
      qualifications: [],
      rows: intensityRows(
        gapFillDailyRows(codexRows, env?.daily?.rows ?? [], 'codex'),
      ).slice(0, DAILY_HISTORY_CAP),
    };
  }

  const outcome = aggregateOutcomeFor(env, 'daily');
  if ('withheld' in outcome) return outcome.withheld;

  // §6.3a — under All the Daily outcome owns its COMPLETE canonical row shape
  // and the presentation layer must not consult `env.daily.rows`. A display-day
  // rollover forces a full rebuild; if the source build then fails, the prior
  // bundle is retained while a freshly built legacy panel sits in the new
  // snapshot, and reshaping the retained rows against it would put days on
  // screen that the published range does not cover.
  // BOTH siblings are required, symmetrically with `presentationProjects`.
  // §3.5.1 names the Codex daily rows a required sibling of this aggregate,
  // and checking only Claude let an `available` outcome with a missing Codex
  // data object publish a Claude-only series under the shared-range label with
  // nothing saying Codex was absent — malformed data reported as honest
  // emptiness, which is the exact failure §3.7 exists to prevent. An EMPTY
  // Codex row list stays a zero leg; only a MISSING sibling withholds.
  const claudeRows = providers.claude?.periods.daily_aggregate?.rows;
  const codexDaily = providers.codex?.periods?.daily?.rows;
  if (claudeRows == null || codexDaily == null) return ROWS_ABSENT;
  const canonicalShape = claudeRows;
  const merged = new Map<string, DailyPanelRow>();
  for (const row of [...claudeRows, ...codexRows]) {
    const old = merged.get(row.date);
    if (!old) {
      merged.set(row.date, { ...row, models: [...row.models] });
      continue;
    }
    const cost = old.cost_usd + row.cost_usd;
    merged.set(row.date, {
      ...old,
      source: 'all',
      cost_usd: cost,
      input_tokens: old.input_tokens + row.input_tokens,
      output_tokens: old.output_tokens + row.output_tokens,
      cache_creation_tokens: old.cache_creation_tokens + row.cache_creation_tokens,
      cache_read_tokens: old.cache_read_tokens + row.cache_read_tokens,
      total_tokens: old.total_tokens + row.total_tokens,
      // §6.1 — WITHHELD, not computed. Claude's ratio is cache-read over input
      // plus cache creation and read; Codex's input is ALREADY cache-inclusive.
      // No ratio over their sum describes anything, and the compensation this
      // replaced ("combine the two native denominators") was arithmetic over a
      // quantity that does not exist. `PeriodDetailCard` already null-gates the
      // block, so it simply does not render — which also removes a live wrong
      // call, `cacheVocabulary(row.source ?? 'claude')` selecting Claude's
      // vocabulary for an `all` row.
      cache_hit_pct: null,
      codex_tokens: undefined,
      // §6.2 — no merged chip set. The deleted `recomputeModelPct` folded two
      // providers' model families into one stack sharing one denominator,
      // which is not a model split of anything. The drill-down renders chips
      // per provider leg instead (`presentationDailyLegs`). Deleting the
      // helper with its last caller is deliberate: `mergePeriodRows` was the
      // other one, and it went in §5.1.
      models: [],
    });
  }
  const combined = [...merged.values()].sort((a, b) => b.date.localeCompare(a.date));
  return {
    state: 'available',
    range: aggregateRange(env),
    qualifications: outcome.available,
    rows: intensityRows(
      gapFillDailyRows(combined, canonicalShape),
    ).slice(0, DAILY_HISTORY_CAP),
  };
}

// #556 S2 §6.3 — the Daily drill-down breakdown.
//
// #312 §7.4 authorises All to aggregate compatible daily USD cost *provided
// the drill-down preserves the provider breakdown*. This is that breakdown,
// built client-side from rows already in the snapshot: no new request, no new
// wire data. It also discharges D11 for Daily — the merged row carries no
// per-provider attribution, so the detail has to.
//
// Both legs come from the SAME collections the merged row was built from: the
// Claude bounded sibling (§6.3a) and the Codex native daily rows. A provider
// with no activity on that date contributes `null` rather than a zero row, so
// the detail can say "Codex had no activity" instead of showing a $0.00 leg
// that looks like a measurement.
export interface DailyProviderLegs {
  claude: DailyPanelRow | null;
  codex: DailyPanelRow | null;
}

export function presentationDailyLegs(
  env: Envelope | null,
  date: string | null | undefined,
): DailyProviderLegs {
  if (date == null || date === '') return { claude: null, codex: null };
  const providers = presentationProviders(env, 'all');
  const claude = (providers.claude?.periods.daily_aggregate?.rows ?? [])
    .find((row) => row.date === date) ?? null;
  const codex = (providers.codex?.periods.daily.rows ?? [])
    .map(codexDailyRow)
    .find((row) => row.date === date) ?? null;
  return {
    claude: claude == null ? null : { ...claude, source: 'claude' },
    codex,
  };
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
    ...(row.account_labels ? { account_labels: row.account_labels } : {}),
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
// to the history row, only when `_active_row_from_history` says the window is
// live (#429). That set is exactly
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
// lists that key TWICE when both are live, once per account, with independent
// evidence (#429 — the decorated fixture disproves the "listed once" reading
// this comment used to carry; the scoped identity is `(account_key, key)`).
// A liveness lookup for ONE account must therefore come from THAT account's
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
  return {
    projected,
    recent: live ? forecast?.current_percent ?? null : null,
    primaryLabel: 'Projected @ reset',
    recentLabel: weekly?.label ?? 'Current quota',
    // #556 S5 §4.7 — `Budget pace` LEFT this footer for the budget block, which
    // is where the configured-budget quantities now live, and is rendered
    // exactly once. `Confidence` stays: it qualifies the PROJECTION, under its
    // existing liveness and decorated-account suppression rules.
    foot: [
      { label: 'Confidence', value: (live && forecast?.confidence) || 'unavailable' },
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

// #556 S5 §4.1 — the configured-BUDGET presentation, beside the forecast one.
//
// Two verdicts must never be confused (§4.5): the forecast surface already
// carries an `ok`/`cap`/`capped` chip for the PROJECTION, while a budget
// verdict is `ok`/`warn`/`over` over configured spend. They are different
// quantities, so this adapter is a separate symbol and touches none of S2's or
// S4's forecast adapters.
//
// The dispositions the server can express (§3.5) are mutually exclusive and all
// OPTIONAL, so the ordinary no-budget payload carries none of them. One
// documented asymmetry is absorbed here: Claude OMITS `status` when there is
// nothing to publish, while Codex's enclosing domain always emits
// `"status": <object|null>`. Both map to the same presentation state.
//
// The union arms are closed for SERVER GENERATION AND TESTS ONLY: `disposition`
// and `reason` are bare strings, and every rendering caller must carry a
// fallback branch for a code this client has never seen. That is S2's rule.
function budgetPresentationOf(
  domain: ClaudeBudgetDomain | CodexBudgetDomain | null | undefined,
): BudgetPresentation {
  if (domain == null) {
    return { state: 'not_configured', disposition: 'provider_budget_unset' };
  }
  const status = domain.status;
  if (status != null) return { state: 'configured', status };
  const unavailable = domain.status_unavailable;
  if (unavailable != null) {
    return { state: 'unavailable', reason: unavailable.code, unavailable };
  }
  const notConfigured = domain.not_configured;
  if (notConfigured != null) {
    return { state: 'not_configured', disposition: notConfigured.disposition };
  }
  return { state: 'not_configured', disposition: 'provider_budget_unset' };
}

export function presentationBudgetComposition(
  env: Envelope | null,
  selection: DashboardSelection,
): ProviderPresentationComposition<BudgetPresentation> {
  const providers = presentationProviders(env, selection);
  return {
    selection,
    sections: compositionSources(selection).map((source) => {
      // Under a Codex account focus the envelope handed in is already SCOPED,
      // so `providers.codex` is the focused child and its `budget` is
      // `account_scopes[key].budget` — never the parent vendor-wide status
      // (§4.8). Nothing here falls back to the parent, for the same reason
      // `accountScope.ts` never does: the parent under focus would attribute
      // every account's spend to one.
      const data = source === 'claude' ? providers.claude : providers.codex;
      return providerSection(
        env,
        source,
        data == null ? null : budgetPresentationOf(data.budget),
        ['budget'],
        [],
        `${providerLabel(source)} budget is unavailable.`,
      );
    }),
  };
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
  // #556 S2 §3.8a — whether the drill-down route resolves this row. A false
  // row keeps its rank, its label and its cost and renders with NO drill
  // affordance, rather than offering an interaction that 404s.
  drillable: boolean;
}

function codexProjectRows(
  providers: PresentationProviders,
): ProjectPresentationRow[] {
  return providers.codex?.projects.rows.map((row) => ({
    key: row.key,
    source: 'codex' as const,
    label: row.label,
    cost: row.cost_usd,
    pct: null,
    sessionsCount: row.session_count,
    firstSeenAt: row.first_seen,
    lastSeenAt: row.last_seen,
    // A Codex project key is a native identity its own detail route resolves;
    // nothing narrows the population the way the Claude legacy envelope does.
    drillable: true,
  })) ?? [];
}

function rankByCostShare(rows: ProjectPresentationRow[]): ProjectPresentationRow[] {
  // §4.2 — the percentage is a SHARE OF COST, not a share of quota, and the
  // panel legend names it as one. This applies to Codex as well as All: the
  // recomputation was never All-specific.
  const total = rows.reduce((sum, row) => sum + row.cost, 0);
  return rows
    .slice()
    .sort((a, b) => b.cost - a.cost)
    .map((row) => ({ ...row, pct: total > 0 ? row.cost / total * 100 : null }));
}

export function presentationProjects(
  env: Envelope | null,
  selection: DashboardSelection,
): ProjectsPresentation {
  const providers = presentationProviders(env, selection);

  if (selection === 'claude') {
    if (env?.projects == null) return { state: 'unavailable' };
    // The Claude tab is untouched: legacy rows, legacy display keys, and
    // `attributed_pct` as a share of the week's quota.
    return {
      state: 'available',
      range: null,
      qualifications: [],
      rows: (env.projects.current_week.rows ?? []).map((row) => ({
        key: row.key,
        source: 'claude' as const,
        label: row.key,
        cost: row.cost_usd ?? 0,
        pct: row.attributed_pct ?? null,
        sessionsCount: row.sessions_count ?? 0,
        firstSeenAt: null,
        lastSeenAt: null,
        drillable: true,
      })),
    };
  }

  if (selection === 'codex') {
    if (providers.codex?.projects == null) return { state: 'unavailable' };
    return {
      state: 'available',
      // The Codex ranking is bounded by the SAME shared range the aggregate
      // publishes: §3.2 resolves one `shared_start` for the tick and sets the
      // exclusive upper bound to `now_utc + 1µs` specifically because that is
      // "what the Codex projects read already uses". So the tab can name its
      // period instead of stating none, which is what it did once the header's
      // `(N this week)` — never true for Codex — was removed.
      range: aggregateRange(env),
      qualifications: [],
      rows: rankByCostShare(codexProjectRows(providers)),
    };
  }

  const outcome = aggregateOutcomeFor(env, 'projects');
  if ('withheld' in outcome) return outcome.withheld;

  // §4.1 + §4.3 — the ranking reads the BOUNDED sibling, both providers folded
  // over the same shared absolute range, and reads the published `label`
  // directly. The deleted alternative joined the opaque provider rows to the
  // legacy envelope by their `(cost_usd, sessions_count)` tuple and fell back
  // to printing the opaque key whenever that search missed.
  const claudeAggregate = providers.claude?.projects.aggregate?.rows;
  const codexDomain = providers.codex?.projects;
  if (claudeAggregate == null || codexDomain == null) return ROWS_ABSENT;

  const claudeRows: ProjectPresentationRow[] = claudeAggregate.map((row) => ({
    key: row.key,
    source: 'claude' as const,
    label: row.label,
    cost: row.cost_usd,
    pct: null,
    sessionsCount: row.sessions_count,
    firstSeenAt: null,
    lastSeenAt: null,
    drillable: row.drillable === true,
  }));

  return {
    state: 'available',
    range: aggregateRange(env),
    qualifications: outcome.available,
    rows: rankByCostShare([...claudeRows, ...codexProjectRows(providers)]),
  };
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
  if (selection === 'claude') return claude;
  if (selection === 'codex') return codex;
  // #556 S2 §6.4 — one time-ordered list, not one provider's list stacked on
  // the other's. `[...claude, ...codex]` put every Claude window above every
  // Codex window regardless of when either happened, so a block from this
  // morning could sit below one from last week. Both providers carry required
  // bounds, so a real ordering exists.
  //
  // A STABLE merge: equal instants tie-break on source then opaque key, so the
  // rendered order cannot flip between two snapshots that carry the same rows.
  return [...claude, ...codex].sort((a, b) => {
    const left = Date.parse(a.start_at);
    const right = Date.parse(b.start_at);
    const byTime = (Number.isNaN(right) ? 0 : right) - (Number.isNaN(left) ? 0 : left);
    if (byTime !== 0) return byTime;
    if (a.source !== b.source) return a.source < b.source ? -1 : 1;
    return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
  });
}

// #556 S2 §6.4 — what the combined blocks footer may say.
//
// The footer keeps its sum: the blocks contract requires it to equal the sum of
// the DISPLAYED rows. Beside it go per-provider counts and costs, and coverage
// stated as the interval the displayed rows actually span. It must never claim
// continuous coverage or imply a shared reset cycle — the two providers run
// independent five-hour clocks, which is why the panel's sub-label reads
// "current provider cycles" in the plural.
export interface BlocksFooterLeg {
  source: SourceName;
  label: 'Claude' | 'Codex';
  count: number;
  cost: number;
}

export function blocksFooterLegs(rows: BlockPresentationRow[]): BlocksFooterLeg[] {
  return (['claude', 'codex'] as const).map((source) => {
    const own = rows.filter((row) => row.source === source);
    return {
      source,
      label: providerLabel(source),
      count: own.length,
      cost: own.reduce((sum, row) => sum + row.value, 0),
    };
  });
}

/** The interval the displayed rows span: earliest start to latest end. */
export function blocksDisplayedSpan(
  rows: BlockPresentationRow[],
): { startAt: string; endAt: string } | null {
  let startAt: string | null = null;
  let endAt: string | null = null;
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const row of rows) {
    const from = Date.parse(row.start_at);
    const to = Date.parse(row.end_at);
    if (!Number.isNaN(from) && from < start) { start = from; startAt = row.start_at; }
    if (!Number.isNaN(to) && to > end) { end = to; endAt = row.end_at; }
  }
  return startAt == null || endAt == null ? null : { startAt, endAt };
}

// #443 S2 — `presentationCacheDays` is DELETED. Its only two production
// consumers were the panel's `adapted` and the modal's `sourceCr`, the
// fabricating fallbacks that session removed: it synthesized a daily series
// with hard-zero saved/wasted/net dollars, which those fallbacks then dressed
// up as a report. Nothing derives a cache report from raw daily rows any more.

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
          // #443 F7 — the populated branch rendered KPIs and a healthy
          // verdict alongside the empty chip. Dropping the value makes that
          // state unrepresentable rather than merely discouraged; both
          // consumers already have a `report == null` branch. Deliberately
          // NOT applied to `degraded`, which keeps its value.
          value: null,
          reason: `No ${providerLabel(source)} cache activity is available for this window.`,
        };
      }
      return section;
    }),
  };
}
