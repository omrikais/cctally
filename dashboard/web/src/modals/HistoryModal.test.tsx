// Modal-level integration test for the S8 History modal (#254). This is
// the key parent-wiring guard (per the "test parent wiring, not just a
// child callback" lesson): it drives the real store + keymap and asserts
// the Day·Week·Month toggle swaps BOTH the dataset AND the variant-
// specific columns/stats, so a broken parent-wiring regression fails RED.
import { afterEach, beforeEach, describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { HistoryModal } from './HistoryModal';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import type {
  DailyPanelRow,
  Envelope,
  ModelCostRow,
  PeriodRow,
} from '../types/envelope';

const models: ModelCostRow[] = [
  { model: 'claude-opus-4-8', display: 'opus-4-8', chip: 'opus', cost_usd: 6, cost_pct: 60 },
  { model: 'claude-haiku-4-5', display: 'haiku-4-5', chip: 'haiku', cost_usd: 4, cost_pct: 40 },
];

function dailyRow(over: Partial<DailyPanelRow>): DailyPanelRow {
  return {
    date: '2026-07-01', label: '07-01', cost_usd: 12, is_today: false,
    intensity_bucket: 3, models,
    input_tokens: 10, output_tokens: 5, cache_creation_tokens: 2,
    cache_read_tokens: 1, total_tokens: 18, cache_hit_pct: 50, ...over,
  };
}

function periodRow(over: Partial<PeriodRow>): PeriodRow {
  return {
    label: '2026-W27', cost_usd: 50, total_tokens: 100, input_tokens: 40,
    output_tokens: 30, cache_creation_tokens: 20, cache_read_tokens: 10,
    used_pct: 9, dollar_per_pct: 5.5, delta_cost_pct: 10, is_current: false,
    models, ...over,
  };
}

const DAILY: DailyPanelRow[] = [
  dailyRow({ date: '2026-07-01', label: '07-01', cost_usd: 12, is_today: true }),
  dailyRow({ date: '2026-06-30', label: '06-30', cost_usd: 8 }),
  dailyRow({ date: '2026-06-29', label: '06-29', cost_usd: 5 }),
];

const WEEKLY: PeriodRow[] = [
  periodRow({ label: '2026-W27', week_start_at: '2026-06-29T00:00:00Z', week_end_at: '2026-07-06T00:00:00Z', cost_usd: 55, used_pct: 9, dollar_per_pct: 6.1, delta_cost_pct: 10, is_current: true }),
  periodRow({ label: '2026-W26', week_start_at: '2026-06-22T00:00:00Z', week_end_at: '2026-06-29T00:00:00Z', cost_usd: 40, used_pct: 7, dollar_per_pct: 5.7, delta_cost_pct: -5 }),
];

const MONTHLY: PeriodRow[] = [
  periodRow({ label: '2026-07', cost_usd: 120, used_pct: null, dollar_per_pct: null, delta_cost_pct: 20, is_current: true }),
  periodRow({ label: '2026-06', cost_usd: 200, used_pct: null, dollar_per_pct: null, delta_cost_pct: -10 }),
];

function baseEnvelope(over: {
  daily?: DailyPanelRow[];
  weekly?: PeriodRow[];
  monthly?: PeriodRow[];
} = {}): Envelope {
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
    monthly: { rows: over.monthly ?? MONTHLY },
    blocks: { rows: [] },
    daily: { rows: over.daily ?? DAILY, quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as unknown as Envelope;
}

function openHistory(dailyDate?: string): void {
  dispatch({ type: 'OPEN_MODAL', kind: 'history', ...(dailyDate ? { dailyDate } : {}) });
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

describe('<HistoryModal /> toggle + dataset wiring', () => {
  it('defaults to Day (from prefs.historyPeriod) and renders the day dataset without the weekly table columns', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    expect(screen.getByRole('radio', { name: 'Day' })).toHaveAttribute('aria-checked', 'true');
    // Day has no sortable table → no Used% / $/1% column headers.
    expect(screen.queryByRole('columnheader', { name: '$/1%' })).toBeNull();
    // The day detail renders (today's row selected by default).
    const detail = document.querySelector('.detail-card');
    expect(detail?.textContent).toContain('07-01');
  });

  it('clicking Week swaps to the weekly dataset AND shows Used % / $/1% columns', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    fireEvent.click(screen.getByRole('radio', { name: 'Week' }));
    expect(screen.getByRole('radio', { name: 'Week' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('columnheader', { name: 'Used %' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: '$/1%' })).toBeInTheDocument();
  });

  it('clicking Month swaps to monthly AND the $/1% column is absent (non-vacuity guard)', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    fireEvent.click(screen.getByRole('radio', { name: 'Month' }));
    expect(screen.getByRole('radio', { name: 'Month' })).toHaveAttribute('aria-checked', 'true');
    // Monthly correctly drops the weekly-only percent columns (WM-1).
    expect(screen.queryByRole('columnheader', { name: '$/1%' })).toBeNull();
    expect(screen.queryByRole('columnheader', { name: 'Used %' })).toBeNull();
    // But the monthly table still renders (Cost column present).
    expect(screen.getByRole('columnheader', { name: 'Cost (USD)' })).toBeInTheDocument();
  });
});

describe('<HistoryModal /> navigator + keyboard selection', () => {
  it('clicking an older navigator bar updates the detail card', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    // Default selection = today (07-01, $12.00).
    expect(document.querySelector('.detail-card')?.textContent).toContain('07-01');
    // Click the 06-30 bar (data-key is the date).
    const bar = document.querySelector('.bar[data-key="2026-06-30"]') as HTMLButtonElement;
    expect(bar).not.toBeNull();
    fireEvent.click(bar);
    expect(document.querySelector('.detail-card')?.textContent).toContain('06-30');
  });

  it('ArrowDown steps the selection over the ordered list', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    expect(document.querySelector('.detail-card')?.textContent).toContain('07-01');
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    // Next (older) day is 06-30.
    expect(document.querySelector('.detail-card')?.textContent).toContain('06-30');
    fireEvent.keyDown(document, { key: 'ArrowUp' });
    expect(document.querySelector('.detail-card')?.textContent).toContain('07-01');
  });

  it('ArrowDown steps the chronological navigator order even when the table is sorted the other way (P2)', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    // Switch to Week, then sort the table by cost ASC. Chronological order is
    // [W27 (newest, $55), W26 ($40)]; cost-asc reverses it to [W26, W27].
    fireEvent.click(screen.getByRole('radio', { name: 'Week' }));
    dispatch({ type: 'SET_TABLE_SORT', table: 'history', override: { column: 'cost_usd', direction: 'asc' } });
    // Default selection = current/first week W27.
    expect(document.querySelector('.detail-card')?.textContent).toContain('2026-W27');
    // ArrowDown = one step OLDER in chronological order → W26 (the adjacent
    // bar), regardless of the table rendering W26 first under the sort. Under
    // the pre-fix sorted-order stepping, W27 was the LAST sorted row so
    // ArrowDown would have been a no-op — so this assertion fails RED on the
    // old behavior.
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.querySelector('.detail-card')?.textContent).toContain('2026-W26');
  });

  it('ArrowRight switches the toggle and persists via SET_HISTORY_PERIOD', () => {
    updateSnapshot(baseEnvelope());
    openHistory();
    render(<HistoryModal />);
    expect(getState().prefs.historyPeriod).toBe('day');
    fireEvent.keyDown(document, { key: 'ArrowRight' });
    expect(getState().prefs.historyPeriod).toBe('week');
    // The rendered dataset follows: weekly columns appear.
    expect(screen.getByRole('columnheader', { name: '$/1%' })).toBeInTheDocument();
  });
});

describe('<HistoryModal /> deep-link + empty states', () => {
  it('an openDailyDate deep-link forces Day and selects that date (not today)', () => {
    // Seed prefs to Week so the deep-link override is non-vacuous.
    dispatch({ type: 'SET_HISTORY_PERIOD', period: 'week' });
    updateSnapshot(baseEnvelope());
    openHistory('2026-06-30');
    render(<HistoryModal />);
    expect(screen.getByRole('radio', { name: 'Day' })).toHaveAttribute('aria-checked', 'true');
    // The selected day is the deep-linked 06-30, NOT today (07-01).
    const detail = document.querySelector('.detail-card');
    expect(detail?.textContent).toContain('06-30');
    expect(detail?.textContent).not.toContain('07-01');
  });

  it('renders the empty state for a period with no rows', () => {
    updateSnapshot(baseEnvelope({ weekly: [] }));
    openHistory();
    render(<HistoryModal />);
    // Day has rows → not empty. Switch to Week (empty).
    fireEvent.click(screen.getByRole('radio', { name: 'Week' }));
    expect(screen.getByText(/No usage history yet/i)).toBeInTheDocument();
  });
});
