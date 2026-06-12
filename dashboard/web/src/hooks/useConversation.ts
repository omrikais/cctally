import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, HttpError, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import type { ConversationDetail } from '../types/conversation';

// Paginated reader. We fetch page 1 on sessionId change and APPEND on
// loadMore()/loadUntil(). Header fields (cost_usd, models, …) are
// whole-session and come from any page.
//
// #175 F4 — live-tail the OPEN conversation at the tail: once history is
// fully paged (hasMore === false), every new SSE `generated_at` tick
// tail-polls `?after=<lastItemId>`, drains any burst, appends the new
// turns, and refreshes the whole-session header totals (so live
// cost/models update too). It deliberately does NOT revalidate page 1 on
// a tick (that would clobber the accumulated pages); the tail-poll only
// surfaces NEW turns past the last loaded item.
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
  // #183 — the session id the current `detail` was loaded FOR. Set atomically
  // with the page-1 `setDetail(body)` in the fetch `.then`. The render derives
  // `detailMatches` from it (see below) so the previous session's `detail` is
  // never exposed under a newer `sessionId` during the cross-session transient.
  // State (not a ref) so the page-1 resolve re-renders with the match visible.
  const [loadedSessionId, setLoadedSessionId] = useState<string | null>(null);
  const nextAfterRef = useRef<number | null>(null);
  const loadingMoreRef = useRef(false);
  const sessionRef = useRef<string | null>(null);
  // #175 F4 live-tail bookkeeping. `hasMoreRef` mirrors hasMore so pollTail can
  // read it synchronously; `pollingRef` serializes the tail poll; `pendingTickRef`
  // records a tick that arrived mid-fetch so it can be replayed once.
  const hasMoreRef = useRef(false);
  const pollingRef = useRef(false);
  const pendingTickRef = useRef(false);
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
    if (!sessionId) { setDetailSynced(null); setLoadedSessionId(null); setLoading(false); setError(null); nextAfterRef.current = null; return; }
    setLoading(true); setError(null); setDetailSynced(null); nextAfterRef.current = null;
    const ctl = new AbortController();
    fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sessionId)}?limit=${PAGE}`, ctl.signal)
      .then((body) => {
        setDetailSynced(body);
        setLoadedSessionId(sessionId);   // #183 — stamp which session this detail is for
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
      // #166: keep the whole-session subagent_meta map across paged appends —
      // every page carries it, but fall back to the prior page's map so a later
      // page never drops it. The first-load path stores the full body already.
      setDetailSynced((prev) => (prev ? { ...prev, items: [...prev.items, ...body.items], page: body.page,
        subagent_meta: body.subagent_meta ?? prev.subagent_meta } : body));
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

  // #183 — derive (don't sync) the cross-session reset. The fetch effect clears
  // `detail` only AFTER the commit (passive phase), so the render right after
  // `sessionId` changes still returns the PREVIOUS session's `detail` for one
  // commit — while TranscriptContext already carries the new sessionId. An
  // auto-fetching MediaFigure then builds `/<newSid>/media?tool_use_id=<oldId>`
  // and 404s (console-visible on every cross-session switch once a screenshot
  // loaded). A `setState`-during-render reset does NOT fix it: it merely
  // schedules a second render, but the FIRST render of the new session still
  // returns the stale value. The robust fix DERIVES the exposed detail in the
  // same render pass: only surface `detail` when it was loaded FOR the requested
  // session (`loadedSessionId === sessionId`, stamped atomically with the
  // page-1 `setDetail`); otherwise present as the loading state, so the reader
  // falls into its existing "Loading conversation…" branch for the transient
  // instead of painting stale items under the new context. `detailRef` /
  // `sessionRef` (live-tail + pagination bookkeeping) are untouched — only the
  // React-visible surface is gated.
  const detailMatches = detail != null && loadedSessionId === sessionId;
  const exposedDetail = detailMatches ? detail : null;
  // While `detail` belongs to a stale session (the cross-session transient) and
  // no error has surfaced for the new one, present as loading so the reader
  // shows "Loading conversation…". `error` is passed through unchanged — gating
  // it could swallow a real not-found for the CURRENT session (whose `detail`
  // is also null, so `detailMatches` is false), and a stale error transient is
  // text-only, not a media 404.
  const exposedLoading = sessionId != null && !detailMatches && error == null ? true : loading;

  const hasMore = exposedDetail?.page?.next_after != null;
  hasMoreRef.current = hasMore;

  // #175 F4 — tail-poll the open conversation. Runs ONLY while already fully
  // paged (hasMore false), so the conversation stays fully-paged and the cursor
  // walks forward each tick. Drains a >PAGE burst within one tick (bounded loop
  // until next_after is null or items empty), coalesces a tick that arrives
  // mid-fetch (pendingTickRef replay after `finally`), and refreshes the
  // whole-session header on EVERY response — including empty ones (no new turns,
  // but cost/models may have changed). Stored page.next_after stays null while
  // live-tailing so the reader never re-enters pagination.
  const pollTail = useCallback(async () => {
    if (pollingRef.current) { pendingTickRef.current = true; return; }  // coalesce a mid-fetch tick
    const sid = sessionRef.current;
    if (!sid || hasMoreRef.current || loadingMoreRef.current) return;   // only at the tail, never racing loadMore
    pollingRef.current = true;
    try {
      for (let i = 0; i < 50; i++) {                                    // drain a >PAGE burst within one tick
        const last = detailRef.current?.items.at(-1);
        if (!last) break;
        let body: ConversationDetail;
        try {
          body = await fetchJson<ConversationDetail>(
            `/api/conversation/${encodeURIComponent(sid)}?limit=${PAGE}&after=${last.anchor.id}`);
        } catch {
          break;                                                        // transient blip — keep what we have
        }
        if (sessionRef.current !== sid) return;                         // session switched mid-fetch
        setDetailSynced((prev) => (prev ? {
          ...prev,
          items: body.items.length ? [...prev.items, ...body.items] : prev.items,
          cost_usd: body.cost_usd, models: body.models,                // refresh whole-session header even on empty
          git_branch: body.git_branch, project_label: body.project_label,
          subagent_meta: body.subagent_meta ?? prev.subagent_meta,
          page: prev.page,                                             // stays fully-paged (next_after === null)
        } : prev));
        if (!body.items.length || body.page.next_after == null) break;  // empty (no new / stale cursor) or fully drained
      }
    } finally {
      pollingRef.current = false;
      if (pendingTickRef.current) { pendingTickRef.current = false; void pollTail(); }  // replay one coalesced tick
    }
  }, [setDetailSynced]);

  // Trigger on each SSE tick, but only while fully paged.
  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  useEffect(() => {
    if (detailRef.current && !hasMoreRef.current) void pollTail();
    // generatedAt only — pollTail is stable (refs + setDetailSynced).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generatedAt]);

  return { detail: exposedDetail, loading: exposedLoading, error, hasMore, loadMore, loadUntil };
}
