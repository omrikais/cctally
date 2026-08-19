// #620 S1 D12 (F1, F3) — every live warning state routes to the explanation
// that already ships, scoped to that warning's provider, account and window.
//
// The four surfaces D12 names: the rows in `RecentAlertsModal`, `Toast`,
// `BudgetBlock`'s warn and over branches, and `ForecastModal`'s warning
// verdict. Each affordance is a NATIVE focusable control, so Tab reaches it and
// Enter or Space activates it without depending on the panel-focus flow that
// residual R7 records as absent.
//
// What JSDOM cannot decide, and the browser gate therefore owns: real Tab
// order, trusted Enter/Space activation, focus rings, and whether the reason
// text is legible at 390px. These tests assert the STRUCTURE that makes native
// activation work — a real `<button>`, no negative tabIndex, not disabled — and
// then assert what activation does to the store.
import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { RecentAlertsModal } from '../src/components/RecentAlertsModal';
import { Toast } from '../src/components/Toast';
import { BudgetComposition } from '../src/components/BudgetBlock';
import { ForecastModal } from '../src/modals/ForecastModal';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../src/store/store';
import {
  CLOSED_WINDOW_REASON,
  NO_BLOCK_IDENTITY_REASON,
  VENDOR_WIDE_PROJECT_REASON,
} from '../src/lib/alertScope';
import fixture from './fixtures/envelope.json';
import type { AlertEntry, Envelope } from '../src/types/envelope';

// The shared fixture's live subscription week: generated_at 2026-04-24T13:07Z,
// reset_at_utc 2026-04-28T00:00Z. Every "live" alert below is anchored to it,
// so the equality D12 requires holds by construction rather than by luck.
const LIVE_WEEK_START = '2026-04-21T00:00:00Z';
const CLOSED_WEEK_START = '2026-03-01T00:00:00Z';

const ALERTS_SETTINGS = {
  enabled: false,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
  budget_thresholds: [90, 100],
  budget_enabled: false,
  projected_weekly_enabled: false,
  projected_budget_enabled: false,
};

function alert(over: Partial<AlertEntry> & { axis: AlertEntry['axis'] }): AlertEntry {
  return {
    id: `${over.axis}:opaque:90`,
    threshold: 90,
    crossed_at: '2026-04-24T12:00:00Z',
    alerted_at: '2026-04-24T12:00:00Z',
    context: {},
    ...over,
  } as AlertEntry;
}

/** The six dashboard axes, each with a LIVE window on the shared fixture, and
 *  what activating its affordance must leave in the store. */
const LIVE_AXES: Array<{
  name: string;
  entry: AlertEntry;
  expect: (state: ReturnType<typeof getState>) => void;
}> = [
  {
    name: 'weekly',
    entry: alert({ axis: 'weekly', context: { week_start_at: LIVE_WEEK_START } }),
    expect: (s) => {
      expect(s.openModal).toBe('current-week');
      expect(s.openModalSource).toBe('claude');
    },
  },
  {
    name: 'five_hour',
    entry: alert({
      axis: 'five_hour',
      context: { block_start_at: '2026-04-24T10:00:00Z', five_hour_window_key: 1 },
    }),
    expect: (s) => {
      expect(s.openModal).toBe('block');
      expect(s.openBlockStartAt).toBe('2026-04-24T10:00:00Z');
    },
  },
  {
    name: 'budget',
    entry: alert({
      axis: 'budget',
      context: { week_start_at: LIVE_WEEK_START, budget_usd: 300, spent_usd: 280 },
    }),
    expect: (s) => {
      expect(s.openModal).toBe('current-week');
      expect(s.openModalSource).toBe('claude');
    },
  },
  {
    name: 'codex_budget',
    entry: alert({
      axis: 'codex_budget',
      context: { period: 'calendar-month', period_start_at: '2026-04-01T00:00:00Z' },
    }),
    expect: (s) => {
      expect(s.openModal).toBe('monthly');
      expect(s.openModalSource).toBe('codex');
    },
  },
  {
    name: 'project_budget',
    entry: alert({
      axis: 'project_budget',
      context: { week_start_at: LIVE_WEEK_START, project_key: '/repo/alpha', project: 'alpha' },
    }),
    expect: (s) => {
      expect(s.openModal).toBe('projects');
      expect(s.openProjectKey).toBe('/repo/alpha');
    },
  },
  {
    name: 'projected',
    entry: alert({
      axis: 'projected',
      metric: 'weekly_pct',
      context: { metric: 'weekly_pct', week_start_at: LIVE_WEEK_START },
    }),
    expect: (s) => {
      expect(s.openModal).toBe('forecast');
      expect(s.openModalSource).toBe('claude');
    },
  },
];

