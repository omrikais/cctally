// #620 S2 — following a warning into the diagnosis of its own window.
//
// Two separable claims, and each has its own recorded failure mode.
//
// **`alertFiredAt` reaches every entry point.** D3's contract is "current
// state, disclosed": the diagnosis re-measures the alert's window against live
// data, so the surface must state both the instant the warning fired and the
// instant the measurement was taken. An entry point that dropped the firing
// instant would look exactly like an alert that recorded none, which is why the
// four surfaces are enumerated here rather than sampled.
//
// **A window no existing modal can render is now offered to the diagnosis.**
// S1 could only say why it opened nothing, because `CurrentWeekModal` always
// opens the live week and `PeriodModal` clamps to the current row. The sentence
// stays — the week genuinely is not opened here — and the diagnosis is offered
// beside it, because `/api/diagnosis` is keyed by explicit half-open bounds.
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
  EXPLAIN_WINDOW_LABEL,
  NO_BLOCK_IDENTITY_REASON,
  NO_CALENDAR_WEEK_SURFACE_REASON,
  VENDOR_WIDE_PROJECT_REASON,
  alertNavigation,
} from '../src/lib/alertScope';
import fixture from './fixtures/envelope.json';
import type { AlertEntry, Envelope } from '../src/types/envelope';

const LIVE_WEEK_START = '2026-04-21T00:00:00Z';
const CLOSED_WEEK_START = '2026-03-01T00:00:00Z';
const FIRED_AT = '2026-04-24T12:00:00Z';

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
    alerted_at: FIRED_AT,
    context: {},
    ...over,
  } as AlertEntry;
}

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

