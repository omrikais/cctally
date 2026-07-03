import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useIsDesktopBento } from './useIsDesktopBento';

function stubMatchMedia(matches: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }));
}

describe('useIsDesktopBento', () => {
  afterEach(() => vi.unstubAllGlobals());
  it('is true at >=900px', () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useIsDesktopBento());
    expect(result.current).toBe(true);
  });
  it('is false below 900px', () => {
    stubMatchMedia(false);
    const { result } = renderHook(() => useIsDesktopBento());
    expect(result.current).toBe(false);
  });
});
