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
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const q = query.trim();
  const debouncedQ = useDebouncedValue(q, DEBOUNCE_MS, '');
  const ctlRef = useRef<AbortController | null>(null);

  // Reset on an empty needle, and abort any in-flight fetch the instant the raw
  // needle changes (incl. clear) — so a prior-needle response can never commit
  // late over the newer needle's state.
  useEffect(() => {
    if (!q) { setHits([]); setMode(null); setTotal(0); setError(null); }
    return () => { ctlRef.current?.abort(); };
  }, [q]);

  // Fetch, keyed on the settled needle.
  useEffect(() => {
    if (!debouncedQ) { setFetching(false); return; }
    const ctl = new AbortController();
    ctlRef.current = ctl;
    setFetching(true);
    fetchJson<ConversationSearchResult>(
      `/api/conversation/search?q=${encodeURIComponent(debouncedQ)}&limit=50&offset=0`,
      ctl.signal,
    )
      .then((body) => {
        setHits(body.hits); setMode(body.mode); setTotal(body.total);
        setError(null); setFetching(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        setError('Search failed.'); setFetching(false);
      });
    return () => ctl.abort();
  }, [debouncedQ]);

  // `loading` is DERIVED, not imperatively set: true while a non-empty needle's
  // results aren't ready — either the debounce hasn't caught up to the typed
  // needle (q !== debouncedQ, the immediate-feedback case) or the settled-needle
  // fetch is in flight. Deriving it avoids a stuck-true state when the needle
  // oscillates back to an already-settled value within the debounce window
  // (debouncedQ never changes, so no fetch re-fires to clear an imperative flag).
  const loading = q !== '' && (q !== debouncedQ || fetching);

  return { hits, mode, total, loading, error };
}
