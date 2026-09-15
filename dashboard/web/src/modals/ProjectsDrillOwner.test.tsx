// #834 S2 (#775) — the project drill has exactly ONE fetch owner.
//
// `ProjectsModal` mounted `ProjectsDrillPanel` at two places, one inside the
// mobile row and one below the table, each calling `useProjectDetail` itself.
// Crossing the 640px breakpoint unmounts one and mounts the other, and the
// hook's state lives in the component, so the newly mounted owner starts from
// `data == null` and refetches — the drill drops to "Loading…" and issues a
// second request for a project whose detail the process already had.
//
// A network-count assertion alone would not establish the fix. A module-global
// cache, a hidden second panel, or request deduplication could each produce
// one request while leaving two owners in the tree, so this counts OWNERS: the
// hook is spied on, and one render pass must invoke it exactly once.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { ProjectsModal } from './ProjectsModal';
import { ProjectsDrillPanel } from './ProjectsDrillPanel';
import {
  _resetForTests,
  dispatch,
  updateSnapshot,
} from '../store/store';
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import { stubResponsiveMedia } from '../test-utils/mobileMedia';
import { MOBILE_MEDIA_QUERY } from '../lib/breakpoints';
import * as projectDetailModule from '../hooks/useProjectDetail';
import type {
  Envelope,
  ProjectDetail,
  ProjectsEnvelope,
  ProjectsTrendProject,
} from '../types/envelope';

const KEY = 'project-1';

// The envelope builders are copied verbatim from ProjectsModal.test.tsx,
// which does not export them. A hand-shaped fixture is not an option here:
// the modal reads `weekly_cost`, `weekly_pct` and `sessions_per_week` off
// every trend project and throws on a shape that lacks them.
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

interface BuildOpts {
  windowWeeks: number;
  projectCount?: number;
  // When set, the trend's `window_weeks` is the SMALLER of these two;
  // the table's first/last columns scale by `windowWeeks`. Pass
  // `actualWeeks < windowWeeks` to exercise the "Showing N cycles"
  // notice. Defaults to `windowWeeks` when unset.
  actualWeeks?: number;
  // Append N additional projects with all-zero `weekly_cost` (still
  // present in the trend matrix because they have historical activity
  // outside the active window). Used to exercise the collapse-to-top-N-
  // active behavior — the inactive tail must hide behind the expand
  // toggle by default.
  inactiveTail?: number;
}

function buildProjectsEnvelope(opts: BuildOpts): Envelope {
  const env = baseEnvelope();
  const projectCount = opts.projectCount ?? 5;
  const actual = opts.actualWeeks ?? opts.windowWeeks;
  const inactiveTail = opts.inactiveTail ?? 0;
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
      // 1 session per week, baseline timestamps that vary by week so
      // window-scoped first/last differ from all-time first/last.
      const sessions_per_week: number[] = Array.from(
        { length: actual },
        () => 1,
      );
      const first_seen_per_week: (string | null)[] = Array.from(
        { length: actual },
        (_, j) => `2026-04-${String(j + 1).padStart(2, '0')}T00:00:00Z`,
      );
      const last_seen_per_week: (string | null)[] = Array.from(
        { length: actual },
        (_, j) => `2026-04-${String(j + 1).padStart(2, '0')}T23:00:00Z`,
      );
      return {
        key: `project-${i + 1}`,
        bucket_path: `/repos/project-${i + 1}`,
        weekly_cost,
        weekly_pct,
        sessions_per_week,
        first_seen_per_week,
        last_seen_per_week,
      };
    },
  );
  for (let k = 0; k < inactiveTail; k++) {
    projects.push({
      key: `inactive-${k + 1}`,
      bucket_path: `/repos/inactive-${k + 1}`,
      weekly_cost: Array.from({ length: actual }, () => 0),
      weekly_pct: Array.from({ length: actual }, () => null),
      sessions_per_week: Array.from({ length: actual }, () => 0),
      first_seen_per_week: Array.from({ length: actual }, () => null),
      last_seen_per_week: Array.from({ length: actual }, () => null),
    });
  }
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

