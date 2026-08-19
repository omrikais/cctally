import type {
  AlertAxis,
  AlertEntry,
  DashboardSelection,
  Envelope,
  SourceName,
} from '../types/envelope';

// #620 S1 D11/D12 — the client mirror of `bin/_lib_alert_scope.py`.
//
// Two rules carry over from the Python kernel unchanged, and this file exists
// so that they hold on the dashboard as well as on the CLI.
//
// **The window END is derived, never stored.** Those rows live in stats.db,
// which returns early at `STATS_INDEX_EPOCH` before `add_column_if_missing` is
// reached, so a `window_end_at` column would cost an epoch bump plus journal,
// replay, cutover, rebuild and rederive parity — disproportionate to a value
// the retained fields already determine.
//
// **Scope is never read from the alert id.** `envelope.ts:288` declares the id
// opaque and never parsed, the journal contract independently forbids parsing
// event ids, and Claude alert ids omit the account entirely, so the id could
// not carry the scope even if parsing it were permitted. Nothing below reads
// `entry.id`.
//
// Returning a bare `null` for "no window" was rejected for the same reason it
// was rejected in Python: it cannot carry the cause, and a caller cannot
// distinguish "no window" from "window withheld because the account is
// vendor-wide". `AlertScope` follows the #556 typed-withholding shape — it
// always returns, and it always says which state it is in.

export const VENDOR_WIDE_KEY = '*';

const SUBSCRIPTION_WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const CALENDAR_WEEK_MS = 7 * 24 * 60 * 60 * 1000;
const FIVE_HOUR_MS = 5 * 60 * 60 * 1000;

export type AlertAccountScope = 'account' | 'vendor_wide';

/** How precisely the retained fields fix the bounds. `day` means the row kept
 *  only a calendar date, so a surface must not state a time or a zone it does
 *  not have. Meaningful only when `available`. */
export type AlertWindowGranularity = 'instant' | 'day';

export interface AlertScope {
  available: boolean;
  /** A sentence a surface can render verbatim. Null exactly when available. */
  withheldReason: string | null;
  provider: SourceName | null;
  accountKey: string | null;
  accountScope: AlertAccountScope | null;
  costBasis: string | null;
  /** Epoch milliseconds. Both set when available, both null otherwise. */
  windowStartMs: number | null;
  windowEndMs: number | null;
  windowGranularity: AlertWindowGranularity;
}

type AlertContext = AlertEntry['context'];

function withheld(
  reason: string,
  common: Partial<AlertScope>,
): AlertScope {
  return {
    available: false,
    withheldReason: reason,
    provider: null,
    accountKey: null,
    accountScope: null,
    costBasis: null,
    ...common,
    windowStartMs: null,
    windowEndMs: null,
    windowGranularity: 'instant',
  };
}

function resolved(
  startMs: number,
  endMs: number,
  common: Partial<AlertScope>,
  granularity: AlertWindowGranularity = 'instant',
): AlertScope {
  return {
    available: true,
    withheldReason: null,
    provider: null,
    accountKey: null,
    accountScope: null,
    costBasis: null,
    ...common,
    windowStartMs: startMs,
    windowEndMs: endMs,
    windowGranularity: granularity,
  };
}

/** Read a retained ISO-8601 instant. A naive value means UTC, exactly as the
 *  Python kernel reads it — the two must agree on the same retained string. */
export function parseInstantMs(value: unknown): number | null {
  if (typeof value !== 'string' || value.trim() === '') return null;
  const text = value.trim();
  // `Date.parse` treats a bare `YYYY-MM-DDTHH:MM:SS` as LOCAL time, which is
  // the whole reset-hour offset this session exists to stop guessing at, so a
  // naive value is pinned to UTC before parsing.
  const naive = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(text);
  const ms = Date.parse(naive ? `${text}Z` : text);
  return Number.isFinite(ms) ? ms : null;
}

/** Read a retained `YYYY-MM-DD` calendar day as its UTC midnight. */
export function parseCalendarDayMs(value: unknown): number | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.trim())) return null;
  const ms = Date.parse(`${value.trim()}T00:00:00Z`);
  return Number.isFinite(ms) ? ms : null;
}

