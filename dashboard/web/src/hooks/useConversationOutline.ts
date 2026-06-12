import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import type { ConversationOutline } from '../types/conversation';

// #177 S5 — full-session outline + stats. Owns its OWN SSE tick subscription
// (Codex F3: useConversation only tail-polls once fully paged), with the same
// coalescing discipline as pollTail: one in-flight fetch, a tick that lands
// mid-fetch replays exactly once after it settles. A fetch error degrades
// gracefully ({outline: null, error}); the reader itself is unaffected. A
// stale-session response (session switched mid-fetch) is dropped, never exposed.
export function useConversationOutline(sessionId: string | null) {
  const [outline, setOutline] = useState<ConversationOutline | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessionRef = useRef(sessionId);
  const outlineRef = useRef<ConversationOutline | null>(null);
  const fetchingRef = useRef(false);
  const pendingRef = useRef(false);

  const refetch = useCallback(async () => {
    // Coalesce a tick that lands mid-fetch into ONE trailing replay (the
    // `finally` re-invokes once pendingRef is set). Never concurrent requests.
    if (fetchingRef.current) { pendingRef.current = true; return; }
    const sid = sessionRef.current;
    if (!sid) return;
    fetchingRef.current = true;
    try {
      const body = await fetchJson<ConversationOutline>(
        `/api/conversation/${encodeURIComponent(sid)}/outline`);
      if (sessionRef.current !== sid) return;   // session switched mid-fetch — drop
      outlineRef.current = body;
      setOutline(body); setError(null); setLoading(false);
    } catch (e) {
      if (isAbortError(e)) return;
      if (sessionRef.current !== sid) return;
      setError("Couldn't load the outline."); setLoading(false);
    } finally {
      fetchingRef.current = false;
      if (pendingRef.current) { pendingRef.current = false; void refetch(); }
    }
  }, []);

  useEffect(() => {
    sessionRef.current = sessionId;
    outlineRef.current = null;
    // Clear the in-flight/coalesce guards on a session switch: a fetch still in
    // flight for the OLD session must not block the NEW session's fetch (its
    // late response is dropped by the sessionRef guard, and the pending-replay
    // would otherwise re-issue against the new session anyway). Without this the
    // new session would stall behind a never-resolving stale fetch.
    fetchingRef.current = false;
    pendingRef.current = false;
    setOutline(null); setError(null);
    if (!sessionId) { setLoading(false); return; }
    setLoading(true);
    void refetch();
  }, [sessionId, refetch]);

  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  useEffect(() => {
    if (outlineRef.current) void refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generatedAt]);

  return { outline, loading, error };
}
