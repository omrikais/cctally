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
  makeDecoratedCodexSourceData,
  makeSourceEnvelope,
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

function codexQuotaRow(): HTMLElement {
  const support = screen.getByTestId('shared-hero-support');
  const row = [...support.querySelectorAll('.sup-row')].find(
    (el) => (el.textContent ?? '').startsWith('Codex quota'),
  );
  if (row == null) throw new Error('no Codex quota row');
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
    expect(usage.textContent).not.toMatch(/resets in \d/);
    expect(screen.getByTestId('hero-per-account-note').textContent)
      .toContain('per account');
  });

  it('does not publish the representative account percent as the Codex quota row', () => {
    renderAll(makeDecoratedCodexSourceData());
    const row = codexQuotaRow();
    expect(row.textContent).not.toContain('61.0%');
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

  it('keeps the merged combined spend and tokens as the headline', () => {
    renderAll(makeDecoratedCodexSourceData());
    const spent = screen.getByTestId('shared-hero-spent');
    // claude 8.4 + codex 12.3 (itself the sum of the cards) = 20.7.
    expect(spent.textContent).toContain('$21');
    expect(spent.textContent).toContain('total tokens');
  });

  it('shows every account regardless of the Codex tab account focus', () => {
    updateSnapshot(envWith(makeDecoratedCodexSourceData()));
    dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'codex', account: ACCOUNT_B });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    render(<HeroStrip />);
    // The combined tab has no account chip, so a focus stored for the Codex tab
    // must never silently narrow it back to one account.
    expect(screen.getAllByTestId('account-hero-card')).toHaveLength(3);
  });
});

describe('All providers — an undecorated Codex provider is unchanged (R8)', () => {
  it('keeps the single-account percent, countdown and quota row', () => {
    renderAll();
    const usage = screen.getByTestId('shared-hero-usage');
    expect(usage.textContent).toContain('61%');
    expect(usage.textContent).toContain('resets in 5d 10h');
    expect(usage.textContent).not.toContain('per account');
    expect(codexQuotaRow().textContent).toBe('Codex quota61.0%');
  });

  it('renders no per-account strip and no caption', () => {
    const { container } = renderAll();
    expect(container.querySelector('[data-testid="account-hero-cards"]')).toBeNull();
    expect(container.querySelector('[data-testid="account-hero-caption"]')).toBeNull();
  });
});
