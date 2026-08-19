// #583 S3 §8 — the hidden tab.
//
// A connected client costs the server a full projection and the browser a full
// parse on every tick, for as long as the tab is open, whether or not anyone is
// looking at it. A tab hidden beyond a grace period therefore disconnects the
// MAIN dashboard stream and reopens it on return.
//
// The grace is thirty seconds, which spans two of the server's fifteen-second
// keep-alive intervals and several publish periods while absorbing an ordinary
// task switch. Switching away and back inside it produces no reconnect at all;
// a longer cycle produces at most one reconnect per hidden interval.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  startSSE, closeSSE, connectionState, _resetForTests as _resetSSE,
} from './sse';
import {
  _resetForTests as _resetStore, dispatch, getState, updateSnapshot,
} from './store';
import type { Envelope } from '../types/envelope';

const GRACE_MS = 30_000;

function envelope(genAt: string, usedPct = 10, extra: object = {}): Envelope {
  return {
    envelope_version: 2, generated_at: genAt, last_sync_at: null,
    sync_age_s: null, last_sync_error: null,
    header: {
      week_label: null, used_pct: usedPct, five_hour_pct: null,
      dollar_per_pct: null, forecast_pct: null, forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null, forecast: null, trend: null,
    weekly: { rows: [] }, monthly: { rows: [] }, blocks: { rows: [] },
    daily: { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] }, projects: null,
    display: {
      tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC',
      offset_seconds: 0,
    },
    alerts: [],
    alerts_settings: {
      enabled: true, weekly_thresholds: [], five_hour_thresholds: [],
      budget_thresholds: [],
    },
    ...extra,
  } as unknown as Envelope;
}

function alertRow(id: string) {
  return {
    id,
    axis: 'weekly' as const,
    threshold: 90,
    crossed_at: '2026-04-29T14:32:11Z',
    alerted_at: '2026-04-29T14:32:11Z',
    context: { week_start_date: '2026-04-27' },
  };
}

// The toast pipeline reads the SOURCE projections, not the legacy top-level
// `alerts` array, so an alert has to travel as a Claude source row.
function withAlerts(genAt: string, ids: string[]): Envelope {
  const rows = ids.map(alertRow);
  return envelope(genAt, 10, {
    source_schema_version: 11,
    default_source: 'claude',
    source_order: ['claude', 'codex', 'all'],
    sources: {
      claude: {
        data: {
          alerts: {
            rows: rows.map((r) => ({ ...r, source: 'claude', key: r.id })),
          },
        },
      },
      codex: { data: { alerts: { rows: [] } } },
    },
  });
}

class FakeES {
  static instances: FakeES[] = [];
  closed = false;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  constructor(public url: string) { FakeES.instances.push(this); }
  addEventListener(t: string, fn: (ev: MessageEvent) => void) {
    (this.listeners[t] ||= []).push(fn);
  }
  removeEventListener() {}
  close() { this.closed = true; }
  emitUpdate(env: Envelope) {
    const ev = { data: JSON.stringify(env) } as MessageEvent;
    (this.listeners['update'] || []).forEach((fn) => fn(ev));
  }
  emitError() { this.onerror?.(); }
}

/** Every EventSource opened against the MAIN dashboard stream, in order. */
function mainStreams(): FakeES[] {
  return FakeES.instances.filter((es) => es.url === '/api/events');
}

function currentMain(): FakeES {
  const all = mainStreams();
  return all[all.length - 1];
}

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  FakeES.instances = [];
  _resetStore();
  _resetSSE();
  vi.stubGlobal('EventSource', FakeES as unknown as typeof EventSource);
  vi.stubGlobal('fetch', vi.fn());
  setVisibility('visible');
});

