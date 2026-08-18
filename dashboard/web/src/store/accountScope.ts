// #416 Task 15 — the SINGLE client selector chokepoint for per-account scoping.
//
// Before this module, `resolveAccountFocus` was called independently by four
// components, each doing something different with the answer — which is exactly
// how the panels drifted out of scope while the chip claimed to filter. Every
// panel now reads its provider data through `scopeEnvelope` / `scopeToAccount`;
// NO panel reaches into `data.account_scopes` itself.
//
// The wire contract this implements (spec §5.3 / §6):
//
//  * `data.account_scopes` is a `{account_key: child}` map, present ONLY when
//    the provider is decorated (>1 REAL account). At <=1 real account the key is
//    ABSENT, not empty — branch on presence.
//  * Each child mirrors the parent's shape exactly, so "All accounts" renders
//    the PARENT (`codex.periods`, `codex.quota`, …) and a focused account
//    renders ITS CHILD. Nothing is summed, averaged or reconstructed here:
//    `models` / `model_breakdowns` are not reconstructible and `used_pct` /
//    `dollar_per_pct` are not additive.
//  * The map may hold keys with no hero card (data-only residuals from registry
//    drift). Those are NOT chip-selectable — chips iterate `accounts[]`, lookup
//    goes through this map.
//  * Every `accounts[]` card key DOES resolve to a scope (an evidence-less
//    account gets `is_empty: true`), so a missing child is drift. We degrade to
//    an explicit EMPTY scope and NEVER fall back to the parent: the parent under
//    focus would attribute every account's numbers to one.
//  * `unattributed` is a first-class scope (dimmed, totals-only), not an error.
//
// Claude ships `accounts[]` but no `account_scopes`, so a Claude focus is a
// hero decoration only. `scopesSupported` says so honestly, and the panels keep
// their "all accounts (unfiltered)" disclaimer on Claude rather than lying.

import { resolveAccountFocus, sourceAccounts } from './accountFocus';
import type {
  AccountCard,
  CodexAccountScope,
  CodexHero,
  CodexSourceData,
  DashboardSelection,
  Envelope,
  SourceEntry,
  SourceName,
  SourcesMap,
} from '../types/envelope';

// The only provider that emits per-account children today.
const SCOPED_SOURCE = 'codex';

export interface AccountScopeResult {
  // The VIEW this scope was resolved for ('claude' | 'codex' | 'all').
  source: DashboardSelection;
  // #556 S5 §5.4 — the PROVIDER the scope describes, which under All is not
  // the same thing as the view. `null` when the view names no provider (an All
  // read that was given none).
  provider: SourceName | null;
  // The effective focused account key, or null for "All accounts". Reconciled
  // against `accounts[]` (stored-valid-else-All), so a vanished or residual key
  // resolves to All rather than to a scope with no chip. Null whenever the
  // source cannot scope, so reading this alone can never produce a scoped claim
  // the data does not support.
  accountKey: string | null;
  // The chip's reconciled selection REGARDLESS of whether the source can scope
  // it. Non-null with `scopesSupported: false` is the honest "a chip is picked
  // but this provider ships no per-account children" state — which is exactly
  // when a panel must keep its "all accounts (unfiltered)" disclaimer.
  requestedKey: string | null;
  // The hero card for the focused key (null under All).
  card: AccountCard | null;
  // The raw child (null under All, and null when the child is missing).
  scope: CodexAccountScope | null;
  // The provider data the panels should render: the parent under All, the
  // account-scoped composition under focus.
  data: CodexSourceData | null;
  // True when the focused account has neither accounting rows nor quota
  // evidence — render an explicit empty state, never the parent's numbers.
  isEmpty: boolean;
  // True when this source can actually scope (Codex, decorated). False for
  // Claude and for undecorated sources: a panel that would otherwise drop its
  // "unfiltered" disclaimer must keep it when this is false.
  scopesSupported: boolean;
}

// Read the per-account children off a source entry. Returns null when the key
// is ABSENT (the <=1-real-account shape) or the payload is not the expected map.
export function accountScopesOf(
  entry: SourceEntry<unknown> | null,
): Record<string, CodexAccountScope> | null {
  const data = entry?.data as { account_scopes?: unknown } | null | undefined;
  const scopes = data?.account_scopes;
  if (scopes == null || typeof scopes !== 'object' || Array.isArray(scopes)) return null;
  return scopes as Record<string, CodexAccountScope>;
}

