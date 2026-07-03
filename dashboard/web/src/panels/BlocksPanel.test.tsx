import { render, screen } from '@testing-library/react';
import { beforeEach, describe, it, expect } from 'vitest';
import { BlocksPanel } from './BlocksPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { BlocksPanelRow, Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

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
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function blockRow(over: Partial<BlocksPanelRow>): BlocksPanelRow {
  return {
    start_at: '2026-07-01T00:00:00Z', end_at: '2026-07-01T05:00:00Z',
    anchor: 'recorded', is_active: false, cost_usd: 2.0, models: [],
    label: 'Block', ...over,
  };
}

describe('BlocksPanel uncap (#264 S4 A2)', () => {
  it('renders every block row (no 3-cap) so all are reachable via scroll', () => {
    const rows = Array.from({ length: 6 }, (_, i) => blockRow({
      start_at: `2026-07-0${i + 1}T00:00:00Z`,
      end_at: `2026-07-0${i + 1}T05:00:00Z`,
      label: `Block ${i}`,
      cost_usd: (i + 1) * 2,
    }));
    const env = baseEnvelope();
    env.blocks = { rows, total_cost_usd: 42 };
    updateSnapshot(env);
    render(<BlocksPanel />);
    expect(screen.getAllByText(/Block \d/)).toHaveLength(6);
  });
});

describe('BlocksPanel empty-week ⤢ (#265 D)', () => {
  it('disables the expand button when there are no blocks this week', () => {
    updateSnapshot(baseEnvelope()); // blocks.rows === []
    const { container } = render(<BlocksPanel />);
    const expand = container.querySelector('.panel-expand') as HTMLButtonElement;
    expect(expand).not.toBeNull();
    expect(expand.disabled).toBe(true);
  });

  it('leaves the expand button enabled when the week has blocks', () => {
    const env = baseEnvelope();
    env.blocks = { rows: [blockRow({})], total_cost_usd: 2 };
    updateSnapshot(env);
    const { container } = render(<BlocksPanel />);
    expect((container.querySelector('.panel-expand') as HTMLButtonElement).disabled).toBe(false);
  });
});
