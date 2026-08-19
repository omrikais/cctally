// #620 S1 D2 (F7), D3 (F8), D13 (F4) and D1's merged-fold clause — every
// figure states its population where a touch user can read it.
//
// A `title` attribute never appears on touch: there is no hover, and a
// long-press opens the platform's own menu instead. So each disclosure below
// must be RENDERED TEXT. The existing `title`s stay; they are simply no longer
// the only place the definition lives.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { ProjectsModal } from '../src/modals/ProjectsModal';
import { ProjectsPanel } from '../src/panels/ProjectsPanel';
import { ConversationRail } from '../src/conversations/ConversationRail';
import { PROJECTS_COLUMNS } from '../src/lib/projectsColumns';
import { _resetForTests, dispatch, updateSnapshot } from '../src/store/store';
import { clearRailPrefs } from '../src/store/conversationRailPrefs';
import { makeSourceEnvelope, makeClaudeSourceEntry } from '../src/test-utils/sourceEnvelope';
import type { Envelope, ProjectsEnvelope } from '../src/types/envelope';
import type { ConversationSummary } from '../src/types/conversation';

// ── rail harness ────────────────────────────────────────────────────────────
// The same stubbing convention `ConversationRail.test.tsx` uses: the data hooks
// are replaced so the render is driven by fixtures rather than live fetches.
let browseRows: ConversationSummary[] = [];

vi.mock('../src/hooks/useConversations', () => ({
  useConversations: () => ({
    rows: browseRows, loading: false, error: null, hasMore: false,
    loadMore: () => {}, loadingMore: false,
    filterDegraded: false, sortDegraded: false, retry: () => {},
  }),
}));
vi.mock('../src/conversations/ConversationFiltersPopover', () => ({
  ConversationFiltersPopover: () => <div data-testid="filters-popover" />,
}));
vi.mock('../src/hooks/useConversationSearch', () => ({
  useConversationSearch: () => ({
    hits: [], mode: 'fts', total: 0, loading: false, loadingMore: false,
    searchDepth: 'full', filterDegraded: false, error: null, loadMore: () => {},
  }),
}));
vi.mock('../src/hooks/useDisplayTz', () => ({
  useDisplayTz: () => ({
    tz: 'utc', resolvedTz: 'Etc/UTC', offsetLabel: 'UTC', offsetSeconds: 0, pinned: false,
  }),
}));

// ── projects fixtures ───────────────────────────────────────────────────────

const WEEKS = 4;

function projectsEnvelope(): ProjectsEnvelope {
  const projects = [1, 2, 3].map((n) => ({
    key: `project-${n}`,
    bucket_path: `/repos/project-${n}`,
    weekly_cost: Array.from({ length: WEEKS }, (_, j) => 10 * n + j),
    weekly_pct: Array.from({ length: WEEKS }, (_, j) => n + j * 0.5),
    sessions_per_week: Array.from({ length: WEEKS }, () => 1),
    first_seen_per_week: Array.from({ length: WEEKS }, () => '2026-04-01T00:00:00Z'),
    last_seen_per_week: Array.from({ length: WEEKS }, () => '2026-04-01T23:00:00Z'),
  }));
  return {
    current_week: {
      week_label: 'wk May 13',
      week_start_date: '2026-05-13',
      week_start_at: '2026-05-13T00:00:00Z',
      total_cost_usd: 60,
      rows: projects.map((p, i) => ({
        key: p.key,
        bucket_path: p.bucket_path,
        cost_usd: 20 - i,
        attributed_pct: 10 - i,
        sessions_count: 3,
      })),
    },
    trend: {
      window_weeks: WEEKS,
      weeks: Array.from({ length: WEEKS }, (_, j) => ({
        week_start_date: `2026-04-0${j + 1}`,
        week_label: `wk0${j + 1}`,
        total_cost_usd: 100 + j,
        total_pct: 10 + j,
      })),
      projects,
    },
  } as unknown as ProjectsEnvelope;
}

function projectsEnv(opts: { accounts?: number } = {}): Envelope {
  const slice = makeSourceEnvelope();
  const claude = makeClaudeSourceEntry();
  if (opts.accounts != null && opts.accounts > 1) {
    // The R8 decoration signal: `accounts[]` appears only above one REAL
    // account per provider, which is exactly the state D1's merged-fold clause
    // describes.
    (claude.data as unknown as { accounts: unknown[] }).accounts =
      Array.from({ length: opts.accounts }, (_, i) => ({
        key: `acct-${i}`, label: `Account ${i}`,
      }));
  }
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null,
      forecast_verdict: 'ok', vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: projectsEnvelope(),
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [],
    },
    ...slice,
    sources: { ...slice.sources, claude },
  } as unknown as Envelope;
}

/** Every rendered `title` on the element, so a test can prove a definition is
 *  NOT only in a tooltip. */
function visibleText(container: HTMLElement): string {
  return container.textContent ?? '';
}

beforeEach(() => {
  localStorage.clear();
  clearRailPrefs();
  _resetForTests();
  browseRows = [];
});

