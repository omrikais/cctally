import { beforeEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { RecentAlertsPanel } from './RecentAlertsPanel';
import { RecentAlertsModal } from './RecentAlertsModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import type { AccountCard, AlertEntry, Envelope } from '../types/envelope';

const WORK = 'a'.repeat(32);
const PERSONAL = 'b'.repeat(32);

function card(accountKey: string, label: string): AccountCard {
  return {
    accountKey, label, plan: 'pro', active: false, weeklyPercent: 10,
    fiveHourPercent: null, resetsAt: null, spendUsd: 0, inputTokens: 0,
    cachedInputTokens: 0, outputTokens: 0, reasoningOutputTokens: 0, totalTokens: 0,
  };
}

function alert(
  threshold: number,
  accountKey?: string,
  accountLabel?: string,
): AlertEntry {
  return {
    id: `weekly:2026-07-13:${threshold}:0`,
    axis: 'weekly',
    threshold,
    crossed_at: `2026-07-15T13:0${threshold % 10}:00Z`,
    alerted_at: `2026-07-15T13:0${threshold % 10}:00Z`,
    context: { week_start_date: '2026-07-13' },
    ...(accountKey == null ? {} : { accountKey, accountLabel }),
  } as AlertEntry;
}

function decoratedClaudeEnv(): Envelope {
  const env = makeSourceEnvelope() as unknown as Envelope & {
    sources: { claude: { data: { accounts?: AccountCard[] } } };
  };
  env.sources.claude.data.accounts = [card(WORK, 'work'), card(PERSONAL, 'personal')];
  env.alerts = [
    alert(91, WORK, 'work'),
    alert(92, PERSONAL, 'personal'),
    alert(93, '*', 'All accounts'),
  ];
  return env;
}

function focusWork(): void {
  const env = decoratedClaudeEnv();
  updateSnapshot(env);
  dispatch({
    type: 'INGEST_SNAPSHOT_ALERTS',
    alerts: env.alerts,
    alertsSettings: {
      enabled: true,
      weekly_thresholds: [90, 95],
      five_hour_thresholds: [90, 95],
      budget_thresholds: [90, 95],
    },
    isFirstTick: true,
  });
  dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
  dispatch({ type: 'SET_ACCOUNT_FOCUS', source: 'claude', account: WORK });
}

beforeEach(() => {
  cleanup();
  localStorage.clear();
  _resetForTests();
});

describe('Recent alerts account decoration (#345)', () => {
  it('filters a focused Claude panel while retaining the vendor-wide row', () => {
    focusWork();
    const { container } = render(<RecentAlertsPanel />);
    expect(container.querySelector('[data-testid="alerts-unfiltered-note"]')).toBeNull();
    expect(screen.getByTestId('alerts-account-note').textContent).toContain('work');
    expect(screen.getByText('work')).toBeTruthy();
    expect(screen.getByText('All accounts')).toBeTruthy();
    expect(screen.queryByText('personal')).toBeNull();
    expect(container.querySelectorAll('.alert-row')).toHaveLength(2);
  });

  it('shows the conditional Account column and chips in the focused modal', () => {
    focusWork();
    const { container } = render(<RecentAlertsModal />);
    expect(screen.getByRole('columnheader', { name: 'Account' })).toBeTruthy();
    expect(screen.getByText('work')).toBeTruthy();
    expect(screen.getByText('All accounts')).toBeTruthy();
    expect(screen.queryByText('personal')).toBeNull();
    expect(container.querySelectorAll('.alert-modal-row')).toHaveLength(2);
  });

  it('keeps an undecorated single-account row free of account UI', () => {
    const env = makeSourceEnvelope() as unknown as Envelope & {
      sources: { claude: { data: { accounts?: AccountCard[] } } };
    };
    env.sources.claude.data.accounts = [card(WORK, 'work')];
    env.alerts = [alert(91)];
    updateSnapshot(env);
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: env.alerts,
      alertsSettings: {
        enabled: true,
        weekly_thresholds: [90, 95],
        five_hour_thresholds: [90, 95],
        budget_thresholds: [90, 95],
      },
      isFirstTick: true,
    });
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'claude' });
    const { container } = render(<RecentAlertsModal />);
    expect(screen.queryByRole('columnheader', { name: 'Account' })).toBeNull();
    expect(container.querySelector('.alert-account-chip')).toBeNull();
  });
});
