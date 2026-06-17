import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { getState, subscribeStore } from '../store/store';
import { useSnapshot } from './useSnapshot';
import type { ConversationFilters, ConversationSummary, ConversationsPage } from '../types/conversation';

// Serialize the active filters into the /api/conversations query string (filters
// spec §4 / §2). Each axis appends a parameterized predicate server-side; absent
// axes are simply omitted. `projects` repeats (?projects=a&projects=b) so the
// server reads the multi-select as an IN(...). Returns '' (NOT '&…') when no axis
// is active so the base URL stays byte-identical to the unfiltered path.
function filterParams(f: ConversationFilters): string {
  const p = new URLSearchParams();
  if (f.dateFrom) p.set('date_from', f.dateFrom);
  if (f.dateTo) p.set('date_to', f.dateTo);
  for (const proj of f.projects) p.append('projects', proj);
  if (f.costMin != null) p.set('cost_min', String(f.costMin));
  if (f.costMax != null) p.set('cost_max', String(f.costMax));
  if (f.rebuildMin != null) p.set('rebuild_min', String(f.rebuildMin));
  const s = p.toString();
  return s ? `&${s}` : '';
}

// Browse-rail list. Offset-paginated, accumulating. Revalidates the
// FIRST page on every SSE tick (the list shifts as new sessions ingest)
// — stale-while-revalidate: rows stay mounted across the refetch — but
// ONLY while the user is still on page 1. Once they've paged (a tail
// beyond PAGE accumulated, or a loadMore is in flight), the tick reload
// is suppressed so it can't clobber the accumulated tail or rewind the
// cursor; a fresh page-1 load only happens on remount. loadMore()
// appends. Sort is fixed to 'recent' in v1.
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
  // filters spec §1 dual-branch parity — true when a project/cost/rebuild filter
  // was requested but the rollup was non-authoritative (the live fallback can only
  // filter by date). The rail surfaces a muted note.
  filterDegraded: boolean;
}

const PAGE = 50;

export function useConversations(): UseConversations {
  const [rows, setRows] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
  const [filterDegraded, setFilterDegraded] = useState(false);
  // Active browse filters from the store (filters spec §4). A `filtersRef` mirror
  // lets the ref-stable loadFirstPage / loadMore read the LATEST filters without
  // closing over them (which would churn the []-dep callbacks and the tick effect).
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;
  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  const loadingMoreRef = useRef(false);
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
  // Filter generation token, bumped on every filter change (the [filterKey]
  // reset effect). loadMore captures it at start and bails out of its setRows /
  // setNextOffset if it changed by the time the response resolves — otherwise an
  // in-flight loadMore would append OLD-filter rows (with the OLD cursor) onto the
  // list the reset effect just emptied (FINDING 2). ctlRef can't cover loadMore
  // because loadMore deliberately omits the abort signal (a transient blip must
  // not wipe the accumulated tail).
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
    fetchJson<ConversationsPage>(`/api/conversations?sort=recent&limit=${PAGE}&offset=0${filterParams(filtersRef.current)}`, ctl.signal)
      .then((body) => {
        setRows(body.conversations);
        setNextOffset(body.page.next_offset);
        setFilterDegraded(body.page.filter_degraded === true);
        setError(null);
        setLoading(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setError("Couldn't load conversations.");
        setLoading(false);
      });
  }, []);

  // Filter-change reset (filters spec §4): a filter change wipes the accumulated
  // tail, rewinds the cursor to offset 0, and refetches page 1 with the new
  // params. Keyed on a stable JSON key so a no-op SET (same values) doesn't
  // refetch. The MOUNT run is skipped (mountFilterKeyRef) — the [generatedAt]
  // mount/tick effect below already issues the initial page-1 load; double-firing
  // here would re-fetch page 1 and, mid-paging, clobber the accumulated tail.
  const filterKey = JSON.stringify(filters);
  const mountFilterKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (mountFilterKeyRef.current === null) {
      // First commit: the mount/tick effect owns the initial load.
      mountFilterKeyRef.current = filterKey;
      return;
    }
    if (mountFilterKeyRef.current === filterKey) return; // no-op SET (same values)
    mountFilterKeyRef.current = filterKey;
    // Invalidate any in-flight loadMore so its (old-filter) result can't append
    // stale rows onto the list we're about to empty (FINDING 2). Also drop the
    // loadingMore flag: a still-in-flight loadMore would otherwise make
    // loadFirstPage()'s guard early-return and the fresh page-1 load would never
    // fire. The superseded loadMore's generation check now no-ops its result, and
    // its own finally re-clears the flag harmlessly.
    filterGenRef.current += 1;
    loadingMoreRef.current = false;
    setRows([]);
    setNextOffset(0);
    rowsLenRef.current = 0;
    loadFirstPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey]);

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
    // Snapshot the filter generation; if a filter change bumps it while this
    // request is in flight, the response is stale (old filter / old cursor) and
    // must be discarded — the reset effect already issued a fresh page-1 load.
    const gen = filterGenRef.current;
    try {
      const body = await fetchJson<ConversationsPage>(`/api/conversations?sort=recent&limit=${PAGE}&offset=${nextOffset}${filterParams(filtersRef.current)}`);
      if (filterGenRef.current !== gen) return; // filter changed mid-flight — drop stale rows
      setRows((prev) => [...prev, ...body.conversations]);
      setNextOffset(body.page.next_offset);
      setFilterDegraded(body.page.filter_degraded === true);
    } catch {
      /* keep what we have; a transient blip shouldn't wipe the list */
    } finally {
      loadingMoreRef.current = false;
    }
  }, [nextOffset]);

  return { rows, loading, error, hasMore: nextOffset != null, loadMore, filterDegraded };
}
