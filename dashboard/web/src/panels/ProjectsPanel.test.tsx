// ProjectsPanel — top-5 horizontal-bar leaderboard with cross-nav to
// the modal pre-expanded on row click; panel-chrome click opens
// un-targeted. Empty states for null envelope / empty rows / null
// attributed_pct. See plan §4 Step 1.
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ProjectsPanel } from './ProjectsPanel';
import {
  _resetForTests,
  getState,
  updateSnapshot,
} from '../store/store';
import type { Envelope, ProjectsEnvelope } from '../types/envelope';

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
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [] },
  };
}

function envelopeWithProjects(rowCount: number): Envelope {
  const env = baseEnvelope();
  const projects: ProjectsEnvelope = {
    current_week: {
      week_label: 'wk May 13',
      week_start_date: '2026-05-13',
      week_start_at: '2026-05-13T00:00:00Z',
      total_cost_usd: 47.61,
      rows: Array.from({ length: rowCount }, (_, i) => ({
        key: `project-${i + 1}`,
        bucket_path: `/repos/project-${i + 1}`,
        cost_usd: (rowCount - i) * 5.0,
        attributed_pct: (rowCount - i) * 3.0,
        sessions_count: 5,
      })),
    },
    trend: { window_weeks: 4, weeks: [], projects: [] },
  };
  env.projects = projects;
  return env;
}

describe('<ProjectsPanel />', () => {
  it('renders top-5 rows when there are exactly 5 projects', () => {
    updateSnapshot(envelopeWithProjects(5));
    render(<ProjectsPanel />);
    const rows = screen.getAllByRole('button', { name: /Open Projects modal for/ });
    expect(rows).toHaveLength(5);
  });

  it('renders top-5 + tail row when more than 5 projects', () => {
    updateSnapshot(envelopeWithProjects(8));
    render(<ProjectsPanel />);
    // 5 clickable rows.
    const rows = screen.getAllByRole('button', { name: /Open Projects modal for/ });
    expect(rows).toHaveLength(5);
    // Tail row with "+3 more".
    expect(screen.getByText(/\+3 more/)).toBeInTheDocument();
  });

  it('renders the "no project activity yet" panel-empty when rows array is empty', () => {
    const env = baseEnvelope();
    env.projects = {
      current_week: {
        week_label: 'wk May 13',
        week_start_date: null,
        week_start_at: null,
        total_cost_usd: 0,
        rows: [],
      },
      trend: { window_weeks: 0, weeks: [], projects: [] },
    };
    updateSnapshot(env);
    render(<ProjectsPanel />);
    expect(screen.getByText(/No project activity yet this week/)).toBeInTheDocument();
  });

  it('renders the "data unavailable" panel-empty when projects envelope is null', () => {
    updateSnapshot(baseEnvelope());  // projects: null already in baseEnvelope
    render(<ProjectsPanel />);
    expect(screen.getByText(/Projects data unavailable/)).toBeInTheDocument();
  });

  it('row click dispatches OPEN_MODAL with projectKey set', () => {
    updateSnapshot(envelopeWithProjects(3));
    render(<ProjectsPanel />);
    const firstRow = screen.getAllByRole('button', { name: /Open Projects modal for/ })[0];
    fireEvent.click(firstRow);
    expect(getState().openModal).toBe('projects');
    expect(getState().openProjectKey).toBe('project-1');
  });

  it('panel chrome click dispatches OPEN_MODAL un-targeted (no projectKey)', () => {
    updateSnapshot(envelopeWithProjects(3));
    render(<ProjectsPanel />);
    const panel = screen.getByRole('region', { name: /Projects panel/ });
    fireEvent.click(panel);
    expect(getState().openModal).toBe('projects');
    expect(getState().openProjectKey).toBeNull();
  });

  it('renders em-dash for null attributed_pct', () => {
    const env = baseEnvelope();
    env.projects = {
      current_week: {
        week_label: 'wk', week_start_date: null, week_start_at: null,
        total_cost_usd: 5,
        rows: [
          { key: 'a', bucket_path: '/a', cost_usd: 5, attributed_pct: null, sessions_count: 1 },
        ],
      },
      trend: { window_weeks: 0, weeks: [], projects: [] },
    };
    updateSnapshot(env);
    render(<ProjectsPanel />);
    // The em-dash MUST appear in the percent cell (not the cost cell) — assert
    // via the row's accessible name + .pct text content.
    const row = screen.getByRole('button', { name: /Open Projects modal for a/ });
    const pct = row.querySelector('.pct');
    expect(pct?.textContent).toBe('—');
  });

  it('row click does not bubble up to the panel-chrome handler', () => {
    updateSnapshot(envelopeWithProjects(3));
    render(<ProjectsPanel />);
    const row = screen.getAllByRole('button', { name: /Open Projects modal for/ })[0];
    fireEvent.click(row);
    // openProjectKey is set from the row's projectKey — would be null if
    // both row + panel handlers fired (panel handler runs last and clobbers).
    expect(getState().openProjectKey).toBe('project-1');
  });
});
