import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { Envelope, SessionRow } from '../types/envelope';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

function baseEnvelope(): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    last_sync_at: null, sync_age_s: null, last_sync_error: null,
    header: {
      week_label: 'wk May 13', used_pct: 0, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  };
}

function sessRow(over: Partial<SessionRow>): SessionRow {
  return {
    session_id: 's1', started_utc: '2026-05-13T09:00:00Z', duration_min: 12,
    model: 'claude-opus-4-8', project: 'p', project_key: 'p', cost_usd: 1.0, ...over,
  };
}

describe('SessionsPanel project-cell title (#207 C4)', () => {
  it('puts the full project name in the resolved button title', () => {
    const env = baseEnvelope();
    const long = 'a-very-long-monorepo-project-key-that-would-truncate';
    env.sessions = { total: 1, sort_key: 'started_desc',
      rows: [sessRow({ project: long, project_key: long })] };
    updateSnapshot(env);
    render(<SessionsPanel />);
    const btn = screen.getByRole('button', { name: `Open Projects modal for ${long}` });
    expect(btn).toHaveAttribute('title', long);
  });
});

describe('#264 SESS-1 — single-model drops the Model column entirely', () => {
  it('single-model: no Model header, no ditto, keeps the caption; Session + Cache headers present', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', model: 'claude-opus-4-8', project: 'alpha', project_key: 'alpha' }),
      sessRow({ session_id: 'b', model: 'claude-opus-4-8', project: 'beta', project_key: 'beta' }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    // Caption stays as the single-model signpost.
    expect(container.querySelector('.sess-model-caption')?.textContent).toContain('opus-4-8');
    // Model column gone ENTIRELY — no header, no filter chip, no ditto middot.
    expect(container.querySelector('thead th[data-col="model"]')).toBeNull();
    expect(container.querySelector('.model-chip')).toBeNull();
    expect(container.querySelector('.model-ditto')).toBeNull();
    // The dead single-model table class is removed.
    expect(container.querySelector('table.sess-table')?.classList.contains('single-model')).toBe(false);
    // Session + Cache headers are present regardless.
    expect(container.querySelector('thead th[data-col="session"]')).not.toBeNull();
    expect(container.querySelector('thead th[data-col="cache"]')).not.toBeNull();
    // Six columns rendered (started, dur, session, project, cache, cost).
    expect(container.querySelectorAll('thead th.th-sortable').length).toBe(6);
  });

  it('multi-model: restores the Model column (header + chips), still no ditto', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', model: 'claude-opus-4-8' }),
      sessRow({ session_id: 'b', model: 'claude-sonnet-5' }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('.sess-model-caption')).toBeNull();
    expect(container.querySelector('thead th[data-col="model"]')).not.toBeNull();
    expect(container.querySelectorAll('.model-chip').length).toBe(2);
    expect(container.querySelector('.model-ditto')).toBeNull();
    // Seven columns (started, dur, model, session, project, cache, cost).
    expect(container.querySelectorAll('thead th.th-sortable').length).toBe(7);
  });
});

describe('#264 S4 (MOB-1/B2) — class-based mobile Sessions card cells', () => {
  // Codex pre-plan P1: the mobile card grid must key off STABLE cell classes,
  // not nth-child. In single-model mode the model <td> isn't rendered, so an
  // nth-child(3)-keyed layout shifts the model grid-area onto the Session cell.
  // Guard both row shapes: the model cell carries .model.model-chip-cell only
  // in multi-model, the duration cell always carries .dur, and td.session is
  // always present as the new primary line.
  it('multi-model rows tag the model cell with .model.model-chip-cell (class-based, not nth-child)', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', model: 'claude-opus-4-8' }),
      sessRow({ session_id: 'b', model: 'claude-sonnet-5' }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('td.model.model-chip-cell')).toBeTruthy();
    expect(container.querySelector('td.dur')).toBeTruthy();
    expect(container.querySelector('td.session')).toBeTruthy();
  });

  it('single-model rows omit the model cell entirely; Session + dur cells still present', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', model: 'claude-opus-4-8', project: 'alpha', project_key: 'alpha' }),
      sessRow({ session_id: 'b', model: 'claude-opus-4-8', project: 'beta', project_key: 'beta' }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('td.model.model-chip-cell')).toBeNull();
    expect(container.querySelector('td.session')).toBeTruthy();
    expect(container.querySelector('td.dur')).toBeTruthy();
  });
});

