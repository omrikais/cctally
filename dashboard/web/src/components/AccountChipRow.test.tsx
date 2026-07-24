import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import { AccountChipRow } from './AccountChipRow';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { cycleActiveAccount } from '../store/globalBindings';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { AccountCard, Envelope } from '../types/envelope';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function card(accountKey: string, label: string, weeklyPercent: number | null): AccountCard {
  return {
    accountKey, label, plan: 'pro', active: false,
    weeklyPercent, fiveHourPercent: null, resetsAt: null, spendUsd: 0,
    inputTokens: 0, cachedInputTokens: 0, outputTokens: 0,
    reasoningOutputTokens: 0, totalTokens: 0,
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

describe('AccountChipRow (Q6 Option A)', () => {
  it('renders nothing on an undecorated source', () => {
    updateSnapshot(makeSourceEnvelope() as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<AccountChipRow />);
    expect(container.querySelector('[data-testid="account-chip-row"]')).toBeNull();
  });

  it('renders nothing under source `all`', () => {
    updateSnapshot(decoratedEnv([card(A, 'alice', 40), card(B, 'bob', 55)]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<AccountChipRow />);
    expect(container.querySelector('[data-testid="account-chip-row"]')).toBeNull();
  });

  it('renders a radiogroup with All + one chip per account (weekly hint)', () => {
    updateSnapshot(decoratedEnv([card(A, 'alice', 40), card(B, 'bob', 55)]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountChipRow />);
    const group = screen.getByRole('radiogroup', { name: 'Account focus' });
    expect(group).toBeTruthy();
    const radios = screen.getAllByRole('radio');
    expect(radios.map((r) => r.textContent)).toEqual(['All accounts', 'alice40%', 'bob55%']);
    // Default focus is All → its chip is checked (the roving tab stop).
    expect(radios[0].getAttribute('aria-checked')).toBe('true');
    expect(radios[0].getAttribute('tabindex')).toBe('0');
    expect(radios[1].getAttribute('tabindex')).toBe('-1');
  });

  it('clicking a chip focuses that account (SET_ACCOUNT_FOCUS)', () => {
    updateSnapshot(decoratedEnv([card(A, 'alice', 40), card(B, 'bob', 55)]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountChipRow />);
    fireEvent.click(screen.getByRole('radio', { name: /bob/ }));
    expect(getState().accountFocus.codex).toBe(B);
  });

  it('dims the unattributed chip', () => {
    updateSnapshot(decoratedEnv([
      card(A, 'alice', 40),
      { ...card('unattributed', 'Unattributed', null), unattributed: true },
    ]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountChipRow />);
    const unattr = screen.getByRole('radio', { name: 'Unattributed' });
    expect(unattr.className).toContain('is-dimmed');
  });
});

// #341 ui-qa P3 (a11y) — the live region must announce EVERY focus change,
// including the global `a` shortcut (cycleActiveAccount), which dispatches
// SET_ACCOUNT_FOCUS directly and used to bypass the chip UI's announce.
describe('AccountChipRow — live-region announce (ui-qa P3)', () => {
  it('is silent on mount and announces the focused chip on the arrow-key path', () => {
    updateSnapshot(decoratedEnv([card(A, 'alice', 40), card(B, 'bob', 55)]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountChipRow />);
    const live = screen.getByTestId('account-chip-live');
    expect(live).toHaveAttribute('aria-live', 'polite');
    expect(live.textContent).toBe(''); // silent on mount — no spurious announce
    const all = screen.getAllByRole('radio')[0];
    all.focus();
    fireEvent.keyDown(all, { key: 'ArrowRight' }); // All → alice
    expect(live).toHaveTextContent(/alice account selected/i);
  });

  it('announces via the global `a` shortcut, not only the chip UI', () => {
    updateSnapshot(decoratedEnv([card(A, 'alice', 40), card(B, 'bob', 55)]));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<AccountChipRow />);
    const live = screen.getByTestId('account-chip-live');
    expect(live.textContent).toBe(''); // silent on mount
    act(() => cycleActiveAccount()); // All → alice (the `a` path)
    expect(live).toHaveTextContent(/alice account selected/i);
    act(() => cycleActiveAccount()); // alice → bob
    expect(live).toHaveTextContent(/bob account selected/i);
  });
});
