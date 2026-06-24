import { useRef } from 'react';

// #231 — Referential-stability hooks for derived collections.
//
// The conversation reader derives whole-conversation lookup collections
// (`suppressToolUseIds`, `spawnKindByToolUseId`) from `groups` / `subagent_meta`
// via `useMemo`, then passes them to EVERY memoized `MessageItem`. Those useMemos
// recompute (yielding a NEW Set/Map identity) on every commit that touches
// `detail.items` — i.e. every reverse-page prepend AND every windowed-DOM-cap
// trim — because `groups` recomputes from the new `detail.items`, and the server
// re-sends `subagent_meta` (a fresh object) on each page. A fresh identity with
// IDENTICAL content still fails React.memo's shallow prop compare, so the entire
// rendered window re-renders (re-parsing every card's markdown) on each such
// commit. A single backward drain fires prepend + cap + trim per page → ~2.4
// whole-window re-render passes per page → an O(n²) cascade that froze the cold
// deep-link reader for 80s+ (#231).
//
// These hooks collapse identity to content: when a recompute produces a
// collection element-equal to the prior one, the PRIOR reference is returned, so
// the memo holds and only genuinely-changed items re-render. They are deliberately
// O(size) per render — cheap relative to the markdown re-render they prevent.

/** Keep the prior Set reference when `next` is element-equal (same size, same
 *  members). Stabilizes a per-render-derived Set so React.memo consumers don't
 *  re-render when the content is unchanged. */
export function useStableSet<T>(next: Set<T>): Set<T> {
  const ref = useRef(next);
  const prev = ref.current;
  if (prev === next) return prev;
  if (prev.size === next.size) {
    let equal = true;
    for (const v of next) {
      if (!prev.has(v)) { equal = false; break; }
    }
    if (equal) return prev;
  }
  ref.current = next;
  return next;
}

/** Monotonic ratchet keyed on a reset token. Returns the maximum `value` seen
 *  since `resetKey` last changed (reset to 0 when it changes, then re-seeded from
 *  the current value). #231 — the windowed DOM cap can TRIM the max-cost item out
 *  of the loaded window, lowering an upstream max; that decrease, riding on a
 *  context every memoized card reads, would re-render the whole window. Ratcheting
 *  keeps the value monotonic within a session so the context (and the memo) stays
 *  stable across paging/trim commits. */
export function useMonotonicMax(value: number, resetKey: string): number {
  const maxRef = useRef(0);
  const keyRef = useRef(resetKey);
  if (keyRef.current !== resetKey) {
    keyRef.current = resetKey;
    maxRef.current = 0;
  }
  if (value > maxRef.current) maxRef.current = value;
  return maxRef.current;
}

/** Keep the prior Map reference when `next` is entry-equal (same size, same
 *  key→value pairs by `Object.is`). Stabilizes a per-render-derived Map so
 *  React.memo consumers don't re-render when the content is unchanged. */
export function useStableMap<K, V>(next: Map<K, V>): Map<K, V> {
  const ref = useRef(next);
  const prev = ref.current;
  if (prev === next) return prev;
  if (prev.size === next.size) {
    let equal = true;
    for (const [k, v] of next) {
      if (!prev.has(k) || !Object.is(prev.get(k), v)) { equal = false; break; }
    }
    if (equal) return prev;
  }
  ref.current = next;
  return next;
}
