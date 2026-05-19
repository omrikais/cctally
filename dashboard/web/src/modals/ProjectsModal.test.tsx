// ProjectsModal — covers window pills + Y-axis toggle, table sort,
// drill-on-click, "Showing N weeks" notice when actual < requested,
// drill-session cross-nav to SessionModal (replace pattern), and
// share-icon `windowWeeks` param plumbing (spec §3, §4.1, §7.3, plan
// Task 5 Step 1).
//
// Mirrors the patterns in panels/ProjectsPanel.test.tsx — uses the
// real store via `_resetForTests` + `updateSnapshot` rather than a
// helper that doesn't exist in this codebase (the plan's
// `renderWithStore` shim).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProjectsModal } from './ProjectsModal';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../store/store';
import type {
  Envelope,
  ProjectDetail,
  ProjectsEnvelope,
  ProjectsTrendProject,
} from '../types/envelope';

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

interface BuildOpts {
  windowWeeks: number;
  projectCount?: number;
  // When set, the trend's `window_weeks` is the SMALLER of these two;
  // the table's first/last columns scale by `windowWeeks`. Pass
  // `actualWeeks < windowWeeks` to exercise the "Showing N weeks"
  // notice. Defaults to `windowWeeks` when unset.
  actualWeeks?: number;
}

function buildProjectsEnvelope(opts: BuildOpts): Envelope {
  const env = baseEnvelope();
  const projectCount = opts.projectCount ?? 5;
  const actual = opts.actualWeeks ?? opts.windowWeeks;
  const projects: ProjectsTrendProject[] = Array.from(
    { length: projectCount },
    (_, i) => {
      // Descending magnitude — index 0 has highest cost, descending by
      // index. windowCost = sum(weekly_cost) over the trailing slice.
      const baseCost = (projectCount - i) * 10;
      const weekly_cost: number[] = Array.from(
        { length: actual },
        (_, j) => baseCost + j,
      );
      const weekly_pct: (number | null)[] = Array.from(
        { length: actual },
        (_, j) => (projectCount - i) + j * 0.1,
      );
      return {
        key: `project-${i + 1}`,
        bucket_path: `/repos/project-${i + 1}`,
        weekly_cost,
        weekly_pct,
        first_seen_at: '2026-04-01T00:00:00Z',
        last_seen_at: '2026-05-13T10:00:00Z',
        sessions_count_12w: 10 + i,
      };
    },
  );
  const projectsEnv: ProjectsEnvelope = {
    current_week: {
      week_label: 'wk May 13',
      week_start_date: '2026-05-13',
      week_start_at: '2026-05-13T00:00:00Z',
      total_cost_usd: projects.reduce((s, p) => s + p.weekly_cost[p.weekly_cost.length - 1]!, 0),
      rows: projects.map((p, i) => ({
        key: p.key,
        bucket_path: p.bucket_path,
        cost_usd: p.weekly_cost[p.weekly_cost.length - 1]!,
        attributed_pct: 10 - i,
        sessions_count: 5,
      })),
    },
    trend: {
      window_weeks: actual,
      weeks: Array.from({ length: actual }, (_, j) => ({
        week_start_date: `2026-0${4}-0${j + 1}`,
        week_label: `wk0${j + 1}`,
        total_cost_usd: 100 + j,
        total_pct: 10 + j,
      })),
      projects,
    },
  };
  env.projects = projectsEnv;
  return env;
}

function buildProjectDetail(key: string): ProjectDetail {
  return {
    key,
    bucket_path: `/repos/${key}`,
    window_weeks: 4,
    window_cost_usd: 42.0,
    window_attributed_pct: 12.5,
    models: [
      { model: 'claude-sonnet-4-5', cost_usd: 30.0, sessions_count: 3, tokens_input: 1000, tokens_output: 500 },
      { model: 'claude-opus-4-7', cost_usd: 12.0, sessions_count: 1, tokens_input: 400, tokens_output: 200 },
    ],
    sessions: [
      { session_id: 's-1', started_at: '2026-05-12T09:00:00Z', last_activity_at: '2026-05-12T10:00:00Z', primary_model: 'claude-sonnet-4-5', cost_usd: 12.0 },
      { session_id: 's-2', started_at: '2026-05-11T09:00:00Z', last_activity_at: '2026-05-11T11:00:00Z', primary_model: 'claude-opus-4-7', cost_usd: 8.0 },
    ],
    models_total: 2,
    sessions_total: 12, // > sessions.length to exercise the "+M more" line
  };
}

