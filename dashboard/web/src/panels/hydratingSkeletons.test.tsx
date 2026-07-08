// #278 Theme A §1.4 — per-panel hydrating skeletons. During the cheap
// first-paint seed (hydrating=true) an empty heavy panel must render a loading
// skeleton, NOT its definitive empty/"unavailable" copy (which looks broken).
// JSDOM can't evaluate @media/scroll/CSS specificity — the real-browser check
// is the ui-qa gate; this only asserts the render-branch selection.
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ProjectsPanel } from './ProjectsPanel';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { Envelope } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null,
      forecast_verdict: 'ok', vs_last_week_delta: null,
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

describe('per-panel hydrating skeletons (#278)', () => {
  it('ProjectsPanel renders a loading skeleton (not the "restart" copy) when hydrating and unavailable', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: true });
    render(<ProjectsPanel />);
    expect(screen.getByText(/Loading/i)).toBeInTheDocument();
    expect(screen.queryByText(/restart the dashboard/i)).toBeNull();
    expect(screen.queryByText(/Projects data unavailable/i)).toBeNull();
  });

  it('ProjectsPanel keeps the "data unavailable" empty copy when NOT hydrating', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: false });
    render(<ProjectsPanel />);
    expect(screen.getByText(/Projects data unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/Loading/i)).toBeNull();
  });

  it('SessionsPanel renders a loading skeleton when hydrating and no sessions', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: true });
    render(<SessionsPanel />);
    expect(screen.getByText(/Loading/i)).toBeInTheDocument();
  });

  it('SessionsPanel does NOT render a skeleton when not hydrating (empty is a real steady state)', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: false });
    render(<SessionsPanel />);
    expect(screen.queryByText(/Loading/i)).toBeNull();
  });
});
