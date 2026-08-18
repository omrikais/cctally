import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';
import { _resetForTests, dispatch } from '../store/store';

const mocked = vi.hoisted(() => ({
  env: {
    sync_age_s: 480 as number | null,
    last_sync_error: null as string | null,
    sync_failure: null as null | {
      kind: string;
      label: string;
      detail: string;
      action: string | null;
    },
    sync_activity: undefined as undefined | {
      server_epoch: string;
      rebuilding: boolean;
      requested_id: number;
      started_id: number;
      settled_id: number;
      settled_status: 'ok' | 'failed' | null;
      settled_warnings: Array<{ code: string }>;
    },
  },
  disconnected: false,
}));

function activity(over: { rebuilding?: boolean } = {}) {
  return {
    server_epoch: 'a1b2c3d4e5f60718',
    rebuilding: false,
    requested_id: 0,
    started_id: 0,
    settled_id: 0,
    settled_status: null as 'ok' | 'failed' | null,
    settled_warnings: [] as Array<{ code: string }>,
    ...over,
  };
}

vi.mock('../hooks/useSnapshot', () => ({ useSnapshot: () => mocked.env }));
vi.mock('../hooks/useConnectionStatus', () => ({
  useConnectionStatus: () => ({ disconnected: mocked.disconnected }),
}));

import { SyncChip } from './SyncChip';

describe('SyncChip freshness (SYNC-1)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    mocked.env = {
      sync_age_s: 480,
      last_sync_error: null,
      sync_failure: null,
      sync_activity: undefined,
    };
    mocked.disconnected = false;
  });
  it('humanizes the age and tags the aging bucket', () => {
    const { container } = render(<SyncChip />);
    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toContain('8m ago');
    expect(span.className).toContain('sync-chip--aging');
  });

  it('renders an actionable server cache-corruption state', () => {
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'raw path must not render',
      sync_failure: {
        kind: 'cache_corruption',
        label: '⚠ cache recovery needed',
        detail: 'The server cache database could not be read safely.',
        action: 'Run cctally cache-sync --rebuild.',
      },
      sync_activity: undefined,
    };

    const { container } = render(<SyncChip />);
    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toBe('⚠ cache recovery needed');
    expect(span.getAttribute('title')).toContain('cctally cache-sync --rebuild');
    expect(span.getAttribute('title')).not.toContain('raw path');
    expect(span.className).toContain('sync-error');
  });

  it('renders an actionable server stats-corruption state without raw detail', () => {
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'raw /private/stats.db path must not render',
      sync_failure: {
        kind: 'stats_corruption',
        label: '⚠ stats recovery needed',
        detail: 'The dashboard statistics database could not be read safely.',
        action: 'cctally db repair --db stats --yes',
      },
      sync_activity: undefined,
    };

    const { container } = render(<SyncChip />);
    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toBe('⚠ stats recovery needed');
    expect(span.getAttribute('title')).toContain(
      'cctally db repair --db stats --yes',
    );
    expect(span.getAttribute('title')).not.toContain('/private/stats.db');
    expect(span.getAttribute('aria-label')).not.toContain('/private/stats.db');
    expect(span.className).toContain('sync-error');
  });

  it('distinguishes active server maintenance from a client disconnect', () => {
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'maintenance raw detail',
      sync_failure: {
        kind: 'maintenance_active',
        label: 'cache repair in progress',
        detail: 'Another cctally process is repairing the server cache.',
        action: null,
      },
      sync_activity: undefined,
    };
    const active = render(<SyncChip />);
    expect(active.container.querySelector('#sync-chip')!.textContent)
      .toBe('cache repair in progress');
    active.unmount();

    mocked.disconnected = true;
    const disconnected = render(<SyncChip />);
    const disconnectedChip = disconnected.container.querySelector('#sync-chip')!;
    expect(disconnectedChip.textContent).toBe('disconnected');
    expect(disconnectedChip.className).toContain('sync-error');
  });

  it('labels a failed manual POST as a client sync-request failure', () => {
    dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });

    const { container } = render(<SyncChip />);

    expect(container.querySelector('#sync-chip')!.textContent)
      .toBe('⚠ sync request failed');
  });
});

