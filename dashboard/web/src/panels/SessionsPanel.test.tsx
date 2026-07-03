import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { SessionsPanel } from './SessionsPanel';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
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