function parseEpochSecondsMs(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.trunc(value) * 1000;
}

/** Advance one civil month on the instant's own UTC fields, matching the
 *  Python kernel's `_add_calendar_month`. A period straddling a DST transition
 *  derives an end one hour from where the next window actually starts; that
 *  caveat is recorded in `docs/alerts-gotchas.md`. */
function addCalendarMonthMs(ms: number): number {
  const d = new Date(ms);
  const year = d.getUTCFullYear() + (d.getUTCMonth() === 11 ? 1 : 0);
  const month = (d.getUTCMonth() + 1) % 12;
  for (let day = d.getUTCDate(); day > 1; day -= 1) {
    const candidate = Date.UTC(
      year, month, day,
      d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds(),
    );
    if (new Date(candidate).getUTCMonth() === month) return candidate;
  }
  return Date.UTC(
    year, month, 1,
    d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), d.getUTCMilliseconds(),
  );
}

/** End of the half-open `[start, end)` window a budget period names. */
export function periodWindowEndMs(
  period: string | undefined | null,
  startMs: number | null,
): number | null {
  if (startMs == null) return null;
  if (period === 'subscription-week') return startMs + SUBSCRIPTION_WEEK_MS;
  if (period === 'calendar-week') return startMs + CALENDAR_WEEK_MS;
  if (period === 'calendar-month') return addCalendarMonthMs(startMs);
  return null;
}

function normalizeAccountKey(key: string | null | undefined): string | null {
  if (key == null) return null;
  const text = String(key).trim();
  return text === '' ? null : text;
}

function accountScopeFor(key: string | null): AlertAccountScope {
  return key === VENDOR_WIDE_KEY ? 'vendor_wide' : 'account';
}

function scopeWeekly(ctx: AlertContext, key: string | null): AlertScope {
  // A subscription week is anchored to a real reset instant, which is what
  // `percent_milestones.week_start_at` retains and what both the envelope and
  // the dispatch payload publish since #620 S1. `week_start_date` is the same
  // week's calendar label and is all a pre-`week_start_at` row has; falling
  // back to it recovers the week but not the reset hour, so the granularity
  // drops with it rather than a midnight instant being invented.
  let start = parseInstantMs(ctx.week_start_at);
  let granularity: AlertWindowGranularity = 'instant';
  if (start == null) {
    start = parseCalendarDayMs(ctx.week_start_date);
    granularity = 'day';
  }
  const common = {
    provider: 'claude' as SourceName,
    accountKey: key,
    accountScope: accountScopeFor(key),
    costBasis: 'cumulative_cost_usd',
  };
  if (start == null) {
    return withheld(
      'the weekly alert retains no week start, so its window cannot be derived',
      common,
    );
  }
  return resolved(start, start + SUBSCRIPTION_WEEK_MS, common, granularity);
}

function scopeFiveHour(ctx: AlertContext, key: string | null): AlertScope {
  const common = {
    provider: 'claude' as SourceName,
    accountKey: key,
    accountScope: accountScopeFor(key),
    costBasis: 'block_cost_usd',
  };
  const start = parseInstantMs(ctx.block_start_at);
  if (start != null) return resolved(start, start + FIVE_HOUR_MS, common);
  // `five_hour_window_key` is the RESET epoch floored to ten minutes
  // (`_canonical_5h_window_key`), so it names the window's END. Reading it as a
  // start would place the window five hours early.
  const end = parseEpochSecondsMs(ctx.five_hour_window_key);
  if (end == null) {
    return withheld(
      'the five-hour alert retains no block start, so its window cannot be derived',
      common,
    );
  }
  return resolved(end - FIVE_HOUR_MS, end, common);
}

function scopeBudgetFamily(
  ctx: AlertContext,
  key: string | null,
  provider: SourceName,
  defaultPeriod: string | null,
): AlertScope {
  const period = ctx.period ?? defaultPeriod ?? undefined;
  const start = parseInstantMs(ctx.period_start_at) ?? parseInstantMs(ctx.week_start_at);
  const common = {
    provider,
    accountKey: key,
    accountScope: accountScopeFor(key),
    costBasis: 'spent_usd',
  };
  if (start == null) {
    return withheld(
      'the budget alert retains no period start, so its window cannot be derived',
      common,
    );
  }
  const end = periodWindowEndMs(period, start);
  if (end == null) {
    return withheld(
      `the budget alert names no derivable period (got ${JSON.stringify(period ?? null)})`,
      common,
    );
  }
  return resolved(start, end, common);
}

