import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
              last_activity: '2026-04-24T12:30:00Z',
              cost_usd: 6.4,
              input_tokens: 240000,
              cached_input_tokens: 60000,
              output_tokens: 32000,
              reasoning_output_tokens: 4000,
              total_tokens: 276000,
              models: ['gpt-5'],
              model_breakdowns: [],
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
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Codex session' }));

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
});
