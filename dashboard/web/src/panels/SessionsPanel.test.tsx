import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope, SessionRow } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
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

function sessRow(over: Partial<SessionRow>): SessionRow {
  return {
    session_id: 's1', started_utc: '2026-05-13T09:00:00Z', duration_min: 12,
    model: 'claude-opus-4-8', project: 'p', project_key: 'p', cost_usd: 1.0, ...over,
  };
}

describe('SessionsPanel project-cell title (#207 C4)', () => {
  it('puts the full project name in the resolved button title', () => {
    const env = baseEnvelope();
    const long = 'a-very-long-monorepo-project-key-that-would-truncate';
    env.sessions = { total: 1, sort_key: 'started_desc',
      rows: [sessRow({ project: long, project_key: long })] };
    updateSnapshot(env);
    render(<SessionsPanel />);
    const btn = screen.getByRole('button', { name: `Open Projects modal for ${long}` });
    expect(btn).toHaveAttribute('title', long);
  });
});
