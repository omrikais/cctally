// #341 Task 4 (Ruling C) — the generic account chip row + hero cards are
// provider-neutral, so a DECORATED Claude source (`data.accounts[]` emitted by
// the Python `_claude_accounts_wire`) lights them up exactly like Codex. This
// proves the symmetry: the same components that render Codex accounts render
// Claude accounts, and an undecorated Claude source stays absent (R8).
import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { AccountChipRow } from './AccountChipRow';
import { AccountHeroCards } from './AccountHeroCards';
import { HeroStrip } from './HeroStrip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  CLAUDE_ACCOUNT_ALT,
  makeAllSourceEntry,
  makeDecoratedClaudeSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
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

function decoratedClaudeHeroEnv(): Envelope {
  const slice = makeSourceEnvelope();
  const claude = {
    ...slice.sources.claude,
    data: makeDecoratedClaudeSourceData(),
  };
  return {
    header: {
      week_label: 'Jul 24–Jul 31',
      used_pct: 64,
      five_hour_pct: 37,
      dollar_per_pct: 1.38,
      forecast_pct: 95,
      forecast_verdict: 'cap',
      vs_last_week_delta: null,
    },
    current_week: {
      used_pct: 64,
      five_hour_pct: 37,
      five_hour_resets_in_sec: null,
      spent_usd: 88.2,
      dollar_per_pct: 1.38,
      reset_at_utc: '2026-04-30T00:00:00Z',
      reset_in_sec: 216000,
      last_snapshot_age_sec: 30,
      milestones: [],
      freshness: {
        label: 'fresh',
        captured_at: '2026-04-24T13:00:00Z',
        age_seconds: 30,
      },
      five_hour_block: null,
    },
    ...slice,
    sources: {
      ...slice.sources,
      claude,
      all: makeAllSourceEntry(claude, slice.sources.codex),
    },
  } as unknown as Envelope;
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

describe('decorated Claude hero disclosure (#423 items 15 and 21)', () => {
  it('blanks merged quota values and points to the per-account strip', () => {
    updateSnapshot(decoratedClaudeHeroEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<HeroStrip />);

    const hero = container.querySelector('.hero-strip')!;
    expect(hero).not.toHaveTextContent('64.0%');
    expect(hero).not.toHaveTextContent('5-HOUR37%');
    expect(hero).not.toHaveTextContent('Forecast @ reset95%');
    expect(hero).toHaveTextContent('per account');
    expect(hero).toHaveTextContent('SPENT THIS WEEK$107.60');
    expect(screen.getAllByTestId('account-hero-card')).toHaveLength(2);
  });

  it('uses the focused Claude card without borrowing the merged forecast', () => {
    updateSnapshot(decoratedClaudeHeroEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'provider', account: CLAUDE_ACCOUNT_ALT,
    });
    const { container } = render(<HeroStrip />);

    const hero = container.querySelector('.hero-strip')!;
    expect(hero).toHaveTextContent('22.0%');
    expect(hero).toHaveTextContent('5-HOUR8%');
    expect(hero).toHaveTextContent('SPENT THIS WEEK$19.40');
    expect(hero).toHaveTextContent('— / 1% used');
    expect(hero).not.toHaveTextContent('$0.88 / 1% used');
    expect(hero).not.toHaveTextContent('64.0%');
    expect(hero).not.toHaveTextContent('5-HOUR37%');
    expect(hero).not.toHaveTextContent('Forecast @ reset95%');
  });

  // #556 S5 round-2 browser gate, P1 — the provider tab half of the same
  // defect, which predates S5 (the expression is byte-identical at the merge
  // base). Spec §5.9 requires All to MATCH the tab, so the two are corrected
  // together: a focused account with no published weekly percentage withholds
  // the headline instead of borrowing `header.used_pct`.
  it('withholds the tab headline when the focused card publishes no weekly percent', () => {
    const env = decoratedClaudeHeroEnv() as unknown as {
      sources: { claude: { data: { accounts: AccountCard[] } } };
    };
    env.sources.claude.data = {
      ...env.sources.claude.data,
      accounts: env.sources.claude.data.accounts.map((c) => ({
        ...c, weeklyPercent: null, fiveHourPercent: null, resetsAt: null,
      })),
    };
    updateSnapshot(env as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({
      type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'provider', account: CLAUDE_ACCOUNT_ALT,
    });
    const { container } = render(<HeroStrip />);

    const num = container.querySelector('.hero-usage .hu-num')!;
    // 64 is BOTH the merged `header.used_pct` and the main account's own
    // figure, so nulling the cards leaves the header as its only source.
    expect(num.textContent).toBe('—');
    expect(num.className).toContain('is-blank');
    expect(container.querySelector('.hero-usage')!.textContent).not.toContain('64.0%');
  });

  it('blanks both Claude quota slots on All and keeps Claude cards visible', () => {
    updateSnapshot(decoratedClaudeHeroEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const usage = screen.getByTestId('shared-hero-usage');
    const support = screen.getByTestId('shared-hero-support');
    expect(usage).not.toHaveTextContent('64.0%');
    // #556 S1 §5 — the block label names the cycle it covers, and the support
    // zone's duplicated quota row became the provider's own spend leg. The
    // decoration contract itself is unchanged: both still blank to per account.
    expect(usage).toHaveTextContent('CLAUDE · WEEK · per account');
    expect(support).toHaveTextContent('Claude · week to dateper account');
    expect(screen.getByTestId('hero-claude-reset')).toHaveTextContent('per account');
    expect(screen.getByText(/Claude accounts — each has its own quota cycle/))
      .toBeInTheDocument();
    expect(screen.getByText('claude-main')).toBeInTheDocument();
    expect(screen.getByText('claude-alt')).toBeInTheDocument();
  });
});
