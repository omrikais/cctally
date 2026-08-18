// #416 QA — the ALL-PROVIDERS tab under Codex decoration.
//
// The epic corrected the focused Codex view, then the default "All accounts"
// Codex view. The combined tab is the same defect one surface further out: its
// `CODEX 7-DAY` percent, its `resets in` countdown and its `Codex quota` row all
// come from `weekly`, which is `joinCodexQuotaLabels` over the PARENT hero — and
// the parent's `hero.cycle` is `cycles_all[0]`, one representative account's
// window. With three Codex accounts the combined tab published one of them as
// the whole picture.
//
// Blanking alone is the wrong remedy HERE: the Codex tab can blank because the
// per-account strip sits directly beneath it, and `AccountHeroCards` deliberately
// self-hides on `all` ("account cards are provider-scoped") — so on the combined
// tab the pointer would point at nothing. Spec §6 says the "All accounts"
// remedy IS the per-account strip, so the strip comes to this tab too and the
// three lying slots point at it.
//
// These tests pin the DATA BINDING only. Layout, wrapping and the real reset
// countdown stay the real-browser QA gate's job.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { HeroStrip } from './HeroStrip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  ACCOUNT_A,
  ACCOUNT_B,
  ACCOUNT_EMPTY,
  makeAllSourceEntry,
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeDecoratedClaudeSourceData,
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
  CLAUDE_ACCOUNT_ALT,
  CLAUDE_ACCOUNT_MAIN,
} from '../test-utils/sourceEnvelope';
import type { CodexSourceData, Envelope, SourcesMap } from '../types/envelope';

// The representative account's weekly window in the shared fixture: 61.0% and a
// 2026-04-30 reset. Pinned "now" puts that reset 5d 10h out, so the undecorated
// control renders a real countdown rather than a clamped `0d 0h`.
const NOW = '2026-04-24T13:07:00Z';

function envWith(codexData?: CodexSourceData): Envelope {
  const claude = makeClaudeSourceEntry();
  const codex = codexData == null
    ? makeCodexSourceEntry()
    : makeCodexSourceEntry({ data: codexData });
  const sources = {
    claude,
    codex,
    all: makeAllSourceEntry(claude, codex),
  } as unknown as SourcesMap;
  return {
    header: {
      used_pct: 17.4, week_label: 'wk', five_hour_pct: null,
      dollar_per_pct: 1.2, forecast_pct: 60, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    ...makeSourceEnvelope({ sources }),
  } as unknown as Envelope;
}

function renderAll(codexData?: CodexSourceData) {
  updateSnapshot(envWith(codexData));
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  return render(<HeroStrip />);
}

// #556 S1 §5 — the support zone's duplicated quota rows are gone. It now
// carries one spend row per provider, which is the per-leg breakdown of the
// figure beside it.
function codexLegRow(): HTMLElement {
  const support = screen.getByTestId('shared-hero-support');
  const row = [...support.querySelectorAll('.sup-row')].find(
    (el) => (el.textContent ?? '').startsWith('Codex · cycle to date'),
  );
  if (row == null) throw new Error('no Codex leg row');
  return row as HTMLElement;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  cleanup();
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse(NOW));
});

