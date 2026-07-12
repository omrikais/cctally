import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchJson, HttpError, isAbortError } from '../lib/fetchJson';
import { useSnapshot } from './useSnapshot';
import { buildOutlineTargets, resolveTurnIndex } from '../conversations/outlineNavigation';
import { planTrim } from '../conversations/windowedCap';
import { VIRTUAL_INDEX_BASE, applyFirstItemDelta } from '../conversations/virtuosoFirstIndex';
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
// #228 S3 B3 — explicit operation metadata. Every window mutation emits a
// WindowOp so the reader NEVER infers paging direction from `items.length`,
// the first-item id, or a count delta — all of which a future prepend+far-trim
// (the windowed DOM cap) silently defeats (a same-commit prepend+bottom-trim
// can keep the count/firstId flat). `rev` is monotonic and bumps on EVERY
// mutation (even a length-flat one), so a reader effect keyed on `rev` fires
// reliably; `op` names the direction; the four `added*`/`dropped*` counts let
// the reader's live-append/stick and `newCount` pill paths read exactly what
// changed at each edge (since #232 the viewport pin across a prepend is Virtuoso's
// `firstItemIndex`, not a reader-side scroll-anchor effect).
export interface WindowOp {
  rev: number;                  // monotonic, bumped on every window mutation
  op: 'prepend' | 'append' | 'reset';
  addedTop: number;
  addedBottom: number;
  droppedTop: number;
  droppedBottom: number;
  // #228 S3 B3 — true when THIS op is the windowed-cap trim itself (a pure drop).
  // The trim effect skips a trim-origin op so it can't recurse, and the reader's
  // append/stick path treats it as a no-op (addedBottom is 0). A non-trim paging
  // op leaves this undefined.
  trim?: true;
}

// #286 B3 (spec §7 carve-out) — the additive `loadToTarget` return shape. The
// reader's jump runner acks the exhaustion clear against the COMMITTED WINDOW
// EPOCH: a stale pre-commit captured render list can never fire a premature
// clear, because the runner defers until `committedRev >= terminalOpRev` with the
// target genuinely absent from the LIVE window (`!found`) and the DRAINED edge
// truly exhausted (`exhausted`, directional). NO other hook surface moves.
export interface LoadToTargetResult {
  // The target ended up in the live window (`detailRef`), even if React hasn't
  // committed the bringing-prepend to the reader's render list yet.
  found: boolean;
  // Which edge the drain paged: 'backward' (top, `?before=`), 'forward' (bottom,
  // `?after=`), or 'none' (no drivable direction — already loaded / not an outline
  // turn / session gone). Informational; the reader reads `exhausted`.
  direction: 'backward' | 'forward' | 'none';
  // The DRAINED edge is genuinely exhausted (directional: `prevBefore == null` for
  // a backward drain, `next_after == null` for forward; `none` → treated exhausted
  // so a truly-unreachable target still clears). A non-exhaustion exit (session
  // change mid-drain) leaves this false so the jump stays pending for a re-fire.
  exhausted: boolean;
  // The rev of the drain's TERMINAL WindowOp (`opRevRef` after the drain). The
  // reader clears only once its captured `lastOp.rev` has caught up to this, so a
  // still-in-flight bringing-prepend defers the clear to its own commit re-fire.
  terminalOpRev: number;
}

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
  // #228 S3 B3 — the latest window-mutation metadata (null before the first
  // mutation). The reader keys its live-append/stick and `newCount` pill on this,
  // NOT on items.length / firstId / count deltas (since #232 the prepend viewport
  // pin is Virtuoso's `firstItemIndex`, not a reader scroll-anchor restore).
  lastOp: WindowOp | null;
  loadMore: () => Promise<WindowOp | null>;
  // #228 S3 B3 — resolves to the prepend's WindowOp (or null for a genuine no-op:
  // null cursor, stale cursor → empty page, or a fetch error). `addedTop > 0`
  // signals a real prepend (NOT a count compare, which a far-trim defeats).
  loadPrev: () => Promise<WindowOp | null>;
  loadToTarget: (uuid: string) => Promise<LoadToTargetResult>;
  jumpToLatest: () => Promise<void>;
  // #217 S4 / I-1.6 — a monotonic counter bumped on each successful pollTail
  // merge (live-tail growth). The open find bar keys its auto-refetch on this
  // (debounced) so tail growth re-runs the query. It is deliberately NOT
  // `detail.items.length`: pollTail can REPLACE/DELETE items inside the overlap
  // window without changing the length while the find corpus still changed
  // (Codex P1), so a length-keyed signal would silently miss those mutations.
  tailRevision: number;
  // #232 — the Virtuoso `firstItemIndex` the reader passes to <Virtuoso>. The
  // hook OWNS it in combined window state (atomically with detail.items) so it
  // moves correctly even across `capWindowDuringDrain`'s head-trim, which emits
  // NO WindowOp; virtualIndex = virtualFirstItemIndex + arrayIndex.
  virtualFirstItemIndex: number;
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

