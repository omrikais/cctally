import { describe, expect, it } from 'vitest';
import { ALL_ACCOUNTS } from './accountFocus';
import {
  accountScopesOf,
  scopeEnvelope,
  scopeToAccount,
} from './accountScope';
import type {
  AccountCard,
  CodexAccountScope,
  CodexSourceData,
  Envelope,
  SourceEntry,
} from '../types/envelope';

// #416 Task 15 — the single client selector chokepoint. Every panel reads its
// scoped provider data through this module; no panel reaches into
// `account_scopes` itself. These tests pin the CONTRACT (which object a given
// focus resolves to, and what must never happen) — they are jsdom-free pure
// selector tests and prove nothing about rendering.

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);
const EMPTY = 'e'.repeat(32);
const RESIDUAL = 'd'.repeat(32);
const UNATTRIBUTED = 'unattributed';

function card(over: Partial<AccountCard> & { accountKey: string }): AccountCard {
  return {
    accountKey: over.accountKey,
    label: over.label ?? over.accountKey.slice(0, 4),
    plan: over.plan ?? null,
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
    unattributed: over.unattributed,
  };
}

function periodView(label: string, cost: number) {
  return {
    rows: [{
      label,
      cost_usd: cost,
      input_tokens: 1,
      cached_input_tokens: 0,
      output_tokens: 1,
      reasoning_output_tokens: 0,
      total_tokens: 2,
      models: ['gpt-5'],
    }],
    total_cost_usd: cost,
    total_tokens: 2,
    display_tz: 'UTC',
  };
}

function scope(over: { marker: string; is_empty?: boolean }): CodexAccountScope {
  return {
    is_empty: over.is_empty ?? false,
    periods: {
      daily: periodView(`${over.marker}-daily`, 1),
      monthly: periodView(`${over.marker}-monthly`, 1),
      weekly: periodView(`${over.marker}-weekly`, 1),
    },
    sessions: {
      rows: [{
        key: `${over.marker}-session`,
        source: 'codex',
        last_activity: '2026-07-28T00:00:00Z',
        cost_usd: 1,
        input_tokens: 1,
        cached_input_tokens: 0,
        output_tokens: 1,
        reasoning_output_tokens: 0,
        total_tokens: 2,
        models: ['gpt-5'],
      }],
      total_sessions: 1,
      total_cost_usd: 1,
      total_tokens: 2,
    },
    projects: { rows: [], total_cost_usd: 0, total_tokens: 0 },
    cache_report: null,
    budget: { status: null, milestones: [], projected: [] },
    quota: {
      summary: {
        window_count: 1,
        active_window_count: 1,
        latest_percent: 12,
        freshness: 'fresh',
        active: [],
      },
      histories: [],
      milestones: [],
      blocks: [],
      cycle_index: [{
        key: `${over.marker}-cycle`,
        label: over.marker,
        start_at_utc: '2026-07-21T00:00:00Z',
        end_at_utc: '2026-07-28T00:00:00Z',
        resets_at_utc: '2026-07-28T00:00:00Z',
        is_current: true,
        milestone_count: 1,
        block_count: 1,
        detail_stamp: `${over.marker}-stamp`,
      }],
    },
    alerts: { rows: [], actual_thresholds: [90], projected_thresholds: [90] },
  };
}

function codexData(over: {
  accounts?: AccountCard[] | null;
  scopes?: Record<string, CodexAccountScope> | null;
}): Partial<CodexSourceData> {
  const parent = scope({ marker: 'parent' });
  return {
    hero: {
      cost_usd: 99,
      input_tokens: 9,
      cached_input_tokens: 0,
      output_tokens: 9,
      reasoning_output_tokens: 0,
      total_tokens: 18,
      cycle: {
        window_minutes: 10_080,
        start_at: '2026-07-21T00:00:00Z',
        resets_at: '2026-07-28T00:00:00Z',
      },
      quota: parent.quota.summary,
      budget: null,
      alerts: { count: 0 },
      cycles: [
        {
          accountKey: A,
          window_minutes: 10_080,
          start_at: '2026-07-21T00:00:00Z',
          resets_at: '2026-07-28T00:00:00Z',
          used_percent: 41,
          cost_usd: 12.5,
          total_tokens: 100,
        },
        {
          accountKey: B,
          window_minutes: 10_080,
          start_at: '2026-07-20T00:00:00Z',
          resets_at: '2026-07-27T00:00:00Z',
          used_percent: 7,
          cost_usd: 3.5,
          total_tokens: 20,
        },
      ],
    },
    periods: parent.periods,
    sessions: parent.sessions,
    projects: parent.projects,
    quota: parent.quota,
    budget: parent.budget,
    alerts: parent.alerts,
    cache_report: null,
    ...(over.accounts == null ? {} : { accounts: over.accounts }),
    ...(over.scopes == null ? {} : { account_scopes: over.scopes }),
  } as Partial<CodexSourceData>;
}

