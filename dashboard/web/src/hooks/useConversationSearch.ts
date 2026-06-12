import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { useDebouncedValue } from './useDebouncedValue';
import type { ConversationSearchResult, SearchHit, SearchKind } from '../types/conversation';

// Debounced cross-session search. Empty/whitespace needle -> no fetch, empty
// hits. 200ms debounce (even on the first keystroke: the rail mounts this hook
// with a non-empty needle, so useDebouncedValue is seeded with '' to defer it).
// `mode` (fts | like) lets the rail show a "· basic search" count-line suffix.
//
// #177 S6 — `kind` joins the fetch (a chip facet: all|prompts|assistant|tools|
// thinking) AND the debounced-needle effect deps: a kind change aborts any
// in-flight fetch and refetches from offset 0, REPLACING hits. `loadMore()`
// fetches offset=hits.length and APPENDS, guarded `!loadingMore && hits.length
// < total`. A loadMore response that lands after a needle/kind change is
// discarded by the same `ctlRef` abort discipline the offset-0 fetch uses.
// `searchDepth` is the interim-index signal ('prose-only' while the one-time
// column split is still backfilling, else 'full') so the rail can degrade the
// Tools/Thinking chips.
export interface UseConversationSearch {
  hits: SearchHit[];
  mode: 'fts' | 'like' | null;
  total: number;
  loading: boolean;
  loadingMore: boolean;
  searchDepth: 'prose-only' | 'full' | null;
  error: string | null;
  loadMore: () => void;
}

const DEBOUNCE_MS = 200;

export function useConversationSearch(
  query: string,
  kind: SearchKind = 'all',
): UseConversationSearch {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [mode, setMode] = useState<'fts' | 'like' | null>(null);
  const [total, setTotal] = useState(0);
  const [searchDepth, setSearchDepth] = useState<'prose-only' | 'full' | null>(null);
  const [fetching, setFetching] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const q = query.trim();
  const debouncedQ = useDebouncedValue(q, DEBOUNCE_MS, '');
  // ONE controller ref shared by the offset-0 fetch AND loadMore: any
  // needle/kind change (or a fresh page-0 fetch) aborts whatever it points at,
  // so a stale append can never commit over the newer query's state.
  const ctlRef = useRef<AbortController | null>(null);
  // Latest hits length, read by loadMore without re-creating the callback on
  // every page (so the rail's button identity stays stable).
  const hitsLenRef = useRef(0);
  hitsLenRef.current = hits.length;
  const totalRef = useRef(0);
  totalRef.current = total;
  const loadingMoreRef = useRef(false);
  loadingMoreRef.current = loadingMore;

  const url = useCallback(
    (offset: number) =>
      `/api/conversation/search?q=${encodeURIComponent(debouncedQ)}&limit=50&offset=${offset}&kind=${kind}`,
    [debouncedQ, kind],
  );

  // Reset on an empty needle, and abort any in-flight fetch the instant the raw
  // needle OR kind changes — so a prior response can never commit late over the
  // newer query's state.
  useEffect(() => {
    if (!q) { setHits([]); setMode(null); setTotal(0); setSearchDepth(null); setError(null); }
    return () => { ctlRef.current?.abort(); };
  }, [q, kind]);

  // Page-0 fetch, keyed on the settled needle + the kind facet. REPLACES hits.
  useEffect(() => {
    // #177 S6 M2 — clear BOTH in-flight flags on the empty-needle early return.
    // `loadingMore` was previously left set here: unreachable as a live bug
    // (an empty needle unmounts SearchList, discarding this hook's state) but
    // defensive symmetry with the `fetching` reset.
    if (!debouncedQ) { setFetching(false); setLoadingMore(false); return; }
    const ctl = new AbortController();
    ctlRef.current = ctl;
    setFetching(true);
    setLoadingMore(false);
    fetchJson<ConversationSearchResult>(url(0), ctl.signal)
      .then((body) => {
        setHits(body.hits);
        setMode(body.mode);
        setTotal(body.total);
        setSearchDepth(body.search_depth ?? 'full');
        setError(null);
        setFetching(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setError('Search failed.'); setFetching(false);
      });
    return () => ctl.abort();
  }, [debouncedQ, kind, url]);

  // Append the next page. Guarded so it can't fire while a load is in flight or
  // once everything is loaded. Shares ctlRef, so a needle/kind change aborts an
  // in-flight append and its response is discarded.
  const loadMore = useCallback(() => {
    if (loadingMoreRef.current || hitsLenRef.current >= totalRef.current) return;
    const offset = hitsLenRef.current;
    const ctl = new AbortController();
    ctlRef.current = ctl;
    setLoadingMore(true);
    fetchJson<ConversationSearchResult>(url(offset), ctl.signal)
      .then((body) => {
        setHits((prev) => [...prev, ...body.hits]);
        setTotal(body.total);
        setMode(body.mode);
        setSearchDepth(body.search_depth ?? 'full');
        setLoadingMore(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;   // stale append discarded on needle/kind change
        setError('Search failed.'); setLoadingMore(false);
      });
  }, [url]);

  // `loading` is DERIVED, not imperatively set: true while a non-empty needle's
  // results aren't ready — either the debounce hasn't caught up to the typed
  // needle (q !== debouncedQ, the immediate-feedback case) or the settled-needle
  // fetch is in flight. Deriving it avoids a stuck-true state when the needle
  // oscillates back to an already-settled value within the debounce window
  // (debouncedQ never changes, so no fetch re-fires to clear an imperative flag).
  const loading = q !== '' && (q !== debouncedQ || fetching);

  return { hits, mode, total, loading, loadingMore, searchDepth, error, loadMore };
}
