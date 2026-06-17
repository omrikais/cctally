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
//   8. SSE-tick regression — a snapshot carrying transcriptsEnabled keeps the
//      switcher; one omitting it hides it (SSE envelopes must carry the gate)
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
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import type { Envelope, SessionRow } from '../types/envelope';

// Mirror of main.tsx's view-aware global panel-digit binding. Registered
// here (rather than importing main.tsx, which boots SSE + createRoot against
// a #root this test does not mount) so scenario 6 exercises the production
// shape: the binding is scope:'global' with NO per-binding view clause, and
// the keymap DISPATCHER makes it inert when view!=='dashboard' (#156).
function registerPanelDigitBindings(): void {
  const guard = (): boolean => {
    const s = getState();
    return !s.update.modalOpen && !s.doctorModalOpen;
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

function baseEnvelope(transcriptsEnabled?: boolean, generatedAt = '2026-05-13T10:00:00Z'): Envelope {
  return {
    envelope_version: 2,
    generated_at: generatedAt,
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

function detail() {
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
      member_uuids: ['a-uuid'],
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

// #177 S5 — outline endpoint fixture. Two turns (a human prompt + an assistant
// reply) and a small stats block; cost matches detail() so parity holds.
function outlinePayload() {
  return {
    session_id: 'sess-1',
    stats: {
      turns: { total: 2, human: 1, assistant: 1, tool_result: 0, meta: 0 },
      tool_counts: { Read: 3, Bash: 1 },
      error_count: 0,
      models: { 'claude-opus-4': 1 },
      duration_seconds: 300,
      tokens: { input: 100, output: 50, cache_creation: 0, cache_read: 0 },
      cost_usd: 1.25,
    },
    turns: [
      {
        uuid: 'h-uuid', kind: 'human', ts: '2026-05-13T09:05:00Z',
        label: 'how does the lock work?', member_uuids: ['h-uuid'],
        subagent_key: null, parent_uuid: null, is_sidechain: false,
      },
      {
        uuid: 'a-uuid', kind: 'assistant', ts: '2026-05-13T09:10:00Z',
        label: 'It uses an flock on cache.db.lock.', member_uuids: ['a-uuid'],
        subagent_key: null, parent_uuid: null, is_sidechain: false,
        model: 'claude-opus-4',
      },
    ],
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
    // The /outline suffix MUST be matched before the catch-all detail route.
    else if (u.includes('/outline')) body = outlinePayload();
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
  installIntersectionObserverStub();
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

  it('9: desktop outline column renders when open + selected, and the conv-view--outline modifier is applied', async () => {
    localStorage.setItem('cctally.conv.outlineOpen', 'true');
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);

    // The third grid column + the modifier class on the shell.
    await waitFor(() => {
      expect(document.querySelector('.conv-outline')).not.toBeNull();
      expect(document.querySelector('.conv-view--outline')).not.toBeNull();
    });
    // Stats card surfaces the session-at-a-glance numbers.
    await waitFor(() => {
      const card = document.querySelector('.conv-outline-stats');
      expect(card).not.toBeNull();
      expect(card!.textContent).toContain('turns');
    });
    expect(screen.getByRole('navigation', { name: 'Session outline' })).not.toBeNull();
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('10: the o key toggles the outline column off and back on', async () => {
    localStorage.setItem('cctally.conv.outlineOpen', 'true');
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);

    await waitFor(() => expect(document.querySelector('.conv-outline')).not.toBeNull());
    // `o` toggles it closed.
    act(() => { fireEvent.keyDown(document, { key: 'o' }); });
    expect(getState().convOutlineOpen).toBe(false);
    await waitFor(() => expect(document.querySelector('.conv-outline')).toBeNull());
    expect(document.querySelector('.conv-view--outline')).toBeNull();
    // `o` again toggles it back open.
    act(() => { fireEvent.keyDown(document, { key: 'o' }); });
    expect(getState().convOutlineOpen).toBe(true);
    await waitFor(() => expect(document.querySelector('.conv-outline')).not.toBeNull());
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('11: mobile renders the outline as a slide-over sheet (not a column) with a dismissing backdrop', async () => {
    // #205 S1 — the sheet now gates on the EPHEMERAL convOutlineMobileOpen flag
    // (NOT the persisted desktop pref); the backdrop dispatches the mobile-close
    // action. Open it via the flag, then verify the panel rides inside the sheet
    // and the backdrop dismisses it.
    stubMobileMedia(true);
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    act(() => { dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' }); });
    render(<App />);

    // The sheet wrapper + backdrop appear; the panel rides inside the sheet.
    await waitFor(() => {
      expect(document.querySelector('.conv-outline-sheet')).not.toBeNull();
      expect(document.querySelector('.conv-outline-sheet .conv-outline')).not.toBeNull();
    });
    const backdrop = document.querySelector<HTMLButtonElement>('.conv-outline-backdrop')!;
    expect(backdrop).not.toBeNull();

    // Clicking the backdrop dispatches CLOSE_CONV_OUTLINE_MOBILE → the sheet
    // unmounts; the persisted desktop pref is untouched.
    fireEvent.click(backdrop);
    expect(getState().convOutlineMobileOpen).toBe(false);
    await waitFor(() => expect(document.querySelector('.conv-outline-sheet')).toBeNull());
  });

  // #205 S1 — the mobile outline sheet defaults closed (never auto-buries the
  // transcript) and opens only on the ephemeral flag, with a titled header + ✕.
  it('16: mobile outline does NOT auto-open on conversation open even with persisted convOutlineOpen=true', () => {
    stubMobileMedia(true);
    localStorage.setItem('cctally.conv.outlineOpen', 'true');
    _resetForTests();
    updateSnapshot(baseEnvelope(true));
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' }); });
    // The persisted desktop pref is true, but the mobile sheet stays closed.
    expect(getState().convOutlineMobileOpen).toBe(false);
    expect(document.querySelector('.conv-outline-sheet')).toBeNull();
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('17: the mobile sheet opens via the store flag with a titled header + ✕, and ✕ closes it', async () => {
    stubMobileMedia(true);
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    act(() => { dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' }); });
    render(<App />);

    await waitFor(() => expect(document.querySelector('.conv-outline-sheet')).not.toBeNull());
    // The titled header label + the ✕ close button (disambiguated by class).
    expect(document.querySelector('.conv-outline-sheet-title')!.textContent).toContain('Outline');
    const closeBtn = document.querySelector<HTMLButtonElement>('.conv-outline-close')!;
    expect(closeBtn).not.toBeNull();
    expect(closeBtn.getAttribute('aria-label')).toBe('Close outline');

    fireEvent.click(closeBtn);
    expect(getState().convOutlineMobileOpen).toBe(false);
    await waitFor(() => expect(document.querySelector('.conv-outline-sheet')).toBeNull());
  });

  // #177 S6 — the '/' rebind matrix (F8). Reader open → opens find; no reader →
  // focuses the rail search input; modal open → neither (guard suppresses it).
  it('12: "/" opens the find bar when a reader is open', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-reader-head')).not.toBeNull());

    expect(getState().convFindOpen).toBe(false);
    act(() => { fireEvent.keyDown(document, { key: '/' }); });
    expect(getState().convFindOpen).toBe(true);
    await waitFor(() => expect(document.querySelector('.conv-findbar')).not.toBeNull());
  });

  it('13: "/" with no conversation open focuses the rail search input (does NOT open find)', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-rail-search-input')).not.toBeNull());

    expect(getState().selectedConversationId).toBeNull();
    act(() => { fireEvent.keyDown(document, { key: '/' }); });
    expect(getState().convFindOpen).toBe(false);
    expect(document.activeElement).toBe(document.querySelector('.conv-rail-search-input'));
  });

  it('14: "/" is inert while a modal is open (guard)', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-reader-head')).not.toBeNull());
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'forecast' }); });

    act(() => { fireEvent.keyDown(document, { key: '/' }); });
    expect(getState().convFindOpen).toBe(false);
  });

  it('15: Esc in the find input closes find WITHOUT exiting the conversations view (no global Esc leak)', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-reader-head')).not.toBeNull());

    act(() => { fireEvent.keyDown(document, { key: '/' }); });
    const input = await waitFor(() => {
      const el = document.querySelector<HTMLInputElement>('.conv-findbar-input');
      expect(el).not.toBeNull();
      return el!;
    });
    // Esc on the FIND input must close find but keep us in the conversations view
    // (the input stops propagation, so the view-level global Esc never fires).
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(getState().convFindOpen).toBe(false);
    expect(getState().view).toBe('conversations');
  });

  it('8: an SSE tick carrying transcriptsEnabled keeps the switcher; a tick omitting it hides it (SSE envelopes must carry the gate)', () => {
    // Bootstrap (the /api/data shape): switcher shown.
    updateSnapshot(baseEnvelope(true, '2026-05-13T10:00:00Z'));
    render(<App />);
    expect(screen.getByRole('tablist', { name: 'Workspace' })).not.toBeNull();

    // A NEWER snapshot that ALSO carries the field (the FIXED SSE envelope —
    // _serve_api_events now injects transcriptsEnabled per connection). The
    // store replaces the whole snapshot on every tick, so the switcher must
    // PERSIST rather than vanishing ~15s after bootstrap.
    act(() => { updateSnapshot(baseEnvelope(true, '2026-05-13T10:00:15Z')); });
    expect(screen.getByRole('tablist', { name: 'Workspace' })).not.toBeNull();

    // Contract pin: a still-newer snapshot that OMITS the field hides the
    // switcher. This is exactly the pre-fix SSE behavior — it documents that
    // SSE envelopes MUST carry transcriptsEnabled or the steady-state UI
    // loses the gate. The day the backend injection is dropped, this branch
    // reproduces the regression (switcher gone after the first SSE tick).
    act(() => { updateSnapshot(baseEnvelope(undefined, '2026-05-13T10:00:30Z')); });
    expect(screen.queryByRole('tablist', { name: 'Workspace' })).toBeNull();
  });
});