function envelope(data: Partial<CodexSourceData>): Envelope {
  const codex = {
    availability: 'ok',
    freshness: 'fresh',
    warnings: [],
    data_version: 'v1',
    last_success_at: '2026-07-28T00:00:00Z',
    capabilities: {},
    data,
  } as unknown as SourceEntry<unknown>;
  return { sources: { claude: null, codex, all: null } } as unknown as Envelope;
}

const DECORATED = envelope(codexData({
  accounts: [
    card({ accountKey: A, label: 'nova@example.com', spendUsd: 12.5, weeklyPercent: 41, inputTokens: 40, totalTokens: 100 }),
    card({ accountKey: B, label: 'lark@example.com', spendUsd: 3.5, weeklyPercent: 7 }),
    card({ accountKey: EMPTY, label: 'quiet@example.com' }),
    card({ accountKey: UNATTRIBUTED, label: 'unattributed', unattributed: true, spendUsd: 400 }),
  ],
  scopes: {
    [A]: scope({ marker: 'A' }),
    [B]: scope({ marker: 'B' }),
    [EMPTY]: { ...scope({ marker: 'E' }), is_empty: true },
    [UNATTRIBUTED]: scope({ marker: 'U' }),
    // A data-only residual key: present in `account_scopes` but with NO hero
    // card. It must never become chip-selectable.
    [RESIDUAL]: scope({ marker: 'R' }),
  },
}));

const UNDECORATED = envelope(codexData({}));

// #556 S5 §5.4 — the SAME decorated Codex data, plus the `all` entry the server
// composes. `combined` and `aggregates` are deliberately unscoped outcomes that
// a focus must not touch.
// #583 S3 §4 — `providers` no longer mirrors the provider data objects. The
// server publishes null for both members and every All panel reads the physical
// `sources.claude` / `sources.codex` entries through `presentationProviders`'
// fallback, so this fixture states the null stub the wire actually carries.
const COMBINED_MARKER = { state: 'available', spend_usd: 999 };
const AGGREGATES_MARKER = { range: { kind: 'shared', start_at: 'x', end_at: 'y' } };
const DECORATED_ALL = (() => {
  const env = envelope(codexData({
    accounts: [
      card({ accountKey: A, label: 'nova@example.com', spendUsd: 12.5, weeklyPercent: 41 }),
      card({ accountKey: B, label: 'lark@example.com', spendUsd: 3.5, weeklyPercent: 7 }),
    ],
    scopes: { [A]: scope({ marker: 'A' }), [B]: scope({ marker: 'B' }) },
  }));
  return {
    ...env,
    sources: {
      ...env.sources,
      all: {
        availability: 'ok',
        freshness: 'fresh',
        warnings: [],
        data_version: 'v1',
        last_success_at: '2026-07-28T00:00:00Z',
        capabilities: {},
        data: {
          providers: { claude: null, codex: null },
          combined: COMBINED_MARKER,
          aggregates: AGGREGATES_MARKER,
        },
      },
    },
  } as unknown as Envelope;
})();

describe('accountScopesOf', () => {
  it('reads the map off a decorated entry and null when the key is ABSENT', () => {
    expect(Object.keys(accountScopesOf(DECORATED.sources!.codex)!).sort())
      .toEqual([A, B, RESIDUAL, EMPTY, UNATTRIBUTED].sort());
    // At <=1 real account the key is absent, not empty — branch on presence.
    expect(accountScopesOf(UNDECORATED.sources!.codex)).toBeNull();
  });
});

