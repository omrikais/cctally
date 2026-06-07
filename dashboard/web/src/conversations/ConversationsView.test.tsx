// Modal-level integration test for the Conversations workspace (plan
// Task 5 Step 6). Renders the full <App /> against the real singleton
// store + keymap registry, with a snapshot carrying transcriptsEnabled
// and `fetch` routed per-endpoint. Mirrors the store/keymap wiring of
// modals/ProjectsModal.test.tsx (real store via _resetForTests +
// updateSnapshot — there is no renderWithStore shim in this codebase).
//
// Covers the 7 plan scenarios:
//   1. switcher shown when transcriptsEnabled → click enters view + rail renders
//   2. transcriptsEnabled:false → no switcher
//   3. Sessions-row entry → reader opens for that session
//   4. search-hit click → reader opens + scrollIntoView + conv-item--jumped
//   5. assistant per-turn cost rendered exactly once
//   6. view-aware keymap — '1' in conversations view does NOT open a panel
//      modal; Esc with empty search exits to dashboard
//   7. mobile (stubMobileMedia(true)) — rail-only until select; Back returns
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { App } from '../App';
import {
  _resetForTests,
  dispatch,
  getState,
  updateSnapshot,
} from '../store/store';
import {
  installGlobalKeydown,
  registerKeymap,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
import { openPanelByPosition } from '../lib/openPanelByPosition';
import { stubMobileMedia } from '../test-utils/mobileMedia';
import type { Envelope, SessionRow } from '../types/envelope';

// jsdom lacks IntersectionObserver — the reader's lazy-load sentinel
// effect constructs one. Install a minimal no-op so the reader mounts.
class IntersectionObserverStub {
  constructor(_cb: IntersectionObserverCallback) {}
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] { return []; }
}

// Mirror of main.tsx's view-aware global panel-digit guard + binding.
// Registered here (rather than importing main.tsx, which boots SSE +
// createRoot against a #root that this test does not mount) so scenario 6
// exercises the EXACT production guard predicate: a digit only opens a
// panel modal while view==='dashboard'.
function registerPanelDigitBindings(): void {
  const guard = (): boolean => {
    const s = getState();
    return s.view === 'dashboard' && !s.update.modalOpen && !s.doctorModalOpen;
  };
  registerKeymap([
    { key: '1', scope: 'global', when: guard, action: () => openPanelByPosition(1) },
  ]);
}

function sessionRow(over: Partial<SessionRow> = {}): SessionRow {
  return {
    session_id: 'sess-1',
    started_utc: '2026-05-13T09:00:00Z',
    duration_min: 12,
    model: 'claude-opus-4',
    project: 'repo-a',
    project_key: null,
    cost_usd: 1.25,
    ...over,
  };
}

function baseEnvelope(transcriptsEnabled?: boolean): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-05-13T10:00:00Z',
    ...(transcriptsEnabled === undefined ? {} : { transcriptsEnabled }),
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
    sessions: { total: 1, sort_key: 'started_desc', rows: [sessionRow()] },
    projects: null,
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [], budget_thresholds: [] },
  } as Envelope;
}

// ---- Endpoint fixtures -------------------------------------------------

const conversationsPage = {
  conversations: [
    {
      session_id: 'sess-1',
      project_label: 'repo-a',
      git_branch: 'main',
      started_utc: '2026-05-13T09:00:00Z',
      last_activity_utc: '2026-05-13T09:30:00Z',
      msg_count: 4,
      cost_usd: 1.25,
      models: ['claude-opus-4'],
    },
  ],
  page: { next_offset: null, has_more: false },
};

const searchResult = {
  query: 'flock',
  mode: 'fts',
  hits: [
    {
      session_id: 'sess-1',
      uuid: 'a-uuid',
      project_label: 'repo-a',
      ts: '2026-05-13T09:10:00Z',
      snippet: 'the [flock] serializes writers',
      cost_usd: 0.05,
    },
  ],
  total: 1,
};

function detail(opts: { withJumpTarget?: boolean } = {}) {
  const items: unknown[] = [
    {
      kind: 'human',
      anchor: { session_id: 'sess-1', uuid: 'h-uuid', id: 1 },
      member_uuids: ['h-uuid'],
      ts: '2026-05-13T09:05:00Z',
      text: 'how does the lock work?',
      blocks: [],
      is_sidechain: false,
    },
    {
      kind: 'assistant',
      anchor: { session_id: 'sess-1', uuid: 'a-uuid', id: 2 },
      member_uuids: opts.withJumpTarget ? ['a-uuid'] : ['a-uuid'],
      ts: '2026-05-13T09:10:00Z',
      text: 'It uses an flock on cache.db.lock.',
      blocks: [],
      model: 'claude-opus-4',
      is_sidechain: false,
      cost_usd: 0.0123,
    },
  ];
  return {
    session_id: 'sess-1',
    project_label: 'repo-a',
    git_branch: 'main',
    started_utc: '2026-05-13T09:00:00Z',
    last_activity_utc: '2026-05-13T09:30:00Z',
    cost_usd: 1.25,
    models: ['claude-opus-4'],
    items,
    page: { next_after: null, has_more: false },
  };
}

// Route fetch by URL. Each route resolves a fresh Response so repeated
// first-page loads (e.g. an SSE revalidate) don't exhaust a queue.
function installRoutedFetch(): void {
  const fn = vi.fn(async (url: string | URL) => {
    const u = String(url);
    let body: unknown;
    if (u.includes('/api/conversation/search')) body = searchResult;
    else if (u.includes('/api/conversations')) body = conversationsPage;
    else if (u.includes('/api/conversation/')) body = detail();
    else body = {};
    return { ok: true, status: 200, json: async () => body } as Response;
  });
  globalThis.fetch = fn as unknown as typeof fetch;
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymap();
  installGlobalKeydown();
  registerPanelDigitBindings();
  installRoutedFetch();
  (globalThis as unknown as { IntersectionObserver: typeof IntersectionObserverStub }).IntersectionObserver =
    IntersectionObserverStub;
});

