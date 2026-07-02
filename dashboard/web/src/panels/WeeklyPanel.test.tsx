// #264 S2 — restored WeeklyPanel tile with S1 card chrome. Asserts the tile
// caps to the 3 most-recent weeks + a whole-window footer total, opens its OWN
// weekly modal (whole-section click AND the ⤢ ExpandButton), and its ShareIcon
// dispatches openShareModal('weekly').
import { afterEach, beforeEach, describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { WeeklyPanel } from './WeeklyPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { Envelope, ModelCostRow, PeriodRow } from '../types/envelope';

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 50 },
  { model: 'claude-sonnet-4-5', display: 'sonnet-4-5', chip: 'sonnet', cost_usd: 4, cost_pct: 33 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 2, cost_pct: 17 },
];

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-W27', cost_usd: 50, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 9, dollar_per_pct: 5.5, delta_cost_pct: 10, is_current: false,
    models, ...over,
  };
}

// 4 rows → proves the VISIBLE_ROWS=3 cap; total_cost_usd is the whole window.
const WEEKLY: PeriodRow[] = [
  periodRow({ label: '2026-W27', cost_usd: 55, delta_cost_pct: 9, is_current: true }),
  periodRow({ label: '2026-W26', cost_usd: 40, delta_cost_pct: -5 }),
  periodRow({ label: '2026-W25', cost_usd: 30, delta_cost_pct: 2 }),
  periodRow({ label: '2026-W24', cost_usd: 20, delta_cost_pct: -1 }),
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
    weekly: { rows: WEEKLY, total_cost_usd: 145 },
    monthly: { rows: [] },
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

describe('<WeeklyPanel /> (#264 S2)', () => {
  it('renders the cyan panel card with the bar-chart icon and recent subtitle', () => {
    render(<WeeklyPanel />);
    const section = document.getElementById('panel-weekly');
    expect(section?.classList.contains('panel')).toBe(true);
    expect(section?.classList.contains('accent-cyan')).toBe(true);
    expect(document.querySelector('#panel-weekly svg use')?.getAttribute('href'))
      .toBe('/static/icons.svg#bar-chart');
    expect(screen.getByText(/recent/i)).toBeInTheDocument();
  });

  it('caps the body to the 3 most-recent rows with a NOW pill on the current row', () => {
    render(<WeeklyPanel />);
    expect(document.querySelectorAll('#panel-weekly .period').length).toBe(3);
    expect(document.querySelectorAll('#panel-weekly .pill-current').length).toBe(1);
    expect(document.querySelector('#panel-weekly .model-stack')?.children.length).toBe(3);
  });

  it('renders the whole-window footer total (all 4 weeks)', () => {
    render(<WeeklyPanel />);
    const foot = document.querySelector('#panel-weekly .panel-foot');
    expect(foot?.textContent).toMatch(/4w total/);
    expect(foot?.textContent).toMatch(/\$145\.00/);
  });

  it('clicking the section opens the weekly modal', () => {
    const { container } = render(<WeeklyPanel />);
    (container.querySelector('#panel-weekly') as HTMLElement).click();
    expect(getState().openModal).toBe('weekly');
  });

  it('the ⤢ ExpandButton opens the weekly modal', () => {
    render(<WeeklyPanel />);
    dispatch({ type: 'CLOSE_MODAL' });
    fireEvent.click(screen.getByRole('button', { name: 'Open Weekly' }));
    expect(getState().openModal).toBe('weekly');
  });

  it('the ShareIcon dispatches openShareModal("weekly")', () => {
    render(<WeeklyPanel />);
    fireEvent.click(screen.getByRole('button', { name: /Share Weekly report/i }));
    expect(getState().shareModal?.panel).toBe('weekly');
  });
});
