import { useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { useDebouncedValue } from './useDebouncedValue';
import type { ConversationFindResult, FindAnchor } from '../types/conversation';

// #177 S6 — in-conversation find. A debounced, session-scoped fetch to
// /api/conversation/<id>/find returning the full ordered rendered-turn anchor
// list (find walks it via the reader's loadUntil(uuid) + jump machinery).
// Mirrors useConversationSearch's debounce/abort skeleton (200ms, seeded ''
// so the first keystroke still debounces, ONE shared AbortController so a
// newer needle aborts the older in-flight fetch and a late prior response can
// never commit). Point-in-time: NO live-tail revalidation — the fetch keys on
// the debounced needle only, never on the reader's growing items, so a tick
// does not silently re-run the query (spec §4).
export interface UseConversationFind {
  anchors: FindAnchor[];
  total: number;
  truncated: boolean;
  mode: 'fts' | 'like' | null;
  loading: boolean;
  error: string | null;
}

const DEBOUNCE_MS = 200;

export function useConversationFind(sessionId: string, needle: string): UseConversationFind {
  const [anchors, setAnchors] = useState<FindAnchor[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [mode, setMode] = useState<'fts' | 'like' | null>(null);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const q = needle.trim();
  const debouncedQ = useDebouncedValue(q, DEBOUNCE_MS, '');
  // ONE controller ref: any needle change aborts whatever it points at, so a
  // stale response can never commit over the newer query's state.
  const ctlRef = useRef<AbortController | null>(null);

  // Reset on an empty needle, and abort any in-flight fetch the instant the raw
  // needle changes (so a prior response can't commit late).
  useEffect(() => {
    if (!q) { setAnchors([]); setTotal(0); setTruncated(false); setMode(null); setError(null); }
    return () => { ctlRef.current?.abort(); };
  }, [q]);

  // Keyed on the settled needle (NOT on any reader/live-tail state — find is a
  // point-in-time snapshot).
  useEffect(() => {
    if (!debouncedQ) { setFetching(false); return; }
    const ctl = new AbortController();
    ctlRef.current = ctl;
    setFetching(true);
    const url = `/api/conversation/${encodeURIComponent(sessionId)}/find?q=${encodeURIComponent(debouncedQ)}`;
    fetchJson<ConversationFindResult>(url, ctl.signal)
      .then((body) => {
        setAnchors(body.anchors);
        setTotal(body.total);
        setTruncated(body.anchors_truncated);
        setMode(body.mode);
        setError(null);
        setFetching(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        // #177 S6 M5 — clear the prior result on a real failure so a failing
        // refetch can't leave the bar navigating stale anchors. Without this,
        // anchors/total/mode/truncated from the last successful query survive
        // and the n/N cursor would still walk matches that may no longer hold.
        setAnchors([]); setTotal(0); setTruncated(false); setMode(null);
        setError('Find failed.'); setFetching(false);
      });
    return () => ctl.abort();
  }, [sessionId, debouncedQ]);

  // Derived, mirroring useConversationSearch: true while a non-empty needle's
  // results aren't ready (debounce lag or fetch in flight).
  const loading = q !== '' && (q !== debouncedQ || fetching);

  return { anchors, total, truncated, mode, loading, error };
}
