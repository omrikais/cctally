import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { useStableSet, useStableMap, useMonotonicMax } from './useStableIdentity';

// #231 — these hooks are the conversation reader's defense against the
// MessageItem React.memo being defeated on every reverse-page prepend: a
// content-derived Set/Map that recomputes to a fresh identity on each commit
// must collapse back to its PRIOR reference when the content is unchanged, or the
// whole rendered window re-renders (re-parsing every card's markdown). The tests
// assert BOTH halves of the contract: same-content → stable identity (the bug
// fix), and changed-content → fresh identity (no stale reads).

describe('useStableSet', () => {
  it('keeps the prior reference when a re-derived Set is element-equal', () => {
    const { result, rerender } = renderHook(({ s }: { s: Set<string> }) => useStableSet(s), {
      initialProps: { s: new Set(['a', 'b', 'c']) },
    });
    const first = result.current;
    // A NEW Set object with identical members (mirrors `subagent_meta` being
    // re-sent on each page apply: fresh object, same content).
    rerender({ s: new Set(['c', 'b', 'a']) });
    expect(result.current).toBe(first); // identity preserved → memo holds
  });

  it('returns the new reference when membership changes', () => {
    const { result, rerender } = renderHook(({ s }: { s: Set<string> }) => useStableSet(s), {
      initialProps: { s: new Set(['a', 'b']) },
    });
    const first = result.current;
    const grown = new Set(['a', 'b', 'c']);
    rerender({ s: grown });
    expect(result.current).not.toBe(first);
    expect(result.current).toBe(grown);
    // Same-size but different member also counts as a change.
    const swapped = new Set(['a', 'c', 'd']);
    rerender({ s: swapped });
    expect(result.current).toBe(swapped);
  });
});

describe('useStableMap', () => {
  it('keeps the prior reference when a re-derived Map is entry-equal', () => {
    const { result, rerender } = renderHook(({ m }: { m: Map<string, string> }) => useStableMap(m), {
      initialProps: { m: new Map([['t1', 'general'], ['t2', 'Explore']]) },
    });
    const first = result.current;
    rerender({ m: new Map([['t2', 'Explore'], ['t1', 'general']]) }); // new object, same entries
    expect(result.current).toBe(first); // identity preserved → memo holds
  });

  it('returns the new reference when a value changes for the same key', () => {
    const { result, rerender } = renderHook(({ m }: { m: Map<string, string> }) => useStableMap(m), {
      initialProps: { m: new Map([['t1', 'general']]) },
    });
    const first = result.current;
    const changed = new Map([['t1', 'Explore']]); // same key, different value
    rerender({ m: changed });
    expect(result.current).not.toBe(first);
    expect(result.current).toBe(changed);
  });

  it('returns the new reference when an entry is added', () => {
    const { result, rerender } = renderHook(({ m }: { m: Map<string, string> }) => useStableMap(m), {
      initialProps: { m: new Map([['t1', 'general']]) },
    });
    const first = result.current;
    const grown = new Map([['t1', 'general'], ['t2', 'Explore']]); // a newly-loaded spawn
    rerender({ m: grown });
    expect(result.current).not.toBe(first);
    expect(result.current).toBe(grown);
  });
});

describe('useMonotonicMax', () => {
  it('ratchets up to the running maximum', () => {
    const { result, rerender } = renderHook(
      ({ v }: { v: number }) => useMonotonicMax(v, 's'),
      { initialProps: { v: 1.5 } },
    );
    expect(result.current).toBe(1.5);
    rerender({ v: 3.2 });
    expect(result.current).toBe(3.2);
  });

  it('does NOT decrease when the value drops (the windowed-trim case)', () => {
    const { result, rerender } = renderHook(
      ({ v }: { v: number }) => useMonotonicMax(v, 's'),
      { initialProps: { v: 4.0 } },
    );
    expect(result.current).toBe(4.0);
    // A trim drops the max-cost item out of the loaded window → raw max falls.
    rerender({ v: 2.1 });
    expect(result.current).toBe(4.0); // ratchet holds — no context churn
    rerender({ v: 3.0 });
    expect(result.current).toBe(4.0); // still below the high-water mark
  });

  it('resets when the reset key (session) changes', () => {
    const { result, rerender } = renderHook(
      ({ v, k }: { v: number; k: string }) => useMonotonicMax(v, k),
      { initialProps: { v: 5.0, k: 'A' } },
    );
    expect(result.current).toBe(5.0);
    // New session: ratchet resets and re-seeds from the new value (not the old max).
    rerender({ v: 1.2, k: 'B' });
    expect(result.current).toBe(1.2);
  });
});