function baseEnv(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(fixture)) as Record<string, unknown>;
}

function seedAlerts(alerts: AlertEntry[], env = baseEnv()): void {
  dispatch({
    type: 'INGEST_SNAPSHOT_ALERTS',
    alerts,
    alertsSettings: ALERTS_SETTINGS,
    isFirstTick: true,
  });
  const sources = (env as unknown as {
    sources: { claude: { data: { alerts: { rows: unknown[] } } } };
  }).sources;
  sources.claude.data.alerts = {
    rows: alerts.map((a) => ({ ...a, source: 'claude', key: a.id })),
  };
  updateSnapshot(env as unknown as Envelope);
}

/** The structural properties that make a control keyboard-operable natively.
 *  Real Tab/Enter/Space traversal is the browser gate's; this pins the shape
 *  that makes it work, so a `<div onClick>` regression fails here. */
function expectNativelyOperable(el: Element | null): void {
  expect(el).not.toBeNull();
  expect(el?.tagName).toBe('BUTTON');
  expect((el as HTMLButtonElement).type).toBe('button');
  expect((el as HTMLButtonElement).disabled).toBe(false);
  const tabIndex = el?.getAttribute('tabindex');
  expect(tabIndex == null || Number(tabIndex) >= 0).toBe(true);
  // A control with no accessible name is reachable and unusable.
  expect((el?.textContent ?? '') + (el?.getAttribute('aria-label') ?? '')).not.toBe('');
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('#620 S1 D12 — RecentAlertsModal rows', () => {
  it.each(LIVE_AXES)('$name opens the mapped target scoped to the alert window', (spec) => {
    seedAlerts([spec.entry]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);

    const button = container.querySelector('.alert-row-open');
    expectNativelyOperable(button);
    fireEvent.click(button as HTMLButtonElement);
    spec.expect(getState());
  });

  it('covers all six dashboard axes', () => {
    expect(new Set(LIVE_AXES.map((a) => a.entry.axis)).size).toBe(6);
  });

  it('a retained historical five-hour block still opens, by block_start_at', () => {
    // Six weeks before the envelope's own instant — long closed, still retained.
    seedAlerts([alert({
      axis: 'five_hour',
      context: { block_start_at: '2026-03-10T05:00:00Z' },
    })]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);

    const button = container.querySelector('.alert-row-open');
    expectNativelyOperable(button);
    fireEvent.click(button as HTMLButtonElement);
    expect(getState().openModal).toBe('block');
    expect(getState().openBlockStartAt).toBe('2026-03-10T05:00:00Z');
  });

  it.each([
    {
      name: 'a closed weekly window',
      entry: alert({ axis: 'weekly', context: { week_start_at: CLOSED_WEEK_START } }),
      reason: CLOSED_WINDOW_REASON,
    },
    {
      name: 'a five-hour alert with no recorded block start',
      entry: alert({
        axis: 'five_hour',
        context: { five_hour_window_key: Date.parse('2026-03-10T10:00:00Z') / 1000 },
      }),
      reason: NO_BLOCK_IDENTITY_REASON,
    },
    {
      name: 'a vendor-wide project-budget row',
      entry: alert({
        axis: 'project_budget',
        accountKey: '*',
        accountLabel: 'All accounts',
        context: { week_start_at: LIVE_WEEK_START, project_key: '/repo/alpha' },
      }),
      reason: VENDOR_WIDE_PROJECT_REASON,
    },
  ])('$name states why and opens nothing', ({ entry: row, reason }) => {
    seedAlerts([row]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);

    expect(container.querySelector('.alert-row-open')).toBeNull();
    const withheld = container.querySelector('.alert-row-withheld');
    expect(withheld?.textContent).toBe(reason);
    // The current window is never substituted: the alerts modal is still the
    // only thing open.
    expect(getState().openModal).toBe('alerts');
    expect(getState().openBlockStartAt).toBeNull();
    expect(getState().openProjectKey).toBeNull();
  });
});

describe('#620 S1 D12 — Toast', () => {
  it('a live alert toast offers the same target and does not merely dismiss', () => {
    updateSnapshot(baseEnv() as unknown as Envelope);
    dispatch({
      type: 'SHOW_ALERT_TOAST',
      alert: alert({ axis: 'weekly', context: { week_start_at: LIVE_WEEK_START } }),
    });
    const { container } = render(<Toast />);

    const button = container.querySelector('.alert-row-open');
    expectNativelyOperable(button);
    fireEvent.click(button as HTMLButtonElement);
    expect(getState().openModal).toBe('current-week');
    // Following the toast also retires it — leaving it over the modal it just
    // opened would cover the answer it led to.
    expect(getState().toast).toBeNull();
  });

  it('a closed-window toast states why and opens nothing', () => {
    updateSnapshot(baseEnv() as unknown as Envelope);
    dispatch({
      type: 'SHOW_ALERT_TOAST',
      alert: alert({ axis: 'weekly', context: { week_start_at: CLOSED_WEEK_START } }),
    });
    const { container } = render(<Toast />);

    expect(container.querySelector('.alert-row-open')).toBeNull();
    expect(container.querySelector('.alert-row-withheld')?.textContent)
      .toBe(CLOSED_WINDOW_REASON);
    expect(getState().openModal).toBeNull();
  });
});

function envWithClaudeBudget(verdict: 'ok' | 'warn' | 'over'): Envelope {
  const env = baseEnv();
  const claude = (env as unknown as {
    sources: { claude: { data: { budget: Record<string, unknown> } } };
  }).sources.claude;
  claude.data.budget = {
    ...(claude.data.budget as Record<string, unknown>),
    status: {
      period: 'subscription-week',
      budget_usd: 300,
      spent_usd: 285,
      remaining_usd: 15,
      consumption_pct: 95,
      verdict,
      low_confidence: false,
      window_start_at: LIVE_WEEK_START,
      window_end_at: '2026-04-28T00:00:00Z',
      recent_24h_usd: 40,
      alert_thresholds: [90, 100],
      pace: {
        daily_usd: 40,
        projected_low_usd: 300,
        projected_high_usd: 320,
        week_avg_projection_usd: 310,
      },
    },
  };
  return env as unknown as Envelope;
}

describe('#620 S1 D12 — BudgetBlock warn and over', () => {
  it.each(['warn', 'over'] as const)('a %s budget opens the period it measures', (verdict) => {
    updateSnapshot(envWithClaudeBudget(verdict));
    const { container } = render(
      <BudgetComposition env={envWithClaudeBudget(verdict)} selection="claude" surface="panel" />,
    );

    const button = container.querySelector('.budget-explain');
    expectNativelyOperable(button);
    fireEvent.click(button as HTMLButtonElement);
    expect(getState().openModal).toBe('current-week');
  });

  it('an ok budget offers nothing to follow', () => {
    updateSnapshot(envWithClaudeBudget('ok'));
    const { container } = render(
      <BudgetComposition env={envWithClaudeBudget('ok')} selection="claude" surface="panel" />,
    );
    expect(container.querySelector('.budget-explain')).toBeNull();
    expect(getState().openModal).toBeNull();
  });
});

describe('#620 S1 D12 — ForecastModal warning verdict', () => {
  function envWithVerdict(verdict: 'ok' | 'cap' | 'capped'): Envelope {
    const env = baseEnv();
    (env as unknown as { forecast: Record<string, unknown> }).forecast = {
      ...((env as unknown as { forecast: Record<string, unknown> }).forecast),
      verdict,
    };
    return env as unknown as Envelope;
  }

  it.each(['cap', 'capped'] as const)('a %s verdict opens the week it warns about', (verdict) => {
    updateSnapshot(envWithVerdict(verdict));
    dispatch({ type: 'OPEN_MODAL', kind: 'forecast' });
    const { container } = render(<ForecastModal />);

    const button = container.querySelector('.mfc-explain');
    expectNativelyOperable(button);
    fireEvent.click(button as HTMLButtonElement);
    expect(getState().openModal).toBe('current-week');
  });

  it('a calm forecast offers nothing to follow', () => {
    updateSnapshot(envWithVerdict('ok'));
    dispatch({ type: 'OPEN_MODAL', kind: 'forecast' });
    const { container } = render(<ForecastModal />);
    expect(container.querySelector('.mfc-explain')).toBeNull();
    expect(getState().openModal).toBe('forecast');
  });
});
