import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useSnapshot } from './useSnapshot';
import { filterParams } from './conversationFilterParams';
import type { ConversationSummary, ConversationsPage } from '../types/conversation';

// Browse-rail list. Offset-paginated, accumulating. Revalidates the
// FIRST page on every SSE tick (the list shifts as new sessions ingest)
// — stale-while-revalidate: rows stay mounted across the refetch — but
// ONLY while the user is still on page 1. Once they've paged (a tail
// beyond PAGE accumulated, or a loadMore is in flight), the tick reload
// is suppressed so it can't clobber the accumulated tail or rewind the
// cursor; a fresh page-1 load only happens on remount. loadMore()
// appends. #217 S4 / I-2.3 — the sort key is read from the store
// (`conversationRailSort`) via a ref and threaded into the `sort` param; a
// sort OR filter change resets the offset and invalidates in-flight appends
// via ONE combined `{filters, sort}` generation token.
//
// Visibility-gating (spec §4): the per-tick page-1 revalidation is skipped
// while the tab is hidden (a backgrounded reader idle for hours otherwise
// re-issues the rail query on every 5s SSE tick, forever). On the
// hidden→visible transition exactly one refetch fires so a freshly-revealed
// tab is current immediately rather than up to 5s stale. The SSE stream
// itself (/api/events) is never gated — only the rail's page-1 refetch is.
export interface UseConversations {
  rows: ConversationSummary[];
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => Promise<void>;
  // #217 S3 E10#7 — true while a loadMore() page fetch is in flight, so the rail
  // Load-more button can disable + show a loading state (matching SearchList).
  loadingMore: boolean;
  // filters spec §1 dual-branch parity — true when a project/cost/rebuild filter
  // was requested but the rollup was non-authoritative (the live fallback can only
  // filter by date). The rail surfaces a muted note.
  filterDegraded: boolean;
  // #217 S4 / I-2.3 — true when a cost/project sort was requested under the
  // non-authoritative window (the live fallback fell back to recent order). The
  // rail surfaces a "sort unavailable while indexing" note.
  sortDegraded: boolean;
  // #205 S3 (F8) — user-initiated re-load of page 1 after a failed fetch.
  retry: () => void;
}

const PAGE = 50;

