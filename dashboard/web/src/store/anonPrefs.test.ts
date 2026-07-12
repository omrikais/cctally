import { afterEach, describe, expect, it, vi } from 'vitest';
import { ANON_MODE_KEY, loadAnonMode, saveAnonMode } from './anonPrefs';

afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
  vi.restoreAllMocks();
});

describe('anonPrefs (#281 S4)', () => {
  it('defaults ON when the key is unset', () => {
    localStorage.removeItem(ANON_MODE_KEY);
    expect(loadAnonMode()).toBe(true);
  });

  it('honors a persisted OFF', () => {
    saveAnonMode(false);
    expect(localStorage.getItem(ANON_MODE_KEY)).toBe('0');
    expect(loadAnonMode()).toBe(false);
  });

  it('honors a persisted ON', () => {
    saveAnonMode(true);
    expect(loadAnonMode()).toBe(true);
  });

  it('falls back to the in-memory default (ON) when localStorage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(loadAnonMode()).toBe(true);
  });

  it('saveAnonMode swallows a storage exception', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(() => saveAnonMode(false)).not.toThrow();
  });
});