afterEach(() => {
  closeSSE();
  setVisibility('visible');
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('#583 S3 §8 — the hidden-tab grace period', () => {
  it('reports a deliberate hidden-tab suspension instead of connected', () => {
    startSSE();
    currentMain().emitUpdate(envelope('2026-04-20T12:00:10Z'));

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);

    expect(connectionState()).toBe('suspended');
  });

  it('reports resuming until the returning tab accepts a fresh seed', () => {
    startSSE();
    currentMain().emitUpdate(envelope('2026-04-20T12:00:10Z'));
    expect(connectionState()).toBe('connected');

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);
    setVisibility('visible');

    expect(connectionState()).toBe('resuming');
    currentMain().emitUpdate(envelope('2026-04-20T12:01:00Z'));
    expect(connectionState()).toBe('connected');
  });

  it('does not reconnect when the tab is hidden and restored inside the grace', () => {
    startSSE();
    currentMain().emitUpdate(envelope('2026-04-20T12:00:10Z'));
    expect(mainStreams()).toHaveLength(1);

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS - 1);
    setVisibility('visible');
    // Well past the grace, to prove the timer was CANCELLED rather than merely
    // not yet due.
    vi.advanceTimersByTime(GRACE_MS * 3);

    expect(mainStreams()).toHaveLength(1);
    expect(currentMain().closed).toBe(false);
  });

  it('closes only the main dashboard stream when the grace expires', () => {
    startSSE();
    // The conversation live-tail and the update-progress stream are separate
    // EventSources owned by other modules. An update running while the tab is
    // hidden must keep streaming, so this feature must not touch them.
    const liveTail = new FakeES('/api/conversation/abc/events');

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);

    expect(currentMain().closed).toBe(true);
    expect(liveTail.closed).toBe(false);
  });

  it('retains the last snapshot and the ordering state across a suspend', () => {
    const onConnect = vi.fn();
    startSSE({ onConnect });
    currentMain().emitUpdate(envelope('2026-04-20T12:00:10Z', 7));
    expect(onConnect).toHaveBeenCalledTimes(1);

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);
    setVisibility('visible');
    expect(connectionState()).toBe('resuming');

    // The snapshot survives: suspension is not a teardown.
    expect(getState().snapshot?.header.used_pct).toBe(7);
    // And so does the ordering state. `closeSSE()` calls
    // `resetSnapshotOrdering()`, which would make this OLDER frame acceptable;
    // the suspend path must not, or a stale replay would repaint the board.
    currentMain().emitUpdate(envelope('2026-04-20T12:00:05Z', 99));
    expect(getState().snapshot?.header.used_pct).toBe(7);
    expect(connectionState()).toBe('resuming');
    expect(onConnect).toHaveBeenCalledTimes(1);

    currentMain().emitUpdate(envelope('2026-04-20T12:01:00Z', 42));
    expect(connectionState()).toBe('connected');
    expect(onConnect).toHaveBeenCalledTimes(2);
  });

  it('increments the generation when the grace timer closes the stream', () => {
    startSSE();
    const suspended = currentMain();
    suspended.emitUpdate(envelope('2026-04-20T12:00:10Z', 7));

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);

    // A callback already queued from the closed EventSource must be inert for
    // the WHOLE hidden interval, not merely until a new stream opens. Bumping
    // the generation only on reopen would leave this window unguarded.
    suspended.emitUpdate(envelope('2026-04-20T12:05:00Z', 99));
    expect(getState().snapshot?.header.used_pct).toBe(7);

    // The same stale stream's error handler is equally invalid.
    suspended.emitError();
    expect(getState().snapshot?.header.used_pct).toBe(7);
  });

  it('reopens the stream ALONE on return, with no bootstrap fetch', () => {
    startSSE();
    currentMain().emitUpdate(envelope('2026-04-20T12:00:10Z', 7));

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);
    expect(mainStreams()).toHaveLength(1);

    setVisibility('visible');
    expect(mainStreams()).toHaveLength(2);
    expect(currentMain().closed).toBe(false);
    // There is no `/api/data` to repeat: the returning tab renders from the
    // hub's subscribe seed, exactly as a cold load does.
    expect(globalThis.fetch).not.toHaveBeenCalled();

    currentMain().emitUpdate(envelope('2026-04-20T12:01:00Z', 42));
    expect(getState().snapshot?.header.used_pct).toBe(42);
  });

  it('re-arms the cold-start rule so an alert from the hidden interval does not toast', () => {
    startSSE();
    currentMain().emitUpdate(withAlerts('2026-04-20T12:00:10Z', []));

    setVisibility('hidden');
    vi.advanceTimersByTime(GRACE_MS);
    setVisibility('visible');

    // The accepted consequence recorded in §8: the alert repaints in its panel
    // but raises no toast, because the client cannot tell a genuinely new
    // crossing from one it simply did not receive while suspended.
    currentMain().emitUpdate(withAlerts('2026-04-20T12:00:20Z', ['weekly:2026-04-27:90']));
    expect(getState().toast).toBeNull();
    expect(getState().seenAlertIds.has('weekly:2026-04-27:90')).toBe(true);

    // Discriminator: the NEXT unseen alert, after the re-armed tick is spent,
    // does toast. Without this the test would pass over a client that had
    // stopped toasting altogether.
    currentMain().emitUpdate(withAlerts('2026-04-20T12:00:30Z', ['weekly:2026-04-27:95']));
    expect(getState().toast?.kind).toBe('alert');
  });

  it('opens NO stream at all for a page restored into an already-hidden tab', () => {
    // A page restored into a background tab never fires `visibilitychange`,
    // because its visibility never changes. Evaluating the CURRENT visibility
    // is therefore required, and the earlier form of this rule opened the
    // stream first and suspended it after the grace — which downloaded and
    // parsed about five full envelopes at the measured 6.5-second publish
    // period, times the number of tabs the browser restored at once, to
    // display nothing. A hidden tab has nothing to display, so it opens
    // nothing and the first return to visible opens the stream.
    setVisibility('hidden');
    startSSE();
    expect(mainStreams()).toHaveLength(0);

    vi.advanceTimersByTime(GRACE_MS * 3);
    expect(mainStreams()).toHaveLength(0);

    setVisibility('visible');
    expect(mainStreams()).toHaveLength(1);
    expect(currentMain().closed).toBe(false);

    // The deferred stream is a working one, not a stub: the tab that returns
    // renders from its seed exactly as a cold load does.
    currentMain().emitUpdate(envelope('2026-04-20T12:01:00Z', 42));
    expect(getState().snapshot?.header.used_pct).toBe(42);
  });

  it('a suspend with no open stream is a no-op rather than a throw', () => {
    startSSE();
    closeSSE();
    setVisibility('hidden');
    expect(() => vi.advanceTimersByTime(GRACE_MS * 2)).not.toThrow();
    // A deliberate teardown is not undone by the tab coming back.
    setVisibility('visible');
    expect(mainStreams()).toHaveLength(1);
  });
});