function projectDetail(key: string): ProjectDetail {
  return {
    key,
    bucket_path: `/repos/${key}`,
    window_weeks: 4,
    window_start_at: '2026-04-20T00:00:00Z',
    window_end_at: '2026-05-18T00:00:00Z',
    window_cost_usd: 42.0,
    window_attributed_pct: 12.5,
    models: [{
      model: 'claude-sonnet-4-5', cost_usd: 30.0, sessions_count: 3,
      tokens_input: 1000, tokens_output: 500,
    }],
    sessions: [{
      session_id: 's-1', started_at: '2026-05-12T09:00:00Z',
      last_activity_at: '2026-05-12T10:00:00Z',
      primary_model: 'claude-sonnet-4-5', cost_usd: 12.0,
    }],
    models_total: 1,
    sessions_total: 1,
  } as unknown as ProjectDetail;
}

describe('the project drill has one fetch owner across a breakpoint change', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
  });

  afterEach(() => {
    _resetKeymap();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('invokes useProjectDetail exactly once per render, at every width', async () => {
    const media = stubResponsiveMedia((query) => query === MOBILE_MEDIA_QUERY);
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => projectDetail(KEY),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchSpy);
    const hookSpy = vi.spyOn(projectDetailModule, 'useProjectDetail');

    render(<ProjectsModal />);
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    await screen.findAllByTestId('projects-table-row');
    await waitFor(() => {
      expect(document.querySelector('[data-testid="projects-drill"]'))
        .not.toBeNull();
    });

    expect(hookSpy.mock.calls.length).toBeGreaterThan(0);

    // Cross the breakpoint: the desktop branch renders the drill below the
    // table instead of inside the row. The spy is cleared first, because the
    // renders BEFORE the auto-select effect legitimately carry a null key.
    hookSpy.mockClear();
    const fetchesBeforeResize = fetchSpy.mock.calls.length;
    expect(fetchesBeforeResize).toBe(1);
    media.set(() => false);
    await waitFor(() => {
      expect(document.querySelector('.projects-drill-row')).toBeNull();
    });
    await waitFor(() => {
      expect(document.querySelector('[data-testid="projects-drill"]'))
        .not.toBeNull();
    });

    // The owner did not change identity, so no remount refetched.
    expect(fetchSpy.mock.calls.length).toBe(fetchesBeforeResize);
    for (const call of hookSpy.mock.calls) expect(call[0]).toBe(KEY);

    // Exactly one drill is rendered, and exactly one owner produced it.
    expect(document.querySelectorAll('[data-testid="projects-drill"]').length)
      .toBe(1);
  });

  it('the drill panel itself owns no request', async () => {
    // The mechanism, not only its symptom. A module-global cache, a hidden
    // second panel or request deduplication could each hold the fetch count at
    // one while leaving two hook owners in the tree; none of them could make
    // the panel stop calling the hook.
    stubResponsiveMedia(() => false);
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => projectDetail(KEY),
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchSpy);
    const hookSpy = vi.spyOn(projectDetailModule, 'useProjectDetail');

    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(
      <ProjectsDrillPanel
        projectKey={KEY}
        windowWeeks={4}
        detail={{ data: projectDetail(KEY), loading: false, error: null }}
      />,
    );
    await screen.findByTestId('projects-drill');
    expect(hookSpy).not.toHaveBeenCalled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('keeps the resolved detail on screen across the transition', async () => {
    const media = stubResponsiveMedia((query) => query === MOBILE_MEDIA_QUERY);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => projectDetail(KEY),
    } as unknown as Response));

    render(<ProjectsModal />);
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    const drill = await screen.findByTestId('projects-drill');
    expect(drill.textContent).toContain('$42.00');

    media.set(() => false);
    // No "Loading…" flash: the state survives because its owner does. A
    // remounted owner starts from `data == null` and renders the spinner.
    await waitFor(() => {
      expect(document.querySelector('.projects-drill-row')).toBeNull();
    });
    const after = await screen.findByTestId('projects-drill');
    expect(after.textContent).toContain('$42.00');
  });
});
