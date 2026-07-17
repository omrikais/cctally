import { renderHook, act } from '@testing-library/react';
import { afterEach, describe, it, expect, vi } from 'vitest';
import { stubResponsiveMedia } from '../test-utils/mobileMedia';
import { BENTO_BREAKPOINT_PX, BOARD_WIDE_PX } from '../lib/breakpoints';
import { useBoardMode } from './useBoardMode';

// Resolve the two queries the hook reads against a synthetic width.
const at = (w: number) => (q: string) => {
  if (q.includes(`${BOARD_WIDE_PX}px`)) return w >= BOARD_WIDE_PX;
  if (q.includes(`${BENTO_BREAKPOINT_PX}px`)) return w >= BENTO_BREAKPOINT_PX;
  return false;
};

describe('useBoardMode', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('resolves stack / intermediate / bento from the matched queries', () => {
    const c1 = stubResponsiveMedia(at(500));
    expect(renderHook(() => useBoardMode()).result.current).toBe('stack');
    vi.unstubAllGlobals();
    stubResponsiveMedia(at(1000));
    expect(renderHook(() => useBoardMode()).result.current).toBe('intermediate');
    vi.unstubAllGlobals();
    stubResponsiveMedia(at(1500));
    expect(renderHook(() => useBoardMode()).result.current).toBe('bento');
    void c1;
  });

  it('updates on a viewport change', () => {
    const ctrl = stubResponsiveMedia(at(1000));
    const { result } = renderHook(() => useBoardMode());
    expect(result.current).toBe('intermediate');
    act(() => ctrl.set(at(1500)));
    expect(result.current).toBe('bento');
  });

  it('defaults to bento when matchMedia is absent', () => {
    vi.stubGlobal('matchMedia', undefined);
    expect(renderHook(() => useBoardMode()).result.current).toBe('bento');
  });
});
