// #416 Task 15 — the panels genuinely re-scope to the focused account.
//
// Before this, the account chip was a hero decoration: every panel kept showing
// the merged all-account numbers, and Sessions/Alerts shipped an explicit
// "all accounts (unfiltered)" badge because they were honest about it. These
// tests pin the DATA BINDING — which rows a focused chip paints — and that the
// badge is gone exactly where scoping is real (Codex) and kept where it is not
// (Claude, which emits no `account_scopes`).
//
// jsdom cannot evaluate @media, real scroll, trusted pointer events or
// `#root .class` specificity, so nothing here claims visual or interaction
// correctness — that is the real-browser QA gate's job.
import { beforeEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { SessionsPanel } from './SessionsPanel';
import { DailyPanel } from './DailyPanel';
import { ProjectsPanel } from './ProjectsPanel';
import { RecentAlertsPanel } from '../components/RecentAlertsPanel';
import { HeroStrip } from '../components/HeroStrip';
import { _resetForTests, dispatch, getScopedSnapshot, updateSnapshot } from '../store/store';
import {
  ACCOUNT_A,
  ACCOUNT_B,
  ACCOUNT_EMPTY,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type {
  AccountCard,
  CodexSourceData,
  Envelope,
  SourcesMap,
} from '../types/envelope';

function decoratedEnv(): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { codex: { data: CodexSourceData } };
  };
  slice.sources.codex.data = makeDecoratedCodexSourceData();
  return slice as unknown as Envelope;
}

// The same decoration, with the `all` entry COMPOSED FROM IT rather than from
// the default undecorated Codex data. `decoratedEnv` mutates `sources.codex`
// after `makeSourceEnvelope` has already built `sources.all`, so its combined
// alert union carries no account keys at all.
//
// That does not make the badge unreachable under `decoratedEnv` — the badge
// reads `sources.codex` through `scopeToAccount`, so it renders either way.
// What `decoratedAllEnv` is for is the CONTENT: only a union composed after the
// decoration carries the account keys these assertions read. Every other test
// in this file dispatches `SET_ACTIVE_SOURCE: 'codex'` and never touches the
// union, so none of them is weakened by the stale composition.
function decoratedAllEnv(): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = makeCodexSourceEntry({ data: makeDecoratedCodexSourceData() });
  return makeSourceEnvelope({
    sources: {
      claude,
      codex,
      all: makeAllSourceEntry(claude, codex),
    } as unknown as SourcesMap,
  }) as unknown as Envelope;
}

// Claude is decorated (>1 real account) but ships NO `account_scopes` — the
// chip is a hero decoration there and the disclaimer must survive.
function claudeDecoratedEnv(): Envelope {
  const slice = makeSourceEnvelope() as unknown as {
    sources: { claude: { data: { accounts?: AccountCard[] } } };
  };
  slice.sources.claude.data.accounts = [
    {
      accountKey: ACCOUNT_A, label: 'work', plan: 'max', active: false,
      weeklyPercent: 10, fiveHourPercent: null, resetsAt: null, spendUsd: 0,
      inputTokens: 0, cachedInputTokens: 0, outputTokens: 0,
      reasoningOutputTokens: 0, totalTokens: 0,
    },
    {
      accountKey: ACCOUNT_B, label: 'personal', plan: 'pro', active: false,
      weeklyPercent: 2, fiveHourPercent: null, resetsAt: null, spendUsd: 0,
      inputTokens: 0, cachedInputTokens: 0, outputTokens: 0,
      reasoningOutputTokens: 0, totalTokens: 0,
    },
  ];
  return slice as unknown as Envelope;
}

function focusCodex(account: string): void {
  updateSnapshot(decoratedEnv());
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
});

describe('getScopedSnapshot (the store-side chokepoint)', () => {
  it('hands panels the focused account child, and the parent under All', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const all = getScopedSnapshot()!.sources!.codex!.data as CodexSourceData;
    expect(all.periods.daily.rows[0].label).toBe('04-24');
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: ACCOUNT_B });
    const scoped = getScopedSnapshot()!.sources!.codex!.data as CodexSourceData;
    expect(scoped.periods.daily.rows[0].label).toBe('B-04-24');
    // Reference-stable across repeated reads so downstream memoisation holds.
    expect(getScopedSnapshot()).toBe(getScopedSnapshot());
  });
});