describe('scopeToAccount', () => {
  it('returns the merged PARENT for All accounts', () => {
    const s = scopeToAccount(DECORATED, 'codex', ALL_ACCOUNTS);
    expect(s.accountKey).toBeNull();
    expect(s.scope).toBeNull();
    expect(s.isEmpty).toBe(false);
    expect(s.data!.periods.daily.rows[0].label).toBe('parent-daily');
  });

  it('returns the CHILD slice for a focused account', () => {
    const s = scopeToAccount(DECORATED, 'codex', A);
    expect(s.accountKey).toBe(A);
    expect(s.card!.label).toBe('nova@example.com');
    expect(s.data!.periods.daily.rows[0].label).toBe('A-daily');
    expect(s.data!.sessions.rows[0].key).toBe('A-session');
    // Never the parent's cycle index — that would render A's history on B.
    expect(s.data!.quota.cycle_index![0].key).toBe('A-cycle');
  });

  it('falls back to All when the focused account vanishes from the cards', () => {
    const s = scopeToAccount(DECORATED, 'codex', 'f'.repeat(32));
    expect(s.accountKey).toBeNull();
    expect(s.data!.periods.daily.rows[0].label).toBe('parent-daily');
  });

  it('reports empty for an account with no observations', () => {
    const s = scopeToAccount(DECORATED, 'codex', EMPTY);
    expect(s.isEmpty).toBe(true);
    expect(s.accountKey).toBe(EMPTY);
  });

  it('treats the unattributed bucket as a first-class scope, not an error', () => {
    const s = scopeToAccount(DECORATED, 'codex', UNATTRIBUTED);
    expect(s.accountKey).toBe(UNATTRIBUTED);
    expect(s.isEmpty).toBe(false);
    expect(s.card!.unattributed).toBe(true);
    expect(s.data!.periods.daily.rows[0].label).toBe('U-daily');
  });

  it('never scopes source `all` or an undecorated source', () => {
    expect(scopeToAccount(DECORATED, 'all', A).accountKey).toBeNull();
    expect(scopeToAccount(UNDECORATED, 'codex', A).accountKey).toBeNull();
  });

  it('does NOT fall back to the parent when a card has no child scope', () => {
    // Contract: every `accounts[]` key resolves to a scope, so a missing child
    // is drift — and showing the PARENT there would attribute every account's
    // numbers to one. Degrade to an explicit empty scope instead.
    const drifted = envelope(codexData({
      accounts: [card({ accountKey: A }), card({ accountKey: B })],
      scopes: { [A]: scope({ marker: 'A' }) },
    }));
    const s = scopeToAccount(drifted, 'codex', B);
    expect(s.accountKey).toBe(B);
    expect(s.isEmpty).toBe(true);
    expect(s.data!.periods.daily.rows).toEqual([]);
    expect(s.data!.sessions.rows).toEqual([]);
    expect(s.data!.quota.cycle_index).toEqual([]);
  });

  it('is not selectable for a residual data-only key with no hero card', () => {
    const s = scopeToAccount(DECORATED, 'codex', RESIDUAL);
    expect(s.accountKey).toBeNull();
    expect(s.data!.periods.daily.rows[0].label).toBe('parent-daily');
  });

  it('derives the focused hero from that account card + its own child', () => {
    const s = scopeToAccount(DECORATED, 'codex', A);
    expect(s.data!.hero.cost_usd).toBe(12.5);
    expect(s.data!.hero.input_tokens).toBe(40);
    expect(s.data!.hero.total_tokens).toBe(100);
    // The account's OWN cycle boundary out of `hero.cycles[]`, not cycles[0].
    expect(s.data!.hero.cycle!.resets_at).toBe('2026-07-28T00:00:00Z');
    const b = scopeToAccount(DECORATED, 'codex', B);
    expect(b.data!.hero.cycle!.resets_at).toBe('2026-07-27T00:00:00Z');
    expect(b.data!.hero.cost_usd).toBe(3.5);
  });

  it('blanks the focused hero cycle for an empty account rather than going stale', () => {
    const s = scopeToAccount(DECORATED, 'codex', EMPTY);
    expect(s.data!.hero.cycle).toBeNull();
    expect(s.data!.hero.cost_usd).toBe(0);
  });

  it('keeps `accounts` and `account_scopes` unscoped so the chip row survives', () => {
    const s = scopeToAccount(DECORATED, 'codex', A);
    expect(s.data!.accounts!.map((c) => c.accountKey)).toEqual([A, B, EMPTY, UNATTRIBUTED]);
    expect(s.data!.hero.cycles!.length).toBe(2);
  });
});

