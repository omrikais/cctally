import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, selectMarkersEnabled, subscribeStore } from '../store/store';
import { useConversation } from '../hooks/useConversation';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { groupSidechains, flattenSubagents, walkSubagents, type RenderNode } from './groupSidechains';
import { isSystemMarker } from './systemMarkers';
import { FindBar } from './FindBar';
import { HighlightContext } from './HighlightContext';
import { MessageItem } from './MessageItem';
import { SidechainGroup } from './SidechainGroup';
import { ResultIcon, SpinnerIcon, WarningIcon, ChatIcon } from './ConvIcons';
import { TranscriptContext } from './TranscriptContext';
import { applyFocusMode, nodeUuid, nodeVisible, type FocusMode } from './applyFocusMode';
import { insertTimeMarkers } from './insertTimeMarkers';
import { buildOutlineTargets, nextTarget, type JumpKind } from './outlineNavigation';
import { fmt } from '../lib/fmt';
import { abbreviateModel } from '../lib/modelName';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { loadReadingPos } from '../store/readingPosition';
import type { ConversationItem, ConversationOutline, OpenIntent, SubagentMeta } from '../types/conversation';

// #186 — belt-and-suspenders title-only skip predicate. Mirrors the server
// `_CMD_FAMILY_RE` / `_looks_like_command_plumbing` (bin/_lib_conversation_query.py):
// a deliberately BROADER match than `isSystemMarker` — it skips a candidate line
// wrapped entirely in any `command-*` / `local-command-*` family tag (a tag-name
// PREFIX shape), including future unrecognized tags not in MARKER_TAGS. Strict
// `isSystemMarker` also drives the fold-to-pill decision (where a false positive
// would hide real user text), so the liberal matching lives ONLY here in title
// selection, where the worst case is the title falls back to the next line or
// the project label — never hiding content. Anchored to the whole string (the
// `^…$` + the unrolled-lazy body match the server `fullmatch`).
const CMD_FAMILY_RE = /^\s*(?:<((?:local-)?command-[a-z-]+)>(?:(?!<\/\1>)[\s\S])*<\/\1>\s*)+$/;
const looksLikeCommandPlumbing = (t: string): boolean => CMD_FAMILY_RE.test(t);

// First non-blank line of the first MAIN-session, non-marker human message;
// fallback project_label → session_id. Mirrors the kernel _session_titles_map
// (#165 Q6). The opening human is always on page 1.
export function deriveReaderTitle(detail: { items: ConversationItem[]; project_label: string; session_id: string }): string {
  for (const it of detail.items) {
    if (it.kind === 'human' && !it.is_sidechain && it.text.trim()
        && !isSystemMarker(it.text) && !looksLikeCommandPlumbing(it.text)) {
      const line = it.text.split('\n').map((s) => s.trim()).find(Boolean);
      if (line) return line.length > 120 ? line.slice(0, 120).trimEnd() + '…' : line;
    }
  }
  return detail.project_label || detail.session_id;
}

// §5 (Codex P1-D) — the ancestor chain of a subagent key: the key itself, then
// its parent_subagent_key, up to the root (a null parent = the main session
// stops the walk). A jump into a grandchild force-opens the grandchild AND its
// parent card so the nested target's element actually renders. `seen` guards
// against a malformed cycle in the linkage.
function ancestorKeys(k: string, meta?: Record<string, SubagentMeta>): string[] {
  const out: string[] = [];
  let cur: string | null = k;
  const seen = new Set<string>();
  while (cur != null && !seen.has(cur)) {
    seen.add(cur);
    out.push(cur);
    cur = meta?.[cur]?.parent_subagent_key ?? null;
  }
  return out;
}

// §5 — find the TOP-LEVEL RenderNode whose subtree contains the jump uuid, for
// the focus-mode visibility test. An `item` node matches by anchor or member
// uuid; a subagent node (top-level OR nested) matches if any of its OWN items'
// member_uuids hold the uuid, in which case the TOP-LEVEL root ancestor node is
// returned (a nested member's visibility is decided by its root ancestor). null
// when the uuid isn't in any built node yet (not-yet-paged).
function findTopLevelNodeFor(
  groups: RenderNode[],
  jumpUuid: string,
  detail: { subagent_meta?: Record<string, SubagentMeta> } | null | undefined,
): RenderNode | null {
  // 1. A top-level item / tool_result_run match.
  for (const n of groups) {
    if (n.kind === 'item' && n.item.member_uuids.includes(jumpUuid)) return n;
    if (n.kind === 'tool_result_run' && n.items.some((it) => it.member_uuids.includes(jumpUuid))) return n;
  }
  // 2. A subagent member (at any depth). Resolve the OWNING subagent key, then
  //    its ROOT ancestor key, then the top-level node for that root.
  let ownerKey: string | null = null;
  for (const node of flattenSubagents(groups)) {
    if (node.items.some((it) => it.member_uuids.includes(jumpUuid))) {
      ownerKey = node.subagentKey;
      break;
    }
  }
  if (ownerKey == null) return null;
  const chain = ancestorKeys(ownerKey, detail?.subagent_meta);
  const rootKey = chain[chain.length - 1];
  return groups.find((n) => n.kind === 'subagent' && n.subagentKey === rootKey) ?? null;
}

