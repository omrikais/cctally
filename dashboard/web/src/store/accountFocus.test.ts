import { beforeEach, describe, expect, it } from 'vitest';
import {
  ACCOUNT_STORAGE_PREFIX,
  ALL_ACCOUNTS,
  loadAccountFocus,
  nextAccountFocus,
  resolveAccountFocus,
  saveAccountFocus,
  seedAccountFocus,
  sourceAccounts,
  sourceIsDecorated,
} from './accountFocus';
import type { AccountCard, Envelope, SourceEntry } from '../types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

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
    inputTokens: 0,
    cachedInputTokens: 0,
    outputTokens: 0,
    reasoningOutputTokens: 0,
    totalTokens: 0,
    unattributed: over.unattributed,
  };
}

function envWith(accounts: AccountCard[] | null): Envelope {
  const codex = {
    availability: 'ok',
    freshness: 'fresh',
    warnings: [],
    data_version: 'v1',
    last_success_at: null,
    capabilities: {},
    data: accounts == null ? {} : { accounts },
  } as unknown as SourceEntry<unknown>;
  return { sources: { claude: null, codex, all: null } } as unknown as Envelope;
}

beforeEach(() => localStorage.clear());

describe('accountFocus persistence (cctally:dashboard:account:<source>)', () => {
  it('defaults to All when absent, round-trips a stored key', () => {
    expect(loadAccountFocus('codex')).toBe(ALL_ACCOUNTS);
    saveAccountFocus('codex', A);
    expect(localStorage.getItem(`${ACCOUNT_STORAGE_PREFIX}codex`)).toBe(A);
    expect(loadAccountFocus('codex')).toBe(A);
  });

  it('seeds both persisted sources', () => {
    saveAccountFocus('claude', B);
    const seed = seedAccountFocus();
    expect(seed.claude).toBe(B);
    expect(seed.codex).toBe(ALL_ACCOUNTS);
  });
});

describe('sourceAccounts / sourceIsDecorated', () => {
  it('reads the accounts array off a decorated entry, null otherwise', () => {
    const env = envWith([card({ accountKey: A }), card({ accountKey: B })]);
    expect(sourceAccounts(env.sources!.codex)!.length).toBe(2);
    expect(sourceIsDecorated(env, 'codex')).toBe(true);
    // Undecorated: no accounts array → null / not decorated.
    expect(sourceIsDecorated(envWith(null), 'codex')).toBe(false);
    // Source `all` never has a selector.
    expect(sourceIsDecorated(env, 'all')).toBe(false);
  });
});

describe('resolveAccountFocus (stored-valid-else-All reconciliation)', () => {
  it('null for All / undecorated; the key when present; All when vanished', () => {
    const env = envWith([card({ accountKey: A }), card({ accountKey: B })]);
    expect(resolveAccountFocus(env, 'codex', ALL_ACCOUNTS)).toBeNull();
    expect(resolveAccountFocus(env, 'codex', A)).toBe(A);
    // A stored key not in the current envelope resets to All (no mutation).
    expect(resolveAccountFocus(env, 'codex', 'c'.repeat(32))).toBeNull();
    // Undecorated source always resolves to All.
    expect(resolveAccountFocus(envWith(null), 'codex', A)).toBeNull();
  });
});

describe('nextAccountFocus cycle order (All → a → b → All)', () => {
  it('cycles through the accounts and wraps back to All', () => {
    const accounts = [card({ accountKey: A }), card({ accountKey: B })];
    expect(nextAccountFocus(accounts, null)).toBe(A);
    expect(nextAccountFocus(accounts, A)).toBe(B);
    expect(nextAccountFocus(accounts, B)).toBe(ALL_ACCOUNTS);
    // No accounts → always All (a no-op cycle).
    expect(nextAccountFocus(null, A)).toBe(ALL_ACCOUNTS);
  });
});