describe('#264 SESS-2 — Session (title) + Cache cells', () => {
  it('renders the title in the Session cell; null title → muted em-dash', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', started_utc: '2026-05-13T09:00:00Z', title: 'Rebuild bundle' }),
      sessRow({ session_id: 'b', started_utc: '2026-05-13T08:00:00Z', title: null }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    const cells = container.querySelectorAll('td.session');
    expect(cells.length).toBe(2);
    // Row a: real title text; title= tooltip set.
    expect(cells[0].textContent).toContain('Rebuild bundle');
    expect(cells[0].getAttribute('title')).toBe('Rebuild bundle');
    // Row b: null title → muted em-dash placeholder, no tooltip.
    expect(cells[1].querySelector('.sess-title-empty')?.textContent).toBe('—');
    expect(cells[1].getAttribute('title')).toBeNull();
  });

  it('renders cache_hit_pct as NN%; null → em-dash', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', started_utc: '2026-05-13T09:00:00Z', cache_hit_pct: 94 }),
      sessRow({ session_id: 'b', started_utc: '2026-05-13T08:00:00Z', cache_hit_pct: null }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    const cells = container.querySelectorAll('td.cache');
    expect(cells.length).toBe(2);
    expect(cells[0].textContent).toBe('94%');
    expect(cells[1].textContent).toBe('—');
  });
});

describe('#253 SESS-2 — current-match emphasis + in-cell marks', () => {
  function threeRowEnv(): Envelope {
    const env = baseEnvelope();
    // started_desc render order: s1, s2, s3.
    env.sessions = { total: 3, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 's1', started_utc: '2026-05-13T09:00:00Z', project: 'alpha', project_key: 'alpha' }),
      sessRow({ session_id: 's2', started_utc: '2026-05-13T08:00:00Z', project: 'alphabeta', project_key: 'alphabeta' }),
      sessRow({ session_id: 's3', started_utc: '2026-05-13T07:00:00Z', project: 'gamma', project_key: 'gamma' }),
    ] };
    return env;
  }

  it('marks exactly one row as aria-current + search-match-current (the searchIndex row)', () => {
    updateSnapshot(threeRowEnv());
    dispatch({ type: 'SET_SEARCH', text: 'alpha' });   // matches s1 + s2, index 0 → s1
    const { container } = render(<SessionsPanel />);
    const current = container.querySelectorAll('tr[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].getAttribute('data-session-id')).toBe('s1');
    expect(current[0].classList.contains('search-match-current')).toBe(true);
    // both matches carry the base wash
    expect(container.querySelectorAll('tr.session-row.search-match').length).toBe(2);
  });

  it('moves aria-current when the searchIndex steps', () => {
    updateSnapshot(threeRowEnv());
    dispatch({ type: 'SET_SEARCH', text: 'alpha' });
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('tr[aria-current="true"]')?.getAttribute('data-session-id')).toBe('s1');
    act(() => {
      dispatch({ type: 'SET_SEARCH_MATCHES', matches: [0, 1], index: 1 });
    });
    const current = container.querySelectorAll('tr[aria-current="true"]');
    expect(current.length).toBe(1);
    expect(current[0].getAttribute('data-session-id')).toBe('s2');
  });

  it('has no current row when there are zero matches', () => {
    updateSnapshot(threeRowEnv());
    dispatch({ type: 'SET_SEARCH', text: 'zzz-no-match' });
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('tr[aria-current="true"]')).toBeNull();
    expect(container.querySelector('tr.session-row.search-match-current')).toBeNull();
    expect(container.querySelector('#sess-rows mark')).toBeNull();
  });

  it('marks the matched substring in a matched cell but not in a non-matching cell', () => {
    updateSnapshot(threeRowEnv());
    dispatch({ type: 'SET_SEARCH', text: 'alpha' });
    const { container } = render(<SessionsPanel />);
    const s1Row = container.querySelector('tr[data-session-id="s1"]')!;
    const s3Row = container.querySelector('tr[data-session-id="s3"]')!;
    const s1Marks = Array.from(s1Row.querySelectorAll('mark')).map((m) => m.textContent);
    expect(s1Marks).toContain('alpha');       // project cell "alpha" is marked
    expect(s3Row.querySelector('mark')).toBeNull();  // "gamma" has no 'alpha'
  });
});