describe('Sessions grid scopes to the focused account', () => {
  it('paints only that account rows and drops the unfiltered badge', () => {
    focusCodex(ACCOUNT_A);
    const { container } = render(<SessionsPanel />);
    expect(screen.queryByText('Session A')).toBeTruthy();
    expect(screen.queryByText('Session 1')).toBeNull();
    expect(screen.queryByText('Session B')).toBeNull();
    expect(container.querySelector('[data-testid="sessions-unfiltered-note"]')).toBeNull();
  });

  it('names the focused account instead of the removed disclaimer', () => {
    focusCodex(ACCOUNT_A);
    render(<SessionsPanel />);
    expect(screen.getByTestId('sessions-account-note').textContent)
      .toContain('work@example.com');
  });

  it('KEEPS the unfiltered disclaimer on Claude, which emits no account scopes', () => {
    updateSnapshot(claudeDecoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'provider', account: ACCOUNT_A });
    render(<SessionsPanel />);
    expect(screen.getByTestId('sessions-unfiltered-note')).toBeTruthy();
  });

  // #556 S5 Unit 2 review F3 — under All the grid lists BOTH providers, and
  // only Codex publishes `account_scopes`, so an unqualified "<label> only"
  // claimed a narrowing the Claude half never got.
  it('qualifies the badge by provider under All, where Claude stays unfiltered', () => {
    updateSnapshot(decoratedAllEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: ACCOUNT_A });
    render(<SessionsPanel />);
    const note = screen.getByTestId('sessions-account-note');
    expect(note.textContent).toContain('Codex:');
    expect(note.getAttribute('title'))
      .toContain('Claude sessions are not filtered by account');
  });

  it('leaves the provider-tab badge unqualified, where the claim is true', () => {
    focusCodex(ACCOUNT_A);
    render(<SessionsPanel />);
    const note = screen.getByTestId('sessions-account-note');
    expect(note.textContent).not.toContain('Codex:');
    expect(note.getAttribute('title'))
      .not.toContain('Claude sessions are not filtered by account');
  });
});

describe('Recent alerts scopes to the focused account', () => {
  it('shows that account rows, drops the badge, and labels vendor-wide rows', () => {
    focusCodex(ACCOUNT_A);
    const { container } = render(<RecentAlertsPanel />);
    expect(container.querySelector('[data-testid="alerts-unfiltered-note"]')).toBeNull();
    // The vendor-wide `*` budget crossing stays VISIBLE under focus and is
    // labelled as vendor-wide — never hidden, never attributed to this account.
    expect(screen.getByTestId('alert-vendor-wide')).toBeTruthy();
  });

  it('shows nothing but the empty gauge for an account with no alerts', () => {
    focusCodex(ACCOUNT_B);
    const { container } = render(<RecentAlertsPanel />);
    expect(container.querySelectorAll('.alert-row').length).toBe(0);
  });

  it('KEEPS the unfiltered disclaimer on Claude', () => {
    updateSnapshot(claudeDecoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'provider', account: ACCOUNT_A });
    render(<RecentAlertsPanel />);
    expect(screen.getByTestId('alerts-unfiltered-note')).toBeTruthy();
  });

  // #556 S5 Unit 2 review F3 — the alerts twin. Under All each provider's rows
  // are filtered by that provider's OWN focus, so focusing one provider leaves
  // the other's rows in full and an unqualified "<label> only" over-claims.
  it('qualifies the badge by provider when only one provider is focused under All', () => {
    updateSnapshot(decoratedAllEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: ACCOUNT_A });
    const { container } = render(<RecentAlertsPanel />);
    const note = screen.getByTestId('alerts-account-note');
    expect(note.textContent).toContain('Codex:');
    expect(note.getAttribute('title'))
      .toContain('Claude alerts are not filtered by account');
    // ... and NOT the opposite over-claim. A bare `useAccountScope()` resolved
    // no provider under All, so `scopesSupported` was false there whatever was
    // focused and this panel printed "all accounts (unfiltered)" over a Codex
    // subtree that had already been narrowed to one account.
    expect(container.querySelector('[data-testid="alerts-unfiltered-note"]')).toBeNull();
  });

  it('leaves the provider-tab badge unqualified, where the claim is true', () => {
    focusCodex(ACCOUNT_A);
    render(<RecentAlertsPanel />);
    const note = screen.getByTestId('alerts-account-note');
    expect(note.textContent).not.toContain('Codex:');
    expect(note.getAttribute('title'))
      .not.toContain('Claude alerts are not filtered by account');
  });
});

