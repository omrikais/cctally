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
import { CONVERSATIONS_BINDINGS } from './ConversationsView';
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
import { stubMobileMedia, stubResponsiveMedia } from '../test-utils/mobileMedia';
import { installIntersectionObserverStub } from '../test-utils/intersectionObserver';
import { MOBILE_MEDIA_QUERY, WIDE_MEDIA_QUERY, COMPACT_WORKSPACE_MEDIA_QUERY } from '../lib/breakpoints';
import type { Envelope, SessionRow } from '../types/envelope';

// #232 — render-all react-virtuoso mock (mirrors ConversationReader.test.tsx).
// The integration tests render the real reader, but real Virtuoso renders
// nothing in JSDOM (zero layout). This passthrough mounts every item so the
// reader's rows, jump flash, and cost footer assertions stay valid. The handle
// surfaces the imperative scrollToIndex spy + startReached load trigger.
const virtuosoTestHandle: {
  scrollToIndex: ReturnType<typeof vi.fn>;
  startReached: (() => void) | null;
} = { scrollToIndex: vi.fn(), startReached: null };
vi.mock('react-virtuoso', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  const Virtuoso = React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
    const scrollToIndex = virtuosoTestHandle.scrollToIndex;
    // #233 — the reader's jump landing routes through the convergent `scrollIntoView`
    // and runs its within-row second pass (the native el.scrollIntoView this test's
    // search-hit flow asserts) inside the `done` callback, so the mock must invoke it.
    React.useImperativeHandle(ref, () => ({ scrollToIndex, scrollIntoView: vi.fn((loc?: { done?: () => void }) => { loc?.done?.(); }), scrollBy: vi.fn(), scrollTo: vi.fn() }), [scrollToIndex]);
    const data = (props.data as unknown[]) ?? [];
    const itemContent = props.itemContent as (index: number, datum: unknown) => React.ReactNode;
    const computeItemKey = props.computeItemKey as ((index: number, datum: unknown) => React.Key) | undefined;
    const components = (props.components as { List?: unknown; Item?: unknown }) ?? {};
    const firstItemIndex = (props.firstItemIndex as number) ?? 0;
    const scrollerRef = props.scrollerRef as ((el: unknown) => void) | undefined;
    const List = (components.List ?? 'div') as React.ElementType;
    const Item = (components.Item ?? 'div') as React.ElementType;
    const scroller = React.useRef<HTMLDivElement>(null);
    virtuosoTestHandle.startReached = (props.startReached as (() => void)) ?? null;
    React.useEffect(() => {
      scrollerRef?.(scroller.current);
      (props.itemsRendered as ((items: unknown[]) => void) | undefined)?.(data.map((d, i) => ({ index: firstItemIndex + i, data: d })));
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [data.length]);
    return React.createElement(
      'div',
      { ref: scroller, className: props.className as string, role: props.role as string, onScroll: props.onScroll as React.UIEventHandler },
      React.createElement(List, {}, data.map((d, i) =>
        React.createElement(Item as React.ElementType,
          { key: computeItemKey ? computeItemKey(firstItemIndex + i, d) : i, 'data-index': firstItemIndex + i },
          itemContent(firstItemIndex + i, d)))),
    );
  });
  return { Virtuoso, VirtuosoMockContext: React.createContext(undefined) };
});

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
    // #304 S2 — a second row so a compact pick can click a NON-anchor row. Shares
    // repo-a so the single-project label suppression is unchanged.
    {
      session_id: 'sess-2',
      project_label: 'repo-a',
      git_branch: 'main',
      started_utc: '2026-05-13T08:00:00Z',
      last_activity_utc: '2026-05-13T08:30:00Z',
      msg_count: 2,
      cost_usd: 0.5,
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
  virtuosoTestHandle.scrollToIndex = vi.fn();
  virtuosoTestHandle.startReached = null;
});