function emptyScope(): CodexAccountScope {
  const period = { rows: [], total_cost_usd: 0, total_tokens: 0, display_tz: 'UTC' };
  return {
    is_empty: true,
    periods: { daily: period, monthly: period, weekly: period },
    sessions: { rows: [], total_sessions: 0, total_cost_usd: 0, total_tokens: 0 },
    projects: { rows: [], total_cost_usd: 0, total_tokens: 0 },
    cache_report: null,
    budget: { status: null, milestones: [], projected: [] },
    quota: {
      summary: {
        window_count: 0,
        active_window_count: 0,
        latest_percent: null,
        freshness: 'unavailable',
        active: [],
      },
      histories: [],
      milestones: [],
      blocks: [],
      cycle_index: [],
    },
    alerts: { rows: [] },
  };
}

// The focused hero: that account's own cycle, percentage, reset, spend and
// tokens (spec §6). Every value is SELECTED from a server-emitted per-account
// field — the card's own totals and the account's own `hero.cycles[]` entry —
// never re-derived by arithmetic across accounts.
//
// An account with no live cycle (an expired one, or one that never had
// evidence) gets a null cycle and no `cycle_freshness`, so the hero renders
// reset and percentage BLANK rather than the aggregate's stale values. That is
// the per-account expiry §6 asks for and subsumes #360's aggregate-only clock.
function focusedHero(
  parent: CodexHero,
  card: AccountCard | null,
  child: CodexAccountScope,
  accountKey: string,
): CodexHero {
  const cycle = (parent.cycles ?? []).find((c) => c.accountKey === accountKey) ?? null;
  const stale = child.quota.summary.freshness === 'stale';
  return {
    cost_usd: card?.spendUsd ?? 0,
    input_tokens: card?.inputTokens ?? 0,
    cached_input_tokens: card?.cachedInputTokens ?? 0,
    output_tokens: card?.outputTokens ?? 0,
    reasoning_output_tokens: card?.reasoningOutputTokens ?? 0,
    total_tokens: card?.totalTokens ?? 0,
    cycle: cycle == null ? null : {
      window_minutes: cycle.window_minutes,
      start_at: cycle.start_at,
      resets_at: cycle.resets_at,
    },
    // Per-card staleness, surfaced independently of `cycles_all[0]`: the child's
    // own quota freshness, not the aggregate's disclosure.
    ...(cycle != null && stale ? { cycle_freshness: 'stale' as const } : {}),
    quota: child.quota.summary,
    budget: child.budget.status,
    alerts: { count: child.alerts.rows.length },
    // Retained UNSCOPED so the "All accounts" per-account strip and the chip
    // row keep working while a chip is focused.
    ...(parent.cycles == null ? {} : { cycles: parent.cycles }),
  };
}

function composeScopedData(
  parent: CodexSourceData,
  child: CodexAccountScope,
  card: AccountCard | null,
  accountKey: string,
): CodexSourceData {
  return {
    ...parent,
    hero: focusedHero(parent.hero, card, child, accountKey),
    periods: child.periods,
    sessions: child.sessions,
    projects: child.projects,
    quota: child.quota,
    budget: child.budget,
    alerts: child.alerts,
    cache_report: child.cache_report ?? null,
    // `accounts` and `account_scopes` ride through untouched: the chip row and
    // the hero cards need the full population regardless of focus.
  };
}

