import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, HttpError, isAbortError } from '../lib/fetchJson';
import type { ConversationDetail } from '../types/conversation';

// Paginated reader. UNLIKE useProjectDetail/useConversations, this does
// NOT revalidate on SSE tick: a past transcript is immutable history
// (live-tailing is an explicit non-goal), and refetching page 1 each
// tick would clobber the accumulated lazy-loaded pages. We fetch page 1
// on sessionId change and APPEND on loadMore()/loadUntil(). Header
// fields (cost_usd, models, …) are whole-session and come from any page;
// we keep the first page's header and only grow `items`.
export interface UseConversation {
  detail: ConversationDetail | null;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => Promise<void>;
  loadUntil: (uuid: string) => Promise<void>;
}

const PAGE = 500;

export function useConversation(sessionId: string | null): UseConversation {
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nextAfterRef = useRef<number | null>(null);
  const loadingMoreRef = useRef(false);
  const sessionRef = useRef<string | null>(null);
  // Synchronous mirror of `detail` so loadUntil() can poll the latest
  // accumulated items without a re-render dependency or a stale closure
  // (the plan's sanctioned alternative to the setDetail((s)=>…) probe).
  const detailRef = useRef<ConversationDetail | null>(null);
  const setDetailSynced = useCallback(
    (next: ConversationDetail | null | ((prev: ConversationDetail | null) => ConversationDetail | null)) => {
      setDetail((prev) => {
        const value = typeof next === 'function' ? next(prev) : next;
        detailRef.current = value;
        return value;
      });
    },
    [],
  );

  useEffect(() => {
    sessionRef.current = sessionId;
    if (!sessionId) { setDetailSynced(null); setLoading(false); setError(null); nextAfterRef.current = null; return; }
    setLoading(true); setError(null); setDetailSynced(null); nextAfterRef.current = null;
    const ctl = new AbortController();
    fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sessionId)}?limit=${PAGE}`, ctl.signal)
      .then((body) => {
        setDetailSynced(body);
        nextAfterRef.current = body.page.next_after;
        setLoading(false);
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        if (e instanceof HttpError && e.status === 404) { setError('Conversation not found.'); setLoading(false); return; }
        setError("Couldn't load the conversation."); setLoading(false);
      });
    return () => ctl.abort();
    // sessionId only — NOT generated_at (immutable transcript).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, setDetailSynced]);

  const fetchNext = useCallback(async (): Promise<boolean> => {
    const after = nextAfterRef.current;
    const sid = sessionRef.current;
    if (after == null || sid == null || loadingMoreRef.current) return false;
    loadingMoreRef.current = true;
    try {
      let body: ConversationDetail;
      try {
        body = await fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sid)}?limit=${PAGE}&after=${after}`);
      } catch {
        return false;
      }
      if (sessionRef.current !== sid) return false;  // session changed mid-fetch — drop this stale page
      setDetailSynced((prev) => (prev ? { ...prev, items: [...prev.items, ...body.items], page: body.page } : body));
      nextAfterRef.current = body.page.next_after;
      return body.page.next_after != null;
    } finally {
      loadingMoreRef.current = false;
    }
  }, [setDetailSynced]);

  const loadMore = useCallback(async () => { await fetchNext(); }, [fetchNext]);

  const loadUntil = useCallback(async (uuid: string) => {
    // Page until an item whose member_uuids includes the target, or
    // until exhausted. Guards against a runaway loop with a hard cap.
    // We read the synchronous `detailRef` mirror (not React state) so the
    // loaded-yet? check always sees the latest accumulated items.
    const has = () => {
      const s = detailRef.current;
      return !!s && s.items.some((it) => it.member_uuids.includes(uuid));
    };
    for (let i = 0; i < 20; i++) {
      if (has()) return;
      const more = await fetchNext();
      if (!more) return;
    }
  }, [fetchNext]);

  return { detail, loading, error, hasMore: detail?.page?.next_after != null, loadMore, loadUntil };
}
