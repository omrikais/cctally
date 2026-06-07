// dashboard/web/src/hooks/useConversationSearch.ts
import { useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { useDebouncedValue } from './useDebouncedValue';
import type { ConversationSearchResult, SearchHit } from '../types/conversation';

// Debounced cross-session search. Empty/whitespace needle -> no fetch, empty
// hits. 200ms debounce (even on the first keystroke: the rail mounts this hook
// with a non-empty needle, so useDebouncedValue is seeded with '' to defer it).
// v1 shows the first page (limit 50); `total` lets the rail extend via offset.
// `mode` (fts | like) lets the rail show a subtle "(basic search)" hint on LIKE.
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
  const debouncedQ = useDebouncedValue(q, DEBOUNCE_MS, '');
  const ctlRef = useRef<AbortController | null>(null);

  // (1) Immediate, keyed on the raw needle: empty -> synchronous reset;
  //     non-empty -> show loading. Cleanup aborts any in-flight fetch the
  //     instant the needle changes (incl. clear), so a prior-needle response
  //     can never commit late over the newer needle's state.
  useEffect(() => {
    if (!q) { setHits([]); setMode(null); setTotal(0); setLoading(false); setError(null); }
    else { setLoading(true); }
    return () => { ctlRef.current?.abort(); };
  }, [q]);

  // (2) Fetch, keyed on the settled needle.
  useEffect(() => {
    if (!debouncedQ) return;
    const ctl = new AbortController();
    ctlRef.current = ctl;
    fetchJson<ConversationSearchResult>(
      `/api/conversation/search?q=${encodeURIComponent(debouncedQ)}&limit=50&offset=0`,
      ctl.signal,
    )
      .then((body) => {
        setHits(body.hits); setMode(body.mode); setTotal(body.total);
        setError(null); setLoading(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setError('Search failed.'); setLoading(false);
      });
    return () => ctl.abort();
  }, [debouncedQ]);

  return { hits, mode, total, loading, error };
}
