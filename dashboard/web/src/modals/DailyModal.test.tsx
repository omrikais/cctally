// Modal-level integration test for the S2 DailyModal (#264). Splits off the
// day half of the former HistoryModal test: Daily renders navigator + detail
// only (no Day·Week·Month toggle, no sortable table), seeds from an
// openDailyDate deep-link, and steps the navigator with ↑/↓.
import { afterEach, beforeEach, describe, it, expect } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { DailyModal } from './DailyModal';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import type { DailyPanelRow, Envelope, ModelCostRow, PeriodRow } from '../types/envelope';

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
  periodRow({ label: '2026-W27', is_current: true }),
];
const MONTHLY: PeriodRow[] = [
  periodRow({ label: '2026-07', used_pct: null, dollar_per_pct: null, is_current: true }),
];

function baseEnvelope(over: { daily?: DailyPanelRow[] } = {}): Envelope {
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
    weekly: { rows: WEEKLY },
    monthly: { rows: MONTHLY },
    blocks: { rows: [] },
    daily: { rows: over.daily ?? DAILY, quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as unknown as Envelope;
}

function openDaily(dailyDate?: string): void {
  dispatch({ type: 'OPEN_MODAL', kind: 'daily', ...(dailyDate ? { dailyDate } : {}) });
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

describe('<DailyModal /> (#264 S2)', () => {
  it('renders the navigator + detail, and NO toggle and NO sortable table', () => {
    updateSnapshot(baseEnvelope());
    openDaily();
    render(<DailyModal />);
    // No Day·Week·Month toggle survives the split.
    expect(document.querySelector('.history-toggle')).toBeNull();
    // Daily has no sortable period table.
    expect(document.querySelector('.history-table')).toBeNull();
    expect(document.querySelector('.period-two-pane')).toBeNull();
    // Navigator + detail render; today (07-01) is selected by default.
    expect(document.querySelector('.bar[data-key="2026-06-30"]')).not.toBeNull();
    expect(document.querySelector('.detail-card')?.textContent).toContain('07-01');
  });

  it('an openDailyDate deep-link selects that date (not today)', () => {
    updateSnapshot(baseEnvelope());
    openDaily('2026-06-30');
    render(<DailyModal />);
    const detail = document.querySelector('.detail-card');
    expect(detail?.textContent).toContain('06-30');
    expect(detail?.textContent).not.toContain('07-01');
  });

  it('ArrowDown / ArrowUp step the navigator selection', () => {
    updateSnapshot(baseEnvelope());
    openDaily();
    render(<DailyModal />);
    expect(document.querySelector('.detail-card')?.textContent).toContain('07-01');
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.querySelector('.detail-card')?.textContent).toContain('06-30');
    fireEvent.keyDown(document, { key: 'ArrowUp' });
    expect(document.querySelector('.detail-card')?.textContent).toContain('07-01');
  });
});
