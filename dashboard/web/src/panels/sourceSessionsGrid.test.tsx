import { beforeEach, describe, expect, it } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import { makeSourceEnvelope } from '../test-utils/sourceEnvelope';
import { stubMobileMedia } from '../test-utils/mobileMedia';
import { gateSessions } from '../lib/sourceGating';
import { resolveSourceView } from '../store/sourceView';
import type { ClaudeSourceData, CodexSourceData, Envelope } from '../types/envelope';

function bundleEnv(): Envelope {
  return makeSourceEnvelope() as unknown as Envelope;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('SourceSessionsGrid — Codex columns + vocabulary (§6.3)', () => {
  beforeEach(() => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });

  it('renders the full panel chrome (section + collapse toggle), not a bare shell table', () => {
    const { container } = render(<SessionsPanel />);
    expect(document.getElementById('panel-sessions')).toBeInTheDocument();
    expect(container.querySelectorAll('#panel-sessions')).toHaveLength(1);
    expect(container.querySelectorAll('#panel-sessions .panel-body--scroll')).toHaveLength(1);
    expect(
      screen.getByRole('button', { name: /Collapse Recent Sessions|Expand Recent Sessions/ }),
    ).toBeInTheDocument();
  });

  it('renders the canonical compact columns; token detail stays in drill-down', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    for (const label of ['Started', 'Dur', 'Model', 'Session', 'Project', 'Cache', 'Cost']) {
      expect(table).toHaveTextContent(label);
    }
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent?.replace('↕', ''))).toEqual(
      ['Started', 'Dur', 'Model', 'Session', 'Project', 'Cache', 'Cost'],
    );
    expect(table).not.toHaveTextContent('Reasoning');
    expect(table).toHaveTextContent('Session 1');
    expect(table).toHaveTextContent('gpt-5-codex'); // Session 2's model chip
  });

  it('gives distinct model ids distinct chip colors', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    const chips = within(table).getAllByRole('button', { name: /Filter by gpt-5/ });
    expect(chips[0].style.backgroundColor).not.toBe(chips[1].style.backgroundColor);
  });

  it('is a roving grid (role=grid, exactly one body tab stop)', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    expect(table).toHaveAttribute('role', 'grid');
    const rows = within(table)
      .getAllByRole('row')
      .filter((r) => r.classList.contains('source-session-row'));
    expect(rows).toHaveLength(2);
    expect(rows.filter((r) => r.getAttribute('tabindex') === '0')).toHaveLength(1);
  });

  it('the Session cell opens the qualified Codex detail (source-aware, not the legacy route)', () => {
    render(<SessionsPanel />);
    const btn = screen.getAllByRole('button', { name: /Open codex session details/ })[0];
    act(() => {
      btn.click();
    });
    expect(getState().openSourceDetail).toEqual({
      source: 'codex',
      resource: 'session',
      key: 'session:codex-a',
    });
  });

  it('does not expose a Codex conversation-reader affordance before Task B', () => {
    render(<SessionsPanel />);
    expect(screen.queryByRole('button', { name: 'Open conversation' })).not.toBeInTheDocument();
  });

  it('shows the canonical empty marker when Codex has no persisted short name', () => {
    const env = bundleEnv();
    const codex = env.sources?.codex?.data as CodexSourceData;
    codex.sessions.rows[0].label = null;
    updateSnapshot(env);

    render(<SessionsPanel />);

    expect(screen.getByRole('button', {
      name: 'Open codex session details: —',
    })).toBeInTheDocument();
  });

  it('a sortable header click dispatches SET_SOURCE_SESSIONS_SORT', () => {
    render(<SessionsPanel />);
    const costHeader = screen.getByText('Cost', { selector: '.th-label' });
    act(() => {
      costHeader.click();
    });
    expect(getState().sourceSessionsSort).toEqual({ column: 'cost', direction: 'desc' });
  });

  it('a search needle marks matched codex rows (highlight aligns with rendered order)', () => {
    render(<SessionsPanel />);
    act(() => {
      dispatch({ type: 'SET_SEARCH', text: 'Session 2' });
    });
    const rows = within(screen.getByTestId('codex-sessions-table'))
      .getAllByRole('row')
      .filter((r) => r.classList.contains('source-session-row'));
    // Only the matching row carries .search-match.
    expect(rows.filter((r) => r.classList.contains('search-match'))).toHaveLength(1);
    expect(rows[1].classList.contains('search-match')).toBe(true); // codex-b
  });
});

describe('SourceSessionsGrid — shared Claude structure', () => {
  it('mounts the same provider-neutral grid and canonical columns in Claude mode', () => {
    const env = bundleEnv();
    env.sessions = {
      total: 1,
      sort_key: 'started_desc',
      rows: [{
        session_id: 'session:claude-a',
        started_utc: '2026-04-24T10:00:00Z',
        duration_min: 15,
        model: 'claude-opus-4-8',
        project: 'project-00',
        project_key: 'project:claude-alpha',
        title: 'Canonical Claude task',
        cost_usd: 1.5,
      }],
    };
    updateSnapshot(env);

    render(<SessionsPanel />);

    const table = screen.getByTestId('claude-sessions-table');
    expect(table).toHaveClass('source-sess-table');
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent?.replace('↕', ''))).toEqual(
      ['Started', 'Dur', 'Session', 'Project', 'Cache', 'Cost'],
    );
    expect(within(table).getAllByRole('row').some((row) => row.classList.contains('source-session-row'))).toBe(true);
  });
});

