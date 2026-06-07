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

  // First-page (re)load on mount + every SSE tick.
  useEffect(() => {
    // Revalidate page 1 on SSE tick ONLY while the user is still on the first
    // page; once they've paged (rows beyond PAGE) we must not clobber the
    // accumulated tail or rewind the cursor. A fresh load happens on remount.
    if (loadingMoreRef.current || rows.length > PAGE) return;
    const ctl = new AbortController();
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
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generatedAt]);

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
