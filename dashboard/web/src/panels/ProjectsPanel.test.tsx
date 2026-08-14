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
import fixture from '../../__tests__/fixtures/envelope.json';
import { dispatch } from '../store/store';

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

  it('null-envelope branch still renders the ShareIcon (spec §2.6)', () => {
    // The "ShareIcon still visible" guarantee from spec §2.6 extends to
    // the unavailable-envelope case so users keep the share affordance
    // even when the panel has no data to render — share kernel handles
    // empty envelope shape per spec §7.4.
    updateSnapshot(baseEnvelope());  // projects: null
    render(<ProjectsPanel />);
    const shareBtn = document.querySelector('#projects-panel');
    expect(shareBtn).not.toBeNull();
    expect(shareBtn?.getAttribute('data-share-panel')).toBe('projects');
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
    // `.pct` now also carries an `.sr-only` unit, so the VISIBLE glyph is read
    // from `.pct-value`. The unit is asserted separately below.
    expect(row.querySelector('.pct .pct-value')?.textContent).toBe('—');
    expect(row.querySelector('.pct .sr-only')?.textContent)
      .toBe(' no share of quota');
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

// #556 S2 Task 10 — the All ranking states its range, names its percentage,
// and does not offer a drill it cannot serve.
describe('<ProjectsPanel /> under the All selection', () => {
  function renderAll() {
    updateSnapshot(structuredClone(fixture) as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    return render(<ProjectsPanel />);
  }

  it('states the resolved dates of the shared range in its header', () => {
    // §4.4 — `(N this week)` was printed over a ranking that is not a week.
    // The Claude leg was bounded by the subscription week and the Codex leg by
    // roughly thirty days, so the sub-span named a period neither leg covered.
    const { container } = renderAll();
    // The range lives on the wrapping sub-line beneath the h2, not inside it:
    // at 390px the h2 ellipsised it away (QA P1-2).
    expect(container.querySelector('.panel-range-note')?.textContent)
      .toContain('Mar 26 – Apr 24');
    const sub = container.querySelector('.panel-header h2 .sub');
    expect(sub?.textContent).not.toContain('this week');
  });

  it('names the metric the percentage column reports', () => {
    // §4.2 — the figure is a share of COST under All and Codex and a share of
    // QUOTA under Claude, in the same visual slot, with nothing saying which.
    const { container } = renderAll();
    expect(container.querySelector('.projects-legend')?.textContent)
      .toContain('share of cost');
  });

  it('names the quota metric on the Claude tab, in the same slot', () => {
    updateSnapshot(envelopeWithProjects(3));
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelector('.projects-legend')?.textContent)
      .toContain('share of quota');
  });

  it('puts the cost and the named percentage in each row accessible name', () => {
    // §4.5 — the comment justifying `aria-hidden` on the cost bar asserted the
    // row "already names the project, cost, and %". It did not.
    renderAll();
    const row = screen.getByRole('button', {
      name: /Open claude project details: project-00/,
    });
    expect(row.getAttribute('aria-label')).toContain('$9.50');
    expect(row.getAttribute('aria-label')).toContain('share of cost');
  });

  it('publishes an undrillable row without offering a drill', () => {
    // §3.8a — the projects envelope the drill resolves against drops any entry
    // after the current week's nominal end, so a project active between a
    // rollover and the re-anchoring is ranked and routes nowhere. The row keeps
    // its rank, its label and its cost; only the broken interaction goes away.
    const { container } = renderAll();
    const row = container.querySelector('[data-project-key="project:agg-orphan"]');
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain('project-rolled-over');
    expect(row!.textContent).toContain('$1.75');

    expect(row!.getAttribute('role')).toBeNull();
    expect(row!.getAttribute('tabindex')).toBeNull();
    expect(row!.getAttribute('data-drillable')).toBe('false');
    expect(
      screen.queryByRole('button', { name: /project-rolled-over/ }),
    ).toBeNull();
  });

  it('states the resolved dates in the empty state too', () => {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.claude.data!.projects.aggregate = { rows: [] };
    env.sources!.codex.data!.projects.rows = [];
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelector('.panel-empty')?.textContent)
      .toContain('Mar 26 – Apr 24');
  });
});

// #556 S2 Task 16 — the withheld state, and the title that says what the panel
// is composed of.
describe('<ProjectsPanel /> withheld and titled', () => {
  function withOutcome(outcome: Record<string, unknown>): Envelope {
    const env = structuredClone(fixture) as unknown as Envelope;
    env.sources!.all.data!.aggregates!.projects = outcome as never;
    updateSnapshot(env);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    return env;
  }

  it('states its composition under All and leaves the Claude tab alone', () => {
    updateSnapshot(structuredClone(fixture) as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container, unmount } = render(<ProjectsPanel />);
    // Stated in the header BLOCK, on the sub-line the range shares — not
    // inside the h2, which ellipsises at phone width.
    expect(container.querySelector('.panel-range-note')!.textContent)
      .toContain('by provider');
    unmount();

    updateSnapshot(envelopeWithProjects(3));
    const claude = render(<ProjectsPanel />);
    expect(claude.container.textContent).not.toContain('by provider');
  });

  it('renders a withheld outcome as its own state, naming the cause', () => {
    withOutcome({ state: 'withheld', code: 'provider_incoherent', provider: 'codex' });
    const { container } = render(<ProjectsPanel />);
    const block = container.querySelector('.panel-withheld');
    expect(block).not.toBeNull();
    expect(block!.textContent).toContain('Codex data is out of date');
    // Distinct from BOTH of today's states.
    expect(container.textContent).not.toContain('restart the dashboard');
    expect(container.textContent).not.toContain('No project activity');
  });

  it('renders generic copy for a code this build has never heard of', () => {
    withOutcome({ state: 'withheld', code: 'some_future_code' });
    const { container } = render(<ProjectsPanel />);
    const block = container.querySelector('.panel-withheld');
    expect(block!.textContent).toContain('withheld');
    expect(block!.textContent).toContain('some_future_code');
  });

  it('never renders a silently empty table for a withheld outcome', () => {
    withOutcome({ state: 'withheld', code: 'range_unresolved' });
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelectorAll('.projects-row')).toHaveLength(0);
    expect(container.querySelector('.panel-withheld')!.textContent!.length)
      .toBeGreaterThan(0);
  });
});