// Resolve the effective scope for one source. Pure; safe to call per render.
//
// #556 S5 §5.4 — `provider` names the provider the scope describes and defaults
// to the view's own provider, which keeps every pre-S5 two-and-three-argument
// call byte-identical: a provider tab scopes itself, and an All read that names
// no provider still resolves to "no scope", exactly as before. Under All a
// caller passes the provider explicitly, and the stored value it hands in comes
// from that provider's All slot.
export function scopeToAccount(
  env: Envelope | null,
  source: DashboardSelection,
  stored: string,
  provider: SourceName | null = source === 'all' ? null : source,
): AccountScopeResult {
  const entry = provider == null
    ? null
    : ((env?.sources?.[provider] ?? null) as SourceEntry<unknown> | null);
  const parent = (entry?.data ?? null) as CodexSourceData | null;
  const scopes = provider === SCOPED_SOURCE ? accountScopesOf(entry) : null;
  const supported = scopes != null;
  const key = provider == null ? null : resolveAccountFocus(env, provider, stored);
  if (key == null || !supported) {
    return {
      source,
      provider,
      accountKey: null,
      requestedKey: key,
      card: null,
      scope: null,
      data: parent,
      isEmpty: false,
      scopesSupported: supported,
    };
  }
  const card = (sourceAccounts(entry) ?? []).find((a) => a.accountKey === key) ?? null;
  // A card without a child is registry/data drift. Degrade to an explicit
  // empty scope — never to the parent, which would show every account's numbers
  // under one account's chip.
  const child = scopes![key] ?? emptyScope();
  return {
    source,
    provider,
    accountKey: key,
    requestedKey: key,
    card,
    scope: child,
    data: parent == null ? null : composeScopedData(parent, child, card, key),
    isEmpty: child.is_empty === true,
    scopesSupported: true,
  };
}

// Memo: one envelope per tick, one scoped rewrite per (source, focus). Without
// this, every render would mint a new `data` object and defeat downstream
// memoisation — and `useSyncExternalStore` requires a reference-stable
// getSnapshot result or it re-renders forever.
const memo = new WeakMap<object, Map<string, Envelope>>();

// The scoped envelope every panel consumes. IDENTITY-PRESERVING when the focus
// resolves to "All accounts" (or the source cannot scope), so an undecorated /
// unfocused dashboard is byte-identical to today and every existing consumer,
// test and golden is untouched.
// #583 S3 §4 — there is now ONE copy. `sources.all.data.providers` publishes
// null for both providers, so rewriting it would reconstruct client-side the
// duplication the wire change removed. Only the physical `sources.codex.data`
// is narrowed, and every All panel reads the physical entry through
// `presentationProviders`' fallback.
// `sources.all.data.combined`, the aggregates and every other deliberately
// unscoped outcome are left intact: a combined figure is never recomputed from
// a focused child.
export function scopeEnvelope(
  env: Envelope | null,
  source: DashboardSelection,
  stored: string,
): Envelope | null {
  if (env == null) return null;
  // Codex is the only provider with per-account children, so it is the only
  // subtree a rewrite can narrow — on its own tab and under All alike.
  if (source !== SCOPED_SOURCE && source !== 'all') return env;
  // The memo key needs only the RECONCILED FOCUS, never the scoped rewrite, so
  // resolve the cheap half first and consult the memo before running
  // `composeScopedData`. Doing the rewrite first and discarding it on a hit ran
  // the 8-field rewrite once per subscribed panel per render on the hot
  // snapshot path (#268 / #313), while the comment above claimed one rewrite
  // per (source, focus). These guards are exactly the conditions under which
  // `scopeToAccount` returns a non-null `accountKey` AND a non-null `data`; the
  // full call below still re-checks both, so it stays the authority.
  const sourceEntry = (env.sources?.[SCOPED_SOURCE] ?? null) as SourceEntry<unknown> | null;
  if (sourceEntry?.data == null || accountScopesOf(sourceEntry) == null) return env;
  const focusKey = resolveAccountFocus(env, SCOPED_SOURCE, stored);
  if (focusKey == null) return env;
  // The VIEW stays part of the memo key, not just the focus. Since #583 S3 the
  // two selections produce structurally identical envelopes, so the key is no
  // longer separating two different rewrites — it keeps each selection's
  // getSnapshot result on its own stable object identity, which is what
  // `useSyncExternalStore` requires, and it means a later per-view rewrite
  // cannot silently serve one view the other's result.
  const cacheKey = `${source}\u0000${focusKey}`;
  let perEnv = memo.get(env as unknown as object);
  if (perEnv == null) {
    perEnv = new Map();
    memo.set(env as unknown as object, perEnv);
  }
  const hit = perEnv.get(cacheKey);
  if (hit != null) return hit;
  const resolved = scopeToAccount(env, source, stored, SCOPED_SOURCE);
  if (resolved.accountKey == null || resolved.data == null) return env;
  const sources = {
    ...env.sources,
    [SCOPED_SOURCE]: { ...sourceEntry, data: resolved.data },
  } as SourcesMap;
  const scoped = { ...env, sources } as Envelope;
  perEnv.set(cacheKey, scoped);
  return scoped;
}
