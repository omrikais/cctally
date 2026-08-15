import { beforeEach, describe, expect, it, vi } from 'vitest';
import { _resetForTests, dispatch, getState, updateSnapshot } from './store';
import { cycleActiveAccount } from './globalBindings';
import { ACCOUNT_STORAGE_PREFIX, ALL_ACCOUNTS } from './accountFocus';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { AccountCard, Envelope } from '../types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function card(accountKey: string, weeklyPercent: number): AccountCard {
  return {
    accountKey, label: accountKey.slice(0, 4), plan: 'pro', active: false,
    weeklyPercent, fiveHourPercent: null, resetsAt: null, spendUsd: 0,
    inputTokens: 0, cachedInputTokens: 0, outputTokens: 0,
    reasoningOutputTokens: 0, totalTokens: 0,
  };
}

// A Codex source decorated with two accounts, selected active.
function decoratedCodexEnv(): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { codex: { data: { accounts?: AccountCard[] } } };
  };
  slice.sources.codex.data.accounts = [card(A, 40), card(B, 55)];
  return slice as unknown as Envelope;
}

// Claude decorated, Codex not — the state that makes `a` cycle a Claude row.
function decoratedClaudeOnlyEnv(): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { claude: { data: { accounts?: AccountCard[] } } };
  };
  slice.sources.claude.data.accounts = [card(A, 40), card(B, 55)];
  return slice as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('SET_ACCOUNT_FOCUS reducer', () => {
  it('sets the per-source slot and persists a bare literal', () => {
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: A });
    expect(getState().accountFocus.provider.codex).toBe(A);
    expect(localStorage.getItem(`${ACCOUNT_STORAGE_PREFIX}codex`)).toBe(A);
    // Per-source: claude is untouched.
    expect(getState().accountFocus.provider.claude).toBe(ALL_ACCOUNTS);
  });

  it('same-value dispatch is a no-op (no write, identity preserved)', () => {
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: A });
    const before = getState();
    const spy = vi.spyOn(Storage.prototype, 'setItem');
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: A });
    expect(spy).not.toHaveBeenCalled();
    expect(getState()).toBe(before);
    spy.mockRestore();
  });
});

describe('cycleActiveAccount (the `a` key)', () => {
  it('cycles All → a → b → All on a decorated source', () => {
    updateSnapshot(decoratedCodexEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    cycleActiveAccount();
    expect(getState().accountFocus.provider.codex).toBe(A);
    cycleActiveAccount();
    expect(getState().accountFocus.provider.codex).toBe(B);
    cycleActiveAccount();
    expect(getState().accountFocus.provider.codex).toBe(ALL_ACCOUNTS);
  });

  it('is a no-op on an undecorated source (single-account inert)', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope); // no accounts[]
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const before = getState();
    cycleActiveAccount();
    expect(getState()).toBe(before);
  });

  // #556 S5 §5.6 REPLACES the pre-S5 contract asserted here, which was "is a
  // no-op under source `all` (no selector)". All has a selector now, so the
  // shortcut must reach it; the assertion is rewritten rather than deleted so
  // the reversal is visible in the estate.
  it('cycles the CODEX row under source `all`, writing only the All slot', () => {
    updateSnapshot(decoratedCodexEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    cycleActiveAccount();
    expect(getState().accountFocus.all.codex).toBe(A);
    // The Codex TAB slot is untouched — the two never write to each other.
    expect(getState().accountFocus.provider.codex).toBe(ALL_ACCOUNTS);
    cycleActiveAccount();
    expect(getState().accountFocus.all.codex).toBe(B);
    cycleActiveAccount();
    expect(getState().accountFocus.all.codex).toBe(ALL_ACCOUNTS);
  });

  it('cycles the lone CLAUDE row under `all` when Codex is undecorated', () => {
    updateSnapshot(decoratedClaudeOnlyEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    cycleActiveAccount();
    // The shortcut is never inert beside a visible control.
    expect(getState().accountFocus.all.claude).toBe(A);
    expect(getState().accountFocus.all.codex).toBe(ALL_ACCOUNTS);
  });

  it('is still a no-op under `all` when neither provider is decorated', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const before = getState();
    cycleActiveAccount();
    expect(getState()).toBe(before);
  });
});

describe('share capture (source, account) at OPEN_SHARE', () => {
  it('stamps the focused account and is immune to a mid-flow switch', () => {
    updateSnapshot(decoratedCodexEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: B });
    dispatch({ type: 'OPEN_SHARE', panel: 'trend', triggerId: null });
    expect(getState().shareModal?.source).toBe('codex');
    expect(getState().shareModal?.account).toBe(B);
    // A mid-flow account switch must NOT restamp the captured account.
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: A });
    expect(getState().shareModal?.account).toBe(B);
  });

  it('captures null (All) when no account is focused', () => {
    updateSnapshot(decoratedCodexEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'OPEN_SHARE', panel: 'trend', triggerId: null });
    expect(getState().shareModal?.account).toBeNull();
  });

  // #556 S5 §5.12 — All no longer forcibly captures NO account. A shared
  // artifact taken under a focused chip must match the view that produced it;
  // exporting all-account data from a focused view is classified as a privacy
  // problem in this project, not a follow-up.
  it('captures the All slot\u2019s Codex focus when the share is taken under All', () => {
    updateSnapshot(decoratedCodexEnv());
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: B });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_SHARE', panel: 'trend', triggerId: null });
    expect(getState().shareModal?.source).toBe('all');
    expect(getState().shareModal?.account).toBe(B);
  });

  it('an All share reads the All slot, never the provider tab slot', () => {
    updateSnapshot(decoratedCodexEnv());
    // Focused on the CODEX TAB only. All must not inherit it.
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: A });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_SHARE', panel: 'trend', triggerId: null });
    expect(getState().shareModal?.account).toBeNull();
  });

  it('a share captured with a since-vanished account resolves to All (null)', () => {
    updateSnapshot(decoratedCodexEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // Persist a focus, then deliver an envelope WITHOUT that account.
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: B });
    const gone = makeSourceEnvelope() as unknown as {
      sources: { codex: { data: { accounts?: AccountCard[] } } };
    };
    gone.sources.codex.data.accounts = [card(A, 40)]; // B vanished
    updateSnapshot(gone as unknown as Envelope);
    dispatch({ type: 'OPEN_SHARE', panel: 'trend', triggerId: null });
    expect(getState().shareModal?.account).toBeNull();
  });
});