afterEach(() => {
  _resetForTests();
  _resetKeymap();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// four-band resolver (#304 S1): phone ≤640 | compact 641–880 (single-pane) |
// upperTablet 881–1100 (two-pane + sheet) | wide ≥1101 (column). Per-query so the
// 640 / 880 / 1100 breakpoints stay independent (stubMobileMedia returns one
// value for every query and can't model them). Shared by the #228 outline-sheet
// band tests and the #304 compact-workspace single-pane tests. The compact query
// matches at phone too (880 ≥ 640), mirroring the real matchMedia.
function bandResolver(band: 'phone' | 'compact' | 'upperTablet' | 'wide') {
  return (q: string): boolean => {
    if (q === MOBILE_MEDIA_QUERY) return band === 'phone';
    if (q === COMPACT_WORKSPACE_MEDIA_QUERY) return band === 'phone' || band === 'compact';
    if (q === WIDE_MEDIA_QUERY) return band === 'wide';
    return false;
  };
}

describe('Conversations workspace integration', () => {
  it('1: switcher is shown; clicking Conversations enters the view and renders the rail', async () => {
    updateSnapshot(baseEnvelope(true));
    render(<App />);

    const switcher = screen.getByRole('group', { name: 'Workspace' });
    expect(switcher).not.toBeNull();
    const dashBtn = within(switcher).getByRole('button', { name: 'Dashboard' });
    expect(dashBtn).toHaveAttribute('aria-pressed', 'true');
    const convTab = within(switcher).getByRole('button', { name: 'Conversations' });
    expect(convTab).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(convTab);
    expect(getState().view).toBe('conversations');
    expect(convTab).toHaveAttribute('aria-pressed', 'true');

    // Rail mounts with its search input + the browsed conversation row. #228 S4
    // D2 — the single-project label is suppressed when every loaded row shares
    // one project (the fixture has one), so assert the row mounted via its model
    // chip (claude-opus-4 → an `opus` chip) rather than the now-hidden project
    // label.
    await waitFor(() => {
      expect(document.querySelector('.conv-rail')).not.toBeNull();
      expect(document.querySelector('.conv-rail-search-input')).not.toBeNull();
    });
    await waitFor(() => {
      expect(document.querySelector('.conv-rail-row')).not.toBeNull();
      expect(document.querySelector('.conv-rail-row-model .chip.opus')).not.toBeNull();
    });
  });

  it('2: switcher is absent when transcriptsEnabled is false', () => {
    updateSnapshot(baseEnvelope(false));
    render(<App />);
    expect(screen.queryByRole('group', { name: 'Workspace' })).toBeNull();
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
    // #234 — the reader's jump landing now writes the scroller's scrollTop directly
    // (scrollNodeIntoView), not a native scrollIntoView. jsdom lacks
    // Element.prototype.scrollTo, so define a no-op first, then spy on it.
    if (typeof Element.prototype.scrollTo !== 'function') {
      Element.prototype.scrollTo = () => {};
    }
    const scrollToSpy = vi.spyOn(Element.prototype, 'scrollTo').mockImplementation(() => {});

    updateSnapshot(baseEnvelope(true));
    render(<App />);

    // Enter the view via the switcher, then type a needle to search.
    fireEvent.click(screen.getByRole('button', { name: 'Conversations' }));
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
    expect(getState().conversationJump).toEqual({
      conversation_ref: { source: 'claude', key: 'sess-1' },
      session_id: 'sess-1',
      uuid: 'a-uuid',
    });

    // The reader pages to the target, direct-scrolls it into view, and flashes it.
    await waitFor(() => expect(scrollToSpy).toHaveBeenCalled());
    await waitFor(() => {
      const target = document.querySelector('[data-uuid="a-uuid"]');
      expect(target).not.toBeNull();
      expect(target!.classList.contains('conv-item--jumped')).toBe(true);
    });
  });

  it('5: the assistant per-turn cost footer is rendered exactly once', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);

    await waitFor(() => {
      expect(document.querySelector('.conv-item--assistant')).not.toBeNull();
    });
    // #228 S3 B2 — exactly one cost footer. The fixture cost (0.0123) is BELOW
    // the $0.05 verbose-text floor, so the footer hides the `$…` text line and
    // carries the exact figure in the `title` (toFixed(4) per the per-turn cost
    // contract) — proving the gating reaches the rendered reader, not just the
    // MessageItem unit.
    const costs = document.querySelectorAll('.conv-item-cost');
    expect(costs).toHaveLength(1);
    expect(costs[0].textContent).not.toContain('$0.0123');
    expect(costs[0].getAttribute('title')).toBe('$0.0123');
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

  // #228 S4 D1 (production-path) — Esc in the FOCUSED rail search must clear the
  // needle and blur but NOT eject the workspace. Before the fix the global Esc
  // binding gated only on `!openModal`, so with the rail input focused the Esc
  // bubbled to the document listener AND fired the global binding; by the time
  // the global action ran the needle was already cleared, so it fell through to
  // SET_VIEW 'dashboard' and the whole workspace ejected. The fix swaps the
  // binding's guard to the shared `inView` (so it's inert while inputMode !==
  // null) and adds e.stopPropagation() to the input's own Esc handler.
  it('6b: Esc in the focused rail search clears the needle and blurs but stays in the workspace; a second Esc with nothing focused exits', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);

    const input = await waitFor(() => {
      const el = document.querySelector<HTMLInputElement>('.conv-rail-search-input');
      expect(el).not.toBeNull();
      return el!;
    });

    // Mirror the input's own focus/change handlers: focus the input (inputMode
    // 'search'), type a needle.
    act(() => {
      input.focus();
      dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'abc' });
      dispatch({ type: 'SET_INPUT_MODE', mode: 'search' });
    });
    expect(getState().conversationSearch).toBe('abc');
    expect(document.activeElement).toBe(input);

    // Esc on the focused input: the input's own onKeyDown clears the needle and
    // blurs; the global Esc is suppressed (inputMode 'search'), so we stay put.
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(getState().conversationSearch).toBe('');
    expect(getState().view).toBe('conversations');
    expect(document.activeElement).not.toBe(input);

    // The blur clears inputMode; now Esc with an empty needle exits to the
    // dashboard — the intended two-step is unchanged.
    act(() => { dispatch({ type: 'SET_INPUT_MODE', mode: null }); });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().view).toBe('dashboard');
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

  it('18: the mobile outline backdrop has a distinct aria-label from the ✕ close (no duplicate)', async () => {
    // #205 S4 (parked B) — backdrop + ✕ both used aria-label="Close outline";
    // a screen reader hit two adjacent identically-named buttons. The ✕ keeps
    // "Close outline"; the backdrop gets a distinct label.
    stubMobileMedia(true);
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    act(() => { dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' }); });
    render(<App />);

    await waitFor(() => expect(document.querySelector('.conv-outline-sheet')).not.toBeNull());
    const backdrop = document.querySelector<HTMLButtonElement>('.conv-outline-backdrop')!;
    const closeBtn = document.querySelector<HTMLButtonElement>('.conv-outline-close')!;
    expect(closeBtn.getAttribute('aria-label')).toBe('Close outline');
    expect(backdrop.getAttribute('aria-label')).toBe('Dismiss outline (tap outside)');
    expect(backdrop.getAttribute('aria-label')).not.toBe(closeBtn.getAttribute('aria-label'));
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

  // #217 S3 E10#8 — reach the rail search from INSIDE an open reader without
  // pressing Esc first. `/` is reader-aware (opens the in-conversation find bar
  // when a reader is open), so a separate key (`f`) focuses the rail search input
  // regardless of whether a reader is open. Gated on the shared guards + the #156
  // conversations-view scope.
  it('13b: "f" focuses the rail search input even with a reader open (no Esc needed)', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-reader-head')).not.toBeNull());
    await waitFor(() => expect(document.querySelector('.conv-rail-search-input')).not.toBeNull());

    expect(getState().selectedConversationId).toBe('sess-1');
    act(() => { fireEvent.keyDown(document, { key: 'f' }); });
    // It focuses the rail search input WITHOUT opening the in-conversation find.
    expect(getState().convFindOpen).toBe(false);
    expect(document.activeElement).toBe(document.querySelector('.conv-rail-search-input'));
  });

  it('13c: "f" is inert while a modal is open (guard)', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-rail-search-input')).not.toBeNull());
    const railInput = document.querySelector('.conv-rail-search-input');
    act(() => { dispatch({ type: 'OPEN_MODAL', kind: 'forecast' }); });
    act(() => { fireEvent.keyDown(document, { key: 'f' }); });
    // Guard suppresses it: focus did NOT move to the rail input.
    expect(document.activeElement).not.toBe(railInput);
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

  // #217 S4 QA fix — Esc while focus is on a find-bar BUTTON (not the input)
  // must behave like Esc in the input: close ONLY the find bar and stay in the
  // reader. Before the fix, Esc on a focused bar button bubbled past the bar to
  // the document keydown listener, firing the view-level global Esc → SET_VIEW
  // 'dashboard' (selectedConversationId cleared, reader torn down). The bar-level
  // Esc handler + stopPropagation now blocks that leak; onClose restores focus to
  // the thread (not <body>).
  it('15b: Esc on a find-bar BUTTON closes find, keeps the reader open, and restores thread focus', async () => {
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-reader-head')).not.toBeNull());
    // The thread must be reachable so onClose can restore focus to it.
    await waitFor(() => expect(document.querySelector('.conv-reader-thread')).not.toBeNull());

    act(() => { fireEvent.keyDown(document, { key: '/' }); });
    const closeBtn = await waitFor(() => {
      const el = document.querySelector<HTMLButtonElement>('.conv-findbar-close');
      expect(el).not.toBeNull();
      return el!;
    });
    // Move focus to the bar's Close button — the QA repro focuses a bar control,
    // not the input.
    act(() => { closeBtn.focus(); });
    expect(document.activeElement).toBe(closeBtn);

    // Esc on the focused button: closes find, but the reader stays mounted and we
    // remain in the conversations view (no global-Esc leak).
    fireEvent.keyDown(closeBtn, { key: 'Escape' });
    expect(getState().convFindOpen).toBe(false);
    expect(getState().view).toBe('conversations');
    expect(getState().selectedConversationId).toBe('sess-1');
    // Focus is restored to the thread, not dropped to <body>.
    await waitFor(() =>
      expect(document.activeElement).toBe(document.querySelector('.conv-reader-thread')),
    );
  });

  it('8: an SSE tick carrying transcriptsEnabled keeps the switcher; a tick omitting it hides it (SSE envelopes must carry the gate)', () => {
    // Bootstrap (the /api/data shape): switcher shown.
    updateSnapshot(baseEnvelope(true, '2026-05-13T10:00:00Z'));
    render(<App />);
    expect(screen.getByRole('group', { name: 'Workspace' })).not.toBeNull();

    // A NEWER snapshot that ALSO carries the field (the FIXED SSE envelope —
    // _serve_api_events now injects transcriptsEnabled per connection). The
    // store replaces the whole snapshot on every tick, so the switcher must
    // PERSIST rather than vanishing ~15s after bootstrap.
    act(() => { updateSnapshot(baseEnvelope(true, '2026-05-13T10:00:15Z')); });
    expect(screen.getByRole('group', { name: 'Workspace' })).not.toBeNull();

    // Contract pin: a still-newer snapshot that OMITS the field hides the
    // switcher. This is exactly the pre-fix SSE behavior — it documents that
    // SSE envelopes MUST carry transcriptsEnabled or the steady-state UI
    // loses the gate. The day the backend injection is dropped, this branch
    // reproduces the regression (switcher gone after the first SSE tick).
    act(() => { updateSnapshot(baseEnvelope(undefined, '2026-05-13T10:00:30Z')); });
    expect(screen.queryByRole('group', { name: 'Workspace' })).toBeNull();
  });

  // #228 S1 (F3) — closing a comparison must return keyboard focus to the
  // "Compare with…" trigger in the single reader, never drop it to <body>. The
  // reader loads async, so this is driven to completion before asserting focus.
  it('F3: closing a comparison returns focus to #conv-compare-with once the reader detail renders', async () => {
    updateSnapshot(baseEnvelope(true));
    // Enter a comparison anchored on sess-1 (A is the anchor → the single
    // reader falls back to it on close).
    act(() => { dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' }); });
    render(<App />);

    // The comparison takes over the workspace.
    await waitFor(() => expect(document.querySelector('.conv-cmp')).not.toBeNull());
    const closeBtn = await waitFor(() => {
      const el = document.querySelector<HTMLButtonElement>('.conv-cmp-close');
      expect(el).not.toBeNull();
      return el!;
    });

    // Close via the header ✕ — the real ComparisonView path through onClose →
    // CLOSE_COMPARE (which arms the focus-return flag).
    fireEvent.click(closeBtn);
    expect(getState().compare).toBeNull();

    // The single reader re-renders for the anchor; once its detail lands, focus
    // returns to the compare trigger — never <body>.
    const trigger = await waitFor(() => {
      const el = document.getElementById('conv-compare-with');
      expect(el).not.toBeNull();
      return el!;
    });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(document.activeElement).not.toBe(document.body);
    // The one-shot flag is cleared after the return.
    expect(getState().compareCloseFocusPending).toBe(false);
  });

  // #228 S1 (F3) — the not-found comparison state routes its close through the
  // SAME shared onClose, so focus return still works when an outline 404s.
  it('F3: the not-found comparison close also returns focus via the shared handler', async () => {
    // Override fetch so sess-2's OUTLINE 404s (→ outB.error → ComparisonNotFound),
    // while sess-1 (the anchor reader) loads normally.
    const fn = vi.fn(async (url: string | URL) => {
      const u = String(url);
      let body: unknown = {};
      let ok = true;
      let status = 200;
      if (u.includes('/api/conversation/search')) body = searchResult;
      else if (u.includes('/api/conversations')) body = conversationsPage;
      else if (u.includes('/outline')) {
        if (u.includes('sess-2')) { ok = false; status = 404; body = {}; }
        else body = outlinePayload();
      } else if (u.includes('/api/conversation/')) body = detail();
      return { ok, status, json: async () => body } as Response;
    });
    globalThis.fetch = fn as unknown as typeof fetch;

    updateSnapshot(baseEnvelope(true));
    act(() => { dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' }); });
    render(<App />);

    // The not-found state renders (sess-2 outline 404'd) with its own close ✕.
    await waitFor(() => expect(document.querySelector('.conv-cmp--notfound')).not.toBeNull());
    const closeBtn = document.querySelector<HTMLButtonElement>('.conv-cmp--notfound .conv-cmp-close')!;
    expect(closeBtn).not.toBeNull();

    fireEvent.click(closeBtn);
    expect(getState().compare).toBeNull();
    expect(getState().compareCloseFocusPending).toBe(true);

    const trigger = await waitFor(() => {
      const el = document.getElementById('conv-compare-with');
      expect(el).not.toBeNull();
      return el!;
    });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(document.activeElement).not.toBe(document.body);
  });

  // C2 (#238 S3, Codex gate #1) — OPEN_COMPARE must clear convFiltersOpen so the
  // inView-gated Escape binding can fire while a comparison is open.
  it('C2: OPEN_COMPARE clears an open filters popover', () => {
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true });
    expect(getState().convFiltersOpen).toBe(true);
    dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' });
    expect(getState().convFiltersOpen).toBe(false);
    expect(getState().compare).toEqual({
      a: { source: 'claude', key: 'sess-1' },
      b: { source: 'claude', key: 'sess-2' },
    });
  });

  // C2 (#238 S3) — Escape inside an open comparison closes it back to the reader
  // (mirroring ✕ Close → CLOSE_COMPARE), NOT eject to the dashboard.
  it('C2: Escape in an open comparison closes it (CLOSE_COMPARE), not eject to dashboard', async () => {
    updateSnapshot(baseEnvelope(true));
    act(() => { dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' }); });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-cmp')).not.toBeNull());

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(getState().compare).toBeNull();
    expect(getState().compareCloseFocusPending).toBe(true);
    expect(getState().view).toBe('conversations'); // stayed in the workspace
  });

  // C2 ordering (resolved Q1) — comparison-close wins over rail-search-clear: one
  // Escape closes the comparison and LEAVES the needle intact.
  it('C2: Escape closes the comparison before clearing a leftover rail-search needle', async () => {
    updateSnapshot(baseEnvelope(true));
    act(() => {
      dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'abc' });
      dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' });
    });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-cmp')).not.toBeNull());

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(getState().compare).toBeNull();
    expect(getState().conversationSearch).toBe('abc'); // needle untouched
  });

  // C2 + Codex gate #1 — with the filters popover opened before the comparison,
  // a single Escape still closes the comparison (convFiltersOpen was cleared, so
  // inView is true and the binding fires).
  it('C2: a single Escape closes the comparison even if the filters popover was open at entry', async () => {
    updateSnapshot(baseEnvelope(true));
    act(() => {
      dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true });
      dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' });
    });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-cmp')).not.toBeNull());
    expect(getState().convFiltersOpen).toBe(false);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().compare).toBeNull();
    // Discriminate close-to-reader from eject-to-dashboard: the eject path ALSO
    // nulls `compare`, so `compare===null` alone is vacuous for the C2 branch.
    // These two prove the comparison closed back to the reader (CLOSE_COMPARE),
    // not that the workspace ejected.
    expect(getState().view).toBe('conversations');
    expect(getState().compareCloseFocusPending).toBe(true);
  });
});