function scopeProjectBudget(ctx: AlertContext, key: string | null): AlertScope {
  // `project_budget_milestones` is deliberately account-blind and stamped `*`,
  // so the scope is vendor-wide whatever the caller passes. Narrowing it to an
  // account would invent an attribution the row never recorded.
  const period = ctx.period ?? 'subscription-week';
  const start = parseInstantMs(ctx.week_start_at) ?? parseInstantMs(ctx.period_start_at);
  const common = {
    provider: 'claude' as SourceName,
    accountKey: key,
    accountScope: 'vendor_wide' as AlertAccountScope,
    costBasis: 'spent_usd',
  };
  if (start == null) {
    return withheld(
      'the project-budget alert retains no window start, so its window cannot be derived',
      common,
    );
  }
  const end = periodWindowEndMs(period, start);
  if (end == null) {
    return withheld(
      `the project-budget alert names no derivable period (got ${JSON.stringify(period)})`,
      common,
    );
  }
  return resolved(start, end, common);
}

function scopeProjected(ctx: AlertContext, key: string | null): AlertScope {
  const metric = ctx.metric;
  const provider: SourceName = metric === 'codex_budget_usd' ? 'codex' : 'claude';
  const common = {
    provider,
    accountKey: key,
    accountScope: accountScopeFor(key),
    // `weekly_pct` projects against the percent cap, which retains no dollar
    // basis; the two budget metrics project a dollar figure.
    costBasis: metric === 'weekly_pct' ? null : 'projected_value',
  };
  const start = parseInstantMs(ctx.week_start_at) ?? parseInstantMs(ctx.period_start_at);
  if (start == null) {
    return withheld(
      'the projected alert retains no period start, so its window cannot be derived',
      common,
    );
  }
  // The metric fixes the period for `weekly_pct` — a weekly-percent projection
  // is a subscription week by construction. The budget metrics do not, so an
  // absent `period` is withheld rather than guessed.
  const period = ctx.period ?? (metric === 'weekly_pct' ? 'subscription-week' : undefined);
  const end = periodWindowEndMs(period, start);
  if (end == null) {
    return withheld(
      `the projected alert retains no period for metric ${JSON.stringify(metric ?? null)}, `
      + 'so its window length is unknown',
      common,
    );
  }
  return resolved(start, end, common);
}

const AXIS_HANDLERS: Record<
  AlertAxis,
  (ctx: AlertContext, key: string | null) => AlertScope
> = {
  weekly: scopeWeekly,
  five_hour: scopeFiveHour,
  budget: (ctx, key) => scopeBudgetFamily(ctx, key, 'claude', 'subscription-week'),
  // No default period: Codex has no subscription week, and guessing between
  // the two calendar periods would double or halve the window.
  codex_budget: (ctx, key) => scopeBudgetFamily(ctx, key, 'codex', null),
  project_budget: scopeProjectBudget,
  projected: scopeProjected,
};

/** Derive one alert's scope from its retained context plus its account stamp.
 *  No argument names or carries an alert identifier. */
export function deriveAlertScope(
  axis: AlertAxis,
  context: AlertContext | null | undefined,
  accountKey?: string | null,
): AlertScope {
  const ctx = (context ?? {}) as AlertContext;
  const handler = AXIS_HANDLERS[axis];
  if (handler == null) {
    return withheld(`unknown alert axis: ${JSON.stringify(axis)}`, {});
  }
  return handler(ctx, normalizeAccountKey(accountKey));
}

/** The scope of one dashboard alert row. */
export function deriveScope(entry: AlertEntry): AlertScope {
  return deriveAlertScope(entry.axis, entry.context, entry.accountKey);
}

/** Whether `now` falls inside the half-open `[start, end)` window.
 *  A withheld scope is never live — there is no window to be inside. */