// #556 S2 QA P1-1 — a COLD load of the All tab must not accuse the server.
//
// The store's initial snapshot is `null` and `activeSource` is persisted, so a
// user whose last selection was All renders once before the bootstrap response
// arrives. `presentationProjects(null, 'all')` synthesizes `rows_absent` for
// that null envelope, and testing `withheld` ahead of `hydrating` printed "This
// page is talking to a server that does not publish the combined ranking.
// Reload to pick up the current one." — a nonexistent server-version problem
// with an action attached. #278 §1.4 added the hydrating branch precisely so a
// first paint shows a skeleton instead of copy implying a broken instance.
describe('<ProjectsPanel /> on a cold load of the All tab', () => {
  it('shows the hydrating skeleton, not the wrong-server copy', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<ProjectsPanel />);

    expect(container.querySelector('.panel-skeleton')).not.toBeNull();
    expect(container.querySelector('.panel-withheld')).toBeNull();
    expect(container.textContent).not.toContain('does not publish');
    expect(container.textContent).not.toContain('Reload to pick up');
  });

  it('keeps the header on "(loading)" rather than "(withheld)"', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<ProjectsPanel />);
    const sub = container.querySelector('.panel-header h2 .sub');
    expect(sub?.textContent).toBe('(loading)');
  });
});

// #556 S2 QA P1-2 / P2-7 / P3 — the header block at phone width, and what a
// non-drillable row tells someone who cannot see the cursor.
describe('<ProjectsPanel /> header block and row accessibility', () => {
  function renderAllPanel() {
    updateSnapshot(structuredClone(fixture) as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    return render(<ProjectsPanel />);
  }

  it('keeps the range OUT of the h2 and on a wrapping sub-line', () => {
    // Measured at 390px: the h2 had clientWidth 174px against scrollWidth
    // 385px under `white-space: nowrap` / `text-overflow: ellipsis`, so it
    // rendered "Projects (41 projects…" — 45% of the string, and the resolved
    // range, which is the entire point of the change, was the part cut off.
    // The range moves to a full-width sub-line beneath the header, where it
    // wraps. It is NOT shortened and NOT truncated further.
    const { container } = renderAllPanel();
    const h2 = container.querySelector('.panel-header h2')!.textContent!;
    expect(h2).toContain('Projects');
    expect(h2).not.toContain('Mar 26 – Apr 24');
    expect(h2).not.toContain('by provider');

    const note = container.querySelector('.panel-range-note')!;
    expect(note.textContent).toContain('Mar 26 – Apr 24');
    expect(note.textContent).toContain('by provider');
  });

  it('states the range but NOT "by provider" on the single-provider Codex tab', () => {
    updateSnapshot(structuredClone(fixture) as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<ProjectsPanel />);

    expect(container.querySelector('.panel-header h2')!.textContent)
      .not.toContain('by provider');
    const note = container.querySelector('.panel-range-note')!;
    expect(note.textContent).toContain('Mar 26 – Apr 24');
    expect(note.textContent).not.toContain('by provider');
  });

  it('renders no sub-line at all on the Claude tab', () => {
    updateSnapshot(envelopeWithProjects(3));
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelector('.panel-range-note')).toBeNull();
    expect(container.querySelector('.panel-header h2')!.textContent)
      .toContain('this week');
  });

  it('names the metric on a non-drillable row, which has no aria-label', () => {
    // The metric name entered only the drillable rows' accessible names, so a
    // non-drillable row announced its percentage as a bare number.
    const { container } = renderAllPanel();
    const row = container.querySelector('[data-drillable="false"]')!;
    expect(row.textContent).toContain('share of cost');
  });

  it('says WHY a non-drillable row has no detail, not only in a title', () => {
    // A `title` attribute is not a touch disclosure and a screen reader does
    // not read one on a non-focusable element, so the reason reached nobody
    // who could not hover.
    const { container } = renderAllPanel();
    const row = container.querySelector('[data-drillable="false"]')!;
    const reason = row.querySelector('[data-nodrill-reason]')!;
    expect(reason.textContent).toMatch(/no detail view/i);
  });
});