// #228 S3 F1 — the outline lives as a slide-over SHEET across the whole
// no-column band (≤1100px, keyed on !useIsWide), not just on mobile (≤640px).
// The persistent column + resizer render only when wide (≥1101px). #304 S1
// split the old 641–1100 tablet band in two (single-pane ≤880 / two-pane
// 881–1100), so these branch tests now use the shared FOUR-band resolver above
// and probe the UPPER tablet band (881–1100) for the two-pane-with-sheet case
// (stubMobileMedia returns one value for all queries, so it can't express a
// band where useIsMobile=false but useIsWide=false too). The visual
// sheet animation / first-paint is the ui-qa gate; here we pin the DOM branch.
describe('Conversations outline sheet generalized to the tablet band (#228 S3 F1)', () => {
  // #304 S1 — the two-pane-with-sheet behavior now belongs to the UPPER tablet
  // band (881–1100); 641–880 became single-pane (see the compact-workspace
  // describe). Uses the shared four-band module resolver above.
  it('renders the outline SHEET (not the column) in the 881–1100 upper-tablet band', async () => {
    stubResponsiveMedia(bandResolver('upperTablet'));
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    act(() => { dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' }); });
    render(<App />);

    // The slide-over sheet mounts (the tablet-band ☰ is now LIVE); the persistent
    // outline column does NOT (it's the wide-only surface).
    await waitFor(() => {
      expect(document.querySelector('.conv-outline-sheet')).not.toBeNull();
      expect(document.querySelector('.conv-outline-sheet .conv-outline')).not.toBeNull();
    });
    expect(document.querySelector('.conv-view--outline > .conv-outline')).toBeNull();
    // It is NOT the mobile single-pane: the rail stays mounted beside the reader
    // (the desktop two-pane shell), confirming the 640 vs 1100 split is distinct.
    expect(document.querySelector('.conv-rail')).not.toBeNull();
  });

  it('renders the COLUMN (not the sheet) only when wide (≥1101px)', async () => {
    stubResponsiveMedia(bandResolver('wide'));
    localStorage.setItem('cctally.conv.outlineOpen', 'true');
    _resetForTests();
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    render(<App />);

    await waitFor(() => {
      expect(document.querySelector('.conv-view--outline > .conv-outline')).not.toBeNull();
    });
    expect(document.querySelector('.conv-outline-sheet')).toBeNull();
    localStorage.removeItem('cctally.conv.outlineOpen');
  });

  it('crossing from upper-tablet (sheet open) into wide closes the sheet (no resurrect-on-resize)', async () => {
    const ctl = stubResponsiveMedia(bandResolver('upperTablet'));
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' });
    act(() => { dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' }); });
    render(<App />);

    await waitFor(() => expect(document.querySelector('.conv-outline-sheet')).not.toBeNull());
    expect(getState().convOutlineMobileOpen).toBe(true);

    // Resize the viewport up into the wide band: the rising edge of isWide must
    // dispatch CLOSE_CONV_OUTLINE_MOBILE so the ephemeral sheet flag is cleared
    // (it otherwise only resets on a conversation switch, so tablet→wide→tablet
    // would resurrect it — Codex P3).
    act(() => { ctl.set(bandResolver('wide')); });
    expect(getState().convOutlineMobileOpen).toBe(false);
    await waitFor(() => expect(document.querySelector('.conv-outline-sheet')).toBeNull());
  });
});

// #304 S1 — the compact-workspace band (641–880) uses single-pane rail/reader
// navigation (like phones), not the crushed two-pane shell. Upper-tablet
// (881–1100) keeps two panes. Per-query matchMedia stub so the 880 and 1100
// breakpoints stay distinct.
describe('Conversations compact-workspace single-pane band (#304 S1)', () => {
  it('641–880: selecting a conversation REPLACES the rail with the reader (single-pane)', async () => {
    stubResponsiveMedia(bandResolver('compact'));
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);
    await waitFor(() => expect(document.querySelector('.conv-rail')).not.toBeNull());
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' }); });
    // Single-pane: reader mounts, rail is gone, Back is present.
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    expect(document.querySelector('.conv-rail')).toBeNull();
    expect(document.querySelector('.conv-back')).not.toBeNull();
    // Back returns to the rail.
    act(() => { (document.querySelector('.conv-back') as HTMLButtonElement).click(); });
    await waitFor(() => expect(document.querySelector('.conv-rail')).not.toBeNull());
  });

  it('881–1100: selecting a conversation keeps the rail mounted beside the reader (two-pane)', async () => {
    stubResponsiveMedia(bandResolver('upperTablet'));
    updateSnapshot(baseEnvelope(true));
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    expect(document.querySelector('.conv-rail')).not.toBeNull();
  });

  it('641–880: an open comparison goes full-width (rail hidden)', async () => {
    stubResponsiveMedia(bandResolver('compact'));
    updateSnapshot(baseEnvelope(true));
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' }); });
    await waitFor(() => expect(document.querySelector('.conv-view--compare')).not.toBeNull());
    expect(document.querySelector('.conv-rail')).toBeNull();
  });

  it('881–1100: an open comparison keeps the rail beside it', async () => {
    stubResponsiveMedia(bandResolver('upperTablet'));
    updateSnapshot(baseEnvelope(true));
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' }); });
    await waitFor(() => expect(document.querySelector('.conv-view--compare')).not.toBeNull());
    expect(document.querySelector('.conv-rail')).not.toBeNull();
  });
});

// #289 — Escape peels one layer at a time (compare → outline sheet →
// reader-deselect → rail-search-clear → dashboard). These cases drive the
// exported binding's `action` directly against the real store, pinning the peel
// order without rendering the whole App. The load-bearing non-vacuous case is
// "reader open AND rail search set": an implementor who reversed the
// reader-deselect and search-clear branches fails exactly there.
describe('#289 Escape peel — CONVERSATIONS_BINDINGS action matrix', () => {
  const escapeAction = () =>
    CONVERSATIONS_BINDINGS.find((b) => b.key === 'Escape')!.action();

  beforeEach(() => { _resetForTests(); dispatch({ type: 'SET_VIEW', view: 'conversations' }); });
  afterEach(() => { _resetForTests(); });

  it('compare open → CLOSE_COMPARE (regression guard), not deselect', () => {
    dispatch({ type: 'OPEN_COMPARE', a: 'abc', b: 'def' });
    escapeAction();
    expect(getState().compare).toBeNull();
    // The anchor selection survives (close-to-reader), and the focus-return flag
    // is armed — proves CLOSE_COMPARE fired, not an eject/deselect.
    expect(getState().selectedConversationId).toBe('abc');
    expect(getState().compareCloseFocusPending).toBe(true);
  });

  it('outline sheet open (reader open) → closes only the sheet, reader stays', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    dispatch({ type: 'TOGGLE_CONV_OUTLINE_MOBILE' }); // opens convOutlineMobileOpen
    expect(getState().convOutlineMobileOpen).toBe(true);
    escapeAction();
    expect(getState().convOutlineMobileOpen).toBe(false);
    expect(getState().selectedConversationId).toBe('abc'); // reader NOT deselected
  });

  it('reader open + nothing else → deselects to the list, not the dashboard', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    escapeAction();
    expect(getState().selectedConversationId).toBeNull();
    expect(getState().view).toBe('conversations'); // NOT 'dashboard'
  });

  it('reader open AND rail search set → deselects but PRESERVES the needle (pins reader-before-search order)', () => {
    dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'abc' });
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'needle' });
    escapeAction();
    expect(getState().selectedConversationId).toBeNull();
    expect(getState().conversationSearch).toBe('needle'); // search survives the deselect
    expect(getState().view).toBe('conversations');
  });

  it('no selection + rail search set → clears the search (still conversations)', () => {
    dispatch({ type: 'SET_CONVERSATION_SEARCH', text: 'x' });
    escapeAction();
    expect(getState().conversationSearch).toBe('');
    expect(getState().view).toBe('conversations');
  });

  it('no selection, no search, no compare → leaves to the dashboard', () => {
    escapeAction();
    expect(getState().view).toBe('dashboard');
  });
});

