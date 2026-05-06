import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useIsMobile } from '../src/hooks/useIsMobile';
import { MOBILE_MEDIA_QUERY } from '../src/lib/breakpoints';

interface FakeMQL {
  matches: boolean;
  media: string;
  listeners: ((e: { matches: boolean }) => void)[];
  addEventListener: (type: 'change', listener: (e: { matches: boolean }) => void) => void;
  removeEventListener: (type: 'change', listener: (e: { matches: boolean }) => void) => void;
  fire: (matches: boolean) => void;
}

function makeFakeMQL(initial: boolean): FakeMQL {
  const mql: FakeMQL = {
    matches: initial,
    media: MOBILE_MEDIA_QUERY,
    listeners: [],
    addEventListener(_type, listener) { this.listeners.push(listener); },
    removeEventListener(_type, listener) {
      this.listeners = this.listeners.filter((l) => l !== listener);
    },
    fire(matches) {
      this.matches = matches;
      this.listeners.forEach((l) => l({ matches }));
    },
  };
  return mql;
}

describe('useIsMobile', () => {
  let fake: FakeMQL;
  let original: typeof window.matchMedia;

  beforeEach(() => {
    fake = makeFakeMQL(false);
    original = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((q: string) => {
      if (q !== MOBILE_MEDIA_QUERY) throw new Error(`unexpected media query: ${q}`);
      return fake;
    }) as unknown as typeof window.matchMedia;
  });

  afterEach(() => {
    window.matchMedia = original;
  });

  it('returns the current matches value on first render', () => {
    fake.matches = true;
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it('updates when the media query toggles', () => {
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
    act(() => fake.fire(true));
    expect(result.current).toBe(true);
    act(() => fake.fire(false));
    expect(result.current).toBe(false);
  });

  it('removes its change listener on unmount', () => {
    const { unmount } = renderHook(() => useIsMobile());
    expect(fake.listeners.length).toBe(1);
    unmount();
    expect(fake.listeners.length).toBe(0);
  });
});
