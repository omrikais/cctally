import { beforeEach, describe, expect, it } from 'vitest';
import { act, render, screen, cleanup, fireEvent } from '@testing-library/react';
import { AccountHeroCards } from './AccountHeroCards';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import { fmt } from '../lib/fmt';
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
    ...(over.spendWindow ? { spendWindow: over.spendWindow } : {}),
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
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: B });
    render(<AccountHeroCards />);
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards.length).toBe(1);
    expect(cards[0].getAttribute('data-account')).toBe(B);
    expect(cards[0].className).toContain('is-focused');
  });

  it('discloses stale quota evidence on only the account that owns it', () => {
    updateSnapshot(decoratedEnv([
      { ...card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1 }), cycleFreshness: 'stale' as const },
      card({ accountKey: B, label: 'bob', weeklyPercent: 55, spendUsd: 2 }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountHeroCards />);
    const cards = screen.getAllByTestId('account-hero-card');
    const alice = cards.find((item) => item.getAttribute('data-account') === A);
    const bob = cards.find((item) => item.getAttribute('data-account') === B);
    expect(alice?.querySelector('[data-testid="account-cycle-stale"]')).toHaveTextContent('stale');
    expect(bob?.querySelector('[data-testid="account-cycle-stale"]')).toBeNull();
    expect(alice).toHaveTextContent('$1.00');
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

describe('AccountHeroCards — selection scroll position (#897)', () => {
  function renderAccounts() {
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', spendUsd: 1 }),
      card({ accountKey: B, label: 'bob', spendUsd: 2 }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountHeroCards />);
  }

  it('starts All accounts at the first card after focusing the second account', () => {
    renderAccounts();
    act(() => dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: B }));
    // Chromium preserves the previously snapped card when its predecessor
    // returns. Retain that offset while changing the visible account set.
    screen.getByTestId('account-hero-cards').scrollLeft = 111;
    act(() => dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: 'all' }));
    expect(screen.getByTestId('account-hero-cards').scrollLeft).toBe(0);
    expect(screen.getAllByTestId('account-hero-card')[0]).toHaveAttribute('data-account', A);
  });

  it('starts a newly selected source at the first account card', () => {
    renderAccounts();
    screen.getByTestId('account-hero-cards').scrollLeft = 111;
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' }));
    expect(screen.getByTestId('account-hero-cards').scrollLeft).toBe(0);
  });

  it('preserves the user scroll position on routine envelope updates', () => {
    renderAccounts();
    screen.getByTestId('account-hero-cards').scrollLeft = 55;
    act(() => updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', spendUsd: 3 }),
      card({ accountKey: B, label: 'bob', spendUsd: 4 }),
    ])));
    expect(screen.getByTestId('account-hero-cards').scrollLeft).toBe(55);
    expect(screen.getByText('$4.00')).toBeInTheDocument();
  });

  it('shows the cue at the start and mid-scroll, and hides it at the end', () => {
    renderAccounts();
    const rail = screen.getByTestId('account-hero-cards');
    Object.defineProperties(rail, {
      scrollWidth: { value: 500 },
      clientWidth: { value: 390 },
    });
    fireEvent.scroll(rail);
    expect(screen.getByTestId('account-hero-scroll-cue')).toHaveAttribute('aria-hidden', 'false');
    rail.scrollLeft = 55;
    fireEvent.scroll(rail);
    expect(screen.getByTestId('account-hero-scroll-cue')).toHaveAttribute('aria-hidden', 'false');
    rail.scrollLeft = 110;
    fireEvent.scroll(rail);
    expect(screen.getByTestId('account-hero-scroll-cue')).toHaveAttribute('aria-hidden', 'true');
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

  // #416 QA P2-A. The guard above anchored only the PREFIX (`/^resets in /`),
  // which passes for "resets in 2d 2h ago" — and that is exactly what every
  // future reset rendered, because `humanizeAge` appends " ago"
  // unconditionally and only the `secs === 0` case was special-cased. The
  // assertion is now on the whole string.
  it.each([
    [2 * 60 * 60, 'resets in 2h'],
    [(2 * 24 + 2) * 60 * 60, 'resets in 2d 2h'],
    [45, 'resets in 45s'],
    [90 * 60, 'resets in 1h 30m'],
  ])('renders a future countdown as a duration, never an age (+%is)', (secs, expected) => {
    const future = new Date(Date.now() + secs * 1000).toISOString();
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1, resetsAt: future }),
      card({ accountKey: B, label: 'bob', weeklyPercent: 55, spendUsd: 2, resetsAt: future }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountHeroCards />);
    const resets = [...container.querySelectorAll('.account-hero-card-reset')];
    expect(resets.length).toBe(2);
    resets.forEach((el) => {
      expect(el.textContent).toBe(expected);
      expect(el.textContent).not.toMatch(/ago/);
      expect(el.textContent).not.toBe('resets now');
    });
  });
});