// Paginated transcript reader (spec §4). Lazy-loads the next page when a
// bottom sentinel scrolls into view (IntersectionObserver), and supports a
// jump-to-message: when a search hit sets conversationJump for THIS session,
// page until the target uuid is loaded, scroll it into view, flash a
// transient highlight (reduced-motion aware), then clear the jump. Every
// member uuid maps to its rendered element so a hit on any folded fragment
// resolves.
// `outline` (#177 S5) is threaded from ConversationsView so the reader's head
// toggle button can reflect open/closed state; Tasks 4/5 consume it further
// (jump-to-next targets, token footer). The scroll-sync IntersectionObserver
// below is independent of it (it observes the reader's own rendered turns).
export function ConversationReader({ sessionId, mobileBack, outline }: { sessionId: string; mobileBack?: boolean; outline?: ConversationOutline | null }) {
  // #217 S3 E2 — compute the open intent ONCE per session open so the hook's
  // FIRST request is precedence-correct (Codex P1; no head-fetch-then-redirect).
  // Precedence: (1) a deep-link / jump anchor for THIS session wins; (2) else a
  // restored E1 reading-position uuid; (3) else open at the bottom (?tail=1).
  // Computed synchronously at session-change time, reading the store's jump (an
  // OPEN_CONVERSATION deep-link sets selectedConversationId + conversationJump in
  // one dispatch, so it's already present) and the saved reading position. Keyed
  // on sessionId only so an in-session jump (which re-dispatches OPEN_CONVERSATION
  // with the same id) doesn't re-trigger the initial fetch — the live `jump`
  // effect below drives in-session jumps.
  const openIntent = useMemo<OpenIntent | null>(() => {
    if (!sessionId) return null;
    const j = getState().conversationJump;
    if (j && j.session_id === sessionId) return { kind: 'anchor', uuid: j.uuid };
    const saved = loadReadingPos(sessionId);
    if (saved) return { kind: 'restore', uuid: saved.uuid };
    return { kind: 'tail' };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);
  const { detail, loading, error, hasMore, hasPrev, openScrollIntent, loadMore, loadPrev, loadToTarget, jumpToLatest: hookJumpToLatest } = useConversation(sessionId, { outlineTurns: outline?.turns, openIntent });
  const jump = useSyncExternalStore(subscribeStore, () => getState().conversationJump);
  const outlineOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineOpen);
  // #205 S1 — the ephemeral mobile outline flag + the effective open-state. On
  // mobile the ☰/`o` toggle and aria-pressed track convOutlineMobileOpen (never
  // the persisted desktop pref); on desktop they track convOutlineOpen.
  const outlineMobileOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineMobileOpen);
  const isMobile = useIsMobile();
  const effectiveOutlineOpen = isMobile ? outlineMobileOpen : outlineOpen;
  // #177 S5 — the active focus mode (all/chat/prompts/errors) + scroll-sync
  // cursor uuid. focusMode drives the `visible` pipeline below; the cursor uuid
  // seeds jump-to-next.
  const focusMode = useSyncExternalStore(subscribeStore, () => getState().convFocusMode);
  const currentTurnUuid = useSyncExternalStore(subscribeStore, () => getState().convCurrentTurnUuid);
  // #188 B5 — the explicit-selection pin. The keyboard jump-to-next (e/u/b/p)
  // resolves its cursor from `pinned ?? currentTurnUuid` so a repeat forward
  // press steps strictly past where the last jump LANDED (closes #187), not past
  // the scroll-sync topmost-visible turn (which sits above a centered target).
  const convPinnedUuid = useSyncExternalStore(subscribeStore, () => getState().convPinnedUuid);
  // #177 S6 — the floating in-conversation find bar. `convFindOpen` gates its
  // render + the n/N step bindings. `findTerms` is the debounced needle split
  // into highlight terms (null when the bar is closed → no prose marks).
  const convFindOpen = useSyncExternalStore(subscribeStore, () => getState().convFindOpen);
  // cache-failure-markers spec §3/§5 — the cache-rebuild marker opt-out, read
  // ONCE here and provided down via TranscriptContext so the memoized
  // MessageItems don't each subscribe. selectMarkersEnabled defaults true.
  const markersEnabled = useSyncExternalStore(subscribeStore, () =>
    selectMarkersEnabled(getState()),
  );
  const [findTerms, setFindTerms] = useState<string[] | null>(null);
  // Live closure to the find bar's cursor stepper (n/N drive it while the bar
  // is open + the input is blurred). FindBar assigns its `step` here each render.
  const findStepRef = useRef<((delta: number) => void) | null>(null);
  const reduced = useReducedMotion();
  const sentinelRef = useRef<HTMLDivElement>(null);
  // #217 S3 E2 — the TOP sentinel (mirror of the bottom one) → loadPrev on
  // scroll-up. Plus scroll-anchoring bookkeeping: a prepend grows scrollHeight
  // above the viewport, so we capture the PRE-prepend height/scrollTop here (in
  // loadPrev) and re-apply the delta in a layout effect after the render commits,
  // keeping the viewport pinned to the same turn. `prependPendingRef` carries the
  // captured metrics across the async fetch + render; `prevItemsLenRef` lets the
  // anchoring layout effect tell a prepend (length grew, top edge advanced) from
  // an append.
  const topSentinelRef = useRef<HTMLDivElement>(null);
  const prependPendingRef = useRef<{ prevScrollHeight: number; prevScrollTop: number } | null>(null);
  const itemRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  // #188 S3/B6 — a SEPARATE map holding each subagent card's <details> element,
  // keyed by the bucket-root uuid. Registered UNCONDITIONALLY (open and closed),
  // so a collapsed-subagent outline jump resolves the CARD (itemRefs misses
  // while closed) and flashes it without force-opening (Bug 1). Typed
  // HTMLElement (a <details>) — distinct from itemRefs' HTMLDivElement so there
  // is no key collision and no open/close toggle race.
  const cardRefs = useRef<Map<string, HTMLElement>>(new Map());
  // Per-anchor-uuid memoized ref callbacks: a stable callback identity per item
  // so the memo'd MessageItems don't detach/re-attach on every paged append.
  const refCallbacks = useRef<Map<string, (el: HTMLDivElement | null) => void>>(new Map());
  // #188 S3/B6 — stable per-rootUuid card-ref callbacks (mirrors refCallbacks),
  // so the memoized SidechainGroups don't churn their <details> ref each render.
  const cardRefCallbacks = useRef<Map<string, (el: HTMLElement | null) => void>>(new Map());
  // Tracks the pending highlight-removal timeout so it can be cancelled on
  // unmount (no classList.remove on a detached node, no leaked timer) and
  // superseded on a rapid re-jump (no two overlapping 2s timers racing).
  const highlightTimerRef = useRef<number | null>(null);
  // §5 (Codex P1-D) — the SET of subagent keys force-opened for the in-flight
  // jump (#160). Empty when no force is active. On a jump into a subagent target
  // this holds the target's WHOLE ancestor chain (grandchild + parent + …) so a
  // nested target's element renders: each SidechainGroup's `forceOpen` is
  // `forcedOpenKeys.has(node.subagentKey)`. Setting it opens those groups in the
  // same render (their `open` is derived), so the target member's ref attaches
  // and the jump effect re-fires (forcedOpenKeys dep) to scroll to it. Identity
  // changes on each set (a fresh Set), which the jump effect deps on.
  const [forcedOpenKeys, setForcedOpenKeys] = useState<Set<string>>(() => new Set());
  // G1 §4b load-in stagger. A Set of anchor uuids already painted at least
  // once (the `daily-fade-in` seen-Set precedent, index.css:2032): each
  // top-level group rises exactly once on first appearance, so paged appends
  // and re-renders don't re-animate already-visible turns. Populated by a
  // post-commit effect AFTER the render-time classifier has read it, so the
  // decision is stable for that frame.
  const seenRef = useRef<Set<string>>(new Set());

  // G3 keyboard navigation. A focused-turn cursor over the DIRECT children of
  // `.conv-reader-thread` (the sentinel lives outside the thread). The
  // `conv-item--focused` class is moved imperatively (mirroring the jump
  // flash) so the memoized MessageItems don't re-render on every step. The
  // ref mirrors the state so the stable keymap action closures read the live
  // cursor without re-registering on every move.
  const [focusedIndex, setFocusedIndex] = useState(0);
  const focusedIndexRef = useRef(0);
  focusedIndexRef.current = focusedIndex;
  // #177 S5 — the focus-mode remap keys off the PREVIOUS render's RENDERED-NODE
  // list (`nodes` = what the thread actually paints: filtered turns + hidden_run
  // markers + time markers). `focusedIndex` indexes thread.children = nodes-space,
  // so the remap must read its prev list AND compute its target in nodes-space
  // too — a marker-less `visible` list would mis-resolve `prevNodesRef[cur]`
  // (and the target) by the count of any markers that precede the cursor.
  // `prevNodesRef` is updated in a post-render effect AFTER the remap reads it,
  // so the remap sees the list the user was actually looking at.
  const prevNodesRef = useRef<ReturnType<typeof insertTimeMarkers>>([]);
  const threadRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  // Stable mirrors so the `useMemo(() => [...], [])` keymap array never churns.
  const hasMoreRef = useRef(hasMore);
  hasMoreRef.current = hasMore;
  const loadMoreRef = useRef(loadMore);
  loadMoreRef.current = loadMore;
  // #217 S3 E2 — top-edge mirrors for the stable top-sentinel observer closure.
  const hasPrevRef = useRef(hasPrev);
  hasPrevRef.current = hasPrev;
  // Scroll-anchored reverse paging: snapshot the body's scroll metrics BEFORE
  // the prepend (the "before" height is unrecoverable post-update, Codex P2),
  // then the layout effect below re-pins the viewport. A guard against
  // overlapping loadPrevs (the captured metrics must not be overwritten
  // mid-flight).
  //
  // P3 fix — the layout effect is the SOLE clearer of prependPendingRef on a
  // SUCCESSFUL prepend. The old `.finally` compared the body's scrollHeight, but
  // `.finally` runs in the resolve microtask BEFORE React commits the prepend, so
  // the DOM height is still the OLD value: the comparison falsely read "no
  // growth" and could clear the snapshot out from under the (post-commit)
  // scroll-anchor effect → a viewport jump. We now drive the decision off the
  // hook's `loadPrev()` boolean (did it prepend ≥1 item — computed off the
  // synchronous detail mirror, no DOM/commit race). On a genuine no-op (stale
  // cursor / error → nothing prepended) NO new commit fires, so the layout effect
  // never runs to clear the snapshot; we clear it here in that case ONLY. On a
  // successful prepend we leave the snapshot for the layout effect to consume.
  const doLoadPrev = useCallback(() => {
    const b = bodyRef.current;
    if (!b || prependPendingRef.current) return;
    prependPendingRef.current = { prevScrollHeight: b.scrollHeight, prevScrollTop: b.scrollTop };
    void loadPrev().then((prepended) => {
      if (!prepended && prependPendingRef.current) {
        prependPendingRef.current = null;  // no-op: free the snapshot for the next loadPrev
      }
    });
  }, [loadPrev]);
  const doLoadPrevRef = useRef(doLoadPrev);
  doLoadPrevRef.current = doLoadPrev;
  const reducedRef = useRef(reduced);
  reducedRef.current = reduced;
  // #205 S1 — live mirror so the stable toggleOutline closure (shared by the ☰
  // button + the `o` keymap binding) reads the current viewport without
  // re-registering the useMemo([]) keymap array or capturing a stale value
  // across a resize.
  const isMobileRef = useRef(isMobile);
  isMobileRef.current = isMobile;
  // #177 S5 §4 — live mirrors for the stable jump-to-next key closures (the
  // keymap array is built once; its actions read refs, never re-registering).
  const outlineRef = useRef<ConversationOutline | null | undefined>(outline);
  outlineRef.current = outline;
  const currentTurnUuidRef = useRef<string | null>(currentTurnUuid);
  currentTurnUuidRef.current = currentTurnUuid;
  // #188 B5 — live mirror so the stable jump-to-next closure reads the pin
  // without re-registering the keymap array.
  const convPinnedUuidRef = useRef<string | null>(convPinnedUuid);
  convPinnedUuidRef.current = convPinnedUuid;
  const focusModeRef = useRef<FocusMode>(focusMode);
  focusModeRef.current = focusMode;
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  // #177 S6 — live mirror so the stable n/N keymap closures read the open flag
  // without re-registering the keymap array.
  const convFindOpenRef = useRef(convFindOpen);
  convFindOpenRef.current = convFindOpen;
  // cache-failure-markers spec §4 — live mirror so the stable `c`/`C` keymap
  // closures no-op when the opt-out is off, without re-registering the array.
  const markersEnabledRef = useRef(markersEnabled);
  markersEnabledRef.current = markersEnabled;

  // #175 F4 — live-tail scroll behavior. `atBottomRef` tracks whether the user
  // is parked at the bottom (updated on every scroll). `prevLenRef`/`prevHasMoreRef`
  // drive the live-append discriminator: a growth is a LIVE append (not the final
  // pagination page) only when the conversation was ALREADY fully paged before it
  // — i.e. prevHasMoreRef.current === false. The final pagination page grows
  // `items` AND flips hasMore false in the same update, so a naive `!hasMore`
  // check would false-positive once on the last page (Codex P0). `newCount` feeds
  // the floating "↓ N new" pill.
  const atBottomRef = useRef(true);
  const prevLenRef = useRef(0);
  const prevHasMoreRef = useRef(false);
  const [newCount, setNewCount] = useState(0);

  // #188 S4/C2 — count only VISIBLE live appends in the "↓ N new" pill (Bug 5).
  // `openKeysRef` tracks which subagent threads are currently expanded (lifted
  // from SidechainGroup via handleSubagentOpenChange); `knownSubagentKeysRef`
  // records every subagent_key seen on a prior commit. A live-appended item is
  // visible — and so counts — iff it's top-level, OR the first item of a
  // brand-new subagent group (its card appears), OR an append into an
  // already-EXPANDED known thread. An append into an existing COLLAPSED thread is
  // below the fold → +0. Both refs reset on session switch and seed (without
  // counting) during non-live pagination growth.
  const openKeysRef = useRef<Set<string>>(new Set());
  const knownSubagentKeysRef = useRef<Set<string>>(new Set());
  const handleSubagentOpenChange = useCallback((key: string, open: boolean) => {
    if (open) openKeysRef.current.add(key);
    else openKeysRef.current.delete(key);
  }, []);

  // #176 — floating "↑ Top of turn" button. Replaces the #175 sticky turn
  // header (which floated an opaque mask over the prose). `jumpTopVisible` gates
  // the button; `jumpTopTargetRef` holds the top-level block currently under the
  // viewport top so a click can scroll it back to its start. Both are reset on a
  // session switch (the reader is reused across conversations).
  const [jumpTopVisible, setJumpTopVisible] = useState(false);
  const jumpTopTargetRef = useRef<HTMLElement | null>(null);

  // jump-to-latest (spec §5) — the "Latest ↓" control's loading affordance. Set
  // true while jumpToLatest() resets to the tail page (a brief beat on a huge
  // beat), so the button shows a spinner glyph + disables to prevent re-entry.
  const [jumpingLatest, setJumpingLatest] = useState(false);

  const onBodyScroll = useCallback(() => {
    const b = bodyRef.current;
    if (!b) return;
    const atBottom = b.scrollTop + b.clientHeight >= b.scrollHeight - 80;
    atBottomRef.current = atBottom;
    if (atBottom) setNewCount((n) => (n ? 0 : n));

    // #176 — decide whether to surface the floating jump-to-top button. Find the
    // top-level block straddling the viewport top, then show the button only once
    // its start has scrolled meaningfully off (THRESHOLD). getBoundingClientRect
    // is used over offsetTop/offsetParent chains: it's robust to the thread's
    // transformed/relative ancestors and reads the live layout each scroll.
    const thread = threadRef.current, body = bodyRef.current;
    if (thread && body) {
      const bodyTop = body.getBoundingClientRect().top;
      let target: HTMLElement | null = null;
      for (const child of Array.from(thread.children) as HTMLElement[]) {
        const r = child.getBoundingClientRect();
        if (r.top <= bodyTop + 1 && r.bottom > bodyTop + 1) { target = child; break; }
      }
      const THRESHOLD = 160; // only once you've scrolled meaningfully past the block's start
      if (target && bodyTop - target.getBoundingClientRect().top > THRESHOLD) {
        jumpTopTargetRef.current = target;
        setJumpTopVisible(true);
      } else {
        jumpTopTargetRef.current = null;
        setJumpTopVisible(false);
      }
    }
  }, []);

  // #176 — scroll the current top-level turn back to its start, then hide the
  // button. reducedRef keeps the jump instant under prefers-reduced-motion.
  const jumpToTurnTop = useCallback(() => {
    jumpTopTargetRef.current?.scrollIntoView({ block: 'start', behavior: reducedRef.current ? 'auto' : 'smooth' });
    setJumpTopVisible(false);
    dispatch({ type: 'CLEAR_CONV_PIN' }); // #188 B3 — explicit nav clears the pin
  }, []);

  // Stick-if-at-bottom on a live append; otherwise preserve position + count the
  // new turns. Keyed on items.length (+ hasMore so prevHasMoreRef tracks each
  // commit). useLayoutEffect so atBottomRef reflects the PRE-append position and
  // the stick happens before paint (no visible jump).
  useLayoutEffect(() => {
    const b = bodyRef.current;
    const items = detail?.items ?? [];
    const len = items.length;
    // P1 fix — a reverse-page PREPEND must NOT be mistaken for a live append. A
    // prepend grows items.length too, and on a tail open (hasMore === false) it
    // satisfies the live discriminator below, so `tail = items.slice(prevLen)`
    // would read the OLD back-of-window turns as "new" and bump the "↓ N new"
    // pill by the prior window size (and could wrongly scroll-to-bottom). This
    // effect runs BEFORE the scroll-anchor layout effect that clears
    // `prependPendingRef`, so the snapshot set by doLoadPrev is still present
    // here for exactly one commit — the unambiguous "this growth is a prepend"
    // signal. Bail (just advancing the prev-trackers) so the scroll-anchor effect
    // owns the prepend.
    if (prependPendingRef.current) {
      prevLenRef.current = len;
      prevHasMoreRef.current = hasMore;
      return;
    }
    const prevLen = prevLenRef.current;
    const added = len - prevLen;
    // Live append (not the final pagination page): already fully paged before
    // this growth, and not the very first page load (prevLen > 0).
    const live = added > 0 && prevHasMoreRef.current === false && prevLen > 0;
    // #188 S4/C2 — classify each newly-appended item by VISIBILITY against the
    // OLD known-set + open-set (Bug 5): top-level (+1); first item of a
    // brand-new subagent group (+1, deduped per key per tick); append into an
    // already-EXPANDED known thread (+1); append into an existing COLLAPSED
    // known thread (+0, below the fold). Computed only on a live append; during
    // non-live growth (first page / pagination) the tail just SEEDS the
    // known-set below WITHOUT counting.
    const tail = added > 0 ? items.slice(prevLen) : [];
    let visibleAdded = 0;
    if (live) {
      const newThisTick = new Set<string>();
      for (const it of tail) {
        const k = it.subagent_key;
        if (k == null) {
          visibleAdded++;                              // top-level → always visible
        } else if (!knownSubagentKeysRef.current.has(k)) {
          // First item of a brand-new subagent group → its card appears once.
          if (!newThisTick.has(k)) { visibleAdded++; newThisTick.add(k); }
        } else if (openKeysRef.current.has(k)) {
          visibleAdded++;                              // append into an expanded thread
        }
        // else: append into an existing collapsed thread → +0 (below the fold).
      }
    }
    // Update the known-set from the tail (AFTER the visibility classification
    // read the OLD set) — covers both live and non-live (seed) growth.
    for (const it of tail) {
      if (it.subagent_key != null) knownSubagentKeysRef.current.add(it.subagent_key);
    }
    if (b && live) {
      if (atBottomRef.current && visibleAdded > 0) {
        b.scrollTo({ top: b.scrollHeight });           // instant stick to the newest turn
      } else if (visibleAdded > 0) {
        setNewCount((n) => n + visibleAdded);          // preserve position, surface the pill
      }
    }
    prevLenRef.current = len;
    prevHasMoreRef.current = hasMore;
  }, [detail?.items.length, hasMore]);

  const jumpToNew = useCallback(() => {
    const b = bodyRef.current;
    if (!b) return;
    b.scrollTo({ top: b.scrollHeight, behavior: reducedRef.current ? 'auto' : 'smooth' });
    setNewCount(0);
    dispatch({ type: 'CLEAR_CONV_PIN' }); // #188 B3 — explicit nav clears the pin
  }, []);

  // jump-to-latest (spec §5; #217 S3 E2 rework) — reset the window to the TAIL
  // page in ONE request (the hook's jumpToLatest = ?tail=1), so it's instant on a
  // huge session instead of draining every forward page. Then park atBottom (the
  // live-append stick-to-bottom path) and dispatch the SAME OPEN_CONVERSATION jump
  // the outline/find use so the final turn flashes + pins (the jump effect runs
  // loadToTarget(last_anchor.uuid), already in-window after the tail reset,
  // scrolls + flashes). No-op when last_anchor is null (a genuinely empty
  // conversation); the control is hidden then too.
  const jumpToLatest = useCallback(async () => {
    const la = detail?.last_anchor;
    if (!la) return;
    setJumpingLatest(true);
    try {
      await hookJumpToLatest();
      atBottomRef.current = true;  // land at the bottom so live appends stick
      dispatch({
        type: 'OPEN_CONVERSATION',
        sessionId: la.session_id,
        jump: { session_id: la.session_id, uuid: la.uuid },
      });
    } finally {
      setJumpingLatest(false);
    }
  }, [detail?.last_anchor, hookJumpToLatest]);
  // Live mirror so the stable `End` keymap closure calls the latest handler
  // without re-registering the `[]`-dep keymap array (the jumpNextRef pattern).
  const jumpToLatestRef = useRef(jumpToLatest);
  jumpToLatestRef.current = jumpToLatest;

  // §5 — pass subagent_meta so the tree build prefers the kernel's read-time
  // parent linkage (parent_subagent_key + spawn_uuid) for nesting, falling back
  // to legacy root.parent_uuid on old transcripts.
  const groups = useMemo(
    () => groupSidechains(detail?.items ?? [], detail?.subagent_meta),
    [detail?.items, detail?.subagent_meta],
  );
  // §5 (Codex P1-C) — the spawn-chip suppression set: every spawn `tool_use_id`
  // the kernel linked to a subagent. A `tool_call` with this id is suppressed in
  // favor of its nested card. tool_use_id granularity (one item can hold several
  // spawns); an UNLINKED spawn (>16 KB clip) has no nested card and no entry
  // here, so its chip still renders. Stable identity (memoized) keeps the
  // memoized MessageItems' memo valid across ticks.
  const suppressToolUseIds = useMemo(() => {
    const s = new Set<string>();
    for (const m of Object.values(detail?.subagent_meta ?? {})) {
      if (m.spawn_tool_use_id) s.add(m.spawn_tool_use_id);
    }
    return s;
  }, [detail?.subagent_meta]);
  // #177 S5 §5 — focus-mode-filtered render list. `all` short-circuits to the
  // SAME `groups` array identity (byte-identical render path); other modes drop
  // suppressed nodes and coalesce them into `hidden_run` markers. EVERYTHING the
  // reader renders + every effect that iterates the rendered thread children
  // keys on `visible`, not `groups`.
  const visible = useMemo(() => applyFocusMode(groups, focusMode), [groups, focusMode]);
  // #177 S5 §6 — interleave gap/day time markers over the VISIBLE sequence (so
  // they recompute per focus mode). Markers carry data-conv-marker (never a
  // keyboard stop) and role="separator". The display-tz context drives the
  // day-boundary + is the same source the dashboard panels use.
  const display = useDisplayTz();
  const fmtCtx = useMemo(
    () => ({ tz: display.resolvedTz, offsetLabel: display.offsetLabel }),
    [display.resolvedTz, display.offsetLabel],
  );
  const nodes = useMemo(() => insertTimeMarkers(visible, fmtCtx), [visible, fmtCtx]);
  // Live mirror of the unfiltered render-tree for the jump-to-next mode-hide
  // check (find the target node in `groups`, test nodeVisible under the mode).
  const groupsRef = useRef<RenderNode[]>(groups);
  groupsRef.current = groups;
  // §5 — live mirror of the subagent-meta map so the stable jumpNext closure can
  // resolve a nested subagent target's root ancestor (findTopLevelNodeFor) for
  // the visibility test without re-registering the keymap array.
  const subagentMetaRef = useRef<Record<string, SubagentMeta> | undefined>(detail?.subagent_meta);
  subagentMetaRef.current = detail?.subagent_meta;
  const title = useMemo(
    // #193: prefer the server-derived title (ai-title -> first prompt -> label
    // -> sid). deriveReaderTitle stays as the client-side fallback for older
    // responses (or any future shape) that arrive without a `title`.
    () => (detail ? (detail.title || deriveReaderTitle(detail)) : ''),
    [detail],
  );
  // Stable provider value so context consumers (the cards) don't re-render on
  // every reader render from a fresh object identity. focusMode rides along so
  // the block walker can suppress chips under chat mode (#177 S5). fmtCtx rides
  // along too (#184) so MessageItem reads the display tz from context instead of
  // a per-item useDisplayTz() subscription — the memoized items would otherwise
  // re-render on every SSE tick. Keyed on fmtCtx (already memoized above), so the
  // provider identity only changes when the resolved tz actually changes.
  // markersEnabled rides along too (cache-failure-markers spec §3) so MessageItem
  // reads the opt-out from context (no per-item store subscription); the provider
  // identity flips only when the opt-out actually changes.
  const transcriptCtx = useMemo(
    () => ({ sessionId, focusMode, fmtCtx, markersEnabled }),
    [sessionId, focusMode, fmtCtx, markersEnabled],
  );

  // Lazy-load when the bottom sentinel scrolls into view.
  useEffect(() => {
    if (!sentinelRef.current || !hasMore) return;
    const obs = new IntersectionObserver((es) => { if (es[0].isIntersecting) void loadMore(); });
    obs.observe(sentinelRef.current);
    return () => obs.disconnect();
  }, [hasMore, loadMore]);

  // #217 S3 E2 — reverse paging: a TOP sentinel mirrors the bottom one. When it
  // scrolls into view (the user scrolled up to the head of the loaded window)
  // and there are more reverse pages, prepend the previous page. Scroll-anchoring
  // (the layout effect below) keeps the viewport pinned to the same turn.
  useEffect(() => {
    if (!topSentinelRef.current || !hasPrev) return;
    const obs = new IntersectionObserver((es) => { if (es[0].isIntersecting) doLoadPrevRef.current(); });
    obs.observe(topSentinelRef.current);
    return () => obs.disconnect();
  }, [hasPrev]);

  // #217 S3 E2 — scroll-anchoring on a prepend. After loadPrev grows the thread
  // ABOVE the viewport, re-pin scrollTop by the scrollHeight delta so the turn
  // the user was reading stays put (the classic reverse-infinite-scroll problem).
  // useLayoutEffect so the correction lands before paint (no visible jump). Keyed
  // on items.length: a prepend grew it AND left a captured snapshot in
  // prependPendingRef (set by doLoadPrev). An append (bottom-stick effect's
  // domain) leaves no snapshot, so this no-ops there.
  useLayoutEffect(() => {
    const snap = prependPendingRef.current;
    const b = bodyRef.current;
    if (!snap || !b) return;
    const delta = b.scrollHeight - snap.prevScrollHeight;
    if (delta > 0) b.scrollTop = snap.prevScrollTop + delta;
    prependPendingRef.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail?.items.length]);

  // #217 S3 E2 — open-scroll-intent: once the FIRST page resolves, land per the
  // hook's precedence verdict. 'bottom' (a multi-page tail open) scrolls to the
  // newest turn and parks atBottom so live-tail sticks; 'top' (a single-page
  // session) scrolls to the start so it reads from the beginning. An anchor /
  // restore open leaves openScrollIntent null — the jump pipeline drives that
  // scroll instead. Reduced-motion-safe (instant).
  //
  // P0 fix — fire EXACTLY ONCE per open. The effect is keyed on items.length so
  // it lands the moment the first non-empty page renders, but `openScrollIntent`
  // is set ONCE (the hook resets it only on session change), so without a guard
  // every reverse-page prepend / live append would re-run it and yank the reader
  // back to the bottom (re-clamping scrollTop + re-arming atBottomRef), defeating
  // reverse pagination, the scroll-anchor, and the "stick only when at bottom"
  // contract. `appliedIntentRef` is a one-shot latch: apply on the first commit
  // where the intent is resolved AND content exists, then bail on every later
  // commit. It is reset to false on session switch (the effect below), so the
  // NEXT open re-applies its own intent.
  const appliedIntentRef = useRef(false);
  useLayoutEffect(() => {
    const b = bodyRef.current;
    if (!b || openScrollIntent == null) return;
    if (appliedIntentRef.current) return;          // already applied this open
    if (!(detail?.items.length)) return;            // wait for the first content page
    appliedIntentRef.current = true;
    if (openScrollIntent === 'bottom') {
      b.scrollTop = b.scrollHeight;
      atBottomRef.current = true;
    } else {
      b.scrollTop = 0;
      atBottomRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openScrollIntent, detail?.items.length]);

  // #217 S3 E1 — restore the saved reading position (open-precedence slot 2). A
  // deep-link anchor (slot 1) already lives in the store as a jump (the jump
  // effect drives it); a saved reading position has NO store jump, so once the
  // first page resolves under a 'restore' intent the reader dispatches a
  // same-session OPEN_CONVERSATION jump to the saved uuid, reusing the full jump
  // pipeline (loadToTarget + scroll + flash + pin). An unresolvable saved uuid
  // falls through the jump effect's exhaustion clear → the tail open stands.
  //
  // P2 fix — A→B→A re-open must re-restore. The reader is mounted PERSISTENTLY
  // (ConversationsView reuses one instance, no key={sessionId}), so a per-open
  // one-shot latch keyed on `sessionId` VALUE breaks: open A (latch := 'a'),
  // switch to B as a non-restore (tail) open — the early `return` above leaves
  // the latch at 'a' — then return to A, and `latch === 'a'` wrongly skips the
  // restore. The latch must therefore be keyed on the OPEN INSTANCE, not the id:
  // `lastOpenSessionRef` records the session this effect last SAW (set on EVERY
  // run, restore or not), and a mismatch means a genuinely new open → clear the
  // restored latch so the new open can fire its own restore. This re-arms on
  // return to A even though B never restored.
  const restoredRef = useRef(false);
  const lastOpenSessionRef = useRef<string | null>(null);
  useEffect(() => {
    // New open (the session changed since the last run) → re-arm the latch.
    if (lastOpenSessionRef.current !== sessionId) {
      lastOpenSessionRef.current = sessionId;
      restoredRef.current = false;
    }
    if (openIntent?.kind !== 'restore') return;
    if (!detail || detail.session_id !== sessionId) return;
    if (restoredRef.current) return;  // already restored this open (paged-growth re-fire guard)
    restoredRef.current = true;
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId,
      jump: { session_id: sessionId, uuid: openIntent.uuid },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openIntent, sessionId, detail?.session_id]);

  // #177 S5 §3 — scroll-sync. A deduped IntersectionObserver over the reader's
  // rendered turns writes the topmost-visible anchor uuid to the store, where
  // the OutlinePanel reads it to highlight + auto-scroll the current entry.
  // Codex F14: `itemRefs` maps EVERY member uuid to the SAME element, so we
  // build the observe set from UNIQUE elements (one observe per node) and
  // resolve each element's anchor uuid from its `data-uuid` attribute
  // (MessageItem renders `data-uuid={item.anchor.uuid}`). On a change we pick
  // the element with the smallest bounding-rect top among the currently
  // intersecting ones and dispatch it. Re-registers when `groups` changes
  // (paged appends grow the rendered set). No scroll listener — the observer's
  // own batched callback is the throttle.
  useEffect(() => {
    const root = bodyRef.current;
    if (!root || typeof IntersectionObserver === 'undefined') return;
    const visible = new Set<Element>();
    const obs = new IntersectionObserver(
      (records) => {
        for (const r of records) {
          if (r.isIntersecting) visible.add(r.target);
          else visible.delete(r.target);
        }
        let top: Element | null = null;
        let topY = Infinity;
        for (const el of visible) {
          const y = el.getBoundingClientRect().top;
          if (y < topY) { topY = y; top = el; }
        }
        const uuid = top?.getAttribute('data-uuid');
        if (uuid) dispatch({ type: 'SET_CONV_CURRENT_TURN', uuid });
      },
      { root, threshold: 0 },
    );
    // Dedup: itemRefs maps many uuids onto few elements, and #188 cardRefs adds
    // the collapsed-subagent <details> elements (so a collapsed subagent reports
    // its bucket-root uuid during free scroll → its outline entry highlights).
    // Observe each unique element once via a Set keyed on node identity.
    const seen = new Set<Element>();
    for (const el of itemRefs.current.values()) {
      if (seen.has(el)) continue;
      seen.add(el);
      obs.observe(el);
    }
    for (const el of cardRefs.current.values()) {
      if (seen.has(el)) continue;
      seen.add(el);
      obs.observe(el);
    }
    return () => obs.disconnect();
    // `visible` changes on every paged append / session switch / focus-mode
    // change — re-register so the observer tracks the freshly-rendered turns
    // AND cards (cardRefs is repopulated in the same commit that grows `visible`).
  }, [visible]);

  // Jump-to-message: page until the target is loaded, then scroll+highlight.
  // Wait for the first page (`detail`) before attempting — otherwise the effect
  // would fire while page 1 is still in flight (nextAfter unknown), page nowhere,
  // and clear the jump prematurely. It re-runs when detail?.items.length grows
  // (a paged-in target's ref attaches on the next commit) and when forcedOpenKeys
  // changes (a force-opened thread's member ref attaches in that commit).
  useEffect(() => {
    if (!jump || jump.session_id !== sessionId) {
      // Jump cleared, or it now points at another session — release any force-pin
      // so a thread we expanded for it isn't left pinned (the user regains
      // collapse control). No loop: this re-fires on the forcedOpenKeys dep,
      // re-hits this guard with an empty set, and returns.
      if (forcedOpenKeys.size > 0) setForcedOpenKeys(new Set());
      return;
    }
    if (!detail || detail.session_id !== sessionId) return; // cross-session transient: keep the pin
    let cancelled = false;
    void (async () => {
      await loadToTarget(jump.uuid);
      if (cancelled) return;
      // #188 B7 — resolve the target element: an inner member ref (itemRefs)
      // OR, for a collapsed subagent whose members are ref-less, the card's
      // <details> element (cardRefs, keyed by the bucket-root uuid). So an
      // outline subagent click flashes the CARD with no force-open; the
      // force-open path below still handles a find-jump to a real INNER uuid
      // (in neither map while the thread is closed).
      // #204 — cardRefs holds ONLY subagent bucket-root uuids. A jump whose uuid
      // is a card root (outline subagent entry, or a collapsed-card flash) aligns
      // the card HEAD to the top; a deep find-jump into an inner member (itemRefs
      // hit, cardRefs miss) still centers. itemRefs takes precedence for `el` (the
      // flash + keyboard-cursor target) so an open card flashes its first member.
      const cardEl = cardRefs.current.get(jump.uuid);
      const el = itemRefs.current.get(jump.uuid) ?? cardEl;
      if (el) {
        // #177 S6 — a find jump whose anchor matched in a tool/thinking block
        // opens the target turn's collapsed disclosures BEFORE scrolling (the
        // client can't know which disclosure holds the needle, so all of the
        // turn's `<details>` open — bounded + predictable). Other jumps
        // (search-hit click, outline, jump-to-next) leave expand_details unset.
        if (jump.expand_details) {
          el.querySelectorAll('details:not([open])').forEach((d) => { (d as HTMLDetailsElement).open = true; });
        }
        // #204 — a subagent CARD-root jump aligns the card HEAD to the top of the
        // viewport (`block: 'start'` on the <summary>). Centering it (`block:
        // 'center'`, the right default for a normal message/member target) puts a
        // TALL subagent card's head far above the fold — a 20000px grandchild card
        // centered leaves its head ~420px above the top (the #204 symptom). The
        // landing is stable, so this is a target/alignment fix, not a timing one.
        // A normal turn or a deep inner-member find-jump (cardEl undefined) keeps
        // the #188 B2 centering: the explicit pin set below drives the outline's
        // aria-current + the jump-to-next cursor onto exactly the jumped target.
        const scrollTarget: Element = cardEl ? (cardEl.querySelector('summary') ?? cardEl) : el;
        const scrollBlock: ScrollLogicalPosition = cardEl ? 'start' : 'center';
        scrollTarget.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: scrollBlock });
        // #204 — when this jump force-opened ancestor cards, re-aim on the next
        // frame once their `::details-content` block-size commits, so a late
        // reflow can't leave the target off-position. A double requestAnimationFrame
        // guarantees a full layout+paint cycle elapsed; idempotent (same target +
        // block). Guarded on isConnected so a session switch in the gap can't
        // scroll a detached node; only runs when a force-open was involved.
        if (forcedOpenKeys.size > 0 && typeof requestAnimationFrame === 'function') {
          const t = scrollTarget;
          requestAnimationFrame(() => requestAnimationFrame(() => {
            if (t.isConnected) {
              t.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: scrollBlock });
            }
          }));
        }
        el.classList.add('conv-item--jumped');
        // #188 B2 — pin the landing so the outline selects EXACTLY this target
        // and a repeat forward jump-to-next steps strictly past it (closes #187).
        dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: jump.uuid });
        // #177 S6 — sync the keyboard cursor to the jumped element so j/k (and
        // find's n/N) resume from the match. The jumped element is a direct
        // thread child; find its index there (mirrors the outline-jump intent
        // of landing focus on the target).
        const thread = threadRef.current;
        if (thread) {
          const idx = Array.prototype.indexOf.call(thread.children, el);
          if (idx >= 0) setFocusedIndex(idx);
        }
        if (highlightTimerRef.current != null) window.clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = window.setTimeout(() => {
          el.classList.remove('conv-item--jumped');
          highlightTimerRef.current = null;
        }, 2000);
        dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
        setForcedOpenKeys(new Set()); // reset for the next jump (threads stay open via their latches)
        return;
      }
      // No ref. Three reasons the target's element is absent:
      //
      //   (1) It just paged in (its MessageItem ref attaches on React's NEXT
      //       commit — the detail?.items.length re-fire handles that).
      //   (2) The current focus mode HIDES it — a non-`all` mode coalesces the
      //       node into a `hidden_run` marker, so it renders no MessageItem and
      //       never attaches a ref, regardless of any force-open. A find-jump
      //       (the find bar dispatches OPEN_CONVERSATION {jump} straight through
      //       this effect, bypassing jumpNext's reset) onto such a turn must
      //       escape the filter the same way jump-to-next does (spec §4 / §5):
      //       reset to `all`, then let the effect re-run and land the jump.
      //   (3) It lives inside a COLLAPSED subagent thread (members are ref-less
      //       while closed) but is otherwise mode-visible — force the owning
      //       thread open so the member's ref attaches.
      //
      // (2) MUST be checked before (3): a mode-hidden node renders no thread at
      // all, so force-opening can't help it; whereas a node inside a collapsed
      // BUT mode-visible subagent (e.g. an erroring sidechain under Errors mode)
      // passes nodeVisible and falls through to the force-open branch. The two
      // are disjoint by construction — nodeVisible decides which applies.
      const mode = focusModeRef.current;
      if (mode !== 'all') {
        // Apply jumpNext's hidden-target VISIBILITY test (nodeVisible under the
        // mode) to the target's RenderNode in the unfiltered `groups` (found by
        // anchor uuid or any member uuid for a folded/sidechain target). The
        // node-absent case diverges DELIBERATELY from jumpNext: here a node
        // missing from `groups` means the target paged in but its node isn't
        // built yet, so we treat it as not-yet-paged and leave it to the (1)
        // re-fire — we do NOT reset to `all`. jumpNext, walking a fixed
        // snapshot, instead treats an unresolved node as hidden.
        // §5 — find the target's TOP-LEVEL RenderNode for the visibility test.
        // A nested subagent member lives inside a parent node's `children`, so
        // its visibility is decided by the top-level ROOT ancestor node — found
        // via findTopLevelNodeFor.
        const node = findTopLevelNodeFor(groupsRef.current, jump.uuid, detail);
        if (node != null && !nodeVisible(node, mode)) {
          dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
          return; // re-run under `all`: the node renders, its ref attaches, scroll
        }
      }
      const targetItem = detail.items.find((it) => it.member_uuids.includes(jump.uuid));
      if (targetItem && targetItem.subagent_key != null) {
        // §5 (Codex P1-D) — force-open the WHOLE ancestor chain (the target
        // subagent + every parent up to the root) so a nested target's element
        // renders. A jump into a grandchild opens the grandchild AND its parent.
        const chain = ancestorKeys(targetItem.subagent_key, detail.subagent_meta);
        const missing = chain.some((k) => !forcedOpenKeys.has(k));
        if (missing) {
          setForcedOpenKeys((prev) => {
            const next = new Set(prev);
            for (const k of chain) next.add(k);
            return next;
          });
          return; // wait for the groups to open + attach the ref, then re-fire
        }
        // Already forced open: the ref attaches in the forcedOpenKeys commit
        // (before this re-fire), so reaching here means it's genuinely absent —
        // fall through to the exhaustion clear rather than spinning.
      }
      if (!hasMore) {
        dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
      }
    })();
    return () => { cancelled = true; };
    // hasMore stays in deps so the give-up clear fires on the edge where the final
    // page appends 0 items (items.length unchanged) but flips the cursor.
    // forcedOpenKeys re-fires the effect once a force-opened thread has attached the
    // target's ref. focusMode re-fires it once the mode-hidden fallback resets to
    // `all` — the hidden target's node renders + its ref attaches in that commit,
    // and this re-fire scrolls via the branch above. No infinite loop:
    // loadToTarget/fetchNext serialize via loadingMoreRef, hasMore transitions a
    // bounded number of times, the forcedOpenKeys path either resolves (clears) or
    // settles to a stable set (every chain key present), and the focusMode reset
    // is one-way (non-`all` → `all`) so the mode-hidden branch can fire at most
    // once per jump.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, sessionId, detail?.items.length, hasMore, forcedOpenKeys, focusMode]);

  // Cancel any pending highlight-removal timer on unmount only (NOT on every
  // jump-effect re-run — that would strip the flash the instant the successful
  // jump dispatches CLEAR_CONVERSATION_JUMP and re-fires the effect).
  useEffect(() => () => {
    if (highlightTimerRef.current != null) window.clearTimeout(highlightTimerRef.current);
  }, []);

  // #188 B3 — clear the explicit pin on user-initiated scrolling. Wheel +
  // touchmove (passive — never preventDefault) and the scroll-navigation keys
  // count as "the user took over"; the pin (an outline/find/jump selection)
  // yields to free scrolling so aria-current resumes its scroll-sync behavior.
  // Deliberately NOT wired to the generic onScroll/onBodyScroll: the jump's own
  // smooth scrollIntoView fires `scroll`, and clearing the pin there would undo
  // the pin the jump just set (the bug this guards). Only explicit input clears.
  // Re-runs once `detail` resolves — the `.conv-reader-body` element only mounts
  // after the first page loads (the loading/empty branches render a different
  // node), so a `[]`-dep effect would capture a null bodyRef and never attach.
  const bodyMounted = detail != null;
  useEffect(() => {
    const b = bodyRef.current;
    if (!b) return;
    const clear = () => dispatch({ type: 'CLEAR_CONV_PIN' });
    const SCROLL_KEYS = new Set([
      'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ',
    ]);
    const onKey = (e: KeyboardEvent) => { if (SCROLL_KEYS.has(e.key)) clear(); };
    b.addEventListener('wheel', clear, { passive: true });
    b.addEventListener('touchmove', clear, { passive: true });
    b.addEventListener('keydown', onKey);
    return () => {
      b.removeEventListener('wheel', clear);
      b.removeEventListener('touchmove', clear);
      b.removeEventListener('keydown', onKey);
    };
  }, [bodyMounted]);

  // The reader is reused across session switches (ConversationsView mounts it at
  // a fixed position), so drop stale ref callbacks when the session changes.
  // #188 — also drop the card-ref callbacks + the resolved card map so the next
  // conversation's subagent cards register fresh.
  useEffect(() => () => {
    refCallbacks.current.clear();
    cardRefCallbacks.current.clear();
    cardRefs.current.clear();
  }, [sessionId]);

  // The reused reader must not carry a force-pin across sessions (subagent_key is
  // only an agent-file hash). Reset on every session change; no-op on first mount.
  useEffect(() => { setForcedOpenKeys(new Set()); }, [sessionId]);

  // #175 — the reused reader must not carry the live-tail pill/scroll state across
  // sessions. Clearing `newCount` drops a stale "↓ N new" pill the instant we switch
  // conversations, and resetting `atBottomRef` keeps the next session's first live
  // append on its default stick-to-bottom path (until the user scrolls it).
  // #176 — also drop a stale floating "↑ Top of turn" button + its target so the
  // next conversation starts with the button hidden.
  useEffect(() => {
    setNewCount(0);
    // #217 S3 E2 — the open-precedence fold: an anchor/restore open lands the
    // user on a SPECIFIC turn (not the tail), so it must NOT force atBottom (else
    // a live append would yank the viewport to the bottom). A tail / legacy open
    // keeps the prior default (true → live appends stick). The openScrollIntent
    // layout effect re-confirms this once the first page resolves.
    atBottomRef.current = openIntent?.kind === 'anchor' || openIntent?.kind === 'restore' ? false : true;
    // P0 fix — re-arm the open-scroll-intent one-shot for the NEW open so its own
    // 'top'/'bottom' verdict applies once when the first page resolves. Safe to
    // reset here (a regular effect): the new session's first page hasn't fetched
    // yet, so the layout effect that consumes the latch only runs in a LATER
    // commit, after this reset.
    appliedIntentRef.current = false;
    setJumpTopVisible(false);
    jumpTopTargetRef.current = null;
    // #188 S4/C2 — the reused reader must not carry a prior conversation's
    // subagent open/known sets across sessions (subagent_key is only an
    // agent-file hash and can collide). Clearing both keeps the next session's
    // first live append counted correctly: an append into a thread that was
    // expanded in the OLD conversation but is collapsed in the new one must NOT
    // count (Bug 5 + #188 B6's per-session reset rationale).
    openKeysRef.current.clear();
    knownSubagentKeysRef.current.clear();
  }, [sessionId, openIntent]);

  // Load-in stagger bookkeeping. On a session change the reused reader must
  // forget which turns it has painted, so the new conversation's opening page
  // rises + staggers afresh — clearing seenRef alone resets "first page", which
  // the render-time classifier reads as `seenRef.size === 0` (no commit-flipped
  // flag to keep in sync).
  useEffect(() => {
    seenRef.current.clear();
  }, [sessionId]);

  // After each commit, mark every currently-rendered top-level group as seen.
  // Runs AFTER the render-time rise classifier has read the prior state
  // (refs/effects observe commit, the classifier observes render), so a turn
  // animates on exactly the frame it first appears and never again (Codex P2: a
  // render-time decision, not an effect-time mutation feeding back into the same
  // frame). Marking the first content page seen here is also what retires "first
  // page" for the stagger: the next render sees a non-empty seenRef. Keyed on
  // the group list so paged appends re-run it. The loading branch renders with
  // an empty `groups`, so this no-ops there and never consumes "first page"
  // before any real content has painted.
  useEffect(() => {
    for (const g of groups) {
      const uuid = g.kind === 'subagent'
        ? g.items[0]?.anchor.uuid
        : g.kind === 'tool_result_run'
          ? g.items[0]?.anchor.uuid
          : g.item.anchor.uuid;
      if (uuid) seenRef.current.add(uuid);
    }
    // §5 — recurse into nested subagents too so a child/grandchild card's root
    // uuid is marked seen (it never goes through the top-level rise classifier,
    // but this keeps the seen set complete for any future first-appearance cue).
    walkSubagents(groups, (n) => {
      const u = n.items[0]?.anchor.uuid;
      if (u) seenRef.current.add(u);
    });
  }, [groups]);

  // Render-time rise classifier (G1 §4b). Returns `['conv-rise', {style}]` for a
  // top-level group's FIRST appearance, or `['', undefined]` to suppress —
  // when reduced-motion is on, when the group was already painted (seenRef),
  // or when it OWNS the active jump target. The jump-target suppression MUST
  // be render-time (Codex P2): refs attach at commit BEFORE the jump effect
  // runs loadToTarget/scroll/flash, so the rise/no-rise choice is made while
  // rendering; the target then takes `conv-item--jumped` (the flash) WITHOUT
  // `conv-rise`, and the two never run on one element.
  const riseFor = useCallback(
    (anchorUuid: string, memberUuids: string[], idx: number): [string, React.CSSProperties | undefined] => {
      if (reduced) return ['', undefined];
      if (seenRef.current.has(anchorUuid)) return ['', undefined];
      const isJumpTarget =
        jump != null && jump.session_id === sessionId && memberUuids.includes(jump.uuid);
      if (isJumpTarget) return ['', undefined];
      // "First page" is computed at RENDER time from the seen-Set being empty —
      // NOT a commit-flipped flag. The populate effect runs AFTER this render
      // commits, so on the first CONTENT render seenRef is still empty for every
      // group and they all get the staggered `idx*40ms`. Any later first
      // appearance (paged in) sees a populated seenRef and fades with no stagger
      // so the scroll position doesn't lurch. The earlier loading branch renders
      // empty `groups`, so it never marks anything seen and never consumes the
      // first page before real content paints (the dead-stagger bug this fixes).
      const firstPage = seenRef.current.size === 0;
      const delay = firstPage ? `${idx * 40}ms` : '0ms';
      return ['conv-rise', { animationDelay: delay }];
    },
    [reduced, jump, sessionId],
  );

  const getItemRef = useCallback((item: ConversationItem) => {
    const cache = refCallbacks.current;
    const key = item.anchor.uuid;
    let cb = cache.get(key);
    if (!cb) {
      cb = (el: HTMLDivElement | null) => {
        // Map EVERY member uuid -> this element so a search hit on any folded
        // fragment resolves (anchor uuid is one prose fragment; the all-member
        // map is belt-and-suspenders per spec §3).
        for (const u of item.member_uuids) {
          if (el) itemRefs.current.set(u, el); else itemRefs.current.delete(u);
        }
      };
      cache.set(key, cb);
    }
    return cb;
  }, []);

  // #188 S3/B6 — a stable card-ref callback per bucket-root uuid: registers the
  // SidechainGroup's <details> element in cardRefs (open AND closed). Memoized
  // per rootUuid so the SidechainGroup's ref identity is stable across renders
  // (no detach/reattach thrash on paged appends / re-renders).
  const getCardRef = useCallback((rootUuid: string) => {
    const cache = cardRefCallbacks.current;
    let cb = cache.get(rootUuid);
    if (!cb) {
      cb = (el: HTMLElement | null) => {
        if (el) cardRefs.current.set(rootUuid, el);
        else cardRefs.current.delete(rootUuid);
      };
      cache.set(rootUuid, cb);
    }
    return cb;
  }, []);

  // §5 — the per-key machinery threaded to a SidechainGroup's recursive children
  // (and to the top-level group's own members for suppression). Keeps every
  // nesting level rendering with the SAME meta-lookup / force-open set / refs /
  // open-state / suppression as a top-level subagent. getItemRef/getCardRef/
  // onOpenChange are stable; the identity changes only when meta / the force set
  // / the suppression set change (so memoized cards don't churn on unrelated
  // re-renders).
  const childCtx = useMemo(
    () => ({
      subagentMeta: detail?.subagent_meta,
      forcedOpenKeys,
      getItemRef,
      getCardRef,
      onOpenChange: handleSubagentOpenChange,
      suppressToolUseIds,
      isMobile,
    }),
    [detail?.subagent_meta, forcedOpenKeys, getItemRef, getCardRef, handleSubagentOpenChange, suppressToolUseIds, isMobile],
  );

  // Reset the focused-turn cursor to the top on a session switch (the reused
  // reader carries no cursor across conversations).
  useEffect(() => { setFocusedIndex(0); }, [sessionId]);

  // Imperatively move the `conv-item--focused` class onto the cursor's child,
  // off every other. Re-runs on a cursor step AND when the rendered list changes
  // (paged appends / focus-mode switch grow or shrink `children`), so the ring
  // tracks the right element. Imperative (not a render prop) so memoized
  // MessageItems don't re-render. hidden_run markers carry `data-conv-marker`;
  // the focused class is never placed on one (stepFocus skips landing on them,
  // and the remap effect resolves the cursor to a real turn after a switch).
  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    const kids = thread.children;
    for (let i = 0; i < kids.length; i++) {
      const isMarker = (kids[i] as HTMLElement).dataset.convMarker != null;
      kids[i].classList.toggle('conv-item--focused', i === focusedIndex && !isMarker);
    }
  }, [focusedIndex, visible]);

  // #177 S5 §5 (Codex F5) — focus-coherence remap. When the mode changes the
  // rendered list reshuffles (turns vanish, hidden_run markers appear, time
  // markers recompute), so the raw index no longer points at the same turn.
  // Everything here is RENDERED-NODE space (`nodes` / `prevNodesRef`) — the same
  // space `focusedIndex` indexes thread.children in — so markers that precede the
  // cursor never offset the resolution. Resolve the formerly-focused node's uuid
  // in the OLD `nodes` list, then find that uuid in the NEW `nodes`; if it was
  // suppressed, land on the nearest FOLLOWING turn by original order; failing
  // that, clamp to the last index. Markers (time_marker + hidden_run) carry no
  // turn uuid, so they're never targets and the nudge below skips them. Keyed on
  // focusMode only — runs once per switch, reading prevNodesRef (the pre-switch
  // rendered list).
  useEffect(() => {
    const prev = prevNodesRef.current;
    const cur = focusedIndexRef.current;
    const prevNode = prev[cur];
    if (!prevNode) return;
    // Markers have no anchor uuid — `null` so they never match a node and never
    // satisfy the nearest-following walk (a marker can never be a focus target).
    const uuidOf = (n: (typeof nodes)[number]): string | null =>
      n.kind === 'time_marker' ? null : nodeUuid(n);
    const wantUuid = uuidOf(prevNode);
    // 1. Same uuid present in the new list? (null wantUuid — the cursor was on a
    //    marker, which can't happen via stepFocus — falls through to step 3.)
    let target = wantUuid == null ? -1 : nodes.findIndex((n) => uuidOf(n) === wantUuid);
    // 2. Else the nearest FOLLOWING node by original order: walk the old list
    //    forward from the focused position, taking the first node whose uuid
    //    survives into the new list.
    if (target < 0) {
      for (let i = cur + 1; i < prev.length; i++) {
        const u = uuidOf(prev[i]);
        if (u == null) continue;
        const hit = nodes.findIndex((n) => uuidOf(n) === u);
        if (hit >= 0) { target = hit; break; }
      }
    }
    // 3. Else clamp to the last index.
    if (target < 0) target = nodes.length - 1;
    // Never land on a marker — nudge forward then backward to the first real
    // turn (a hidden_run / time marker can sit between two keepers, so search
    // both ways).
    const isMarker = (i: number) => {
      const n = nodes[i];
      return n != null && (n.kind === 'time_marker' || n.kind === 'hidden_run');
    };
    if (target >= 0 && isMarker(target)) {
      let t = target;
      while (t < nodes.length && isMarker(t)) t++;
      if (t >= nodes.length) { t = target; while (t >= 0 && isMarker(t)) t--; }
      target = t;
    }
    if (target < 0) target = 0;
    setFocusedIndex(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusMode]);

  // Post-render snapshot of the rendered-node list for the next remap. Declared
  // AFTER the remap effect so a focus-mode switch lets the remap read the
  // PRE-switch list before this overwrites it (React runs effects in declaration
  // order).
  useEffect(() => {
    prevNodesRef.current = nodes;
  }, [nodes]);

  // G3 bindings. A `useMemo(() => [...], [])`-stable array (identity never
  // churns) whose action closures read refs — so a cursor step or a pagination
  // never re-registers the keymap. Each binding is conversations-view scoped
  // and inert while a modal is open or input-mode (rail search/filter) is
  // active; the keymap store already swallows single-char keys while a text
  // input is focused.
  const stepFocus = useCallback((delta: number) => {
    const thread = threadRef.current;
    if (!thread) return;
    const last = thread.children.length - 1;
    if (last < 0) return;
    const cur = focusedIndexRef.current;
    const dir = delta >= 0 ? 1 : -1;
    // Step at least one child, then keep walking PAST any `data-conv-marker`
    // child (the hidden_run buttons) so the cursor never lands on a marker — a
    // marker can never take keyboard focus (Codex F5). Stops at the edge.
    let next = cur + delta;
    while (next >= 0 && next <= last && (thread.children[next] as HTMLElement).dataset.convMarker != null) {
      next += dir;
    }
    next = Math.max(0, Math.min(last, next));
    // If clamping landed back on a marker (the run sits at an edge), there is no
    // real turn that way — stay put.
    if ((thread.children[next] as HTMLElement | undefined)?.dataset.convMarker != null) return;
    // At the last loaded group with more to come, kick a load; the cursor
    // advances on the next press once the new child has mounted.
    if (delta > 0 && cur === last && hasMoreRef.current) { void loadMoreRef.current(); return; }
    if (next === cur) return;
    dispatch({ type: 'CLEAR_CONV_PIN' }); // #188 B3 — j/k focus-step is explicit nav
    setFocusedIndex(next);
    const target = thread.children[next] as HTMLElement | undefined;
    target?.scrollIntoView({ block: 'nearest', behavior: reducedRef.current ? 'auto' : 'smooth' });
  }, []);

  // Collapse-all / expand-all sweep. A transient bulk-suppression class on the
  // thread sets `::details-content { transition: none }` for the frame so the
  // N disclosures snap rather than cascade; removed next tick (§4d).
  const sweepDetails = useCallback((open: boolean) => {
    const thread = threadRef.current;
    if (!thread) return;
    thread.classList.add('conv-reader-thread--bulk');
    thread.querySelectorAll('details').forEach((d) => { (d as HTMLDetailsElement).open = open; });
    const drop = () => thread.classList.remove('conv-reader-thread--bulk');
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(drop); else drop();
  }, []);

  const jumpToTop = useCallback(() => {
    const body = bodyRef.current;
    body?.scrollTo({ top: 0, behavior: reducedRef.current ? 'auto' : 'smooth' });
    setFocusedIndex(0);
    dispatch({ type: 'CLEAR_CONV_PIN' }); // #188 B3 — the `g` key is explicit nav
  }, []);

  // #177 S5 §4 — jump-to-next. Targets derive from the reader's full-session
  // `outline.turns` (Codex F4), NOT the paged-in detail. A jump-kind names which
  // target list to walk; `jumpNext` resolves the cursor (the scroll-sync turn,
  // else the focused child's data-uuid, else -1 = "before the start"), finds the
  // next/previous target via the pure `nextTarget`, and — on a hit — resets the
  // focus mode to `all` IF that mode would hide the target, then dispatches the
  // deep-link jump. A miss pulses the matching cluster button (reduced-motion:
  // no pulse). Stable closure: reads refs, so the keymap array never churns.
  // #184 — build the four target index lists + the uuid→index map over the
  // outline skeleton via the SHARED builder (outlineNavigation.ts), so the
  // reader keys and the OutlinePanel cluster can never drift. Memoized on
  // `outline` so a paged tick doesn't rebuild them; jumpNext reads via refs.
  const { indexByUuid: turnIndexByUuid, ...targetLists } = useMemo(
    () => buildOutlineTargets(outline?.turns ?? []),
    [outline],
  );
  const targetListsRef = useRef(targetLists);
  targetListsRef.current = targetLists;
  const turnIndexByUuidRef = useRef(turnIndexByUuid);
  turnIndexByUuidRef.current = turnIndexByUuid;

  // Transient 300ms pulse on the OutlinePanel cluster button for a kind. Skipped
  // entirely under reduced motion (spec §5 / §7). Found via data-jump-kind in
  // the DOM (the cluster lives in a sibling component).
  const pulseClusterButton = useCallback((kind: JumpKind) => {
    if (reducedRef.current) return;
    const btn = document.querySelector<HTMLElement>(`[data-jump-kind="${kind}"]`);
    if (!btn) return;
    btn.classList.add('conv-pulse-disabled');
    window.setTimeout(() => btn.classList.remove('conv-pulse-disabled'), 300);
  }, []);

  const jumpNext = useCallback((kind: JumpKind, dir: 1 | -1) => {
    const turns = outlineRef.current?.turns ?? [];
    if (turns.length === 0) return;
    const list = targetListsRef.current[kind];
    // Resolve the cursor in skeleton-index space. #188 B5 — prefer the explicit
    // pin (where the last jump LANDED) over the scroll-sync turn (the topmost
    // visible, which lags above a centered target); else the focused child's
    // data-uuid; else -1 ("before the start") so a forward jump finds the first
    // target.
    const byUuid = turnIndexByUuidRef.current;
    let cursor = -1;
    const cu = convPinnedUuidRef.current ?? currentTurnUuidRef.current;
    if (cu != null && byUuid.has(cu)) {
      cursor = byUuid.get(cu)!;
    } else {
      const focusedEl = threadRef.current?.children[focusedIndexRef.current] as HTMLElement | undefined;
      const du = focusedEl?.getAttribute('data-uuid');
      if (du != null && byUuid.has(du)) cursor = byUuid.get(du)!;
    }
    const targetIdx = nextTarget(list, cursor, dir);
    if (targetIdx == null) { pulseClusterButton(kind); return; }
    const turn = turns[targetIdx];
    // Reset to `all` IF the current mode would hide the target. Precise check
    // (spec §5): find the target's TOP-LEVEL RenderNode (recursing into nested
    // subagents), test nodeVisible. A node missing from `groups` (not yet paged
    // in) is treated as hidden → reset.
    const mode = focusModeRef.current;
    if (mode !== 'all') {
      const node = findTopLevelNodeFor(groupsRef.current, turn.uuid, { subagent_meta: subagentMetaRef.current });
      const targetHidden = node == null || !nodeVisible(node, mode);
      if (targetHidden) dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId: sessionIdRef.current,
      jump: { session_id: sessionIdRef.current, uuid: turn.uuid },
    });
  }, [pulseClusterButton]);
  const jumpNextRef = useRef(jumpNext);
  jumpNextRef.current = jumpNext;

  // #217 S3 E8 — direct jump to the LAST (most-recent) occurrence of a landmark
  // family (prompt / error), distinct from the e/E,u/U next/prev STEPPING. Lands
  // on targets.<kind>.at(-1) rather than walking backward from the latest turn.
  // Reuses the same OPEN_CONVERSATION jump pipeline (loadToTarget via the jump
  // effect + flash + pin) and the same focus-mode-unhide check as jumpNext.
  // Empty list → a graceful no-op (no pulse — this is a direct action, not a step).
  const jumpToLast = useCallback((kind: JumpKind) => {
    const turns = outlineRef.current?.turns ?? [];
    if (turns.length === 0) return;
    const list = targetListsRef.current[kind];
    const targetIdx = list.at(-1);
    if (targetIdx == null) return;  // no occurrence → no-op
    const turn = turns[targetIdx];
    const mode = focusModeRef.current;
    if (mode !== 'all') {
      const node = findTopLevelNodeFor(groupsRef.current, turn.uuid, { subagent_meta: subagentMetaRef.current });
      const targetHidden = node == null || !nodeVisible(node, mode);
      if (targetHidden) dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId: sessionIdRef.current,
      jump: { session_id: sessionIdRef.current, uuid: turn.uuid },
    });
  }, []);
  const jumpToLastRef = useRef(jumpToLast);
  jumpToLastRef.current = jumpToLast;

  // #177 S6 — the find bar reports its DEBOUNCED needle here; split into
  // highlight terms (whitespace-split, empties dropped) for the prose marks.
  // Stable identity so FindBar's onTermsChange effect doesn't re-fire per render.
  const onFindTermsChange = useCallback((terms: string) => {
    const split = terms.split(/\s+/).filter(Boolean);
    setFindTerms(split.length ? split : null);
  }, []);

  // #177 S6 — close-restore: return keyboard focus to the thread so j/k resume.
  const onFindClose = useCallback(() => {
    setFindTerms(null);
    threadRef.current?.focus?.();
  }, []);

  // #177 S6 — drop highlight terms whenever the bar closes (e.g. a session
  // switch closes find via the store) so stale marks don't linger.
  useEffect(() => {
    if (!convFindOpen) setFindTerms(null);
  }, [convFindOpen]);

  // `v` cycles the focus mode all → chat → prompts → errors → all.
  const cycleFocusMode = useCallback(() => {
    const order: FocusMode[] = ['all', 'chat', 'prompts', 'errors'];
    const cur = focusModeRef.current;
    const next = order[(order.indexOf(cur) + 1) % order.length];
    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: next });
  }, []);

  // #205 S1 — one stable toggle for the ☰ button AND the `o` key. Reads the
  // live isMobile mirror so the useMemo([]) keymap neither churns nor captures
  // a stale viewport across a resize.
  const toggleOutline = useCallback(() => {
    dispatch({ type: isMobileRef.current ? 'TOGGLE_CONV_OUTLINE_MOBILE' : 'TOGGLE_CONV_OUTLINE' });
  }, []);

  // #205 S2 (F3) — Find toggle, mirroring toggleOutline: one stable handler
  // shared by the button (the `/` keymap is the keyboard counterpart). Reads
  // live store state at click time (convFindOpen is store state, so no stale
  // closure) to open or close the find bar. No isMobile branch — the button
  // shows on both breakpoints; the find bar floats over the body either way.
  const toggleFind = useCallback(() => {
    dispatch({ type: getState().convFindOpen ? 'CLOSE_CONV_FIND' : 'OPEN_CONV_FIND' });
  }, []);

  const keymapBindings = useMemo(
    () => {
      // §4/§5 (Codex P2 #7) — the named-key guard also excludes an open filter
      // popover. The input-focus suppression only swallows SINGLE-char keys, so
      // `End` (a named key) would otherwise fire jump-to-latest while a cost
      // input in the popover is focused. convFiltersOpen gates it (alongside the
      // existing openModal/inputMode guards).
      const guard = () => !getState().openModal && getState().inputMode === null
                          && !getState().convFiltersOpen;
      const mk = (key: string, action: () => void) =>
        ({ key, scope: 'global' as const, view: 'conversations' as const, when: guard, action });
      return [
        mk('j', () => stepFocus(1)),
        mk('k', () => stepFocus(-1)),
        mk('[', () => sweepDetails(false)),
        mk(']', () => sweepDetails(true)),
        mk('g', () => jumpToTop()),
        mk('o', () => toggleOutline()),
        // Jump-to-next family. Uppercase (shift) = previous. KeyboardEvent.key
        // delivers the uppercase char under shift, so each register as its own
        // binding (Codex F4).
        mk('e', () => jumpNextRef.current('error', 1)),
        mk('E', () => jumpNextRef.current('error', -1)),
        mk('u', () => jumpNextRef.current('prompt', 1)),
        mk('U', () => jumpNextRef.current('prompt', -1)),
        mk('b', () => jumpNextRef.current('subagent', 1)),
        mk('B', () => jumpNextRef.current('subagent', -1)),
        mk('p', () => jumpNextRef.current('plan', 1)),
        mk('P', () => jumpNextRef.current('plan', -1)),
        // cache-failure-markers spec §4 — `c`/`C` jump to next/prev cache
        // rebuild. The `c` letter is collision-free here: main.tsx's `c`
        // (Sessions collapse) is scope:'sessions' → view:'dashboard', and the
        // keymap dispatcher gates by view, so the two never coexist. Guarded by
        // the opt-out (no-op when markers are off, so the key does nothing once
        // every cache surface is hidden) on TOP of the shared `guard`.
        {
          key: 'c', scope: 'global' as const, view: 'conversations' as const,
          when: () => guard() && markersEnabledRef.current,
          action: () => jumpNextRef.current('cache', 1),
        },
        {
          key: 'C', scope: 'global' as const, view: 'conversations' as const,
          when: () => guard() && markersEnabledRef.current,
          action: () => jumpNextRef.current('cache', -1),
        },
        mk('v', () => cycleFocusMode()),
        // #217 S3 E8 — direct jump to the LAST (most-recent) prompt / error,
        // distinct from u/U,e/E STEPPING. `a` = last user prompt ("ask"); `L`
        // = last error. Both are free single-char slots (no collision with the
        // taken conversations-view set j k [ ] g o e E u U b B p P c C v n N End).
        // Gated on the shared guard (no open modal / input mode / filter popover)
        // + the #156 conversations-view scope, like every other jump key. A
        // graceful no-op when the family is empty.
        mk('a', () => jumpToLastRef.current('prompt')),
        mk('L', () => jumpToLastRef.current('error')),
        // jump-to-latest spec §5 — `End` runs the same handler as the "Latest ↓"
        // control: reset to the tail, jump+flash the final turn. `guard` already
        // excludes the open filter popover (Codex P2 #7) so it never fires while
        // a filter input is focused. The handler no-ops when last_anchor is null.
        mk('End', () => { void jumpToLatestRef.current(); }),
        // #177 S6 — n/N step the find-bar matches, but ONLY while the bar is
        // open (the input-blurred case; the focused input owns Enter/Shift+Enter
        // itself). `guard` already excludes input-mode + open modals.
        {
          key: 'n', scope: 'global' as const, view: 'conversations' as const,
          when: () => guard() && convFindOpenRef.current,
          action: () => findStepRef.current?.(1),
        },
        {
          key: 'N', scope: 'global' as const, view: 'conversations' as const,
          when: () => guard() && convFindOpenRef.current,
          action: () => findStepRef.current?.(-1),
        },
      ];
    },
    // Actions are stable (refs-only), so the array is built once. The lint
    // disable mirrors the existing #160 effect's stable-closure rationale.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  useKeymap(keymapBindings);

  if (loading && !detail) return (
    <div className="conv-reader conv-reader--loading">
      <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><SpinnerIcon /></span>
        <div className="conv-state-title">Loading conversation…</div></div>
    </div>
  );
  if (error) return (
    <div className="conv-reader conv-reader--error">
      <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><WarningIcon /></span>
        <div className="conv-state-title">{error}</div></div>
    </div>
  );
  if (!detail) return (
    <div className="conv-reader conv-reader--empty">
      <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><ChatIcon /></span>
        <div className="conv-state-title">Select a conversation</div>
        <div className="conv-state-hint">Choose one from the list to start reading.</div></div>
    </div>
  );

  return (
    <div className="conv-reader">
      <div className="conv-reader-head">
        {mobileBack && (
          <button type="button" className="conv-back" onClick={() => dispatch({ type: 'SELECT_CONVERSATION', sessionId: null })}>← Back</button>
        )}
        {/* #177 S5 — flex row: title/meta block grows, controls right-align. The
            Task-3 `float: right` on the outline toggle is dropped (a reviewer
            flagged it as fragile); both controls reflow into the flex row. */}
        <div className="conv-reader-headmain">
          <div className="conv-reader-title">{title || detail.session_id}</div>
          <div className="conv-reader-meta">
            {detail.project_label || '—'} · {detail.git_branch ?? '—'} · {fmt.usd2(detail.cost_usd)} · {(isMobile ? Array.from(new Set(detail.models.map(abbreviateModel))) : detail.models).join(', ')}
          </div>
        </div>
        <div className="conv-reader-controls">
          {/* #177 S5 §5 — focus-mode segmented control. A labeled radiogroup;
              each button's aria-checked reflects the active mode (the valid
              selected-state attribute for role="radio" — #184 dropped the
              invalid aria-pressed, which belongs to toggle buttons, not radios).
              Errors carries a count badge from the outline stats when > 0. */}
          <div className="conv-focus-seg" role="radiogroup" aria-label="Focus mode">
            {(['all', 'chat', 'prompts', 'errors'] as const).map((m) => {
              const labels: Record<FocusMode, string> = { all: 'All', chat: 'Chat', prompts: 'Prompts', errors: 'Errors' };
              const errCount = outline?.stats.error_count ?? 0;
              return (
                <button
                  key={m}
                  type="button"
                  className={['conv-focus-seg-btn', focusMode === m ? 'conv-focus-seg-btn--on' : ''].filter(Boolean).join(' ')}
                  role="radio"
                  aria-checked={focusMode === m}
                  onClick={() => dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: m })}
                >
                  {labels[m]}
                  {m === 'errors' && errCount > 0 && (
                    <span className="conv-focus-seg-badge">{errCount}</span>
                  )}
                </button>
              );
            })}
          </div>
          {/* jump-to-latest spec §5 — "Latest ↓" control. Hidden when
              last_anchor is null (a genuinely empty conversation). Disabled with
              a spinner glyph while jumpToLatest() resets to the tail. Bound to `End`. */}
          {detail.last_anchor && (
            <button
              type="button"
              className="conv-jump-latest"
              aria-label="Jump to latest message"
              title="Jump to latest (End)"
              disabled={jumpingLatest}
              onClick={() => { void jumpToLatest(); }}
            >{jumpingLatest ? '… ' : ''}Latest ↓</button>
          )}
          {/* #205 S2 (F3) — Find toggle. Mirrors the outline toggle's
              aria-pressed semantics + chrome; gives the `/` shortcut a visible,
              tappable counterpart (the only find affordance on touch). */}
          <button
            type="button"
            className="conv-find-toggle"
            aria-pressed={convFindOpen}
            aria-label="Find in conversation"
            title="Find in conversation (/)"
            onClick={toggleFind}
          >🔍 Find</button>
          {/* outline toggle. Visible on desktop + mobile; aria-pressed reflects
              the EFFECTIVE open flag (mobile sheet flag on mobile, persisted pref
              on desktop). On mobile it opens the slide-over sheet (#205 S1). */}
          <button
            type="button"
            className="conv-outline-toggle"
            aria-pressed={effectiveOutlineOpen}
            aria-label="Toggle session outline"
            title="Toggle session outline (o)"
            onClick={toggleOutline}
          >☰ Outline</button>
        </div>
      </div>
      {/* #177 S6 — the floating in-conversation find bar. Absolutely
          positioned top-right inside the reader column (zero layout shift). The
          stepRef wires its cursor to the reader's n/N bindings. */}
      {convFindOpen && (
        <FindBar
          sessionId={sessionId}
          onClose={onFindClose}
          onTermsChange={onFindTermsChange}
          stepRef={findStepRef}
        />
      )}
      <div className="conv-reader-body" ref={bodyRef} onScroll={onBodyScroll}>
        {/* #217 S3 E2 — TOP sentinel (mirror of the bottom one). When it scrolls
            into view at the head of the loaded window, loadPrev prepends the
            previous page (scroll-anchored). Rendered only while there are more
            reverse pages. */}
        {hasPrev && <div ref={topSentinelRef} className="conv-load-sentinel conv-load-sentinel--top">Loading earlier…</div>}
        <HighlightContext.Provider value={findTerms}>
        <TranscriptContext.Provider value={transcriptCtx}>
        <div className="conv-reader-thread" ref={threadRef}>
          {nodes.map((g, idx) => {
            // #177 S5 §6 — an inter-turn gap/day marker. Real DOM text
            // (screen-reader visible), role="separator", data-conv-marker so j/k
            // and the focus-class effect skip it (never a keyboard stop).
            if (g.kind === 'time_marker') {
              const gapTxt = g.gapSeconds != null ? `⏸ ${fmt.gapDuration(g.gapSeconds)} later` : null;
              const text =
                gapTxt && g.dayLabel ? `${gapTxt} · ${g.dayLabel}`
                : gapTxt ? gapTxt
                : `— ${g.dayLabel} —`;
              return (
                <div key={g.key} className="conv-time-marker" data-conv-marker="" role="separator">
                  {text}
                </div>
              );
            }
            // #177 S5 §5 — a coalesced run of focus-hidden nodes. Renders as a
            // marker button (data-conv-marker: never keyboard-focusable, never
            // gets conv-item--focused). Clicking it drops back to `all` and jumps
            // to the first hidden node so the user can resume reading there.
            if (g.kind === 'hidden_run') {
              return (
                <button
                  key={`hr-${g.firstUuid}`}
                  type="button"
                  className="conv-hidden-run"
                  data-conv-marker=""
                  onClick={() => {
                    dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
                    dispatch({
                      type: 'OPEN_CONVERSATION',
                      sessionId,
                      jump: { session_id: sessionId, uuid: g.firstUuid },
                    });
                  }}
                >· {g.count} hidden ·</button>
              );
            }
            if (g.kind === 'subagent') {
              // The thread's member_uuids (every fragment) decide jump-target
              // suppression so a folded/sidechain jump target is covered.
              const members = g.items.flatMap((it) => it.member_uuids);
              const [riseClass, riseStyle] = riseFor(g.items[0].anchor.uuid, members, idx);
              return (
                <SidechainGroup
                  key={`sc-${g.subagentKey}`}
                  subagentKey={g.subagentKey}
                  items={g.items}
                  nested={g.nested}
                  meta={detail.subagent_meta?.[g.subagentKey]}
                  getItemRef={getItemRef}
                  // #188 S3/B6 — the bucket-root uuid (the same value the
                  // outline subagent entry jumps to). It tags the card's
                  // <details> via data-uuid and keys it in cardRefs.
                  rootUuid={g.items[0].anchor.uuid}
                  getCardRef={getCardRef}
                  // #188 S4/C2 — lift the thread's open-state so the "↓ N new"
                  // pill counts only VISIBLE appends (Bug 5).
                  onOpenChange={handleSubagentOpenChange}
                  // §5 (Codex P1-D) — force-open iff this key is in the ancestor
                  // chain set (a jump into a nested target opens this parent too).
                  forceOpen={detail.session_id === sessionId && forcedOpenKeys.has(g.subagentKey)}
                  isMobile={isMobile}
                  riseClassName={riseClass}
                  riseStyle={riseStyle}
                  // §5 — recursive nesting: the child subagent threads + this
                  // node's depth + the per-key machinery for every nested level.
                  children={g.children}
                  depth={g.depth}
                  childCtx={childCtx}
                />
              );
            }
            if (g.kind === 'tool_result_run') {
              // Collapsed orphan-result run (#164). Members render their own
              // MessageItem so each keeps its data-uuid + per-member ref for the
              // #160 jump; the disclosure is open by default so a jump target
              // inside it is reachable without a force-open dance.
              const members = g.items.flatMap((it) => it.member_uuids);
              const [riseClass, riseStyle] = riseFor(g.items[0].anchor.uuid, members, idx);
              return (
                <details
                  key={`trr-${g.items[0].anchor.uuid}`}
                  className={['conv-toolresult-run', riseClass].filter(Boolean).join(' ')}
                  style={riseStyle}
                  open
                >
                  <summary>
                    <span className="conv-chev" aria-hidden="true" />
                    <ResultIcon /> {g.items.length} tool results
                  </summary>
                  <div className="conv-toolresult-run-body">
                    {g.items.map((item) => (
                      <MessageItem key={item.anchor.uuid} item={item} ref={getItemRef(item)} suppressToolUseIds={suppressToolUseIds} />
                    ))}
                  </div>
                </details>
              );
            }
            const [riseClass, riseStyle] = riseFor(g.item.anchor.uuid, g.item.member_uuids, idx);
            return (
              <MessageItem
                key={g.item.anchor.uuid}
                item={g.item}
                ref={getItemRef(g.item)}
                className={riseClass}
                style={riseStyle}
                // §5 — suppress a spawn chip on a main-thread item (its nested
                // subagent card is canonical).
                suppressToolUseIds={suppressToolUseIds}
              />
            );
          })}
        </div>
        </TranscriptContext.Provider>
        </HighlightContext.Provider>
        {hasMore && <div ref={sentinelRef} className="conv-load-sentinel">Loading more…</div>}
      </div>
      {/* #175 F4 — "↓ N new" pill. A child of .conv-reader (NOT the scrolling
          .conv-reader-body), absolutely positioned so it floats over the body
          without scrolling with it. Shown only while scrolled up with unseen
          live-appended turns; clicking it scrolls to the newest turn. */}
      {newCount > 0 && !atBottomRef.current && (
        <button type="button" className="conv-new-pill" onClick={jumpToNew}>↓ {newCount} new</button>
      )}
      {/* #176 — floating "↑ Top of turn" button. A child of .conv-reader (NOT the
          scrolling .conv-reader-body), absolutely positioned bottom-right so it
          floats over the body without scrolling with it and clears the
          bottom-center "↓ N new" pill. Shown only when the current turn's start
          is scrolled off; clicking it returns to that turn's start. */}
      {jumpTopVisible && (
        <button
          type="button"
          className="conv-jump-top"
          onClick={jumpToTurnTop}
          title="Jump to the start of this turn"
          aria-label="Jump to the start of this turn"
        >↑</button>
      )}
    </div>
  );
}
