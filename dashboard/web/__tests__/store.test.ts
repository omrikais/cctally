import { describe, it, expect, beforeEach } from 'vitest';
import type { Envelope } from '../src/types/envelope';
import {
  getState,
  updateSnapshot,
  dispatch,
  resetSnapshotOrdering,
  subscribeStore,
  _resetForTests,
  loadInitialForTests,
} from '../src/store/store';

function mkSnap(generated_at: string, used_pct = 10): Envelope {
  return {
    envelope_version: 2,
    generated_at,
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'Apr 20–27',
      used_pct,
      five_hour_pct: null,
      dollar_per_pct: null,
      forecast_pct: null,
      forecast_verdict: 'ok',
      vs_last_week_delta: null,
    },
    current_week: null,
    forecast: null,
    trend: null,
    weekly: { rows: [] },
    monthly: { rows: [] },
    blocks:  { rows: [] },
    daily:   { rows: [], quantile_thresholds: [], peak: null },
    sessions: { total: 0, sort_key: 'started_desc', rows: [] },
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('updateSnapshot — monotonic guard', () => {
  it('accepts the first snapshot', () => {
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z'));
    expect(getState().snapshot?.generated_at).toBe('2026-04-24T10:00:00Z');
  });
  it('accepts a newer snapshot', () => {
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z', 10));
    updateSnapshot(mkSnap('2026-04-24T10:00:05Z', 20));
    expect(getState().snapshot?.header.used_pct).toBe(20);
  });
  it('drops an older snapshot (bootstrap race)', () => {
    updateSnapshot(mkSnap('2026-04-24T10:00:05Z', 20));
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z', 10));
    expect(getState().snapshot?.header.used_pct).toBe(20);
  });
  it('accepts a snapshot with identical generated_at (ties allowed)', () => {
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z', 10));
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z', 20));
    expect(getState().snapshot?.header.used_pct).toBe(20);
  });
  it('resetSnapshotOrdering re-accepts any frame after reset', () => {
    updateSnapshot(mkSnap('2026-04-24T10:00:05Z', 20));
    resetSnapshotOrdering();
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z', 10));
    expect(getState().snapshot?.header.used_pct).toBe(10);
  });
});

describe('subscribeStore', () => {
  it('fires on updateSnapshot', () => {
    let calls = 0;
    const unsub = subscribeStore(() => { calls++; });
    updateSnapshot(mkSnap('2026-04-24T10:00:00Z'));
    expect(calls).toBe(1);
    unsub();
    updateSnapshot(mkSnap('2026-04-24T10:00:05Z'));
    expect(calls).toBe(1);
  });
});

describe('dispatch — UI state', () => {
  it('OPEN_MODAL / CLOSE_MODAL', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
    expect(getState().openModal).toBe('current-week');
    dispatch({ type: 'CLOSE_MODAL' });
    expect(getState().openModal).toBe(null);
    expect(getState().openSessionId).toBe(null);
  });
  it('OPEN_MODAL kind=session carries sessionId', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: 'abc-123' });
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('abc-123');
  });
  it('SET_FILTER persists to localStorage', () => {
    dispatch({ type: 'SET_FILTER', text: 'opus' });
    expect(getState().filterText).toBe('opus');
    expect(localStorage.getItem('ccusage.dashboard.filter')).toBe('opus');
  });
  it('SET_FILTER empty clears localStorage key', () => {
    dispatch({ type: 'SET_FILTER', text: 'opus' });
    dispatch({ type: 'SET_FILTER', text: '' });
    expect(getState().filterText).toBe('');
    expect(localStorage.getItem('ccusage.dashboard.filter')).toBeNull();
  });
  it('SET_SEARCH is in-memory only', () => {
    dispatch({ type: 'SET_SEARCH', text: 'needle' });
    expect(getState().searchText).toBe('needle');
    expect(localStorage.getItem('ccusage.dashboard.search')).toBeNull();
  });
  it('SET_SORT updates current sort and does not mutate prefs.sortDefault', () => {
    const beforeDefault = getState().prefs.sortDefault;
    dispatch({ type: 'SET_SORT', key: 'cost desc' });
    expect(getState().sessionsSort).toBe('cost desc');
    expect(getState().prefs.sortDefault).toBe(beforeDefault);
  });
  it('SAVE_PREFS persists merged prefs', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { sortDefault: 'cost desc' } });
    expect(getState().prefs.sortDefault).toBe('cost desc');
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).sortDefault).toBe('cost desc');
  });
  it('SAVE_PREFS round-trips sessionsCollapsed to localStorage', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: false } });
    expect(getState().prefs.sessionsCollapsed).toBe(false);
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).sessionsCollapsed).toBe(false);
  });

  it('RESET_PREFS resets sessionsCollapsed to true', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: false } });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.sessionsCollapsed).toBe(true);
  });
  it('RESET_PREFS resets in-memory state and rewrites prefs to defaults', () => {
    dispatch({ type: 'SET_FILTER', text: 'opus' });
    dispatch({ type: 'SAVE_PREFS', patch: { sortDefault: 'cost desc' } });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().filterText).toBe('');
    expect(getState().prefs.sortDefault).toBe('started desc');
    // RESET_PREFS persists a fresh prefs object (not null) so the preserved
    // onboardingToastSeen flag survives the next page load. The filter key,
    // however, is cleared outright.
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).sortDefault).toBe('started desc');
    expect(localStorage.getItem('ccusage.dashboard.filter')).toBeNull();
  });
  it('SET_INPUT_MODE', () => {
    dispatch({ type: 'SET_INPUT_MODE', mode: 'filter' });
    expect(getState().inputMode).toBe('filter');
    dispatch({ type: 'SET_INPUT_MODE', mode: null });
    expect(getState().inputMode).toBe(null);
  });
  it('SET_FOCUS', () => {
    dispatch({ type: 'SET_FOCUS', index: 3 });
    expect(getState().focusIndex).toBe(3);
  });
});

