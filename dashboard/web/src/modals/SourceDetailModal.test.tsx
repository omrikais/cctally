import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { SourceDetailModal } from './SourceDetailModal';
import { Modal } from './Modal';
import { SessionsPanel } from '../panels/SessionsPanel';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import fixture from '../../__tests__/fixtures/envelope.json';
import type { Envelope } from '../types/envelope';
import {
  installGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
});
afterEach(() => {
  vi.unstubAllGlobals();
  _resetKeymap();
});

describe('Codex source rows open the qualified detail modal (§5.6)', () => {
  it('clicking a Codex session row dispatches OPEN_SOURCE_DETAIL under the codex source', () => {
    updateSnapshot(fixture as unknown as Envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    render(<SessionsPanel />);
    const table = screen.getByTestId('codex-sessions-table');
    // #294 S5 §6.3 — the source Sessions grid's detail-open control carries a
    // descriptive aria-label ("Open <source> session details: <title>").
    fireEvent.click(
      within(table).getByRole('button', { name: 'Open codex session details: Session 1' }),
    );
    expect(getState().openSourceDetail).toEqual({
      source: 'codex',
      resource: 'session',
      key: 'session:codex-a',
    });
  });
});

describe('SourceDetailModal — qualified fetch + native vocabulary (§5.6)', () => {
  it('fetches the qualified route, renders native token vocabulary, and calls NO legacy /api/session/ route', async () => {
    const fetchFn = vi.fn((_url: string) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            source: 'codex',
            resource: 'session',
            data: {
              detail_kind: 'codex_session',
              key: 'session:codex-a',
              label: 'Fix modal parity',
              project: 'cctally-dev',
              started_at: '2026-04-24T12:00:00Z',
              last_activity: '2026-04-24T12:30:00Z',
              duration_min: 30,
              cost_usd: 6.4,
              input_tokens: 240000,
              cached_input_tokens: 60000,
              output_tokens: 32000,
              reasoning_output_tokens: 4000,
              total_tokens: 276000,
              models: ['gpt-5'],
              model_breakdowns: [{ modelName: 'gpt-5', cost: 6.4, totalTokens: 276000 }],
            },
          }),
      } as unknown as Response),
    );
    vi.stubGlobal('fetch', fetchFn);

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'session', key: 'session:codex-a' });
    render(<SourceDetailModal />);

    await waitFor(() => expect(screen.getByTestId('codex-session-detail')).toBeInTheDocument());
    // Qualified route only.
    expect(fetchFn).toHaveBeenCalledWith('/api/source/codex/session/session%3Acodex-a');
    const legacyCalls = fetchFn.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.startsWith('/api/session/') && !u.startsWith('/api/source/'));
    expect(legacyCalls).toEqual([]);
    // Native token vocabulary.
    const detail = screen.getByTestId('codex-session-detail');
    expect(detail).toHaveTextContent('Reasoning');
    expect(detail).toHaveTextContent('Cached input');
    expect(detail).toHaveTextContent('Fix modal parity');
    expect(detail).toHaveTextContent('cctally-dev');
    expect(detail).toHaveTextContent('30 min');
    expect(detail.querySelector('.m-chipstrip')).not.toBeNull();
    expect(detail.querySelector('.m-hero.cols-3')).not.toBeNull();
    expect(detail.querySelector('.msess-ts')).not.toBeNull();
    expect(detail.querySelector('.msess-tok-grid')).not.toBeNull();
    expect(detail.querySelector('.msess-model-caption')).not.toBeNull();
    expect(screen.getByRole('heading', { name: 'Session detail' })).toBeInTheDocument();
    // No conversation-reader affordance — only the canonical Share and close
    // controls are present.
    const buttons = within(screen.getByRole('dialog')).getAllByRole('button');
    expect(buttons).toHaveLength(2);
    expect(buttons[1]).toHaveAttribute('aria-label', 'Close');
  });

  it('renders the friendly not-found variant for a 404 envelope', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ code: 'source_resource_not_found' }) } as unknown as Response),
      ),
    );
    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'block', key: 'block:gone' });
    render(<SourceDetailModal />);
    await waitFor(() => expect(screen.getByTestId('source-detail-error')).toBeInTheDocument());
    expect(screen.getByTestId('source-detail-error')).toHaveTextContent('no longer available');
  });

  it('renders a qualified Claude session with canonical anatomy and safe project cross-navigation', async () => {
    const nativeSessionId = 'raw-session-id-must-stay-private';
    const nativeSourcePath = '/private/projects/project-red/raw-session.jsonl';
    const fetchFn = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          source: 'claude',
          resource: 'session',
          data: {
            detail_kind: 'claude_session',
            key: 'session:opaque',
            label: 'Claude session',
            project_label: 'project-red',
            project_key: 'project:opaque',
            started_utc: '2026-07-14T12:00:00Z',
            last_activity_utc: '2026-07-14T12:30:00Z',
            duration_min: 30,
            models: [{ name: 'claude-opus-4-8', role: 'primary' }],
            input_tokens: 1000,
            cache_creation_tokens: 2000,
            cache_read_tokens: 7000,
            output_tokens: 500,
            cache_hit_pct: 70,
            cost_per_model: [{ model: 'claude-opus-4-8', cost_usd: 4.25 }],
            cost_total_usd: 4.25,
            privacy_note: 'Native session identity and source files are withheld to preserve opaque identity.',
          },
        }),
      } as unknown as Response),
    );
    vi.stubGlobal('fetch', fetchFn);

    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'claude', resource: 'session', key: 'session:opaque' });
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('claude-session-detail');
    expect(detail.querySelector('.m-chipstrip')).not.toBeNull();
    expect(detail.querySelector('.m-hero.cols-3')).not.toBeNull();
    expect(detail.querySelector('.msess-ts')).not.toBeNull();
    expect(detail.querySelector('.msess-tok-grid')).not.toBeNull();
    expect(detail.querySelector('.msess-model-caption')).not.toBeNull();
    expect(detail).toHaveTextContent('Claude session');
    expect(detail).toHaveTextContent('project-red');
    expect(detail).toHaveTextContent('Cache hit %');
    expect(detail).toHaveTextContent('70.0%');
    expect(detail).toHaveTextContent('withheld to preserve opaque identity');
    expect(detail).not.toHaveTextContent(nativeSessionId);
    expect(detail).not.toHaveTextContent(nativeSourcePath);

    fireEvent.click(screen.getByRole('button', { name: 'Open Claude project details: project-red' }));
    expect(getState().openSourceDetail).toEqual({
      source: 'claude',
      resource: 'project',
      key: 'project:opaque',
    });
    expect(getState().openSourceDetailSelection).toBe('all');
  });

  it('renders a qualified Claude project at the saved 4w window with capped opaque session actions', async () => {
    const fetchFn = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          source: 'claude',
          resource: 'project',
          data: {
            detail_kind: 'claude_project',
            key: 'project:opaque',
            label: 'project-red',
            window_weeks: 4,
            window_cost_usd: 8.5,
            window_attributed_pct: 23.5,
            models: [{ model: 'claude-opus-4-8', cost_usd: 8.5, sessions_count: 3, tokens_input: 1000, tokens_output: 500 }],
            sessions: [{ key: 'session:opaque-a', started_at: '2026-07-14T10:00:00Z', last_activity_at: '2026-07-14T10:30:00Z', primary_model: 'claude-opus-4-8', cost_usd: 4.5 }],
            models_total: 1,
            sessions_total: 3,
          },
        }),
      } as unknown as Response),
    );
    vi.stubGlobal('fetch', fetchFn);

    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    act(() => dispatch({
      type: 'OPEN_SOURCE_DETAIL',
      source: 'claude',
      resource: 'project',
      key: 'project:opaque',
    }));
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('claude-project-detail');
    expect(fetchFn).toHaveBeenCalledWith(
      '/api/source/claude/project/project%3Aopaque?weeks=4',
    );
    expect(detail).toHaveTextContent('project-red · 3 sessions · $8.50 (4w)');
    expect(detail).toHaveTextContent('Models (this project)');
    expect(detail).toHaveTextContent('Recent sessions');
    expect(detail).toHaveTextContent('+2 more');

    const sessionButton = screen.getByTestId('qualified-project-session-0');
    sessionButton.focus();
    fireEvent.click(sessionButton);
    expect(getState().openSourceDetail).toEqual({
      source: 'claude',
      resource: 'session',
      key: 'session:opaque-a',
    });
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Session detail' }));

    act(() => dispatch({
      type: 'OPEN_SOURCE_DETAIL',
      source: 'claude',
      resource: 'project',
      key: 'project:opaque',
    }));
    fireEvent.click(screen.getByTestId('qualified-project-show-in-sessions'));
    expect(getState().filterText).toBe('project-red');
    expect(getState().openSourceDetail).toBeNull();
  });

  it('uses the shared labelled modal lifecycle for focus, Escape, and return focus', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
    const trigger = document.createElement('button');
    trigger.id = 'source-detail-trigger';
    document.body.appendChild(trigger);
    trigger.focus();

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'session', key: 'session:codex-a' });
    render(<SourceDetailModal />);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-labelledby', 'source-detail-title');
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Session detail' }));

    act(() => dispatch({
      type: 'OPEN_SOURCE_DETAIL',
      source: 'claude',
      resource: 'project',
      key: 'project:claude-a',
    }));
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Project detail' }));

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(getState().openSourceDetail).toBeNull());
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it('closes only the topmost qualified detail before its underlying panel modal', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
    dispatch({ type: 'OPEN_MODAL', kind: 'projects' });
    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'project', key: 'project:codex-a' });
    render(
      <>
        <Modal title="Projects" accentClass="accent-orange"><p>Projects body</p></Modal>
        <SourceDetailModal />
      </>,
    );

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().openSourceDetail).toBeNull();
    expect(getState().openModal).toBe('projects');

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().openModal).toBeNull();
  });

  it('clamps a long Codex prompt while exposing the full label through an accessible disclosure', async () => {
    const prompt = 'Investigate every retained Codex session fact without exposing native identity. '.repeat(12);
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        source: 'codex',
        resource: 'session',
        data: {
          detail_kind: 'codex_session',
          key: 'session:long-prompt',
          label: prompt,
          project: 'cctally-dev',
          started_at: '2026-07-21T08:00:00.123456Z',
          last_activity: '2026-07-21T10:37:42.987654Z',
          duration_min: 158,
          cost_usd: 18.25,
          input_tokens: 450000,
          cached_input_tokens: 337500,
          output_tokens: 64000,
          reasoning_output_tokens: 12000,
          total_tokens: 514000,
          models: ['gpt-5-codex'],
          model_breakdowns: [{ modelName: 'gpt-5-codex', cost: 18.25, totalTokens: 514000 }],
        },
      }),
    } as Response)));

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'session', key: 'session:long-prompt' });
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('codex-session-detail');
    const clamp = detail.querySelector('.sd-prompt-clamp');
    expect(clamp).toHaveTextContent(prompt.trim());
    expect(screen.getByText('Show full prompt')).toBeInTheDocument();
    expect(detail.querySelector('.m-hero.cols-3')).not.toBeNull();

    fireEvent.click(screen.getByText('Show full prompt'));
    expect(detail.querySelector('.sd-prompt-full')).toHaveTextContent(prompt.trim());
  });

  it('localizes and bounds large Codex project model/session collections', async () => {
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      display: {
        tz: 'America/Los_Angeles', resolved_tz: 'America/Los_Angeles',
        offset_label: 'PDT', offset_seconds: -25200,
      },
    });
    const models = Array.from({ length: 14 }, (_, index) => ({
      model: `gpt-5-codex-variant-${index + 1}`,
      cost_usd: 12 - index / 2,
      input_tokens: 1000,
      cached_input_tokens: 500,
      output_tokens: 100,
      reasoning_output_tokens: 20,
      total_tokens: 1100,
    }));
    const sessions = Array.from({ length: 18 }, (_, index) => ({
      label: `Session ${index + 1}`,
      last_activity: `2026-07-${String(21 - Math.min(index, 20)).padStart(2, '0')}T10:37:42.987654Z`,
      cost_usd: 10 - index / 4,
      input_tokens: 1000,
      cached_input_tokens: 500,
      output_tokens: 100,
      reasoning_output_tokens: 20,
      total_tokens: 1100,
    }));
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        source: 'codex', resource: 'project', data: {
          detail_kind: 'codex_project', key: 'project:large', label: 'large-project',
          range_start: '2026-06-01T00:00:00.123456Z', range_end: '2026-07-21T11:00:00.654321Z',
          first_seen: '2026-06-01T00:00:00.123456Z', last_seen: '2026-07-21T10:37:42.987654Z',
          session_count: 18, cost_usd: 123.45, input_tokens: 3200000,
          cached_input_tokens: 2100000, output_tokens: 440000,
          reasoning_output_tokens: 88000, total_tokens: 3640000, models, sessions,
        },
      }),
    } as Response)));

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'project', key: 'project:large' });
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('codex-project-detail');
    expect(detail).toHaveTextContent('Native token totals');
    expect(detail).toHaveTextContent('Cached input');
    expect(detail).toHaveTextContent('Reasoning');
    expect(detail).toHaveTextContent('Jul 21 03:37 PDT');
    expect(detail.textContent).not.toMatch(/T\d{2}:\d{2}:\d{2}\.\d{6}Z/);
    expect(within(detail).getAllByTestId('codex-project-model-row')).toHaveLength(6);
    expect(within(detail).getAllByTestId('codex-project-session-row')).toHaveLength(6);
    expect(within(detail).getByRole('button', { name: 'Show all 14 models' })).toHaveTextContent('+8 more');
    expect(within(detail).getByRole('button', { name: 'Show all 18 sessions' })).toHaveTextContent('+12 more');

    fireEvent.click(within(detail).getByRole('button', { name: 'Show all 14 models' }));
    expect(within(detail).getAllByTestId('codex-project-model-row')).toHaveLength(14);
    expect(detail.querySelector('.sd-bounded-collection')).not.toBeNull();
  });

  it('renders a localized, bounded Codex quota progression from retained observations and milestones', async () => {
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      display: {
        tz: 'America/Los_Angeles', resolved_tz: 'America/Los_Angeles',
        offset_label: 'PDT', offset_seconds: -25200,
      },
    });
    const modelBreakdowns = Array.from({ length: 14 }, (_, index) => ({
      modelName: `gpt-5-codex-variant-${index + 1}`,
      cost: 1 + index / 10,
    }));
    const observations = Array.from({ length: 36 }, (_, index) => ({
      captured_at: new Date(Date.parse('2026-07-21T08:00:00Z') + index * 8 * 60000).toISOString(),
      used_percent: 20 + index * 1.3,
      resets_at: '2026-07-21T13:00:00.654321Z',
    }));
    const milestones = Array.from({ length: 24 }, (_, index) => ({
      percent: 20 + index * 2,
      captured_at: new Date(Date.parse('2026-07-21T08:00:00Z') + index * 11 * 60000).toISOString(),
    }));
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        source: 'codex', resource: 'block', data: {
          detail_kind: 'codex_block', key: 'block:large', label: 'Codex 5-hour limit',
          observed_slot: 0, window_minutes: 300,
          start_at: '2026-07-21T08:00:00.123456Z', end_at: '2026-07-21T13:00:00.654321Z',
          resets_at: '2026-07-21T13:00:00.654321Z', current_percent: 67,
          orphaned: false, is_active: true, cost_usd: 18.25, freshness: 'fresh',
          model_breakdowns: modelBreakdowns, observations, milestones,
          forecast: { status: 'ok', current_percent: 67, projected_percent: 89.4, resets_at: '2026-07-21T13:00:00.654321Z' },
        },
      }),
    } as Response)));

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'block', key: 'block:large' });
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('codex-block-detail');
    expect(detail).toHaveTextContent('Quota progression');
    expect(detail).toHaveTextContent('67%');
    expect(detail).toHaveTextContent('$18.25');
    expect(detail).toHaveTextContent('89.4%');
    expect(detail).toHaveTextContent('Jul 21 06:00 PDT');
    expect(detail.textContent).not.toMatch(/T\d{2}:\d{2}:\d{2}\.\d{6}Z/);
    expect(within(detail).getAllByTestId('codex-block-observation-row')).toHaveLength(8);
    expect(within(detail).getAllByTestId('codex-block-milestone-row')).toHaveLength(8);
    expect(within(detail).getByRole('button', { name: 'Show all 36 observations' })).toHaveTextContent('+28 more');
    expect(within(detail).getByRole('button', { name: 'Show all 24 milestones' })).toHaveTextContent('+16 more');
    expect(within(detail).getAllByTestId('codex-block-model-row')).toHaveLength(6);
    expect(within(detail).getByRole('button', { name: 'Show all 14 models' })).toHaveTextContent('+8 more');
    expect(detail).not.toHaveTextContent('Burn rate');
    expect(detail).not.toHaveTextContent('Token total');
  });

  it('states truthful empty and partial Codex project metadata instead of painting blank sections', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        source: 'codex', resource: 'project', data: {
          detail_kind: 'codex_project', key: 'project:partial', label: null,
          range_start: '2026-07-01T00:00:00Z', range_end: '2026-07-21T00:00:00Z',
          first_seen: '2026-07-01T00:00:00Z', last_seen: '2026-07-21T00:00:00Z',
          session_count: 0, cost_usd: 0, input_tokens: 0, cached_input_tokens: 0,
          output_tokens: 0, reasoning_output_tokens: 0, total_tokens: 0,
          models: [], sessions: [], metadata_availability: 'partial',
          metadata_reason: 'Project metadata is unavailable for this item.',
        },
      }),
    } as Response)));

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'project', key: 'project:partial' });
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('codex-project-detail');
    expect(detail).toHaveTextContent('Project metadata is unavailable for this item.');
    expect(detail).toHaveTextContent('No model breakdown is available.');
    expect(detail).toHaveTextContent('No retained sessions are available.');
  });

  it('keeps a sparse cache-only Codex block usable when progression arrays are unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        source: 'codex', resource: 'block', data: {
          detail_kind: 'codex_block', key: 'block:sparse', label: 'Codex quota',
          observed_slot: 0, window_minutes: 300,
          start_at: '2026-07-21T08:00:00Z', end_at: '2026-07-21T13:00:00Z',
          resets_at: '2026-07-21T13:00:00Z', current_percent: 42,
          orphaned: false, is_active: true, cost_usd: 1.25,
          model_breakdowns: [],
          forecast: { status: 'stale', current_percent: 42, projected_percent: null, resets_at: '2026-07-21T13:00:00Z' },
        },
      }),
    } as Response)));

    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: 'codex', resource: 'block', key: 'block:sparse' });
    render(<SourceDetailModal />);

    const detail = await screen.findByTestId('codex-block-detail');
    expect(detail).toHaveTextContent('unavailable');
    expect(detail).toHaveTextContent('No model breakdown is available.');
    expect(detail).toHaveTextContent('No retained quota observations are available.');
    expect(detail).toHaveTextContent('No quota milestones were crossed in this window.');
  });
});
