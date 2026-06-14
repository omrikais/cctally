import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import type { ConversationSummary, ConversationsPage } from '../types/conversation';

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
}

const PAGE = 50;

export function useConversations(): UseConversations {
  const [rows, setRows] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(0);
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
    fetchJson<ConversationsPage>(`/api/conversations?sort=recent&limit=${PAGE}&offset=0`, ctl.signal)
      .then((body) => {
        setRows(body.conversations);
        setNextOffset(body.page.next_offset);
        setError(null);
        setLoading(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setError("Couldn't load conversations.");
        setLoading(false);
      });
  }, []);

  // First-page (re)load on mount + every SSE tick — but never while hidden.
  // Keyed on [generatedAt, loadFirstPage]; loadFirstPage is ref-stable so the
  // effect re-runs only when generatedAt changes (the SSE tick), never on a
  // plain row update.
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
    try {
      const body = await fetchJson<ConversationsPage>(`/api/conversations?sort=recent&limit=${PAGE}&offset=${nextOffset}`);
      setRows((prev) => [...prev, ...body.conversations]);
      setNextOffset(body.page.next_offset);
    } catch {
      /* keep what we have; a transient blip shouldn't wipe the list */
    } finally {
      loadingMoreRef.current = false;
    }
  }, [nextOffset]);

  return { rows, loading, error, hasMore: nextOffset != null, loadMore };
}
