import React, { forwardRef, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Virtuoso, type VirtuosoHandle, type ListItem, type Components } from 'react-virtuoso';
import { dispatch, getState, selectMarkersEnabled, subscribeStore } from '../store/store';
import { useConversation } from '../hooks/useConversation';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { useIsWide } from '../hooks/useIsWide';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { groupSidechains, flattenSubagents, walkSubagents, type RenderNode } from './groupSidechains';
import { isSystemMarker } from './systemMarkers';
import { FindBar } from './FindBar';
import { ExportMenu } from './ExportMenu';
import { FocusMoreMenu, type FocusSubagentOption } from './FocusMoreMenu';
import { FocusCompactMenu } from './FocusCompactMenu';
import { ReaderOverflowMenu } from './ReaderOverflowMenu';
import { HighlightContext, type HighlightTerms } from './HighlightContext';
import { MessageItem } from './MessageItem';
import { SidechainGroup } from './SidechainGroup';
import { useStableSet, useStableMap, useMonotonicMax } from './useStableIdentity';
import { CumulativeCostChip } from './CumulativeCostChip';
import { cumulativeCostThrough } from './cumulativeCost';
import { ResultIcon, SpinnerIcon, WarningIcon, ChatIcon, SearchIcon } from './ConvIcons';
import { TranscriptContext } from './TranscriptContext';
import { applyFocusMode, nodeUuid, nodeVisible, type FocusMode } from './applyFocusMode';
import { insertTimeMarkers, type TimedNode } from './insertTimeMarkers';
import { nodeIndexForUuid } from './nodeIndexForUuid';
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

// #232 — the stable per-kind React identity key for a rendered node. Reuses the
// exact keys the pre-virtualization `nodes.map` used (so the virtual index only
// drives Virtuoso scroll stability, never React identity): time markers carry
// their own stable `key` (re-keyed off adjacent uuids in insertTimeMarkers, T1),
// every other kind keys off its anchor/root uuid. Fed to <Virtuoso computeItemKey>.
function nodeKey(node: TimedNode): React.Key {
  switch (node.kind) {
    case 'time_marker': return node.key;
    case 'hidden_run': return `hr-${node.firstUuid}`;
    case 'subagent': return `sc-${node.subagentKey}`;
    case 'tool_result_run': return `trr-${node.items[0].anchor.uuid}`;
    case 'item': return node.item.anchor.uuid;
  }
}

// #232 — the cursor TURN uuid of a render node (null for markers, which are never
// a keyboard cursor stop). Mirrors `nodeUuid` for the turn-bearing kinds.
function nodeTurnUuid(node: TimedNode): string | null {
  if (node.kind === 'time_marker' || node.kind === 'hidden_run') return null;
  return nodeUuid(node);
}

