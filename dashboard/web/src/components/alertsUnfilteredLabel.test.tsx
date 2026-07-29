// #341 Task 4 (Decision R4) → #416 Task 15. Alert rows now carry `account_key`
// and the panel scopes to the focused account, so the "all accounts
// (unfiltered)" note survives only where a focus genuinely cannot be applied:
// an envelope with account cards but NO `account_scopes` (Claude today, and any
// Codex envelope from a pre-#416 server). This file pins that degrade path; the
// scoped path — note gone, `alerts-account-note` in its place, vendor-wide rows
// labelled — is pinned in `panels/accountScopedPanels.test.tsx`.
//
// The fixture below deliberately has cards without scopes. Do NOT "fix" it by
// adding `account_scopes`: that would silently retarget these assertions at a
// path the code no longer takes.
import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { RecentAlertsPanel } from './RecentAlertsPanel';
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

describe('Alerts R4 unfiltered label', () => {
  it('shows the unfiltered note when an account is focused', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: A });
    render(<RecentAlertsPanel />);
    expect(screen.getByTestId('alerts-unfiltered-note')).toBeTruthy();
  });

  it('hides the note when focus is All accounts', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<RecentAlertsPanel />);
    expect(container.querySelector('[data-testid="alerts-unfiltered-note"]')).toBeNull();
  });

  it('hides the note on an undecorated source (no chip row at all)', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<RecentAlertsPanel />);
    expect(container.querySelector('[data-testid="alerts-unfiltered-note"]')).toBeNull();
  });
});
