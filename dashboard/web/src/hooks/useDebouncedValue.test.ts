// dashboard/web/src/hooks/useDebouncedValue.test.ts
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDebouncedValue } from './useDebouncedValue';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('useDebouncedValue', () => {
  it('emits the initial value immediately (initial defaults to value)', () => {
    const { result } = renderHook(({ v }) => useDebouncedValue(v, 200), { initialProps: { v: 'a' } });
    expect(result.current).toBe('a');
  });

  it('surfaces a change only after delayMs', () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 200), { initialProps: { v: 'a' } });
    rerender({ v: 'b' });
    expect(result.current).toBe('a');
    act(() => { vi.advanceTimersByTime(199); });
    expect(result.current).toBe('a');
    act(() => { vi.advanceTimersByTime(1); });
    expect(result.current).toBe('b');
  });

  it('coalesces rapid changes to the last value (timer resets on each change)', () => {
    const { result, rerender } = renderHook(({ v }) => useDebouncedValue(v, 200), { initialProps: { v: 'a' } });
    rerender({ v: 'b' });
    act(() => { vi.advanceTimersByTime(100); });
    rerender({ v: 'c' });
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current).toBe('a');            // 200ms since 'b' but only 100ms since 'c'
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current).toBe('c');
  });

  it('defers a non-empty initial value when initial is overridden', () => {
    const { result } = renderHook(() => useDebouncedValue('flock', 200, ''));
    expect(result.current).toBe('');             // cold start, not 'flock'
    act(() => { vi.advanceTimersByTime(200); });
    expect(result.current).toBe('flock');
  });
});