describe('All providers — a decorated Codex provider is never one account', () => {
  it('does not publish the representative account percent as CODEX 7-DAY', () => {
    renderAll(makeDecoratedCodexSourceData());
    const usage = screen.getByTestId('shared-hero-usage');
    // 61% is account A's own weekly percent; B is at 12% and the third account
    // has none. Printing it unlabelled is the reported defect.
    expect(usage.textContent).not.toContain('61%');
    expect(usage.textContent).toContain('per account');
  });

  it('does not publish the representative account reset as the countdown', () => {
    renderAll(makeDecoratedCodexSourceData());
    const usage = screen.getByTestId('shared-hero-usage');
    // The Claude block keeps its own labelled reset; only the Codex one blanks.
    expect(usage.textContent).not.toMatch(/Codex resets in \d/);
    expect(screen.getByTestId('hero-codex-reset').textContent)
      .toContain('per account');
  });

  it('does not publish the representative account spend as the Codex leg row', () => {
    renderAll(makeDecoratedCodexSourceData());
    const row = codexLegRow();
    expect(row.textContent).not.toContain('$12.30');
    expect(row.textContent).toContain('per account');
  });

  it('renders the Codex per-account strip so nothing is merely blanked', () => {
    renderAll(makeDecoratedCodexSourceData());
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards.map((el) => el.getAttribute('data-account')))
      .toEqual([ACCOUNT_A, ACCOUNT_B, ACCOUNT_EMPTY]);
    const strip = screen.getByTestId('account-hero-cards');
    // Every account's OWN percent and spend — server-emitted per-account fields,
    // never re-derived here. The blanked headline loses no information.
    expect(strip.textContent).toContain('61%');
    expect(strip.textContent).toContain('12%');
    expect(strip.textContent).toContain('$8.00');
    expect(strip.textContent).toContain('$4.30');
  });

  it('names the provider the strip belongs to on a combined tab', () => {
    renderAll(makeDecoratedCodexSourceData());
    expect(screen.getByTestId('account-hero-caption').textContent).toMatch(/Codex/);
  });

  it('withholds the combined headline for a decorated provider', () => {
    renderAll(makeDecoratedCodexSourceData());
    const spent = screen.getByTestId('shared-hero-spent');
    expect(spent.textContent).not.toContain('$20.70');
    expect(spent.textContent).toContain('Combined withheld');
    expect(spent.getAttribute('title')).toBe(
      'Codex has 3 accounts on separate cycles, so a combined total is not '
      + 'published; see the per-account cards.',
    );
  });

  // #556 S5 §5.10 — the ASSERTION is kept and its RATIONALE is replaced. The
  // original reason ("the combined tab has no account chip") is now false: All
  // has one chip row per decorated provider. The invariant survives on a
  // different and stronger footing — the two focus slots are INDEPENDENT and
  // never write to each other, so a focus stored for the Codex tab still
  // cannot narrow All.
  it('shows every account regardless of the Codex tab account focus', () => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'provider', account: ACCOUNT_B });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    expect(screen.getAllByTestId('account-hero-card')).toHaveLength(3);
    // ... and the provider-tab focus does not reach All's own slots either.
    const usage = screen.getByTestId('shared-hero-usage');
    expect(usage.textContent).toContain('per account');
  });

  // The INVERSE, which had no test at all: an All focus must not narrow the
  // provider tab. Slot isolation is only an invariant if it holds both ways.
  it('an All focus does not narrow the Codex tab', () => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: ACCOUNT_B });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<HeroStrip />);
    // The Codex tab is still on "All accounts", so its own D6 blank holds.
    expect(container.textContent).toContain('per account');
  });

  // #556 S5 §5.8 — an All CODEX focus un-blanks the provider-native slots and
  // reads the scoped child, while the combined spend stays withheld.
  it('an All Codex focus un-blanks the Codex percent and reset from the child', () => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: ACCOUNT_A });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const usage = screen.getByTestId('shared-hero-usage');
    // Account A's OWN weekly percent, not a representative window's.
    expect(usage.textContent).toContain('61.0%');
    expect(usage.textContent).toMatch(/Codex resets in \d/);
  });

  it.each([
    [ACCOUNT_A, '$8.00'],
    [ACCOUNT_B, '$4.30'],
    [ACCOUNT_EMPTY, '$0.00'],
  ])('reads the focused Codex account spend in the cycle leg for %s', (account, spend) => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const focusedCard = screen.getAllByTestId('account-hero-card')
      .find((card) => card.className.includes('is-focused'));
    expect(focusedCard).toHaveTextContent(spend);
    expect(codexLegRow().textContent).toBe(`Codex · cycle to date${spend}`);
  });

  it('keeps every account card visible under an All Codex focus', () => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: ACCOUNT_A });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    // §5.7 — the strip HIGHLIGHTS and never filters. It is the disclosure that
    // makes a blanked headline honest; filtering it would hide the evidence.
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards).toHaveLength(3);
    const focused = cards.filter((el) => el.className.includes('is-focused'));
    expect(focused.map((el) => el.getAttribute('data-account'))).toEqual([ACCOUNT_A]);
  });

  it('keeps the combined figure withheld under an All Codex focus', () => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const first = render(<HeroStrip />);
    const unfocused = screen.getByTestId('shared-hero-spent').textContent;
    first.unmount();
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', slot: 'all', account: ACCOUNT_A });
    render(<HeroStrip />);
    // Provider-native quota surfaces consume the scoped child; combined SPEND
    // consumes the unscoped withheld outcome and is never recomputed from a
    // focused child.
    const spent = screen.getByTestId('shared-hero-spent');
    expect(spent.textContent).toBe(unfocused);
  });
});