describe('localStorage migration — ccusage.dashboard.sort retirement', () => {
  it('prefs-only: sort is deleted, prefs wins', () => {
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({ sortDefault: 'cost desc', sessionsPerPage: 100 }));
    localStorage.setItem('ccusage.dashboard.sort', 'started desc');
    const init = loadInitialForTests();
    expect(init.prefs.sortDefault).toBe('cost desc');
    expect(localStorage.getItem('ccusage.dashboard.sort')).toBeNull();
  });
  it('sort-only: migrates into prefs.sortDefault', () => {
    localStorage.setItem('ccusage.dashboard.sort', 'duration desc');
    const init = loadInitialForTests();
    expect(init.prefs.sortDefault).toBe('duration desc');
    expect(localStorage.getItem('ccusage.dashboard.sort')).toBeNull();
    expect(JSON.parse(localStorage.getItem('ccusage.dashboard.prefs')!).sortDefault).toBe('duration desc');
  });
  it('both present: prefs wins, sort is deleted', () => {
    localStorage.setItem('ccusage.dashboard.prefs', JSON.stringify({ sortDefault: 'cost desc', sessionsPerPage: 100 }));
    localStorage.setItem('ccusage.dashboard.sort', 'duration desc');
    const init = loadInitialForTests();
    expect(init.prefs.sortDefault).toBe('cost desc');
    expect(localStorage.getItem('ccusage.dashboard.sort')).toBeNull();
  });
  it('neither present: defaults', () => {
    const init = loadInitialForTests();
    expect(init.prefs.sortDefault).toBe('started desc');
    expect(init.prefs.sessionsPerPage).toBe(100);
  });
  it('neither present: sessionsCollapsed defaults to true', () => {
    const init = loadInitialForTests();
    expect(init.prefs.sessionsCollapsed).toBe(true);
  });

  it('stored prefs without sessionsCollapsed key default to true', () => {
    localStorage.setItem(
      'ccusage.dashboard.prefs',
      JSON.stringify({ sortDefault: 'cost desc', sessionsPerPage: 100 }),
    );
    const init = loadInitialForTests();
    expect(init.prefs.sessionsCollapsed).toBe(true);
    expect(init.prefs.sortDefault).toBe('cost desc');
  });
  it('neither present: blocksCollapsed defaults to true', () => {
    const init = loadInitialForTests();
    expect(init.prefs.blocksCollapsed).toBe(true);
  });

  it('neither present: dailyCollapsed defaults to true', () => {
    const init = loadInitialForTests();
    expect(init.prefs.dailyCollapsed).toBe(true);
  });

  it('stored prefs without blocksCollapsed/dailyCollapsed default to true', () => {
    localStorage.setItem(
      'ccusage.dashboard.prefs',
      JSON.stringify({ sortDefault: 'cost desc', sessionsPerPage: 100, sessionsCollapsed: false }),
    );
    const init = loadInitialForTests();
    expect(init.prefs.blocksCollapsed).toBe(true);
    expect(init.prefs.dailyCollapsed).toBe(true);
    // Existing prefs preserved.
    expect(init.prefs.sortDefault).toBe('cost desc');
    expect(init.prefs.sessionsCollapsed).toBe(false);
  });
});