export function useConversations(): UseConversations {
  const [rows, setRows] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [filterDegraded, setFilterDegraded] = useState(false);
  const [sortDegraded, setSortDegraded] = useState(false);
  // Active browse filters from the store (filters spec §4). A `filtersRef` mirror
  // lets the ref-stable loadFirstPage / loadMore read the LATEST filters without
  // closing over them (which would churn the []-dep callbacks and the tick effect).
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  // #217 S4 / I-2.3 — the active rail sort key, mirrored through a ref the same
  // way as filters so the ref-stable loadFirstPage / loadMore read the LATEST
  // sort without closing over it.
  const sort = useSyncExternalStore(subscribeStore, () => getState().conversationRailSort);
  const sortRef = useRef(sort);
  sortRef.current = sort;
  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  // Sync in-flight guard (loadingMoreRef) gates re-entrancy + the page-1
  // revalidation suppression; the loadingMore STATE drives the rail's Load-more
  // disabled/loading affordance (#217 S3 E10#7). Two surfaces: the ref must flip
  // synchronously inside loadMore (so a refocus tick can't double-fetch), the
  // state can lag a render (it only paints a button).
  const loadingMoreRef = useRef(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Mirror rows.length through a ref so loadFirstPage can read the page-1
  // suppression predicate WITHOUT closing over rows.length — that would
  // recreate the callback on every row update and could re-trigger the tick
  // effect (Codex gate, MAJOR 5: loadFirstPage must be ref-stable).
  const rowsLenRef = useRef(0);
  useEffect(() => { rowsLenRef.current = rows.length; }, [rows.length]);
  // Single in-flight controller: each invocation aborts the prior before
  // starting a new one, so a refocus burst (the visibilitychange listener +
  // the [generatedAt] tick effect) collapses to a single completing fetch.
  const ctlRef = useRef<AbortController | null>(null);
  // Combined {filters, sort} generation token, bumped on every filter OR sort
  // change (the [queryKey] reset effect — #217 S4 / I-2.3). loadMore captures it
  // at start and bails out of its setRows / setNextOffset if it changed by the
  // time the response resolves — otherwise an in-flight loadMore would append
  // OLD-order/OLD-filter rows (with the OLD cursor) onto the list the reset
  // effect just emptied (FINDING 2). ctlRef can't cover loadMore because loadMore
  // deliberately omits the abort signal (a transient blip must not wipe the
  // accumulated tail).
  const filterGenRef = useRef(0);

  // Stable page-1 (re)load. Reads suppression state through refs so it is NOT
  // recreated on every render — that ref-stability is what keeps it from
  // churning the tick effect or refetching on row updates.
  const loadFirstPage = useCallback(() => {
    // Revalidate page 1 ONLY while the user is still on the first page; once
    // they've paged (rows beyond PAGE) we must not clobber the accumulated
    // tail or rewind the cursor. A fresh load happens on remount.
    if (loadingMoreRef.current || rowsLenRef.current > PAGE) return;
    ctlRef.current?.abort();
    const ctl = new AbortController();
    ctlRef.current = ctl;
    fetchJson<ConversationsPage>(`/api/conversations?sort=${sortRef.current}&limit=${PAGE}&offset=0${filterParams(filtersRef.current)}`, ctl.signal)
      .then((body) => {
        setRows(body.conversations);
        setNextOffset(body.page.next_offset);
        setFilterDegraded(body.page.filter_degraded === true);
        setSortDegraded(body.page.sort_degraded === true);
        setError(null);
        setLoading(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setError("Couldn't load conversations.");
        setLoading(false);
      });
  }, []);

  // #205 S3 (F8) — explicit retry for the dead error state. Clears the error
  // and shows the "Loading…" branch (rows are empty on a failed first load),
  // then re-issues page 1. loadFirstPage itself is NOT changed to flip loading
  // (it is shared with the silent SSE revalidation, which must not flash a
  // spinner over a populated list) — only this wrapper sets it.
  const retry = useCallback(() => {
    setError(null);
    setLoading(true);
    loadFirstPage();
  }, [loadFirstPage]);

  // Filter/sort-change reset (filters spec §4 / #217 S4 / I-2.3): a filter OR
  // sort change wipes the accumulated tail, rewinds the cursor to offset 0, and
  // refetches page 1 with the new params. Keyed on a stable JSON key of BOTH
  // filters AND sort so a no-op SET (same values) doesn't refetch but EITHER axis
  // changing does. The MOUNT run is skipped (mountQueryKeyRef) — the
  // [generatedAt] mount/tick effect below already issues the initial page-1 load;
  // double-firing here would re-fetch page 1 and, mid-paging, clobber the
  // accumulated tail.
  const queryKey = JSON.stringify({ filters, sort });
  const mountQueryKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (mountQueryKeyRef.current === null) {
      // First commit: the mount/tick effect owns the initial load.
      mountQueryKeyRef.current = queryKey;
      return;
    }
    if (mountQueryKeyRef.current === queryKey) return; // no-op SET (same values)
    mountQueryKeyRef.current = queryKey;
    // Invalidate any in-flight loadMore so its (old-filter) result can't append
    // stale rows onto the list we're about to empty (FINDING 2). Also drop the
    // loadingMore flag: a still-in-flight loadMore would otherwise make
    // loadFirstPage()'s guard early-return and the fresh page-1 load would never
    // fire. The superseded loadMore's generation check now no-ops its result, and
    // its own finally re-clears the flag harmlessly.
    filterGenRef.current += 1;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    setRows([]);
    setNextOffset(0);
    rowsLenRef.current = 0;
    loadFirstPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryKey]);

  // First-page (re)load on mount + every SSE tick — but never while hidden.
  // Keyed on [generatedAt, loadFirstPage]; loadFirstPage is ref-stable so the
  // effect re-runs only when generatedAt changes (the SSE tick), never on a
  // plain row update. The tick reload reads filtersRef so it always carries the
  // active filters (a tick must never repaint an UNfiltered page 1).
  useEffect(() => {
    if (typeof document !== 'undefined' && document.hidden) return;
    loadFirstPage();
    return () => ctlRef.current?.abort();
  }, [generatedAt, loadFirstPage]);

  // Refetch once on the hidden→visible transition so a freshly-revealed tab is
  // current immediately (covers the case where the SSE stream was idle or
  // reconnecting during the away period).
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const onVisibility = (): void => {
      if (!document.hidden) loadFirstPage();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [loadFirstPage]);

  const loadMore = useCallback(async () => {
    if (nextOffset == null || loadingMoreRef.current) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    // Snapshot the {filters, sort} generation; if a filter OR sort change bumps
    // it while this request is in flight, the response is stale (old order/filter
    // / old cursor) and must be discarded — the reset effect already issued a
    // fresh page-1 load.
    const gen = filterGenRef.current;
    try {
      const body = await fetchJson<ConversationsPage>(`/api/conversations?sort=${sortRef.current}&limit=${PAGE}&offset=${nextOffset}${filterParams(filtersRef.current)}`);
      if (filterGenRef.current !== gen) return; // filter/sort changed mid-flight — drop stale rows
      setRows((prev) => [...prev, ...body.conversations]);
      setNextOffset(body.page.next_offset);
      setFilterDegraded(body.page.filter_degraded === true);
      setSortDegraded(body.page.sort_degraded === true);
    } catch {
      /* keep what we have; a transient blip shouldn't wipe the list */
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  }, [nextOffset]);

  // #227 — feed the shared session_id → title cache as rows land (page 1, every
  // loadMore append, and each SSE-tick revalidation). The reducer merges
  // non-empty titles and no-ops when nothing changed, so re-dispatching the same
  // rows on every tick is cheap. ComparisonView reads this so its header can show
  // the real derived title without issuing its own browse fetch.
  useEffect(() => {
    if (rows.length === 0) return;
    dispatch({ type: 'CACHE_CONVERSATION_TITLES', titles: rows.map((r) => [r.session_id, r.title]) });
  }, [rows]);

  return { rows, loading, error, hasMore: nextOffset != null, loadMore, loadingMore, filterDegraded, sortDegraded, retry };
}