// #228 S3 B3 — the soft windowed-DOM cap, in ITEMS. Page-aligned to PAGE so the
// trim drops whole pages and never strands a partial page. Kept ≤ 2 pages so the
// loaded window is bounded at ~1000 items on a very long transcript (one 500-item
// page can alone be ~650k DOM nodes at the audit's worst ~1.3k nodes/item, so two
// is the ceiling). The exact cap K and whether to LOWER `PAGE` (e.g. to ~150–200,
// so the cap bites harder and the initial tail page isn't itself oversized) is a
// ui-qa tuning decision against the real long #217 transcripts — any reduction
// must preserve TAIL_WINDOW=10 (the live-tail overlap) and the server's ≤1000
// per-request limit. Trimming never crosses a protected uuid (windowedCap.ts).
const WINDOW_CAP_ITEMS = 2 * PAGE;

export interface UseConversationOptions {
  // #217 S3 E2 — the full-session outline turns, for `loadToTarget`'s nearest-edge
  // direction decision + member-uuid resolution. Independent of the loaded
  // page-window, so it always yields a reliable above/below verdict.
  outlineTurns?: OutlineTurn[];
  // #217 S3 E2 — the precedence-resolved open intent. The hook's FIRST request
  // follows it: 'anchor'/'restore' → loadToTarget(uuid); 'tail' → ?tail=1.
  // Omitted/undefined → the legacy head-fetch (?limit=500).
  openIntent?: OpenIntent | null;
  // #228 S3 B3 — uuids the windowed-DOM-cap trim must NEVER drop (the active find
  // match, the current/pinned turn, an in-flight jump target). The hook can't own
  // these (they live in FindBar / the store / loadToTarget's caller), so the
  // reader assembles the set and passes it in. The trim skips any page holding a
  // member of this set.
  protectedUuids?: Set<string>;
  // #278 Theme B — the shared live-tail signal (from useConversationLiveTail via
  // ConversationsView → ConversationReader). `live` true = the server is actively
  // live-tailing, so growth arrives via `growthNonce` and the global-tick refetch
  // is skipped; false = fall back to the memo-backed global tick. Defaults keep
  // pre-#278 behavior (live=false → global tick still fires).
  growthNonce?: number;
  live?: boolean;
}

