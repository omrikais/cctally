import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, defaultPrefs, dispatch, getState } from './store';

// S2 (#264) — the Day·Week·Month toggle (and its persisted `historyPeriod`
// pref + SET_HISTORY_PERIOD action) are gone. The one surviving period pref is
// the shared Weekly/Monthly table sort override:
//   - historySortOverride: SortOverride | null (default null), routed by
//     SET_TABLE_SORT { table: 'history' } and cleared by CLEAR_TABLE_SORTS,
//     coerced on load via coerceSortOverride. (The `history` name on the pref
//     + sort key is retained — renaming it is churn for no behavior change.)
const PREFS_KEY = 'ccusage.dashboard.prefs';

describe('Weekly/Monthly table sort pref (historySortOverride)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });
  afterEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('defaults historySortOverride to null', () => {
    expect(defaultPrefs().historySortOverride).toBeNull();
    expect(getState().prefs.historySortOverride).toBeNull();
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

  it('tolerates a stale retired historyPeriod key in saved prefs (never read)', () => {
    // A user upgraded from S8 may carry a `historyPeriod` key. It rides along
    // harmlessly — the Prefs type no longer declares it and nothing reads it.
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({ ...defaultPrefs(), historyPeriod: 'week' }),
    );
    _resetForTests();
    expect(getState().prefs.historySortOverride).toBeNull();
    expect((getState().prefs as unknown as Record<string, unknown>).historyPeriod).toBe('week');
  });
});