afterEach(() => {
  _resetForTests();
  _resetKeymap();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('Conversations workspace integration', () => {
  it('1: switcher is shown; clicking Conversations enters the view and renders the rail', async () => {
    updateSnapshot(baseEnvelope(true));
    render(<App />);

    const switcher = screen.getByRole('tablist', { name: 'Workspace' });
    expect(switcher).not.toBeNull();
    const convTab = within(switcher).getByRole('tab', { name: 'Conversations' });

    fireEvent.click(convTab);
    expect(getState().view).toBe('conversations');

    // Rail mounts with its search input + the browsed conversation row.
    await waitFor(() => {
      expect(document.querySelector('.conv-rail')).not.toBeNull();
      expect(document.querySelector('.conv-rail-search-input')).not.toBeNull();
    });
    await waitFor(() => {
      expect(screen.getByText('repo-a')).not.toBeNull();
    });
  });

  it('2: switcher is absent when transcriptsEnabled is false', () => {
    updateSnapshot(baseEnvelope(false));
    render(<App />);
    expect(screen.queryByRole('tablist', { name: 'Workspace' })).toBeNull();
  });

  it('3: a Sessions-row entry button opens the reader for that session', async () => {
    updateSnapshot(baseEnvelope(true));
    render(<App />);

    // The per-row affordance lives in the always-mounted dashboard body.
    const openBtn = await screen.findByRole('button', { name: 'Open conversation' });
    fireEvent.click(openBtn);

    expect(getState().view).toBe('conversations');
    expect(getState().selectedConversationId).toBe('sess-1');

    // Reader opens (header carries the whole-session cost + branch).
    await waitFor(() => {
      const head = document.querySelector('.conv-reader-head');
      expect(head).not.toBeNull();
      expect(head!.textContent).toContain('repo-a');
    });
  });

  it('4: clicking a search hit opens the reader, scrolls to the message, and flashes it', async () => {
    const scrollSpy = vi
      .spyOn(Element.prototype, 'scrollIntoView')
      .mockImplementation(() => {});

    updateSnapshot(baseEnvelope(true));
    render(<App />);

    // Enter the view via the switcher, then type a needle to search.
    fireEvent.click(screen.getByRole('tab', { name: 'Conversations' }));
    const input = (await waitFor(() => {
      const el = document.querySelector<HTMLInputElement>('.conv-rail-search-input');
      expect(el).not.toBeNull();
      return el!;
    }));
    fireEvent.change(input, { target: { value: 'flock' } });

    // Debounced search → the hit row (with its highlighted snippet).
    const hitRow = await screen.findByRole('button', { name: /flock/ }, { timeout: 1500 });
    expect(hitRow.querySelector('mark')).not.toBeNull();

    fireEvent.click(hitRow);
    expect(getState().selectedConversationId).toBe('sess-1');
    expect(getState().conversationJump).toEqual({ session_id: 'sess-1', uuid: 'a-uuid' });

    // The reader pages to the target, scrolls it into view, and flashes it.
    await waitFor(() => expect(scrollSpy).toHaveBeenCalled());
    await waitFor(() => {
      const target = document.querySelector('[data-uuid="a-uuid"]');
      expect(target).not.toBeNull();
      expect(target!.classList.contains('conv-item--jumped')).toBe(true);
    });
  });

  it('5: the assistant per-turn cost is rendered exactly once', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);

    await waitFor(() => {
      expect(document.querySelector('.conv-item--assistant')).not.toBeNull();
    });
    // 0.0123 → "$0.0123" (toFixed(4) per the per-turn cost contract).
    const costs = document.querySelectorAll('.conv-item-cost');
    expect(costs).toHaveLength(1);
    expect(costs[0].textContent).toBe('$0.0123');
  });

  it('6: in conversations view, "1" does not open a panel modal; Esc with empty search exits to dashboard', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-rail')).not.toBeNull());

    expect(getState().openModal).toBeNull();
    fireEvent.keyDown(document, { key: '1' });
    // The view-aware guard suppresses the panel-digit binding.
    expect(getState().openModal).toBeNull();
    expect(getState().view).toBe('conversations');

    // Esc with an empty needle exits the view back to the dashboard.
    expect(getState().conversationSearch).toBe('');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().view).toBe('dashboard');

    // Sanity: the same digit DOES open a panel modal in dashboard view,
    // proving the binding fires when the guard allows (non-vacuous).
    act(() => { fireEvent.keyDown(document, { key: '1' }); });
    expect(getState().openModal).not.toBeNull();
  });

  it('7: mobile shows rail-only until a conversation is selected; Back returns to the rail', async () => {
    stubMobileMedia(true);
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);

    // Rail-only: no reader yet.
    await waitFor(() => expect(document.querySelector('.conv-rail')).not.toBeNull());
    expect(document.querySelector('.conv-reader')).toBeNull();

    // Select a conversation → reader replaces the rail (single column).
    act(() => { dispatch({ type: 'SELECT_CONVERSATION', sessionId: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    expect(document.querySelector('.conv-rail')).toBeNull();

    // The Back control returns to the rail (clears the selection).
    const back = await screen.findByRole('button', { name: /Back/ });
    fireEvent.click(back);
    expect(getState().selectedConversationId).toBeNull();
    await waitFor(() => expect(document.querySelector('.conv-rail')).not.toBeNull());
  });
});
