import { describe, expect, it, beforeEach } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';
import { EMPTY_FILTERS } from '../types/conversation';
import { clearRailPrefs } from './conversationRailPrefs';

// #217 S4 / I-2.2 — clear the persisted railPrefs blob before each reset so a
// prior test's filter edit can't bleed into loadInitial.
beforeEach(() => { clearRailPrefs(); _resetForTests(); });

describe('conversationFilters', () => {
  it('starts empty and closed', () => {
    expect(getState().conversationFilters).toEqual(EMPTY_FILTERS);
    expect(getState().convFiltersOpen).toBe(false);
  });
  it('SET merges a partial patch', () => {
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { costMin: 5, projects: ['p'] } });
    expect(getState().conversationFilters.costMin).toBe(5);
    expect(getState().conversationFilters.projects).toEqual(['p']);
  });
  it('CLEAR resets to empty', () => {
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 3 } });
    dispatch({ type: 'CLEAR_CONVERSATION_FILTERS' });
    expect(getState().conversationFilters).toEqual(EMPTY_FILTERS);
  });
  it('TOGGLE_CONV_FILTERS flips open state', () => {
    dispatch({ type: 'TOGGLE_CONV_FILTERS' });
    expect(getState().convFiltersOpen).toBe(true);
  });
  it('SET_CONV_FILTERS_OPEN sets the open flag explicitly', () => {
    dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: true });
    expect(getState().convFiltersOpen).toBe(true);
    dispatch({ type: 'SET_CONV_FILTERS_OPEN', open: false });
    expect(getState().convFiltersOpen).toBe(false);
  });
});