export function isWindowLive(scope: AlertScope, now: Date): boolean {
  if (!scope.available || scope.windowStartMs == null || scope.windowEndMs == null) {
    return false;
  }
  const nowMs = now.getTime();
  return nowMs >= scope.windowStartMs && nowMs < scope.windowEndMs;
}

/** The instant the dashboard's own data describes.
 *
 * `generated_at` rather than the browser clock, because every other figure on
 * the page is as of that instant; a tab left open overnight would otherwise
 * declare a window closed while the numbers beside it still describe it.
 * Falls back to the client clock only when the envelope has not arrived. */
export function envelopeNow(env: Envelope | null): Date {
  const ms = parseInstantMs(env?.generated_at);
  return ms == null ? new Date() : new Date(ms);
}

// ───────────────────────── dashboard navigation (D12) ────────────────────────
//
// Live windows navigate; closed windows say why they cannot. The existing
// modals cannot render an arbitrary historical window — `CurrentWeekModal`
// always opens on the live week, the project drill accepts only 1/4/8/12 weeks
// anchored at the current week, and modal actions carry only session, block,
// date and project selectors. Extending those contracts is S2's work.
//
// This costs S1 nothing for the case that matters, because a warn or over
// state is live by construction. Where the fixed window cannot be addressed,
// navigation is WITHHELD with a stated reason; substituting the current window
// would be the silent scope change the epic forbids.

export type AlertTargetModal =
  | 'current-week'
  | 'forecast'
  | 'projects'
  | 'block'
  | 'monthly';

export interface AlertTarget {
  source: SourceName;
  modal: AlertTargetModal;
  blockStartAt?: string;
  projectKey?: string;
  /** What the affordance says it opens. */
  label: string;
}

export interface AlertNavigation {
  available: boolean;
  withheldReason: string | null;
  target: AlertTarget | null;
}

export const CLOSED_WINDOW_REASON =
  'The window this alert describes has closed. The dashboard can only show the '
  + 'live one, so it is not opened here.';

// The condition this names is the one the check actually detects: the alert
// carries no `block_start_at`, so no block row can be addressed. It is NOT a
// retention check, and must not be worded as one — a genuinely purged block
// whose alert DOES carry a start still opens `BlockModal`, which renders its
// own not-found message. Both outcomes are honest; only the sentence was not.
export const NO_BLOCK_IDENTITY_REASON =
  'This alert recorded no five-hour block start, so the dashboard cannot tell '
  + 'which block to open.';

export const VENDOR_WIDE_PROJECT_REASON =
  'This alert is stamped vendor-wide across accounts, so it cannot be narrowed '
  + 'to one account’s projects.';

export const NO_CALENDAR_WEEK_SURFACE_REASON =
  'The dashboard publishes no calendar-week view, so this budget period cannot '
  + 'be opened.';

export const LIVE_WEEK_MISMATCH_REASON =
  'The live week on this dashboard is not the week this alert describes, so it '
  + 'is not opened here.';

function navWithheld(reason: string): AlertNavigation {
  return { available: false, withheldReason: reason, target: null };
}

function navTo(target: AlertTarget): AlertNavigation {
  return { available: true, withheldReason: null, target };
}

/** Half-open equality against the dashboard's live subscription week.
 *
 * D12 requires the client to ASSERT that the target modal's window is the
 * alert's window rather than assume it. `current_week.reset_at_utc` is the
 * live week's end, so comparing ends compares the windows: two subscription
 * weeks of the same nominal length that end at the same instant are the same
 * week. When the envelope publishes no live end, there is nothing to compare
 * against and the liveness test above is all the evidence there is. */
function liveWeekEndMs(env: Envelope | null): number | null {
  return parseInstantMs(env?.current_week?.reset_at_utc ?? null);
}

function weekTarget(
  scope: AlertScope,
  env: Envelope | null,
  source: SourceName,
  label: string,
): AlertNavigation {
  const liveEnd = liveWeekEndMs(env);
  if (liveEnd != null && scope.windowEndMs != null) {
    // A whole minute of tolerance: the reset instant is normalised to the hour
    // on the way in, and the two legs can round a stored second differently.
    if (Math.abs(liveEnd - scope.windowEndMs) > 60_000) {
      return navWithheld(LIVE_WEEK_MISMATCH_REASON);
    }
  }
  return navTo({ source, modal: 'current-week', label });
}