describe('SAVE_PREFS — blocks/daily collapse', () => {
  it('SAVE_PREFS round-trips blocksCollapsed to localStorage', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { blocksCollapsed: false } });
    expect(getState().prefs.blocksCollapsed).toBe(false);
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).blocksCollapsed).toBe(false);
  });

  it('SAVE_PREFS round-trips dailyCollapsed to localStorage', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { dailyCollapsed: false } });
    expect(getState().prefs.dailyCollapsed).toBe(false);
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).dailyCollapsed).toBe(false);
  });

  it('RESET_PREFS resets blocksCollapsed/dailyCollapsed to true', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { blocksCollapsed: false, dailyCollapsed: false } });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.blocksCollapsed).toBe(true);
    expect(getState().prefs.dailyCollapsed).toBe(true);
  });
});

// Shared default for INGEST_SNAPSHOT_ALERTS dispatches whose tests
// don't care about the alertsSettings payload. Values match the Python
// validator's defaults (`enabled=False`, `[90, 95]` for both axes).
const DEFAULT_ALERTS_SETTINGS = {
  enabled: false,
  weekly_thresholds: [90, 95],
  five_hour_thresholds: [90, 95],
};

describe('alerts store (T8)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('seenAlertIds defaults to empty Set', () => {
    expect(getState().seenAlertIds.size).toBe(0);
  });

  it('alerts defaults to empty array', () => {
    expect(getState().alerts).toEqual([]);
  });

  it('alertsConfig defaults match the Python source-of-truth (disabled, [90,95]/[90,95])', () => {
    // Mirrors bin/cctally::_validate_alerts_config defaults:
    //   enabled = block.get("enabled", False)
    //   weekly_thresholds  default [90, 95]
    //   five_hour_thresholds default [90, 95]
    // The store hardcoded default must match so a brand-new user with
    // no `alerts.*` config keys sees a UI consistent with the server
    // until the first SSE tick replaces this slice via
    // INGEST_SNAPSHOT_ALERTS.
    expect(getState().alertsConfig).toEqual({
      enabled: false,
      weekly_thresholds: [90, 95],
      five_hour_thresholds: [90, 95],
    });
  });

  it('INGEST_SNAPSHOT_ALERTS replaces alertsConfig wholesale from envelope', () => {
    // C1 regression: prior to this fix, the reducer ignored
    // `alertsSettings` and `state.alertsConfig` was frozen at the
    // hardcoded default forever. After the fix the envelope is the
    // source of truth — a server-side flip flows through here on the
    // very next tick.
    const incoming = {
      enabled: true,
      weekly_thresholds: [80, 90],
      five_hour_thresholds: [85],
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: incoming,
      isFirstTick: true,
    });
    expect(getState().alertsConfig).toEqual(incoming);

    // And on subsequent (non-cold-start) ticks too — server-side
    // cross-tab updates must propagate without waiting for a reload.
    const next = {
      enabled: false,
      weekly_thresholds: [50, 75, 99],
      five_hour_thresholds: [60],
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [],
      alertsSettings: next,
      isFirstTick: false,
    });
    expect(getState().alertsConfig).toEqual(next);
  });

  it('alertsCollapsed default false', () => {
    expect(getState().prefs.alertsCollapsed).toBe(false);
  });

  it('SAVE_PREFS persists alertsCollapsed', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { alertsCollapsed: true } });
    expect(getState().prefs.alertsCollapsed).toBe(true);
    const raw = localStorage.getItem('ccusage.dashboard.prefs');
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).alertsCollapsed).toBe(true);
  });

  it('RESET_PREFS resets alertsCollapsed to false', () => {
    dispatch({ type: 'SAVE_PREFS', patch: { alertsCollapsed: true } });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.alertsCollapsed).toBe(false);
  });

  it('toast variant: SHOW_ALERT_TOAST sets {kind:"alert", payload}', () => {
    const alert = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T14:32:11Z',
      alerted_at: '2026-04-29T14:32:11Z',
      context: {},
    };
    dispatch({ type: 'SHOW_ALERT_TOAST', alert });
    expect(getState().toast).toEqual({ kind: 'alert', payload: alert });
  });

  it('toast variant: SHOW_STATUS_TOAST sets {kind:"status", text}', () => {
    dispatch({ type: 'SHOW_STATUS_TOAST', text: 'hello' });
    expect(getState().toast).toEqual({ kind: 'status', text: 'hello' });
  });

  it('HIDE_TOAST clears either kind', () => {
    dispatch({ type: 'SHOW_STATUS_TOAST', text: 'x' });
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toBeNull();

    dispatch({
      type: 'SHOW_ALERT_TOAST',
      alert: {
        id: 'weekly:2026-04-27:90',
        axis: 'weekly',
        threshold: 90,
        crossed_at: '2026-04-29T14:32:11Z',
        alerted_at: '2026-04-29T14:32:11Z',
        context: {},
      },
    });
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toBeNull();
  });

  it('INGEST_SNAPSHOT_ALERTS cold-start: unions seen ids without surfacing toast', () => {
    const alerts = [
      {
        id: 'weekly:2026-04-27:90',
        axis: 'weekly' as const,
        threshold: 90,
        crossed_at: '2026-04-29T14:32:11Z',
        alerted_at: '2026-04-29T14:32:11Z',
        context: {},
      },
      {
        id: 'weekly:2026-04-27:75',
        axis: 'weekly' as const,
        threshold: 75,
        crossed_at: '2026-04-29T10:00:00Z',
        alerted_at: '2026-04-29T10:00:00Z',
        context: {},
      },
    ];
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts,
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    expect(getState().alerts).toEqual(alerts);
    expect(getState().seenAlertIds.has('weekly:2026-04-27:90')).toBe(true);
    expect(getState().seenAlertIds.has('weekly:2026-04-27:75')).toBe(true);
    // Cold-start MUST NOT surface a toast.
    expect(getState().toast).toBeNull();
  });

  it('INGEST_SNAPSHOT_ALERTS steady-state: surfaces first unseen as alert toast', () => {
    const initialAlert = {
      id: 'weekly:2026-04-27:75',
      axis: 'weekly' as const,
      threshold: 75,
      crossed_at: '2026-04-29T10:00:00Z',
      alerted_at: '2026-04-29T10:00:00Z',
      context: {},
    };
    // Cold-start with one alert; nothing surfaces.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [initialAlert],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    expect(getState().toast).toBeNull();

    // Subsequent tick adds a NEW alert; it should fire as alert toast.
    const fresh = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T14:32:11Z',
      alerted_at: '2026-04-29T14:32:11Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [fresh, initialAlert],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast).toEqual({ kind: 'alert', payload: fresh });
    expect(getState().seenAlertIds.has(fresh.id)).toBe(true);
  });

  it('INGEST_SNAPSHOT_ALERTS steady-state with multiple fresh alerts surfaces head + queues the rest; all marked seen this tick', () => {
    // Spec: a tick that crosses multiple thresholds simultaneously
    // (e.g. 88→96 jumping both 90 and 95) MUST surface every fresh
    // alert. Prior behavior surfaced only the first and left the
    // rest "unseen for next tick" — under `--no-sync` (no further
    // ticks), that next tick never came and the surplus alerts were
    // buried. The queue model marks ALL fresh ids seen this tick AND
    // pushes the tail onto alertToastQueue, drained one-at-a-time by
    // HIDE_TOAST so each gets its own popup.
    const seedAlert = {
      id: 'weekly:2026-04-27:75',
      axis: 'weekly' as const,
      threshold: 75,
      crossed_at: '2026-04-29T10:00:00Z',
      alerted_at: '2026-04-29T10:00:00Z',
      context: {},
    };
    // Cold-start with one alert; populates seenAlertIds without toast.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [seedAlert],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    expect(getState().toast).toBeNull();
    expect(getState().alertToastQueue).toEqual([]);

    const fresh90 = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T14:32:00Z',
      alerted_at: '2026-04-29T14:32:00Z',
      context: {},
    };
    const fresh95 = {
      id: 'weekly:2026-04-27:95',
      axis: 'weekly' as const,
      threshold: 95,
      crossed_at: '2026-04-29T14:32:01Z',
      alerted_at: '2026-04-29T14:32:01Z',
      context: {},
    };
    // Multi-threshold-jump tick: both 90 and 95 land on the same
    // snapshot. Reducer surfaces fresh90 as the toast, queues fresh95,
    // and marks BOTH seen this tick.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [fresh90, fresh95, seedAlert],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast).toEqual({ kind: 'alert', payload: fresh90 });
    expect(getState().alertToastQueue).toEqual([fresh95]);
    expect(getState().seenAlertIds.has(fresh90.id)).toBe(true);
    expect(getState().seenAlertIds.has(fresh95.id)).toBe(true);

    // HIDE_TOAST promotes the queued fresh95 to the toast slot.
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toEqual({ kind: 'alert', payload: fresh95 });
    expect(getState().alertToastQueue).toEqual([]);

    // HIDE_TOAST again with empty queue clears the toast outright.
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toBeNull();
  });

  it('INGEST_SNAPSHOT_ALERTS multi-fresh on empty toast: head surfaces, tail queued, all seen', () => {
    const a = {
      id: 'weekly:2026-04-27:80',
      axis: 'weekly' as const,
      threshold: 80,
      crossed_at: '2026-04-29T12:00:00Z',
      alerted_at: '2026-04-29T12:00:00Z',
      context: {},
    };
    const b = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T12:00:01Z',
      alerted_at: '2026-04-29T12:00:01Z',
      context: {},
    };
    // No prior cold-start: seenAlertIds empty, no toast.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [a, b],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast).toEqual({ kind: 'alert', payload: a });
    expect(getState().alertToastQueue).toEqual([b]);
    expect(getState().seenAlertIds.has(a.id)).toBe(true);
    expect(getState().seenAlertIds.has(b.id)).toBe(true);
  });

  it('INGEST_SNAPSHOT_ALERTS multi-fresh while alert toast already showing: queue grows by every new alert', () => {
    const existing = {
      id: 'weekly:2026-04-27:70',
      axis: 'weekly' as const,
      threshold: 70,
      crossed_at: '2026-04-29T11:00:00Z',
      alerted_at: '2026-04-29T11:00:00Z',
      context: {},
    };
    dispatch({ type: 'SHOW_ALERT_TOAST', alert: existing });
    expect(getState().toast).toEqual({ kind: 'alert', payload: existing });

    const a = {
      id: 'weekly:2026-04-27:80',
      axis: 'weekly' as const,
      threshold: 80,
      crossed_at: '2026-04-29T12:00:00Z',
      alerted_at: '2026-04-29T12:00:00Z',
      context: {},
    };
    const b = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T12:00:01Z',
      alerted_at: '2026-04-29T12:00:01Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [a, b],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    // Existing alert toast survives; both fresh alerts queued behind it.
    expect(getState().toast).toEqual({ kind: 'alert', payload: existing });
    expect(getState().alertToastQueue).toEqual([a, b]);
  });

  it('HIDE_TOAST drains alertToastQueue head when current toast is alert', () => {
    const a1 = {
      id: 'weekly:2026-04-27:80',
      axis: 'weekly' as const,
      threshold: 80,
      crossed_at: '2026-04-29T12:00:00Z',
      alerted_at: '2026-04-29T12:00:00Z',
      context: {},
    };
    const a2 = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T12:00:01Z',
      alerted_at: '2026-04-29T12:00:01Z',
      context: {},
    };
    const a3 = {
      id: 'weekly:2026-04-27:95',
      axis: 'weekly' as const,
      threshold: 95,
      crossed_at: '2026-04-29T12:00:02Z',
      alerted_at: '2026-04-29T12:00:02Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [a1, a2, a3],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast).toEqual({ kind: 'alert', payload: a1 });
    expect(getState().alertToastQueue).toEqual([a2, a3]);

    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toEqual({ kind: 'alert', payload: a2 });
    expect(getState().alertToastQueue).toEqual([a3]);
  });

  it('HIDE_TOAST clears toast and leaves empty queue alone (drain-to-empty)', () => {
    const a = {
      id: 'weekly:2026-04-27:80',
      axis: 'weekly' as const,
      threshold: 80,
      crossed_at: '2026-04-29T12:00:00Z',
      alerted_at: '2026-04-29T12:00:00Z',
      context: {},
    };
    dispatch({ type: 'SHOW_ALERT_TOAST', alert: a });
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toBeNull();
    expect(getState().alertToastQueue).toEqual([]);
  });

  it('Cold-start clears stale alertToastQueue', () => {
    // Simulate a queued state surviving a reconnect: seed via a
    // steady-state dispatch (no prior cold-start), then a fresh
    // cold-start tick should wipe the queue.
    const a = {
      id: 'weekly:2026-04-27:80',
      axis: 'weekly' as const,
      threshold: 80,
      crossed_at: '2026-04-29T12:00:00Z',
      alerted_at: '2026-04-29T12:00:00Z',
      context: {},
    };
    const b = {
      id: 'weekly:2026-04-27:90',
      axis: 'weekly' as const,
      threshold: 90,
      crossed_at: '2026-04-29T12:00:01Z',
      alerted_at: '2026-04-29T12:00:01Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [a, b],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().alertToastQueue).toEqual([b]);

    // Cold-start with a different alerts list: queue resets, toast
    // unchanged here because cold-start doesn't surface (it just
    // populates seenAlertIds).
    const c = {
      id: 'weekly:2026-04-27:95',
      axis: 'weekly' as const,
      threshold: 95,
      crossed_at: '2026-04-29T12:00:02Z',
      alerted_at: '2026-04-29T12:00:02Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [c],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    expect(getState().alertToastQueue).toEqual([]);
    expect(getState().seenAlertIds.has(c.id)).toBe(true);
  });

  it('HIDE_TOAST on a status toast does NOT consume from alertToastQueue', () => {
    // Defensive: status-toast dismissal must not promote a queued alert
    // (the queue is alert-only state). This shouldn't normally happen
    // — INGEST_SNAPSHOT_ALERTS preempts status toasts when there's
    // fresh alerts — but the reducer must be safe regardless.
    const a = {
      id: 'weekly:2026-04-27:80',
      axis: 'weekly' as const,
      threshold: 80,
      crossed_at: '2026-04-29T12:00:00Z',
      alerted_at: '2026-04-29T12:00:00Z',
      context: {},
    };
    // Manually wedge a queue + status toast (only achievable via the
    // test entry point; the reducer wouldn't normally produce this
    // arrangement). We dispatch a steady-state INGEST then SHOW_STATUS
    // to overwrite the alert toast — alertToastQueue survives.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [
        a,
        {
          id: 'weekly:2026-04-27:90',
          axis: 'weekly' as const,
          threshold: 90,
          crossed_at: '2026-04-29T12:00:01Z',
          alerted_at: '2026-04-29T12:00:01Z',
          context: {},
        },
      ],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().alertToastQueue.length).toBe(1);
    dispatch({ type: 'SHOW_STATUS_TOAST', text: 'sync ok' });
    expect(getState().toast).toEqual({ kind: 'status', text: 'sync ok' });
    expect(getState().alertToastQueue.length).toBe(1);

    // Hide the status toast; queue is preserved (defensive — status
    // dismissal must not touch alert state).
    dispatch({ type: 'HIDE_TOAST' });
    expect(getState().toast).toBeNull();
    expect(getState().alertToastQueue.length).toBe(1);
  });

  it('INGEST_SNAPSHOT_ALERTS steady-state with no fresh alert preserves existing toast', () => {
    const a = {
      id: 'weekly:2026-04-27:75',
      axis: 'weekly' as const,
      threshold: 75,
      crossed_at: '2026-04-29T10:00:00Z',
      alerted_at: '2026-04-29T10:00:00Z',
      context: {},
    };
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [a],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: true,
    });
    dispatch({ type: 'SHOW_STATUS_TOAST', text: 'x' });
    // Re-ingest the same alert list (no fresh ids); status toast stays.
    dispatch({
      type: 'INGEST_SNAPSHOT_ALERTS',
      alerts: [a],
      alertsSettings: DEFAULT_ALERTS_SETTINGS,
      isFirstTick: false,
    });
    expect(getState().toast).toEqual({ kind: 'status', text: 'x' });
  });
});

