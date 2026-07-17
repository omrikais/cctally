import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SOURCE_STORAGE_KEY, loadActiveSource, saveActiveSource } from './sourcePrefs';

beforeEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe('sourcePrefs — loadActiveSource bootstrap precedence (§5.1)', () => {
  it('a valid stored literal wins', () => {
    for (const v of ['claude', 'codex', 'all'] as const) {
      localStorage.setItem(SOURCE_STORAGE_KEY, v);
      expect(loadActiveSource()).toBe(v);
    }
  });

  it('missing key → claude', () => {
    expect(loadActiveSource()).toBe('claude');
  });

  it('unknown / wrong-case / garbage stored value → claude', () => {
    for (const bad of ['ALL', 'openai', 'Claude', '', '{"x":1}', '["all"]', 'null']) {
      localStorage.setItem(SOURCE_STORAGE_KEY, bad);
      expect(loadActiveSource()).toBe('claude');
    }
  });

  it('a throwing localStorage.getItem → claude (no crash)', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('private mode');
      },
      setItem: () => {},
    });
    expect(loadActiveSource()).toBe('claude');
  });
});

describe('sourcePrefs — saveActiveSource', () => {
  it('round-trips through load', () => {
    saveActiveSource('codex');
    expect(loadActiveSource()).toBe('codex');
    saveActiveSource('all');
    expect(loadActiveSource()).toBe('all');
  });

  it('persists a BARE literal (not JSON-encoded)', () => {
    saveActiveSource('codex');
    expect(localStorage.getItem(SOURCE_STORAGE_KEY)).toBe('codex');
  });

  it('a throwing localStorage.setItem is swallowed (no crash)', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => {
        throw new Error('quota exceeded');
      },
    });
    expect(() => saveActiveSource('all')).not.toThrow();
  });
});
