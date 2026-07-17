import { beforeEach, describe, expect, it, vi } from 'vitest';
import { _resetForTests, dispatch, getState } from './store';
import { SOURCE_STORAGE_KEY } from './sourcePrefs';

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

describe('store activeSource — bootstrap seeding (§5.1)', () => {
  it('defaults to claude when no stored value', () => {
    expect(getState().activeSource).toBe('claude');
  });

  it('seeds from a valid stored selection', () => {
    localStorage.setItem(SOURCE_STORAGE_KEY, 'codex');
    _resetForTests();
    expect(getState().activeSource).toBe('codex');
  });

  it('an invalid stored value seeds claude', () => {
    localStorage.setItem(SOURCE_STORAGE_KEY, 'openai');
    _resetForTests();
    expect(getState().activeSource).toBe('claude');
  });
});

describe('store activeSource — SET_ACTIVE_SOURCE', () => {
  it('updates state and persists to localStorage', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    expect(getState().activeSource).toBe('codex');
    expect(localStorage.getItem(SOURCE_STORAGE_KEY)).toBe('codex');

    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'all' });
    expect(getState().activeSource).toBe('all');
    expect(localStorage.getItem(SOURCE_STORAGE_KEY)).toBe('all');
  });

  it('setting the same value is a no-op write (identity-gated, like basket persistence)', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    const spy = vi.spyOn(Storage.prototype, 'setItem');
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    expect(spy).not.toHaveBeenCalledWith(SOURCE_STORAGE_KEY, 'codex');
    // state identity is preserved (no needless emit) on a same-value dispatch
    const before = getState();
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    expect(getState()).toBe(before);
    spy.mockRestore();
  });

  it('never reconciles against an envelope (no auto-switch machinery here)', () => {
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
    // A no-op action must not touch activeSource.
    dispatch({ type: 'CLOSE_MODAL' });
    expect(getState().activeSource).toBe('codex');
  });
});
