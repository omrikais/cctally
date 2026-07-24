// #341 Task 4 (Ruling C) — the generic account chip row + hero cards are
// provider-neutral, so a DECORATED Claude source (`data.accounts[]` emitted by
// the Python `_claude_accounts_wire`) lights them up exactly like Codex. This
// proves the symmetry: the same components that render Codex accounts render
// Claude accounts, and an undecorated Claude source stays absent (R8).
import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { AccountChipRow } from './AccountChipRow';
import { AccountHeroCards } from './AccountHeroCards';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { AccountCard, Envelope } from '../types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function card(over: Partial<AccountCard> & { accountKey: string; label: string }): AccountCard {
  return {
    accountKey: over.accountKey, label: over.label, plan: over.plan ?? 'max',
    active: over.active ?? false, weeklyPercent: over.weeklyPercent ?? null,
    fiveHourPercent: over.fiveHourPercent ?? null, resetsAt: over.resetsAt ?? null,
    spendUsd: over.spendUsd ?? 0, inputTokens: 0, cachedInputTokens: 0,
    outputTokens: 0, reasoningOutputTokens: 0, totalTokens: 0,
    unattributed: over.unattributed,
  };
}

// Attach `data.accounts[]` to the CLAUDE source entry (symmetric with the Codex
// helper in AccountChipRow.test.tsx / AccountHeroCards.test.tsx).
function decoratedClaudeEnv(accounts: AccountCard[]): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { claude: { data: { accounts?: AccountCard[] } } };
  };
  slice.sources.claude.data.accounts = accounts;
  return slice as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
});

describe('decorated Claude source lights up the generic account UI (Ruling C)', () => {
  it('chip row: undecorated Claude renders nothing (R8 byte-stable)', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<AccountChipRow />);
    expect(container.querySelector('[data-testid="account-chip-row"]')).toBeNull();
  });

  it('chip row: decorated Claude renders the radiogroup + one chip per account', () => {
    updateSnapshot(decoratedClaudeEnv([
      card({ accountKey: A, label: 'work', weeklyPercent: 42, active: true }),
      card({ accountKey: B, label: 'personal', weeklyPercent: 8 }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    render(<AccountChipRow />);
    expect(screen.getByRole('radiogroup', { name: 'Account focus' })).toBeTruthy();
    const radios = screen.getAllByRole('radio');
    expect(radios.map((r) => r.textContent)).toEqual(['All accounts', 'work42%', 'personal8%']);
  });

  it('hero cards: decorated Claude renders one per-account card with spend', () => {
    updateSnapshot(decoratedClaudeEnv([
      card({ accountKey: A, label: 'work', weeklyPercent: 42, fiveHourPercent: 60, spendUsd: 12.5, active: true }),
      card({ accountKey: B, label: 'personal', weeklyPercent: 8, fiveHourPercent: 3, spendUsd: 1.25 }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    render(<AccountHeroCards />);
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards.map((c) => c.getAttribute('data-account'))).toEqual([A, B]);
    expect(screen.getByText('work')).toBeTruthy();
    expect(screen.getByText('$12.50')).toBeTruthy();
    expect(screen.getByText('$1.25')).toBeTruthy();
  });

  it('hero cards: undecorated Claude renders nothing', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<AccountHeroCards />);
    expect(container.querySelector('[data-testid="account-hero-cards"]')).toBeNull();
  });
});
