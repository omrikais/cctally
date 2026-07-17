import { render } from '@testing-library/react';
import { beforeEach, describe, it, expect } from 'vitest';
import { PanelHost } from './PanelHost';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

// Minimal-but-valid envelope so the real Sessions panel Component renders
// without throwing (mirrors the sibling-test mock shape).
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
    current_week: {
      used_pct: 11, five_hour_pct: 8, five_hour_resets_in_sec: null,
      spent_usd: 14.2, dollar_per_pct: 23.4, reset_at_utc: '2026-07-03T00:00:00Z',
      reset_in_sec: 200000, last_snapshot_age_sec: 30, milestones: [],
      freshness: null, five_hour_block: null,
    },
    forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

beforeEach(() => {
  _resetForTests();
  updateSnapshot(env());
});

describe('PanelHost data-span follows board mode', () => {
  it('sessions is span 12 in intermediate, span 6 in bento', () => {
    const { container: inter } = render(<PanelHost id="sessions" index={0} mode="intermediate" />);
    expect(inter.querySelector('[data-panel-host="sessions"]')).toHaveAttribute('data-span', '12');
    const { container: bento } = render(<PanelHost id="sessions" index={0} mode="bento" />);
    expect(bento.querySelector('[data-panel-host="sessions"]')).toHaveAttribute('data-span', '6');
  });
});
