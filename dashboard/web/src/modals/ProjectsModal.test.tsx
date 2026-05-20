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
import { stubMobileMedia } from '../test-utils/mobileMedia';
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
    // The share button forwards `dataTestId` directly onto its <button>
    // (issue #67) so no wrapper element is needed.
    const shareBtn = screen.getByTestId('share-icon-projects-modal');
    expect(shareBtn.tagName).toBe('BUTTON');
    fireEvent.click(shareBtn);
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

  it('mobile: tapping a top-N legend item selects that project in the table', async () => {
    stubMobileMedia(true);
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-3')));
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    render(<ProjectsModal />);
    const buttons = document.querySelectorAll(
      '.projects-trend-legend > button.projects-trend-legend-item[data-series-key]',
    );
    expect(buttons.length).toBeGreaterThanOrEqual(2);
    const target = buttons[1] as HTMLButtonElement;
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

  // Issue #66 — column headers route through `SortableHeader`, default
  // sort is cost-desc, override persists at `prefs.projectsSortOverride`,
  // and the action widens `SET_TABLE_SORT.table` to include `'projects'`.
  describe('column sort (#66 / spec §3.4)', () => {
    // Three projects with distinct, non-monotone-with-cost values so each
    // header click resolves to a different visible row order than the
    // cost-desc default.
    function buildSortEnvelope(): Envelope {
      const env = baseEnvelope();
      const make = (
        key: string,
        cost: number,
        pct: number,
        sessions: number,
        dayOffset: number,
      ): ProjectsTrendProject => ({
        key,
        bucket_path: `/repos/${key}`,
        weekly_cost: Array.from({ length: 4 }, () => cost),
        weekly_pct: Array.from({ length: 4 }, () => pct),
        sessions_per_week: Array.from({ length: 4 }, () => sessions),
        first_seen_per_week: Array.from(
          { length: 4 },
          (_, j) =>
            `2026-04-${String(dayOffset + j * 7).padStart(2, '0')}T00:00:00Z`,
        ),
        last_seen_per_week: Array.from(
          { length: 4 },
          (_, j) =>
            `2026-04-${String(dayOffset + j * 7).padStart(2, '0')}T23:00:00Z`,
        ),
      });
      // Column orderings (rows always presented in their list order here;
      // the modal sorts cost-desc by default, so leader is zulu):
      //   cost desc:    zulu(200), mid(100), alpha(40)
      //   project asc:  alpha,     mid,      zulu
      //   sessions desc: alpha(36), zulu(16), mid(8)
      const projects: ProjectsTrendProject[] = [
        make('alpha', 10, 1, 9, 1),
        make('mid', 25, 5, 2, 2),
        make('zulu', 50, 10, 4, 3),
      ];
      env.projects = {
        current_week: {
          week_label: 'wk May 13',
          week_start_date: '2026-05-13',
          week_start_at: '2026-05-13T00:00:00Z',
          total_cost_usd: projects.reduce(
            (s, p) => s + p.weekly_cost[p.weekly_cost.length - 1]!,
            0,
          ),
          rows: projects.map((p) => ({
            key: p.key,
            bucket_path: p.bucket_path,
            cost_usd: p.weekly_cost[p.weekly_cost.length - 1]!,
            attributed_pct: 10,
            sessions_count: 5,
          })),
        },
        trend: {
          window_weeks: 4,
          weeks: Array.from({ length: 4 }, (_, j) => ({
            week_start_date: `2026-04-0${j + 1}`,
            week_label: `wk0${j + 1}`,
            total_cost_usd: 85,
            total_pct: 17,
          })),
          projects,
        },
      };
      return env;
    }

    function rowKeys(): string[] {
      return screen
        .getAllByTestId('projects-table-row')
        .map((r) => r.querySelector('td.project')?.textContent ?? '');
    }

    it('renders SortableHeader columnheaders with no active sort by default', () => {
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('zulu')));
      updateSnapshot(buildSortEnvelope());
      render(<ProjectsModal />);
      // Every header is a columnheader with aria-sort="none" (no override).
      const headers = screen.getAllByRole('columnheader');
      expect(headers).toHaveLength(7);
      for (const h of headers) {
        expect(h.getAttribute('aria-sort')).toBe('none');
      }
      // Default order is still cost-desc — the static ▼ on the Cost header
      // is gone (now driven by SortableHeader's caret span).
      expect(rowKeys()).toEqual(['zulu', 'mid', 'alpha']);
    });

    it('clicking the Cost header cycles desc → asc → null (3-state) and persists override', () => {
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('zulu')));
      updateSnapshot(buildSortEnvelope());
      render(<ProjectsModal />);
      const costHeader = screen.getByRole('columnheader', { name: 'Cost' });

      // First click: null → cost desc. Rows already cost-desc; caret = ▼.
      fireEvent.click(costHeader);
      expect(getState().prefs.projectsSortOverride).toEqual({
        column: 'cost',
        direction: 'desc',
      });
      expect(costHeader.getAttribute('aria-sort')).toBe('descending');
      expect(rowKeys()).toEqual(['zulu', 'mid', 'alpha']);

      // Second click: desc → asc. Rows flip; caret = ▲.
      fireEvent.click(costHeader);
      expect(getState().prefs.projectsSortOverride).toEqual({
        column: 'cost',
        direction: 'asc',
      });
      expect(costHeader.getAttribute('aria-sort')).toBe('ascending');
      expect(rowKeys()).toEqual(['alpha', 'mid', 'zulu']);

      // Third click: asc → null. Rows fall back to default cost-desc.
      fireEvent.click(costHeader);
      expect(getState().prefs.projectsSortOverride).toBeNull();
      expect(costHeader.getAttribute('aria-sort')).toBe('none');
      expect(rowKeys()).toEqual(['zulu', 'mid', 'alpha']);
    });

    it('clicking the Project header sorts ascending by display key (text default)', () => {
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('zulu')));
      updateSnapshot(buildSortEnvelope());
      render(<ProjectsModal />);
      const projectHeader = screen.getByRole('columnheader', { name: 'Project' });
      fireEvent.click(projectHeader);
      expect(getState().prefs.projectsSortOverride).toEqual({
        column: 'project',
        direction: 'asc',
      });
      expect(projectHeader.getAttribute('aria-sort')).toBe('ascending');
      expect(rowKeys()).toEqual(['alpha', 'mid', 'zulu']);
    });

    it('clicking the Sessions header sorts descending by sessions count (num default)', () => {
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('zulu')));
      updateSnapshot(buildSortEnvelope());
      render(<ProjectsModal />);
      const sessionsHeader = screen.getByRole('columnheader', { name: 'Sessions' });
      fireEvent.click(sessionsHeader);
      expect(getState().prefs.projectsSortOverride).toEqual({
        column: 'sessions',
        direction: 'desc',
      });
      expect(sessionsHeader.getAttribute('aria-sort')).toBe('descending');
      // sessions per project per week: alpha=9, mid=2, zulu=4 (× 4 weeks).
      expect(rowKeys()).toEqual(['alpha', 'zulu', 'mid']);
    });

    it('hydrates persisted override on mount (project asc)', () => {
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('alpha')));
      // Seed pref before mount so the modal reads the persisted override.
      dispatch({
        type: 'SET_TABLE_SORT',
        table: 'projects',
        override: { column: 'project', direction: 'asc' },
      });
      updateSnapshot(buildSortEnvelope());
      render(<ProjectsModal />);
      expect(rowKeys()).toEqual(['alpha', 'mid', 'zulu']);
      expect(
        screen.getByRole('columnheader', { name: 'Project' }).getAttribute('aria-sort'),
      ).toBe('ascending');
    });
  });

  describe("'% of week' column (#72)", () => {
    // The legacy `$/1%` column was mathematically degenerate — every row
    // showed `total_cost / weekly_used_pct` independent of the project.
    // Replaced with a per-row `% of week` = cost[p] / sum(cost) over the
    // active window, which IS differential across rows (spec §3.4).
    it("replaces the '$/1%' header with '% of week' (header count unchanged)", () => {
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
      updateSnapshot(buildProjectsEnvelope({ windowWeeks: 1, projectCount: 3 }));
      render(<ProjectsModal />);
      expect(
        screen.getByRole('columnheader', { name: '% of week' }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('columnheader', { name: '$/1%' }),
      ).toBeNull();
      // Column count stays at 7 — old shape, new label/derivation.
      expect(screen.getAllByRole('columnheader')).toHaveLength(7);
    });

    it("renders each row's share of window cost", () => {
      // buildProjectsEnvelope(windowWeeks=1, projectCount=3) yields
      // weekly_cost = [30] / [20] / [10] → total 60 → shares 50% / 33% / 17%.
      vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
      updateSnapshot(buildProjectsEnvelope({ windowWeeks: 1, projectCount: 3 }));
      render(<ProjectsModal />);
      const rows = screen.getAllByTestId('projects-table-row');
      expect(rows).toHaveLength(3);
      const cell = (rowIdx: number) =>
        rows[rowIdx]!.querySelectorAll('td')[6]!.textContent;
      expect(cell(0)).toBe('50%');
      expect(cell(1)).toBe('33%');
      expect(cell(2)).toBe('17%');
    });
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

// Issue #73 — ProjectsModal mobile (≤640w) stacked-card layout.
// JSDOM does not evaluate @media rules; these tests cover only the
// React conditional branches gated by useIsMobile() (sort-cycle pill
// presence, inline drill rendering, desktop-position drill suppression).
// CSS reflow itself is verified by manual mobile-viewport check during
// implementation.
describe('ProjectsModal — mobile (≤640w) card layout', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    stubMobileMedia(true);
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
  });

  afterEach(() => {
    _resetKeymap();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function openWithProjects(): void {
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
  }

  it('renders the mobile sort-cycle pill above the card list', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const pill = await screen.findByTestId('projects-mobile-sort');
    expect(pill).toBeInTheDocument();
    expect(pill.textContent).toMatch(/sort/i);
  });

  it('cycle pill cycles sort: cost → sessions → used → share → first → last → project → cost', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const pill = await screen.findByTestId('projects-mobile-sort');
    const expectedSequence = [
      /sessions/i, /used/i, /share|week/i, /first/i, /last/i, /project/i, /cost/i,
    ];
    for (const re of expectedSequence) {
      fireEvent.click(pill);
      await waitFor(() => expect(pill.textContent ?? '').toMatch(re));
    }
  });

  it('every metric is rendered in each card (no display:none losses)', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const rows = await screen.findAllByTestId('projects-table-row');
    expect(rows.length).toBeGreaterThan(0);
    const first = rows[0]!;
    expect(first.querySelectorAll('td').length).toBe(7);
    expect(first.querySelector('td.project')?.textContent).toBeTruthy();
    expect(first.querySelector('td.first-seen')?.textContent).toBeTruthy();
    expect(first.querySelector('td.last-seen')?.textContent).toBeTruthy();
  });

  it('selected card has aria-expanded=true and inline drill rendered as the next sibling', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const rows = await screen.findAllByTestId('projects-table-row');
    // Stay on the auto-selected leader (project-1) so the stubFetchOk
    // payload's `key` matches; the inline drill sibling is rendered
    // synchronously off `selectedKey`, but the inner ProjectsDrillPanel
    // testid only appears after `useProjectDetail` resolves (and only
    // if `data.key === projectKey`).
    expect(rows[0]!.getAttribute('aria-expanded')).toBe('true');
    const sibling = rows[0]!.nextElementSibling;
    expect(sibling).not.toBeNull();
    expect(sibling?.classList.contains('projects-drill-row')).toBe(true);
    await waitFor(() => {
      expect(sibling?.querySelector('[data-testid="projects-drill"]')).not.toBeNull();
    });
  });

  it('selecting a different card moves the inline drill; only one inline drill at a time', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const rows = await screen.findAllByTestId('projects-table-row');
    // Auto-selected leader is row 0; click row 1 to move selection. The
    // inline `tr.projects-drill-row` follows `selectedKey` synchronously
    // — we don't depend on the drill payload fetch resolving here.
    fireEvent.click(rows[1]!);
    await waitFor(() => {
      expect(document.querySelectorAll('.projects-drill-row').length).toBe(1);
      expect(rows[0]!.getAttribute('aria-expanded')).toBe('false');
      expect(rows[1]!.getAttribute('aria-expanded')).toBe('true');
    });
    // The drill sibling now follows row 1.
    expect(rows[1]!.nextElementSibling?.classList.contains('projects-drill-row')).toBe(true);
  });

  it('tapping the selected card again removes the inline drill', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const rows = await screen.findAllByTestId('projects-table-row');
    // Leader (row 0) is auto-selected on mount; the inline drill row
    // appears immediately (no fetch dependency). Tap again to deselect.
    await waitFor(() => expect(document.querySelector('.projects-drill-row')).not.toBeNull());
    fireEvent.click(rows[0]!);
    await waitFor(() => expect(document.querySelector('.projects-drill-row')).toBeNull());
  });

  it('does NOT render the desktop-position drill below the toggle when isMobile', async () => {
    render(<ProjectsModal />);
    openWithProjects();
    const rows = await screen.findAllByTestId('projects-table-row');
    // Leader (project-1) is auto-selected; matches stubFetchOk payload.
    expect(rows[0]!.getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => expect(document.querySelector('.projects-drill-row')).not.toBeNull());
    // Drill panel resolves once `useProjectDetail` returns the matching
    // payload — assert the testid appears exclusively inside the
    // .projects-drill-row, not below the toggle.
    await waitFor(() => {
      const allDrills = document.querySelectorAll('[data-testid="projects-drill"]');
      expect(allDrills.length).toBe(1);
    });
    const inlineDrills = document.querySelectorAll('.projects-drill-row [data-testid="projects-drill"]');
    expect(inlineDrills.length).toBe(1);
  });

  it('long project keys (>40 chars) render in row 1 with the ellipsis class hook', async () => {
    const longKey = 'ccusage-subscription-stats-followups-mobile-redesign-v2';
    const env = buildProjectsEnvelope({ windowWeeks: 4, projectCount: 3 });
    // Rename the leader project so both trend and current_week rows carry
    // the long key; the leader is auto-selected on mount.
    env.projects!.trend.projects[0]!.key = longKey;
    env.projects!.current_week!.rows[0]!.key = longKey;
    updateSnapshot(env);
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    render(<ProjectsModal />);
    // The key also appears in the drill panel header (which renders
    // inline on mobile), so scope the lookup to the `.project` cell on
    // the auto-selected leader row.
    const rows = await screen.findAllByTestId('projects-table-row');
    const cell = rows[0]!.querySelector('td.project');
    expect(cell?.textContent).toBe(longKey);
    expect(cell?.classList.contains('project')).toBe(true);
  });
});