// #583 S2 §6.3 — chip priority, in order:
//   local in-flight POST → local outstanding queued request → error floor →
//   success flash → a standing server failure → server `rebuilding` → today's
//   branches unchanged. The browser gate inserted the server-failure rank; §6.3
//   records the measurement that moved it.
describe('SyncChip refresh contract (#583 S2)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    mocked.env = cleanEnv();
    mocked.disconnected = false;
  });

  function cleanEnv() {
    return {
      sync_age_s: 5 as number | null,
      last_sync_error: null as string | null,
      sync_failure: null,
      sync_activity: activity(),
    };
  }

  function chip() {
    return render(<SyncChip />).container.querySelector('#sync-chip')!;
  }

  it('renders queued… while a request this tab issued is still outstanding', () => {
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    const span = chip();
    expect(span.textContent).toBe('queued…');
    expect(span.className).toContain('syncing');
  });

  it('a local outstanding queued request outranks server rebuilding', () => {
    mocked.env.sync_activity = activity({ rebuilding: true });
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    // The user's own pending click is the more specific fact, so it wins over
    // the server-wide "some rebuild is running".
    expect(chip().textContent).toBe('queued…');
  });

  it('an in-flight POST outranks a queued request', () => {
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    dispatch({ type: 'SET_SYNC_BUSY', busy: true });
    expect(chip().textContent).toBe('syncing…');
  });

  it('renders a neutral refresh-busy notice distinct from failure and stale', () => {
    dispatch({ type: 'SET_SYNC_BUSY_NOTICE', untilMs: Date.now() + 3000 });
    const span = chip();
    expect(span.textContent).toBe('refresh busy');
    expect(span.className).toContain('sync-busy-notice');
    expect(span.className).not.toContain('sync-error');
    expect(span.className).not.toContain('sync-chip--stale');
    expect(span.getAttribute('aria-live')).toBe('polite');
  });

  it('a queued request outranks the error floor and the success flash', () => {
    const t = Date.now();
    dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: t + 3000 });
    dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: t + 1200 });
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    expect(chip().textContent).toBe('queued…');
  });

  it('renders syncing… while the server reports a rebuild in flight', () => {
    mocked.env.sync_activity = activity({ rebuilding: true });
    const span = chip();
    expect(span.textContent).toBe('syncing…');
    expect(span.className).toContain('syncing');
  });

  // The freshness bucket describes the DATA and the label describes the
  // ACTIVITY, so an activity label must never suppress the bucket. The browser
  // round measured the consequence of coupling them: with 42-minute-old data,
  // flipping `rebuilding` to true removed the wrapper's `:has(.sync-chip--stale)`
  // match and its red dot resolved to `content: none`. On mobile the chip text is
  // sr-only, so that dot is the only staleness channel a sighted user has, and a
  // rebuild runs 42–52% of wall-clock time.
  it('keeps the freshness bucket while the server rebuilds', () => {
    mocked.env.sync_age_s = 2520;
    mocked.env.sync_activity = activity({ rebuilding: true });
    const span = chip();
    expect(span.textContent).toBe('syncing…');
    expect(span.className).toContain('sync-chip--stale');
  });

  it('keeps the freshness bucket while a local POST is in flight', () => {
    mocked.env.sync_age_s = 2520;
    dispatch({ type: 'SET_SYNC_BUSY', busy: true });
    const span = chip();
    expect(span.textContent).toBe('syncing…');
    expect(span.className).toContain('sync-chip--stale');
  });

  it('keeps the freshness bucket while a queued request is outstanding', () => {
    mocked.env.sync_age_s = 600;
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    const span = chip();
    expect(span.textContent).toBe('queued…');
    expect(span.className).toContain('sync-chip--aging');
  });

  // The chip holds two readings of the same freshness: the bucket its own
  // 1-second tick maintains, and the bucket the current envelope implies. The
  // paint effect returns early while the error floor or the success flash is
  // active, so an envelope that arrives inside that window never reaches the
  // ticked bucket, and preferring the ticked one unconditionally renders the
  // PREVIOUS envelope's freshness until the window closes. Age only increases
  // within one sync, so the more severe of the two readings is the later one.
  it('takes the more severe bucket when the error floor held the ticked one back', () => {
    mocked.env = { ...mocked.env, sync_age_s: 60 };
    const { container } = render(<SyncChip />);
    expect(container.querySelector('#sync-chip')!.className)
      .toContain('sync-chip--fresh');

    act(() => {
      dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });
    });
    // A fresh envelope identity, so the paint effect re-runs and reaches its
    // early return with the floor still active.
    mocked.env = { ...mocked.env, sync_age_s: 2520 };
    act(() => {
      dispatch({ type: 'SET_SYNC_BUSY', busy: true });
    });

    const span = container.querySelector('#sync-chip')!;
    expect(span.textContent).toBe('syncing…');
    expect(span.className).toContain('sync-chip--stale');
    expect(span.className).not.toContain('sync-chip--fresh');
  });

  it('server rebuilding does NOT outrank the error floor', () => {
    mocked.env.sync_activity = activity({ rebuilding: true });
    dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });
    expect(chip().textContent).toBe('⚠ sync request failed');
  });

  it('server rebuilding does NOT outrank the success flash', () => {
    mocked.env.sync_activity = activity({ rebuilding: true });
    dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: Date.now() + 1200 });
    expect(chip().textContent).toBe('✓ updated');
  });

  // The browser round reversed this ordering. A dashboard with a persistently
  // failing leg changed state 26 times in 112 seconds, strictly alternating the
  // failure label and `syncing…` — a swap every 4.3 s, with `syncing…` windows as
  // short as 1.30 s, and a red spinning icon caught mid-transition. The failure
  // is the more actionable fact, and it is still true once the rebuild has
  // finished, so it now outranks it. Spec §6.3 records the change and the measurement behind it.
  function serverFailingEnv() {
    return {
      sync_age_s: null,
      last_sync_error: 'cache.db stayed locked',
      sync_failure: {
        kind: 'cache_busy',
        label: '⚠ cache database busy',
        detail: 'The dashboard could not complete sync because cache.db stayed locked.',
        action: 'cctally db checkpoint',
      },
      sync_activity: activity({ rebuilding: true }),
    };
  }

  it('a standing server failure outranks server rebuilding', () => {
    mocked.env = serverFailingEnv();
    const span = chip();
    expect(span.textContent).toBe('⚠ cache database busy');
    expect(span.className).toContain('sync-error');
    expect(span.className).not.toContain('syncing');
    expect(span.getAttribute('title')).toContain('cctally db checkpoint');
  });

  it('an older server failure without typed metadata still outranks rebuilding', () => {
    mocked.env = { ...serverFailingEnv(), sync_failure: null };
    expect(chip().textContent).toBe('⚠ server sync error');
  });

  it('the five local states still outrank a standing server failure', () => {
    mocked.env = serverFailingEnv();
    dispatch({ type: 'SET_SYNC_BUSY', busy: true });
    expect(chip().textContent).toBe('syncing…');

    _resetForTests();
    mocked.env = serverFailingEnv();
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    expect(chip().textContent).toBe('queued…');

    _resetForTests();
    mocked.env = serverFailingEnv();
    dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });
    expect(chip().textContent).toBe('⚠ sync request failed');

    _resetForTests();
    mocked.env = serverFailingEnv();
    dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: Date.now() + 1200 });
    expect(chip().textContent).toBe('✓ updated');

    _resetForTests();
    mocked.env = serverFailingEnv();
    dispatch({ type: 'SET_SYNC_BUSY_NOTICE', untilMs: Date.now() + 3000 });
    expect(chip().textContent).toBe('refresh busy');
  });

  // The same standing guard, for the same reason and with one addition: an
  // outstanding identifier is cleared ONLY by `reconcileOutstandingSync`, which
  // needs a frame to run. Every other local state that outranks the
  // disconnected paint is bounded by a timer (the error floor, the success
  // flash) or by an in-flight request (`busy`); a queued identifier is bounded
  // by nothing at all. So a tab whose server died while its request was queued
  // would render `queued…` for the life of the tab and never say it is
  // disconnected. While the stream is down the tab cannot know whether the
  // request is still pending, which is exactly the argument that gated
  // `rebuilding`.
  it('a disconnected client renders disconnected, not its outstanding request', () => {
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    mocked.disconnected = true;
    const span = chip();
    expect(span.textContent).toBe('disconnected');
    expect(span.className).toContain('sync-error');
  });

  // The chip's standing guard — "never overwrite disconnected" — outranks the
  // new branch. A `rebuilding` flag carried on the last frame this tab received
  // is stale the moment the stream drops, so honouring it would claim live
  // progress from a server the tab can no longer reach.
  it('a disconnected client never renders rebuilding progress', () => {
    mocked.env.sync_activity = activity({ rebuilding: true });
    mocked.disconnected = true;
    const span = chip();
    expect(span.textContent).toBe('disconnected');
    expect(span.className).toContain('sync-error');
  });

  it('an idle activity object leaves every existing branch untouched', () => {
    // rebuilding false + nothing outstanding must fall through to today's
    // freshness rendering, byte-for-byte.
    const span = chip();
    expect(span.textContent).toBe('synced 5s ago');
    expect(span.className).not.toContain('syncing');
  });

  it('an envelope without sync_activity reads as idle', () => {
    mocked.env.sync_activity = undefined;
    expect(chip().textContent).toBe('synced 5s ago');
  });

  // Spec acceptance criterion 8: a rebuild in flight, stale data and a failed
  // tick must be three visually DISTINCT states — distinct text and distinct
  // class, not one label doing three jobs.
  it('renders a rebuild, stale data and a failed tick as three distinct states', () => {
    mocked.env.sync_activity = activity({ rebuilding: true });
    const rebuilding = chip();
    const rebuildingText = rebuilding.textContent;
    const rebuildingClass = rebuilding.className;

    _resetForTests();
    mocked.env = {
      sync_age_s: 3600,
      last_sync_error: null,
      sync_failure: null,
      sync_activity: activity(),
    };
    const stale = chip();
    const staleText = stale.textContent;
    const staleClass = stale.className;

    _resetForTests();
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'cache.db stayed locked',
      sync_failure: {
        kind: 'cache_busy',
        label: '⚠ cache database busy',
        detail: 'The dashboard could not complete sync because cache.db stayed locked.',
        action: 'cctally db checkpoint',
      },
      sync_activity: activity(),
    };
    const failed = chip();
    const failedText = failed.textContent;
    const failedClass = failed.className;

    expect(rebuildingText).toBe('syncing…');
    expect(staleText).toBe('synced 1h ago');
    expect(failedText).toBe('⚠ cache database busy');
    expect(new Set([rebuildingText, staleText, failedText]).size).toBe(3);

    expect(rebuildingClass).toContain('syncing');
    expect(staleClass).toContain('sync-chip--stale');
    expect(failedClass).toContain('sync-error');
    expect(new Set([rebuildingClass, staleClass, failedClass]).size).toBe(3);
  });

  it('renders the cache_busy label with its action in the tooltip', () => {
    mocked.env = {
      sync_age_s: null,
      last_sync_error: 'raw sqlite text must not render',
      sync_failure: {
        kind: 'cache_busy',
        label: '⚠ cache database busy',
        detail: 'The dashboard could not complete sync because cache.db stayed locked.',
        action: 'cctally db checkpoint',
      },
      sync_activity: activity(),
    };
    const span = chip();
    expect(span.textContent).toBe('⚠ cache database busy');
    expect(span.getAttribute('title')).toContain('cctally db checkpoint');
    expect(span.getAttribute('title')).not.toContain('raw sqlite text');
  });

  // A branch announces when its text settles and then holds; it stays silent
  // when its text keeps changing on its own. The browser round found the
  // rebuilding branch announcing itself roughly every ten seconds, because that
  // branch flips on every period — and the default branch is worse, because the
  // server refreshes `last_sync_at` on every clean tick, so its age text changes
  // about once a second for as long as the tab stays open. `aria-busy` stays
  // wherever it was set: a screen-reader user can query a state without being
  // interrupted by it.
  it('announces the states that hold and stays silent on the two that keep changing', () => {
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'a1b2c3d4e5f60718' });
    const queued = chip();
    expect(queued.getAttribute('aria-live')).toBe('polite');
    expect(queued.getAttribute('aria-busy')).toBe('true');

    _resetForTests();
    dispatch({ type: 'SET_SYNC_BUSY', busy: true });
    expect(chip().getAttribute('aria-live')).toBe('polite');

    _resetForTests();
    dispatch({ type: 'SET_SYNC_ERROR_FLOOR', untilMs: Date.now() + 3000 });
    expect(chip().getAttribute('aria-live')).toBe('polite');

    _resetForTests();
    dispatch({ type: 'SET_SYNC_SUCCESS_FLASH', untilMs: Date.now() + 1200 });
    expect(chip().getAttribute('aria-live')).toBe('polite');

    _resetForTests();
    mocked.env = serverFailingEnv();
    expect(chip().getAttribute('aria-live')).toBe('polite');

    // The two settled texts of the default branch.
    _resetForTests();
    mocked.env = { ...cleanEnv(), sync_age_s: null };
    expect(chip().textContent).toBe('sync paused');
    expect(chip().getAttribute('aria-live')).toBe('polite');

    _resetForTests();
    mocked.env = cleanEnv();
    mocked.disconnected = true;
    expect(chip().textContent).toBe('disconnected');
    expect(chip().getAttribute('aria-live')).toBe('polite');
    mocked.disconnected = false;

    // The ticking age, which is the default branch's third text.
    _resetForTests();
    mocked.env = cleanEnv();
    const ticking = chip();
    expect(ticking.textContent).toBe('synced 5s ago');
    expect(ticking.getAttribute('aria-live')).toBeNull();

    _resetForTests();
    mocked.env = cleanEnv();
    mocked.env.sync_activity = activity({ rebuilding: true });
    const rebuilding = chip();
    expect(rebuilding.getAttribute('aria-live')).toBeNull();
    expect(rebuilding.getAttribute('aria-busy')).toBe('true');
  });
});