function envWithForecastVerdict(verdict: 'ok' | 'cap' | 'capped'): Envelope {
  const env = baseEnv();
  (env as unknown as { forecast: Record<string, unknown> }).forecast = {
    ...((env as unknown as { forecast: Record<string, unknown> }).forecast),
    verdict,
  };
  return env as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

// ─── the scope-derived fields on the target itself ──────────────────────────

describe('#620 S2 — AlertTarget carries the scope the diagnosis needs', () => {
  it('carries alertFiredAt from the envelope row into the target', () => {
    const nav = alertNavigation(
      alert({ axis: 'weekly', context: { week_start_at: LIVE_WEEK_START } }),
      baseEnv() as unknown as Envelope,
      new Date('2026-04-24T13:07:00Z'),
    );
    expect(nav.target?.alertFiredAt).toBe(FIRED_AT);
  });

  it('carries the explicit half-open bounds and the account', () => {
    const nav = alertNavigation(
      alert({
        axis: 'weekly',
        accountKey: 'acct-1',
        context: { week_start_at: LIVE_WEEK_START },
      }),
      baseEnv() as unknown as Envelope,
      new Date('2026-04-24T13:07:00Z'),
    );
    expect(nav.target?.windowStartAt).toBe('2026-04-21T00:00:00.000Z');
    expect(nav.target?.windowEndAt).toBe('2026-04-28T00:00:00.000Z');
    expect(nav.target?.accountKey).toBe('acct-1');
  });

  it('reports an unrecorded firing instant as absent, not as an empty time', () => {
    // `BudgetExplain` and the forecast's cap affordance synthesize an entry
    // with an empty `alerted_at`, because nothing fired.
    const nav = alertNavigation(
      alert({ axis: 'weekly', alerted_at: '', context: { week_start_at: LIVE_WEEK_START } }),
      baseEnv() as unknown as Envelope,
      new Date('2026-04-24T13:07:00Z'),
    );
    expect(nav.target?.alertFiredAt).toBeNull();
  });

  it('never passes the vendor-wide sentinel as an account key', () => {
    // `*` means "across every account", which the diagnosis expresses by
    // selecting no account at all. Sent as a ref, no registry resolves it.
    const nav = alertNavigation(
      alert({
        axis: 'project_budget',
        accountKey: '*',
        context: { week_start_at: CLOSED_WEEK_START, project_key: '/repo/alpha' },
      }),
      baseEnv() as unknown as Envelope,
      new Date('2026-04-24T13:07:00Z'),
    );
    expect(nav.explainTarget?.accountKey).toBeNull();
  });
});

// ─── the four entry points ──────────────────────────────────────────────────
//
// Enumerated rather than sampled: missing one is the recurring failure here.

describe('#620 S2 — every follow entry point delivers the scope', () => {
  it('the alerts modal row passes the firing instant and the window', () => {
    seedAlerts([alert({ axis: 'weekly', context: { week_start_at: LIVE_WEEK_START } })]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);
    fireEvent.click(container.querySelector('.alert-row-open') as HTMLButtonElement);
    expect(getState().openExplainAlertFiredAt).toBe(FIRED_AT);
    expect(getState().openExplainWindowStartAt).toBe('2026-04-21T00:00:00.000Z');
  });

  it('the toast passes the firing instant and the window', () => {
    updateSnapshot(baseEnv() as unknown as Envelope);
    dispatch({
      type: 'SHOW_ALERT_TOAST',
      alert: alert({ axis: 'weekly', context: { week_start_at: LIVE_WEEK_START } }),
    });
    const { container } = render(<Toast />);
    fireEvent.click(container.querySelector('.alert-row-open') as HTMLButtonElement);
    expect(getState().openExplainAlertFiredAt).toBe(FIRED_AT);
    expect(getState().openExplainWindowStartAt).toBe('2026-04-21T00:00:00.000Z');
  });

  it('the budget block passes the window it measures', () => {
    updateSnapshot(envWithClaudeBudget('over'));
    const { container } = render(
      <BudgetComposition env={envWithClaudeBudget('over')} selection="claude" surface="panel" />,
    );
    fireEvent.click(container.querySelector('.budget-explain') as HTMLButtonElement);
    // Nothing FIRED here — the block is a live warning state, not an alert row —
    // so the instant is absent rather than blank, and the window is what
    // discriminates a delivered scope from a defaulted one.
    expect(getState().openExplainWindowStartAt).toBe('2026-04-21T00:00:00.000Z');
    expect(getState().openExplainAlertFiredAt).toBeNull();
  });

  it('the forecast modal passes the week it warns about', () => {
    updateSnapshot(envWithForecastVerdict('cap'));
    dispatch({ type: 'OPEN_MODAL', kind: 'forecast' });
    const { container } = render(<ForecastModal />);
    fireEvent.click(container.querySelector('.mfc-explain') as HTMLButtonElement);
    expect(getState().openExplainWindowStartAt).toBe('2026-04-21T00:00:00.000Z');
    expect(getState().openExplainAlertFiredAt).toBeNull();
  });
});

// ─── the diagnosis of an unaddressable window ───────────────────────────────

describe('#620 S2 — a window no modal can render is offered to the diagnosis', () => {
  it.each([
    {
      name: 'a closed weekly window',
      entry: alert({ axis: 'weekly', context: { week_start_at: CLOSED_WEEK_START } }),
      reason: CLOSED_WINDOW_REASON,
      startAt: '2026-03-01T00:00:00.000Z',
    },
    {
      name: 'a five-hour alert with no recorded block start',
      entry: alert({
        axis: 'five_hour',
        context: { five_hour_window_key: Date.parse('2026-03-10T10:00:00Z') / 1000 },
      }),
      reason: NO_BLOCK_IDENTITY_REASON,
      // The retained key is the RESET instant, so the window it recovers is
      // the five hours ending there.
      startAt: '2026-03-10T05:00:00.000Z',
    },
    {
      // `docs/commands/dashboard.md` and the CHANGELOG both claim this case,
      // and it was the only one of the three withheld-with-a-window reasons
      // with no test. The dashboard's weekly views are keyed by subscription
      // week (Claude) and quota cycle (Codex), so neither is the civil week a
      // calendar-week budget measures.
      name: 'a budget measured over a calendar week',
      entry: alert({
        axis: 'budget',
        context: { period: 'calendar-week', period_start_at: LIVE_WEEK_START },
      }),
      reason: NO_CALENDAR_WEEK_SURFACE_REASON,
      startAt: '2026-04-21T00:00:00.000Z',
    },
    {
      // The other documented-but-untested case. A `project_budget` row is
      // stamped `*`, so no account's project drill can be opened — but the
      // window is fixed, and the diagnosis measures it across every account,
      // which is what the stamp means.
      name: 'a vendor-wide project budget',
      entry: alert({
        axis: 'project_budget',
        accountKey: '*',
        context: { week_start_at: LIVE_WEEK_START, project_key: 'p1' },
      }),
      reason: VENDOR_WIDE_PROJECT_REASON,
      startAt: '2026-04-21T00:00:00.000Z',
    },
  ])('$name states why, and offers the diagnosis beside it', ({ entry, reason, startAt }) => {
    seedAlerts([entry]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);

    // The S1 statement is unchanged: the week or the block is not opened here.
    expect(container.querySelector('.alert-row-open')).toBeNull();
    expect(container.querySelector('.alert-row-withheld')?.textContent).toBe(reason);

    const explain = container.querySelector('.alert-row-explain');
    expect(explain).not.toBeNull();
    expect(explain?.tagName).toBe('BUTTON');
    expect(explain?.textContent).toBe(EXPLAIN_WINDOW_LABEL);
    fireEvent.click(explain as HTMLButtonElement);
    expect(getState().openModal).toBe('explain');
    expect(getState().openExplainWindowStartAt).toBe(startAt);
    expect(getState().openExplainAlertFiredAt).toBe(FIRED_AT);
  });

  it('offers nothing when the alert fixes no window at all', () => {
    // A weekly row that retained neither a week start nor a week date derives
    // no window, and the diagnosis cannot measure one either.
    seedAlerts([alert({ axis: 'weekly', context: {} })]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);
    expect(container.querySelector('.alert-row-open')).toBeNull();
    expect(container.querySelector('.alert-row-explain')).toBeNull();
    expect(getState().openModal).toBe('alerts');
  });

  it('offers no second button where the primary target resolved', () => {
    seedAlerts([alert({ axis: 'weekly', context: { week_start_at: LIVE_WEEK_START } })]);
    dispatch({ type: 'OPEN_MODAL', kind: 'alerts' });
    const { container } = render(<RecentAlertsModal />);
    expect(container.querySelector('.alert-row-open')).not.toBeNull();
    expect(container.querySelector('.alert-row-explain')).toBeNull();
  });
});
