// #278 Theme A §1.4 — per-panel hydrating skeletons. During the cheap
// first-paint seed (hydrating=true) an empty heavy panel must render a loading
// skeleton, NOT its definitive empty/"unavailable" copy (which looks broken).
// JSDOM can't evaluate @media/scroll/CSS specificity — the real-browser check
// is the ui-qa gate; this only asserts the render-branch selection.
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ProjectsPanel } from './ProjectsPanel';
import { SessionsPanel } from './SessionsPanel';
import { TrendPanel } from './TrendPanel';
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
    // Target the skeleton's role="status" element specifically: the header
    // sub-label now also reads "(loading)" (matches /Loading/i), so a text
    // query would find two elements.
    expect(screen.getByRole('status')).toBeInTheDocument();
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
    // role="status" targets the skeleton specifically — the header sub-label
    // now also reads "(loading)" (matches /Loading/i).
    expect(screen.getByRole('status')).toBeInTheDocument();
  });

  it('SessionsPanel does NOT render a skeleton when not hydrating (empty is a real steady state)', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: false });
    render(<SessionsPanel />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByText(/Loading/i)).toBeNull();
  });
});

// #278 Theme A (ui-qa P3) — hydration-aware panel HEADERS. During the cheap
// first-paint seed the Projects / Trend / Sessions header sub-labels used to
// show their final-state copy ("(unavailable)" / "(0 weeks)" / "(0 total)"),
// which reads as a broken instance. They must mirror CacheReportPanel and show
// "(loading)" while hydrating+empty, then flip to the real count once hydrated.
// The literal parens target the header sub-span specifically (PanelSkeleton's
// body copy is "Loading…", no parens).
describe('per-panel hydrating headers (#278)', () => {
  it('ProjectsPanel header shows "(loading)" (not "(unavailable)") when hydrating and empty', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: true });
    render(<ProjectsPanel />);
    expect(screen.getByText('(loading)')).toBeInTheDocument();
    expect(screen.queryByText('(unavailable)')).toBeNull();
  });

  it('ProjectsPanel header shows the real count when hydrated', () => {
    updateSnapshot({
      ...baseEnvelope(),
      hydrating: false,
      projects: {
        current_week: {
          week_label: 'wk May 13',
          week_start_date: null,
          week_start_at: null,
          total_cost_usd: 10,
          rows: [
            {
              key: 'proj-a',
              bucket_path: '/proj-a',
              cost_usd: 10,
              attributed_pct: 100,
              sessions_count: 1,
            },
          ],
        },
        trend: { window_weeks: 12, weeks: [], projects: [] },
      },
    });
    render(<ProjectsPanel />);
    expect(screen.getByText('(1 this week)')).toBeInTheDocument();
    expect(screen.queryByText('(loading)')).toBeNull();
  });

  it('TrendPanel header shows "(loading)" (not "(0 weeks)") when hydrating and empty', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: true });
    render(<TrendPanel />);
    expect(screen.getByText('(loading)')).toBeInTheDocument();
    expect(screen.queryByText(/0 weeks/i)).toBeNull();
  });

  it('TrendPanel header shows the real week count when hydrated', () => {
    updateSnapshot({
      ...baseEnvelope(),
      hydrating: false,
      trend: {
        weeks: [
          { label: 'w1', used_pct: 10, dollar_per_pct: 1, delta: null, is_current: true },
        ],
        spark_heights: [1],
        history: [],
      },
    });
    render(<TrendPanel />);
    expect(screen.getByText('(1 week)')).toBeInTheDocument();
    expect(screen.queryByText('(loading)')).toBeNull();
  });

  it('SessionsPanel header shows "(loading)" (not "(0 total)") when hydrating and empty', () => {
    updateSnapshot({ ...baseEnvelope(), hydrating: true });
    render(<SessionsPanel />);
    expect(screen.getByText('(loading)')).toBeInTheDocument();
    expect(screen.queryByText(/0 total/i)).toBeNull();
  });

  it('SessionsPanel header shows the real total when hydrated', () => {
    updateSnapshot({
      ...baseEnvelope(),
      hydrating: false,
      sessions: { total: 2, sort_key: 'started_desc', rows: [] },
    });
    render(<SessionsPanel />);
    expect(screen.getByText('(2 total)')).toBeInTheDocument();
    expect(screen.queryByText('(loading)')).toBeNull();
  });
});
