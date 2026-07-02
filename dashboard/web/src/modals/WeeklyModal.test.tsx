// Modal-level integration test for the S2 WeeklyModal (#264). Splits off the
// week half of the former HistoryModal test: Weekly opens the WIDE modal with
// the two-pane body (detail LEFT, sortable table RIGHT), the weekly-only
// Used %/$1% columns, and a working ShareIcon.
import { afterEach, beforeEach, describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { WeeklyModal } from './WeeklyModal';
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
    label: '2026-W27', cost_usd: 50, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 9, dollar_per_pct: 5.5, delta_cost_pct: 10, is_current: false,
    models, ...over,
  };
}

const WEEKLY: PeriodRow[] = [
  periodRow({ label: '2026-W27', week_start_at: '2026-06-29T00:00:00Z', week_end_at: '2026-07-06T00:00:00Z', cost_usd: 55, used_pct: 9, dollar_per_pct: 6.1, delta_cost_pct: 10, is_current: true }),
  periodRow({ label: '2026-W26', week_start_at: '2026-06-22T00:00:00Z', week_end_at: '2026-06-29T00:00:00Z', cost_usd: 40, used_pct: 7, dollar_per_pct: 5.7, delta_cost_pct: -5 }),
];

function baseEnvelope(over: { weekly?: PeriodRow[] } = {}): Envelope {
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
    weekly: { rows: over.weekly ?? WEEKLY },
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
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
});
afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
});

describe('<WeeklyModal /> (#264 S2)', () => {
  it('opens the wide modal with a two-pane body (detail left, weekly table right)', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    render(<WeeklyModal />);
    // Wide two-pane variant.
    expect(document.querySelector('.modal-card.modal-wide')).not.toBeNull();
    const twoPane = document.querySelector('.period-two-pane');
    expect(twoPane).not.toBeNull();
    expect(twoPane?.querySelector('.period-detail-pane .detail-card')).not.toBeNull();
    expect(twoPane?.querySelector('.period-table-pane .history-table--weekly')).not.toBeNull();
    // No toggle survives.
    expect(document.querySelector('.history-toggle')).toBeNull();
  });

  it('shows the weekly-only Used % / $/1% table columns', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    render(<WeeklyModal />);
    expect(screen.getByRole('columnheader', { name: 'Used %' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '$/1%' })).toBeInTheDocument();
  });

  it('the header ShareIcon dispatches openShareModal("weekly")', () => {
    updateSnapshot(baseEnvelope());
    dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
    render(<WeeklyModal />);
    fireEvent.click(screen.getByRole('button', { name: /Share Weekly report/i }));
    expect(getState().shareModal?.panel).toBe('weekly');
  });
});