// #304 S2 — compact comparison pick flow (spec §1). The view-layer gate makes
// the rail (with its pick banner) the visible pane whenever comparePick is
// set, so the F2 five-step dead end is structurally impossible.
describe('#304 S2 compact comparison pick flow', () => {
  async function openCompactReader(): Promise<void> {
    stubResponsiveMedia(bandResolver('compact'));
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
  }

  it('⋯ → Compare with… swaps the reader for the rail in pick mode, anchor retained', async () => {
    await openCompactReader();
    fireEvent.click(document.querySelector('.conv-overflow-toggle') as HTMLButtonElement);
    fireEvent.click(screen.getByRole('menuitem', { name: /compare with/i }));
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    expect(document.querySelector('.conv-reader')).toBeNull();      // single-pane: rail took over
    expect(document.querySelector('.conv-back')).toBeNull();        // no Back in pick mode
    expect(getState().comparePick).toEqual({ anchor: { source: 'claude', key: 'sess-1' } });
    expect(getState().selectedConversationId).toBe('sess-1');       // anchor retained
  });

  it('full chain: pick a non-anchor row → comparison → swap → close → Back to rail', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    const rows = Array.from(document.querySelectorAll<HTMLButtonElement>('.conv-rail-row'));
    const target = rows.find((r) => !r.disabled)!;                  // anchor row is disabled
    fireEvent.click(target);
    await waitFor(() => expect(document.querySelector('.conv-cmp')).not.toBeNull());
    expect(getState().compare).toEqual({
      a: { source: 'claude', key: 'sess-1' },
      b: { source: 'claude', key: 'sess-2' },
    });
    expect(document.querySelector('.conv-rail')).toBeNull();        // full-width compact comparison
    fireEvent.click(screen.getByRole('button', { name: /swap the two sessions/i }));
    expect(getState().compare).toEqual({
      a: { source: 'claude', key: 'sess-2' },
      b: { source: 'claude', key: 'sess-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: /close comparison/i }));
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    expect(getState().selectedConversationId).toBe('sess-1');       // anchor reader is back
    act(() => { (document.querySelector('.conv-back') as HTMLButtonElement).click(); });
    await waitFor(() => expect(document.querySelector('.conv-rail')).not.toBeNull());
    expect(document.querySelector('.conv-rail-pickbanner')).toBeNull(); // Back leaves no stale pick
  });

  it('banner Cancel returns to the anchor reader (pick cleared, selection kept)', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /cancel comparison pick/i }));
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    expect(getState().comparePick).toBeNull();
    expect(getState().selectedConversationId).toBe('sess-1');
  });

  it('Escape cancels pick back to the anchor reader (rail capture listener)', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    expect(getState().comparePick).toBeNull();
    expect(getState().selectedConversationId).toBe('sess-1');
  });

  it("'/' during pick focuses the rail search, never the (unmounted) reader find", async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    fireEvent.keyDown(document, { key: '/' });
    expect(getState().convFindOpen).toBe(false);
    expect(document.activeElement).toBe(document.querySelector('.conv-rail-search input'));
  });

  it('881–1100 two-pane pick is unchanged: rail shows the banner beside the reader', async () => {
    stubResponsiveMedia(bandResolver('upperTablet'));
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    expect(document.querySelector('.conv-reader')).not.toBeNull();  // reader stays mounted
  });

  it('compact entry steals stranded focus to the banner Cancel', async () => {
    await openCompactReader();
    fireEvent.click(document.querySelector('.conv-overflow-toggle') as HTMLButtonElement);
    fireEvent.click(screen.getByRole('menuitem', { name: /compare with/i }));
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    // The ⋯ trigger the menu refocused has unmounted with the reader — the
    // rail's entry effect must move the stranded focus onto Cancel.
    await waitFor(() =>
      expect(document.activeElement).toBe(document.querySelector('.conv-rail-pickcancel')));
  });

  it('desktop (two-pane) entry does NOT steal focus from the compare trigger', async () => {
    stubResponsiveMedia(bandResolver('wide'));
    updateSnapshot(baseEnvelope(true));
    dispatch({ type: 'SET_VIEW', view: 'conversations' });
    render(<App />);
    act(() => { dispatch({ type: 'OPEN_CONVERSATION', sessionId: 'sess-1' }); });
    await waitFor(() => expect(document.getElementById('conv-compare-with')).not.toBeNull());
    (document.getElementById('conv-compare-with') as HTMLButtonElement).focus();
    fireEvent.click(document.getElementById('conv-compare-with') as HTMLButtonElement);
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    expect(document.activeElement).toBe(document.getElementById('conv-compare-with'));
  });

  it('compact cancel lands focus on the compact ⋯ toggle once the reader is back', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /cancel comparison pick/i }));
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    await waitFor(() => {
      expect(document.activeElement).toBe(document.querySelector('.conv-overflow-toggle'));
      expect(getState().compareCloseFocusPending).toBe(false);      // consumed
    });
  });

  it('compact comparison ✕ Close lands focus on the ⋯ toggle too', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'OPEN_COMPARE', a: 'sess-1', b: 'sess-2' }); });
    await waitFor(() => expect(document.querySelector('.conv-cmp')).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /close comparison/i }));
    await waitFor(() => expect(document.querySelector('.conv-reader')).not.toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(document.querySelector('.conv-overflow-toggle')));
  });

  it('Escape in the focused search input clears the needle, NOT the pick', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    const input = document.querySelector('.conv-rail-search-input') as HTMLInputElement;
    fireEvent.focus(input);                                  // SET_INPUT_MODE 'search'
    fireEvent.change(input, { target: { value: 'abc' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(getState().comparePick).toEqual({
      anchor: { source: 'claude', key: 'sess-1' },
    });   // pick survives
    expect(getState().conversationSearch).toBe('');                 // input handled its Esc
    fireEvent.blur(input);                                   // (the real handler blurs; JSDOM needs the explicit event)
    fireEvent.keyDown(document, { key: 'Escape' });          // next Esc cancels the pick
    expect(getState().comparePick).toBeNull();
  });

  it('Escape with the filters popover open closes the popover, NOT the pick', async () => {
    await openCompactReader();
    act(() => { dispatch({ type: 'START_COMPARE_PICK', anchor: 'sess-1' }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-pickbanner')).not.toBeNull());
    act(() => { dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true }); });
    await waitFor(() => expect(document.querySelector('.conv-rail-filters')).not.toBeNull());
    fireEvent.keyDown(document.querySelector('.conv-rail-filters') as HTMLElement, { key: 'Escape' });
    expect(getState().convFiltersOpen).toBe(false);
    expect(getState().comparePick).toEqual({
      anchor: { source: 'claude', key: 'sess-1' },
    });   // pick survives
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(getState().comparePick).toBeNull();
  });
});