function budgetTarget(
  scope: AlertScope,
  env: Envelope | null,
  entry: AlertEntry,
  source: SourceName,
): AlertNavigation {
  const period = entry.context.period
    ?? (source === 'claude' ? 'subscription-week' : undefined);
  if (period === 'subscription-week') {
    // Codex navigates reset-defined quota CYCLES, and every other Codex
    // surface says so — `trendVocabulary`'s cycle vocabulary and the hero
    // navigator's "Older cycle". A button reading "week" here would name a
    // Claude concept over the cycle it actually opens.
    const label = source === 'codex' ? 'Open this cycle' : 'Open this week';
    return weekTarget(scope, env, source, label);
  }
  if (period === 'calendar-month') {
    // `presentationPeriodRows(env, source, 'monthly')` is calendar months for
    // both providers, and `PeriodModal` clamps its selection to the first
    // (current) row, which is the live month — this alert's own window.
    return navTo({ source, modal: 'monthly', label: 'Open this month' });
  }
  // `calendar-week` has no matching surface: the Claude weekly view is keyed by
  // subscription week and the Codex weekly view by quota cycle, so neither is
  // the civil week this budget measures. Offering one would state a window the
  // surface does not show.
  return navWithheld(NO_CALENDAR_WEEK_SURFACE_REASON);
}

/**
 * Where one dashboard alert row leads, or why it leads nowhere.
 *
 * `now` is passed rather than read, so the liveness question has one visible
 * anchor at every call site (`envelopeNow(env)` in production).
 */
export function alertNavigation(
  entry: AlertEntry,
  env: Envelope | null,
  now: Date,
): AlertNavigation {
  const scope = deriveScope(entry);
  if (!scope.available) {
    return navWithheld(scope.withheldReason ?? CLOSED_WINDOW_REASON);
  }

  // `five_hour` is the one axis with a historical target: `BlockModal` is
  // addressed by `block_start_at`, so a retained block still opens however old
  // it is. A row that recovered its window from the reset key alone names no
  // block row, so `--block-start`'s dashboard equivalent would select nothing.
  if (entry.axis === 'five_hour') {
    const blockStartAt = entry.context.block_start_at;
    if (typeof blockStartAt !== 'string' || blockStartAt.trim() === '') {
      return navWithheld(NO_BLOCK_IDENTITY_REASON);
    }
    return navTo({
      source: 'claude',
      modal: 'block',
      blockStartAt,
      label: 'Open this block',
    });
  }

  if (!isWindowLive(scope, now)) return navWithheld(CLOSED_WINDOW_REASON);

  if (entry.axis === 'weekly') {
    return weekTarget(scope, env, 'claude', 'Open this week');
  }
  if (entry.axis === 'budget') {
    return budgetTarget(scope, env, entry, 'claude');
  }
  if (entry.axis === 'codex_budget') {
    return budgetTarget(scope, env, entry, 'codex');
  }
  if (entry.axis === 'projected') {
    return navTo({
      source: scope.provider ?? 'claude',
      modal: 'forecast',
      label: 'Open the forecast',
    });
  }
  // `project_budget`. The row is stamped `*`, and the stamp only APPEARS on a
  // decorated provider — below two real accounts the account is unambiguous
  // and the field is omitted entirely. So a present `*` means the dashboard
  // cannot say which account's projects this describes, and the drill would
  // silently narrow a vendor-wide figure to one account.
  if (entry.accountKey === VENDOR_WIDE_KEY) {
    return navWithheld(VENDOR_WIDE_PROJECT_REASON);
  }
  const projectKey = entry.context.project_key;
  if (typeof projectKey !== 'string' || projectKey.trim() === '') {
    return navWithheld(
      'This alert retains no project identity, so its project cannot be opened.',
    );
  }
  return navTo({
    source: 'claude',
    modal: 'projects',
    projectKey,
    label: 'Open this project',
  });
}

/** The selection a target needs the board to be on before its modal opens.
 *  `null` when the board is already bound to that provider. */
export function selectionShiftFor(
  target: AlertTarget,
  activeSource: DashboardSelection,
): DashboardSelection | null {
  return activeSource === target.source ? null : target.source;
}