// #232 — Virtuoso's per-item wrapper. A known className (`.conv-reader-item`)
// gives the CSS retargets (T6) a stable hook through Virtuoso's wrapper, and the
// VIRTUAL `data-index` rides through so the index math is inspectable. Virtuoso
// forwards `data-index` / `data-item-index` (both the VIRTUAL index =
// firstItemIndex + arrayIndex) / `style` via the spread props, but does NOT
// inject `aria-posinset` / `aria-setsize` (verified against react-virtuoso 4.18.7
// — its Item props carry only the data-* / style / key set). So under
// `role="feed"` we set them OURSELVES: the 1-based ARRAY position
// (virtualIndex − firstItemIndex + 1) and the set size (total node count), both
// derived from values threaded through Virtuoso's `context`. Without them a
// screen reader can't announce "item N of M" for the virtualized feed.
const ReaderItem = forwardRef<HTMLDivElement, Record<string, unknown>>(
  function ReaderItem(props, ref) {
    // Virtuoso's `data-item-index` is the VIRTUAL index (firstItemIndex +
    // arrayIndex), so the 1-based feed position = (virtual − firstItemIndex) + 1.
    // Both `firstItemIndex` and `setSize` (the total node count) ride through
    // Virtuoso's `context`. (react-virtuoso 4.18.7 does NOT inject posinset/setsize
    // itself — verified against its dist — so role="feed" needs them set here.)
    const virtualIndex = Number(props['data-item-index']);
    const ctx = props.context as { setSize?: number; firstItemIndex?: number } | undefined;
    const setSize = ctx?.setSize;
    const firstItemIndex = ctx?.firstItemIndex;
    const aria =
      Number.isFinite(virtualIndex) && typeof setSize === 'number' && typeof firstItemIndex === 'number'
        ? { 'aria-posinset': virtualIndex - firstItemIndex + 1, 'aria-setsize': setSize }
        : {};
    // `context` is a Virtuoso-internal prop — don't spread it onto the DOM node.
    const { context: _context, ...domProps } = props;
    return <div {...domProps} {...aria} ref={ref} className="conv-reader-item" />;
  },
) as unknown as Components<TimedNode>['Item'];

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
  const jump = useSyncExternalStore(subscribeStore, () => getState().conversationJump);
  // #228 S3 B3 — read the protected-anchor sources BEFORE the hook so the
  // windowed-cap trim never drops the turn the user is on / navigating to. The
  // current-turn + pinned reads are also used below; declared once here.
  const currentTurnUuidEarly = useSyncExternalStore(subscribeStore, () => getState().convCurrentTurnUuid);
  const convPinnedUuidEarly = useSyncExternalStore(subscribeStore, () => getState().convPinnedUuid);
  // The set passed INTO the hook (Codex P1 — the hook doesn't own these anchors):
  // the active find match + in-flight jump target (both flow through
  // `conversationJump` for THIS session), the keyboard current turn, and the
  // explicit pin. Memoized on the member uuids so identity is stable between
  // unrelated renders.
  const jumpUuidForSession = jump && jump.session_id === sessionId ? jump.uuid : null;
  const protectedUuids = useMemo(() => {
    const s = new Set<string>();
    if (jumpUuidForSession) s.add(jumpUuidForSession);
    if (currentTurnUuidEarly) s.add(currentTurnUuidEarly);
    if (convPinnedUuidEarly) s.add(convPinnedUuidEarly);
    return s;
  }, [jumpUuidForSession, currentTurnUuidEarly, convPinnedUuidEarly]);
  const { detail, loading, error, hasMore, hasPrev, openScrollIntent, lastOp, loadMore, loadPrev, loadToTarget, jumpToLatest: hookJumpToLatest, tailRevision, virtualFirstItemIndex } = useConversation(sessionId, { outlineTurns: outline?.turns, openIntent, protectedUuids });
  // #232 — the imperative Virtuoso handle (scrollToIndex for jumps / keyboard
  // nav / the "↓ N new" pill) and a live mirror of the firstItemIndex so
  // `itemContent`'s array-index math (`virtualIndex − firstItemIndex`) reads the
  // current offset without re-subscribing. Virtuoso speaks the VIRTUAL index
  // space (firstItemIndex + arrayIndex); the array index feeds riseFor's stagger.
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const firstItemIndexRef = useRef(virtualFirstItemIndex);
  firstItemIndexRef.current = virtualFirstItemIndex;
  // #228 S1 (F3) — after CLOSE_COMPARE, return focus to the compare trigger
  // once the single reader has rendered. The reader loads async, so a single
  // rAF fired at close time can land during the loading branch and miss the
  // trigger; instead we consume the store flag once detail is ready.
  const compareCloseFocusPending = useSyncExternalStore(
    subscribeStore, () => getState().compareCloseFocusPending,
  );
  useEffect(() => {
    if (!compareCloseFocusPending) return;
    if (loading && !detail) return;          // wait for the detail branch
    const el =
      document.getElementById('conv-compare-with') ??
      document.querySelector<HTMLElement>('.conv-reader');
    el?.focus();
    dispatch({ type: 'CLEAR_COMPARE_CLOSE_FOCUS' });
  }, [compareCloseFocusPending, loading, detail]);
  const outlineOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineOpen);
  // #205 S1 / #228 S3 F1 — the ephemeral outline-sheet flag + the effective
  // open-state. The SHEET governs whenever the column is hidden (≤1100px =
  // !isWide); the persisted column pref governs only when wide. So the ☰/`o`
  // toggle and aria-pressed track convOutlineMobileOpen across the whole
  // no-column band (mobile AND the 641–1100 tablet band, where the ☰ is now a
  // live control), and convOutlineOpen only ≥1101px (was keyed on isMobile in
  // #205 S1; widened to isWide so the tablet-band ☰ stops lying — §8).
  const outlineMobileOpen = useSyncExternalStore(subscribeStore, () => getState().convOutlineMobileOpen);
  const isMobile = useIsMobile();
  const isWide = useIsWide();
  const effectiveOutlineOpen = isWide ? outlineOpen : outlineMobileOpen;
  // #177 S5 — the active focus mode (all/chat/prompts/errors) + scroll-sync
  // cursor uuid. focusMode drives the `visible` pipeline below; the cursor uuid
  // seeds jump-to-next.
  const focusMode = useSyncExternalStore(subscribeStore, () => getState().convFocusMode);
  // #228 S3 B3 — reuse the early reads (declared above the hook for protectedUuids)
  // so there's one subscription per store slice. The keyboard jump-to-next
  // (e/u/b/p) resolves its cursor from `pinned ?? currentTurnUuid` so a repeat
  // forward press steps strictly past where the last jump LANDED (#188 B5 / #187).
  const currentTurnUuid = currentTurnUuidEarly;
  const convPinnedUuid = convPinnedUuidEarly;
  // #217 S6 F4 — the current session's bookmarks, threaded into the reader's
  // buildOutlineTargets memo so the `bookmark` jump list (the i/I keys) stays in
  // lock-step with the OutlinePanel cluster. A toggle re-derives the targets.
  const convBookmarks = useSyncExternalStore(subscribeStore, () => getState().convBookmarks);
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
  const [findTerms, setFindTerms] = useState<HighlightTerms | null>(null);
  // Live closure to the find bar's cursor stepper (n/N drive it while the bar
  // is open + the input is blurred). FindBar assigns its `step` here each render.
  const findStepRef = useRef<((delta: number) => void) | null>(null);
  const reduced = useReducedMotion();
  // #232 — the bottom/top sentinel IntersectionObservers + the `prependPendingRef`
  // scroll-anchor snapshot are GONE: Virtuoso's `startReached`/`endReached` drive
  // the load triggers and `firstItemIndex` (owned in useConversation, T2) keeps
  // the viewport pinned across a reverse-page prepend without any scrollTop math.
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
  // #232 — the jump/find FLASH is now render-driven (Codex P0-1). Under
  // virtualization the target row may mount AFTER scrollToIndex settles, so the
  // old imperative `el.classList.add('conv-item--jumped')` would no-op against an
  // unmounted element. Instead `jumpedUuid` is state: `renderNode` passes
  // `flashed={uuid === jumpedUuid}` to the matching card, which applies
  // `conv-item--jumped` whenever it (re)mounts. A single timer clears it after 2s.
  const [jumpedUuid, setJumpedUuid] = useState<string | null>(null);
  // Tracks the pending flash-clear timeout so it can be cancelled on unmount /
  // session change and superseded on a rapid re-jump (no two overlapping 2s
  // timers racing).
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
  // #232 — the bulk `[`/`]` expand/collapse sweep on the DATA MODEL (Codex P1-1).
  // The old sweep walked `thread.querySelectorAll('details')`, which under
  // virtualization sees only the mounted overscan window — so it silently missed
  // off-screen sidechains. Instead a monotonic `rev` + a desired `open` flag is
  // threaded to every SidechainGroup (mounted AND on next mount), which adopts the
  // sweep's open-state in render whenever the rev advances. So a sweep reaches
  // every group regardless of whether it is currently rendered.
  const [bulkSweep, setBulkSweep] = useState<{ rev: number; open: boolean }>({ rev: 0, open: false });
  // G1 §4b load-in stagger. A Set of anchor uuids already painted at least
  // once (the `daily-fade-in` seen-Set precedent, index.css:2032): each
  // top-level group rises exactly once on first appearance, so paged appends
  // and re-renders don't re-animate already-visible turns. Populated by a
  // post-commit effect AFTER the render-time classifier has read it, so the
  // decision is stable for that frame.
  const seenRef = useRef<Set<string>>(new Set());
  // #231 — per-uuid freeze of the rise-class decision (see riseFor). Cleared on
  // session change alongside seenRef.
  const riseCacheRef = useRef<Map<string, [string, React.CSSProperties | undefined]>>(new Map());

  // G3 keyboard navigation. A focused-turn cursor over the rendered nodes. The
  // `conv-item--focused` class is now RENDER-DRIVEN (#232 Codex P1-1): `renderNode`
  // adds it to the node whose uuid === `cursorUuid`, keyed per-uuid so the class
  // only flips on a real cursor move (a uuid is stable across head mutations,
  // unlike a raw index) — that keeps the MessageItem memo intact instead of
  // re-rendering the whole window. The ref mirrors the state so the stable keymap
  // action closures read the live cursor without re-registering on every move.
  const [focusedIndex, setFocusedIndex] = useState(0);
  const focusedIndexRef = useRef(0);
  focusedIndexRef.current = focusedIndex;
  // #232 — the keyboard cursor's TURN UUID (Codex P1-1 + #231 memo invariant).
  // The render-driven ring matches THIS uuid, not the array index: an index would
  // flip the `conv-item--focused` className on a reverse-page PREPEND (the same
  // node sits at a different index), defeating the MessageItem memo for the whole
  // window — the #231 cascade. A uuid is stable across head mutations, so the ring
  // class only changes on a real cursor move. `setCursor(i)` sets both: the index
  // (used by remap / stepping / the keymap closures) and the uuid (the render
  // key). null = no ring (markers and the empty state).
  const [cursorUuid, setCursorUuid] = useState<string | null>(null);
  const cursorUuidRef = useRef<string | null>(null);
  cursorUuidRef.current = cursorUuid;
  // #177 S5 — the focus-mode remap keys off the PREVIOUS render's RENDERED-NODE
  // list (`nodes` = the full logical render list: filtered turns + hidden_run
  // markers + time markers; under virtualization only a window is mounted, but
  // `nodes` and `focusedIndex` are nodes-space, not DOM-space). `focusedIndex`
  // indexes that nodes array, so the remap must read its prev list AND compute its
  // target in nodes-space too — a marker-less `visible` list would mis-resolve
  // `prevNodesRef[cur]`
  // (and the target) by the count of any markers that precede the cursor.
  // `prevNodesRef` is updated in a post-render effect AFTER the remap reads it,
  // so the remap sees the list the user was actually looking at.
  const prevNodesRef = useRef<ReturnType<typeof insertTimeMarkers>>([]);
  // #232 — both are now assigned imperatively (threadRef from the ReaderThread
  // List wrapper's ref callback, bodyRef from Virtuoso's scrollerRef), so they
  // must be MUTABLE refs (`| null` widens to MutableRefObject).
  const threadRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  // Stable mirrors so the `useMemo(() => [...], [])` keymap array never churns.
  const hasMoreRef = useRef(hasMore);
  hasMoreRef.current = hasMore;
  const loadMoreRef = useRef(loadMore);
  loadMoreRef.current = loadMore;
  // #217 S3 E2 — top-edge mirrors for the stable top-sentinel observer closure.
  const hasPrevRef = useRef(hasPrev);
  hasPrevRef.current = hasPrev;
  // #232 — reverse paging now needs no scroll-anchor snapshot. Virtuoso's
  // `firstItemIndex` (decremented by the prepended count in useConversation's
  // state, T2, in the SAME commit as the prepend) keeps the viewport pinned to
  // the same turn across a `?before=` prepend — the classic reverse-infinite-
  // scroll problem is solved by the library. So `doLoadPrev` is just `loadPrev()`
  // (Virtuoso's `startReached` calls it when the head scrolls into view).
  const doLoadPrev = useCallback(() => {
    void loadPrev();
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
  // #228 S3 F1 — live mirror of isWide for the same stable toggleOutline closure:
  // the column/sheet decision is keyed on isWide (≥1101 = column), so the toggle
  // must read the live wide-state, not the mobile-state.
  const isWideRef = useRef(isWide);
  isWideRef.current = isWide;
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
  // #232 — paging-arming gate (the cold-load freeze fix's defense-in-depth layer).
  // FALSE on every session open; flipped TRUE only once the initial open
  // positioning has SETTLED, so Virtuoso's transient startReached/endReached
  // during cold mount + the programmatic jump drain can't trigger paging (which
  // would re-enter the drain that's positioning the window). A real user
  // scroll-to-edge only happens after settle, so genuine reverse/forward paging
  // still works. `jumpDrainingRef` additionally suppresses both edges WHILE a
  // loadToTarget drain is in flight (set around the reader's await of it).
  const reversePagingArmedRef = useRef(false);
  const forwardPagingArmedRef = useRef(false);
  const jumpDrainingRef = useRef(false);
  const armPagingTimerRef = useRef<number | null>(null);
  // Arm both edges (idempotent). Called when the open settles: the first
  // atBottomStateChange (tail open lands at the bottom), the jump pipeline's
  // scrollToIndex landing (deep-link lands on the target), or the 750ms fallback
  // armed on each session open — whichever fires first.
  const armPaging = useCallback(() => {
    reversePagingArmedRef.current = true;
    forwardPagingArmedRef.current = true;
    if (armPagingTimerRef.current != null) { window.clearTimeout(armPagingTimerRef.current); armPagingTimerRef.current = null; }
  }, []);
  // P1 (cross-branch review) — track the FIRST item's stable anchor uuid so the
  // stick-to-bottom effect can recognise a PREPEND by a top-edge advance (the
  // first-item id changed) regardless of which code path prepended. This is more
  // robust than `prependPendingRef` alone: that ref is set only by the top-sentinel
  // path (doLoadPrev), but a JUMP to an early target prepends INSIDE the hook
  // (loadToTarget → fetchPrev) where the reader-owned ref can never be set, so a
  // tail-opened (hasMore===false) session would mis-read the old back-of-window
  // turns as "↓ N new". A first-item-id advance catches EVERY prepend source.
  const prevFirstIdRef = useRef<string | null>(null);
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

  // #232 — bumped by Virtuoso's `itemsRendered` whenever the rendered range
  // changes (scroll mounts/unmounts rows), so the scroll-sync IntersectionObserver
  // re-registers over the current mounted itemRefs/cardRefs. `renderedRangeRef`
  // dedups: only bump when the [first,last] range actually moves (itemsRendered
  // fires on every measure tick, not just range changes).
  const [renderedRangeRev, setRenderedRangeRev] = useState(0);
  const renderedRangeRef = useRef<{ first: number; last: number }>({ first: -1, last: -1 });

  const onBodyScroll = useCallback(() => {
    const b = bodyRef.current;
    if (!b) return;
    // #232 — the at-bottom signal moved to Virtuoso's `atBottomStateChange`
    // (accurate under virtualization, where `scrollHeight` is only an estimate of
    // the measured rows). `onBodyScroll` stays only for the #176 jump-to-top
    // button (a viewport-geometry read over the mounted rows).

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

  // #232 — the "↓ N new" pill COUNT. The actual stick-to-bottom moved onto
  // Virtuoso's `followOutput` (Task 3); this effect now only feeds `setNewCount`
  // with the PRESERVED `visibleAdded` classifier (Codex P1-2 — do NOT replace it
  // with a raw append count). Keyed on `lastOp.rev` (+ hasMore so prevHasMoreRef
  // tracks each commit), NOT items.length: a prepend+far-trim (the windowed DOM
  // cap) can keep items.length flat while still mutating the window, and a length
  // key would miss it. A plain useEffect (no longer pre-paint — Virtuoso owns the
  // scroll, so there is no manual scrollTo to land before paint).
  useEffect(() => {
    const items = detail?.items ?? [];
    const len = items.length;
    const firstId = items[0]?.anchor.uuid ?? null;
    // #228 S3 B3 — direction comes straight from the hook op, not a top-edge id
    // advance. A reverse-page PREPEND (top-sentinel doLoadPrev OR a jump's
    // backward branch inside the hook) must NOT be mistaken for a live append:
    // it grows items.length too and on a tail open (hasMore === false) would
    // satisfy the live discriminator, bumping "↓ N new" by the prior window size.
    // A reset replaces the whole window and is likewise neither a stick nor a
    // count. Bail (advancing the prev-trackers) so this effect never miscounts a
    // prepend as a live append; #232 Virtuoso's `firstItemIndex` keeps the
    // viewport pinned across the prepend, so there is no scroll-anchor snapshot to
    // preserve here.
    if (lastOp != null && lastOp.op !== 'append') {
      // A RESET replaces the whole window, so it must SEED the known-subagent set
      // from the ENTIRE new window (mirrors the old code, where the null-detail
      // transient zeroed prevLen and the next page seeded the full slice). Without
      // this the new session's first live append into a thread present on page-1
      // would mis-read as a brand-new group and over-count "↓ N new".
      if (lastOp.op === 'reset') {
        for (const it of items) {
          if (it.subagent_key != null) knownSubagentKeysRef.current.add(it.subagent_key);
        }
      }
      prevLenRef.current = len;
      prevHasMoreRef.current = hasMore;
      prevFirstIdRef.current = firstId;
      return;
    }
    // The count of genuinely-new TAIL items comes from the op (addedBottom), not
    // `len - prevLen` (which a top-trim in the same commit would corrupt). The
    // newly-appended items are the LAST `added` items of the window — trim-safe
    // because any top-drop shifts indices but never the bottom slice's content.
    const added = lastOp?.op === 'append' ? lastOp.addedBottom : 0;
    // Live append (not the final pagination page): already fully paged before
    // this growth, and not the very first page load (prevLen > 0).
    const live = added > 0 && prevHasMoreRef.current === false && prevLenRef.current > 0;
    // #188 S4/C2 — classify each newly-appended item by VISIBILITY against the
    // OLD known-set + open-set (Bug 5): top-level (+1); first item of a
    // brand-new subagent group (+1, deduped per key per tick); append into an
    // already-EXPANDED known thread (+1); append into an existing COLLAPSED
    // known thread (+0, below the fold). Computed only on a live append; during
    // non-live growth (first page / pagination) the tail just SEEDS the
    // known-set below WITHOUT counting.
    const tail = added > 0 ? items.slice(len - added) : [];
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
    // #232 — stick is `followOutput`'s job now (it sticks when already at bottom).
    // When the user is scrolled UP (atBottomRef false), surface the pill with the
    // SAME `visibleAdded` count as before. When at bottom, followOutput sticks and
    // atBottomStateChange resets the count, so no pill bump here.
    if (live && visibleAdded > 0 && !atBottomRef.current) {
      setNewCount((n) => n + visibleAdded);
    }
    prevLenRef.current = len;
    prevHasMoreRef.current = hasMore;
    prevFirstIdRef.current = firstId;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastOp?.rev, hasMore]);

  const jumpToNew = useCallback(() => {
    // #232 — the pill scrolls to the LAST node via Virtuoso, aligned to the bottom
    // edge, instead of a raw scrollTo({top: scrollHeight}) (scrollHeight is only an
    // estimate under virtualization). scrollToIndex takes the 0-based DATA (array)
    // index (NOT the firstItemIndex-offset virtual index — see the jump-landing fix
    // note), so the last node's array index is nodes.length − 1.
    const count = nodesRef.current.length;
    if (count > 0) {
      virtuosoRef.current?.scrollToIndex({
        index: count - 1,
        align: 'end',
        behavior: reducedRef.current ? 'auto' : 'smooth',
      });
    }
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
  // #217 S6 F3 — the session's heaviest LOADED per-turn cost (the micro-bar
  // denominator). Max over loaded assistant items. Provided ONCE on the transcript
  // context so memoized MessageItems don't subscribe to recompute it.
  const sessionMaxTurnCostRaw = useMemo(() => {
    let m = 0;
    for (const it of detail?.items ?? []) {
      if (it.kind === 'assistant' && typeof it.cost_usd === 'number') m = Math.max(m, it.cost_usd);
    }
    return m;
  }, [detail?.items]);
  // #231 — monotonic ratchet (the "monotonic ref" the comment above anticipated).
  // The windowed DOM cap (added in this same fix) can TRIM the max-cost item OUT
  // of the loaded window, which would LOWER this value. Because it rides on the
  // TranscriptContext that EVERY memoized MessageItem consumes (via useMaxTurnCost),
  // any change re-renders the entire rendered window — bypassing React.memo
  // entirely — and a non-monotonic value churns on every prepend AND every trim,
  // the O(n²) re-render cascade that froze the cold deep-link reader (~80s). Ratchet
  // it so it only ever rises within a session (reset on session switch): "heaviest
  // turn seen this session" is the correct micro-bar denominator AND a stable
  // context that lets the memo hold across paging/trim commits.
  const sessionMaxTurnCost = useMonotonicMax(sessionMaxTurnCostRaw, sessionId);
  // #217 S6 F3 — cumulative cost through the topmost-visible turn for the header
  // chip. `approx` ≡ hasPrev: any unloaded earlier page makes the prefix-sum a
  // lower bound (the honesty marker, Codex P1).
  const cumCost = useMemo(
    () => cumulativeCostThrough(detail?.items ?? [], currentTurnUuid, { hasPrev: !!hasPrev }),
    [detail?.items, currentTurnUuid, hasPrev],
  );
  // #217 S5 E4 — the TOP-LEVEL subagent keys present in the loaded groups, with
  // labels from subagent_meta (kind, then the spawning description) and a key
  // fallback when meta is empty (buckets exist even without meta — Codex P1-4).
  // Feeds the focus "▾ More" menu's Subagent submenu. Top-level only, since
  // `subagent:<key>` filters at the top-level node (Codex P1-3).
  const subagentOptions = useMemo<FocusSubagentOption[]>(() => {
    const meta = detail?.subagent_meta ?? {};
    const out: FocusSubagentOption[] = [];
    const seen = new Set<string>();
    for (const g of groups) {
      if (g.kind !== 'subagent' || seen.has(g.subagentKey)) continue;
      seen.add(g.subagentKey);
      const m = meta[g.subagentKey];
      out.push({ key: g.subagentKey, label: (m?.kind || m?.description || '').trim() });
    }
    return out;
  }, [groups, detail?.subagent_meta]);
  // §5 (Codex P1-C) — the spawn-chip suppression set: every spawn `tool_use_id`
  // the kernel linked to a subagent. A `tool_call` with this id is suppressed in
  // favor of its nested card. tool_use_id granularity (one item can hold several
  // spawns); an UNLINKED spawn (>16 KB clip) has no nested card and no entry
  // here, so its chip still renders. Stable identity (memoized) keeps the
  // memoized MessageItems' memo valid across ticks.
  const suppressToolUseIdsRaw = useMemo(() => {
    const s = new Set<string>();
    for (const m of Object.values(detail?.subagent_meta ?? {})) {
      if (m.spawn_tool_use_id) s.add(m.spawn_tool_use_id);
    }
    return s;
  }, [detail?.subagent_meta]);
  // #231 — collapse identity to content: the server re-sends `subagent_meta` (a
  // fresh object, usually identical content) on every page apply, so the raw
  // useMemo identity churns each prepend and defeats the MessageItem memo for the
  // whole window. Stabilize so the identity changes only when a spawn id does.
  const suppressToolUseIds = useStableSet(suppressToolUseIdsRaw);
  // #228 S2 (A3) — tool_use_id → kind for spawns whose subagent card is LOADED
  // (walk the emitted render tree, NOT whole-session subagent_meta), so a
  // connector never dangles above a paged-out agent. `suppressToolUseIds` stays
  // whole-session (to avoid a duplicate chip when a paged-out agent later
  // loads), but a spawn whose bucket is still paged out is ABSENT here, so it
  // renders neither a chip (suppressed) nor a dangling connector. Stable
  // identity (memoized) keeps the memoized MessageItems valid across ticks.
  const spawnKindByToolUseIdRaw = useMemo(() => {
    const m = new Map<string, string>();
    for (const node of flattenSubagents(groups)) {
      const meta = detail?.subagent_meta?.[node.subagentKey];
      if (meta?.spawn_tool_use_id) m.set(meta.spawn_tool_use_id, meta.kind ?? '');
    }
    return m;
  }, [groups, detail?.subagent_meta]);
  // #231 — `groups` recomputes to a NEW identity on every prepend AND every
  // windowed-DOM-cap trim (both rewrite `detail.items`), so without this the Map
  // identity churns each commit and re-renders the whole window. Stabilize to
  // content: identity changes only when a loaded spawn entry actually changes.
  const spawnKindByToolUseId = useStableMap(spawnKindByToolUseIdRaw);
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
  // #232 — the Virtuoso `context` threaded to every ReaderItem so it can set
  // `aria-posinset` (1-based feed position = virtualIndex − firstItemIndex + 1) and
  // `aria-setsize` (total node count) under role="feed". Memoized so the object
  // identity only changes when the count or offset actually moves.
  const virtuosoContext = useMemo(
    () => ({ setSize: nodes.length, firstItemIndex: virtualFirstItemIndex }),
    [nodes.length, virtualFirstItemIndex],
  );
  // #232 — live mirror of the render-node list so the stable (empty-dep) keymap /
  // pill closures can read the CURRENT nodes (the "↓ N new" pill's last-node
  // scroll, the j/k cursor clamp) without re-registering.
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  // #232 — set the keyboard cursor to a nodes-array index: stores BOTH the index
  // (remap / stepping / keymap closures) and the turn UUID (the stable render key
  // the ring matches, so a prepend can't flip the class — #231). A marker index
  // clears the uuid (no ring) but keeps the index for stepping math.
  const setCursor = useCallback((nodeIndex: number) => {
    setFocusedIndex(nodeIndex);
    const n = nodesRef.current[nodeIndex];
    setCursorUuid(n ? nodeTurnUuid(n) : null);
  }, []);
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
  // #217 S6 F3 — maxTurnCost rides along too so the per-turn cost micro-bar can
  // size itself from context (no per-item store subscription); the provider
  // identity flips when the session's heaviest loaded turn cost changes.
  const transcriptCtx = useMemo(
    () => ({ sessionId, focusMode, fmtCtx, markersEnabled, maxTurnCost: sessionMaxTurnCost }),
    [sessionId, focusMode, fmtCtx, markersEnabled, sessionMaxTurnCost],
  );

  // #232 — the bottom sentinel observer, the top sentinel observer, and the
  // prepend scroll-anchor `useLayoutEffect` are all DELETED. Lazy-load on scroll
  // now rides Virtuoso's `startReached` (→ doLoadPrev) / `endReached` (→ loadMore)
  // props (wired on the <Virtuoso> below), and `firstItemIndex` (T2) keeps the
  // viewport pinned across a prepend with no scrollTop math.

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
  // #232 — land through Virtuoso's `scrollToIndex` (not a raw `scrollTop` write,
  // which fights the library's scroll management). A 'bottom' open jumps to the
  // last node aligned to the bottom edge; a 'top' open jumps to the first. The
  // one-shot latch + atBottomRef arming are unchanged.
  const appliedIntentRef = useRef(false);
  useEffect(() => {
    if (openScrollIntent == null) return;
    if (appliedIntentRef.current) return;          // already applied this open
    const len = detail?.items.length ?? 0;
    if (!len) return;                               // wait for the first content page
    const nodeCount = nodes.length;
    if (!nodeCount) return;                         // wait for the render list too
    appliedIntentRef.current = true;
    // #232 fix — scrollToIndex takes the 0-based DATA (array) index, NOT the
    // firstItemIndex-offset virtual index (which the library clamps + ignores —
    // see the jump-landing fix note). A 'bottom' open lands on the last node
    // (array index nodeCount − 1); a 'top' open lands on the first (array index 0).
    if (openScrollIntent === 'bottom') {
      virtuosoRef.current?.scrollToIndex({ index: nodeCount - 1, align: 'end' });
      atBottomRef.current = true;
    } else {
      virtuosoRef.current?.scrollToIndex({ index: 0, align: 'start' });
      atBottomRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openScrollIntent, detail?.items.length, nodes.length]);

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
    // #232 — re-register on `visible` (paged append / session switch / focus-mode
    // change) AND on Virtuoso's rendered-range change (`renderedRangeRev`, bumped
    // by onItemsRendered): under virtualization, SCROLLING mounts/unmounts rows
    // without changing `visible`, so the observe-set must be rebuilt over the
    // fresh mounted itemRefs/cardRefs whenever the window of rendered items moves.
    // The observer keeps its existing topmost-VISIBLE pick (re-deriving from
    // Virtuoso's overscan-inclusive range would shift the semantics).
  }, [visible, renderedRangeRev]);

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
      // #232 — suppress startReached/endReached while the programmatic jump drain
      // pages the window toward the target. Even with the arming gate, a drain that
      // runs AFTER paging is armed (an in-session jump, not just the cold open)
      // must not let Virtuoso's settle-time edge hits re-enter paging mid-drain.
      jumpDrainingRef.current = true;
      try {
        await loadToTarget(jump.uuid);
      } finally {
        jumpDrainingRef.current = false;
      }
      if (cancelled) return;
      // #232 — the mode-hidden check runs FIRST, before resolving the node index.
      // A non-`all` focus mode coalesces the target's turn into a `hidden_run`
      // marker, and `nodeIndexForUuid` matches a hidden_run by its `firstUuid`, so
      // a target that IS the run's first uuid would otherwise resolve onto the
      // marker and we'd scroll/flash the run instead of unhiding the turn. Reset to
      // `all` so the turn re-renders, then the focusMode re-fire lands the jump via
      // the index path below. (Carry-forward: this ordering is exactly why
      // nodeIndexForUuid's hidden_run case can stay firstUuid-only.)
      const mode = focusModeRef.current;
      if (mode !== 'all') {
        // §5 — the target's TOP-LEVEL RenderNode (recursing into nested subagents)
        // decides visibility under the mode. A node missing from `groups` (paged in
        // but its node not built yet) is treated as not-yet-paged → leave it to the
        // detail-growth re-fire; do NOT reset (diverges from jumpNext's snapshot).
        const node = findTopLevelNodeFor(groupsRef.current, jump.uuid, detail);
        if (node != null && !nodeVisible(node, mode)) {
          dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
          return; // re-run under `all`: the node renders + its index resolves
        }
      }
      // Resolve the target to its TOP-LEVEL node POSITION (Codex P0-2), not a
      // mounted DOM element: an off-screen-but-loaded target has no element under
      // virtualization. `nodeIndexForUuid` matches an anchor / folded member /
      // subagent-subtree uuid and returns both index spaces; the virtual index
      // feeds Virtuoso's scrollToIndex. The jump is data-presence-driven, not
      // DOM-presence-driven.
      const hit = nodeIndexForUuid(nodes, jump.uuid, virtualFirstItemIndex);
      if (hit) {
        // A subagent CARD-ROOT jump (the bucket-root uuid — an outline subagent
        // entry / collapsed-card flash) aligns the card HEAD to the top; a normal
        // turn / a deep inner-member find-jump centers (#204).
        const hitNode = nodes[hit.arrayIndex];
        const isCardRoot = hitNode.kind === 'subagent' && hitNode.items[0].anchor.uuid === jump.uuid;
        // §5 (Codex P1-D) — a find-jump into a NESTED subagent MEMBER (not the
        // bucket root) must force-open the owning ancestor chain FIRST, so the
        // member renders, takes the render-driven flash, and can be centered. The
        // node itself is already in `nodes` (so `hit` resolved on the card), but
        // its member element is ref-less while the thread is collapsed. Set the
        // force keys and return; the forcedOpenKeys re-fire then scrolls + flashes.
        if (!isCardRoot) {
          const targetItem = detail.items.find((it) => it.member_uuids.includes(jump.uuid));
          if (targetItem && targetItem.subagent_key != null) {
            const chain = ancestorKeys(targetItem.subagent_key, detail.subagent_meta);
            if (chain.some((k) => !forcedOpenKeys.has(k))) {
              setForcedOpenKeys((prev) => {
                const next = new Set(prev);
                for (const k of chain) next.add(k);
                return next;
              });
              return; // wait for the thread to open, then re-fire to scroll + flash
            }
          }
        }
        const align: 'start' | 'center' = isCardRoot ? 'start' : 'center';
        // #232 fix (P1-A) — react-virtuoso's scrollToIndex takes the 0-based DATA
        // (array) index. `firstItemIndex` shifts the `itemContent` index + the prepend
        // bookkeeping, but NOT the scrollToIndex input space — passing the virtual
        // index (firstItemIndex + arrayIndex, ~1,000,000+) lands far outside
        // [0, totalCount], so react-virtuoso clamps + ignores it and a warm in-session
        // jump never scrolls toward its target (measured in-browser: scrollTop did not
        // move at all; scrollToIndex with the DATA index moved, with the virtual index
        // did not). The cold deep-link only appeared to work because its near/in-window
        // target also caught the DOM scrollIntoView precise-align pass below. Pass the
        // array index. (itemContent still receives the virtual index from Virtuoso, so
        // the `index - firstItemIndex` conversion there is unchanged + correct.)
        virtuosoRef.current?.scrollToIndex({ index: hit.arrayIndex, align, behavior: reduced ? 'auto' : 'smooth' });
        // #232 — the jump has LANDED on the target: the open has settled, so arm
        // paging. From here a user scroll to either edge legitimately pages.
        armPaging();
        // Render-driven flash (#232): the row may mount AFTER the scroll settles,
        // and the class still lands because renderNode reads `jumpedUuid` on every
        // (re)mount — unmount-safe, unlike the old imperative classList.add.
        setJumpedUuid(jump.uuid);
        // #188 B2 — pin the landing so the outline selects EXACTLY this target and
        // a repeat forward jump-to-next steps strictly past it (closes #187).
        dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: jump.uuid });
        // #177 S6 — sync the keyboard cursor to the jumped node so j/k (and find's
        // n/N) resume from the match (sets both the index + the stable ring uuid).
        setCursor(hit.arrayIndex);
        // Within-row second pass once the row mounts (#177 S6 / #204): open the
        // turn's collapsed disclosures for a tool/thinking find hit, then center
        // the specific member element inside the (now-mounted) row. rAF guarantees
        // the row committed; isConnected guards a session switch in the gap.
        // #232/#204 — a NESTED subagent CARD root (jump.uuid is a card bucket-root
        // but its TOP-LEVEL node is the ROOT ancestor, so isCardRoot is false) is
        // resolved from cardRefs and re-aimed at its <summary> HEAD (block 'start'),
        // not centered — a tall card centered hides its head far above the fold.
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(() => requestAnimationFrame(() => {
            const cardEl = cardRefs.current.get(jump.uuid);
            const memberEl = itemRefs.current.get(jump.uuid) ?? cardEl;
            if (memberEl && memberEl.isConnected) {
              if (jump.expand_details) {
                memberEl.querySelectorAll('details:not([open])').forEach((d) => { (d as HTMLDetailsElement).open = true; });
              }
              if (cardEl) {
                // A card root: align its head to the top (#204).
                const head = cardEl.querySelector('summary') ?? cardEl;
                head.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
              } else if (!isCardRoot) {
                memberEl.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' });
              }
            }
          }));
        }
        if (highlightTimerRef.current != null) window.clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = window.setTimeout(() => {
          setJumpedUuid(null);
          highlightTimerRef.current = null;
        }, 2000);
        dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
        setForcedOpenKeys(new Set()); // reset for the next jump (threads stay open via their latches)
        return;
      }
      // #232 — no node hit. Two remaining reasons the target's node is absent from
      // `nodes` (the mode-hidden reset already ran ABOVE, before resolving the
      // index):
      //
      //   (1) It just paged in but its node isn't built/rendered yet (the
      //       detail-growth / forcedOpenKeys re-fire handles that).
      //   (2) It lives inside a COLLAPSED subagent thread whose top-level card
      //       didn't yet resolve a hit (e.g. the chain isn't open) — force the
      //       owning ancestor chain open so the target's node enters `nodes`.
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
    // once per jump. #232 — `nodes` + `virtualFirstItemIndex` are deps so the
    // post-load scrollToIndex re-fires once the target is paged into the render
    // list (a prepend can shift the virtual index without growing items.length).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, sessionId, detail?.items.length, hasMore, forcedOpenKeys, focusMode, nodes, virtualFirstItemIndex]);

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
  // #232 — also reset the bulk-sweep state so a prior conversation's expand/collapse-
  // all doesn't sweep the new session's sidechains on mount.
  useEffect(() => {
    setForcedOpenKeys(new Set());
    setBulkSweep({ rev: 0, open: false });
  }, [sessionId]);

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
    // P1 (cross-branch review) — drop the prior conversation's first-item id so
    // the new session's opening page is treated as a fresh seed (null prev), not
    // a prepend (which would otherwise bail-and-reseed harmlessly anyway, but an
    // explicit null keeps the discriminator's intent legible).
    prevFirstIdRef.current = null;
    // #232 — DISARM paging for the new open and arm the fallback timer. Both edges
    // stay no-op until the open settles (atBottomStateChange / jump-landing /
    // this 750ms fallback). Clearing any prior timer first keeps it one-shot per
    // open. The fallback guarantees paging is eventually usable even if neither
    // settle signal fires (e.g. a single-page conversation that never reaches the
    // bottom-state edge nor runs a jump).
    reversePagingArmedRef.current = false;
    forwardPagingArmedRef.current = false;
    jumpDrainingRef.current = false;
    if (armPagingTimerRef.current != null) window.clearTimeout(armPagingTimerRef.current);
    armPagingTimerRef.current = window.setTimeout(() => {
      reversePagingArmedRef.current = true;
      forwardPagingArmedRef.current = true;
      armPagingTimerRef.current = null;
    }, 750);
  }, [sessionId, openIntent]);

  // #232 — clear the arming fallback timer on unmount so it never fires into a
  // torn-down reader.
  useEffect(() => () => {
    if (armPagingTimerRef.current != null) { window.clearTimeout(armPagingTimerRef.current); armPagingTimerRef.current = null; }
  }, []);

  // Load-in stagger bookkeeping. On a session change the reused reader must
  // forget which turns it has painted, so the new conversation's opening page
  // rises + staggers afresh — clearing seenRef alone resets "first page", which
  // the render-time classifier reads as `seenRef.size === 0` (no commit-flipped
  // flag to keep in sync).
  useEffect(() => {
    seenRef.current.clear();
    riseCacheRef.current.clear();  // #231 — the new session's items rise afresh
    // #232 — drop a stale render-driven jump flash + its pending clear-timer so
    // the next conversation doesn't carry the prior session's highlight.
    setJumpedUuid(null);
    if (highlightTimerRef.current != null) { window.clearTimeout(highlightTimerRef.current); highlightTimerRef.current = null; }
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
      // #231 — FREEZE each item's rise decision the first time it renders, and
      // return that SAME tuple (stable className + stable style-object identity)
      // on every later render. Without this, an item rendered with `conv-rise`
      // flips to `['', undefined]` once the post-commit effect marks it seen — and
      // because that effect is a ref mutation (no re-render), the flip is deferred
      // to the NEXT commit, which is a reverse-page PREPEND. On that prepend EVERY
      // retained item's className AND style change at once, defeating the
      // MessageItem React.memo for the whole window — the O(n²) re-render cascade
      // behind the cold-load freeze (measured: ~2.4× the mounted window per
      // prepend). `conv-rise` uses `animation-fill-mode: both` ending at the
      // natural state, so keeping the class after the one-shot entrance animation
      // is visually inert (and a stable class never re-triggers the animation).
      const cached = riseCacheRef.current.get(anchorUuid);
      if (cached) return cached;
      const isJumpTarget =
        jump != null && jump.session_id === sessionId && memberUuids.includes(jump.uuid);
      let result: [string, React.CSSProperties | undefined];
      if (reduced || isJumpTarget || seenRef.current.has(anchorUuid)) {
        result = ['', undefined];
      } else {
        // "First page" is computed at RENDER time from the seen-Set being empty —
        // the populate effect runs AFTER this render commits, so on the first
        // CONTENT render seenRef is still empty for every group and they all get
        // the staggered `idx*40ms`. A later first appearance (paged in) sees a
        // populated seenRef and fades with no stagger so the scroll doesn't lurch.
        const firstPage = seenRef.current.size === 0;
        result = ['conv-rise', { animationDelay: firstPage ? `${idx * 40}ms` : '0ms' }];
      }
      // Don't freeze a TRANSIENT jump-target suppression: once the jump clears the
      // item should be free to settle (or rise). Every other decision is stable
      // for the life of the session and is frozen so the memo holds.
      if (!isJumpTarget) riseCacheRef.current.set(anchorUuid, result);
      return result;
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
      spawnKindByToolUseId,
      isMobile,
      // #232 — the bulk-sweep state so nested sidechains adopt expand/collapse-all.
      bulkSweep,
    }),
    [detail?.subagent_meta, forcedOpenKeys, getItemRef, getCardRef, handleSubagentOpenChange, suppressToolUseIds, spawnKindByToolUseId, isMobile, bulkSweep],
  );

  // Reset the focused-turn cursor to the top on a session switch (the reused
  // reader carries no cursor across conversations). #232 — also clear the ring
  // uuid so a stale cursor turn from the prior conversation doesn't flash.
  useEffect(() => { setFocusedIndex(0); setCursorUuid(null); }, [sessionId]);

  // #232 — default the cursor to the FIRST real turn once content renders (the
  // pre-virtualization default was index 0). Only when no cursor is set yet (a
  // user j/k/jump takes over), so this fires once per open. Skips leading markers.
  useEffect(() => {
    if (cursorUuid != null) return;
    const idx = nodes.findIndex((n) => nodeTurnUuid(n) != null);
    if (idx >= 0) setCursor(idx);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cursorUuid, nodes]);

  // #232 — the keyboard cursor ring is now RENDER-DRIVEN (Codex P1-1): `renderNode`
  // adds `conv-item--focused` to the node at `focusedIndex` (the nodes-array
  // index), so the old imperative DOM-walking effect (which under virtualization
  // could only reach the mounted overscan window) is gone. `focusedIndex` is the
  // single source of truth in nodes-space; a marker is never a cursor stop
  // (stepFocus skips them, and the remap resolves to a real turn after a switch).

  // #177 S5 §5 (Codex F5) — focus-coherence remap. When the mode changes the
  // rendered list reshuffles (turns vanish, hidden_run markers appear, time
  // markers recompute), so the raw index no longer points at the same turn.
  // Everything here is RENDERED-NODE space (`nodes` / `prevNodesRef`) — the same
  // nodes-space `focusedIndex` indexes (NOT DOM-space: under virtualization only a
  // window is mounted, but the cursor ring is uuid-keyed and the remap walks the
  // full `nodes` array) — so markers that precede the cursor never offset the
  // resolution. Resolve the formerly-focused node's uuid
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
    setCursor(target);
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
    // #232 — the cursor walks `nodes` INDEX space (Codex P1-1), not DOM children:
    // under virtualization `thread.children` holds only the mounted overscan
    // window, so a DOM walk can't move the cursor past it. `nodesRef` is the live
    // render list; a marker (time_marker / hidden_run) is never a cursor stop.
    const nodeList = nodesRef.current;
    const last = nodeList.length - 1;
    if (last < 0) return;
    const isMarkerAt = (i: number): boolean => {
      const n = nodeList[i];
      return n != null && (n.kind === 'time_marker' || n.kind === 'hidden_run');
    };
    // Resolve the CURRENT cursor index from its UUID (the index ref can be stale
    // after a head mutation; the uuid is the source of truth — #231). Fall back to
    // the index ref, then 0.
    const curUuid = cursorUuidRef.current;
    let cur = curUuid != null ? nodeList.findIndex((n) => nodeTurnUuid(n) === curUuid) : -1;
    if (cur < 0) cur = Math.min(Math.max(0, focusedIndexRef.current), last);
    const dir = delta >= 0 ? 1 : -1;
    // Step at least one, then keep walking PAST any marker so the cursor never
    // lands on one (a marker can never take keyboard focus — Codex F5). Stops at
    // the edge.
    let next = cur + delta;
    while (next >= 0 && next <= last && isMarkerAt(next)) {
      next += dir;
    }
    next = Math.max(0, Math.min(last, next));
    // If clamping landed back on a marker (the run sits at an edge), there is no
    // real turn that way — stay put.
    if (isMarkerAt(next)) return;
    // At the last loaded node with more to come, kick a load; the cursor advances
    // on the next press once the new node renders.
    if (delta > 0 && cur === last && hasMoreRef.current) { void loadMoreRef.current(); return; }
    if (next === cur) return;
    dispatch({ type: 'CLEAR_CONV_PIN' }); // #188 B3 — j/k focus-step is explicit nav
    setCursor(next);
    // Bring the cursor's node into view via Virtuoso (the row may be unmounted —
    // scrollToIndex mounts it). #232 fix — scrollToIndex takes the 0-based DATA
    // (array) index, NOT the firstItemIndex-offset virtual index (which the library
    // clamps + ignores — see the jump-landing fix note). `next` is already the
    // array index into `nodes`.
    virtuosoRef.current?.scrollToIndex({
      index: next,
      align: 'center',
      behavior: reducedRef.current ? 'auto' : 'smooth',
    });
  }, [setCursor]);

  // #232 — Collapse-all / expand-all sweep on the DATA MODEL (Codex P1-1).
  // Advancing `bulkSweep.rev` makes every SidechainGroup (mounted or not) adopt
  // the new open-state in render, so off-screen sidechains are swept too. The
  // transient `--bulk` class still snaps the `::details-content` transition for
  // the mounted ones so the visible cascade doesn't animate; removed next tick.
  const sweepDetails = useCallback((open: boolean) => {
    setBulkSweep((s) => ({ rev: s.rev + 1, open }));
    const thread = threadRef.current;
    if (thread) {
      thread.classList.add('conv-reader-thread--bulk');
      const drop = () => thread.classList.remove('conv-reader-thread--bulk');
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(drop); else drop();
    }
  }, []);

  const jumpToTop = useCallback(() => {
    // #232 — route through Virtuoso (not a raw body.scrollTo): the mounted window
    // may not include the first item, so a raw scrollTop:0 would land short of it.
    // #232 fix — scrollToIndex takes the 0-based DATA (array) index, NOT the
    // firstItemIndex-offset virtual index (which the library clamps + ignores — see
    // the jump-landing fix note). The first loaded node is array index 0.
    virtuosoRef.current?.scrollToIndex({
      index: 0,
      align: 'start',
      behavior: reducedRef.current ? 'auto' : 'smooth',
    });
    setCursor(0);
    dispatch({ type: 'CLEAR_CONV_PIN' }); // #188 B3 — the `g` key is explicit nav
  }, [setCursor]);

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
    () => buildOutlineTargets(outline?.turns ?? [], convBookmarks),
    [outline, convBookmarks],
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
      // #232 — the keyboard cursor's turn uuid IS the source of truth (the cursored
      // node may be UNMOUNTED under virtualization, and the index ref can be stale
      // after a head mutation, so a DOM/index read is wrong). Use cursorUuidRef.
      const du = cursorUuidRef.current;
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

  // #217 S5 F7 — the header completion chip jumps to the final main-thread task
  // snapshot turn, reusing the same OPEN_CONVERSATION jump pipeline the outline
  // landmarks use (loadToTarget + scroll + flash + pin via the jump effect).
  const jumpToCompletion = useCallback((uuid: string) => {
    dispatch({
      type: 'OPEN_CONVERSATION',
      sessionId: sessionIdRef.current,
      jump: { session_id: sessionIdRef.current, uuid },
    });
  }, []);

  // #177 S6 / #217 S4 / #223 — the find bar reports its DEBOUNCED needle + the
  // case + regex flags here; build the highlight context value. Terms mode
  // whitespace-splits; regex mode passes the source for best-effort inline
  // highlighting (supersedes S4 decision b). Stable identity so FindBar's
  // onTermsChange effect doesn't re-fire per render.
  const onFindTermsChange = useCallback((needle: string, caseSensitive: boolean, regex: boolean) => {
    if (regex) {
      setFindTerms(needle ? { kind: 'regex', source: needle, caseSensitive } : null);
      return;
    }
    const split = needle.split(/\s+/).filter(Boolean);
    setFindTerms(split.length ? { kind: 'terms', terms: split, caseSensitive } : null);
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

  // #205 S1 / #228 S3 F1 — one stable toggle for the ☰ button AND the `o` key.
  // Reads the live isWide mirror so the useMemo([]) keymap neither churns nor
  // captures a stale viewport across a resize. When wide (≥1101px) it flips the
  // persisted column pref; otherwise (the whole ≤1100px no-column band) it flips
  // the ephemeral sheet flag — so the tablet-band ☰ opens the sheet, not a lie.
  const toggleOutline = useCallback(() => {
    dispatch({ type: isWideRef.current ? 'TOGGLE_CONV_OUTLINE' : 'TOGGLE_CONV_OUTLINE_MOBILE' });
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
        // #217 S3 F8 — `m`/`M` step to the next/prev compaction landmark (the
        // compaction-summary turns, #191). Mirrors the c/C cache pattern; `m`/`M`
        // are free single-char slots (no collision with the taken conversations-
        // view set j k [ ] g o e E u U b B p P c C v n N End a L). Gated by the
        // shared guard + the #156 conversations-view scope like every jump key.
        mk('m', () => jumpNextRef.current('compaction', 1)),
        mk('M', () => jumpNextRef.current('compaction', -1)),
        // #217 S6 F4 — `i`/`I` step to the next/prev bookmark (the ★ jump family),
        // reusing the reader's real jump dispatcher (jumpNextRef) exactly like
        // e/E. `t` toggles a bookmark on the CURRENT turn — the explicit pin (where
        // the last jump landed) if set, else the scroll-sync topmost-visible turn;
        // a no-op when neither is set. `i`/`I`/`t` come from the free single-char
        // set (h i t w x y z) confirmed unused in the conversations view. Gated by
        // the shared `guard` + the #156 conversations-view scope like every jump key.
        mk('i', () => jumpNextRef.current('bookmark', 1)),
        mk('I', () => jumpNextRef.current('bookmark', -1)),
        mk('t', () => {
          const u = getState().convPinnedUuid ?? getState().convCurrentTurnUuid;
          if (u) dispatch({ type: 'TOGGLE_BOOKMARK', uuid: u });
        }),
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

  // #232 — Virtuoso's `List` wrapper IS the `.conv-reader-thread`. It carries the
  // focus target (`threadRef`, tabIndex={-1} so onFindClose's restore lands here
  // and j/k run through the document keymap, never thread focus) and the thread
  // styling. Built ONCE (empty-dep useMemo) so the <Virtuoso components> object
  // never churns Virtuoso's internals across reader re-renders; it captures
  // `threadRef` (a stable ref) so the closure stays correct.
  const virtuosoComponents = useMemo<Components<TimedNode>>(() => {
    const ReaderThread = forwardRef<HTMLDivElement, { children?: React.ReactNode }>(
      function ReaderThread(props, ref) {
        return (
          <div
            ref={(el) => {
              threadRef.current = el;
              if (typeof ref === 'function') ref(el);
              else if (ref) (ref as React.MutableRefObject<HTMLDivElement | null>).current = el;
            }}
            className="conv-reader-thread"
            tabIndex={-1}
          >
            {props.children}
          </div>
        );
      },
    ) as unknown as Components<TimedNode>['List'];
    return { List: ReaderThread, Item: ReaderItem };
    // threadRef is a stable ref — the components must be built once so Virtuoso's
    // internal state doesn't reset on every reader render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // #232 — the per-node render switch, lifted verbatim from the old `nodes.map`
  // callback (so it returns the SAME JSX per kind) into a function Virtuoso calls
  // per visible item. `arrayIndex` is the position in `nodes` (the ARRAY index,
  // = virtualIndex − firstItemIndex — Codex P0-2): it feeds `riseFor`'s stagger
  // only. The resolved `node` comes straight from Virtuoso's `data` arg, so there
  // is no `nodes[...]` lookup. Closes over the same refs/handlers the old map did.
  const renderNode = useCallback((g: TimedNode, arrayIndex: number): React.ReactNode => {
    if (!detail) return null;
    // #177 S5 §6 — an inter-turn gap/day marker. Real DOM text (screen-reader
    // visible), role="separator", data-conv-marker so j/k and the focus-class
    // effect skip it (never a keyboard stop).
    if (g.kind === 'time_marker') {
      const gapTxt = g.gapSeconds != null ? `⏸ ${fmt.gapDuration(g.gapSeconds)} later` : null;
      const text =
        gapTxt && g.dayLabel ? `${gapTxt} · ${g.dayLabel}`
        : gapTxt ? gapTxt
        : `— ${g.dayLabel} —`;
      return (
        <div className="conv-time-marker" data-conv-marker="" role="separator">
          {text}
        </div>
      );
    }
    // #177 S5 §5 — a coalesced run of focus-hidden nodes. Renders as a marker
    // button (data-conv-marker: never keyboard-focusable, never gets
    // conv-item--focused). Clicking it drops back to `all` and jumps to the first
    // hidden node so the user can resume reading there.
    if (g.kind === 'hidden_run') {
      return (
        <button
          type="button"
          className="conv-hidden-run"
          data-conv-marker=""
          // #217 S3 E10#1 — the `· N hidden ·` pill is icon-like prose; name the
          // action for screen readers (the glyph run alone is opaque). Click drops
          // back to `all` and jumps to the first hidden turn.
          aria-label={`Show ${g.count} hidden ${g.count === 1 ? 'turn' : 'turns'}`}
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
    // #232 — the keyboard cursor ring is RENDER-DRIVEN (Codex P1-1) and keyed on
    // the cursor's TURN UUID, NOT the array index (#231 memo invariant): a prepend
    // shifts indices but not uuids, so the `conv-item--focused` class only changes
    // on a real cursor move — never on a reverse-page commit. Applied here so the
    // ring lands even on a row that mounts after scrollToIndex.
    const cursored = cursorUuid != null && nodeTurnUuid(g) === cursorUuid;
    if (g.kind === 'subagent') {
      // The thread's member_uuids (every fragment) decide jump-target suppression
      // so a folded/sidechain jump target is covered.
      const members = g.items.flatMap((it) => it.member_uuids);
      const [riseClass, riseStyle] = riseFor(g.items[0].anchor.uuid, members, arrayIndex);
      return (
        <SidechainGroup
          subagentKey={g.subagentKey}
          items={g.items}
          meta={detail.subagent_meta?.[g.subagentKey]}
          cursored={cursored}
          getItemRef={getItemRef}
          // #188 S3/B6 — the bucket-root uuid (the same value the outline subagent
          // entry jumps to). It tags the card's <details> via data-uuid and keys
          // it in cardRefs.
          rootUuid={g.items[0].anchor.uuid}
          getCardRef={getCardRef}
          // #188 S4/C2 — lift the thread's open-state so the "↓ N new" pill counts
          // only VISIBLE appends (Bug 5).
          onOpenChange={handleSubagentOpenChange}
          // §5 (Codex P1-D) — force-open iff this key is in the ancestor chain set
          // (a jump into a nested target opens this parent too).
          forceOpen={detail.session_id === sessionId && forcedOpenKeys.has(g.subagentKey)}
          isMobile={isMobile}
          riseClassName={riseClass}
          riseStyle={riseStyle}
          // #232 — the render-driven jump flash (Codex P0-1): the card root flashes
          // when it owns the jump, and a nested member flashes via its own
          // MessageItem (threaded down through childCtx). Replaces the old
          // imperative classList.add, which can't reach an unmounted off-screen row.
          flashedUuid={jumpedUuid}
          // #232 — the bulk [/] sweep state (data-model expand/collapse-all).
          bulkSweep={bulkSweep}
          // #232 (Codex P1-4) — re-pin this depth-0 card through Virtuoso on a
          // user click-collapse: scroll to THIS node aligned to the scroller top,
          // instead of the old raw `scrollTop +=` write that fought Virtuoso.
          // #232 fix — scrollToIndex takes the 0-based DATA (array) index, NOT the
          // firstItemIndex-offset virtual index (which the library clamps + ignores
          // — see the jump-landing fix note). `arrayIndex` is already this node's
          // position in `nodes`.
          pinToSelf={() => {
            virtuosoRef.current?.scrollToIndex({
              index: arrayIndex,
              align: 'start',
              behavior: reducedRef.current ? 'auto' : 'smooth',
            });
          }}
          // §5 — recursive nesting: the child subagent threads + this node's depth
          // + the per-key machinery for every nested level.
          children={g.children}
          depth={g.depth}
          childCtx={childCtx}
        />
      );
    }
    if (g.kind === 'tool_result_run') {
      // Collapsed orphan-result run (#164). Members render their own MessageItem
      // so each keeps its data-uuid + per-member ref for the #160 jump; the
      // disclosure is open by default so a jump target inside it is reachable
      // without a force-open dance.
      const members = g.items.flatMap((it) => it.member_uuids);
      const [riseClass, riseStyle] = riseFor(g.items[0].anchor.uuid, members, arrayIndex);
      return (
        <details
          className={['conv-toolresult-run', riseClass, cursored ? 'conv-item--focused' : ''].filter(Boolean).join(' ')}
          style={riseStyle}
          open
        >
          <summary>
            <span className="conv-chev" aria-hidden="true" />
            <ResultIcon /> {g.items.length} tool results
          </summary>
          <div className="conv-toolresult-run-body">
            {g.items.map((item) => (
              <MessageItem
                key={item.anchor.uuid}
                item={item}
                ref={getItemRef(item)}
                suppressToolUseIds={suppressToolUseIds}
                spawnKindByToolUseId={spawnKindByToolUseId}
                // #232 — render-driven flash on a folded-member jump hit.
                flashed={jumpedUuid != null && item.member_uuids.includes(jumpedUuid)}
              />
            ))}
          </div>
        </details>
      );
    }
    const [riseClass, riseStyle] = riseFor(g.item.anchor.uuid, g.item.member_uuids, arrayIndex);
    return (
      <MessageItem
        item={g.item}
        ref={getItemRef(g.item)}
        className={[riseClass, cursored ? 'conv-item--focused' : ''].filter(Boolean).join(' ')}
        style={riseStyle}
        // §5 — suppress a spawn chip on a main-thread item (its nested subagent
        // card is canonical).
        suppressToolUseIds={suppressToolUseIds}
        // #228 S2 (A3) — the loaded-spawn kind map for the connector that replaces
        // a suppressed spawn chip (main-thread spawns).
        spawnKindByToolUseId={spawnKindByToolUseId}
        // #232 — render-driven flash (Codex P0-1): the turn flashes when the jump
        // hit any of its member fragments. Survives an unmount/remount on scroll,
        // unlike the old imperative classList.add against a (possibly absent) ref.
        flashed={jumpedUuid != null && g.item.member_uuids.includes(jumpedUuid)}
      />
    );
  }, [detail, sessionId, riseFor, getItemRef, getCardRef, handleSubagentOpenChange, forcedOpenKeys, isMobile, childCtx, suppressToolUseIds, spawnKindByToolUseId, jumpedUuid, cursorUuid, bulkSweep]);

  // #232 — Virtuoso's itemsRendered callback. On a genuine rendered-range MOVE
  // (the first/last mounted index changed), bump `renderedRangeRev` so the
  // scroll-sync IntersectionObserver effect re-registers over the freshly mounted
  // itemRefs/cardRefs. Deduped so a same-range measure tick doesn't churn state.
  const onItemsRendered = useCallback((items: ListItem<TimedNode>[]) => {
    if (items.length === 0) return;
    const first = items[0].index;
    const last = items[items.length - 1].index;
    const prev = renderedRangeRef.current;
    if (prev.first !== first || prev.last !== last) {
      renderedRangeRef.current = { first, last };
      setRenderedRangeRev((r) => r + 1);
    }
  }, []);

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
    <div className="conv-reader" tabIndex={-1}>
      {isMobile ? (
        // #228 S3 C2 — the two-row mobile header (≤640px only). Row 1: ← Back ·
        // title · ⋯ overflow. Row 2 (slim): the compact Focus dropdown · 🔍 Find
        // · ☰ Outline. The secondary actions (Export, Compare, Latest, bulk
        // expand/collapse) + the completion/cost summaries fold into the ⋯ menu so
        // reading starts in the top ~40% of the screen. Desktop/tablet keep the
        // full inline header below — this branch is ≤640px ONLY.
        <div className="conv-reader-head conv-reader-head--mobile">
          <div className="conv-reader-row1">
            {mobileBack && (
              <button type="button" className="conv-back" onClick={() => dispatch({ type: 'SELECT_CONVERSATION', sessionId: null })}>← Back</button>
            )}
            <div className="conv-reader-headmain">
              <div className="conv-reader-title">{title || detail.session_id}</div>
              <div className="conv-reader-meta">
                {detail.project_label || '—'} · {detail.git_branch ?? '—'} · {fmt.usd2(detail.cost_usd)} · {Array.from(new Set(detail.models.map(abbreviateModel))).join(', ')}
              </div>
            </div>
            {/* #228 S3 C2 — the ⋯ overflow menu: Export, Compare with…, Latest ↓,
                Expand-all, Collapse-all, plus the read-only completion + cost rows.
                Built on the shared menu primitive (Escape-to-close, focus-return). */}
            <ReaderOverflowMenu
              sessionId={sessionId}
              exportTitle={detail.title}
              onCompare={() => dispatch({ type: 'START_COMPARE_PICK', anchor: sessionId })}
              onLatest={detail.last_anchor ? () => { void jumpToLatest(); } : null}
              latestBusy={jumpingLatest}
              onExpandAll={() => sweepDetails(true)}
              onCollapseAll={() => sweepDetails(false)}
              completionTotal={outline?.task_completion?.all_done ? outline.task_completion.total : null}
              costCumulative={cumCost.cost}
              costTotal={detail.cost_usd}
              costApprox={cumCost.approx}
            />
          </div>
          <div className="conv-reader-row2">
            {/* The 4-button focus segment collapses to one compact dropdown that
                also absorbs the FocusMoreMenu sub-options (Edits/Bash/Subagents). */}
            <FocusCompactMenu
              focusMode={focusMode}
              subagents={subagentOptions}
              onSelect={(mode) => dispatch({ type: 'SET_CONV_FOCUS_MODE', mode })}
              errorCount={targetLists.error.length}
            />
            <button
              type="button"
              className="conv-find-toggle"
              aria-pressed={convFindOpen}
              aria-label="Find in conversation"
              title="Find in conversation (/ or ⌘F / Ctrl+F)"
              onClick={toggleFind}
            ><SearchIcon /> Find</button>
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
      ) : (
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
            {detail.project_label || '—'} · {detail.git_branch ?? '—'} · {fmt.usd2(detail.cost_usd)} · {detail.models.join(', ')}
          </div>
        </div>
        <div className="conv-reader-controls">
          {/* #228 S2 — surface the keyboard-only bulk collapse/expand
              (sweepDetails, bound to ] / [) as a discoverable control. Sweeps
              all MOUNTED disclosures, in lockstep with the keys. */}
          <div className="conv-bulk-toggle" role="group" aria-label="Expand or collapse all threads">
            <button
              type="button"
              className="conv-bulk-btn"
              aria-label="Expand all threads"
              title="Expand all (])"
              onClick={() => sweepDetails(true)}
            >⤢</button>
            <button
              type="button"
              className="conv-bulk-btn"
              aria-label="Collapse all threads"
              title="Collapse all ([)"
              onClick={() => sweepDetails(false)}
            >⤡</button>
          </div>
          {/* #217 S6 F3 — cumulative-cost chip: $through-current-turn / $session
              total + a progress bar. The reader computes the prefix-sum keyed off
              the scroll-sync current turn; `approx` flags a lower bound when
              earlier pages aren't loaded. Hidden for a costless session
              (total === 0), and while no current turn is established yet
              (`pending` — #226) to suppress the transient $0.00 flash. First
              control in the row. */}
          <CumulativeCostChip cumulative={cumCost.cost} total={detail.cost_usd} approx={cumCost.approx} pending={currentTurnUuid == null} />
          {/* #217 S5 F7 — task-completion chip. Always visible (regardless of
              scroll position) when the main thread's final task snapshot is fully
              done; clicking jumps to the snapshot turn. Hidden otherwise.
              Reduced-motion handled in CSS; ≥44px touch target (#205 mobile). */}
          {outline?.task_completion?.all_done && (
            <button
              type="button"
              className="conv-complete-chip"
              aria-label={`Session complete: ${outline.task_completion.total} tasks done — jump to the final checklist`}
              title="Jump to the final task checklist"
              onClick={() => jumpToCompletion(outline.task_completion!.anchor_uuid)}
            >
              ✓ Complete · {outline.task_completion.total}
            </button>
          )}
          {/* #177 S5 §5 — focus-mode segmented control. A labeled radiogroup;
              each button's aria-checked reflects the active mode (the valid
              selected-state attribute for role="radio" — #184 dropped the
              invalid aria-pressed, which belongs to toggle buttons, not radios).
              Errors carries a count badge from the outline stats when > 0. */}
          <div className="conv-focus-seg" role="radiogroup" aria-label="Focus mode">
            {(['all', 'chat', 'prompts', 'errors'] as const).map((m) => {
              // #217 S5 E4 — only the four PRIMARY modes live in the segmented
              // control (edits/bash/subagent ride the FocusMoreMenu), so the
              // label map is keyed to that narrowed union, not the full FocusMode.
              const labels: Record<'all' | 'chat' | 'prompts' | 'errors', string> = { all: 'All', chat: 'Chat', prompts: 'Prompts', errors: 'Errors' };
              // #217 S3 E10#2 — the badge is the error-TURN count (== the jump
              // cluster chip == what clicking the Errors filter navigates to),
              // NOT stats.error_count (the server's total error-EVENT count, which
              // double-counts a turn with multiple error tools). The Stats card
              // keeps the reconciliation phrasing "N errors in M turns".
              const errCount = targetLists.error.length;
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
          {/* #217 S5 E4 — the focus "▾ More" menu: Edits / Bash / per-Subagent.
              Single-select on the same axis (a More-mode shows the four primary
              segmented buttons unselected + the ▾ trigger labelled active). */}
          <FocusMoreMenu
            focusMode={focusMode}
            subagents={subagentOptions}
            onSelect={(mode) => dispatch({ type: 'SET_CONV_FOCUS_MODE', mode })}
          />
          {/* #217 S5 §4 — whole-session export menu (F1/F5). Local state with
              its own Esc/outside-click close; fetches the new /export route. */}
          <ExportMenu sessionId={sessionId} title={detail.title} />
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
          {/* #217 S7 F10 — "Compare with…" — enters rail pick-mode with this
              session as the anchor (START_COMPARE_PICK). The rail then shows a
              banner and rows pick the second session. ≥44px touch target (#205);
              CSS in index.css. */}
          <button
            type="button"
            id="conv-compare-with"
            className="conv-compare-with"
            aria-label="Compare this session with another"
            title="Compare with another session"
            onClick={() => dispatch({ type: 'START_COMPARE_PICK', anchor: sessionId })}
          >⟷ Compare with…</button>
          {/* #205 S2 (F3) — Find toggle. Mirrors the outline toggle's
              aria-pressed semantics + chrome; gives the `/` shortcut a visible,
              tappable counterpart (the only find affordance on touch). */}
          <button
            type="button"
            className="conv-find-toggle"
            aria-pressed={convFindOpen}
            aria-label="Find in conversation"
            title="Find in conversation (/ or ⌘F / Ctrl+F)"
            onClick={toggleFind}
          ><SearchIcon /> Find</button>
          {/* outline toggle. Visible on desktop + tablet; aria-pressed reflects
              the EFFECTIVE open flag (sheet flag ≤1100px, persisted pref ≥1101px).
              In the tablet band it opens the slide-over sheet (#228 S3 F1). */}
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
      )}
      {/* #177 S6 — the floating in-conversation find bar. Absolutely
          positioned top-right inside the reader column (zero layout shift). The
          stepRef wires its cursor to the reader's n/N bindings. */}
      {convFindOpen && (
        <FindBar
          sessionId={sessionId}
          onClose={onFindClose}
          onTermsChange={onFindTermsChange}
          stepRef={findStepRef}
          tailRevision={tailRevision}
        />
      )}
      {/* #232 — the reader list is virtualized: <Virtuoso> IS the
          `.conv-reader-body` scroll surface (not a nested scroller — its own
          scroller carries the className and `scrollerRef` keeps `bodyRef`
          pointing at it). Only viewport-near cards mount, so cold-mount is
          O(viewport), not O(window). `firstItemIndex` (owned in useConversation,
          T2) pins the viewport across reverse-page prepends; `startReached` /
          `endReached` replace the deleted sentinel observers; `followOutput` /
          `atBottomStateChange` (with the 80px slack) drive stick + the "↓ N new"
          pill; `role="feed"` keeps the off-screen turns navigable for a screen
          reader (T5). The Highlight/Transcript providers wrap it so every mounted
          card reads the find terms + transcript context. */}
      <HighlightContext.Provider value={findTerms}>
      <TranscriptContext.Provider value={transcriptCtx}>
      <Virtuoso
        ref={virtuosoRef}
        className="conv-reader-body"
        role="feed"
        scrollerRef={(el) => { bodyRef.current = el as HTMLDivElement | null; }}
        data={nodes}
        context={virtuosoContext}
        firstItemIndex={virtualFirstItemIndex}
        computeItemKey={(_index, node) => nodeKey(node)}
        itemContent={(index, node) => renderNode(node, index - firstItemIndexRef.current)}
        components={virtuosoComponents}
        startReached={() => {
          // #232 — ARMING GATE (defense-in-depth on top of loadToTarget's
          // re-entrancy guard). On a cold open Virtuoso fires startReached/endReached
          // as it settles the initial position (the deep-link target's scrollToIndex,
          // or the tail), BEFORE any user scroll. Paging on those transient edge hits
          // re-enters the very drain that's positioning the window. Gate both edges:
          // they no-op until the open has SETTLED (first atBottomStateChange OR the
          // jump landed OR a 750ms fallback — see the arming effect) AND while a
          // programmatic jump drain is in flight. A genuine user scroll-to-edge
          // happens only after settle, so real reverse/forward paging is preserved.
          if (!reversePagingArmedRef.current || jumpDrainingRef.current) return;
          doLoadPrevRef.current();
        }}
        endReached={() => {
          if (!forwardPagingArmedRef.current || jumpDrainingRef.current) return;
          void loadMore();
        }}
        followOutput={(atBottom) => (atBottom ? (reduced ? 'auto' : 'smooth') : false)}
        atBottomThreshold={80}
        atBottomStateChange={(atBottom) => { atBottomRef.current = atBottom; armPaging(); if (atBottom) setNewCount((n) => (n ? 0 : n)); }}
        itemsRendered={onItemsRendered}
        increaseViewportBy={600}
        onScroll={onBodyScroll}
      />
      </TranscriptContext.Provider>
      </HighlightContext.Provider>
      {/* #175 F4 — "↓ N new" pill. A child of .conv-reader (NOT the scrolling
          .conv-reader-body), absolutely positioned so it floats over the body
          without scrolling with it. Shown only while scrolled up with unseen
          live-appended turns; clicking it scrolls to the newest turn. */}
      {newCount > 0 && !atBottomRef.current && (
        <button type="button" className="conv-new-pill" onClick={jumpToNew}>↓ {newCount} new</button>
      )}
      {/* #228 S1 (§6c) — the pill above is conditionally mounted, so aria-live on
          it can't announce. This persistent .sr-only polite region is ALWAYS
          rendered and mirrors newCount, so a screen reader hears live-tail
          arrivals. */}
      <div className="sr-only" aria-live="polite" data-testid="conv-newcount-live">
        {newCount > 0 ? `${newCount} new message${newCount === 1 ? '' : 's'} below` : ''}
      </div>
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
