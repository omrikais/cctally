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
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
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
  _resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  _resetKeymap();
});

describe('<ProjectsModal />', () => {
  it('Sessions / First seen / Last seen columns render bare labels (issue #71 full fix)', () => {
    // Per spec §3.4 these three columns are window-scoped — derived
    // client-side from the envelope's per-week arrays. The widened
    // "(12w)" / "(all-time)" labels from the I2 stopgap are gone.
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 12 }));
    render(<ProjectsModal />);
    expect(
      screen.getByRole('columnheader', { name: 'Sessions' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('columnheader', { name: 'First seen' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('columnheader', { name: 'Last seen' }),
    ).toBeInTheDocument();
    // The widened labels are absent.
    expect(
      screen.queryByRole('columnheader', { name: 'Sessions (12w)' }),
    ).toBeNull();
    expect(
      screen.queryByRole('columnheader', { name: 'First seen (all-time)' }),
    ).toBeNull();
    expect(
      screen.queryByRole('columnheader', { name: 'Last seen (all-time)' }),
    ).toBeNull();
  });

  it('flipping the window pill rescopes Sessions / First seen / Last seen cells (issue #71)', () => {
    // Each project's per-week arrays carry 1 session per week + ascending
    // first_seen / last_seen timestamps (one calendar day per week). The
    // 4w and 12w windows therefore land on different counts (4 vs 12),
    // different first_seen (slice-head), and identical-or-different
    // last_seen (slice-tail) — proving the table cells follow the pill.
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    dispatch({ type: 'SAVE_PREFS', patch: { projectsWindowWeeks: 12 } });
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 12, projectCount: 1 }));
    render(<ProjectsModal />);

    const row12 = screen.getByTestId('projects-table-row');
    expect(row12.getAttribute('data-sessions')).toBe('12');
    const cells12 = row12.querySelectorAll('td');
    const sessionsCell12 = cells12[1].textContent;
    const firstSeenCell12 = cells12[2].textContent;
    const lastSeenCell12 = cells12[3].textContent;
    expect(sessionsCell12).toBe('12');

    // Flip to 4w — Sessions count must collapse to 4, and the
    // first-seen / last-seen cells should shift to the trailing 4 weeks
    // (i.e. NOT the same as the 12w extremes).
    fireEvent.click(screen.getByRole('radio', { name: '4w' }));
    const row4 = screen.getByTestId('projects-table-row');
    expect(row4.getAttribute('data-sessions')).toBe('4');
    const cells4 = row4.querySelectorAll('td');
    expect(cells4[1].textContent).toBe('4');
    // Different first-seen between 12w (week 0) and 4w (week 8).
    expect(cells4[2].textContent).not.toBe(firstSeenCell12);
    // Same last-seen (both windows end at the latest week).
    expect(cells4[3].textContent).toBe(lastSeenCell12);
  });

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

  it('collapses to top-10 active projects by default and hides the rest behind expand', () => {
    // 12 active projects (cost > 0) + 20 inactive (cost == 0) = 32 rows
    // total. Default-collapsed view should render only the top 10 active.
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(
      buildProjectsEnvelope({ windowWeeks: 4, projectCount: 12, inactiveTail: 20 }),
    );
    render(<ProjectsModal />);
    const rows = screen.getAllByTestId('projects-table-row');
    expect(rows).toHaveLength(10);
    // All ten visible rows have non-zero cost.
    const costs = rows.map((r) => parseFloat(r.getAttribute('data-cost') ?? '0'));
    expect(costs.every((c) => c > 0)).toBe(true);
    // Toggle text advertises the full count + hidden delta.
    const toggle = screen.getByTestId('projects-table-toggle');
    expect(toggle).toHaveTextContent(/Show all 32 projects \(\+22\)/);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('clicking the expand toggle reveals all rows including the inactive tail', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(
      buildProjectsEnvelope({ windowWeeks: 4, projectCount: 12, inactiveTail: 20 }),
    );
    render(<ProjectsModal />);
    fireEvent.click(screen.getByTestId('projects-table-toggle'));
    expect(screen.getAllByTestId('projects-table-row')).toHaveLength(32);
    const toggle = screen.getByTestId('projects-table-toggle');
    expect(toggle).toHaveTextContent(/Show top 10 active/);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);
    expect(screen.getAllByTestId('projects-table-row')).toHaveLength(10);
  });

  it('does NOT render the expand toggle when total rows already fit within the collapse limit', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    render(<ProjectsModal />);
    expect(screen.getAllByTestId('projects-table-row')).toHaveLength(5);
    expect(screen.queryByTestId('projects-table-toggle')).toBeNull();
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

  it('SSE-tick race: in-flight initial fetch survives a snapshot update', async () => {
    // Regression: Playwright e2e surfaced an endless-loading bug where the
    // SWR effect aborted the in-flight initial fetch on every SSE tick
    // (generatedAt change in the dep array). With ~10s server response
    // and ~5s SSE cadence, the fetch was constantly cancelled and never
    // resolved — `Loading…` rendered indefinitely.
    //
    // Verify the fix by:
    //   1. Stubbing fetch with a manually-resolved promise (held open).
    //   2. Updating the snapshot to a new generated_at (simulates SSE tick).
    //   3. Asserting the fetch was called EXACTLY ONCE (no abort+restart).
    //   4. Resolving the held promise and verifying the drill renders.
    let resolveFetch: ((r: Response) => void) | null = null;
    const fetchSpy = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((res) => {
          resolveFetch = res;
        }),
    );
    vi.stubGlobal('fetch', fetchSpy);

    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);

    // Leader (project-1) is pre-selected on mount → fetch #1 starts.
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });

    // Simulate an SSE tick: same envelope shape but a new generated_at.
    const next = buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 });
    next.generated_at = '2026-05-13T10:00:05Z';
    updateSnapshot(next);

    // Give React a microtask to flush; the in-flight fetch must NOT be
    // cancelled + restarted. Still exactly one fetch.
    await new Promise((f) => setTimeout(f, 10));
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    // Resolve the held fetch — drill should now render.
    resolveFetch!({
      ok: true,
      status: 200,
      json: async () => buildProjectDetail('project-1'),
    } as unknown as Response);
    await waitFor(() => {
      expect(screen.getByText(/Models \(this project\)/)).toBeInTheDocument();
    });
  });

  it('stale-on-switch: drill renders Loading… not stale data when projectKey changes', async () => {
    // Regression (Bug #2 from Playwright e2e): the SWR pattern keeps the
    // previously-fetched `data` mounted while the next fetch is in
    // flight. That's correct for SSE-tick revalidation but WRONG when
    // the user changed selection — the drill title is built from
    // `data.key` and would show the PREVIOUS project's name + stats
    // under a table row visually marked as the new selection.
    //
    // Verify the drill renders the Loading… placeholder while
    // `data.key !== projectKey`.
    let detailCalls = 0;
    let resolveSecond: ((r: Response) => void) | null = null;
    const fetchSpy = vi.fn().mockImplementation((url: string) => {
      detailCalls += 1;
      if (detailCalls === 1) {
        // First call: project-1 (auto-selected leader) — resolve immediately.
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => buildProjectDetail('project-1'),
        } as unknown as Response);
      }
      // Second call: project-2 — hold it open so we can observe the
      // intermediate Loading… state.
      expect(url).toContain('project-2');
      return new Promise<Response>((res) => {
        resolveSecond = res;
      });
    });
    vi.stubGlobal('fetch', fetchSpy);

    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);

    // Wait for the first fetch (project-1) to land and the drill to
    // render its content.
    await waitFor(() => {
      expect(screen.getByText(/Models \(this project\)/)).toBeInTheDocument();
    });
    expect(screen.getByText(/▾ project-1/)).toBeInTheDocument();

    // Switch selection to project-2 — second fetch starts but stays
    // open. The drill MUST NOT show project-1's stats anymore.
    const rows = screen.getAllByTestId('projects-table-row');
    fireEvent.click(rows[1]); // project-2
    await waitFor(() => {
      // Drill renders "Loading…" instead of the stale project-1 title.
      expect(screen.queryByText(/▾ project-1/)).toBeNull();
      expect(screen.getByText(/Loading…/)).toBeInTheDocument();
    });

    // Resolve project-2 — drill swaps to its title.
    resolveSecond!({
      ok: true,
      status: 200,
      json: async () => buildProjectDetail('project-2'),
    } as unknown as Response);
    await waitFor(() => {
      expect(screen.getByText(/▾ project-2/)).toBeInTheDocument();
    });
  });

  it('drill footer "Show in Sessions" filters Sessions to the project + closes modal (spec §4.3)', async () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    // Open the modal so CLOSE_MODAL has something to close.
    dispatch({ type: 'OPEN_MODAL', kind: 'projects', projectKey: 'project-1' });
    expect(getState().openModal).toBe('projects');

    // Footer renders after the drill fetch resolves.
    const link = await screen.findByTestId('drill-show-in-sessions');
    expect(link).toHaveTextContent(/Show in Sessions/);
    fireEvent.click(link);
    expect(getState().filterText).toBe('project-1');
    expect(getState().openModal).toBeNull();
  });

  it('renders the footer hint row with shortcuts + freshness chip (spec §3.1 item 6)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    const footer = screen.getByTestId('projects-modal-footer-hint');
    // Shortcut chips are advertised in the hint.
    expect(footer).toHaveTextContent('window');
    expect(footer).toHaveTextContent('row');
    expect(footer).toHaveTextContent('drill');
    expect(footer).toHaveTextContent('close');
    // SyncChip rendered as a child — falls back to "sync paused" when
    // the envelope's sync_age_s is null (baseEnvelope default), which
    // is enough to prove the freshness slot is wired.
    expect(footer.querySelector('.sync-chip')).not.toBeNull();
  });

  it('keymap: pressing 8 sets the window to 8w; 0 sets it to 12w (spec §3.7)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 12 }));
    render(<ProjectsModal />);
    expect(getState().prefs.projectsWindowWeeks).toBe(4); // default
    fireEvent.keyDown(document, { key: '8' });
    expect(getState().prefs.projectsWindowWeeks).toBe(8);
    fireEvent.keyDown(document, { key: '0' });
    expect(getState().prefs.projectsWindowWeeks).toBe(12);
    fireEvent.keyDown(document, { key: '1' });
    expect(getState().prefs.projectsWindowWeeks).toBe(1);
  });

  it('keymap: pressing s toggles the yMode (spec §3.7)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4 }));
    render(<ProjectsModal />);
    expect(getState().prefs.projectsTrendYMode).toBe('absolute');
    fireEvent.keyDown(document, { key: 's' });
    expect(getState().prefs.projectsTrendYMode).toBe('share');
    fireEvent.keyDown(document, { key: 's' });
    expect(getState().prefs.projectsTrendYMode).toBe('absolute');
  });

  it('keymap: ArrowDown / ArrowUp navigates row selection (spec §3.7)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    // Leader (project-1) pre-selected on mount.
    expect(document.querySelector('tr.selected')?.firstElementChild?.textContent).toBe('project-1');
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.querySelector('tr.selected')?.firstElementChild?.textContent).toBe('project-2');
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.querySelector('tr.selected')?.firstElementChild?.textContent).toBe('project-3');
    // Wrap from last → first.
    fireEvent.keyDown(document, { key: 'ArrowDown' });
    expect(document.querySelector('tr.selected')?.firstElementChild?.textContent).toBe('project-1');
    // ArrowUp wraps the other way.
    fireEvent.keyDown(document, { key: 'ArrowUp' });
    expect(document.querySelector('tr.selected')?.firstElementChild?.textContent).toBe('project-3');
  });

  it('keymap: Enter toggles drill on the selected row (spec §3.7)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    // Leader auto-selected → drill is open. Enter collapses.
    expect(document.querySelector('tr.selected')).not.toBeNull();
    fireEvent.keyDown(document, { key: 'Enter' });
    expect(document.querySelector('tr.selected')).toBeNull();
    // Press Enter again with no selection → re-opens on the leader.
    fireEvent.keyDown(document, { key: 'Enter' });
    expect(document.querySelector('tr.selected')?.firstElementChild?.textContent).toBe('project-1');
  });

  it('chart aria-label reflects yMode (cost vs share %)', () => {
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 }));
    render(<ProjectsModal />);
    // Default yMode is 'absolute' — aria-label mentions "cost".
    const svgAbs = document.querySelector('svg[role="img"]');
    expect(svgAbs?.getAttribute('aria-label')).toContain('cost');
    expect(svgAbs?.getAttribute('aria-label')).not.toContain('share %');

    // Flip to 'share %' — aria-label updates.
    fireEvent.click(screen.getByRole('radio', { name: 'share %' }));
    const svgShare = document.querySelector('svg[role="img"]');
    expect(svgShare?.getAttribute('aria-label')).toContain('share %');
    expect(svgShare?.getAttribute('aria-label')).not.toContain('cost');
  });
});