describe('SourceSessionsGrid — All-mode interleave (§6.3)', () => {
  beforeEach(() => {
    updateSnapshot(bundleEnv());
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
  });

  it('renders one interleaved grid with a per-row source chip (both providers present)', () => {
    render(<SessionsPanel />);
    const table = screen.getByTestId('source-sessions-table');
    const chipLabels = within(table)
      .getAllByText(/Claude|Codex/, { selector: '.source-chip' })
      .map((c) => c.textContent);
    expect(chipLabels).toContain('Claude');
    expect(chipLabels).toContain('Codex');
    // Three interleaved rows (2 codex + 1 claude), recency-ordered.
    const rows = within(table)
      .getAllByRole('row')
      .filter((r) => r.classList.contains('source-session-row'));
    expect(rows).toHaveLength(3);
    expect(rows[2].getAttribute('data-detail-source')).toBe('claude'); // oldest
  });

  it('routes an All-mode Claude row through its opaque qualified identity', () => {
    render(<SessionsPanel />);
    const button = screen.getByRole('button', {
      name: 'Open claude session details: —',
    });
    act(() => button.click());
    expect(getState().openSourceDetail).toEqual({
      source: 'claude',
      resource: 'session',
      key: 'session:claude-a',
    });
    expect(getState().openModal).toBeNull();
  });

  const NOTE = 'both providers, interleaved';

  it('states its composition on the shared sub-line under All', () => {
    const { container } = render(<SessionsPanel />);
    // Precondition: the All grid really painted.
    expect(screen.getByTestId('source-sessions-table')).toBeInTheDocument();
    const notes = container.querySelectorAll('.panel-range-note');
    expect(notes).toHaveLength(1);
    expect(notes[0].textContent).toBe(NOTE);
  });

  it('renders the note as a panel-level sibling of the header, never inside it', () => {
    const { container } = render(<SessionsPanel />);
    const section = container.querySelector('#panel-sessions')!;
    const note = container.querySelector('.panel-range-note')!;
    expect(note).not.toBeNull();
    // The mobile h2 is nowrap + ellipsis, so the note must be outside the header.
    expect(container.querySelector('.panel-header .panel-range-note')).toBeNull();
    // ...and outside the scrolling body too, which the check above would miss.
    expect(note.parentElement).toBe(section);
    expect(section.querySelector(':scope > .panel-header')).not.toBeNull();
  });

  it('orders header, then note, then controls on mobile', () => {
    stubMobileMedia(true);
    const { container } = render(<SessionsPanel />);
    const section = container.querySelector('#panel-sessions')!;
    // Precondition: the mobile controls strip really rendered, so the order
    // assertion below cannot be satisfied by its absence.
    expect(section.querySelector(':scope > .sessions-ctrls')).not.toBeNull();
    const order = [...section.children]
      .map((el) =>
        el.classList.contains('panel-header') ? 'header'
        : el.classList.contains('panel-range-note') ? 'note'
        : el.classList.contains('sessions-ctrls') ? 'controls'
        : null)
      .filter((k): k is 'header' | 'note' | 'controls' => k !== null);
    expect(order).toEqual(['header', 'note', 'controls']);
  });

  it('keeps the same note when only one provider contributed rows', () => {
    const env = bundleEnv();
    (env.sources!.codex!.data as CodexSourceData).sessions.rows = [];
    act(() => {
      updateSnapshot(env);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    });

    const { container } = render(<SessionsPanel />);
    const table = screen.getByTestId('source-sessions-table');
    // Precondition: the painted rows really are single-provider. Counted from
    // the rendered chips rather than trusted from the fixture, because the All
    // adapter can fall back from nested provider data to sibling source
    // entries (lib/sourceRows.ts:174).
    const chips = [...table.querySelectorAll('.source-chip')].map((c) => c.textContent);
    expect(chips.length).toBeGreaterThan(0);
    expect(new Set(chips)).toEqual(new Set(['Claude']));

    expect(container.querySelector('.panel-range-note')!.textContent).toBe(NOTE);
  });

  it('keeps the note when All has no rows at all', () => {
    const env = bundleEnv();
    // Clear EVERY leg `collectSourceSessionRows` reads for All, not just one.
    // Under All the Claude rows come from the SOURCES bundle, not from
    // `env.sessions` — the Claude session row is seeded inside `sources.claude`
    // (`test-utils/sourceEnvelope.ts`, `makeClaudeSourceData`). Clear that leg
    // as well as the Codex one. The `all` entry's `providers` block holds the
    // SAME two `data` objects, so clearing the siblings clears both reads.
    // The precondition assertion below is what proves you cleared the right
    // ones: if the empty marker is absent, the fixture is wrong, not the test.
    (env.sources!.claude!.data as ClaudeSourceData).sessions.rows = [];
    (env.sources!.codex!.data as CodexSourceData).sessions.rows = [];
    act(() => {
      updateSnapshot(env);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    });

    const { container } = render(<SessionsPanel />);
    expect(screen.getByTestId('source-sessions-empty')).toBeInTheDocument();
    expect(container.querySelector('.panel-range-note')!.textContent).toBe(NOTE);
  });

  it('keeps the note when the panel is collapsed', () => {
    const { container } = render(<SessionsPanel />);
    act(() => {
      dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: true } });
    });
    const section = container.querySelector('#panel-sessions')!;
    // Precondition: the panel really is in its collapsed presentation.
    expect(section.classList.contains('sessions-collapsed')).toBe(true);
    expect(container.querySelector('.panel-range-note')!.textContent).toBe(NOTE);
  });

  it('keeps the note while the panel is still loading', () => {
    const env = bundleEnv();
    // The hydrating ENTRY SHAPE, which is what `isHydratingEntry` keys off —
    // not `availability`, which the server publishes as `partial` for the
    // hydrating seed (store/sourceView.ts:35-50). Both children must take it:
    // `gateSessions` only reports `skeleton` for All when neither child is
    // renderable and at least one is hydrating.
    for (const source of ['claude', 'codex'] as const) {
      Object.assign(env.sources![source]!, {
        data: null,
        last_success_at: null,
        warnings: [],
        capabilities: {},
      });
    }
    act(() => {
      updateSnapshot(env);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    });

    const { container } = render(<SessionsPanel />);
    // Precondition: the panel really is painting its loading state, rather
    // than an empty table that would make the note assertion say nothing.
    expect(container.querySelector('.panel-skeleton')).not.toBeNull();
    expect(container.querySelector('.panel-range-note')!.textContent).toBe(NOTE);
  });

  it('does not claim an ordering: the note survives a column sort unchanged', () => {
    const { container } = render(<SessionsPanel />);
    const before = container.querySelector('.panel-range-note')!.textContent;
    const costHeader = screen.getByText('Cost', { selector: '.th-label' });
    act(() => {
      costHeader.click();
    });
    // Precondition: the sort really took effect, so the table is no longer
    // recency-ordered while the note is still on screen.
    expect(getState().sourceSessionsSort).toEqual({ column: 'cost', direction: 'desc' });
    expect(container.querySelector('.panel-range-note')!.textContent).toBe(before);
    expect(before).not.toMatch(/recency/);
  });

  it('never reports a degraded gate under All, even when a provider is degraded', () => {
    const env = bundleEnv();
    // Make the Codex child genuinely degraded. `gatePhysicalSessions`
    // (lib/sourceGating.ts, just above `gateSessions`) has exactly two
    // degraded exits: `availability === 'partial'` combined with either a
    // stale `sessions` domain freshness or a non-null warning, and a `status`
    // outside `supported`/`derived`. This takes the first.
    env.sources!.codex!.availability = 'partial';
    env.sources!.codex!.domain_freshness = { hero: 'fresh', quota: 'fresh', sessions: 'stale' };
    // The assertion on the next line is the proof the degraded child was
    // actually constructed; do not weaken it to make the test pass.
    expect(gateSessions(resolveSourceView(env, 'codex')).mode).toBe('degraded');
    // Yet All collapses that to `render`, which is why §5 of the spec lists no
    // degraded row: such a test would have run in ordinary render mode and
    // asserted nothing.
    expect(gateSessions(resolveSourceView(env, 'all')).mode).toBe('render');
  });
});

describe('SourceSessionsGrid — the composition sub-line is All-only (#572)', () => {
  const CLAUDE_ROWS = [{
    session_id: 'session:claude-a',
    started_utc: '2026-04-24T10:00:00Z',
    duration_min: 15,
    model: 'claude-opus-4-8',
    project: 'project-00',
    project_key: 'project:claude-alpha',
    title: 'Canonical Claude task',
    cost_usd: 1.5,
  }];

  it.each([
    ['claude', 'claude-sessions-table'],
    ['codex', 'codex-sessions-table'],
  ] as const)('renders no composition sub-line under %s', (source, tableTestId) => {
    const env = bundleEnv();
    env.sessions = { total: 1, sort_key: 'started_desc', rows: CLAUDE_ROWS };
    act(() => {
      updateSnapshot(env);
      dispatch({ type: 'SET_ACTIVE_SOURCE', source });
    });

    const { container } = render(<SessionsPanel />);
    // Precondition: that provider's own grid really painted, so the absence
    // below is not satisfied by a panel that failed to render.
    expect(screen.getByTestId(tableTestId)).toBeInTheDocument();
    expect(container.querySelector('.panel-range-note')).toBeNull();
  });
});