describe('OPEN_MODAL kind=daily', () => {
  beforeEach(() => {
    _resetForTests();
  });

  it('OPEN_MODAL with kind=daily and dailyDate sets both slices', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-22' });
    const s = getState();
    expect(s.openModal).toBe('daily');
    expect(s.openDailyDate).toBe('2026-04-22');
  });

  it('OPEN_MODAL with kind=daily and no dailyDate leaves openDailyDate null', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily' });
    const s = getState();
    expect(s.openModal).toBe('daily');
    expect(s.openDailyDate).toBeNull();
  });

  it('CLOSE_MODAL clears openDailyDate alongside the other slices', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-22' });
    dispatch({ type: 'CLOSE_MODAL' });
    const s = getState();
    expect(s.openModal).toBeNull();
    expect(s.openDailyDate).toBeNull();
    expect(s.openSessionId).toBeNull();
    expect(s.openBlockStartAt).toBeNull();
  });

  it('switching from another modal to daily clears its bound id', () => {
    dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: 'abc-123' });
    dispatch({ type: 'OPEN_MODAL', kind: 'daily', dailyDate: '2026-04-22' });
    const s = getState();
    expect(s.openModal).toBe('daily');
    expect(s.openSessionId).toBeNull();
    expect(s.openDailyDate).toBe('2026-04-22');
  });
});
