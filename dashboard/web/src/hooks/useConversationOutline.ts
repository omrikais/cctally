import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import type { ConversationOutline } from '../types/conversation';

// #177 S5 — full-session outline + stats. Owns its OWN SSE tick subscription
// (Codex F3: useConversation only tail-polls once fully paged), with the same
// coalescing discipline as pollTail: one in-flight fetch, a tick that lands
// mid-fetch replays exactly once after it settles. A fetch error degrades
// gracefully ({outline: null, error}); the reader itself is unaffected. A
// stale-session response (session switched mid-fetch) is dropped, never exposed.
// #227 — `revalidateOnTick` (default true) gates the per-SSE-tick refetch. The
// reader/OutlinePanel keep the default (a live session's outline must track
// growth); ComparisonView passes false so its two finished-run snapshots open
// once and don't re-fetch on every dashboard tick (the comparison never
// live-tails by design).
export function useConversationOutline(
  sessionId: string | null,
  opts?: { revalidateOnTick?: boolean },
) {
  const revalidateOnTick = opts?.revalidateOnTick ?? true;
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
    } catch {
      // Deliberate no-AbortController choice (#184): the single-in-flight guard
      // (`fetchingRef`) already prevents overlapping requests, and the
      // `sessionRef.current !== sid` check below drops any stale response a
      // session switch left in flight — so there is no fetch to abort and no
      // AbortError to special-case. A genuine fetch failure for the CURRENT
      // session degrades to the inline error banner.
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
    // #227 — skip the SSE-tick revalidation entirely when the caller opted out
    // (ComparisonView's static two-run snapshot). The initial-load effect above
    // is unaffected, so a non-revalidating caller still gets its first fetch.
    if (revalidateOnTick && outlineRef.current) void refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generatedAt, revalidateOnTick]);

  return { outline, loading, error };
}
