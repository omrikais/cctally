import { describe, expect, it, beforeEach } from 'vitest';
import { _resetForTests, dispatch, getState, loadInitialForTests } from './store';
import { EMPTY_FILTERS } from '../types/conversation';
import { RAIL_PREFS_KEY, clearRailPrefs, loadRailPrefs } from './conversationRailPrefs';

beforeEach(() => {
  clearRailPrefs();
  _resetForTests();
});

describe('conversationRailSort + railPrefs persistence (#217 S4 / I-2.2)', () => {
  it('defaults to recent sort and empty filters', () => {
    expect(getState().conversationRailSort).toBe('recent');
    expect(getState().conversationFilters).toEqual(EMPTY_FILTERS);
  });

  it('SET_CONVERSATION_RAIL_SORT updates state and persists the blob', () => {
    dispatch({ type: 'SET_CONVERSATION_RAIL_SORT', sort: 'cost' });
    expect(getState().conversationRailSort).toBe('cost');
    const saved = loadRailPrefs();
    expect(saved.sort).toBe('cost');
  });

  it('a filter SET persists the blob (filters + sort together)', () => {
    dispatch({ type: 'SET_CONVERSATION_RAIL_SORT', sort: 'messages' });
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { costMin: 5 } });
    const saved = loadRailPrefs();
    expect(saved.filters.costMin).toBe(5);
    expect(saved.sort).toBe('messages');
  });

  it('a filter CLEAR persists EMPTY_FILTERS', () => {
    dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: 3 } });
    dispatch({ type: 'CLEAR_CONVERSATION_FILTERS' });
    const saved = loadRailPrefs();
    expect(saved.filters).toEqual(EMPTY_FILTERS);
  });

  it('loadInitial seeds filters AND sort from a pre-seeded blob', () => {
    localStorage.setItem(RAIL_PREFS_KEY, JSON.stringify({
      filters: { ...EMPTY_FILTERS, projects: ['proj-a'], costMin: 1.5 },
      sort: 'project',
    }));
    const s = loadInitialForTests();
    expect(s.conversationRailSort).toBe('project');
    expect(s.conversationFilters.projects).toEqual(['proj-a']);
    expect(s.conversationFilters.costMin).toBe(1.5);
  });

  it('a corrupt blob falls back to EMPTY_FILTERS + recent', () => {
    localStorage.setItem(RAIL_PREFS_KEY, '{not valid json');
    const s = loadInitialForTests();
    expect(s.conversationRailSort).toBe('recent');
    expect(s.conversationFilters).toEqual(EMPTY_FILTERS);
  });

  it('an unknown sort in the blob falls back to recent', () => {
    localStorage.setItem(RAIL_PREFS_KEY, JSON.stringify({
      filters: EMPTY_FILTERS, sort: 'bogus',
    }));
    const s = loadInitialForTests();
    expect(s.conversationRailSort).toBe('recent');
  });
});
