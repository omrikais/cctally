import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useScrollLock, _resetForTests } from './useScrollLock';

const htmlOverflow = () => document.documentElement.style.overflow;
const bodyOverflow = () => document.body.style.overflow;

function resetOverflow() {
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
}

beforeEach(() => {
  _resetForTests();
  resetOverflow();
});

afterEach(() => {
  resetOverflow();
});

describe('useScrollLock', () => {
  it('locks <html>+<body> overflow on mount and restores the prior values on unmount', () => {
    // <html> is the real viewport scroller — it is the load-bearing lock.
    document.documentElement.style.overflow = 'visible';
    document.body.style.overflow = 'auto';
    const { unmount } = renderHook(() => useScrollLock(true));
    expect(htmlOverflow()).toBe('hidden');
    expect(bodyOverflow()).toBe('hidden');
    unmount();
    expect(htmlOverflow()).toBe('visible');
    expect(bodyOverflow()).toBe('auto');
  });

  it('is a no-op when inactive', () => {
    document.documentElement.style.overflow = 'scroll';
    document.body.style.overflow = 'scroll';
    const { unmount } = renderHook(() => useScrollLock(false));
    expect(htmlOverflow()).toBe('scroll');
    expect(bodyOverflow()).toBe('scroll');
    unmount();
    expect(htmlOverflow()).toBe('scroll');
  });

  it('stays locked until the LAST of two stacked locks releases (refcount)', () => {
    const a = renderHook(() => useScrollLock(true));
    const b = renderHook(() => useScrollLock(true));
    expect(htmlOverflow()).toBe('hidden');
    a.unmount();
    // one lock still held -> page stays locked
    expect(htmlOverflow()).toBe('hidden');
    b.unmount();
    // both released -> restored to the original ''
    expect(htmlOverflow()).toBe('');
  });

  it('does not capture "hidden" as the original when a second lock acquires', () => {
    document.documentElement.style.overflow = 'auto';
    const a = renderHook(() => useScrollLock(true)); // saves 'auto', sets 'hidden'
    const b = renderHook(() => useScrollLock(true)); // must NOT re-save 'hidden'
    b.unmount();
    a.unmount();
    expect(htmlOverflow()).toBe('auto');
  });
});
