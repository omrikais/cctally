import { describe, it, expect, beforeEach } from 'vitest';
import {
  getState,
  dispatch,
  updateSnapshot,
  _resetForTests,
  loadInitialForTests,
} from '../src/store/store';
import type { Envelope, SessionRow } from '../src/types/envelope';

const PREFS_KEY = 'ccusage.dashboard.prefs';

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

describe('store table-sort overrides', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('defaults both override fields to null', () => {
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
  });

  it('SET_TABLE_SORT writes override and persists to localStorage', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'desc' },
    });
    expect(getState().prefs.sessionsSortOverride).toEqual({
      column: 'cost', direction: 'desc',
    });
    const persisted = JSON.parse(localStorage.getItem(PREFS_KEY) ?? '{}');
    expect(persisted.sessionsSortOverride).toEqual({
      column: 'cost', direction: 'desc',
    });
  });

  it('SET_TABLE_SORT writes Trend override independently', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'trend',
      override: { column: 'used_pct', direction: 'asc' },
    });
    expect(getState().prefs.trendSortOverride).toEqual({
      column: 'used_pct', direction: 'asc',
    });
    expect(getState().prefs.sessionsSortOverride).toBeNull();
  });

  it('SET_TABLE_SORT with override:null clears the field', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'desc' },
    });
    dispatch({ type: 'SET_TABLE_SORT', table: 'sessions', override: null });
    expect(getState().prefs.sessionsSortOverride).toBeNull();
  });

  it('CLEAR_TABLE_SORTS zeroes both override fields and persists', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'desc' },
    });
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'trend',
      override: { column: 'week', direction: 'asc' },
    });
    dispatch({ type: 'CLEAR_TABLE_SORTS' });
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
    const persisted = JSON.parse(localStorage.getItem(PREFS_KEY) ?? '{}');
    expect(persisted.trendSortOverride).toBeNull();
    expect(persisted.sessionsSortOverride).toBeNull();
  });

  it('RESET_PREFS zeroes both override fields', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'desc' },
    });
    dispatch({ type: 'RESET_PREFS' });
    expect(getState().prefs.trendSortOverride).toBeNull();
    expect(getState().prefs.sessionsSortOverride).toBeNull();
  });

  it('loadInitial coerces malformed override (bad direction) to null', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        sessionsSortOverride: { column: 'cost', direction: 'DESC' },
      }),
    );
    const init = loadInitialForTests();
    expect(init.prefs.sessionsSortOverride).toBeNull();
  });

  it('loadInitial coerces malformed override (missing column) to null', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ trendSortOverride: { direction: 'asc' } }),
    );
    const init = loadInitialForTests();
    expect(init.prefs.trendSortOverride).toBeNull();
  });

  it('loadInitial preserves a well-formed override', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        sessionsSortOverride: { column: 'project', direction: 'asc' },
      }),
    );
    const init = loadInitialForTests();
    expect(init.prefs.sessionsSortOverride).toEqual({
      column: 'project', direction: 'asc',
    });
  });

  it('loadInitial coerces array-shaped override to null', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ sessionsSortOverride: ['cost', 'desc'] }),
    );
    const init = loadInitialForTests();
    expect(init.prefs.sessionsSortOverride).toBeNull();
  });
});

describe('store table-sort search-match recompute', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  // started_desc-sorted (default): row 'b' (latest) at index 0, row 'a' at index 1.
  // 'opus' substring matches only row 'a' on the model field.
  // After SET_TABLE_SORT cost asc: row 'a' (cost 1) at index 0, row 'b' (cost 5) at index 1.
  // searchMatches must shift from [1] → [0] without any other action.
  it('SET_TABLE_SORT recomputes searchMatches when sessions sort changes', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({
          session_id: 'a', model: 'opus', project: 'repo',
          started_utc: '2026-04-24T10:00:00Z', cost_usd: 1.0,
        }),
        mkRow({
          session_id: 'b', model: 'sonnet', project: 'repo',
          started_utc: '2026-04-24T11:00:00Z', cost_usd: 5.0,
        }),
      ]),
    );
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    expect(getState().searchMatches).toEqual([1]);

    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'asc' },
    });
    expect(getState().searchMatches).toEqual([0]);
  });

  it('CLEAR_TABLE_SORTS recomputes searchMatches after sessions override is cleared', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({
          session_id: 'a', model: 'opus', project: 'repo',
          started_utc: '2026-04-24T10:00:00Z', cost_usd: 1.0,
        }),
        mkRow({
          session_id: 'b', model: 'sonnet', project: 'repo',
          started_utc: '2026-04-24T11:00:00Z', cost_usd: 5.0,
        }),
      ]),
    );
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'asc' },
    });
    dispatch({ type: 'SET_SEARCH', text: 'opus' });
    expect(getState().searchMatches).toEqual([0]);

    dispatch({ type: 'CLEAR_TABLE_SORTS' });
    expect(getState().searchMatches).toEqual([1]);
  });

  it('SET_TABLE_SORT is a no-op for searchMatches when no search is active', () => {
    updateSnapshot(
      mkEnvelope([
        mkRow({ session_id: 'a', model: 'opus', cost_usd: 1.0 }),
      ]),
    );
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'sessions',
      override: { column: 'cost', direction: 'asc' },
    });
    expect(getState().searchMatches).toEqual([]);
    expect(getState().searchIndex).toBe(-1);
  });
});
