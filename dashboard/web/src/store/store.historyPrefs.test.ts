import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, defaultPrefs, dispatch, getState } from './store';

// S8 (#254) — the History modal's two persisted prefs:
//   - historyPeriod: 'day' | 'week' | 'month' (default 'day'), set by
//     SET_HISTORY_PERIOD, coerced on load (invalid → 'day').
//   - historySortOverride: SortOverride | null (default null), routed by
//     SET_TABLE_SORT { table: 'history' } and cleared by CLEAR_TABLE_SORTS,
//     coerced on load via coerceSortOverride.
const PREFS_KEY = 'ccusage.dashboard.prefs';

describe('History modal prefs (historyPeriod + historySortOverride)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });
  afterEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('defaults historyPeriod to "day" and historySortOverride to null', () => {
    expect(defaultPrefs().historyPeriod).toBe('day');
    expect(defaultPrefs().historySortOverride).toBeNull();
    expect(getState().prefs.historyPeriod).toBe('day');
    expect(getState().prefs.historySortOverride).toBeNull();
  });

  it('SET_HISTORY_PERIOD persists the toggle', () => {
    dispatch({ type: 'SET_HISTORY_PERIOD', period: 'week' });
    expect(getState().prefs.historyPeriod).toBe('week');
    dispatch({ type: 'SET_HISTORY_PERIOD', period: 'month' });
    expect(getState().prefs.historyPeriod).toBe('month');
  });

  it('SET_HISTORY_PERIOD survives a reload (persisted to localStorage)', () => {
    dispatch({ type: 'SET_HISTORY_PERIOD', period: 'week' });
    _resetForTests(); // re-reads localStorage
    expect(getState().prefs.historyPeriod).toBe('week');
  });

  it('load-coercion: an invalid persisted historyPeriod falls back to "day"', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ ...defaultPrefs(), historyPeriod: 'decade' }),
    );
    _resetForTests();
    expect(getState().prefs.historyPeriod).toBe('day');
  });

  it('SET_TABLE_SORT table:history sets historySortOverride', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'history',
      override: { column: 'cost_usd', direction: 'asc' },
    });
    expect(getState().prefs.historySortOverride).toEqual({
      column: 'cost_usd',
      direction: 'asc',
    });
  });

  it('CLEAR_TABLE_SORTS clears history too', () => {
    dispatch({
      type: 'SET_TABLE_SORT',
      table: 'history',
      override: { column: 'cost_usd', direction: 'asc' },
    });
    expect(getState().prefs.historySortOverride).not.toBeNull();
    dispatch({ type: 'CLEAR_TABLE_SORTS' });
    expect(getState().prefs.historySortOverride).toBeNull();
  });

  it('load-coercion: a garbage persisted historySortOverride coerces to null', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ ...defaultPrefs(), historySortOverride: { column: 42 } }),
    );
    _resetForTests();
    expect(getState().prefs.historySortOverride).toBeNull();
  });
});