describe('#620 S1 D2 (F7) — Used pp is self-describing', () => {
  it('the column header states it is a sum of weekly points', () => {
    // The id is the persisted sort key, so it stays `used_pct`; only the
    // rendered label changes.
    const column = PROJECTS_COLUMNS.find((c) => c.id === 'used_pct');
    expect(column).toBeDefined();
    expect(column?.label.toLowerCase()).toContain('sum');
    expect(column?.label.toLowerCase()).toContain('pp');
    // The tooltip stays; it is simply no longer the only disclosure.
    expect(column?.title).toBeTruthy();
  });

  it('a visible caption under the window selector gives the definition', () => {
    updateSnapshot(projectsEnv());
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    const { container } = render(<ProjectsModal />);

    const controls = container.querySelector('.projects-controls');
    expect(controls).not.toBeNull();
    const caption = container.querySelector('.projects-caption');
    expect(caption).not.toBeNull();
    // Under the selector, not above it: the caption explains the columns the
    // selector reshapes.
    const position = (controls as Element).compareDocumentPosition(caption as Node);
    expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const text = (caption?.textContent ?? '').toLowerCase();
    expect(text).toContain('percentage point');
    expect(text).toContain('week');
    // A `title` alone would not satisfy this — the definition is in the
    // rendered text.
    expect(visibleText(container).toLowerCase()).toContain('percentage point');
  });
});

describe('#620 S1 D13 (F4) — the cost-share denominator is visible without hover', () => {
  it('names total project spend in the selected window, in rendered text', () => {
    updateSnapshot(projectsEnv());
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    const { container } = render(<ProjectsModal />);

    const caption = container.querySelector('.projects-caption');
    const text = (caption?.textContent ?? '').toLowerCase();
    expect(text).toContain('cost share');
    expect(text).toContain('total project spend');
  });

  it('adds no second column — the existing one already carries the figure', () => {
    const shareColumns = PROJECTS_COLUMNS.filter(
      (c) => c.id === 'share_of_window' || c.label.toLowerCase().includes('share'),
    );
    expect(shareColumns.length).toBe(1);
  });
});

describe('#620 S1 D1 — the merged fold across accounts is stated on the surface', () => {
  it('the Projects panel states the rule when the provider has more than one account', () => {
    updateSnapshot(projectsEnv({ accounts: 2 }));
    const { container } = render(<ProjectsPanel />);

    const note = container.querySelector('.projects-merged-note');
    expect(note).not.toBeNull();
    const text = (note?.textContent ?? '').toLowerCase();
    expect(text).toContain('account');
    // The other half of D1 — never summed — stated in words rather than only
    // enforced in the server arithmetic.
    expect(text).toMatch(/never (added|summed)/);
  });

  it('says nothing on a single-account install, where nothing is merged', () => {
    updateSnapshot(projectsEnv());
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelector('.projects-merged-note')).toBeNull();
  });

  it('the Projects modal states the same rule over the same fold', () => {
    updateSnapshot(projectsEnv({ accounts: 2 }));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    const { container } = render(<ProjectsModal />);
    expect(container.querySelector('.projects-merged-note')).not.toBeNull();
  });

  // The sentence names a Claude fold. On the Codex tab the ranking below it is
  // a Codex ranking, so the sentence would describe a population the figures
  // are not computed over — the exact failure this session exists to remove.
  it('the Projects panel says nothing on the Codex tab, whose ranking folds no Claude accounts', () => {
    updateSnapshot(projectsEnv({ accounts: 2 }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const { container } = render(<ProjectsPanel />);
    // Asserted at call time: the panel really is rendering its Codex ranking.
    // Without this the absence below would also hold on an empty, withheld or
    // unavailable panel, which renders no note either way.
    expect(container.querySelector('.projects-legend')).not.toBeNull();
    expect(container.querySelector('.projects-merged-note')).toBeNull();
  });

  it('the Projects modal says nothing on the Codex tab either', () => {
    updateSnapshot(projectsEnv({ accounts: 2 }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    const { container } = render(<ProjectsModal />);
    // The caption block that would carry the note is rendered on every tab, so
    // its presence proves the absence below is a gate and not a dead branch.
    expect(container.querySelector('.projects-caption')).not.toBeNull();
    expect(container.querySelector('.projects-merged-note')).toBeNull();
  });

  // Under All the ranking genuinely does fold every Claude account into one
  // row set, so the sentence stays true there and must keep rendering.
  it('the Projects panel keeps the note under All, where the fold is real', () => {
    updateSnapshot(projectsEnv({ accounts: 2 }));
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    const { container } = render(<ProjectsPanel />);
    expect(container.querySelector('.projects-legend')).not.toBeNull();
    expect(container.querySelector('.projects-merged-note')).not.toBeNull();
  });
});

describe('#620 S1 D3 (F8) — the rail states its message population', () => {
  it('says the count includes subagent sidechains, as rendered text', () => {
    browseRows = [{
      session_id: 's1',
      title: 'a conversation',
      project_label: 'proj',
      git_branch: 'main',
      started_utc: '2026-06-09T01:00:00Z',
      last_activity_utc: '2026-06-09T02:00:00Z',
      msg_count: 4,
      cost_usd: 1.25,
      models: ['claude-opus-4'],
    }];
    const { container } = render(<ConversationRail />);

    const note = container.querySelector('.conv-rail-population-note');
    expect(note).not.toBeNull();
    expect((note?.textContent ?? '').toLowerCase()).toContain('subagent');
  });

  it('states it unconditionally — the count always includes them', () => {
    browseRows = [];
    const { container } = render(<ConversationRail />);
    expect(container.querySelector('.conv-rail-population-note')).not.toBeNull();
  });
});