export function useConversation(sessionId: string | null, opts: UseConversationOptions = {}): UseConversation {
  const { outlineTurns, openIntent, protectedUuids, growthNonce = 0, live = false } = opts;
  // Live mirror so the ref-stable trim effect reads the latest protected set
  // without re-creating itself (mirrors outlineTurnsRef).
  const protectedUuidsRef = useRef<Set<string> | undefined>(protectedUuids);
  protectedUuidsRef.current = protectedUuids;
  // #232 — combined window state: `detail.items` and the Virtuoso firstItemIndex
  // move together, in ONE state object updated atomically by each head-mutating
  // updater. A reader ref accumulator would double-process under StrictMode
  // (main.tsx) and miss `capWindowDuringDrain`'s emit-less head trim; folding the
  // index into the same setState that holds the items keeps it pure (StrictMode-
  // safe) and covers EVERY head mutation. virtualIndex = firstItemIndex +
  // arrayIndex; firstItemIndex moves so data[0] keeps a stable virtual index.
  interface WindowState { detail: ConversationDetail | null; firstItemIndex: number; }
  const [windowState, setWindowState] = useState<WindowState>({ detail: null, firstItemIndex: VIRTUAL_INDEX_BASE });
  const detail = windowState.detail;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openScrollIntent, setOpenScrollIntent] = useState<'top' | 'bottom' | null>(null);
  // #228 S3 B3 — the latest window-mutation metadata + its monotonic source.
  // `opRevRef` is the single monotonic counter (a ref so concurrent emits in one
  // tick still get distinct, ordered revisions without a stale-state race).
  const [lastOp, setLastOp] = useState<WindowOp | null>(null);
  const opRevRef = useRef(0);
  const emitOp = useCallback((op: Omit<WindowOp, 'rev'>): WindowOp => {
    const next: WindowOp = { rev: ++opRevRef.current, ...op };
    setLastOp(next);
    return next;
  }, []);
  // #217 S4 / I-1.6 — monotonic live-tail merge counter (see UseConversation).
  const [tailRevision, setTailRevision] = useState(0);
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
  // #232 — single-in-flight guard for loadToTarget. Holds the running drain's
  // promise so a re-entrant call (the jump effect re-fires on every prepend the
  // drain itself causes) awaits the existing drain instead of starting a rival
  // that would livelock on the overlap flags. See loadToTarget for the full note.
  const drainingRef = useRef<Promise<void> | null>(null);
  // #232 — holds the drain body so the guarded `loadToTarget` wrapper (declared
  // first, to keep its identity stable for the reader's effect deps) can invoke it
  // without a temporal-dead-zone reference to the const declared further below.
  const runDrainToTargetRef = useRef<(uuid: string, sid: string) => Promise<void>>(
    () => Promise.resolve(),
  );
  // #286 B3 — the drain's direction (set once per drain by `runDrainImpl` when it
  // picks a loop) and its committed-epoch result, so the guarded `loadToTarget`
  // wrapper (and a re-entrant awaiter) can return the actual `{found, direction,
  // exhausted, terminalOpRev}` without restructuring the freeze-sensitive drain.
  const lastDrainDirectionRef = useRef<'backward' | 'forward' | 'none'>('none');
  const drainResultRef = useRef<LoadToTargetResult>({
    found: false, direction: 'none', exhausted: true, terminalOpRev: 0,
  });
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
  // #232 — the detail-only setter. It preserves `firstItemIndex` by default, so
  // every caller that only touches the items (append / tail-poll / detail-only
  // resets) leaves the Virtuoso offset untouched. The head-mutating call sites
  // (applyWindow reset, fetchPrev prepend, capWindowDuringDrain, the decoupled
  // trim) call setWindowState directly to move `firstItemIndex` atomically.
  const setDetailSynced = useCallback(
    (next: ConversationDetail | null | ((prev: ConversationDetail | null) => ConversationDetail | null)) => {
      setWindowState((ws) => {
        const value = typeof next === 'function' ? next(ws.detail) : next;
        detailRef.current = value;
        return { detail: value, firstItemIndex: ws.firstItemIndex };
      });
    },
    [],
  );

  // Apply a fetched page as the SOLE window (open / tail / loadToTarget reset),
  // arming BOTH edges from the envelope. Used by the initial fetch + jumpToLatest.
  const applyWindow = useCallback((body: ConversationDetail) => {
    // #232 — a window RESET re-bases the Virtuoso offset to VIRTUAL_INDEX_BASE
    // (the fresh window's data[0] is its new anchor).
    setWindowState(() => { detailRef.current = body; return { detail: body, firstItemIndex: VIRTUAL_INDEX_BASE }; });
    nextAfterRef.current = body.page.next_after;
    prevBeforeRef.current = body.page.prev_before ?? null;
    setHasPrev(body.page.has_prev ?? false);
    // #228 S3 B3 — a window reset replaces the whole window; the reader treats it
    // as neither a prepend nor an append (its one-shot open-scroll-intent latch
    // and the prev-trackers handle the fresh window).
    emitOp({ op: 'reset', addedTop: 0, addedBottom: 0, droppedTop: 0, droppedBottom: 0 });
    // #232 — applyWindow now calls setWindowState directly (to re-base firstItemIndex
    // atomically), so setDetailSynced is no longer a dependency.
  }, [emitOp]);

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
  // #228 S3 B3 — resolves to the append's WindowOp (addedBottom = the count this
  // page appended) or null for a no-op (null/stale cursor, session change, or a
  // fetch error). `fetchNext` is also driven by the jump pager; loadMore wraps it.
  const fetchNext = useCallback(async (): Promise<WindowOp | null> => {
    const after = nextAfterRef.current;
    const sid = sessionRef.current;
    if (after == null || sid == null || loadingMoreRef.current) return null;
    loadingMoreRef.current = true;
    try {
      let body: ConversationDetail;
      try {
        body = await fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sid)}?limit=${PAGE}&after=${after}`);
      } catch {
        return null;
      }
      if (sessionRef.current !== sid) return null;  // session changed mid-fetch — drop this stale page
      // #166: keep the whole-session subagent_meta map across paged appends.
      // Codex P1 — an `after` page updates ONLY the bottom edge; the top edge
      // (prevBeforeRef / hasPrev) is left untouched.
      setDetailSynced((prev) => (prev ? { ...prev, items: [...prev.items, ...body.items],
        page: { ...prev.page, next_after: body.page.next_after, has_more: body.page.has_more },
        subagent_meta: body.subagent_meta ?? prev.subagent_meta } : body));
      nextAfterRef.current = body.page.next_after;
      // Emit even for an empty page (addedBottom 0) so a follow-up trim tick still
      // fires; the reader's stick/newCount paths gate on addedBottom themselves.
      return emitOp({ op: 'append', addedTop: 0, addedBottom: body.items.length, droppedTop: 0, droppedBottom: 0 });
    } finally {
      loadingMoreRef.current = false;
    }
  }, [setDetailSynced, emitOp]);

  const loadMore = useCallback(async (): Promise<WindowOp | null> => fetchNext(), [fetchNext]);

  // ── Reverse paging (top edge) — Codex P1: NEVER touches the bottom edge ─────
  // #228 S3 B3 — resolves to the prepend's WindowOp (addedTop = the count this
  // page prepended) or null for a no-op (null/stale cursor, session change, or a
  // fetch error). The drain loops below read `prevBeforeRef.current` for the
  // "more reverse pages remain" signal, NOT the op (an op is emitted even on the
  // last page, when the cursor goes null).
  const fetchPrev = useCallback(async (): Promise<WindowOp | null> => {
    const before = prevBeforeRef.current;
    const sid = sessionRef.current;
    if (before == null || sid == null || loadingPrevRef.current) return null;
    loadingPrevRef.current = true;
    try {
      let body: ConversationDetail;
      try {
        body = await fetchJson<ConversationDetail>(`/api/conversation/${encodeURIComponent(sid)}?limit=${PAGE}&before=${before}`);
      } catch {
        return null;
      }
      if (sessionRef.current !== sid) return null;
      // PREPEND the page and update ONLY the top edge. The before-page envelope
      // legitimately carries next_after / has_more for the items AFTER it (already
      // loaded) — storing those would flip the reader to "not at tail" and kill
      // live-tail / stick-to-bottom (Codex P1). So the bottom edge (page.next_after,
      // page.has_more, nextAfterRef, hasMore) is deliberately preserved.
      // #232 — the head grew by body.items.length, so DROP firstItemIndex by that
      // count (atomically with the prepend) to keep data[0]'s virtual index stable
      // — this is how Virtuoso pins the viewport across a reverse-paging prepend.
      setWindowState((ws) => {
        const prev = ws.detail;
        const next = prev ? { ...prev, items: [...body.items, ...prev.items],
          page: { ...prev.page, prev_before: body.page.prev_before ?? null, has_prev: body.page.has_prev ?? false },
          subagent_meta: body.subagent_meta ?? prev.subagent_meta } : body;
        detailRef.current = next;
        return { detail: next, firstItemIndex: applyFirstItemDelta(ws.firstItemIndex, { addedTop: body.items.length, droppedTop: 0 }) };
      });
      prevBeforeRef.current = body.page.prev_before ?? null;
      setHasPrev(body.page.has_prev ?? false);
      return emitOp({ op: 'prepend', addedTop: body.items.length, addedBottom: 0, droppedTop: 0, droppedBottom: 0 });
    } finally {
      loadingPrevRef.current = false;
    }
  }, [setDetailSynced, emitOp]);

  // #228 S3 B3 — resolves to the prepend's WindowOp (or null for a genuine no-op).
  // The reader checks `op?.addedTop` for success rather than a count compare,
  // which a same-commit prepend+far-trim (the windowed cap) would defeat. The
  // synchronous detailRef mirror is updated inside fetchPrev BEFORE this resolves,
  // and the emitted op carries the true addedTop regardless of any trailing trim.
  const loadPrev = useCallback(async (): Promise<WindowOp | null> => {
    const op = await fetchPrev();
    return op && op.addedTop > 0 ? op : null;
  }, [fetchPrev]);

  // ── Unified jump pager (replaces loadUntil + loadToEnd) ─────────────────────
  // Resolve the target uuid to its OWNING outline turn (Codex P1 — a deep-link /
  // search uuid can be a folded fragment's uuid), decide whether it sits above
  // or below the loaded window from the full-session outline index, then page
  // from the NEAREST edge toward it until it is in-window. No cap — paging
  // strictly advances toward a known-position target and stops on in-window or a
  // genuine edge exhaustion. A uuid resolving to no outline turn is a graceful
  // no-op (current behavior).
  const loadToTarget = useCallback(async (uuid: string): Promise<LoadToTargetResult> => {
    const sid = sessionRef.current;
    // Session gone — no drain, no absence claim (defer, never clear): exhausted
    // false so the reader stays pending for the re-open re-fire (#286 B3).
    if (sid == null) return { found: false, direction: 'none', exhausted: false, terminalOpRev: opRevRef.current };
    // #232 — RE-ENTRANCY GUARD (the cold-deep-link freeze fix). The jump effect
    // re-fires on every prepend THIS drain itself causes (its deps include
    // `nodes` / `virtualFirstItemIndex` / `detail.items.length`), and each re-fire
    // calls loadToTarget again. Without a guard that spawns MULTIPLE concurrent
    // backward drains that race on the single `loadingPrevRef` overlap flag: one
    // drain holds it while paging (its progress gated on a macrotask `yieldPaint`),
    // a sibling drain finds `op == null` and enters the `while (loadingPrevRef…)
    // await Promise.resolve()` MICROTASK spin — and because microtasks always drain
    // before the holder's `setTimeout(0)` can run, the holder can never make
    // progress to release the flag. The result is an unbreakable microtask livelock
    // that pins the main thread at 100% forever (measured: the spin iterated past
    // 50 000 with the holder never advancing — the P0 #232 cold-load freeze). A
    // single in-flight drain per hook instance eliminates the race entirely: a
    // re-fire while a drain runs simply AWAITS the existing one (already paging
    // toward the same target) instead of starting a rival. Awaiting (not dropping)
    // also lets a genuine NEW target — a second jump dispatched mid-drain — run its
    // own drain right after the first settles.
    // A re-entrant call awaits the in-flight drain and returns ITS committed-epoch
    // result (`drainResultRef`, set when that drain finished) — so a re-fire that
    // rode an existing drain acks against the same terminal op (#286 B3).
    const inFlight = drainingRef.current;
    if (inFlight) { await inFlight; return drainResultRef.current; }
    let release: () => void = () => {};
    drainingRef.current = new Promise<void>((res) => { release = res; });
    try {
      // runDrainImpl resets `lastDrainDirectionRef` to 'none' at its top and sets
      // 'backward'/'forward' once it picks a loop (via the ref indirection).
      await runDrainToTarget(uuid, sid);
    } finally {
      drainingRef.current = null;
      release();
    }
    // #286 B3 — compute the committed-epoch result from the terminal drain state:
    // `found` = the target reached the live window; `exhausted` = the DRAINED
    // edge's cursor is null (directional; 'none' → treated exhausted so a
    // truly-unreachable uuid still clears); `terminalOpRev` = the last emitted op.
    const direction = lastDrainDirectionRef.current;
    const found = detailRef.current?.items.some((it) => it.member_uuids.includes(uuid)) ?? false;
    const exhausted = direction === 'backward'
      ? prevBeforeRef.current == null
      : direction === 'forward'
        ? nextAfterRef.current == null
        : true;
    drainResultRef.current = { found, direction, exhausted, terminalOpRev: opRevRef.current };
    return drainResultRef.current;
    // runDrainToTargetRef is a stable ref, so this callback's identity never churns.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Hold the drain body in a ref so the guarded wrapper above (declared first, to
  // keep loadToTarget's identity stable for the reader's effect deps) can call it
  // without a temporal-dead-zone reference to a const declared below.
  const runDrainToTarget = useCallback(
    (uuid: string, sid: string): Promise<void> => runDrainToTargetRef.current(uuid, sid),
    [],
  );

  // The actual nearest-edge drain, wrapped by loadToTarget's re-entrancy guard
  // above (so only ONE drain is ever in flight per hook instance — see the freeze
  // note there). `sid` is captured by the wrapper at entry.
  const runDrainImpl = useCallback(async (uuid: string, sid: string) => {
    lastDrainDirectionRef.current = 'none';  // #286 B3 — reset; set once a loop is picked
    // Already loaded? (read the synchronous mirror, not React state.)
    const has = () => {
      const s = detailRef.current;
      return !!s && s.items.some((it) => it.member_uuids.includes(uuid));
    };
    if (has()) return;

    // #231 — cap the window IN-PLACE right after each drain page, from inside a
    // functional `setState` updater. A cold deep-link into a >cap conversation
    // pages the reader open by draining from the tail toward the target; a tight
    // programmatic drain batches every page's prepend/append into ONE React
    // commit, so the mounted window balloons toward the full transcript and that
    // single giant commit blocks the main thread on a cold deep-link (the
    // pre-existing P1 the #230 B3 QA surfaced). The decoupled passive-effect trim
    // can't prevent it: it computes from `detailRef`, which only catches up once
    // React processes the batch, and it never gets an idle, fetch-free moment
    // mid-drain — so observed peaks reached the full ~1764 items. Trimming from
    // the updater's `prev` (ALWAYS the latest queued state, unlike `detailRef`)
    // instead means even a single batched commit's FINAL state is already bounded:
    // each page's prepend collapses with its trim to ≤cap. The committed tree
    // therefore never exceeds the cap, DETERMINISTICALLY, independent of React's
    // commit/paint timing.
    //
    // Safe to trim in the SAME commit as the prepend here (the Codex P0 hazard a
    // same-commit prepend+far-trim once posed for a reader-side scroll-anchor
    // restore no longer applies): since #232 the reader has NO scroll-anchor
    // snapshot / useLayoutEffect — Virtuoso's `firstItemIndex` (owned here,
    // decremented atomically by the prepended count in this SAME commit) keeps the
    // viewport pinned across the prepend with no scrollTop math. The in-flight jump
    // target is in `protectedUuids`, so the trim never drops it even if a stale
    // `has()` over-drains past it. The opposite-edge cursor is re-armed so
    // the dropped far edge stays re-fetchable: a bottom drop (backward drain)
    // re-arms the bottom cursor — `hasMore` is DERIVED from `page.next_after`, no
    // state to sync; a top drop (forward drain) re-arms the top cursor and syncs
    // the `hasPrev` STATE out-of-render via a microtask. No-op (ref-equal `prev` →
    // React bails the render) under the cap or when all-protected.
    const capWindowDuringDrain = (drainOp: 'prepend' | 'append'): void => {
      // #232 — this is the ONE head mutation that emits no WindowOp, so it must
      // move firstItemIndex itself (an emitOp-keyed accumulator would miss it).
      // A bottom-drop leaves the head fixed; a top-drop raises firstItemIndex by
      // the trimmed count to keep the surviving data[0]'s virtual index stable.
      setWindowState((ws) => {
        const prev = ws.detail;
        if (!prev || prev.items.length <= WINDOW_CAP_ITEMS) return ws;
        const plan = planTrim({
          items: prev.items,
          op: drainOp,
          cap: WINDOW_CAP_ITEMS,
          protectedUuids: protectedUuidsRef.current ?? new Set<string>(),
          fetchInFlight: false,
        });
        if (plan.keep === prev.items) return ws;  // all-protected → React bails
        if (plan.droppedBottom > 0) {
          nextAfterRef.current = plan.resetBottomCursorTo;
          const next = { ...prev, items: plan.keep,
            page: { ...prev.page, next_after: plan.resetBottomCursorTo, has_more: true } };
          detailRef.current = next;
          return { detail: next, firstItemIndex: ws.firstItemIndex };  // bottom-drop: head unchanged
        }
        prevBeforeRef.current = plan.resetTopCursorTo;
        queueMicrotask(() => setHasPrev(true));  // hasPrev is state, not derived — sync out-of-render
        const next = { ...prev, items: plan.keep,
          page: { ...prev.page, prev_before: plan.resetTopCursorTo, has_prev: true } };
        detailRef.current = next;
        return { detail: next, firstItemIndex: applyFirstItemDelta(ws.firstItemIndex, { addedTop: 0, droppedTop: plan.droppedTop }) };
      });
    };

    // #231 — yield a macrotask between drain pages so React COMMITS AND PAINTS the
    // (already capped) bounded window before the next page is fetched. The cap
    // bounds how many items are committed, but a cold deep-link still has to MOUNT
    // that bounded window from scratch (no prior fiber tree to diff) — and a tight
    // back-to-back drain lets React fold the whole drain into one cold first-mount
    // commit, which pins the main thread even at the cap (the residual freeze the
    // #230 B3 / #231 QA surfaced — each card is a deep nested `<details>` block).
    // Yielding makes the cold mount INCREMENTAL — one ≤cap commit per page with a
    // paint between — so the reader becomes interactive after the first page and
    // stays responsive as the rest streams in, the same way a normal tail-open
    // already mounts. Paired with the in-place cap this gives bounded AND
    // incremental; on a warm jump (prior tree already mounted) the per-page commit
    // is cheap, so the few extra macrotasks are negligible. List virtualization
    // (render only visible cards) is the deeper fix for the per-card mount cost and
    // is tracked separately.
    const yieldPaint = (): Promise<void> =>
      new Promise<void>((resolve) => { setTimeout(resolve, 0); });

    const turns = outlineTurnsRef.current ?? [];

    // Fallback: with NO full-session outline (it hasn't loaded yet, or a caller
    // passed none), we can't decide a nearest-edge direction — drive the legacy
    // FORWARD drain (the old loadUntil/loadToEnd behavior), paging the bottom
    // edge until the target's member_uuids appear or the cursor exhausts. The
    // overlap-race disambiguation is preserved. This keeps a deep-link arriving
    // before the outline skeleton from no-op'ing.
    if (turns.length === 0) {
      lastDrainDirectionRef.current = 'forward';  // #286 B3 — no-outline legacy forward drain
      const sidF = sid;
      for (;;) {
        if (has()) return;
        // #228 S3 B3 — fetchNext now returns the append WindowOp (or null on a
        // no-op); "more pages remain" is read from the bottom-edge cursor, not the
        // op (an op is emitted even on the last page when the cursor goes null).
        const op = await fetchNext();
        if (op && nextAfterRef.current != null) { capWindowDuringDrain('append'); await yieldPaint(); if (sessionRef.current !== sidF) return; continue; }
        if (nextAfterRef.current == null || sessionRef.current !== sidF) return;
        // #232 — macrotask yield (see the directional drains' note) instead of an
        // `await Promise.resolve()` microtask spin, so an overlap wait can never
        // starve the in-flight fetchNext's macrotask progress.
        while (loadingMoreRef.current && sessionRef.current === sidF) await yieldPaint();
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
    lastDrainDirectionRef.current = backward ? 'backward' : 'forward';  // #286 B3 — drained edge

    if (backward) {
      // Page head-ward via the top edge until the target loads or the head is
      // reached. fetchPrev returns null for a genuine exhaustion (cursor null /
      // session change / error) OR a transient overlap early-return; disambiguate
      // via the synchronous cursor mirror (same discipline as the forward path).
      // "More remain" is read from prevBeforeRef, not the op (#228 S3 B3).
      for (;;) {
        if (has()) return;
        const op = await fetchPrev();
        if (op && prevBeforeRef.current != null) { capWindowDuringDrain('prepend'); await yieldPaint(); if (sessionRef.current !== sid) return; continue; }
        if (prevBeforeRef.current == null || sessionRef.current !== sid) return;
        // #232 — wait for an overlapping fetchPrev to finish via a MACROTASK
        // (`yieldPaint`), NOT `await Promise.resolve()`. The re-entrancy guard on
        // loadToTarget already makes a concurrent drain impossible, but a microtask
        // spin here would starve the page-fetch holder's own `setTimeout(0)`
        // continuation if one ever recurred — a macrotask yield can't (it sits in
        // the SAME queue as the holder's progress, so the holder advances and
        // clears the flag). Belt-and-suspenders against the freeze.
        while (loadingPrevRef.current && sessionRef.current === sid) await yieldPaint();
        if (sessionRef.current !== sid) return;
      }
    } else {
      // Page tail-ward via the bottom edge (the ported overlap-race-safe drain).
      for (;;) {
        if (has()) return;
        const op = await fetchNext();
        if (op && nextAfterRef.current != null) { capWindowDuringDrain('append'); await yieldPaint(); if (sessionRef.current !== sid) return; continue; }
        if (nextAfterRef.current == null || sessionRef.current !== sid) return;
        // #232 — macrotask yield (see the backward branch's note) instead of an
        // `await Promise.resolve()` microtask spin, so an overlap wait can never
        // starve the in-flight fetchNext's own macrotask progress.
        while (loadingMoreRef.current && sessionRef.current === sid) await yieldPaint();
        if (sessionRef.current !== sid) return;
      }
    }
  }, [fetchNext, fetchPrev]);
  runDrainToTargetRef.current = runDrainImpl;

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
        // Did this poll actually change the find corpus? An empty tail response
        // (returned.length === 0 → merged === items) is a genuine no-op; only a
        // non-empty response can append/replace/fold (#217 S4 / I-1.6).
        let corpusChanged = false;
        if (returned.length === 0) {
          merged = items;
        } else {
          corpusChanged = true;
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
        // #217 S4 / I-1.6 — a merge that touched the corpus bumps the monotonic
        // revision so a mounted find bar re-runs its query against the grown
        // corpus. Bumped even when `merged` length is unchanged (an in-place
        // overlap-window replace/delete still changes the find corpus) — the
        // explicit reason it is NOT keyed off items.length (Codex P1). But an
        // EMPTY tail response (`returned.length === 0`, merged === items) is a
        // genuine no-op and must NOT bump, else an idle conversation with the
        // find bar open refetches /find every ~5s snapshot tick for nothing.
        if (corpusChanged) setTailRevision((r) => r + 1);
        // #228 S3 B3 — a live-tail merge that touched the corpus emits an append
        // op so the reader's stick-to-bottom / "↓ N new" paths fire off explicit
        // metadata (addedBottom = the count of genuinely-new tail items) rather
        // than an items.length delta. An in-place overlap replace/delete with no
        // append (corpusChanged && appended===0) still emits (addedBottom 0) so
        // the reader re-seeds its trackers but neither sticks nor counts.
        if (corpusChanged) {
          emitOp({ op: 'append', addedTop: 0, addedBottom: appended, droppedTop: 0, droppedBottom: 0 });
        }
        if (appended === 0 || body.page.next_after == null) break;
      }
    } finally {
      pollingRef.current = false;
      if (pendingTickRef.current) { pendingTickRef.current = false; void pollTail(); }  // replay one coalesced tick
    }
  }, [setDetailSynced, emitOp]);

  // #278: the global-tick refetch is now only a FALLBACK — when the server is
  // actively live-tailing (`live`), genuine growth arrives via `growthNonce`
  // (below), so an idle unchanged reader issues no per-tick request. When live-
  // tail is off/passive/degraded (`live` never true) the global tick stays as the
  // fallback, made cheap by the #278 backend assembly memo.
  const env = useSnapshot();
  const generatedAt = env?.generated_at ?? '';
  useEffect(() => {
    if (live) return;                       // live-tail covers growth; memo-backed fallback only when off
    if (detailRef.current && !hasMoreRef.current) void pollTail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generatedAt, live]);

  // #278: genuine per-conversation growth push (from the shared live-tail hook,
  // via the `ready`/`tail` events). pollTail self-guards to the bottom edge
  // (hasMoreRef), so a head-open large conversation is a no-op here and catches
  // up when the user scrolls down (Codex F5) — unchanged behavior. Only the
  // trigger moves from the deleted local EventSource to this nonce.
  useEffect(() => {
    if (growthNonce === 0) return;          // 0 is the initial value; initial load is handled elsewhere
    if (detailRef.current && !hasMoreRef.current) void pollTail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [growthNonce]);

  // ── #228 S3 B3 — the decoupled windowed-DOM-cap trim ───────────────────────
  // Applied on a FOLLOW-UP passive effect keyed on `lastOp.rev`, NEVER in the
  // same commit as the paging mutation. A passive effect runs after the commit's
  // layout work AND after the browser paints, so the far-edge drop — already
  // off-screen — is invisible and never coincides with a measurement. Since #232
  // the viewport is pinned across a prepend by Virtuoso's `firstItemIndex` (owned
  // here, compensated in the prepend's own commit), not a reader-side scroll-anchor
  // useLayoutEffect. The brief DOM peak of cap+1 page before this fires is accepted.
  useEffect(() => {
    const op = lastOp;
    if (!op || op.trim) return;  // the trim's own op must never re-trigger a trim (no recursion)
    if (op.op !== 'append' && op.op !== 'prepend') return;  // reset / no op → nothing to trim
    // Never trim while ANY fetch is in flight (forward / reverse / live-tail) —
    // a trim must not race an in-progress page apply or the overlap re-fetch.
    const fetchInFlight = loadingMoreRef.current || loadingPrevRef.current || pollingRef.current;
    const items = detailRef.current?.items;
    if (!items) return;
    const plan = planTrim({
      items,
      op: op.op,
      cap: WINDOW_CAP_ITEMS,
      protectedUuids: protectedUuidsRef.current ?? new Set<string>(),
      fetchInFlight,
    });
    // #230 P3 — dev-only telemetry. The cap can stay exceeded when protected edges
    // (the active find match / current / pinned turn / an in-flight target) block
    // the trim from reaching it: correctness wins over the cap, so the helper never
    // evicts a protected uuid. This warns rather than force-trimming. Bounded in
    // practice (only a handful are ever protected at once). `import.meta.env.DEV` is
    // statically false in the committed production bundle, so the whole branch is
    // dead-code-eliminated — zero shipped cost. Must precede the `plan.keep === items`
    // early-out so the all-protected no-op (which is over the cap) is also caught.
    if (import.meta.env.DEV && !fetchInFlight && plan.keep.length > WINDOW_CAP_ITEMS) {
      console.warn(
        `[reader] windowed DOM cap (${WINDOW_CAP_ITEMS}) exceeded: ${plan.keep.length} items kept — protected edges block the trim`,
      );
    }
    if (plan.keep === items) return;  // no-op (under cap / in-flight / all-protected)

    // Apply `keep` + reset ONLY the opposite edge cursor + its has-more flag, then
    // emit a trim op carrying the drop counts so the reader knows the window
    // shrank. The opposite-edge invariant mirrors the pager: a bottom-drop touches
    // ONLY the bottom edge (nextAfterRef / page.next_after / has_more); a top-drop
    // ONLY the top edge (prevBeforeRef / page.prev_before / has_prev).
    if (plan.droppedBottom > 0) {
      // Prepend trimmed the bottom → re-arm the bottom cursor so scroll-down
      // re-fetches the dropped tail; the top edge is untouched.
      nextAfterRef.current = plan.resetBottomCursorTo;
      setDetailSynced((prev) => (prev ? {
        ...prev,
        items: plan.keep,
        page: { ...prev.page, next_after: plan.resetBottomCursorTo, has_more: true },
      } : prev));
    } else if (plan.droppedTop > 0) {
      // Append / live-tail trimmed the top → re-arm the top cursor so scroll-up
      // re-fetches the dropped head; the bottom edge (and the live-tail gate) is
      // untouched. #232 — a head trim RAISES firstItemIndex by droppedTop (moved
      // atomically with the items) so the surviving data[0]'s virtual index stays
      // stable. The droppedBottom branch above leaves the head fixed, so it stays
      // on setDetailSynced (firstItemIndex preserved).
      prevBeforeRef.current = plan.resetTopCursorTo;
      setHasPrev(true);
      setWindowState((ws) => {
        const prev = ws.detail;
        if (!prev) return ws;
        const next = { ...prev, items: plan.keep,
          page: { ...prev.page, prev_before: plan.resetTopCursorTo, has_prev: true } };
        detailRef.current = next;
        return { detail: next, firstItemIndex: applyFirstItemDelta(ws.firstItemIndex, { addedTop: 0, droppedTop: plan.droppedTop }) };
      });
    } else {
      return;
    }
    // The trim is itself a window mutation — emit `op: 'append'` (so the reader's
    // anchor-restore, which keys on `op === 'prepend'`, never fires) with
    // addedBottom 0 (so the reader neither sticks nor counts) carrying the dropped
    // counts, and `trim: true` so this effect skips it (no recursion). One pass
    // per paging op: a protected-uuid round may leave the window above the cap,
    // but it does NOT re-trim (which would drop the OTHER edge and defeat the
    // protection) — the cap re-applies on the NEXT genuine paging op.
    emitOp({
      op: 'append', addedTop: 0, addedBottom: 0,
      droppedTop: plan.droppedTop, droppedBottom: plan.droppedBottom,
      trim: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastOp?.rev]);

  return {
    detail: exposedDetail, loading: exposedLoading, error,
    hasMore, hasPrev, prevBefore, openScrollIntent,
    lastOp,
    loadMore, loadPrev, loadToTarget, jumpToLatest,
    tailRevision,
    virtualFirstItemIndex: windowState.firstItemIndex,
  };
}