describe('All providers — an undecorated Codex provider is unchanged (R8)', () => {
  it('keeps the single-account percent, countdown and leg row', () => {
    renderAll();
    const usage = screen.getByTestId('shared-hero-usage');
    // One precision across both blocks now (§5), so `61%` became `61.0%`.
    expect(usage.textContent).toContain('61.0%');
    expect(usage.textContent).toContain('Codex resets in 5d 10h');
    expect(usage.textContent).not.toContain('per account');
    expect(codexLegRow().textContent).toBe('Codex · cycle to date$12.30');
  });

  it('renders no per-account strip and no caption', () => {
    const { container } = renderAll();
    expect(container.querySelector('[data-testid="account-hero-cards"]')).toBeNull();
    expect(container.querySelector('[data-testid="account-hero-caption"]')).toBeNull();
  });
});

// #556 S5 §5.9 / acceptance criterion 10 — an All CLAUDE focus.
//
// The `all-budget-account-focus` fixture seeds ONE Claude account, so no
// decorated-Claude state is reachable from it and this criterion has to be
// built here (Unit 1 review item R8). Revision 1 of the spec claimed a Claude
// focus was a hero highlight and nothing else; §1.4 corrects that — on the
// Claude tab `SharedHero` substitutes the account's own numbers and the alert
// panel filters its rows, and operator decision 7 says All matches the tab on
// the corrected facts. What a Claude focus CANNOT do is scope the panels,
// because Claude publishes no `account_scopes`, and that is exactly what the
// row's `hero and alerts only` qualifier states.
describe('All providers — a decorated CLAUDE provider under focus (#556 S5)', () => {
  function decoratedClaudeEnv(claudeData = makeDecoratedClaudeSourceData()): Envelope {
    const claude = makeClaudeSourceEntry({ data: claudeData });
    const codex = makeCodexSourceEntry();
    const sources = {
      claude, codex, all: makeAllSourceEntry(claude, codex),
    } as unknown as SourcesMap;
    return {
      header: {
        used_pct: 17.4, week_label: 'wk', five_hour_pct: null,
        dollar_per_pct: 1.2, forecast_pct: 60, forecast_verdict: 'ok',
        vs_last_week_delta: null,
      },
      current_week: null,
      ...makeSourceEnvelope({ sources }),
    } as unknown as Envelope;
  }

  // The state the round-2 browser gate found in production: every Claude
  // account publishes `weeklyPercent: null`, because Claude's per-account
  // quota evidence is far sparser than its per-account accounting.
  function claudeEnvWithoutPerAccountQuota(): Envelope {
    const base = makeDecoratedClaudeSourceData();
    return decoratedClaudeEnv({
      ...base,
      accounts: (base.accounts ?? []).map((card) => ({
        ...card, weeklyPercent: null, fiveHourPercent: null, resetsAt: null,
      })),
    });
  }

  it('blanks the Claude percent under All accounts, as Codex already did', () => {
    updateSnapshot(decoratedClaudeEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const usage = screen.getByTestId('shared-hero-usage');
    // Non-vacuity for the test below: 17.4 is the MERGED header percent, and
    // publishing it as one account's is the defect the blank exists for.
    expect(usage.textContent).not.toContain('17.4%');
    expect(usage.textContent).toContain('per account');
  });

  it('substitutes the focused account\u2019s own weekly percent', () => {
    updateSnapshot(decoratedClaudeEnv());
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_ALT });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const usage = screen.getByTestId('shared-hero-usage');
    // The ALT account is at 22%; MAIN is at 64% and the merged header at 17.4%.
    // Reading either of the other two would be a different account's number
    // printed under this one's chip.
    expect(usage.textContent).toContain('22.0%');
    expect(usage.textContent).not.toContain('64.0%');
    expect(usage.textContent).not.toContain('17.4%');
  });

  // #556 S5 round-2 browser gate, P1. Substitution has a second half: when the
  // focused account publishes NO weekly percentage, the headline must say so.
  // Falling through to `header.used_pct` replaced an honest withheld state with
  // the provider-wide roll-up, so the largest figure on the page attributed
  // every Claude account's consumption to the one account under focus — and two
  // different accounts printed the identical number. This is the headline layer
  // of the rule `accountScope.ts` already states for the panels: never fall back
  // to the parent under focus.
  it('withholds the headline when the focused account publishes no weekly percent', () => {
    updateSnapshot(claudeEnvWithoutPerAccountQuota());
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_ALT });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const usage = screen.getByTestId('shared-hero-usage');
    // 17.4 is the provider-wide merged percent. No account published it.
    expect(usage.textContent).not.toContain('17.4%');
    const num = usage.querySelector('[data-provider-block="claude"] .hu-num')!;
    expect(num.textContent).toBe('—');
    // Dimmed, so the blank reads as a deliberate absence rather than a pending
    // load (the #416 QA P3-C vocabulary the per-account blank already uses).
    expect(num.className).toContain('is-blank');
  });

  it.each([
    [CLAUDE_ACCOUNT_MAIN, '$88.20'],
    [CLAUDE_ACCOUNT_ALT, '$19.40'],
  ])('reads the focused Claude account spend in the week leg for %s', (account, spend) => {
    updateSnapshot(decoratedClaudeEnv());
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const focusedCard = screen.getAllByTestId('account-hero-card')
      .find((card) => card.className.includes('is-focused'));
    expect(focusedCard).toHaveTextContent(spend);
    expect(screen.getByTestId('hero-leg-claude').textContent).toBe(spend);
  });

  it('renders a resolved zero from the focused Claude card as $0.00', () => {
    const data = makeDecoratedClaudeSourceData();
    const zeroData = {
      ...data,
      accounts: (data.accounts ?? []).map((card) => (
        card.accountKey === CLAUDE_ACCOUNT_ALT ? { ...card, spendUsd: 0 } : card
      )),
    };
    updateSnapshot(decoratedClaudeEnv(zeroData));
    dispatch({
      type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_ALT,
    });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);

    const focusedCard = screen.getAllByTestId('account-hero-card')
      .find((card) => card.className.includes('is-focused'));
    expect(focusedCard).toHaveTextContent('$0.00');
    expect(screen.getByTestId('hero-leg-claude').textContent).toBe('$0.00');
  });

  it('does not print one figure for two different focused accounts', () => {
    updateSnapshot(claudeEnvWithoutPerAccountQuota());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_MAIN });
    const first = render(<HeroStrip />);
    const mainPct = screen
      .getByTestId('shared-hero-usage')
      .querySelector('[data-provider-block="claude"] .hu-num')!.textContent;
    first.unmount();
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_ALT });
    render(<HeroStrip />);
    const altPct = screen
      .getByTestId('shared-hero-usage')
      .querySelector('[data-provider-block="claude"] .hu-num')!.textContent;
    // Identical is correct ONLY because both are the withheld blank; the
    // assertion above pins which value that is, so this cannot pass on a
    // shared borrowed percentage.
    expect([mainPct, altPct]).toEqual(['—', '—']);
  });

  it('keeps every Claude card visible and highlights only the focused one', () => {
    updateSnapshot(decoratedClaudeEnv());
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_MAIN });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    const cards = screen.getAllByTestId('account-hero-card');
    expect(cards.length).toBeGreaterThan(1);
    expect(
      cards.filter((el) => el.className.includes('is-focused'))
        .map((el) => el.getAttribute('data-account')),
    ).toEqual([CLAUDE_ACCOUNT_MAIN]);
  });

  it('leaves the combined spend headline untouched by a Claude focus', () => {
    updateSnapshot(decoratedClaudeEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const before = screen.queryByTestId('shared-hero-spent');
    expect(before).toBeNull();
    const first = render(<HeroStrip />);
    const unfocused = screen.getByTestId('shared-hero-spent').textContent;
    first.unmount();
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', slot: 'all', account: CLAUDE_ACCOUNT_MAIN });
    render(<HeroStrip />);
    // Combined spend consumes the unscoped `combined` outcome and is never
    // recomputed from a focused child, so it must not move.
    expect(screen.getByTestId('shared-hero-spent').textContent).toBe(unfocused);
  });
});
