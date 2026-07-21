import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { getState, subscribeStore } from '../store/store';
import { useDebouncedValue } from './useDebouncedValue';
import { filterParams } from './conversationFilterParams';
import { adaptQualifiedSearch } from '../lib/conversationAdapters';
import { qualifiedSearchUrl, type QualifiedSearchEnvelope } from '../lib/conversationTransport';
import type { ConversationSearchResult, ConversationSource, SearchHit, SearchKind } from '../types/conversation';

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
//
// #217 S4 / I-2.5 — the SHARED `conversationFilters` state (the same set the
// browse rail uses) auto-applies to search: the six filter params thread into
// the URL, and a filter change is folded into the `url` callback's deps so it
// joins the reset/abort key (offset → 0, in-flight loadMore aborted via ctlRef)
// exactly like a needle/kind change. `filterDegraded` surfaces the response's
// TOP-LEVEL `filter_degraded` flag (distinct from browse's under-`page`).
export interface UseConversationSearch {
  hits: SearchHit[];
  mode: 'fts' | 'like' | null;
  total: number;
  loading: boolean;
  loadingMore: boolean;
  searchDepth: 'prose-only' | 'full' | null;
  filterDegraded: boolean;
  error: string | null;
  loadMore: () => void;
  pending: boolean;
}

const DEBOUNCE_MS = 200;

export function useConversationSearch(
  query: string,
  kind: SearchKind = 'all',
  source: ConversationSource = 'claude',
  options: { qualified?: boolean } = {},
): UseConversationSearch {
  const qualified = options.qualified === true || source === 'codex';
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [mode, setMode] = useState<'fts' | 'like' | null>(null);
  const [total, setTotal] = useState(0);
  const [searchDepth, setSearchDepth] = useState<'prose-only' | 'full' | null>(null);
  const [filterDegraded, setFilterDegraded] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const q = query.trim();
  // #217 S4 / I-2.5 — the shared browse filters (auto-applied to search). A
  // stable JSON key folds into the `url` callback's deps so a filter change
  // re-keys the page-0 effect (reset/abort) the same way a needle/kind change
  // does. `filtersRef` lets loadMore read the LATEST filters without re-creating
  // the callback on every filter edit.
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  const filterKey = JSON.stringify(filters);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
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
    (offset: number, nextCursor?: string | null) => qualified
      ? qualifiedSearchUrl(source, { query: debouncedQ, kind, limit: 50, cursor: nextCursor ?? undefined })
      : `/api/conversation/search?q=${encodeURIComponent(debouncedQ)}&limit=50&offset=${offset}&kind=${kind}${filterParams(filtersRef.current)}`,
    // filterKey is in the deps so a filter change re-creates `url` → re-fires the
    // keyed page-0 effect (reset/abort), even though the body reads filtersRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [debouncedQ, kind, filterKey, source, qualified],
  );

  // Reset on an empty needle, and abort any in-flight fetch the instant the raw
  // needle OR kind OR filter set changes — so a prior response can never commit
  // late over the newer query's state.
  useEffect(() => {
    if (!q) { setHits([]); setMode(null); setTotal(0); setSearchDepth(null); setFilterDegraded(false); setError(null); }
    return () => { ctlRef.current?.abort(); };
  }, [q, kind, filterKey]);

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
    fetchJson<ConversationSearchResult | QualifiedSearchEnvelope>(url(0), ctl.signal)
      .then((raw) => {
        const body = qualified
          ? adaptQualifiedSearch(source, raw as QualifiedSearchEnvelope)
          : { ...(raw as ConversationSearchResult), cursor: null, pending: false };
        setHits(body.hits);
        setMode(body.mode);
        setTotal(body.total);
        setSearchDepth(body.search_depth ?? 'full');
        setFilterDegraded(body.filter_degraded === true);
        setCursor(body.cursor);
        setPending(body.pending);
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
    fetchJson<ConversationSearchResult | QualifiedSearchEnvelope>(url(offset, cursor), ctl.signal)
      .then((raw) => {
        const body = qualified
          ? adaptQualifiedSearch(source, raw as QualifiedSearchEnvelope)
          : { ...(raw as ConversationSearchResult), cursor: null, pending: false };
        setHits((prev) => [...prev, ...body.hits]);
        setTotal(body.total);
        setMode(body.mode);
        setSearchDepth(body.search_depth ?? 'full');
        setFilterDegraded(body.filter_degraded === true);
        setCursor(body.cursor);
        setPending(body.pending);
        setLoadingMore(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;   // stale append discarded on needle/kind change
        setError('Search failed.'); setLoadingMore(false);
      });
  }, [url, cursor, source, qualified]);

  // `loading` is DERIVED, not imperatively set: true while a non-empty needle's
  // results aren't ready — either the debounce hasn't caught up to the typed
  // needle (q !== debouncedQ, the immediate-feedback case) or the settled-needle
  // fetch is in flight. Deriving it avoids a stuck-true state when the needle
  // oscillates back to an already-settled value within the debounce window
  // (debouncedQ never changes, so no fetch re-fires to clear an imperative flag).
  const loading = q !== '' && (q !== debouncedQ || fetching);

  return { hits, mode, total, loading, loadingMore, searchDepth, filterDegraded, error, loadMore, pending };
}