function stubFetchOk(body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('<ProjectsModal />', () => {
  it('renders window pills with the current selection (default 4w)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 12 }));
    render(<ProjectsModal />);
    expect(screen.getByRole('radio', { name: '4w' })).toHaveAttribute('aria-checked', 'true');
    // The pill for 8w is rendered but not checked.
    expect(screen.getByRole('radio', { name: '8w' })).toHaveAttribute('aria-checked', 'false');
  });

  it('clicking 8w pill updates prefs and re-renders the title', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 12 }));
    render(<ProjectsModal />);
    fireEvent.click(screen.getByRole('radio', { name: '8w' }));
    expect(getState().prefs.projectsWindowWeeks).toBe(8);
    // Title reflects the new window
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent(/last 8w/);
  });

  it('table renders all projects sorted desc by window cost (default)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    render(<ProjectsModal />);
    const rows = screen.getAllByTestId('projects-table-row');
    expect(rows).toHaveLength(5);
    const costs = rows.map((r) => parseFloat(r.getAttribute('data-cost') ?? '0'));
    const sorted = [...costs].sort((a, b) => b - a);
    expect(costs).toEqual(sorted);
  });

  it('clicking a row expands the drill', async () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-2')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    // Click on the SECOND row so we exercise click-to-expand (the leader
    // is auto-selected on mount; clicking it would collapse rather than
    // expand the drill).
    const rows = screen.getAllByTestId('projects-table-row');
    fireEvent.click(rows[1]);
    await waitFor(() => {
      expect(screen.getByText(/Models \(this project\)/)).toBeInTheDocument();
      expect(screen.getByText(/Recent sessions/)).toBeInTheDocument();
    });
  });

  it('renders the "Showing N weeks" notice when actual < requested', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    // User has 8w pref but the snapshot only has 3 weeks of history.
    dispatch({ type: 'SAVE_PREFS', patch: { projectsWindowWeeks: 8 } });
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 8, actualWeeks: 3 }));
    render(<ProjectsModal />);
    expect(screen.getByText(/Showing 3 weeks/)).toBeInTheDocument();
  });

  it('drill session row click opens SessionModal (replace pattern)', async () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    // The leader (project-1) is pre-selected; the drill renders sessions
    // after the lazy fetch resolves.
    const sessionBtn = await screen.findByTestId('drill-session-0');
    fireEvent.click(sessionBtn);
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('s-1');
  });

  it('share icon dispatches openShareModal with windowWeeks param', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    dispatch({ type: 'SAVE_PREFS', patch: { projectsWindowWeeks: 8 } });
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 8 }));
    render(<ProjectsModal />);
    // ShareIcon doesn't pass data-testid through to its <button>, so we
    // target the wrapping <span> the modal exposes for testing and
    // click the contained share button.
    const wrapper = screen.getByTestId('share-icon-projects-modal');
    const shareBtn = wrapper.querySelector('button[data-share-panel="projects"]');
    expect(shareBtn).not.toBeNull();
    fireEvent.click(shareBtn!);
    const share = getState().shareModal;
    expect(share?.panel).toBe('projects');
    expect(share?.params?.windowWeeks).toBe(8);
  });

  it('clicking the (other) band in the trend chart does NOT change selection', async () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    // 7 projects in a 4w window → 5 top + 2 in (other). The (other) band
    // is a synthetic series and must not dispatch onProjectSelect.
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 7 }));
    render(<ProjectsModal />);
    const otherPoly = document.querySelector('polygon[data-series-key="(other)"]') as SVGPolygonElement | null;
    expect(otherPoly).not.toBeNull();
    // Note initial selection (leader, project-1) and click (other).
    const before = document.querySelector('tr.selected')?.firstElementChild?.textContent;
    fireEvent.click(otherPoly!);
    const after = document.querySelector('tr.selected')?.firstElementChild?.textContent;
    expect(after).toBe(before);
  });

  it('clicking a top-N band selects that project in the table', async () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-3')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    render(<ProjectsModal />);
    // Pick a polygon that is NOT the leader so the assertion is sharp.
    const polygons = document.querySelectorAll('polygon[data-series-key]:not([data-series-key="(other)"])');
    expect(polygons.length).toBeGreaterThanOrEqual(2);
    // Index 1 corresponds to rank-2 (project-2). We just need a non-leader.
    const target = polygons[1] as SVGPolygonElement;
    const targetKey = target.getAttribute('data-series-key');
    fireEvent.click(target);
    await waitFor(() => {
      const selectedText = document.querySelector('tr.selected')?.firstElementChild?.textContent;
      expect(selectedText).toBe(targetKey);
    });
  });
});
