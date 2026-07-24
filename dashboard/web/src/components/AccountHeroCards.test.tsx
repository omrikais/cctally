import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { AccountHeroCards } from './AccountHeroCards';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { AccountCard, Envelope } from '../types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function card(over: Partial<AccountCard> & { accountKey: string; label: string }): AccountCard {
  return {
    accountKey: over.accountKey, label: over.label, plan: over.plan ?? 'pro',
    active: over.active ?? false, weeklyPercent: over.weeklyPercent ?? null,
    fiveHourPercent: over.fiveHourPercent ?? null, resetsAt: over.resetsAt ?? null,
    spendUsd: over.spendUsd ?? 0, inputTokens: 0, cachedInputTokens: 0,
    outputTokens: 0, reasoningOutputTokens: 0, totalTokens: 0,
    unattributed: over.unattributed,
  };
}

function decoratedEnv(accounts: AccountCard[]): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { codex: { data: { accounts?: AccountCard[] } } };
  };
  slice.sources.codex.data.accounts = accounts;
  return slice as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
});

describe('AccountHeroCards (unified per-account view)', () => {
  it('renders nothing on an undecorated source', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountHeroCards />);
    expect(container.querySelector('[data-testid="account-hero-cards"]')).toBeNull();
  });

  it('All accounts → one card per account with bars + spend', () => {
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, fiveHourPercent: 12, spendUsd: 1.5, active: true }),
      card({ accountKey: B, label: 'bob', weeklyPercent: 55, fiveHourPercent: 30, spendUsd: 2.25 }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountHeroCards />);
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards.map((c) => c.getAttribute('data-account'))).toEqual([A, B]);
    expect(screen.getByText('alice')).toBeTruthy();
    expect(screen.getByText('$1.50')).toBeTruthy();
    expect(screen.getByText('$2.25')).toBeTruthy();
  });

  it('a focused chip narrows to that one card', () => {
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1 }),
      card({ accountKey: B, label: 'bob', weeklyPercent: 55, spendUsd: 2 }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: B });
    render(<AccountHeroCards />);
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards.length).toBe(1);
    expect(cards[0].getAttribute('data-account')).toBe(B);
    expect(cards[0].className).toContain('is-focused');
  });

  it('the unattributed card is dimmed with no bars (totals only)', () => {
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1 }),
      { ...card({ accountKey: 'unattributed', label: 'Unattributed', spendUsd: 0.5 }), unattributed: true },
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountHeroCards />);
    const unattr = screen.getByTestId('account-hero-cards')
      .querySelector('[data-account="unattributed"]') as HTMLElement;
    expect(unattr.className).toContain('is-dimmed');
    expect(unattr.querySelector('.account-hero-card-bars')).toBeNull();
    expect(unattr.textContent).toContain('totals only');
  });
});

// #341 ui-qa P3 (copy) — a reset at/after its boundary clamps to 0s and used to
// render the contradictory "resets in 0s ago". The at/past-boundary case must
// read "resets now"; the future path is untouched.
describe('AccountHeroCards — reset countdown copy (ui-qa P3)', () => {
  it('renders "resets now" (not "resets in 0s ago") at/after the reset boundary', () => {
    const past = new Date(Date.now() - 5000).toISOString(); // 5s past the reset
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1, resetsAt: past }),
      card({ accountKey: B, label: 'bob', weeklyPercent: 55, spendUsd: 2, resetsAt: past }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountHeroCards />);
    const resets = [...container.querySelectorAll('.account-hero-card-reset')];
    expect(resets.length).toBe(2);
    resets.forEach((el) => {
      expect(el.textContent).toBe('resets now');
      expect(el.textContent).not.toMatch(/ago/);
    });
  });

  it('leaves the future reset countdown unchanged ("resets in …")', () => {
    const future = new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(); // 2h ahead
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1, resetsAt: future }),
      card({ accountKey: B, label: 'bob', weeklyPercent: 55, spendUsd: 2, resetsAt: future }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountHeroCards />);
    const resets = [...container.querySelectorAll('.account-hero-card-reset')];
    expect(resets.length).toBe(2);
    resets.forEach((el) => {
      expect(el.textContent).toMatch(/^resets in /);
      expect(el.textContent).not.toBe('resets now');
    });
  });
});
