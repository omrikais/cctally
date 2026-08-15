import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ACCOUNT_STORAGE_PREFIX,
  ALL_ACCOUNTS,
  decoratedProvidersFor,
  focusSlotFor,
  loadAccountFocus,
  nextAccountFocus,
  providerIsDecorated,
  resolveAccountFocus,
  resolveViewAccountFocus,
  saveAccountFocus,
  seedAccountFocus,
  shortAccountLabel,
  sourceAccounts,
  sourceIsDecorated,
  storedFocusFor,
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

describe('shortAccountLabel', () => {
  it('preserves short labels and both ends of long labels', () => {
    expect(shortAccountLabel('work')).toBe('work');
    expect(shortAccountLabel('client-acme-consulting-longname'))
      .toBe('client-acme-…longname');
  });
});

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
    expect(seed.provider.claude).toBe(B);
    expect(seed.provider.codex).toBe(ALL_ACCOUNTS);
  });
});

// #556 S5 §5.1/§5.2 — the two slots and their isolation.
describe('#556 S5 — the All focus slot', () => {
  it('an All focus does not write the provider slot', () => {
    saveAccountFocus('codex', A, 'all');
    expect(localStorage.getItem(`${ACCOUNT_STORAGE_PREFIX}all:codex`)).toBe(A);
    expect(localStorage.getItem(`${ACCOUNT_STORAGE_PREFIX}codex`)).toBeNull();
    const seed = seedAccountFocus();
    expect(seed.all.codex).toBe(A);
    expect(seed.provider.codex).toBe(ALL_ACCOUNTS);
  });

  it('a provider focus does not write the All slot', () => {
    saveAccountFocus('codex', B, 'provider');
    expect(localStorage.getItem(`${ACCOUNT_STORAGE_PREFIX}all:codex`)).toBeNull();
    const seed = seedAccountFocus();
    expect(seed.provider.codex).toBe(B);
    expect(seed.all.codex).toBe(ALL_ACCOUNTS);
  });

  it('focusSlotFor maps the view, not the provider', () => {
    expect(focusSlotFor('all')).toBe('all');
    expect(focusSlotFor('codex')).toBe('provider');
    expect(focusSlotFor('claude')).toBe('provider');
  });

  it('storedFocusFor reads the view slot and never a provider the view is not showing', () => {
    const focus = {
      provider: { claude: ALL_ACCOUNTS, codex: A },
      all: { claude: B, codex: B },
    };
    expect(storedFocusFor(focus, 'codex', 'codex')).toBe(A);
    expect(storedFocusFor(focus, 'all', 'codex')).toBe(B);
    // The Claude tab must not read the Codex slot at all.
    expect(storedFocusFor(focus, 'claude', 'codex')).toBe(ALL_ACCOUNTS);
  });
});

describe('#556 S5 — resolveViewAccountFocus (the one shared resolver)', () => {
  const env = envWith([card({ accountKey: A }), card({ accountKey: B })]);
  const focus = {
    provider: { claude: ALL_ACCOUNTS, codex: A },
    all: { claude: ALL_ACCOUNTS, codex: B },
  };

  it('selects the slot from the view and reconciles against the provider', () => {
    expect(resolveViewAccountFocus(env, 'codex', 'codex', focus)).toBe(A);
    expect(resolveViewAccountFocus(env, 'all', 'codex', focus)).toBe(B);
  });

  it('returns null for a provider the view does not show', () => {
    expect(resolveViewAccountFocus(env, 'claude', 'codex', focus)).toBeNull();
  });

  it('a vanished account resolves to All accounts without mutating storage', () => {
    const vanished = {
      provider: { claude: ALL_ACCOUNTS, codex: 'c'.repeat(32) },
      all: { claude: ALL_ACCOUNTS, codex: 'c'.repeat(32) },
    };
    const spy = vi.spyOn(Storage.prototype, 'setItem');
    expect(resolveViewAccountFocus(env, 'codex', 'codex', vanished)).toBeNull();
    expect(resolveViewAccountFocus(env, 'all', 'codex', vanished)).toBeNull();
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});

describe('#556 S5 — per-provider decoration under All', () => {
  it('names every decorated provider for All, and only the tab provider otherwise', () => {
    const env = envWith([card({ accountKey: A }), card({ accountKey: B })]);
    expect(providerIsDecorated(env, 'codex')).toBe(true);
    expect(providerIsDecorated(env, 'claude')).toBe(false);
    // `sourceIsDecorated` keeps its older, view-shaped answer for `all`.
    expect(sourceIsDecorated(env, 'all')).toBe(false);
    expect(decoratedProvidersFor(env, 'all')).toEqual(['codex']);
    expect(decoratedProvidersFor(env, 'codex')).toEqual(['codex']);
    expect(decoratedProvidersFor(env, 'claude')).toEqual([]);
    expect(decoratedProvidersFor(envWith(null), 'all')).toEqual([]);
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
