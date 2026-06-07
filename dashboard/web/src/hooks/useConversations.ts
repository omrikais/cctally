import { useCallback, useEffect, useRef, useState } from 'react';
import { useSnapshot } from './useSnapshot';
import type { ConversationSummary, ConversationsPage } from '../types/conversation';

// Browse-rail list. Offset-paginated, accumulating. Revalidates the
// FIRST page on every SSE tick (the list shifts as new sessions ingest)
// — stale-while-revalidate: rows stay mounted across the refetch. A
// fresh first-page load (mount, or sort change) resets the accumulator;
// loadMore() appends. Sort is fixed to 'recent' in v1.
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
    const ctl = new AbortController();
    fetch(`/api/conversations?sort=recent&limit=${PAGE}&offset=0`, { signal: ctl.signal })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = (await r.json()) as ConversationsPage;
        setRows(body.conversations);
        setNextOffset(body.page.next_offset);
        setError(null);
        setLoading(false);
      })
      .catch((e) => {
        if ((e as DOMException)?.name === 'AbortError') return;
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
      const r = await fetch(`/api/conversations?sort=recent&limit=${PAGE}&offset=${nextOffset}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = (await r.json()) as ConversationsPage;
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
