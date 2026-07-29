// #341 Task 4 (Decision R4) → #416 Task 15. Sessions DO scope by account now,
// so the "all accounts (unfiltered)" note survives only where a focus genuinely
// cannot be applied: an envelope that ships account cards but NO
// `account_scopes` (Claude today, and any Codex envelope from a server that
// pre-dates #416). This file pins that degrade path; the scoped path — where
// the note is gone and replaced by `sessions-account-note` — is pinned in
// `accountScopedPanels.test.tsx`.
//
// The fixture below deliberately has cards without scopes, which is exactly the
// unscopeable shape. Do NOT "fix" it by adding `account_scopes`: that would
// silently turn these into assertions about a path the code no longer takes.
import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { AccountCard, Envelope } from '../types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function card(accountKey: string, label: string): AccountCard {
  return {
    accountKey, label, plan: 'pro', active: false, weeklyPercent: 10,
    fiveHourPercent: null, resetsAt: null, spendUsd: 0, inputTokens: 0,
    cachedInputTokens: 0, outputTokens: 0, reasoningOutputTokens: 0, totalTokens: 0,
  };
}

function decoratedEnv(): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { codex: { data: { accounts?: AccountCard[] } } };
  };
  slice.sources.codex.data.accounts = [card(A, 'work'), card(B, 'personal')];
  return slice as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
});

describe('Sessions R4 unfiltered label', () => {
  it('shows the unfiltered note when an account is focused', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: A });
    render(<SessionsPanel />);
    expect(screen.getByTestId('sessions-unfiltered-note')).toBeTruthy();
  });

  it('hides the note when focus is All accounts', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('[data-testid="sessions-unfiltered-note"]')).toBeNull();
  });

  it('hides the note on an undecorated source (no chip row at all)', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('[data-testid="sessions-unfiltered-note"]')).toBeNull();
  });
});
