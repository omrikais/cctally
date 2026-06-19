import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { useScrollLock } from './useScrollLock';

afterEach(() => {
  document.body.style.overflow = '';
});

describe('useScrollLock', () => {
  it('locks body overflow on mount and restores the prior value on unmount', () => {
    document.body.style.overflow = 'visible';
    const { unmount } = renderHook(() => useScrollLock(true));
    expect(document.body.style.overflow).toBe('hidden');
    unmount();
    expect(document.body.style.overflow).toBe('visible');
  });

  it('is a no-op when inactive', () => {
    document.body.style.overflow = 'scroll';
    const { unmount } = renderHook(() => useScrollLock(false));
    expect(document.body.style.overflow).toBe('scroll');
    unmount();
    expect(document.body.style.overflow).toBe('scroll');
  });

  it('stays locked until the LAST of two stacked locks releases (refcount)', () => {
    document.body.style.overflow = '';
    const a = renderHook(() => useScrollLock(true));
    const b = renderHook(() => useScrollLock(true));
    expect(document.body.style.overflow).toBe('hidden');
    a.unmount();
    // one lock still held -> body stays locked
    expect(document.body.style.overflow).toBe('hidden');
    b.unmount();
    // both released -> restored to the original ''
    expect(document.body.style.overflow).toBe('');
  });

  it('does not capture "hidden" as the original when a second lock acquires', () => {
    document.body.style.overflow = 'auto';
    const a = renderHook(() => useScrollLock(true)); // saves 'auto', sets 'hidden'
    const b = renderHook(() => useScrollLock(true)); // must NOT re-save 'hidden'
    b.unmount();
    a.unmount();
    expect(document.body.style.overflow).toBe('auto');
  });
});