// #564 — a card totalled over the bounded fallback window says which window it
// covers, in the slot a cycle card uses for its reset countdown. The caption is
// derived from the published bounds, never from a hardcoded seven days, because
// the server clamps the window to the accounting range it actually loaded.
describe('#564 — a fallback card names the window its total covers', () => {
  const WEEK = {
    kind: 'trailing-cycle' as const,
    startAt: '2026-04-17T13:00:00Z',
    endAt: '2026-04-24T13:00:00Z',
  };

  it('captions a full-width window with the operator wording', () => {
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1.5,
        resetsAt: '2026-04-30T00:00:00Z' }),
      card({ accountKey: B, label: 'bob', spendUsd: 2.25, spendWindow: WEEK }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountHeroCards />);
    const captions = [...container.querySelectorAll('[data-testid="account-card-window"]')];
    expect(captions.length).toBe(1);
    expect(captions[0].textContent).toBe('last 7 days');
    // The caption belongs to the fallback card, not to the cycle-bounded one.
    expect(captions[0].closest('[data-account]')?.getAttribute('data-account')).toBe(B);
  });

  it('names the true span when the window is clamped shorter', () => {
    updateSnapshot(decoratedEnv([
      card({ accountKey: A, label: 'alice', weeklyPercent: 40, spendUsd: 1.5,
        resetsAt: '2026-04-30T00:00:00Z' }),
      card({
        accountKey: B, label: 'bob', spendUsd: 2.25,
        spendWindow: {
          kind: 'trailing-cycle',
          startAt: '2026-04-21T13:00:00Z',
          endAt: '2026-04-24T13:00:00Z',
        },
      }),
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountHeroCards />);
    const caption = container.querySelector('[data-testid="account-card-window"]');
    expect(caption?.textContent).toBe('last 3 days');
  });
});

// #416 QA P2-B — `SPENT THIS WEEK` under a focused account with a sub-dollar
// spend. The reported symptom was `$0` sitting directly above a card reading
// `$0.23`. The value is NOT a real zero: it is the card's own range total.
// Blanking it would hide real data; the shared formatter now preserves every
// known fractional dollar amount at both hero call sites.
describe('fmt.usd0 — a real sub-dollar spend never reads as nothing', () => {
  it('keeps cents when whole dollars would round a real spend to $0', () => {
    expect(fmt.usd0(0.23)).toBe('$0.23');
    expect(fmt.usd0(0.4)).toBe('$0.40');
    // Below a cent, the honest form is the convention, not a rounded-up figure.
    expect(fmt.usd0(0.004)).toBe('<$0.01');
    expect(fmt.usd0(-0.23)).toBe('−$0.23');
  });

  it('keeps exact dollars compact and preserves known fractional dollars', () => {
    expect(fmt.usd0(254)).toBe('$254');
    expect(fmt.usd0(254.27)).toBe('$254.27');
    expect(fmt.usd0(254.6)).toBe('$254.60');
    expect(fmt.usd0(5.02)).toBe('$5.02');
    expect(fmt.usd0(1)).toBe('$1');
    expect(fmt.usd0(0.999)).toBe('$1.00');
    expect(fmt.usd0(0.5)).toBe('$0.50');
    expect(fmt.usd0(0)).toBe('$0');
    expect(fmt.usd0(null)).toBe('—');
  });
});
