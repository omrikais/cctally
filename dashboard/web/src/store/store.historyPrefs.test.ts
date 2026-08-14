import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { _resetForTests, defaultPrefs, dispatch, getState } from './store';

// S2 (#264) — the Day·Week·Month toggle (and its persisted `historyPeriod`
// pref + SET_HISTORY_PERIOD action) are gone.
//
// #556 S2 §5.3 — the surviving sort override is now PERIOD-KIND-SCOPED, because
// weekly and monthly expose different columns: weekly has `Used %` and `$/1%`,
// monthly has neither. One shared override meant a sort chosen on the weekly
// table silently applied to a monthly table that has no such column.
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

describe('#556 S2 §5.3 — period-kind-scoped sort overrides', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
  });
  afterEach(() => {
    localStorage.clear();
    _resetForTests();
  });

  it('defaults both period kinds to null', () => {
    expect(defaultPrefs().historySortOverrides).toEqual({ week: null, month: null });
  });

  it('keeps a weekly sort out of the monthly table', () => {
    dispatch({
      type: 'SET_TABLE_SORT', table: 'history', periodKind: 'week',
      override: { column: 'used_pct', direction: 'desc' },
    });
    expect(getState().prefs.historySortOverrides.week).toEqual({
      column: 'used_pct', direction: 'desc',
    });
    // `used_pct` is not a monthly column at all.
    expect(getState().prefs.historySortOverrides.month).toBeNull();
  });

  it('CLEAR_TABLE_SORTS clears both kinds', () => {
    dispatch({
      type: 'SET_TABLE_SORT', table: 'history', periodKind: 'week',
      override: { column: 'cost_usd', direction: 'asc' },
    });
    dispatch({
      type: 'SET_TABLE_SORT', table: 'history', periodKind: 'month',
      override: { column: 'cost_usd', direction: 'desc' },
    });
    dispatch({ type: 'CLEAR_TABLE_SORTS' });
    expect(getState().prefs.historySortOverrides).toEqual({ week: null, month: null });
  });

  it('RESETS a legacy shared override rather than guessing which table it was', () => {
    // The persisted `historySortOverride` carries no provenance: nothing in it
    // records whether the user chose it on the weekly table or the monthly one.
    // Adopting it into either would apply a sort the user never asked that
    // table for, so it is dropped.
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        ...defaultPrefs(),
        historySortOverride: { column: 'cost_usd', direction: 'asc' },
      }),
    );
    _resetForTests();
    expect(getState().prefs.historySortOverrides).toEqual({ week: null, month: null });
  });

  it('coerces a garbage persisted scoped override to null', () => {
    localStorage.setItem(
      PREFS_KEY,
      JSON.stringify({
        ...defaultPrefs(),
        historySortOverrides: { week: { column: 42 }, month: 'nope' },
      }),
    );
    _resetForTests();
    expect(getState().prefs.historySortOverrides).toEqual({ week: null, month: null });
  });
});

