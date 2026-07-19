// Modal-level integration test for the S2 MonthlyModal (#264). Splits off the
// month half of the former HistoryModal test: Monthly opens the WIDE two-pane
// modal with a monthly table that DROPS the weekly-only Used %/$1% columns
// (non-vacuity guard), and a working ShareIcon.
import { afterEach, beforeEach, describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MonthlyModal } from './MonthlyModal';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import type { Envelope, ModelCostRow, PeriodRow } from '../types/envelope';

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 60 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 4, cost_pct: 40 },
];

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-07', cost_usd: 120, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: null, dollar_per_pct: null, delta_cost_pct: 20, is_current: false,
    models, ...over,
  };
}

const MONTHLY: PeriodRow[] = [
  periodRow({ label: '2026-07', cost_usd: 120, delta_cost_pct: 20, is_current: true }),
  periodRow({ label: '2026-06', cost_usd: 200, delta_cost_pct: -10 }),
];

function baseEnvelope(over: { monthly?: PeriodRow[] } = {}): Envelope {
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
    monthly: { rows: over.monthly ?? MONTHLY },
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
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
});
afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
});

describe('<MonthlyModal /> (#264 S2)', () => {
  it('opens the wide two-pane modal with a monthly table', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
    render(<MonthlyModal />);
    expect(document.querySelector('.modal-card.modal-wide')).not.toBeNull();
    const twoPane = document.querySelector('.period-two-pane');
    expect(twoPane).not.toBeNull();
    expect(twoPane?.querySelector('.period-detail-pane .detail-card')).not.toBeNull();
    expect(twoPane?.querySelector('.period-table-pane .history-table--monthly')).not.toBeNull();
  });

  it('DROPS the weekly-only Used % / $/1% columns (non-vacuity guard); keeps Cost', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
    render(<MonthlyModal />);
    expect(screen.queryByRole('columnheader', { name: 'Used %' })).toBeNull();
    expect(screen.queryByRole('columnheader', { name: '$/1%' })).toBeNull();
    expect(screen.getByRole('columnheader', { name: 'Cost (USD)' })).toBeInTheDocument();
  });

  it('the header ShareIcon dispatches openShareModal("monthly")', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
    render(<MonthlyModal />);
    fireEvent.click(screen.getByRole('button', { name: /Share Monthly report/i }));
    expect(getState().shareModal?.panel).toBe('monthly');
  });

  // #293 S3 — the panel slices to CAP=3 in stack mode, but the modal retains
  // every row inside the shared 8-month detail window.
  it('renders every envelope row inside the canonical 8-month window', () => {
    const MONTHLY4: PeriodRow[] = [
      periodRow({ label: '2026-07', is_current: true }),
      periodRow({ label: '2026-06' }),
      periodRow({ label: '2026-05' }),
      periodRow({ label: '2026-04' }),
    ];
    updateSnapshot(baseEnvelope({ monthly: MONTHLY4 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
    render(<MonthlyModal />);
    expect(document.querySelectorAll('.history-table--monthly tbody tr').length).toBe(4);
  });
});
