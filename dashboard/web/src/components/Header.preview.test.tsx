import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Header } from './Header';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

// Minimal-but-valid envelope carrying only the fields the Header (and its
// child chips) read. Mirrors the seeding idiom in HeroStrip.test.tsx:
// _resetForTests() then updateSnapshot(env). The preview badge gates on the
// additive-optional `channel` field, so each test toggles just that.
function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-07-03T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk Jul 03',
      used_pct: 11,
      five_hour_pct: 8,
      dollar_per_pct: 23.4,
      forecast_pct: 31,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

describe('Header preview badge', () => {
  beforeEach(() => {
    _resetForTests();
  });

  it('shows the PREVIEW pill when channel === "preview"', () => {
    updateSnapshot({ ...baseEnvelope(), channel: 'preview' });
    render(<Header />);
    const badge = screen.getByText('PREVIEW');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveClass('preview-badge');
  });

  it('hides the PREVIEW pill when channel is absent', () => {
    updateSnapshot(baseEnvelope());
    render(<Header />);
    expect(screen.queryByText('PREVIEW')).toBeNull();
  });
});