describe('SessionsPanel row-open button (#293 S2 A11Y-2)', () => {
  function renderWith(rows: SessionRow[]) {
    const env = baseEnvelope();
    env.sessions = { total: rows.length, sort_key: 'started_desc', rows };
    act(() => { updateSnapshot(env); });
    return render(<SessionsPanel />);
  }

  it('renders the title as a button with a title-based aria-label when session_id present', () => {
    renderWith([sessRow({ session_id: 's1', title: 'Fix the parser' })]);
    const btn = screen.getByRole('button', { name: 'Open session details: Fix the parser' });
    expect(btn).toHaveClass('sess-open-title');
  });

  it('uses the started-time fallback aria-label when the title is empty', () => {
    renderWith([sessRow({ session_id: 's2', title: null })]);
    expect(
      screen.getByRole('button', { name: /^Open session details, started / }),
    ).toBeInTheDocument();
  });

  it('renders a plain title span (no button) when session_id is absent', () => {
    // session_id is declared non-null in the envelope type, but the component
    // defensively guards `r.session_id ?` for the id-less row the server may
    // emit before session_files is ingested — exercise that real branch.
    renderWith([sessRow({ session_id: null as unknown as string, title: 'orphan' })]);
    expect(screen.queryByRole('button', { name: /Open session details/ })).toBeNull();
    expect(screen.getByText('orphan')).toBeInTheDocument();
  });

  // RETARGET of the former 'never puts role=button / tabIndex on the <tr>' test
  // (#299). The <tr> is now the grid's roving unit — role="row" + a roving
  // tabIndex — but the invalid-nested-interactive guard it originally protected
  // (a <tr> with nested buttons must NEVER be role="button") is preserved.
  it('makes the <tr> a role=row grid row with a roving tabIndex (never role=button)', () => {
    const env = baseEnvelope();
    env.sessions = { total: 2, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', project: 'alpha', project_key: 'alpha' }),
      sessRow({ session_id: 'b', project: 'beta', project_key: 'beta' }),
    ] };
    updateSnapshot(env);
    const { container } = render(<SessionsPanel />);
    const rows = Array.from(container.querySelectorAll('tr.session-row')) as HTMLElement[];
    rows.forEach((tr) => {
      expect(tr.getAttribute('role')).toBe('row');
      // The invalid-nested-interactive guard the original test protected:
      expect(tr.getAttribute('role')).not.toBe('button');
      expect(tr.getAttribute('tabindex')).not.toBeNull();
    });
  });

  it('opens the session modal via keyboard on the title button (no double-dispatch)', async () => {
    const user = userEvent.setup();
    renderWith([sessRow({ session_id: 's1', title: 't' })]);
    const btn = screen.getByRole('button', { name: 'Open session details: t' });
    btn.focus();
    await user.keyboard('{Enter}');
    // The store keeps the modal kind + its target in sibling fields
    // (openModal is a bare ModalKind string; the id lives in openSessionId).
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('s1');
  });

  it('activating the project link opens the projects modal, not the session modal', async () => {
    const user = userEvent.setup();
    renderWith([sessRow({ session_id: 's1', title: 't', project: 'proj', project_key: 'proj' })]);
    await user.click(screen.getByRole('button', { name: /Open Projects modal for proj/ }));
    // Genuine no-double-dispatch guard: if the link's stopPropagation failed,
    // the <tr> onClick would ALSO fire OPEN_MODAL{session} and overwrite this.
    expect(getState().openModal).toBe('projects');
  });
});

describe('SessionsPanel Dur fold structure (#293 S2 SESS-1)', () => {
  it('renders both the standalone .dur cell and an inline .dur-fold (not aria-hidden)', () => {
    const env = baseEnvelope();
    env.sessions = { total: 1, sort_key: 'started_desc',
      rows: [sessRow({ session_id: 's1', duration_min: 7 })] };
    act(() => { updateSnapshot(env); });
    const { container } = render(<SessionsPanel />);
    const durCell = container.querySelector('td.dur');
    const durFold = container.querySelector('.dur-fold');
    expect(durCell).toBeInTheDocument();
    expect(durFold).toBeInTheDocument();
    expect(durFold).not.toHaveAttribute('aria-hidden');
    expect(durFold!.textContent).toContain('7m');
  });
});

