// #264 S2 — restored MonthlyPanel tile with S1 card chrome. Asserts the tile
// caps to the 3 most-recent months + a whole-window footer total, opens its OWN
// monthly modal (whole-section click AND the ⤢ ExpandButton), and its ShareIcon
// dispatches openShareModal('monthly').
import { afterEach, beforeEach, describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MonthlyPanel } from './MonthlyPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { Envelope, ModelCostRow, PeriodRow } from '../types/envelope';

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 50 },
  { model: 'claude-sonnet-4-5', display: 'sonnet-4-5', chip: 'sonnet', cost_usd: 4, cost_pct: 33 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 2, cost_pct: 17 },
];

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-07', cost_usd: 120, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: null, dollar_per_pct: null, delta_cost_pct: 20, is_current: false,
    models, ...over,
  };
}

// 4 rows → proves the VISIBLE_ROWS=3 cap; total_cost_usd is the whole window.
const MONTHLY: PeriodRow[] = [
  periodRow({ label: '2026-07', cost_usd: 120, delta_cost_pct: 20, is_current: true }),
  periodRow({ label: '2026-06', cost_usd: 200, delta_cost_pct: -10 }),
  periodRow({ label: '2026-05', cost_usd: 150, delta_cost_pct: 5 }),
  periodRow({ label: '2026-04', cost_usd: 90, delta_cost_pct: -3 }),
];

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-07-01T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jul 1', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] },
    monthly: { rows: MONTHLY, total_cost_usd: 560 },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as unknown as Envelope;
}

beforeEach(() => {
  _resetForTests();
  updateSnapshot(baseEnvelope());
});
afterEach(() => {
  _resetForTests();
});

describe('<MonthlyPanel /> (#264 S2)', () => {
  it('renders the pink panel card with the calendar icon and recent subtitle', () => {
    render(<MonthlyPanel />);
    const section = document.getElementById('panel-monthly');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-pink')).toBe(true);
    expect(document.querySelector('#panel-monthly svg use')?.getAttribute('href'))
      .toBe('/static/icons.svg#calendar');
    expect(screen.getByText(/recent/i)).toBeInTheDocument();
  });

  it('caps the body to the 3 most-recent rows with a NOW pill on the current row', () => {
    render(<MonthlyPanel />);
    expect(document.querySelectorAll('#panel-monthly .period').length).toBe(3);
    expect(document.querySelectorAll('#panel-monthly .pill-current').length).toBe(1);
    expect(document.querySelector('#panel-monthly .model-stack')?.children.length).toBe(3);
  });

  it('renders the whole-window footer total (all 4 months)', () => {
    render(<MonthlyPanel />);
    const foot = document.querySelector('#panel-monthly .panel-foot');
    expect(foot?.textContent).toMatch(/4mo total/);
    expect(foot?.textContent).toMatch(/\$560\.00/);
  });

  it('clicking the section opens the monthly modal', () => {
    const { container } = render(<MonthlyPanel />);
    (container.querySelector('#panel-monthly') as HTMLElement).click();
    expect(getState().openModal).toBe('monthly');
  });

  it('the ⤢ ExpandButton opens the monthly modal', () => {
    render(<MonthlyPanel />);
    dispatch({ type: 'CLOSE_MODAL' });
    fireEvent.click(screen.getByRole('button', { name: 'Open Monthly' }));
    expect(getState().openModal).toBe('monthly');
  });

  it('the ShareIcon dispatches openShareModal("monthly")', () => {
    render(<MonthlyPanel />);
    fireEvent.click(screen.getByRole('button', { name: /Share Monthly report/i }));
    expect(getState().shareModal?.panel).toBe('monthly');
  });
});