// #583 S3 §8, the restart edge. B4.
describe('#583 S3 §8 — a changed server epoch outranks the ordering guard', () => {
  function withEpoch(genAt: string, epoch: string, usedPct = 10): Envelope {
    return envelope(genAt, usedPct, {
      sync_activity: {
        server_epoch: epoch,
        rebuilding: false,
        requested_id: 0,
        started_id: 0,
        settled_id: 0,
        settled_status: null,
        settled_warnings: [],
      },
    });
  }

  it('accepts the first seed after a server restart even when its clock is behind', () => {
    expect(updateSnapshot(withEpoch('2026-04-20T12:00:10Z', 'A', 7))).toBe(true);

    // The server restarted while the tab was hidden, and the new process's
    // wall clock is behind the dead one's. Two processes' clocks are not
    // comparable, so time order says nothing here.
    const accepted = updateSnapshot(withEpoch('2026-04-20T12:00:05Z', 'B', 42));

    expect(accepted).toBe(true);
    expect(getState().snapshot?.header.used_pct).toBe(42);
  });

  it('still rejects an older frame from the SAME server process', () => {
    expect(updateSnapshot(withEpoch('2026-04-20T12:00:10Z', 'A', 7))).toBe(true);
    expect(updateSnapshot(withEpoch('2026-04-20T12:00:05Z', 'A', 42))).toBe(false);
    expect(getState().snapshot?.header.used_pct).toBe(7);
  });

  it('reconciles the dead process’s outstanding sync once the seed is accepted', () => {
    updateSnapshot(withEpoch('2026-04-20T12:00:10Z', 'A', 7));
    // A rebuild was requested against the process that then died.
    dispatch({ type: 'REGISTER_SYNC_REQUEST', id: 4, epoch: 'A' });
    expect(getState().outstandingSyncId).toBe(4);

    updateSnapshot(withEpoch('2026-04-20T12:00:05Z', 'B', 42));

    // Rejecting the seed on time order left the changed epoch unreconciled,
    // so the dead identifier survived and freshness stayed stuck. Discarding
    // is NOT settling: no success flash fires for work that never ran.
    expect(getState().outstandingSyncId).toBeNull();
    expect(getState().syncEpoch).toBeNull();
  });

  it('an absent or empty server_epoch is not authoritative and does not bypass', () => {
    expect(updateSnapshot(envelope('2026-04-20T12:00:10Z', 7))).toBe(true);
    // No `sync_activity` at all: a server without the feature says nothing
    // about process identity, so the ordering guard still applies.
    expect(updateSnapshot(envelope('2026-04-20T12:00:05Z', 42))).toBe(false);
    // An empty epoch is a snapshot that never passed through `_SnapshotRef`,
    // which is equally uninformative.
    expect(updateSnapshot(withEpoch('2026-04-20T12:00:05Z', '', 42))).toBe(false);
    expect(getState().snapshot?.header.used_pct).toBe(7);
  });
});
