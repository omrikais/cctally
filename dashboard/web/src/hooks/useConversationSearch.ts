import { useEffect, useState } from 'react';
import type { ConversationSearchResult, SearchHit } from '../types/conversation';

// Debounced cross-session search. Empty/whitespace needle → no fetch,
// empty hits. 200ms debounce. v1 shows the first page (limit 50); the
// rail's "load more" can extend via offset using `total`. `mode` (fts |
// like) lets the rail show a subtle "(basic search)" hint on LIKE.
export interface UseConversationSearch {
  hits: SearchHit[];
  mode: 'fts' | 'like' | null;
  total: number;
  loading: boolean;
  error: string | null;
}

const DEBOUNCE_MS = 200;

export function useConversationSearch(query: string): UseConversationSearch {
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [mode, setMode] = useState<'fts' | 'like' | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const q = query.trim();

  useEffect(() => {
    if (!q) {
      setHits([]); setMode(null); setTotal(0); setLoading(false); setError(null);
      return;
    }
    setLoading(true);
    const ctl = new AbortController();
    const t = setTimeout(() => {
      fetch(`/api/conversation/search?q=${encodeURIComponent(q)}&limit=50&offset=0`, { signal: ctl.signal })
        .then(async (r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const body = (await r.json()) as ConversationSearchResult;
          setHits(body.hits); setMode(body.mode); setTotal(body.total);
          setError(null); setLoading(false);
        })
        .catch((e) => {
          if ((e as DOMException)?.name === 'AbortError') return;
          setError('Search failed.'); setLoading(false);
        });
    }, DEBOUNCE_MS);
    return () => { clearTimeout(t); ctl.abort(); };
  }, [q]);

  return { hits, mode, total, loading, error };
}