describe('Period and project panels scope to the focused account', () => {
  it('renders the focused account daily bucket, not the merged one', () => {
    focusCodex(ACCOUNT_B);
    // The heatmap cell paints only the day-of-month + cost, so the identity
    // lives on `data-cell-date` (the row's `date`), not in visible text.
    const { container } = render(<DailyPanel />);
    expect(container.querySelector('[data-cell-date="B-04-24"]')).toBeTruthy();
    expect(container.querySelector('[data-cell-date="A-04-24"]')).toBeNull();
    expect(container.querySelector('[data-cell-date="04-24"]')).toBeNull();
  });

  it('renders the focused account projects', () => {
    focusCodex(ACCOUNT_B);
    render(<ProjectsPanel />);
    expect(screen.queryByText('proj-B')).toBeTruthy();
    expect(screen.queryByText('alpha')).toBeNull();
  });
});

describe('The empty state is explicit, never the previous account numbers', () => {
  it('renders an honest empty note for an account with no activity', () => {
    focusCodex(ACCOUNT_EMPTY);
    render(<HeroStrip />);
    const note = screen.getByTestId('account-hero-empty');
    expect(note.textContent).toContain('quiet@example.com');
  });

  it('does not render the note for an account that has activity', () => {
    focusCodex(ACCOUNT_A);
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('[data-testid="account-hero-empty"]')).toBeNull();
  });
});

describe('All accounts never blends independent quota percentages (D6)', () => {
  it('blanks the headline percent and defers to the per-account strip', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<HeroStrip />);
    expect(screen.getByTestId('hero-per-account-note')).toBeTruthy();
    // Two per-account cards carry the two independent percentages.
    expect(screen.getAllByTestId('account-hero-card').length).toBe(3);
  });

  it('shows the focused account own percentage once a chip is picked', () => {
    focusCodex(ACCOUNT_A);
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('[data-testid="hero-per-account-note"]')).toBeNull();
  });

  // #416 QA P1-C — the week label is one account's cycle window. Printing
  // `JUL 24–JUL 31` on the very line that blanks the percentage *because each
  // account has its own cycle* contradicts itself in a single glance.
  it('does not print one account cycle window as the merged week label', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('.hu-week')).toBeNull();
  });

  it('restores the week label for a focused account own cycle', () => {
    focusCodex(ACCOUNT_A);
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('.hu-week')).not.toBeNull();
  });

  // #416 QA P2-D — `Forecast @ reset —`, `$/1% vs last week —` and
  // `— / 1% used` render bare. The usage/reset blank reads as intentional
  // because it carries the `per account` caption; these three read as broken.
  it('points every deliberate blank at the per-account cards', () => {
    updateSnapshot(decoratedEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('.hero-spent')!.textContent)
      .toContain('per account');
    expect(container.querySelector('.hero-support')!.textContent)
      .toContain('per account');
    expect(container.querySelectorAll('[data-testid="hero-per-account-value"]').length)
      .toBe(3);
  });

  it('leaves no per-account pointer once a chip is picked', () => {
    focusCodex(ACCOUNT_A);
    const { container } = render(<HeroStrip />);
    expect(container.querySelectorAll('[data-testid="hero-per-account-value"]').length)
      .toBe(0);
  });

  // D6 is unconditional under "All accounts": spend and tokens merge, the
  // percentage / reset / $/1% do not. With ONE live cycle the old `> 1` gate
  // let the surviving account's percentage stand in for the whole while the
  // headline spend already merged every account — a blended $/1% by
  // construction (merged spend over one account's percent).
  it('still refuses to blend when only one account cycle is live', () => {
    const env = decoratedEnv();
    const codex = env.sources!.codex!.data as CodexSourceData;
    codex.hero.cycles = codex.hero.cycles!.slice(0, 1);
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<HeroStrip />);
    expect(container.querySelector('[data-testid="hero-per-account-note"]')).not.toBeNull();
    expect(container.querySelector('.hu-week')).toBeNull();
  });
});
