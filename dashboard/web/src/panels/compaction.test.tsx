// #248 Task 4 — Weekly / Monthly / Blocks compaction. The two-tier grid puts
// these three in the uniform tile row, so each panel body caps to the ~3 most-
// recent rows while the envelope (and the drill-in modal) keep the full history.
// The cap is the PANEL's, not the data's — these tests feed 8 rows and assert
// the body renders ≤3 while the store snapshot still carries all 8.
import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { WeeklyPanel } from './WeeklyPanel';
import { MonthlyPanel } from './MonthlyPanel';
import { BlocksPanel } from './BlocksPanel';
import { _resetForTests, getState, updateSnapshot } from '../store/store';
import type { BlocksPanelRow, Envelope, PeriodRow } from '../types/envelope';

function periodRow(label: string, cost: number): PeriodRow {
  return {
    label,
    cost_usd: cost,
    total_tokens: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    used_pct: null,
    dollar_per_pct: null,
    delta_cost_pct: 0.1,
    is_current: false,
    models: [{ model: 'm', display: 'opus', chip: 'opus', cost_usd: cost, cost_pct: 100 }],
  };
}

function blocksRow(start: string, label: string, cost: number): BlocksPanelRow {
  return {
    start_at: start,
    end_at: start,
    anchor: 'recorded',
    is_active: false,
    cost_usd: cost,
    label,
    models: [{ model: 'm', display: 'opus', chip: 'opus', cost_usd: cost, cost_pct: 100 }],
  };
}

const PERIODS: PeriodRow[] = Array.from({ length: 8 }, (_, i) => periodRow(`P${i}`, (i + 1) * 10));
const BLOCKS: BlocksPanelRow[] = Array.from({ length: 8 }, (_, i) =>
  blocksRow(`2026-06-${10 + i}T00:00:00Z`, `B${i}`, (i + 1) * 5),
);

function env(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-06-30T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk Jun 30', used_pct: 11, five_hour_pct: 8,
      dollar_per_pct: 23.4, forecast_pct: 31, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: PERIODS, total_cost_usd: 360 },
    monthly: { rows: PERIODS, total_cost_usd: 360 },
    blocks: { rows: BLOCKS, total_cost_usd: 180 },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  updateSnapshot(env());
});

describe('#248 Task 4 — Weekly/Monthly/Blocks compact to ≤3 rows', () => {
  it('WeeklyPanel renders ≤3 .period rows while the envelope carries 8', () => {
    const { container } = render(<WeeklyPanel />);
    expect(container.querySelectorAll('.period').length).toBeLessThanOrEqual(3);
    // The data is untouched — the modal drill still gets the full history.
    expect(getState().snapshot?.weekly?.rows?.length).toBe(8);
  });

  it('MonthlyPanel renders ≤3 .period rows while the envelope carries 8', () => {
    const { container } = render(<MonthlyPanel />);
    expect(container.querySelectorAll('.period').length).toBeLessThanOrEqual(3);
    expect(getState().snapshot?.monthly?.rows?.length).toBe(8);
  });

  it('BlocksPanel renders ≤3 .blocks-row rows while the envelope carries 8', () => {
    const { container } = render(<BlocksPanel />);
    expect(container.querySelectorAll('.blocks-row').length).toBeLessThanOrEqual(3);
    expect(getState().snapshot?.blocks?.rows?.length).toBe(8);
  });

  it('the compacted panels render the 3 MOST-RECENT rows (slice from the head)', () => {
    const { container } = render(<WeeklyPanel />);
    const labels = Array.from(container.querySelectorAll('.period .label')).map((n) =>
      (n.textContent ?? '').replace('Now', '').trim(),
    );
    expect(labels).toEqual(['P0', 'P1', 'P2']);
  });
});
