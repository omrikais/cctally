import { useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError, HttpError } from '../lib/fetchJson';
import { useDebouncedValue } from './useDebouncedValue';
import type { ConversationFindResult, FindAnchor } from '../types/conversation';

// #177 S6 — in-conversation find. A debounced, session-scoped fetch to
// /api/conversation/<id>/find returning the full ordered rendered-turn anchor
// list (find walks it via the reader's loadToTarget(uuid) + jump machinery).
// Mirrors useConversationSearch's debounce/abort skeleton (200ms, seeded ''
// so the first keystroke still debounces, ONE shared AbortController so a
// newer needle aborts the older in-flight fetch and a late prior response can
// never commit).
//
// #217 S4 / I-1 power features:
//  - `regex` / `case` append `&regex=1` / `&case=1`; the fetch effect keys on
//    them so flipping a toggle re-runs the query.
//  - an invalid-regex 400 (HttpError(400) — `fetchJson` discards the body) maps
//    client-side to error === 'invalid regex' (matches cleared).
//  - `tailRevision` is a fetch dependency (DEBOUNCED via the existing 200ms
//    debounce) so live-tail growth re-runs the query — keyed on the monotonic
//    pollTail counter, NOT items.length (Codex P1, see useConversation).
export interface UseConversationFind {
  anchors: FindAnchor[];
  total: number;
  truncated: boolean;
  mode: 'fts' | 'like' | 'regex' | null;
  loading: boolean;
  error: string | null;
}

export interface UseConversationFindOptions {
  regex?: boolean;
  case?: boolean;
  // The monotonic live-tail merge counter from useConversation; a bump re-runs
  // the find query (debounced). Omitted → point-in-time (no live-refresh).
  tailRevision?: number;
}

const DEBOUNCE_MS = 200;

export function useConversationFind(
  sessionId: string,
  needle: string,
  opts: UseConversationFindOptions = {},
): UseConversationFind {
  const { regex = false, case: caseSensitive = false, tailRevision = 0 } = opts;
  const [anchors, setAnchors] = useState<FindAnchor[]>([]);
  const [total, setTotal] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [mode, setMode] = useState<'fts' | 'like' | 'regex' | null>(null);
  const [fetching, setFetching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const q = needle.trim();
  const debouncedQ = useDebouncedValue(q, DEBOUNCE_MS, '');
  // Debounce the live-tail revision too so a burst of ticks coalesces into one
  // refetch (same 200ms window as the needle).
  const debouncedRev = useDebouncedValue(tailRevision, DEBOUNCE_MS, 0);
  // ONE controller ref: any needle change aborts whatever it points at, so a
  // stale response can never commit over the newer query's state.
  const ctlRef = useRef<AbortController | null>(null);

  // Reset on an empty needle, and abort any in-flight fetch the instant the raw
  // needle changes (so a prior response can't commit late).
  useEffect(() => {
    if (!q) { setAnchors([]); setTotal(0); setTruncated(false); setMode(null); setError(null); }
    return () => { ctlRef.current?.abort(); };
  }, [q]);

  // Keyed on the settled needle + regex/case toggles + the debounced live-tail
  // revision (a tail bump re-runs the query against the grown corpus).
  useEffect(() => {
    if (!debouncedQ) { setFetching(false); return; }
    const ctl = new AbortController();
    ctlRef.current = ctl;
    setFetching(true);
    let url = `/api/conversation/${encodeURIComponent(sessionId)}/find?q=${encodeURIComponent(debouncedQ)}`;
    if (regex) url += '&regex=1';
    if (caseSensitive) url += '&case=1';
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
        // refetch can't leave the bar navigating stale anchors.
        setAnchors([]); setTotal(0); setTruncated(false); setMode(null);
        // #217 S4 / I-1.3 — a 400 from the regex pre-validation is an invalid
        // pattern. `fetchJson` throws HttpError(status) and discards the body
        // (Codex P2), so the actionable message is reconstructed client-side
        // rather than read from the server's `{"error":"invalid regex…"}`.
        // Both strings are rendered verbatim by FindBar (role="alert"), so they
        // must match the UI wording exactly — keep them lowercase + un-punctuated.
        setError(e instanceof HttpError && e.status === 400 ? 'invalid regex' : 'find failed');
        setFetching(false);
      });
    return () => ctl.abort();
  }, [sessionId, debouncedQ, regex, caseSensitive, debouncedRev]);

  // Derived, mirroring useConversationSearch: true while a non-empty needle's
  // results aren't ready (debounce lag or fetch in flight).
  const loading = q !== '' && (q !== debouncedQ || fetching);

  return { anchors, total, truncated, mode, loading, error };
}