describe('SessionsPanel roving-tabindex invariant (#299)', () => {
  function threeRows() {
    const env = baseEnvelope();
    env.sessions = { total: 3, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', project: 'alpha', project_key: 'alpha' }),
      sessRow({ session_id: 'b', project: 'beta', project_key: 'beta' }),
      sessRow({ session_id: 'c', project: 'gamma', project_key: 'gamma' }),
    ] };
    updateSnapshot(env);
    return env;
  }

  it('table is role=grid; exactly one row is the tab stop and all nested buttons are -1', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('table.sess-table')?.getAttribute('role')).toBe('grid');
    expect(container.querySelector('#sess-rows')?.getAttribute('role')).toBe('rowgroup');
    const rows = Array.from(container.querySelectorAll('tr.session-row')) as HTMLElement[];
    const zero = rows.filter((r) => r.getAttribute('tabindex') === '0');
    expect(zero.length).toBe(1);
    rows.forEach((r) => expect(['0', '-1']).toContain(r.getAttribute('tabindex')));
    // Every nested control is removed from the Tab order.
    container.querySelectorAll(
      '#sess-rows .sess-open-conv, #sess-rows .chip.model-chip, #sess-rows .sess-open-title, #sess-rows .project-cell-link',
    ).forEach((btn) => expect(btn.getAttribute('tabindex')).toBe('-1'));
    // Body cells are gridcells.
    expect(container.querySelector('#sess-rows td')?.getAttribute('role')).toBe('gridcell');
  });

  it('default tab stop is the first row when no search is active', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    const rows = Array.from(container.querySelectorAll('tr.session-row')) as HTMLElement[];
    expect(rows[0].getAttribute('tabindex')).toBe('0');
  });

  it('empty session list renders no body tab stop', () => {
    updateSnapshot(baseEnvelope()); // sessions.rows = []
    const { container } = render(<SessionsPanel />);
    expect(container.querySelector('tr.session-row')).toBeNull();
  });
});

describe('SessionsPanel roving keydown (#299)', () => {
  function threeRows() {
    const env = baseEnvelope();
    env.sessions = { total: 3, sort_key: 'started_desc', rows: [
      sessRow({ session_id: 'a', project: 'alpha', project_key: 'alpha' }),
      sessRow({ session_id: 'b', project: 'beta', project_key: 'beta' }),
      sessRow({ session_id: 'c', project: 'gamma', project_key: 'gamma' }),
    ] };
    updateSnapshot(env);
  }
  const rowsOf = (c: HTMLElement) =>
    Array.from(c.querySelectorAll('tr.session-row')) as HTMLElement[];

  it('ArrowDown moves the tabIndex=0 stop to the next row and focuses it', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    const rows = rowsOf(container);
    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: 'ArrowDown' });
    expect(rows[1].getAttribute('tabindex')).toBe('0');
    expect(rows[0].getAttribute('tabindex')).toBe('-1');
    expect(document.activeElement).toBe(rows[1]);
  });

  it('ArrowUp clamps at the first row (no wrap)', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    const rows = rowsOf(container);
    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: 'ArrowUp' });
    expect(rows[0].getAttribute('tabindex')).toBe('0');
    expect(document.activeElement).toBe(rows[0]);
  });

  it('ArrowRight from the row focuses the first visible control', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    const rows = rowsOf(container);
    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: 'ArrowRight' });
    // threeRows() is single-model (oneModel drops the chip column) and
    // transcripts-off, so the first control is the .sess-open-title button;
    // assert focus left the row and landed on a control button within it.
    expect(document.activeElement).not.toBe(rows[0]);
    expect(rows[0].contains(document.activeElement)).toBe(true);
    expect((document.activeElement as HTMLElement).tagName).toBe('BUTTON');
  });

  it('Enter on the focused row opens the session modal', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    const rows = rowsOf(container);
    rows[1].focus();
    fireEvent.keyDown(rows[1], { key: 'Enter' });
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('b');
  });

  it('Shift+ArrowDown is NOT consumed (bubbles to panel reorder)', () => {
    threeRows();
    const { container } = render(<SessionsPanel />);
    const rows = rowsOf(container);
    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: 'ArrowDown', shiftKey: true });
    // The roving stop must be unchanged — the handler bailed on the modifier.
    expect(rows[0].getAttribute('tabindex')).toBe('0');
    expect(rows[1].getAttribute('tabindex')).toBe('-1');
  });
});