describe('ProjectsModal — desktop (>640w) unchanged regression', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    stubMobileMedia(false);
    vi.stubGlobal('fetch', stubFetchOk(buildProjectDetail('project-1')));
  });

  afterEach(() => {
    _resetKeymap();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('does NOT render the mobile sort-cycle pill', () => {
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    render(<ProjectsModal />);
    expect(screen.queryByTestId('projects-mobile-sort')).toBeNull();
  });

  it('renders the drill below the toggle (desktop position, NOT inline)', async () => {
    updateSnapshot(buildProjectsEnvelope({ windowWeeks: 4, projectCount: 5 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    render(<ProjectsModal />);
    const rows = await screen.findAllByTestId('projects-table-row');
    // Stay on the auto-selected leader (project-1) so the stubFetchOk
    // payload's `key` matches — clicking row[1] would trigger a switch
    // to project-2 and trip the stale-on-switch guard (Loading…).
    expect(rows[0]!.getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => {
      // Desktop branch: no inline .projects-drill-row inside <tbody>.
      expect(document.querySelector('.projects-drill-row')).toBeNull();
      // Drill panel still renders, but in its desktop position (below
      // the toggle, NOT as a child of the table).
      expect(document.querySelector('[data-testid="projects-drill"]')).not.toBeNull();
    });
  });
});
