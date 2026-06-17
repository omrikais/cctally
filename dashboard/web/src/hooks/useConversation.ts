import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { fetchJson, HttpError, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import { getState, selectLiveTailEnabled, subscribeStore } from '../store/store';
import type { ConversationDetail, ConversationItem } from '../types/conversation';

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
  loadToEnd: () => Promise<void>;
}

const PAGE = 500;
// §6 (Bug 1) — the live-tail overlap window. Each tail tick re-fetches the last
// TAIL_WINDOW local items (cursor = the item just BEFORE the window) so a later
// fold/update into an already-delivered item reaches the live client (the strict
// after-last append could only ever surface NEW turns, never an in-place
// mutation the kernel folds into an earlier item). ≈10 covers the realistic fold
// distance (a skill body lands a beat after its chip). A fold further back than
// this is documented (vitest) and not picked up — widen here if it proves too
// tight. Items OUTSIDE the window are never touched (earlier pages preserved).
const TAIL_WINDOW = 10;

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

  const loadToEnd = useCallback(async () => {
    // Uncapped forward pager for the explicit "Jump to latest" action (spec §5,
    // Codex P1 #2). loadUntil caps at 20 pages; this drains ALL pages so the
    // final anchor is always reachable past the cap. Driven by an explicit user
    // action with a loading state, so the unbounded loop is acceptable —
    // forward-only, and fetchNext returns falsy the moment the cursor is null
    // (page exhausted) OR a guard/error trips (after==null, session changed,
    // overlapping load, or a failed fetch), so it cannot infinite-loop.
    for (;;) {
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
  // until next_after is null or nothing new appended), coalesces a tick that
  // arrives mid-fetch (pendingTickRef replay after `finally`), and refreshes the
  // whole-session header on EVERY response — including empty ones (no new turns,
  // but cost/models may have changed). Stored page.next_after stays null while
  // live-tailing so the reader never re-enters pagination.
  //
  // §6 (Bug 1) — OVERLAP UPSERT. The cursor is the item just BEFORE a small
  // recent window (TAIL_WINDOW items back), NOT the strict last item, so the
  // server's `after=` re-returns the window itself. We then MERGE the refreshed
  // window into the accumulator by `anchor.id`:
  //   • items OUTSIDE the window pass through untouched (earlier pages safe);
  //   • an in-window item still returned is REPLACED in place (a fold/update the
  //     kernel folded into an already-delivered item — e.g. a skill body folded
  //     into its chip — which the old strict-append could never surface);
  //   • an in-window item ABSENT from the refresh is DELETED (Codex P1-E:
  //     Phase-4b drops a standalone body once folded, an orphan tool_result drops
  //     once its tool_use pairs) — deletion is bounded to the window;
  //   • a genuinely-new id (not previously held) is APPENDED in order.
  // The merge runs against the SAME accumulator each drain iteration (we re-read
  // detailRef.current?.items at the top of every pass), so a burst still drains.
  const pollTail = useCallback(async () => {
    if (pollingRef.current) { pendingTickRef.current = true; return; }  // coalesce a mid-fetch tick
    const sid = sessionRef.current;
    if (!sid || hasMoreRef.current || loadingMoreRef.current) return;   // only at the tail, never racing loadMore
    pollingRef.current = true;
    try {
      for (let i = 0; i < 50; i++) {                                    // drain a >PAGE burst within one tick
        const items = detailRef.current?.items ?? [];
        if (!items.length) break;
        // The window = the last TAIL_WINDOW local items; the cursor is the item
        // just BEFORE it (no `after` when the whole accumulator fits the window,
        // so the server re-returns everything). Position-based split for
        // robustness; id-based membership for the merge below.
        const splitIdx = Math.max(0, items.length - TAIL_WINDOW);
        const cursor = splitIdx > 0 ? items[splitIdx - 1].anchor.id : null;
        let body: ConversationDetail;
        try {
          const q = `/api/conversation/${encodeURIComponent(sid)}?limit=${PAGE}`
            + (cursor != null ? `&after=${cursor}` : '');
          body = await fetchJson<ConversationDetail>(q);
        } catch {
          break;                                                        // transient blip — keep what we have
        }
        if (sessionRef.current !== sid) return;                         // session switched mid-fetch

        const returned = body.items;
        // Empty response carries NO fold information (a re-priced turn with no new
        // items, or a stale/transient blip) — never let it delete the window.
        // Preserve the accumulator unchanged and just refresh the whole-session
        // header (#193 / #175 F4 empty-tail-header-refresh). The overlap window is
        // always re-returned by a real `?after=<windowCursor>` response, so a
        // legitimate fold/delete arrives non-empty.
        let merged: ConversationItem[];
        let appended = 0;
        if (returned.length === 0) {
          merged = items;
        } else {
          const byId = new Map(returned.map((r) => [r.anchor.id, r] as const));
          const prevIds = new Set(items.map((it) => it.anchor.id));
          const head = items.slice(0, splitIdx);                        // outside window — untouched
          const windowItems = items.slice(splitIdx);                    // eligible for replace/delete
          merged = [...head];
          for (const it of windowItems) {
            const fresh = byId.get(it.anchor.id);
            if (fresh !== undefined) merged.push(fresh);                // replace in place (fold/update)
            // else: folded away (Phase-4b drop / orphan pairing) → DELETE from window
          }
          for (const r of returned) {                                   // genuinely-new ids → append in order
            if (!prevIds.has(r.anchor.id)) { merged.push(r); appended += 1; }
          }
        }

        setDetailSynced((prev) => (prev ? {
          ...prev,
          items: merged,
          cost_usd: body.cost_usd, models: body.models,                // refresh whole-session header even on empty
          title: body.title ?? prev.title,                            // #193 P1-4: a rewritten ai-title reaches the open reader
          git_branch: body.git_branch, project_label: body.project_label,
          subagent_meta: body.subagent_meta ?? prev.subagent_meta,
          page: prev.page,                                             // stays fully-paged (next_after === null)
        } : prev));
        // Stop the burst-drain once nothing new was appended (folds applied, no
        // unseen turns) or the cursor is fully drained — never re-enter paging.
        if (appended === 0 || body.page.next_after == null) break;
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

  // Live-tail (spec §3.1): a dedicated per-conversation EventSource that fires
  // pollTail() the instant the server sees this session's JSONL grow — far
  // faster than the 5s generated_at backstop above (which stays as the slow
  // fallback). The same fully-paged / coalescing guards inside pollTail() apply,
  // so this never races pagination or double-fetches. Gated on transcriptsEnabled
  // (the reader isn't shown otherwise → avoids a 403 reconnect loop) and the
  // dashboard.live_tail opt-out (selectLiveTailEnabled, absence = ON).
  const transcriptsEnabled = env?.transcriptsEnabled ?? false;
  const liveTailEnabled = useSyncExternalStore(subscribeStore, () => selectLiveTailEnabled(getState()));
  useEffect(() => {
    if (!sessionId || !transcriptsEnabled || !liveTailEnabled) return;
    // EventSource is universal in real browsers but absent in some runtimes
    // (JSDOM, SSR). Degrade silently to the generated_at backstop above when
    // it's missing rather than throwing on mount.
    if (typeof EventSource === 'undefined') return;
    const es = new EventSource(`/api/conversation/${encodeURIComponent(sessionId)}/events`);
    es.addEventListener('tail', () => { void pollTail(); });
    es.addEventListener('open', () => { void pollTail(); });  // (re)connect catch-up
    return () => es.close();
    // pollTail is ref-stable (refs + setDetailSynced).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, transcriptsEnabled, liveTailEnabled]);

  return { detail: exposedDetail, loading: exposedLoading, error, hasMore, loadMore, loadUntil, loadToEnd };
}