describe('scopeEnvelope', () => {
  it('returns the SAME envelope object identity for All accounts', () => {
    expect(scopeEnvelope(DECORATED, 'codex', ALL_ACCOUNTS)).toBe(DECORATED);
    // #556 S5 §5.4 — All under "All accounts" is still identity-preserving.
    // What CHANGED is that a focused key under All now scopes (below); before
    // S5 the `all` selection returned identity unconditionally.
    expect(scopeEnvelope(DECORATED_ALL, 'all', ALL_ACCOUNTS)).toBe(DECORATED_ALL);
    expect(scopeEnvelope(UNDECORATED, 'codex', A)).toBe(UNDECORATED);
    expect(scopeEnvelope(null, 'codex', A)).toBeNull();
  });

  it('rewrites only sources.codex.data and keeps the entry metadata intact', () => {
    const scoped = scopeEnvelope(DECORATED, 'codex', A)!;
    expect(scoped).not.toBe(DECORATED);
    expect(scoped.sources!.codex!.availability).toBe('ok');
    expect(scoped.sources!.codex!.data_version).toBe('v1');
    expect(scoped.sources!.claude).toBe(DECORATED.sources!.claude);
    const data = scoped.sources!.codex!.data as CodexSourceData;
    expect(data.periods.daily.rows[0].label).toBe('A-daily');
  });

  it('is memoized — repeated calls with the same inputs are reference-stable', () => {
    const first = scopeEnvelope(DECORATED, 'codex', A);
    const second = scopeEnvelope(DECORATED, 'codex', A);
    expect(second).toBe(first);
    expect(scopeEnvelope(DECORATED, 'codex', B)).not.toBe(first);
  });

  // #583 S3 §4 — there is now ONE copy. `sources.all.data.providers` publishes
  // null for both providers, so rewriting it would reconstruct client-side the
  // duplication the wire change removed. Every All panel reads the physical
  // entry through `presentationProviders`' fallback, which is what makes
  // narrowing only `sources.codex.data` sufficient.
  it('rewrites only the physical codex entry under All focus', () => {
    const scoped = scopeEnvelope(DECORATED_ALL, 'all', A)!;
    expect(scoped).not.toBe(DECORATED_ALL);
    const physical = scoped.sources!.codex!.data as CodexSourceData;
    expect(physical).not.toBe(DECORATED_ALL.sources!.codex!.data);
    expect(physical.periods.daily.rows[0].label).toBe('A-daily');
    // The mirror is NOT reconstructed client-side.
    expect((scoped.sources!.all!.data as unknown as {
      providers: unknown;
    }).providers).toEqual({ claude: null, codex: null });
  });

  it('an All Codex focus leaves all.data.combined and the aggregates untouched', () => {
    const scoped = scopeEnvelope(DECORATED_ALL, 'all', A)!;
    const allData = scoped.sources!.all!.data as unknown as {
      combined: unknown; aggregates: unknown;
    };
    // A combined figure is never recomputed from a focused child.
    expect(allData.combined).toBe(COMBINED_MARKER);
    expect(allData.aggregates).toBe(AGGREGATES_MARKER);
    expect(scoped.sources!.all!.availability).toBe('ok');
  });

  it('the All rewrite and the Codex-tab rewrite are memoised separately', () => {
    const underAll = scopeEnvelope(DECORATED_ALL, 'all', A);
    const underTab = scopeEnvelope(DECORATED_ALL, 'codex', A);
    expect(underAll).not.toBe(underTab);
    // #583 S3 §4 — neither view touches the mirror any more; the view stays in
    // the memo key so the two selections keep independent reference identity,
    // which `useSyncExternalStore` requires of every getSnapshot result.
    for (const scoped of [underAll, underTab]) {
      expect((scoped!.sources!.all!.data as unknown as {
        providers: unknown;
      }).providers).toEqual({ claude: null, codex: null });
    }
    expect(scopeEnvelope(DECORATED_ALL, 'all', A)).toBe(underAll);
    expect(scopeEnvelope(DECORATED_ALL, 'codex', A)).toBe(underTab);
  });

  it('identity is preserved when no focus resolves under All', () => {
    // A stale key that names no current account resolves to All accounts.
    expect(scopeEnvelope(DECORATED_ALL, 'all', 'f'.repeat(32))).toBe(DECORATED_ALL);
  });

  it('leaves Claude unscoped — Claude ships no account_scopes', () => {
    const claudeEnv = {
      sources: {
        claude: {
          availability: 'ok', freshness: 'fresh', warnings: [], data_version: 'v1',
          last_success_at: null, capabilities: {},
          data: { accounts: [card({ accountKey: A }), card({ accountKey: B })] },
        },
        codex: null,
        all: null,
      },
    } as unknown as Envelope;
    expect(scopeEnvelope(claudeEnv, 'claude', A)).toBe(claudeEnv);
    expect(scopeToAccount(claudeEnv, 'claude', A).scopesSupported).toBe(false);
    expect(scopeToAccount(DECORATED, 'codex', A).scopesSupported).toBe(true);
  });
});
