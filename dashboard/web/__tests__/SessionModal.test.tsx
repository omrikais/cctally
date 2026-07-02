import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { SessionModal } from '../src/modals/SessionModal';
import { dispatch, updateSnapshot, _resetForTests } from '../src/store/store';
import fixture from './fixtures/envelope.json';
import sessionFixture from './fixtures/session-detail.json';
import type { Envelope } from '../src/types/envelope';

// The modal's CacheRebuildsSection fires its OWN GET /api/conversation/<id>/
// outline per tick (a sibling of the /api/session/<id> fetch). It is
// suppressed when markers are off — so these tests flip markers OFF in
// beforeEach to keep the fetch stream session-only, isolating the
// session-detail fetch behavior these regressions assert on. (A dedicated
// SessionModal.test.tsx under src/modals covers the section's own wiring.)
describe('<SessionModal />', () => {
  beforeEach(() => {
    _resetForTests();
    // Markers OFF → CacheRebuildsSection short-circuits (no outline fetch), so
    // the fetch stream stays session-only for these session-detail tests.
    dispatch({ type: 'INGEST_DASHBOARD_PREFS', prefs: { cache_failure_markers: false } });
    updateSnapshot(fixture as unknown as Envelope);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        status: 200,
        json: () => Promise.resolve(sessionFixture),
        ok: true,
      }),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fetches /api/session/:id on mount', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(
      () => {
        expect(vi.mocked(fetch)).toHaveBeenCalledWith(
          `/api/session/${sid}`,
          expect.anything(),
        );
      },
      { timeout: 1000 },
    );
  });

  it('refetches /api/session/:id on each new generated_at', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1), {
      timeout: 1000,
    });
    // Three monotonically-advancing ticks → three additional fetches.
    for (let i = 0; i < 3; i++) {
      updateSnapshot({
        ...(fixture as unknown as Envelope),
        generated_at: `2026-04-24T13:08:0${i + 1}Z`,
      });
      // Allow microtasks to flush so the effect re-runs and dispatches the fetch.
      await Promise.resolve();
      await Promise.resolve();
    }
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(4), {
      timeout: 1000,
    });
  });

  it('does not refetch when generated_at is unchanged', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1), {
      timeout: 1000,
    });
    // Re-publish the same snapshot (same generated_at). Note: updateSnapshot's
    // monotonic guard rejects strictly-older generated_at values, but equal
    // values pass through — the React effect's dep comparator (Object.is) is
    // what prevents the refetch in this case.
    for (let i = 0; i < 3; i++) {
      updateSnapshot(fixture as unknown as Envelope);
      await Promise.resolve();
    }
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it('stale-while-revalidate: spinner not shown on refetch', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    // Wait for initial content to render.
    await waitFor(() => expect(document.getElementById('msess-content')).not.toBeNull(), {
      timeout: 1000,
    });
    // Advance generated_at; during the in-flight refetch the prior content
    // must remain mounted and the loading spinner must NOT appear.
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:01Z',
    });
    expect(document.getElementById('msess-content')).not.toBeNull();
    expect(document.getElementById('msess-loading')).toBeNull();
  });

  it('refetch network error silently keeps stale content', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.getElementById('msess-content')).not.toBeNull(), {
      timeout: 1000,
    });
    // Next fetch rejects.
    vi.mocked(fetch).mockRejectedValueOnce(new Error('connection refused'));
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:01Z',
    });
    await Promise.resolve();
    await Promise.resolve();
    // Stale content kept; no error surfaced; spinner never appeared.
    expect(document.getElementById('msess-content')).not.toBeNull();
    expect(document.getElementById('msess-error')).toBeNull();
    expect(document.getElementById('msess-loading')).toBeNull();
  });

  it('initial fetch interrupted by tick: 404 on retry still evicts (not silently kept)', async () => {
    // Regression: with the empty-modal-as-initial guard, an aborted initial
    // fetch followed by a 404 retry must surface the error rather than take
    // the keep-stale path (which would leave the spinner stuck).
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    // Fetch #1 hangs forever — only resolves via abort.
    vi.mocked(fetch).mockImplementationOnce(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          (init as RequestInit | undefined)?.signal?.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          });
        }),
    );
    // Fetch #2 (after tick) returns 404.
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 404,
      ok: false,
      json: () => Promise.resolve({}),
    } as unknown as Response);

    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1), {
      timeout: 1000,
    });
    expect(document.getElementById('msess-loading')).not.toBeNull();

    // Tick: aborts fetch #1 and dispatches fetch #2 (the 404). Without the
    // empty-modal guard, isInitialFetch would be false → keep-stale path →
    // setLoading(false) never fires → spinner sticks.
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:01Z',
    });

    await waitFor(
      () => {
        expect(document.getElementById('msess-error')?.textContent).toMatch(
          /Session not found/,
        );
      },
      { timeout: 1000 },
    );
    expect(document.getElementById('msess-loading')).toBeNull();
    expect(document.getElementById('msess-content')).toBeNull();
  });

  it('initial fetch interrupted by tick: network error on retry surfaces "Failed to load"', async () => {
    // Regression: parallel to the 404 case above for non-404 errors. Without
    // the empty-modal guard, the catch's isInitialFetch=false branch would
    // silently swallow the error and leave the spinner stuck.
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    vi.mocked(fetch).mockImplementationOnce(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          (init as RequestInit | undefined)?.signal?.addEventListener('abort', () => {
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          });
        }),
    );
    vi.mocked(fetch).mockRejectedValueOnce(new Error('connection refused'));

    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1), {
      timeout: 1000,
    });
    expect(document.getElementById('msess-loading')).not.toBeNull();

    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:01Z',
    });

    await waitFor(
      () => {
        expect(document.getElementById('msess-error')?.textContent).toMatch(
          /Failed to load/,
        );
      },
      { timeout: 1000 },
    );
    expect(document.getElementById('msess-loading')).toBeNull();
    expect(document.getElementById('msess-content')).toBeNull();
  });

  it('single 404 on refetch keeps stale content; second consecutive 404 evicts', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.getElementById('msess-content')).not.toBeNull(), {
      timeout: 1000,
    });

    // First refetch: 404 → keep stale content.
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 404,
      ok: false,
      json: () => Promise.resolve({}),
    } as unknown as Response);
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:01Z',
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(document.getElementById('msess-content')).not.toBeNull();
    expect(document.getElementById('msess-error')).toBeNull();

    // Second consecutive refetch: 404 again → eviction.
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 404,
      ok: false,
      json: () => Promise.resolve({}),
    } as unknown as Response);
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:02Z',
    });
    await waitFor(
      () => {
        expect(document.getElementById('msess-error')?.textContent).toMatch(
          /Session not found/,
        );
      },
      { timeout: 1000 },
    );
    expect(document.getElementById('msess-content')).toBeNull();
  });

  it('successful refetch clears the 404 arm', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.getElementById('msess-content')).not.toBeNull(), {
      timeout: 1000,
    });

    // 404 #1 → arm.
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 404,
      ok: false,
      json: () => Promise.resolve({}),
    } as unknown as Response);
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:01Z',
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(document.getElementById('msess-content')).not.toBeNull();

    // 200 → clears the arm (mock the default return for the next fetch via mockResolvedValueOnce).
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 200,
      ok: true,
      json: () => Promise.resolve(sessionFixture),
    } as unknown as Response);
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:02Z',
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(document.getElementById('msess-content')).not.toBeNull();

    // 404 #2 (post-success) → keep stale, do NOT evict (arm was cleared).
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 404,
      ok: false,
      json: () => Promise.resolve({}),
    } as unknown as Response);
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:03Z',
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(document.getElementById('msess-content')).not.toBeNull();
    expect(document.getElementById('msess-error')).toBeNull();

    // 404 #3 (consecutive) → NOW evict.
    vi.mocked(fetch).mockResolvedValueOnce({
      status: 404,
      ok: false,
      json: () => Promise.resolve({}),
    } as unknown as Response);
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:04Z',
    });
    await waitFor(
      () => {
        expect(document.getElementById('msess-error')?.textContent).toMatch(
          /Session not found/,
        );
      },
      { timeout: 1000 },
    );
    expect(document.getElementById('msess-content')).toBeNull();
  });

  it('bound id is stable across ticks even if newest row changes', async () => {
    // Open the modal with no sessionId — falls back to the newest row.
    dispatch({ type: 'OPEN_MODAL', kind: 'session' });
    render(<SessionModal />);
    const originalId = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    await waitFor(
      () => {
        expect(vi.mocked(fetch)).toHaveBeenCalledWith(
          `/api/session/${originalId}`,
          expect.anything(),
        );
      },
      { timeout: 1000 },
    );

    // Construct a new snapshot whose newest row is a DIFFERENT id, then tick.
    const env = fixture as unknown as Envelope;
    const newerRow = { ...env.sessions.rows[0], session_id: 'session-NEWER-0000-0000-0000-000000000000' };
    updateSnapshot({
      ...env,
      generated_at: '2026-04-24T13:08:01Z',
      sessions: {
        ...env.sessions,
        rows: [newerRow, ...env.sessions.rows],
      },
    });
    await Promise.resolve();
    await Promise.resolve();

    // The next fetch must STILL hit the originally-bound id, not the new newest.
    const calls = vi.mocked(fetch).mock.calls;
    const lastCall = calls[calls.length - 1];
    expect(lastCall[0]).toBe(`/api/session/${originalId}`);
  });

  it('aborts fetch on unmount (StrictMode double-mount safety)', async () => {
    const abortSpy = vi.spyOn(AbortController.prototype, 'abort');
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    const { unmount } = render(<SessionModal />);
    unmount();
    expect(abortSpy).toHaveBeenCalled();
    abortSpy.mockRestore();
  });

  it('renders loading state before fetch resolves', () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    const loading = document.getElementById('msess-loading');
    expect(loading).not.toBeNull();
    expect(loading?.textContent).toMatch(/Loading session detail/);
  });

  it('renders badge + three hero kv cards after fetch', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.getElementById('msess-content')).not.toBeNull(), {
      timeout: 1000,
    });
    const badge = document.getElementById('msess-id');
    expect(badge?.classList.contains('msess-badge')).toBe(true);
    expect(badge?.textContent).toBe('session-0000-0000-0000-0000-000000000000');
    // Three hero cards with correct icon hrefs
    expect(document.querySelector('.m-kv.kv-cost svg use')?.getAttribute('href')).toBe('/static/icons.svg#dollar');
    expect(document.querySelector('.m-kv.kv-dur svg use')?.getAttribute('href')).toBe('/static/icons.svg#clock');
    expect(document.querySelector('.m-kv.kv-proj svg use')?.getAttribute('href')).toBe('/static/icons.svg#folder');
    expect(document.getElementById('msess-cost')?.textContent).toBe('$1.23');
    expect(document.getElementById('msess-dur')?.textContent).toBe('15 min');
    expect(document.getElementById('msess-project')?.textContent).toBe('project-00');
  });

  it('renders Tokens grid with tiles and cache-hit bar', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.getElementById('msess-tokens')).not.toBeNull(), {
      timeout: 1000,
    });
    const tok = document.getElementById('msess-tokens');
    const tiles = tok?.querySelectorAll('.msess-tok-tile');
    // All 5 fields present in fixture → 5 tiles
    expect(tiles?.length).toBe(5);
    const cacheTile = tok?.querySelector('.msess-tok-tile.cache-hit');
    expect(cacheTile).not.toBeNull();
    expect(cacheTile?.querySelector('.bar .fill')).not.toBeNull();
  });

  // #260 — the standalone "Models" chip strip was dropped; ModelCostBars (the
  // shared History/Block/Projects recipe) carries a model-family colour chip
  // per row, so the chip coverage moved there.
  it('drops the standalone Models section; ModelCostBars carries family-class chips (#260)', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.querySelector('.sec-costm')).not.toBeNull(), {
      timeout: 1000,
    });
    // The former standalone "Models" chip strip is gone.
    expect(document.getElementById('msess-models')).toBeNull();
    expect(document.querySelector('.m-sec.sec-mod')).toBeNull();
    // ModelCostBars renders one chip per model with the model-family colour class.
    const chips = document.querySelectorAll('.drill-bar-row .chip');
    expect(chips.length).toBe(2);
    const classes = Array.from(chips).map((c) => c.className);
    expect(classes.some((c) => c.includes('opus'))).toBe(true);
    expect(classes.some((c) => c.includes('haiku'))).toBe(true);
  });

  it('renders Cost by model via the shared ModelCostBars (#260)', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.querySelector('.sec-costm')).not.toBeNull(), {
      timeout: 1000,
    });
    // The bespoke segmented bar + legend are gone.
    expect(document.getElementById('msess-cost-bar')).toBeNull();
    expect(document.getElementById('msess-cost-legend')).toBeNull();
    // One ModelCostBars row per model, short abbreviateModel labels + fmt.usd2
    // costs (2-dec), cost-descending.
    const rows = document.querySelectorAll('.drill-bar-row');
    expect(rows.length).toBe(2);
    expect(document.body.textContent).toContain('opus-4-5');
    expect(document.body.textContent).toContain('$0.95');
    expect(document.body.textContent).toContain('$0.28');
    // pie-chart icon on the section header (unchanged).
    expect(document.querySelector('.m-sec.sec-costm svg use')?.getAttribute('href')).toBe('/static/icons.svg#pie-chart');
  });

  it('falls back to newest row when OPEN_MODAL dispatched without sessionId', async () => {
    // Panel-level Tab+Enter path: dispatch with no sessionId. Parity with
    // main's renderSessionsModal → rows[0].session_id fallback.
    dispatch({ type: 'OPEN_MODAL', kind: 'session' });
    render(<SessionModal />);
    const expected = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    await waitFor(
      () => {
        expect(vi.mocked(fetch)).toHaveBeenCalledWith(
          `/api/session/${expected}`,
          expect.anything(),
        );
      },
      { timeout: 1000 },
    );
  });

  it('shows "No session available." when no id and rows is empty', () => {
    // Wipe sessions.rows so the fallback has nothing to resolve to. Use a
    // generated_at later than the seeded fixture so the monotonic guard in
    // updateSnapshot lets this frame replace the fixture rows.
    updateSnapshot({
      ...(fixture as unknown as Envelope),
      generated_at: '2026-04-24T13:08:00Z',
      sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    });
    dispatch({ type: 'OPEN_MODAL', kind: 'session' });
    render(<SessionModal />);
    // No fetch fired, loading cleared, error surfaced.
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
    expect(document.getElementById('msess-loading')).toBeNull();
    const errorEl = document.getElementById('msess-error');
    expect(errorEl?.textContent).toBe('No session available.');
  });

  it('renders Source files section with count-pill, primary path, and subagents details', async () => {
    const sid = (fixture as unknown as Envelope).sessions.rows[0].session_id;
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
    render(<SessionModal />);
    await waitFor(() => expect(document.getElementById('msess-src')).not.toBeNull(), {
      timeout: 1000,
    });
    const srcHeader = document.querySelector('.m-sec.sec-src');
    expect(srcHeader?.querySelector('svg use')?.getAttribute('href')).toBe('/static/icons.svg#file-text');
    const src = document.getElementById('msess-src');
    const pill = src?.querySelector('.src-head .count-pill');
    expect(pill?.textContent).toBe('2 files');
    // 1 primary + 1 subagent → subagent details visible
    expect(src?.querySelectorAll('.primary-path').length).toBe(1);
    const details = src?.querySelector('details.subagents');
    expect(details).not.toBeNull();
    expect(details?.querySelectorAll('ul.paths li').length).toBe(1);
  });
});
