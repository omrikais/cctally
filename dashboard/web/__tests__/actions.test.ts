import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  openMostRecentSessionModal,
  stepMatch,
  tryQuit,
  QUIT_TOAST_MS,
  QUIT_TOAST_MESSAGE,
} from '../src/store/actions';
import {
  getState,
  dispatch,
  updateSnapshot,
  _resetForTests,
} from '../src/store/store';
import type { Envelope, SessionRow } from '../src/types/envelope';

function mkRow(partial: Partial<SessionRow>): SessionRow {
  return {
    session_id: 'x',
    started_utc: '2026-04-24T10:00:00Z',
    duration_min: 10,
    model: 'sonnet',
    project: 'repo',
    cost_usd: 1.0,
    ...partial,
  };
}

function mkEnvelope(rows: SessionRow[]): Envelope {
  return {
    envelope_version: 2,
    generated_at: '2026-04-24T10:00:00Z',
    last_sync_at: null,
    sync_age_s: null,
    last_sync_error: null,
    header: {
      week_label: 'Apr 20–27',
      used_pct: 0,
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
    sessions: { total: rows.length, sort_key: 'started_desc', rows },
    display: { tz: 'local', resolved_tz: 'Etc/UTC', offset_label: 'UTC', offset_seconds: 0 },
    alerts: [],
    alerts_settings: { enabled: true, weekly_thresholds: [], five_hour_thresholds: [] },
  };
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('openMostRecentSessionModal (4-key)', () => {
  it('opens the Session modal for rows[0].session_id (default sort = started desc)', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'newest', started_utc: '2026-04-24T11:00:00Z' }),
        mkRow({ session_id: 'older',  started_utc: '2026-04-24T09:00:00Z' }),
      ]),
    );
    openMostRecentSessionModal();
    expect(getState().openModal).toBe('session');
    expect(getState().openSessionId).toBe('newest');
  });

  it('no-ops when sessions.rows is empty', () => {
    updateSnapshot(mkEnvelope([]));
    openMostRecentSessionModal();
    expect(getState().openModal).toBe(null);
    expect(getState().openSessionId).toBe(null);
  });

  it('no-ops when snapshot is null', () => {
    // No updateSnapshot call — state starts with snapshot: null.
    openMostRecentSessionModal();
    expect(getState().openModal).toBe(null);
  });
});

describe('stepMatch (n / N)', () => {
  beforeEach(() => {
    // Seed 3 matches at indices [0, 1, 2] via SET_SEARCH_MATCHES directly.
    dispatch({ type: 'SET_SEARCH_MATCHES', matches: [0, 1, 2], index: 0 });
  });
  it('advances forward', () => {
    stepMatch(1);
    expect(getState().searchIndex).toBe(1);
    stepMatch(1);
    expect(getState().searchIndex).toBe(2);
  });
  it('wraps forward at the end', () => {
    dispatch({ type: 'SET_SEARCH_MATCHES', matches: [0, 1, 2], index: 2 });
    stepMatch(1);
    expect(getState().searchIndex).toBe(0);
  });
  it('wraps backward at the start', () => {
    dispatch({ type: 'SET_SEARCH_MATCHES', matches: [0, 1, 2], index: 0 });
    stepMatch(-1);
    expect(getState().searchIndex).toBe(2);
  });
  it('no-ops on empty matches', () => {
    dispatch({ type: 'SET_SEARCH_MATCHES', matches: [], index: -1 });
    stepMatch(1);
    expect(getState().searchIndex).toBe(-1);
  });
});

describe('tryQuit', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('calls window.close(); if tab did not close, dispatches SHOW_STATUS_TOAST after ~100ms', () => {
    // jsdom: window.closed is false by default; window.close() is a no-op
    // there, exactly matching the browser-opened-tab production case.
    const closeSpy = vi.spyOn(window, 'close').mockImplementation(() => {});
    expect(getState().toast).toBe(null);

    tryQuit();
    expect(closeSpy).toHaveBeenCalledOnce();
    // Before the delay fires, no toast yet.
    expect(getState().toast).toBe(null);

    // Advance past the deferred check. Toast should now be present.
    vi.advanceTimersByTime(QUIT_TOAST_MS + 1);
    expect(getState().toast).toEqual({ kind: 'status', text: QUIT_TOAST_MESSAGE });
  });
});
