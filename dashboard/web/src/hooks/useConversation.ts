import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { fetchJson, HttpError, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import { getState, selectLiveTailEnabled, subscribeStore } from '../store/store';
import { buildOutlineTargets, resolveTurnIndex } from '../conversations/outlineNavigation';
import type { ConversationDetail, ConversationItem, OpenIntent, OutlineTurn } from '../types/conversation';

// #217 S3 E2 — the bidirectional windowed reader pager. The reader holds ONE
// contiguous window with TWO first-class edge cursors: `prevBeforeRef` (the TOP
// edge, for reverse paging via `?before=`) and `nextAfterRef` (the BOTTOM edge,
// for forward paging via `?after=` + the live-tail gate). Open-at-bottom loads
// the tail (`?tail=1`); scroll-up prepends (scroll-anchored, in the reader);
// scroll-down appends. `loadToTarget(uuid)` pages from the NEAREST edge toward a
// target resolved via the full-session outline index (head-ward if early,
// tail-ward if late) until it is in-window. `jumpToLatest` is a `?tail=1` reset.
//
// The hook OWNS the initial fetch (Codex P1): it takes an `openIntent` (deep-link
// anchor / saved reading-position uuid / tail) so its FIRST request is already
// precedence-correct — no head-fetch-then-redirect flash. With no `openIntent`
// (legacy callers) it keeps the previous head-fetch (`?limit=500`).
//
// #175 F4 — live-tail the OPEN conversation at the tail: once history is fully
// paged at the BOTTOM edge (hasMore === false), every new SSE `generated_at`
// tick (and the dedicated per-conversation EventSource) tail-polls
// `?after=<lastItemId>`, drains any burst, upserts the overlap window, and
// refreshes the whole-session header. Codex P1: a `before` page must NEVER touch
// the bottom edge — `hasMore` / the live-tail gate key on the bottom edge only,
// else a tail-opened reader would look "not at tail" and live-tail would die.
export interface UseConversation {
  detail: ConversationDetail | null;
  loading: boolean;
  error: string | null;
  // BOTTOM edge: more forward pages exist (also the live-tail gate).
  hasMore: boolean;
  // TOP edge (#217 S3 E2): more reverse pages exist.
  hasPrev: boolean;
  // The current top-edge cursor (the id for the next `?before=`), or null.
  prevBefore: number | null;
  // #217 S3 E2 — where the reader should land on open (computed from the FIRST
  // response's `has_prev`): 'bottom' for a multi-page tail open, 'top' for a
  // single-page session (everything fits one page → read from the start). null
  // until the first page resolves, or when the open was a deep-link/restore
  // anchor (the jump pipeline drives the scroll then).
  openScrollIntent: 'top' | 'bottom' | null;
  loadMore: () => Promise<void>;
  // Resolves to whether the reverse page actually PREPENDED ≥1 item (false for a
  // no-op: null cursor, stale cursor → empty page, or a fetch error). The reader
  // uses this to clear its scroll-anchor snapshot only on a genuine no-op.
  loadPrev: () => Promise<boolean>;
  loadToTarget: (uuid: string) => Promise<void>;
  jumpToLatest: () => Promise<void>;
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

export interface UseConversationOptions {
  // #217 S3 E2 — the full-session outline turns, for `loadToTarget`'s nearest-edge
  // direction decision + member-uuid resolution. Independent of the loaded
  // page-window, so it always yields a reliable above/below verdict.
  outlineTurns?: OutlineTurn[];
  // #217 S3 E2 — the precedence-resolved open intent. The hook's FIRST request
  // follows it: 'anchor'/'restore' → loadToTarget(uuid); 'tail' → ?tail=1.
  // Omitted/undefined → the legacy head-fetch (?limit=500).
  openIntent?: OpenIntent | null;
}

export function useConversation(sessionId: string | null, opts: UseConversationOptions = {}): UseConversation {
  const { outlineTurns, openIntent } = opts;
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openScrollIntent, setOpenScrollIntent] = useState<'top' | 'bottom' | null>(null);
  // #183 — the session id the current `detail` was loaded FOR (see the derive
  // guard below). State (not a ref) so the page-1 resolve re-renders with it.
  const [loadedSessionId, setLoadedSessionId] = useState<string | null>(null);
  // The TWO edge cursors. `nextAfterRef` = bottom (forward + live-tail gate);
  // `prevBeforeRef` = top (reverse). Each is fed ONLY from its own envelope key.
  const nextAfterRef = useRef<number | null>(null);
  const prevBeforeRef = useRef<number | null>(null);
  const [hasPrev, setHasPrev] = useState(false);
  const hasPrevRef = useRef(false);
  hasPrevRef.current = hasPrev;
  const loadingMoreRef = useRef(false);   // forward (after) overlap guard
  const loadingPrevRef = useRef(false);   // reverse (before) overlap guard
  const sessionRef = useRef<string | null>(null);
  // #175 F4 live-tail bookkeeping.
  const hasMoreRef = useRef(false);
  const pollingRef = useRef(false);
  const pendingTickRef = useRef(false);
  // Synchronous mirror of `detail` so loadToTarget() can poll the latest
  // accumulated items without a re-render dependency or a stale closure.
  const detailRef = useRef<ConversationDetail | null>(null);
  // Live mirror of the outline turns so the ref-stable loadToTarget reads the
  // latest skeleton without re-creating the callback.
  const outlineTurnsRef = useRef<OutlineTurn[] | undefined>(outlineTurns);
  outlineTurnsRef.current = outlineTurns;
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

  // Apply a fetched page as the SOLE window (open / tail / loadToTarget reset),
  // arming BOTH edges from the envelope. Used by the initial fetch + jumpToLatest.
  const applyWindow = useCallback((body: ConversationDetail) => {
    setDetailSynced(body);
    nextAfterRef.current = body.page.next_after;
    prevBeforeRef.current = body.page.prev_before ?? null;
    setHasPrev(body.page.has_prev ?? false);
  }, [setDetailSynced]);

  // ── Initial fetch (hook-owned, precedence-correct — Codex P1) ──────────────
  useEffect(() => {
    sessionRef.current = sessionId;
    if (!sessionId) {
      setDetailSynced(null); setLoadedSessionId(null); setLoading(false); setError(null);
      nextAfterRef.current = null; prevBeforeRef.current = null; setHasPrev(false);
      setOpenScrollIntent(null);
      return;
    }
    setLoading(true); setError(null); setDetailSynced(null);
    nextAfterRef.current = null; prevBeforeRef.current = null; setHasPrev(false);
    setOpenScrollIntent(null);
    const ctl = new AbortController();

    // The FIRST request URL follows the open intent. 'anchor'/'restore' resolve
    // through loadToTarget after a tail fetch (so the window starts at the
    // bottom and pages head-ward to the target); 'tail' (or no intent past the
    // legacy head-fetch) issues ?tail=1 / ?limit=500.
    const intentKind = openIntent?.kind;
    // For an anchor/restore open we still START from the tail (the natural
    // resting place) and let loadToTarget walk to the target; for a bare tail
    // open we land per has_prev; with NO intent we keep the legacy head page.
    const firstUrl = intentKind == null
      ? `/api/conversation/${encodeURIComponent(sessionId)}?limit=${PAGE}`
      : `/api/conversation/${encodeURIComponent(sessionId)}?tail=1&limit=${PAGE}`;

    fetchJson<ConversationDetail>(firstUrl, ctl.signal)
      .then((body) => {
        if (sessionRef.current !== sessionId) return;  // session changed mid-fetch
        applyWindow(body);
        setLoadedSessionId(sessionId);
        setLoading(false);
        if (intentKind === 'tail') {
          // Multi-page (has_prev) ⇒ land at the bottom (live-tail engaged);
          // single page ⇒ scroll to the top (read from the start, Q1).
          setOpenScrollIntent(body.page.has_prev ? 'bottom' : 'top');
        } else if (intentKind == null) {
          // Legacy head-fetch: no scroll intent (the reader keeps its prior
          // top-default behavior).
          setOpenScrollIntent(null);
        }
        // 'anchor'/'restore' leave openScrollIntent null — the jump pipeline
        // drives the scroll. The reader fires loadToTarget(openIntent.uuid)
        // after this resolves.
      })
      .catch((e) => {
        if (isAbortError(e)) return;
        if (e instanceof HttpError && e.status === 404) { setError('Conversation not found.'); setLoading(false); return; }
        setError("Couldn't load the conversation."); setLoading(false);
      });
    return () => ctl.abort();
    // sessionId + the intent KIND/uuid only — NOT generated_at (immutable
    // transcript). A changed intent for the SAME session would re-fetch; in
    // practice the reader sets the intent once per session open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, openIntent?.kind, openIntent && 'uuid' in openIntent ? openIntent.uuid : null, setDetailSynced, applyWindow]);

  // ── Forward paging (bottom edge) ───────────────────────────────────────────
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
      // #166: keep the whole-session subagent_meta map across paged appends.
      // Codex P1 — an `after` page updates ONLY the bottom edge; the top edge
      // (prevBeforeRef / hasPrev) is left untouched.
      setDetailSynced((prev) => (prev ? { ...prev, items: [...prev.items, ...body.items],
        page: { ...prev.page, next_after: body.page.next_after, has_more: body.page.has_more },
        subagent_meta: body.subagent_meta ?? prev.subagent_meta } : body));
      nextAfterRef.current = body.page.next_after;
      return body.page.next_after != null;
    } finally {
      loadingMoreRef.current = false;
    }
  }, [setDetailSynced]);

  const loadMore = useCallback(async () => { await fetchNext(); }, [fetchNext]);

  // ── Reverse paging (top edge) — Codex P1: NEVER touches the bottom edge ─────
  const fetchPrev = useCallback(async (): Promise<boolean> => {
    const before = prevBeforeRef.current;
    const sid = sessionRef.current;
    if (before == null || sid == null || loadingPrevRef.current) return false;
    loadingPrevRef.current = true;
    try {
      let body: ConversationDetail;
      try {
        body = await fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sid)}?limit=${PAGE}&before=${before}`);
      } catch {
        return false;
      }
      if (sessionRef.current !== sid) return false;
      // PREPEND the page and update ONLY the top edge. The before-page envelope
      // legitimately carries next_after / has_more for the items AFTER it (already
      // loaded) — storing those would flip the reader to "not at tail" and kill
      // live-tail / stick-to-bottom (Codex P1). So the bottom edge (page.next_after,
      // page.has_more, nextAfterRef, hasMore) is deliberately preserved.
      setDetailSynced((prev) => (prev ? { ...prev, items: [...body.items, ...prev.items],
        page: { ...prev.page, prev_before: body.page.prev_before ?? null, has_prev: body.page.has_prev ?? false },
        subagent_meta: body.subagent_meta ?? prev.subagent_meta } : body));
      prevBeforeRef.current = body.page.prev_before ?? null;
      setHasPrev(body.page.has_prev ?? false);
      return (body.page.has_prev ?? false) && prevBeforeRef.current != null;
    } finally {
      loadingPrevRef.current = false;
    }
  }, [setDetailSynced]);

  // Returns whether the reverse page actually prepended ≥1 item. The synchronous
  // detailRef mirror is updated inside fetchPrev's setDetailSynced BEFORE this
  // await resolves, so the count delta is race-free (no DOM / commit dependency).
  const loadPrev = useCallback(async (): Promise<boolean> => {
    const before = detailRef.current?.items.length ?? 0;
    await fetchPrev();
    const after = detailRef.current?.items.length ?? 0;
    return after > before;
  }, [fetchPrev]);

  // ── Unified jump pager (replaces loadUntil + loadToEnd) ─────────────────────
  // Resolve the target uuid to its OWNING outline turn (Codex P1 — a deep-link /
  // search uuid can be a folded fragment's uuid), decide whether it sits above
  // or below the loaded window from the full-session outline index, then page
  // from the NEAREST edge toward it until it is in-window. No cap — paging
  // strictly advances toward a known-position target and stops on in-window or a
  // genuine edge exhaustion. A uuid resolving to no outline turn is a graceful
  // no-op (current behavior).
  const loadToTarget = useCallback(async (uuid: string) => {
    const sid = sessionRef.current;
    if (sid == null) return;
    // Already loaded? (read the synchronous mirror, not React state.)
    const has = () => {
      const s = detailRef.current;
      return !!s && s.items.some((it) => it.member_uuids.includes(uuid));
    };
    if (has()) return;

    const turns = outlineTurnsRef.current ?? [];

    // Fallback: with NO full-session outline (it hasn't loaded yet, or a caller
    // passed none), we can't decide a nearest-edge direction — drive the legacy
    // FORWARD drain (the old loadUntil/loadToEnd behavior), paging the bottom
    // edge until the target's member_uuids appear or the cursor exhausts. The
    // overlap-race disambiguation is preserved. This keeps a deep-link arriving
    // before the outline skeleton from no-op'ing.
    if (turns.length === 0) {
      const sidF = sid;
      for (;;) {
        if (has()) return;
        const more = await fetchNext();
        if (more) continue;
        if (nextAfterRef.current == null || sessionRef.current !== sidF) return;
        while (loadingMoreRef.current && sessionRef.current === sidF) await Promise.resolve();
        if (sessionRef.current !== sidF) return;
      }
    }

    const targets = buildOutlineTargets(turns);
    const targetIdx = resolveTurnIndex(targets, uuid);
    if (targetIdx === undefined) return;  // not an outline turn → graceful no-op

    // Decide direction from the loaded window's outline span. Map the window's
    // first/last loaded item to outline indices; if the target is BELOW the last
    // loaded item → page forward (after); if ABOVE the first → page backward
    // (before). On a cold/sparse window fall back to forward.
    const windowItems = detailRef.current?.items ?? [];
    const idxOf = (it: ConversationItem): number | undefined => {
      for (const u of it.member_uuids) {
        const i = resolveTurnIndex(targets, u);
        if (i !== undefined) return i;
      }
      return undefined;
    };
    let firstIdx: number | undefined;
    let lastIdx: number | undefined;
    for (const it of windowItems) {
      const i = idxOf(it);
      if (i === undefined) continue;
      if (firstIdx === undefined || i < firstIdx) firstIdx = i;
      if (lastIdx === undefined || i > lastIdx) lastIdx = i;
    }
    // backward iff the target is strictly above the window's first loaded turn;
    // otherwise forward (covers below-window AND the cold-window fallback).
    const backward = firstIdx !== undefined && targetIdx < firstIdx;

    if (backward) {
      // Page head-ward via the top edge until the target loads or the head is
      // reached. fetchPrev returns false for a genuine exhaustion (cursor null /
      // session change / error) OR a transient overlap early-return; disambiguate
      // via the synchronous cursor mirror (same discipline as the forward path).
      for (;;) {
        if (has()) return;
        const more = await fetchPrev();
        if (more) continue;
        if (prevBeforeRef.current == null || sessionRef.current !== sid) return;
        while (loadingPrevRef.current && sessionRef.current === sid) await Promise.resolve();
        if (sessionRef.current !== sid) return;
      }
    } else {
      // Page tail-ward via the bottom edge (the ported overlap-race-safe drain).
      for (;;) {
        if (has()) return;
        const more = await fetchNext();
        if (more) continue;
        if (nextAfterRef.current == null || sessionRef.current !== sid) return;
        while (loadingMoreRef.current && sessionRef.current === sid) await Promise.resolve();
        if (sessionRef.current !== sid) return;
      }
    }
  }, [fetchNext, fetchPrev]);

  // ── Jump-to-latest = a ?tail=1 RESET (instant; not a forward drain) ─────────
  const jumpToLatest = useCallback(async () => {
    const sid = sessionRef.current;
    if (sid == null) return;
    let body: ConversationDetail;
    try {
      body = await fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sid)}?tail=1&limit=${PAGE}`);
    } catch {
      return;
    }
    if (sessionRef.current !== sid) return;
    applyWindow(body);
  }, [applyWindow]);

  // #183 — derive (don't sync) the cross-session reset: only surface `detail`
  // when it was loaded FOR the requested session, so the previous session's
  // detail is never exposed under a newer sessionId during the transient.
  const detailMatches = detail != null && loadedSessionId === sessionId;
  const exposedDetail = detailMatches ? detail : null;
  const exposedLoading = sessionId != null && !detailMatches && error == null ? true : loading;

  // BOTTOM edge → hasMore + the live-tail gate. (Codex P1: the top edge plays no
  // part here, so a `before` prepend never makes the reader look "not at tail".)
  const hasMore = exposedDetail?.page?.next_after != null;
  hasMoreRef.current = hasMore;
  // Surface the current top-edge cursor for the reader's loadPrev sentinel.
  const prevBefore = exposedDetail?.page?.prev_before ?? null;

  // #175 F4 — tail-poll the open conversation (bottom-edge gated). Unchanged from
  // the prior implementation except the merge preserves the top-edge keys.
  const pollTail = useCallback(async () => {
    if (pollingRef.current) { pendingTickRef.current = true; return; }  // coalesce a mid-fetch tick
    const sid = sessionRef.current;
    if (!sid || hasMoreRef.current || loadingMoreRef.current) return;   // only at the tail, never racing loadMore
    pollingRef.current = true;
    try {
      for (let i = 0; i < 50; i++) {                                    // drain a >PAGE burst within one tick
        const items = detailRef.current?.items ?? [];
        if (!items.length) break;
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
          last_anchor: body.last_anchor ?? prev.last_anchor,
          last_activity_utc: body.last_activity_utc ?? prev.last_activity_utc,
          subagent_meta: body.subagent_meta ?? prev.subagent_meta,
          page: prev.page,                                             // stays fully-paged at the bottom; top edge preserved
        } : prev));
        if (appended === 0 || body.page.next_after == null) break;
      }
    } finally {
      pollingRef.current = false;
      if (pendingTickRef.current) { pendingTickRef.current = false; void pollTail(); }  // replay one coalesced tick
    }
  }, [setDetailSynced]);

  // Trigger on each SSE tick, but only while fully paged at the bottom.
  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  useEffect(() => {
    if (detailRef.current && !hasMoreRef.current) void pollTail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generatedAt]);

  // Live-tail (spec §3.1): a dedicated per-conversation EventSource that fires
  // pollTail() the instant the server sees this session's JSONL grow.
  const transcriptsEnabled = env?.transcriptsEnabled ?? false;
  const liveTailEnabled = useSyncExternalStore(subscribeStore, () => selectLiveTailEnabled(getState()));
  useEffect(() => {
    if (!sessionId || !transcriptsEnabled || !liveTailEnabled) return;
    if (typeof EventSource === 'undefined') return;
    const es = new EventSource(`/api/conversation/${encodeURIComponent(sessionId)}/events`);
    es.addEventListener('tail', () => { void pollTail(); });
    es.addEventListener('open', () => { void pollTail(); });  // (re)connect catch-up
    return () => es.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, transcriptsEnabled, liveTailEnabled]);

  return {
    detail: exposedDetail, loading: exposedLoading, error,
    hasMore, hasPrev, prevBefore, openScrollIntent,
    loadMore, loadPrev, loadToTarget, jumpToLatest,
  };
}
