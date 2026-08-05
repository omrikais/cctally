import React, { forwardRef, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Virtuoso, type VirtuosoHandle, type ListItem, type Components } from 'react-virtuoso';
import { dispatch, getState, selectMarkersEnabled, subscribeStore } from '../store/store';
import { useConversation } from '../hooks/useConversation';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';
import { useIsWide } from '../hooks/useIsWide';
import { useReaderControlsDensity } from '../hooks/useReaderControlsDensity';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { groupSidechains, flattenSubagents, walkSubagents, type RenderNode } from './groupSidechains';
import { isSystemMarker } from './systemMarkers';
import { FindBar } from './FindBar';
import { ExportMenu } from './ExportMenu';
import { FocusMoreMenu, type FocusSubagentOption } from './FocusMoreMenu';
import { FocusCompactMenu } from './FocusCompactMenu';
import { ReaderOverflowMenu } from './ReaderOverflowMenu';
import {
  ExactFindContext,
  HighlightContext,
  type ExactFindState,
  type HighlightTerms,
} from './HighlightContext';
import { MessageItem } from './MessageItem';
import { SidechainGroup } from './SidechainGroup';
import { useStableSet, useStableMap, useMonotonicMax, useStableSessionIndex } from './useStableIdentity';
import { CumulativeCostChip } from './CumulativeCostChip';
import { cumulativeCostThrough } from './cumulativeCost';
import { ResultIcon, SpinnerIcon, WarningIcon, ChatIcon, SearchIcon } from './ConvIcons';
import { TranscriptContext } from './TranscriptContext';
import { loadAnonMode, saveAnonMode } from '../store/anonPrefs';
import { applyFocusMode, nodeUuid, nodeVisible, type FocusMode, type FilteredNode } from './applyFocusMode';
import { insertTimeMarkers, type TimedNode } from './insertTimeMarkers';
import { suppressedHeadingKeys } from './suppressRepeatedHeadings';
import { headingIsVisible } from './headingVisibility';
import { nodeIndexForUuid } from './nodeIndexForUuid';
import { scrollNodeIntoView, alignScrollTop } from './scrollNodeIntoView';
import { resolveJumpAnchor } from './resolveJumpAnchor';
import {
  applyCurrentMark,
  applyCurrentOccurrence,
  firstLandableMark,
  firstLandableOccurrenceFragment,
} from './findMark';
import { walkToTarget } from './walkToTarget';
import { reassertCenter } from './reassertCenter';
import { resolveJumpOwner } from './resolveJumpOwner';
import { isLayoutStable, type LayoutSnapshot } from './layoutStable';
import {
  buildOutlineTargets, cursorIndex, distinctOwnerTurns, nextTargetEntry, type JumpKind,
} from './outlineNavigation';
import {
  classifyWindowGrowth,
  applyOpenChange,
  resetPillTrackers,
  INITIAL_PILL_TRACKERS,
  runJumpPipeline,
  type PillTrackers,
  type JumpRunnerDeps,
} from './readerScrollMachine';
import { useReaderMachine } from './useReaderMachine';
import { fmt } from '../lib/fmt';
import { abbreviateModel } from '../lib/modelName';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { loadReadingPos } from '../store/readingPosition';
import {
  buildConversationJump,
  conversationJumpRef,
  conversationRefKey,
  sameConversationRef,
  type ConversationDetail,
  type ConversationItem,
  type ConversationOutline,
  type ConversationRef,
  type OpenIntent,
  type SubagentMeta,
} from '../types/conversation';

// #463 S4 remediation C-2 — `innerAnchorKey` is the finer address INSIDE the
// item the jump loads, and this builder had no parameter for it, so a keyboard
// jump to a landmark carried a strictly smaller payload than the rail row and
// the chip carried for the same landmark: the segment without the failing call.
// One Codex segment can be 4,098px tall, so the browser gate measured the key
// press landing up to 6,574px above what it named while a click on the same
// landmark landed on it. Omitted for a site whose target is a bare turn or
// segment uuid, which keeps that payload byte-identical to before.
//
// Round 3 — the body moved to `buildConversationJump` in types/conversation.ts,
// which the rail row and the cluster chip now call as well, so the "identical
// payload for the same target" rule is structural rather than test-enforced.
// The local name stays because nine call sites read `readerJump(...)`.
//
// Round 7 — the optional inputs are the builder's options object, so the three
// sites here that carry an inner address name it (`{ innerAnchorKey: … }`) and
// the six that do not simply pass nothing.
const readerJump = buildConversationJump;

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

// #237/#479 — convergent landing bounds. Find hits center; ordinary deep links
// and card roots align start. Every path survives deferred Virtuoso re-measure.
const REASSERT_STABLE_FRAMES = 4;  // consecutive same-target within-tol frames = settled
const REASSERT_BUDGET_MS = 800;    // wall-clock fallback ceiling (refresh-rate-independent)

// #463 S2 §2.7 — bounds on the reasoning-heading spine's two unattended loops.
// `EDGE_WALK_MAX` caps how many heading-less pages one `h`/`H` press will load
// while walking past segments that carry no reasoning; `MOUNT_ATTEMPTS` caps how
// many post-jump commits a subagent request waits through for its inner row to
// render. A press restarts either, so both only stop an unattended walk.
const HEADING_EDGE_WALK_MAX = 8;
const HEADING_MOUNT_ATTEMPTS = 6;

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
type ConversationReaderProps = {
  conversationRef?: ConversationRef;
  // Legacy Claude test/boundary input. Production callers pass conversationRef.
  sessionId?: string;
  mobileBack?: boolean;
  outline?: ConversationOutline | null;
  growthNonce?: number;
  live?: boolean;
};

// Exported for #463 S5's account-retention test: the whole reader is a heavy
// mount with its own fetch and virtualization, and the property under test is
// the ref this strip CONSTRUCTS, not how the reader renders it.
export function ProviderThreadNav({ detail, conversationRef }: { detail: ConversationDetail; conversationRef: ConversationRef }) {
  const meta = detail.provider_meta;
  if (!meta) return null;
  // #463 S5 (F24d, spec §4.6) — carry the CURRENT conversation's account through
  // parent-and-child thread navigation. Conversation identity includes
  // `account_key`, so rebuilding the ref without it opened an accountless
  // identity while the global chip still named an account, and no rail row
  // compared as current — the same identity-loss class as the deep-link defect.
  // Only carried when the two refs share a source; an account key is scoped to
  // one provider and means nothing on the other.
  const open = (key: string) => dispatch({
    type: 'SELECT_CONVERSATION',
    conversationRef: conversationRef.account_key && conversationRef.source === meta.source
      ? { source: meta.source, key, account_key: conversationRef.account_key }
      : { source: meta.source, key },
  });
  const tokens = meta.tokens;
  return (
    <div className="conv-provider-strip" aria-label={`${meta.source === 'codex' ? 'Codex' : 'Claude'} conversation context`}>
      <span className={`conv-source-badge conv-source-badge--${meta.source}`}>{meta.source === 'codex' ? 'Codex' : 'Claude'}</span>
      {meta.parent && (
        <button type="button" className="conv-thread-link" onClick={() => open(meta.parent!.conversation_key)}>
          ← Parent · {meta.parent.title || 'conversation'}
        </button>
      )}
      {(meta.children ?? []).map((child) => (
        <button key={child.conversation_key} type="button" className="conv-thread-link" onClick={() => open(child.conversation_key)}>
          Child → {child.title || 'conversation'} · {fmt.usd2(child.cost_usd)}
        </button>
      ))}
      {tokens?.source === 'codex' && (
        <span className="conv-provider-tokens">
          in {fmt.tokens(tokens.input)} · out {fmt.tokens(tokens.output)} · cached in {fmt.tokens(tokens.cached_input ?? 0)} · reasoning out {fmt.tokens(tokens.reasoning_output ?? 0)}
        </span>
      )}
      {(meta.unattributed_cost_usd ?? 0) > 0 && (
        <span className="conv-provider-unattributed">unattributed {fmt.usd2(meta.unattributed_cost_usd ?? 0)}</span>
      )}
      {meta.source === 'codex' && <span className="conv-provider-capability">media unavailable</span>}
      <span className="sr-only">Qualified conversation {conversationRef.key}</span>
    </div>
  );
}

export function ConversationReader({ conversationRef: qualifiedRef, sessionId: legacySessionId, mobileBack, outline, growthNonce, live }: ConversationReaderProps) {
  const conversationRef = qualifiedRef ?? { source: 'claude', key: legacySessionId! };
  const qualifiedInput = qualifiedRef != null;
  const sessionId = conversationRef.key;
  const identityKey = conversationRefKey(conversationRef);
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
    if (j && sameConversationRef(conversationJumpRef(j), conversationRef)) return { kind: 'anchor', uuid: j.uuid };
    const saved = loadReadingPos(conversationRef);
    if (saved) return { kind: 'restore', uuid: saved.uuid };
    return { kind: 'tail' };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identityKey]);
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
  const jumpUuidForSession = jump && sameConversationRef(conversationJumpRef(jump), conversationRef) ? jump.uuid : null;
  // #291 — a same-session jump is in flight (find / outline / restore / Latest /
  // landmark nav / deep-link — all flow through `conversationJump`). Suspend
  // react-virtuoso's raw-truthy resize-autoscroll-to-LAST watcher for the jump's
  // whole lifetime by passing literal `followOutput={false}` (below). Derived from
  // the store (not the follow controller), so it is in effect in the SAME render
  // the jump is dispatched — before a force-open SIZE_INCREASED can arm the
  // watcher — and releases automatically when the jump clears.
  const activeJumpForSession = jumpUuidForSession != null;
  const protectedUuids = useMemo(() => {
    const s = new Set<string>();
    if (jumpUuidForSession) s.add(jumpUuidForSession);
    if (currentTurnUuidEarly) s.add(currentTurnUuidEarly);
    if (convPinnedUuidEarly) s.add(convPinnedUuidEarly);
    return s;
  }, [jumpUuidForSession, currentTurnUuidEarly, convPinnedUuidEarly]);
  const { detail, loading, error, hasMore, hasPrev, openScrollIntent, lastOp, loadMore, loadPrev, loadToTarget, jumpToLatest: hookJumpToLatest, fetching, tailRevision, virtualFirstItemIndex } = useConversation(conversationRef, { outlineTurns: outline?.turns, outlinePositions: outline?.positionByKey, openIntent, protectedUuids, growthNonce, live });
  // #232 — the imperative Virtuoso handle (scrollToIndex for jumps / keyboard
  // nav / the "↓ N new" pill) and a live mirror of the firstItemIndex so
  // `itemContent`'s array-index math (`virtualIndex − firstItemIndex`) reads the
  // current offset without re-subscribing. Virtuoso speaks the VIRTUAL index
  // space (firstItemIndex + arrayIndex); the array index feeds riseFor's stagger.
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const firstItemIndexRef = useRef(virtualFirstItemIndex);
  firstItemIndexRef.current = virtualFirstItemIndex;
  // #281 S5 — the reader's scroll-orchestration machine (A2: paging gates + the
  // programmatic-run token; A3/A4 grow it). `gates` owns the arming / suppression
  // cluster + the walk-token that used to be `reversePagingArmedRef`/
  // `forwardPagingArmedRef`/`jumpDrainingRef`/`armPagingTimerRef`/`walkTokenRef`.
  // The walk-token guard (spec §2.2-1 / Codex P0-3): a cold far jump runs a longer
  // async walk-to-mount than the old single scrollIntoView, and the effect re-fires
  // on node/window/forcedOpenKeys changes WHILE that walk is in flight; a monotonic
  // token lets each run own itself (a new jump bumps it via `beginProgrammaticRun`;
  // an older superseded run detects the mismatch via `isCurrentRun` after every
  // await and bails WITHOUT clearing a newer run's suppression).
  const { gates, lifecycle, generation, followMode, follow } = useReaderMachine(identityKey);
  // #228 S1 (F3) — after CLOSE_COMPARE, return focus to the compare trigger
  // once the single reader has rendered. The reader loads async, so a single
  // rAF fired at close time can land during the loading branch and miss the
  // trigger; instead we consume the store flag once detail is ready.
  // #304 S2 — the flag also serves CANCEL_COMPARE_PICK now (it generalized to
  // "compare-flow focus return pending"), so this same consume effect returns
  // focus on cancel too, with a fallback chain extended for compact headers.
  const compareCloseFocusPending = useSyncExternalStore(
    subscribeStore, () => getState().compareCloseFocusPending,
  );
  useEffect(() => {
    if (!compareCloseFocusPending) return;
    if (loading && !detail) return;          // wait for the detail branch
    const el =
      document.getElementById('conv-compare-with') ??
      // #304 S2 — compact headers have no #conv-compare-with; the ⋯ overflow
      // toggle is the control that launched the compare flow there.
      document.querySelector<HTMLElement>('.conv-overflow-toggle') ??
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
  // #304 S3 §1 — the reader-column element-width fold axis. Called
  // unconditionally (hooks rules); consulted only in the isWide (≥1101) branch.
  // In the ≤1100 compact band the S1 window axis already governs, so density is
  // ignored there. `readerRef` attaches to the .conv-reader detail root below.
  const { density, readerRef } = useReaderControlsDensity();
  // #304 S3 (Codex F2) — a parallel ref to the same root so the focus-continuity
  // layout effect can query the header subtree after a density flip (the hook's
  // readerRef is a callback, not a stored node).
  const readerRootRef = useRef<HTMLElement | null>(null);
  const setReaderRoot = useCallback((el: HTMLElement | null) => {
    readerRootRef.current = el;
    readerRef(el);
  }, [readerRef]);
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
  // #239 — the active window anchor for giant-subagent internal windowing: the
  // in-flight jump target (available in render, not cleared until after landing)
  // or the pinned turn. Threaded to every SidechainGroup so the windowed body
  // centers on the member the reader is trying to reach, keeping it mounted for
  // the existing force-open + walk-to-mount land. Null => head-anchored.
  const windowAnchorUuid = jumpUuidForSession ?? convPinnedUuid;
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
  const [exactFind, setExactFind] = useState<ExactFindState | null>(null);
  // #281 S4 — the "Anonymize" mode (default ON, persisted). Single source for
  // the header toggle chip, the Export menu, and every per-card CopyButton (via
  // TranscriptContext). Seeded from localStorage; a flip persists immediately.
  const [anonMode, setAnonMode] = useState<boolean>(loadAnonMode);
  const toggleAnonMode = useCallback(() => {
    setAnonMode((v) => {
      const next = !v;
      saveAnonMode(next);
      return next;
    });
  }, []);
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
  // #304 S3 (Codex F2) — focus continuity across a density flip. A flip unmounts
  // the focused header control (e.g. clicking ☰ Outline opens the outline column
  // → reader narrows → fold → the very button clicked unmounts), and React fires
  // no onBlur on unmount → focus falls to <body> and the reader keymap deadens
  // (the same failure class S2 fixed for compare-cancel). Identity is captured on
  // every header focus (cheap capture handler); a layout effect keyed on density
  // restores focus to the new mode's semantic equivalent. S2's
  // compareCloseFocusPending chain runs in its own effect and takes precedence
  // (this effect bails when it is armed). Initial mount is not a flip.
  const headIdentityRef = useRef<{ role: string | null; group: string | null }>({ role: null, group: null });
  const onHeadFocusCapture = useCallback((e: React.FocusEvent) => {
    const t = (e.target as HTMLElement).closest?.(
      '[data-hdr-role], [data-hdr-group], .conv-overflow-menu, .conv-overflow-toggle, .conv-focus-compact, .conv-find-toggle, .conv-outline-toggle',
    );
    if (!(t instanceof HTMLElement)) return;
    const role = t.getAttribute('data-hdr-role')
      ?? (t.classList.contains('conv-find-toggle') ? 'find'
        : t.classList.contains('conv-outline-toggle') ? 'outline'
        : t.classList.contains('conv-focus-compact') ? 'focus'
        : (t.classList.contains('conv-overflow-menu') || t.classList.contains('conv-overflow-toggle')) ? 'overflow'
        : null);
    headIdentityRef.current = { role, group: t.closest('[data-hdr-group]')?.getAttribute('data-hdr-group') ?? null };
  }, []);
  const prevDensityRef = useRef<'full' | 'compact' | null>(null);
  useLayoutEffect(() => {
    const prev = prevDensityRef.current;
    prevDensityRef.current = density;
    if (prev == null || prev === density || !isWideRef.current) return;     // mount / no flip / compact band
    const ae = document.activeElement;
    if (ae && ae !== document.body) return;                                 // focus survived elsewhere
    if (getState().compareCloseFocusPending) return;                        // S2 precedence
    const { role, group } = headIdentityRef.current;
    if (!role && !group) return;
    const head = readerRootRef.current?.querySelector('.conv-reader-head');
    if (!head) return;
    const sel = density === 'compact'
      ? (role === 'find' ? '.conv-find-toggle'
        : role === 'outline' ? '.conv-outline-toggle'
        : (role === 'focus' || group === 'reading') ? '.conv-focus-compact-toggle'
        : '.conv-overflow-toggle')
      : (role === 'find' ? '.conv-find-toggle'
        : role === 'outline' ? '.conv-outline-toggle'
        : (role === 'focus' || group === 'reading') ? '.conv-focus-seg-btn[aria-checked="true"]'
        : role === 'overflow' ? '[data-hdr-role="anon"]'
        : `[data-hdr-role="${role}"]`);
    (head.querySelector(sel) as HTMLElement | null)?.focus();
  }, [density]);
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
  const conversationRefRef = useRef(conversationRef);
  conversationRefRef.current = conversationRef;
  const qualifiedInputRef = useRef(qualifiedInput);
  qualifiedInputRef.current = qualifiedInput;
  // #177 S6 — live mirror so the stable n/N keymap closures read the open flag
  // without re-registering the keymap array.
  const convFindOpenRef = useRef(convFindOpen);
  convFindOpenRef.current = convFindOpen;
  // #238 R5 — the currently-highlighted find <mark> (the n/N "current" match).
  // Imperative because the marks live deep inside memoized react-markdown (matches
  // the #236 landing architecture). `markCurrent` moves the conv-mark--current
  // class to the landed mark (or clears it), updating the ref. Best-effort: a
  // Virtuoso recycle can drop the class; it re-applies on the next n/N step.
  const currentMarkRef = useRef<HTMLElement | null>(null);
  const markCurrent = useCallback((el: HTMLElement | null) => {
    currentMarkRef.current = applyCurrentMark(currentMarkRef.current, el);
  }, []);
  // cache-failure-markers spec §4 — live mirror so the stable `c`/`C` keymap
  // closures no-op when the opt-out is off, without re-registering the array.
  const markersEnabledRef = useRef(markersEnabled);
  markersEnabledRef.current = markersEnabled;

  // #175 F4 — live-tail scroll behavior. `atBottomRef` tracks whether the user
  // is parked at the bottom (updated on every scroll). `pillTrackersRef` holds the
  // immutable per-open pill state (#281 S5 A1 — `prevLen`/`prevHasMore`/known-set
  // now live in `readerScrollMachine.classifyWindowGrowth`): a growth is a LIVE
  // append (not the final pagination page) only when the conversation was ALREADY
  // fully paged before it — i.e. `prevHasMore === false`. The final pagination page
  // grows `items` AND flips hasMore false in the same update, so a naive `!hasMore`
  // check would false-positive once on the last page (Codex P0). `newCount` feeds
  // the floating "↓ N new" pill.
  const atBottomRef = useRef(true);
  const pillTrackersRef = useRef<PillTrackers>(INITIAL_PILL_TRACKERS);
  // #232 / #281 S5 A2 — the paging-arming gate (the cold-load freeze fix's
  // defense-in-depth layer) now lives in `gates` (readerScrollMachine). Both edges
  // are DISARMED on every session open (`gates.sessionOpened()`) and armed only
  // once the initial open positioning has SETTLED — so Virtuoso's transient
  // startReached/endReached during cold mount + the programmatic jump drain can't
  // trigger paging (which would re-enter the drain that's positioning the window).
  // A real user scroll-to-edge only happens after settle, so genuine reverse/forward
  // paging still works. A programmatic run additionally suppresses both edges WHILE
  // a loadToTarget drain is in flight (the `beginProgrammaticRun`/`endProgrammaticRun`
  // pair). `gates.arm()` arms both edges (idempotent) when the open settles: the
  // first atBottomStateChange (tail open lands at the bottom), the jump pipeline's
  // scrollToIndex landing (deep-link lands on the target), or the 750ms fallback
  // armed on each session open — whichever fires first.
  const [newCount, setNewCount] = useState(0);
  // #463 S1 — true once a jump has been abandoned as unreachable, so the reader
  // can say so instead of falling silent. Reset when a new jump starts and when
  // the conversation identity changes; the user can also dismiss it.
  const [jumpFailure, setJumpFailure] = useState<'unresolved' | 'landing_failed' | 'load_failed' | null>(null);
  // #463 S1 — a give-up message is about ONE finished jump, so the next page
  // request retires it. The render used to hide it while `fetching` was true
  // instead, which made it vanish and return around every later page the user's
  // own scrolling triggered, long after the jump had ended. Clearing on the
  // RISING edge retires the message without resurrecting one for a jump that is
  // no longer running. It does NOT make the two indicators mutually exclusive —
  // this effect is passive, so it clears after paint, and a request already in
  // flight when the give-up is set never produces a rising edge at all. A rising
  // edge in the same commit as the set also retires the message before it
  // renders. Both windows are narrow and deliberately left alone.
  const fetchingWasTrueRef = useRef(false);
  useEffect(() => {
    if (fetching && !fetchingWasTrueRef.current) setJumpFailure(null);
    fetchingWasTrueRef.current = fetching;
  }, [fetching]);

  // #188 S4/C2 — count only VISIBLE live appends in the "↓ N new" pill (Bug 5).
  // `openKeysRef` tracks which subagent threads are currently expanded (lifted
  // from SidechainGroup via handleSubagentOpenChange); the known-subagent set now
  // lives inside `pillTrackersRef` (#281 S5 A1). A live-appended item is visible —
  // and so counts — iff it's top-level, OR the first item of a brand-new subagent
  // group (its card appears), OR an append into an already-EXPANDED known thread.
  // An append into an existing COLLAPSED thread is below the fold → +0. Both reset
  // on session switch and seed (without counting) during non-live pagination growth.
  // The open-set is an IMMUTABLE value (#281 S5 A1 / spec F5) — a change replaces
  // the ref with a fresh set via `applyOpenChange`, never mutating in place.
  const openKeysRef = useRef<ReadonlySet<string>>(new Set());
  const handleSubagentOpenChange = useCallback((key: string, open: boolean) => {
    openKeysRef.current = applyOpenChange(openKeysRef.current, key, open);
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
  // with a raw append count). Keyed on `lastOp.rev` (+ hasMore so the trackers'
  // prevHasMore tracks each commit), NOT items.length: a prepend+far-trim (the
  // windowed DOM cap) can keep items.length flat while still mutating the window,
  // and a length key would miss it. A plain useEffect (no longer pre-paint —
  // Virtuoso owns the scroll, so there is no manual scrollTo to land before paint).
  //
  // #281 S5 A1 — the whole discriminator (reset re-seed, prepend/trim bail,
  // addedBottom tail slice, known/open visibility classification) now lives in the
  // pure `classifyWindowGrowth`; this effect just feeds it the current inputs +
  // the immutable trackers, applies the count, and stores the fresh trackers.
  // #232 — stick is `followOutput`'s job now (it sticks when already at bottom);
  // `atBottom` gates the count so an at-bottom growth (which followOutput sticks +
  // atBottomStateChange zeroes) never double-bumps the pill.
  useEffect(() => {
    const result = classifyWindowGrowth(
      {
        op: lastOp,
        items: detail?.items ?? [],
        hasMore,
        atBottom: atBottomRef.current,
        openKeys: openKeysRef.current,
      },
      pillTrackersRef.current,
    );
    if (result.countsTowardPill) setNewCount((n) => n + result.visibleAdded);
    pillTrackersRef.current = result.nextTrackers;
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
        conversationRef,
        jump: readerJump(conversationRef, la.uuid, qualifiedInput),
      });
    } finally {
      setJumpingLatest(false);
    }
  }, [detail?.last_anchor, hookJumpToLatest, identityKey]);
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
  const sessionMaxTurnCost = useMonotonicMax(sessionMaxTurnCostRaw, identityKey);
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
  // #463 S2 — the items the reader is ACTUALLY rendering, flattened into
  // document order. §2.6's duplicate-heading suppression and §2.7's heading walk
  // are both derived from this one list, so the two cannot disagree. They were
  // derived from different sources before: suppression ran over every loaded
  // item while the walk ran over the focus-filtered tree, so under a non-`all`
  // mode a heading could be suppressed because a HIDDEN sibling had rendered it
  // first — and a visible segment whose headings all repeat a hidden one then
  // showed no reasoning at all.
  const visibleItems = useMemo(() => {
    const out: ConversationItem[] = [];
    const walk = (list: readonly (FilteredNode | RenderNode)[]) => {
      for (const node of list) {
        if (node.kind === 'hidden_run') continue;
        if (node.kind === 'item') { out.push(node.item); continue; }
        if (node.kind === 'tool_result_run') { out.push(...node.items); continue; }
        // A subagent card renders its own items and then its nested children.
        out.push(...node.items);
        walk(node.children);
      }
    };
    walk(visible);
    return out;
  }, [visible]);
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
  // #463 S2 §2.6 — the reasoning heading keys an earlier block of the same TURN
  // already rendered. Computed here because the decision needs the whole turn,
  // which one block cannot see, and stabilized to content so the identity (and
  // therefore the provider identity, and therefore every memoized MessageItem)
  // only changes when the set actually changes.
  // Derived from `visibleItems`, not `detail.items`: a heading may only be
  // suppressed on the strength of a block the reader can actually see.
  const suppressedHeadingKeysRaw = useMemo(
    () => suppressedHeadingKeys(visibleItems),
    [visibleItems],
  );
  const suppressedHeadings = useStableSet(suppressedHeadingKeysRaw);
  // #463 S3 §3.2 — whole-conversation session index. The server rebuilds it on
  // EVERY fetch and useConversation prefers any truthy value, so the raw field
  // is a fresh object on every live-tail tick. Stabilized to content, like the
  // other two members of this memo, or the provider identity would flip on each
  // tick and re-render every memoized MessageItem in the visible window.
  const sessionIndex = useStableSessionIndex(detail?.session_index);
  const transcriptCtx = useMemo(
    () => ({
      sessionId, conversationRef, focusMode, fmtCtx, markersEnabled,
      maxTurnCost: sessionMaxTurnCost, anonMode,
      suppressedHeadingKeys: suppressedHeadings,
      sessionIndex,
    }),
    [identityKey, focusMode, fmtCtx, markersEnabled, sessionMaxTurnCost, anonMode,
     suppressedHeadings, sessionIndex],
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
  // P0 requirement — fire EXACTLY ONCE per open. The effect is keyed on
  // items.length so it lands the moment the first non-empty page renders, but
  // `openScrollIntent` is set ONCE (the hook resets it only on session change), so
  // without a one-shot every reverse-page prepend / live append would re-run it and
  // yank the reader back to the bottom (re-clamping scrollTop + re-arming
  // atBottomRef), defeating reverse pagination, the scroll-anchor, and the "stick
  // only when at bottom" contract.
  // #232 — land through Virtuoso's `scrollToIndex` (not a raw `scrollTop` write,
  // which fights the library's scroll management). A 'bottom' open jumps to the
  // last node aligned to the bottom edge; a 'top' open jumps to the first.
  // #281 S5 A3 — the one-shot latch now lives in the machine's `firstWindowReady`,
  // keyed on the OPEN GENERATION (so the NEXT open re-applies its own intent); the
  // effect just consumes the landing command. `firstWindowReady` returns null
  // until the intent resolves AND items AND rendered nodes are non-empty, then
  // exactly once per generation.
  useEffect(() => {
    const nodeCount = nodes.length;
    const cmd = lifecycle.firstWindowReady(
      generation, openScrollIntent, detail?.items.length ?? 0, nodeCount,
    );
    if (!cmd) return;
    // #232 fix — scrollToIndex takes the 0-based DATA (array) index, NOT the
    // firstItemIndex-offset virtual index (which the library clamps + ignores —
    // see the jump-landing fix note). A 'bottom' open lands on the last node
    // (array index nodeCount − 1); a 'top' open lands on the first (array index 0).
    // #281 S5 B1 (#285 FIX) — a 'top' landing suspends follow (the reader passes
    // literal `followOutput={false}`) so react-virtuoso's raw-truthy
    // resize-autoscroll-to-LAST watcher is DISABLED and the scrollToIndex(0)
    // actually sticks (a single-page conversation opens at the TOP again); a
    // 'bottom' landing keeps follow live so a multi-page tail open stays stuck to
    // the bottom with live-tail engaged. The suspension holds until settle (first
    // atBottomStateChange after the landing / a jump landing / the fallback).
    if (cmd.target === 'bottom') {
      virtuosoRef.current?.scrollToIndex({ index: nodeCount - 1, align: 'end' });
    } else {
      virtuosoRef.current?.scrollToIndex({ index: 0, align: 'start' });
    }
    atBottomRef.current = cmd.setAtBottom;
    follow.landed(cmd.target);
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
  // one-shot latch keyed on `sessionId` VALUE breaks: open A, switch to B as a
  // non-restore (tail) open, then return to A — a value-keyed latch wrongly skips
  // the restore. #281 S5 A3 — the latch is keyed on the OPEN GENERATION instead
  // (the machine's `restoreReady`): a genuinely new open (incl. the A→B→A return)
  // is a fresh generation, so it re-fires its own restore even though B never did.
  // `restoreReady` also folds in the cross-session-transient guard (detail's
  // session_id must match) so a stale prior-session detail never restores.
  useEffect(() => {
    const cmd = lifecycle.restoreReady(
      generation,
      openIntent?.kind ?? null,
      openIntent?.kind === 'restore' ? openIntent.uuid : null,
      detail?.session_id ?? null,
      sessionId,
    );
    if (!cmd) return;
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef,
      jump: readerJump(conversationRef, cmd.uuid, qualifiedInput),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openIntent, identityKey, detail?.session_id]);

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

  // #234 / #486 — resolve once the layout tuple (mounted array-range,
  // scrollHeight, scrollTop, target-anchor rect) is stable across consecutive
  // frames, or after a bounded frame count. Both the jump pipeline and heading
  // navigation use this: a one-row giant tail can briefly mount the requested
  // neighbour, then rebound after Virtuoso corrects its size model. Waiting for
  // the structural tuple — rather than merely one itemsRendered callback — lets
  // the caller observe that rebound and issue the next measurement step itself.
  const waitForLayoutQuiesce = useCallback((
    anchorUuid: string,
    isAborted: () => boolean,
    stableFrames = 1,
    timerDriven = false,
  ): Promise<void> => new Promise((resolve) => {
    const body = bodyRef.current;
    const snap = (): LayoutSnapshot => {
      const el = body?.querySelector(`[data-uuid="${CSS.escape(anchorUuid)}"]`) as HTMLElement | null;
      const sr = body?.getBoundingClientRect();
      return {
        first: renderedRangeRef.current.first - firstItemIndexRef.current,
        last: renderedRangeRef.current.last - firstItemIndexRef.current,
        scrollHeight: body?.scrollHeight ?? 0, scrollTop: body?.scrollTop ?? 0,
        anchorTop: el && sr ? el.getBoundingClientRect().top - sr.top : null,
      };
    };
    const structuralStable = (a: LayoutSnapshot, b: LayoutSnapshot): boolean =>
      isLayoutStable({ ...a, anchorTop: 0 }, { ...b, anchorTop: 0 }, 1);
    let prev = snap(); let frames = 0; let stableRun = 0;
    const schedule = (fn: () => void) => {
      if (timerDriven) window.setTimeout(fn, 16);
      else requestAnimationFrame(fn);
    };
    const tick = () => {
      if (isAborted()) { resolve(); return; }
      const cur = snap();
      const mounted = cur.anchorTop != null && prev.anchorTop != null;
      const settled = mounted ? isLayoutStable(prev, cur, 1) : structuralStable(prev, cur);
      stableRun = settled ? stableRun + 1 : 0;
      if (stableRun >= stableFrames || frames++ > 30) { resolve(); return; }
      prev = cur; schedule(tick);
    };
    schedule(tick);
  }), []);

  // Jump-to-message: page until the target is loaded, then scroll+highlight.
  // Wait for the first page (`detail`) before attempting — otherwise the effect
  // would fire while page 1 is still in flight (nextAfter unknown), page nowhere,
  // and clear the jump prematurely. It re-runs when detail?.items.length grows
  // (a paged-in target's ref attaches on the next commit) and when forcedOpenKeys
  // changes (a force-opened thread's member ref attaches in that commit).
  useEffect(() => {
    if (!jump || !sameConversationRef(conversationJumpRef(jump), conversationRef)) {
      // Jump cleared, or it now points at another session — release any force-pin
      // so a thread we expanded for it isn't left pinned (the user regains
      // collapse control). No loop: this re-fires on the forcedOpenKeys dep,
      // re-hits this guard with an empty set, and returns.
      if (forcedOpenKeys.size > 0) setForcedOpenKeys(new Set());
      return;
    }
    if (!detail || detail.session_id !== sessionId) return; // cross-session transient: keep the pin
    // #463 S1 — a fresh jump supersedes any previous give-up message. Setting the
    // state it already holds is a no-op re-render, so this is safe on a re-fire.
    setJumpFailure(null);
    let cancelled = false;
    // #234 / #281 S5 A2 — this jump owns a fresh programmatic-run token. The walk
    // and the final landing run inside the async block below; `aborted()` after
    // every await covers BOTH a session/jump-clear cancel AND a newer jump that
    // superseded this run's token (Codex P0-3). `myToken` is assigned by
    // `gates.beginProgrammaticRun()` inside the async body (where the drain
    // suppression used to be set), so `aborted()` is never consulted before then.
    let myToken = 0;
    const aborted = () => cancelled || !gates.isCurrentRun(myToken);
    const targetUuid = jump.uuid;
    const exactOccurrence = jump.find_occurrence;
    const reassertLanding = async (
      resolveTarget: () => HTMLElement | null,
      align: 'start' | 'center',
      minimumDurationMs = 0,
    ): Promise<boolean> => {
      const result = await reassertCenter({
        measure: () => {
          const body = bodyRef.current;
          const target = resolveTarget();
          return body && target && target.isConnected
            ? { desired: alignScrollTop(body, target, align), target }
            : null;
        },
        apply: (frame) => {
          const body = bodyRef.current;
          if (body) scrollNodeIntoView(body, frame.target as HTMLElement, align, 'auto');
        },
        nextFrame: () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
        now: () => performance.now(),
        isAborted: aborted,
        tol: 1,
        stableNeeded: REASSERT_STABLE_FRAMES,
        minimumDurationMs,
        budgetMs: REASSERT_BUDGET_MS,
        // Virtuoso can recycle the row briefly while applying a measured height.
        // A longer disappearance is a truthful landing failure, not success.
        transientGoneGraceMs: 200,
      });
      return result === 'settled';
    };
    // #281 S5 A4 — the jump PIPELINE runs in the pure `runJumpPipeline`; the
    // adapter supplies every side effect. `captured.*` closures read the effect-fire
    // captures (jump/detail/nodes/virtualFirstItemIndex/hasMore/focusMode); `live.*`
    // closures read the refs LIVE during the run. The snapshot-vs-live split is
    // PRESERVED VERBATIM (spec §3-A4) — this is exactly how the #285 inert top-open
    // and the #286 exhaustion-clear race stay bug-for-bug until Phase B.
    const runnerDeps: JumpRunnerDeps = {
      aborted,
      loadToTarget: () => loadToTarget(targetUuid),
      // #286 B3 — the captured committed-window epoch (lastOp.rev at effect-fire).
      // The no-hit exhaustion clear acks the drain's terminalOpRev against this, so
      // a stale pre-commit captured render list can never fire a premature clear.
      committedRev: lastOp?.rev ?? 0,
      captured: {
        // #232 — mode-hidden: a non-`all` focus mode coalesces the target's turn
        // into a hidden_run marker; reset to `all` so the turn re-renders + its
        // index resolves. findTopLevelNodeFor reads groupsRef/detail, focusMode via
        // focusModeRef; a node missing from `groups` is left to the re-fire.
        modeHidden: () => {
          const mode = focusModeRef.current;
          if (mode !== 'all') {
            const node = findTopLevelNodeFor(groupsRef.current, targetUuid, detail);
            if (node != null && !nodeVisible(node, mode)) return 'reset-needed';
          }
          return 'proceed';
        },
        // Resolve the target to its TOP-LEVEL node POSITION over the CAPTURED nodes
        // + virtualFirstItemIndex (Codex P0-2) — data-presence-driven, not DOM.
        resolveHit: () => nodeIndexForUuid(nodes, targetUuid, virtualFirstItemIndex),
        // #234 §2.3-1 — the owning subagent from the RENDER TREE (nodesRef live). A
        // find-jump into a NESTED member returns its ancestor chain (minus already-
        // forced keys) so the card re-measures; a card-root / top-level hit -> null.
        ownerChainToOpen: () => {
          const owner = resolveJumpOwner(nodesRef.current, targetUuid);
          const isCardRoot = owner?.isCardRoot ?? false;
          if (!isCardRoot && owner && owner.ownerSubagentKey != null) {
            const chain = ancestorKeys(owner.ownerSubagentKey, detail.subagent_meta);
            if (chain.some((k) => !forcedOpenKeys.has(k))) return chain;
          }
          return null;
        },
        // The no-hit fallback: a COLLAPSED subagent thread whose card didn't resolve
        // a hit — force the owning ancestor chain open so the node enters `nodes`.
        fallbackChainToOpen: () => {
          const targetItem = detail.items.find((it) => it.member_uuids.includes(targetUuid));
          if (targetItem && targetItem.subagent_key != null) {
            const chain = ancestorKeys(targetItem.subagent_key, detail.subagent_meta);
            if (chain.some((k) => !forcedOpenKeys.has(k))) return chain;
          }
          return null;
        },
      },
      live: {
        // #234 — already mounted (a warm jump, or the force-open attached its ref)?
        // Then skip the walk. The DOM presence check is robust to a lagging range.
        isTargetMounted: () => {
          if (itemRefs.current.has(targetUuid) || cardRefs.current.has(targetUuid)) return true;
          const b = bodyRef.current;
          return !!b?.querySelector(`[data-uuid="${CSS.escape(targetUuid)}"]`);
        },
        // #234 §2.2 (R1) — WALK Virtuoso toward the target in mounted-window steps so
        // the path rows MEASURE (a single estimate hop strands the target). The walk
        // re-resolves the array index each step through the live refs.
        walk: async () => {
          const r = await walkToTarget({
            getTargetArrayIndex: () => {
              const h = nodeIndexForUuid(nodesRef.current, targetUuid, firstItemIndexRef.current);
              return h ? h.arrayIndex : null;
            },
            getMountedArrayRange: () => ({
              first: renderedRangeRef.current.first - firstItemIndexRef.current,
              last: renderedRangeRef.current.last - firstItemIndexRef.current,
            }),
            scrollToIndex: (index, alignEdge) =>
              virtuosoRef.current?.scrollToIndex({ index, align: alignEdge, behavior: 'auto' }),
            quiesce: () => waitForLayoutQuiesce(targetUuid, aborted),
            isAborted: aborted,
            // #286 B3 — the step budget must span a FULL drained window in ONE
            // walk call. Removing the scenario-4 pre-page workaround means a cold
            // TAIL jump now walks the whole distance from the bottom to an early
            // turn without a prior scroll shrinking it; each mounted-window step
            // advances only a handful of items over the giant (multi-KB) rows, so
            // the pre-B3 budget of 60 exhausted mid-traversal and the flash-only
            // fallback prematurely cleared the jump. Bound it to the current window
            // size (each step advances >= 1 item, so nodeCount steps guarantee the
            // crossing) with a 60 floor; the walk still early-exits the instant the
            // target mounts, so the ceiling is only ever hit by a genuinely stuck
            // (no-progress) walk, which the halving step-shrink already terminates.
            maxSteps: Math.max(60, nodesRef.current.length),
            initialWindow: Math.max(1, renderedRangeRef.current.last - renderedRangeRef.current.first + 1),
          });
          return r === 'mounted' ? 'mounted' : 'exhausted';
        },
        quiesce: () => waitForLayoutQuiesce(targetUuid, aborted),
        // body && el resolvable — the current `result === 'mounted' && body && el`
        // guard for whether the landing block runs at all.
        hasLandableElement: () => {
          const body = bodyRef.current;
          const el = itemRefs.current.get(targetUuid)
            ?? body?.querySelector(`[data-uuid="${CSS.escape(targetUuid)}"]`);
          return !!body && !!el;
        },
        hasCardRef: () => cardRefs.current.has(targetUuid),
        findOpen: () => convFindOpenRef.current,
        // #234 §2.3-2/§2.3-3 (R2) — open the matched member's collapsed inner
        // disclosures so the <mark> renders, suppressing their open transition.
        openDisclosures: () => {
          const body = bodyRef.current;
          const el = (itemRefs.current.get(targetUuid)
            ?? body?.querySelector(`[data-uuid="${CSS.escape(targetUuid)}"]`)) as HTMLElement | null;
          const disclosures = exactOccurrence
            ? exactOccurrence.disclosure.flatMap((key) => Array.from(
                el?.querySelectorAll<HTMLDetailsElement>(
                  `details[data-disclosure-key="${CSS.escape(key)}"]:not([open])`,
                ) ?? [],
              ))
            : Array.from(el?.querySelectorAll<HTMLDetailsElement>('details:not([open])') ?? []);
          disclosures.forEach((d) => {
            d.classList.add('conv-details--jumpopen');
            d.open = true;
          });
        },
        // #204/#234/#479 — a card-root jump aligns the <details> CARD to the top
        // (NOT the sticky summary), and reasserts through deferred re-measure.
        landCard: async () => {
          const resolveCard = (): HTMLElement | null => {
            const card = cardRefs.current.get(targetUuid);
            return card && card.isConnected ? card : null;
          };
          const landed = await reassertLanding(resolveCard, 'start', 250);
          if (aborted()) return false;
          const card = resolveCard();
          // #238 R5 — mark the first landable match in the aligned card (never the
          // card root); a no-mark landing clears the highlight.
          if (card && convFindOpenRef.current && exactOccurrence) {
            applyCurrentOccurrence(card, exactOccurrence.occurrence_id);
          } else {
            markCurrent(card && convFindOpenRef.current ? firstLandableMark(card) : null);
          }
          return landed && card != null;
        },
        // #237/#291 — the convergent find landing, used for EVERY find jump (not just
        // auto-expanded disclosures): re-center the matched mark EVERY frame until the
        // center offset stops changing, so the landing survives BOTH a disclosure
        // settling shorter over ~150ms AND virtuoso's deferred ResizeObserver
        // re-measure (the top-reset). The target re-resolves each frame.
        landFindReassert: async () => {
          const body = bodyRef.current;
          if (!body) return false;
          const probeTarget = (): HTMLElement | null => {
            const turn = (itemRefs.current.get(targetUuid)
              ?? body.querySelector(`[data-uuid="${CSS.escape(targetUuid)}"]`)) as HTMLElement | null;
            if (!turn || !turn.isConnected) return null;
            if (convFindOpenRef.current && exactOccurrence) {
              applyCurrentOccurrence(turn, exactOccurrence.occurrence_id);
              return firstLandableOccurrenceFragment(turn, exactOccurrence.occurrence_id) ?? turn;
            }
            return (convFindOpenRef.current ? firstLandableMark(turn) : null) ?? turn;
          };
          const landed = await reassertLanding(probeTarget, 'center');
          if (aborted()) return false;
          const ct = (itemRefs.current.get(targetUuid)
            ?? body.querySelector(`[data-uuid="${CSS.escape(targetUuid)}"]`)) as HTMLElement | null;
          if (ct && ct.isConnected && convFindOpenRef.current && exactOccurrence) {
            applyCurrentOccurrence(ct, exactOccurrence.occurrence_id);
          } else {
            markCurrent(ct && ct.isConnected && convFindOpenRef.current ? firstLandableMark(ct) : null);
          }
          return landed && !!ct?.isConnected;
        },
        // #479 — an ordinary deep link lands at the START of the requested message,
        // not the center of a potentially multi-thousand-pixel row. Re-resolve and
        // reassert every frame so Virtuoso's measured-height correction cannot move
        // the target away after the runner has declared success.
        landStartReassert: async () => {
          const body = bodyRef.current;
          if (!body) return false;
          const resolveTarget = (): HTMLElement | null => {
            const el = (itemRefs.current.get(targetUuid)
              ?? body.querySelector(`[data-uuid="${CSS.escape(targetUuid)}"]`)) as HTMLElement | null;
            if (!el || !el.isConnected) return null;
            // #463 S4 F-A — a tier-2 jump names a block inside the item, not the
            // item. The browser gate measured the item-only landing putting the
            // failure it named up to 6,574px below a 635px viewport, because one
            // Codex segment can be 4,098px tall. A key that resolves to nothing
            // falls back to the item, which is the pre-S4 landing.
            const inner = resolveJumpAnchor(el, jump.inner_anchor_key);
            return inner && inner.isConnected ? inner : el;
          };
          // Four stable frames can still precede Virtuoso's delayed correction;
          // keep observing long enough to catch and repair the measured 68ms shift.
          const landed = await reassertLanding(resolveTarget, 'start', 250);
          if (aborted()) return false;
          const el = resolveTarget();
          markCurrent(null);
          return landed && el != null;
        },
        // §5 (Codex P1-D) — force-open the WHOLE ancestor chain (the target subagent
        // + every parent up to the root). Setting it re-fires the effect.
        requestForceOpen: (chain) => {
          setForcedOpenKeys((prev) => {
            const next = new Set(prev);
            for (const k of chain) next.add(k);
            return next;
          });
        },
        dispatchModeReset: () => dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' }),
        // #234 §2.2-7 — post-landing bookkeeping runs ONLY after the verified final
        // center, on the non-aborted path: arm paging, render-driven flash, pin,
        // cursor sync, the 2s flash-clear timer, clear the jump, reset the force set.
        landedBookkeeping: (arrayIndex) => {
          gates.arm();
          // #281 S5 B1 — a jump landing settles the open: release any follow
          // suspension (an anchor/restore open was suspended jump-driven) so
          // live-tail stick resumes once the target is centered.
          follow.settle();
          setJumpedUuid(targetUuid);
          // #463 S4 remediation C-3 — the pin carries the inner anchor the jump
          // REQUESTED, so the outline rail can mark the row the jump named
          // whichever entry point issued it. Null when the jump named none,
          // which clears a stale anchor from a previous landmark landing.
          //
          // Round 3 (F6) — "requested", not "aligned": when
          // `resolveJumpAnchor` finds nothing for the key, the landing falls
          // back to aligning the item while this still records the key. That is
          // the behaviour we want — the rail should mark the row the user asked
          // for even when the block it names is not in the DOM — but the
          // comment claimed the pin recorded what the landing actually used.
          dispatch({ type: 'SET_CONV_PINNED_TURN', uuid: targetUuid,
                     anchorKey: jump.inner_anchor_key ?? null });
          setCursor(arrayIndex);
          if (highlightTimerRef.current != null) window.clearTimeout(highlightTimerRef.current);
          highlightTimerRef.current = window.setTimeout(() => {
            setJumpedUuid(null);
            if (bodyRef.current) bodyRef.current.querySelectorAll('.conv-details--jumpopen')
              .forEach((d) => d.classList.remove('conv-details--jumpopen'));
            highlightTimerRef.current = null;
          }, 2000);
          dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
          setForcedOpenKeys(new Set()); // reset for the next jump (threads stay open via their latches)
        },
        clearJump: () => dispatch({ type: 'CLEAR_CONVERSATION_JUMP' }),
      },
      expandDetails: !!jump.expand_details,
    };
    void (async () => {
      // #232/#234/#281 S5 A2 — begin THIS programmatic run: bump the walk token +
      // suppress startReached/endReached for the WHOLE jump operation (drain + walk
      // + landing). Released in the finally via `endProgrammaticRun`, which no-ops if
      // a newer run has taken ownership.
      myToken = gates.beginProgrammaticRun();
      try {
        // #463 S1 — 'exhausted-cleared' is the pipeline's ONLY give-up outcome:
        // the target never reached the window, the drained edge is genuinely
        // exhausted, and the jump has just been cleared. Surface it.
        //
        // Deliberately NOT gated on this effect run's `cancelled` flag. Clearing
        // the jump is what makes `jump` null, which re-fires the effect and runs
        // this run's cleanup — so by the time the awaited pipeline resolves,
        // `cancelled` is already true on the very run that decided to give up,
        // and the message would never appear. The pipeline's own `aborted()`
        // check is the correct supersession guard: a superseded or torn-down run
        // returns 'aborted', never 'exhausted-cleared'.
        const outcome = await runJumpPipeline(runnerDeps);
        if (outcome === 'exhausted-cleared') setJumpFailure('unresolved');
        if (outcome === 'landing-failed') setJumpFailure('landing_failed');
        if (outcome === 'load-failed') setJumpFailure('load_failed');
      } finally {
        // Re-enable edge paging once the whole jump operation has run or bailed, but
        // ONLY if THIS run is still the current owner (a newer run that superseded
        // the token owns the gate now). `endProgrammaticRun` bakes the ownership
        // check in (spec §2.2-1 / F4).
        gates.endProgrammaticRun(myToken);
      }
    })();
    return () => { cancelled = true; };
    // hasMore stays in deps so a re-fire still fires on the edge where the final
    // page appends 0 items (items.length unchanged) but flips the cursor.
    // #286 B3 — `lastOp?.rev` is a dep too: the committed-window-epoch exhaustion
    // re-evaluation must re-fire on the drain's TERMINAL op commit (a 0-item
    // append or a trimmed prepend can leave items.length / nodes unchanged while
    // the rev — and thus `committedRev` — advances past the pending terminalOpRev).
    // forcedOpenKeys re-fires the effect once a force-opened thread has attached the
    // target's ref. focusMode re-fires it once the mode-hidden fallback resets to
    // `all` — the hidden target's node renders + its ref attaches in that commit,
    // and this re-fire scrolls via the branch above. No infinite loop:
    // loadToTarget/fetchNext serialize via loadingMoreRef, hasMore/rev transition a
    // bounded number of times, the forcedOpenKeys path either resolves (clears) or
    // settles to a stable set (every chain key present), and the focusMode reset
    // is one-way (non-`all` → `all`) so the mode-hidden branch can fire at most
    // once per jump. #232 — `nodes` + `virtualFirstItemIndex` are deps so the
    // post-load scrollToIndex re-fires once the target is paged into the render
    // list (a prepend can shift the virtual index without growing items.length).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, sessionId, detail?.items.length, hasMore, lastOp?.rev, forcedOpenKeys, focusMode, nodes, virtualFirstItemIndex]);

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
  }, [identityKey]);

  // The reused reader must not carry a force-pin across sessions (subagent_key is
  // only an agent-file hash). Reset on every session change; no-op on first mount.
  // #232 — also reset the bulk-sweep state so a prior conversation's expand/collapse-
  // all doesn't sweep the new session's sidechains on mount.
  useEffect(() => {
    setForcedOpenKeys(new Set());
    setBulkSweep({ rev: 0, open: false });
  }, [identityKey]);

  // #175 — the reused reader must not carry the live-tail pill/scroll state across
  // sessions. Clearing `newCount` drops a stale "↓ N new" pill the instant we switch
  // conversations, and resetting `atBottomRef` keeps the next session's first live
  // append on its default stick-to-bottom path (until the user scrolls it).
  // #176 — also drop a stale floating "↑ Top of turn" button + its target so the
  // next conversation starts with the button hidden.
  useEffect(() => {
    setNewCount(0);
    // #463 S1 — a give-up message belongs to the conversation that produced it.
    setJumpFailure(null);
    // #217 S3 E2 / #281 S5 A3 — the open-precedence fold: an anchor/restore open
    // lands the user on a SPECIFIC turn (not the tail), so it must NOT force
    // atBottom (else a live append would yank the viewport to the bottom). A tail
    // / legacy open keeps the prior default (true → live appends stick). The
    // machine's `sessionChanged` returns this default (idempotent per generation);
    // the `firstWindowReady` landing re-confirms it once the first page resolves.
    // Re-arming the one-shot lander for the NEW open is now automatic — the
    // generation bump makes `firstWindowReady` fire once for this open.
    const reset = lifecycle.sessionChanged(generation, openIntent?.kind ?? null);
    if (reset) atBottomRef.current = reset.atBottom;
    // #281 S5 B1 — reset the follow-suspension for the new open. anchor/restore
    // → suspend (jump-driven, until the landing settle); tail/legacy → live (a
    // subsequent single-page 'top' landing re-suspends via follow.landed).
    follow.openChanged(openIntent?.kind ?? null);
    setJumpTopVisible(false);
    jumpTopTargetRef.current = null;
    // #188 S4/C2 — the reused reader must not carry a prior conversation's
    // subagent open/known sets across sessions (subagent_key is only an
    // agent-file hash and can collide). Clearing both keeps the next session's
    // first live append counted correctly: an append into a thread that was
    // expanded in the OLD conversation but is collapsed in the new one must NOT
    // count (Bug 5 + #188 B6's per-session reset rationale). #281 S5 A1 — the
    // known-set lives in the immutable pill trackers now; `resetPillTrackers()`
    // mints a fresh empty value, and the open-set is replaced with a fresh set.
    openKeysRef.current = new Set();
    pillTrackersRef.current = resetPillTrackers();
    // #232 / #281 S5 A2 — DISARM paging for the new open and arm the fallback
    // timer via the machine. Both edges stay no-op until the open settles
    // (atBottomStateChange / jump-landing / the 750ms fallback). `sessionOpened()`
    // clears any prior fallback first (one-shot per open) + drops in-flight
    // suppression; the fallback guarantees paging is eventually usable even if
    // neither settle signal fires (e.g. a single-page conversation that never
    // reaches the bottom-state edge nor runs a jump). The unmount cleanup that
    // cancelled this timer now lives in `useReaderMachine` (`gates.dispose()`).
    gates.sessionOpened();
  }, [identityKey, openIntent, gates, lifecycle, generation, follow]);

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
  }, [identityKey]);

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
        jump != null && sameConversationRef(conversationJumpRef(jump), conversationRef) && memberUuids.includes(jump.uuid);
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
    [reduced, jump, identityKey],
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
  useEffect(() => { setFocusedIndex(0); setCursorUuid(null); }, [identityKey]);

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
  // #463 S4 remediation C-1 — the WHOLE object is retained rather than peeled
  // down to the family lists plus a bare `indexByUuid`. `cursorIndex` needs all
  // three maps, because the pin a landmark jump leaves behind is a SEGMENT key
  // and only `segmentIndex` holds those.
  const targetLists = useMemo(
    () => buildOutlineTargets(outline?.turns ?? [], convBookmarks, outline?.landmarks),
    [outline, convBookmarks],
  );
  const targetListsRef = useRef(targetLists);
  targetListsRef.current = targetLists;

  // #463 S4 remediation round 3 (F2) — the number every Errors-filter badge in
  // this header shows. It is the error-TURN count, which is what the outline
  // chip beside it already reported; the length of `targetLists.error` is one
  // entry per failing CALL under landmark awareness, and the browser gate saw
  // that length render 27 beside a chip reading 14. Computed once here rather
  // than at each render site, because the three sites are the desktop segmented
  // control and its two compact-menu twins, and the previous round fixed the
  // chip and left all three of them.
  //
  // Round 4 — this is the CHIP's number, not a row count for the Errors filter.
  // Round 3 asserted the two were the same and they are not: `applyFocusMode`
  // keeps visible NODES, and a segmented Codex turn contributes one node per
  // segment, so a turn whose failures fall in two segments shows two rows here
  // against a badge contribution of one. Far closer than the call count it
  // replaced, and exactly what the badge is specified to equal — but do not
  // restate it as the filter's row count.
  const errorTurnCount = useMemo(() => distinctOwnerTurns(targetLists.error), [targetLists]);

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
    const all = targetListsRef.current;
    const cu = convPinnedUuidRef.current ?? currentTurnUuidRef.current;
    // #232 — the keyboard cursor's turn uuid IS the source of truth (the cursored
    // node may be UNMOUNTED under virtualization, and the index ref can be stale
    // after a head mutation, so a DOM/index read is wrong). Use cursorUuidRef.
    // #463 S4 remediation C-1 — both lookups go through `cursorIndex`, which
    // resolves a segment key too. With the own-uuid map alone, the pin a
    // landmark jump had just written resolved to nothing, the cursor stayed at
    // -1, and every forward press re-found the FIRST target while every
    // backward press found none.
    const resolved = cursorIndex(all, cu);
    const cursor = resolved >= 0 ? resolved : cursorIndex(all, cursorUuidRef.current);
    const target = nextTargetEntry(list, cursor, dir);
    if (target == null) { pulseClusterButton(kind); return; }
    // #463 S4 §6.3 — the jump loads the target's ANCHOR, which for a tier-2
    // landmark is the segment holding the failure; the visibility check below
    // resolves the OWNING turn through `ownerTurnIndex`, which indexes this
    // same tier-1 array.
    const turn = turns[target.ownerTurnIndex];
    // Reset to `all` IF the current mode would hide the target. Precise check
    // (spec §5): find the target's TOP-LEVEL RenderNode (recursing into nested
    // subagents), test nodeVisible. A node missing from `groups` (not yet paged
    // in) is treated as hidden → reset.
    const mode = focusModeRef.current;
    if (turn && mode !== 'all') {
      const node = findTopLevelNodeFor(groupsRef.current, turn.uuid, { subagent_meta: subagentMetaRef.current });
      const targetHidden = node == null || !nodeVisible(node, mode);
      if (targetHidden) dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef: conversationRefRef.current,
      jump: readerJump(conversationRefRef.current, target.anchorKey,
                       qualifiedInputRef.current, { innerAnchorKey: target.innerAnchorKey }),
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
    const target = list.at(-1);
    if (target == null) return;  // no occurrence → no-op
    const turn = turns[target.ownerTurnIndex];
    const mode = focusModeRef.current;
    if (turn && mode !== 'all') {
      const node = findTopLevelNodeFor(groupsRef.current, turn.uuid, { subagent_meta: subagentMetaRef.current });
      const targetHidden = node == null || !nodeVisible(node, mode);
      if (targetHidden) dispatch({ type: 'SET_CONV_FOCUS_MODE', mode: 'all' });
    }
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef: conversationRefRef.current,
      jump: readerJump(conversationRefRef.current, target.anchorKey,
                       qualifiedInputRef.current, { innerAnchorKey: target.innerAnchorKey }),
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
      conversationRef: conversationRefRef.current,
      jump: readerJump(conversationRefRef.current, uuid, qualifiedInputRef.current),
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
    setExactFind(null);
    threadRef.current?.focus?.();
  }, []);

  // #177 S6 — drop highlight terms whenever the bar closes (e.g. a session
  // switch closes find via the store) so stale marks don't linger.
  // #238 R5 — also clear the distinct current-match class on close.
  useEffect(() => {
    if (!convFindOpen) { setFindTerms(null); setExactFind(null); markCurrent(null); }
  }, [convFindOpen, markCurrent]);
  // #238 R5 — clear the current-match class when the needle changes: React may
  // reuse an imperatively-classed <mark> across a find-terms change, so a stale
  // conv-mark--current must be dropped before the next landing re-applies it.
  useEffect(() => { markCurrent(null); }, [findTerms, markCurrent]);
  // #238 R5 — clear the current-match class on reader unmount (best-effort
  // teardown so a recycled DOM node never carries a leftover class).
  useEffect(() => () => { markCurrent(null); }, [markCurrent]);

  // ── #463 S2 §2.7 — the reasoning reading spine ───────────────────────────
  //
  // Sequential movement between Codex reasoning headings, resolved in TWO steps
  // so it works across a page boundary without touching S4's surfaces.
  //
  // Every heading lives inside a segment, and segment keys are already
  // resolvable through the outline's `segment_uuids` channel and
  // `positionByKey`. So when the next heading is not in the loaded window the
  // reader resolves the target SEGMENT first, through the existing jump /
  // `loadToTarget` pipeline — which no-ops only when an identifier is absent
  // from that map, and a segment key never is — then locates the heading inside
  // that segment once it has loaded.
  //
  // Heading keys are never placed in the outline and never in `member_uuids`:
  // `loadToTarget` reads `member_uuids` as "already loaded" and would no-op on
  // content that has not been fetched (§1.3). This adds no outline entry, no new
  // jump family and no change to `buildOutlineTargets` — those belong to S4.
  //
  // THE INVARIANT, and the reason the code below is shaped the way it is: the
  // heading cursor advances if and only if the step actually lands on a visible
  // heading. The first implementation advanced it unconditionally, before the
  // "is the element there?" guard, which made navigation dead in the browser —
  // Virtuoso mounts a window (measured: 18 of 348 heading elements on a 99-item
  // conversation), so nearly every step targeted an unmounted element, consumed
  // a heading and returned with nothing marked and no feedback. The same
  // unconditional advance also walked headings that a non-`all` focus mode
  // removes from the render tree, and left the cursor stranded whenever the next
  // segment carried no reasoning at all (spec §7: 82.9% of them do not).
  //
  // `visibleItems` — the flattened focus-mode-filtered render tree — rather than
  // `detail.items`, so a mode that hides a turn also removes its headings from
  // the walk instead of leaving invisible stops in it. It is the SAME list the
  // suppression set above is computed from, which is what keeps the two in
  // agreement. Under the default `all` mode `visible` IS `groups` (same array
  // identity), so this is the same list as before.
  const headingTargets = useMemo(() => {
    const out: { key: string; uuid: string }[] = [];
    for (const item of visibleItems) {
      for (const block of item.blocks) {
        if (block.kind !== 'codex_reasoning' || !block.headings?.length) continue;
        for (const heading of block.headings) {
          // §2.6 — a heading the duplicate-aggregate rule removed has no element,
          // so leaving it in the walk would make `h` refuse to advance (correctly,
          // per the invariant above) and read as stuck.
          if (suppressedHeadings.has(heading.key)) continue;
          out.push({ key: heading.key, uuid: item.anchor.uuid });
        }
      }
    }
    return out;
  }, [visibleItems, suppressedHeadings]);
  const headingTargetsRef = useRef(headingTargets);
  headingTargetsRef.current = headingTargets;
  // Every identifier the loaded window can already resolve. The edge walk uses it
  // to skip past keys that are loaded and carry no heading, instead of asking the
  // jump pipeline for a neighbour it already holds (which no-ops, so the walk
  // would re-request the same neighbour on every press and never progress).
  //
  // `turn_uuid` is deliberately NOT added. It is segment 0's KEY (§1.3), so
  // adding it marked an unloaded segment 0 as loaded whenever any later segment
  // of that turn was in the window: a deep link landing mid-turn then made every
  // heading in segment 0 unreachable by backward stepping, because the walk
  // skipped the key it needed to ask for. `anchor.uuid` and `member_uuids`
  // already cover exactly what is genuinely loaded — segment 0's own anchor uuid
  // IS its turn_uuid — so nothing is lost by leaving it out.
  const loadedKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const item of detail?.items ?? []) {
      keys.add(item.anchor.uuid);
      for (const member of item.member_uuids ?? []) keys.add(member);
    }
    return keys;
  }, [detail?.items]);
  const loadedKeysRef = useRef(loadedKeys);
  loadedKeysRef.current = loadedKeys;
  const currentHeadingRef = useRef<string | null>(null);
  // The item that owns the cursor's heading. Suppression is window-relative
  // (§2.6 evaluates "first occurrence in the turn" over the LOADED window), so a
  // backward page load can retire the very heading the reader is standing on.
  // Remembering the owner lets the next press resume inside the item they were
  // reading instead of treating the missing key as "no cursor at all" and
  // jumping to the top of the window.
  const currentHeadingUuidRef = useRef<string | null>(null);
  // An unresolved request. `key` = a heading that is loaded but whose row is not
  // mounted; `edge` = the cross-page continuation past the loaded window.
  const pendingHeadingRef = useRef<
    | { kind: 'key'; key: string; uuid: string; attempts: number }
    | { kind: 'edge'; dir: 1 | -1; afterKey: string | null; afterUuid: string | null;
        segment: string; steps: number }
    | null
  >(null);
  // A loaded top-level heading whose owning Virtuoso row is not mounted. State,
  // rather than only a ref, is load-bearing: the render that records the target
  // must first pass literal `followOutput={false}` before the measurement walk
  // starts, or react-virtuoso's resize watcher can pull the one-row tail back to
  // the bottom while the requested neighbour is being measured (#486).
  const [headingWalkTarget, setHeadingWalkTarget] = useState<
    { key: string; uuid: string } | null
  >(null);

  // The VISIBLE element carrying a heading key, or null. Attribute-walk rather
  // than an interpolated attribute selector: a heading key embeds an opaque hash
  // and a `#`, so building a selector from it would need escaping to stay valid.
  // `headingIsVisible` is what makes "landed" mean on screen rather than merely
  // in the document — see headingVisibility.ts.
  const findHeadingElement = useCallback((key: string): Element | null => {
    const root = bodyRef.current;
    if (!root) return null;
    const el = [...root.querySelectorAll('[data-heading-key]')]
      .find((node) => node.getAttribute('data-heading-key') === key);
    return el != null && headingIsVisible(el) ? el : null;
  }, []);
  // Move the `.conv-heading--current` class onto one element. Imperative,
  // mirroring the jump pipeline's own `.conv-details--jumpopen` handling: the
  // heading elements are rendered deep inside `MessageBlocks`, and threading a
  // "current" prop through the whole block walk to style one line would re-render
  // every block on every step.
  const paintHeading = useCallback((el: Element) => {
    bodyRef.current?.querySelectorAll('.conv-heading--current')
      .forEach((node) => node.classList.remove('conv-heading--current'));
    el.classList.add('conv-heading--current');
  }, []);

  // Mark + reveal one heading, and REPORT whether it landed. The lookup happens
  // BEFORE anything is mutated: a step that cannot land must leave both the
  // previous mark and the cursor exactly as they were.
  const focusHeading = useCallback((target: { key: string; uuid: string }): boolean => {
    const el = findHeadingElement(target.key);
    if (!el) return false;
    paintHeading(el);
    el.scrollIntoView({ block: 'center' });
    currentHeadingRef.current = target.key;
    currentHeadingUuidRef.current = target.uuid;
    return true;
  }, [findHeadingElement, paintHeading]);
  const focusHeadingRef = useRef(focusHeading);
  focusHeadingRef.current = focusHeading;

  // Ask for one heading. It lands immediately when its row is mounted; otherwise
  // the viewport is moved toward the OWNING item and the request is retried once
  // the row renders. The cursor is not touched until one of those lands.
  const requestHeading = useCallback((target: { key: string; uuid: string }) => {
    if (focusHeadingRef.current(target)) {
      pendingHeadingRef.current = null;
      setHeadingWalkTarget(null);
      return;
    }
    const existing = pendingHeadingRef.current;
    if (existing?.kind === 'key' && existing.key === target.key) return;
    pendingHeadingRef.current = { kind: 'key', key: target.key, uuid: target.uuid, attempts: 0 };
    // Loaded, and inside the render tree: this is Virtuoso's window, so scroll
    // the owning row into the mounted set and land on the retry.
    //
    // A hit inside a SUBAGENT node does not qualify. `nodeIndexForUuid` recurses
    // into a subagent's `items` and `children`, so a nested target always
    // resolves — even when the thread's disclosure is shut, or the member sits
    // outside the thread's own internal render window (subagentWindow.ts). A raw
    // scrollToIndex cannot open either, so the fallback below was unreachable
    // for exactly the targets that needed it.
    const hit = nodeIndexForUuid(nodesRef.current, target.uuid, firstItemIndexRef.current);
    if (hit && nodesRef.current[hit.arrayIndex]?.kind !== 'subagent') {
      setHeadingWalkTarget(target);
      return;
    }
    const owner = resolveJumpOwner(nodesRef.current, target.uuid);
    if (owner?.isCardRoot && owner.ownerSubagentKey != null) {
      // A bucket-root jump deliberately flashes the collapsed card without
      // opening it (#188 B1). Heading navigation has a different contract: the
      // heading itself must become visible, so force-open the complete ancestor
      // chain and let the same measurement walk mount its top-level card.
      const chain = ancestorKeys(owner.ownerSubagentKey, detail?.subagent_meta);
      setForcedOpenKeys((prev) => {
        const next = new Set(prev);
        for (const key of chain) next.add(key);
        return next;
      });
      setHeadingWalkTarget(target);
      return;
    }
    // Not reachable by scrolling alone — a collapsed or internally windowed
    // subagent thread, or a target the current focus mode coalesced away. The
    // jump pipeline pages, un-hides and force-opens; it is the only path that
    // can do those things.
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef: conversationRefRef.current,
      // #463 S4 remediation C-2 — a heading target's `key` is exactly the
      // `data-heading-key` the reader renders, so the landing can align the
      // heading itself rather than the top of the item holding it. The pending
      // request below still runs and still paints the mark; this only removes
      // the intermediate landing on the wrong scroll position.
      jump: readerJump(conversationRefRef.current, target.uuid,
                       qualifiedInputRef.current, { innerAnchorKey: target.key }),
    });
  }, [detail?.subagent_meta]);
  const requestHeadingRef = useRef(requestHeading);
  requestHeadingRef.current = requestHeading;

  // Drive a loaded, unmounted heading through the same coverage walk as a jump,
  // but land on the exact heading rather than the owning row. The walk retries
  // after structural quiescence even when the final rendered range is unchanged,
  // breaking the circular "range change arms the retry" dependency from #486.
  // A failed walk restores the previous heading before releasing follow mode, so
  // a keypress cannot consume or visibly clear the established cursor.
  useEffect(() => {
    const target = headingWalkTarget;
    if (!target) return;
    let cancelled = false;
    let token = 0;
    const aborted = () => cancelled || !gates.isCurrentRun(token);
    const previous = currentHeadingRef.current && currentHeadingUuidRef.current
      ? { key: currentHeadingRef.current, uuid: currentHeadingUuidRef.current }
      : null;
    const walkOwner = (owner: { key: string; uuid: string }) => {
      let lastRequest: { index: number; first: number; last: number } | null = null;
      const body = bodyRef.current;
      const pixelHop = Math.max((body?.clientHeight ?? 0) * 2, 1_024);
      // Enough retries to physically cross the whole current size model in the
      // worst one-giant-row case, with a ceiling that keeps a detached walk
      // bounded. Ordinary item-level walks still finish far earlier.
      const pixelBudget = body ? Math.ceil(body.scrollHeight / pixelHop) + 4 : 0;
      return walkToTarget({
        getTargetArrayIndex: () => {
          const hit = nodeIndexForUuid(nodesRef.current, owner.uuid, firstItemIndexRef.current);
          return hit ? hit.arrayIndex : null;
        },
        getMountedArrayRange: () => ({
          first: renderedRangeRef.current.first - firstItemIndexRef.current,
          last: renderedRangeRef.current.last - firstItemIndexRef.current,
        }),
        scrollToIndex: (index, alignEdge) => {
          const range = {
            first: renderedRangeRef.current.first - firstItemIndexRef.current,
            last: renderedRangeRef.current.last - firstItemIndexRef.current,
          };
          if (body && lastRequest?.index === index
              && lastRequest.first === range.first && lastRequest.last === range.last) {
            // Virtuoso ignores an identical estimate-based request after its
            // measurement correction rebounds to the same mounted row. Move
            // through that row in real pixels instead; the next item-index step
            // is then issued from new geometry rather than replaying a no-op.
            body.scrollTop += index < range.first ? -pixelHop : pixelHop;
          } else {
            virtuosoRef.current?.scrollToIndex({ index, align: alignEdge, behavior: 'auto' });
          }
          lastRequest = { index, ...range };
        },
        // A bare scrollToIndex can look unchanged for one animation frame before
        // Virtuoso mounts the requested row and then rebounds after measuring it.
        // Three consecutive stable frames keep the walk alive through that delayed
        // correction; the generic jump pipeline retains its established one-frame
        // quiescence contract.
        // A timer keeps bounded heading navigation progressing even when the
        // browser throttles animation frames for a partially obscured tab.
        quiesce: () => waitForLayoutQuiesce(owner.uuid, aborted, 3, true),
        isAborted: aborted,
        maxSteps: Math.min(512, Math.max(60, nodesRef.current.length, pixelBudget)),
        initialWindow: Math.max(1, renderedRangeRef.current.last - renderedRangeRef.current.first + 1),
      });
    };
    void (async () => {
      token = gates.beginProgrammaticRun();
      try {
        if (focusHeadingRef.current(target)) {
          pendingHeadingRef.current = null;
          return;
        }
        const outcome = await walkOwner(target);
        if (aborted()) return;
        if (outcome === 'mounted' && focusHeadingRef.current(target)) {
          pendingHeadingRef.current = null;
          return;
        }
        if (previous && !focusHeadingRef.current(previous)) {
          const restored = await walkOwner(previous);
          if (!aborted() && restored === 'mounted') focusHeadingRef.current(previous);
        }
        if (pendingHeadingRef.current?.kind === 'key'
            && pendingHeadingRef.current.key === target.key) {
          pendingHeadingRef.current = null;
        }
      } finally {
        gates.endProgrammaticRun(token);
        if (!cancelled) {
          setHeadingWalkTarget((live) => live?.key === target.key ? null : live);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [headingWalkTarget, gates, waitForLayoutQuiesce]);

  // The nearest key past `fromUuid` in DOCUMENT order that the loaded window does
  // NOT already hold, from the outline's positional index over the FULL wire turn
  // list. That map covers segments, which the navigation turn subset does not:
  // `adaptQualifiedOutline` drops meta and event-bearing turns, and on real Codex
  // data that is every heavy assistant response and so every multi-segment turn.
  //
  // Loaded keys are skipped rather than returned. Past the end of the heading
  // list every loaded key in that direction carries no heading by construction,
  // so returning the immediate neighbour would ask the pipeline to load something
  // it already has — the request no-ops, nothing changes, and the next press
  // computes the same neighbour again.
  const nextUnloadedKey = useCallback((fromUuid: string | null, dir: 1 | -1) => {
    const positions = outlineRef.current?.positionByKey;
    if (!positions || fromUuid == null) return null;
    const from = positions.get(fromUuid);
    if (from == null) return null;
    const loaded = loadedKeysRef.current;
    let best: { key: string; pos: number } | null = null;
    for (const [key, pos] of positions) {
      if (dir > 0 ? pos <= from : pos >= from) continue;
      if (loaded.has(key)) continue;
      if (best == null || (dir > 0 ? pos < best.pos : pos > best.pos)) best = { key, pos };
    }
    return best?.key ?? null;
  }, []);

  // Where the cursor sits in a heading list, tolerant of its key having left it.
  //
  // `exact` distinguishes the two cases the step then has to treat differently.
  // On an exact hit the press moves ONE heading on. When the key is gone but its
  // owning item survives — suppression is window-relative, so a backward page
  // load can retire the heading the reader is standing on — the press instead
  // LANDS on the item's FIRST surviving heading, which keeps the reader inside
  // the passage they were reading. The fallback to the first (or last) heading
  // of the whole window covers both remaining cases: the item is gone, and the
  // item survives with EVERY one of its headings retired (the 19.8% exact-repeat
  // case, where a follower segment's whole heading list duplicates the arriving
  // segment 0's).
  //
  // The first surviving heading is the right answer in BOTH directions, not an
  // approximation of "nearest". Retirement removes a PREFIX of the item's list,
  // because Codex aggregates are cumulative and prefix-extended (§7: 19.8% of
  // adjacent block pairs repeat exactly, 12.5% extend as a strict prefix, and
  // 0.06% merely overlap), so the arriving earlier segment renders the item's
  // LEADING headings and every survivor sits after the retired cursor. Taking
  // the LAST one going back therefore walked the reader FORWARD, to the end of
  // the item they were reading. On the 0.06% partial-overlap shape the first
  // survivor can sit slightly before the cursor instead of after it, which
  // still leaves the reader inside the passage they were reading.
  //
  // The ordinal in the retired key is recoverable — heading keys are
  // `<block_key>#<zero-based ordinal>` (§1.2) — but it is not used, because the
  // ordinal is scoped to a BLOCK and says nothing about position among the
  // item's surviving headings once a whole block has dropped out.
  const locateHeadingCursor = useCallback((
    list: readonly { key: string; uuid: string }[],
    key: string | null,
    uuid: string | null,
  ): { index: number; exact: boolean } => {
    if (key == null) return { index: -1, exact: false };
    const exact = list.findIndex((t) => t.key === key);
    if (exact >= 0) return { index: exact, exact: true };
    if (uuid == null) return { index: -1, exact: false };
    return { index: list.findIndex((t) => t.uuid === uuid), exact: false };
  }, []);

  const stepHeading = useCallback((dir: 1 | -1) => {
    const list = headingTargetsRef.current;
    const current = currentHeadingRef.current;
    const { index, exact } = locateHeadingCursor(
      list, current, currentHeadingUuidRef.current);
    // No cursor at all: the press lands on the first (or last) heading rather
    // than skipping it. A surviving-but-moved cursor lands on its own item's
    // first surviving heading; only an exact hit steps on by one.
    const next = index < 0
      ? (dir > 0 ? list[0] : list[list.length - 1])
      : exact ? list[index + dir] : list[index];
    if (next) { requestHeadingRef.current(next); return; }
    if (index < 0) return;   // nothing to step to at all — a graceful no-op
    // Past the loaded edge. Resolve the nearest UNLOADED key and let the existing
    // jump pipeline fetch it; the effect below continues from there.
    const target = nextUnloadedKey(list[index]?.uuid ?? null, dir);
    if (target == null) return;  // genuinely the end — a graceful no-op
    pendingHeadingRef.current = {
      kind: 'edge', dir, afterKey: current, afterUuid: currentHeadingUuidRef.current,
      segment: target, steps: 0,
    };
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef: conversationRefRef.current,
      jump: readerJump(conversationRefRef.current, target, qualifiedInputRef.current),
    });
  }, [nextUnloadedKey, locateHeadingCursor]);
  const stepHeadingRef = useRef(stepHeading);
  stepHeadingRef.current = stepHeading;

  // Step two of the two-step resolution, and the mounted-window retry. Runs on
  // every axis a landing depends on: the loaded heading list, Virtuoso's
  // rendered range (`renderedRangeRev`) — a heading can become reachable purely
  // by scrolling, with no data change at all — and `forcedOpenKeys`, because a
  // heading inside a collapsed subagent thread becomes reachable purely by that
  // thread opening. Opening a small thread moves neither of the other two, so
  // without this dep the request waited for an unrelated scroll or was dropped
  // at `HEADING_MOUNT_ATTEMPTS`, leaving the reader at the thread with no mark.
  useEffect(() => {
    const pending = pendingHeadingRef.current;
    if (!pending) return;
    if (pending.kind === 'key') {
      // The self-driven top-level walk owns both its retry budget and its exact
      // landing. Checking this BEFORE focus is load-bearing: a passive range
      // effect can otherwise claim the briefly mounted row during Virtuoso's
      // correction, clear the walk, and leave the new cursor unpainted after
      // the row rebounds. This path remains for post-jump subagent commits only.
      if (headingWalkTarget?.key === pending.key) return;
      if (focusHeading(pending)) {
        pendingHeadingRef.current = null;
        setHeadingWalkTarget((live) => live?.key === pending.key ? null : live);
        return;
      }
      // Drop a request whose heading has left the visible list (a window trim, a
      // focus-mode change). A stale request must never fire later with no
      // keypress behind it.
      if (!headingTargets.some((t) => t.key === pending.key)) {
        pendingHeadingRef.current = null;
        return;
      }
      if (pending.attempts >= HEADING_MOUNT_ATTEMPTS) { pendingHeadingRef.current = null; return; }
      pending.attempts += 1;
      return;
    }
    // The edge walk. The cursor has not moved, so continue from it — through the
    // same tolerant lookup `stepHeading` uses, so a page that arrived and retired
    // the cursor's own heading resumes inside its item instead of dropping the
    // request and leaving the press dead.
    const { index: idx, exact } = locateHeadingCursor(
      headingTargets, pending.afterKey, pending.afterUuid);
    if (pending.afterKey != null && idx < 0) { pendingHeadingRef.current = null; return; }
    const next = idx < 0
      ? (pending.dir > 0 ? headingTargets[0] : headingTargets[headingTargets.length - 1])
      : exact ? headingTargets[idx + pending.dir] : headingTargets[idx];
    if (next) { pendingHeadingRef.current = null; requestHeadingRef.current(next); return; }
    // What loaded carries no heading in this direction. Walk on rather than
    // dead-ending here: most segments carry no reasoning, so stopping at the
    // first neighbour would strand the reader mid-conversation.
    const further = pending.steps + 1 >= HEADING_EDGE_WALK_MAX
      ? null
      : nextUnloadedKey(pending.segment, pending.dir);
    if (further == null) { pendingHeadingRef.current = null; return; }
    pendingHeadingRef.current = { ...pending, segment: further, steps: pending.steps + 1 };
    dispatch({
      type: 'OPEN_CONVERSATION',
      conversationRef: conversationRefRef.current,
      jump: readerJump(conversationRefRef.current, further, qualifiedInputRef.current),
    });
  }, [headingTargets, renderedRangeRev, forcedOpenKeys, headingWalkTarget, focusHeading, nextUnloadedKey, locateHeadingCursor]);

  // Re-assert the mark from the cursor.
  //
  // `.conv-heading--current` is a class on one DOM element while the cursor is a
  // ref, so any remount — a page swap, a Virtuoso window move — destroys the
  // first and keeps the second, and the two then disagree. QA measured exactly
  // that on conversation 019f5b77: the press that triggers a cross-page load
  // left `.conv-heading--current` null while mounted headings dropped to zero,
  // so the press read as dead even though the cursor had moved. Re-applying the
  // class on every pass that can change the DOM is what makes a press either
  // complete onto its heading once the page arrives or leave the existing mark
  // where it was until it can.
  //
  // Declared AFTER the resolution effect so a landing in the same commit paints
  // first; this is then a no-op, because the class is already where it belongs.
  // It never scrolls — restoring a mark is not a navigation.
  useEffect(() => {
    const key = currentHeadingRef.current;
    if (key == null) return;
    const marked = bodyRef.current?.querySelector('.conv-heading--current');
    if (marked?.getAttribute('data-heading-key') === key) return;
    const el = findHeadingElement(key);
    if (el) paintHeading(el);
  }, [headingTargets, renderedRangeRev, forcedOpenKeys, findHeadingElement, paintHeading]);

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
        // #463 S2 §2.7 — `h`/`H` step to the next/previous Codex reasoning
        // HEADING. Mnemonic, and free: the taken conversations-view set is
        // j k [ ] g o e E u U b B p P c C v n N End a L m M i I t f / Escape,
        // and `h` was one of the slots (h w x y z) the #217 S6 F4 audit
        // confirmed unused. `H` is likewise free — the taken uppercase set is
        // E U B P C N M I L, plus the dashboard-view-only `S` (share) and `N`.
        // The dashboard globals `r`/`q`/`v`/`a`/`n`/`N` carry no `view`, so the
        // keymap dispatcher scopes them to the dashboard and they never reach
        // the conversations view. Uppercase-is-previous matches every other
        // jump family here. Gated on the shared `guard` (no open modal, no
        // input mode, no filter popover) + the #156 conversations-view scope.
        mk('h', () => stepHeadingRef.current(1)),
        mk('H', () => stepHeadingRef.current(-1)),
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
    const ReaderThread = forwardRef<HTMLDivElement, Record<string, unknown>>(
      function ReaderThread(props, ref) {
        // #232 fix — SPREAD the props Virtuoso passes the List. The critical one is
        // `style`, which carries `padding-top` / `padding-bottom` = the VIRTUAL
        // SCROLL SPACE (the total measured height of the rows above / below the
        // mounted window). Dropping it (the prior `className`-only render) collapses
        // the List to just the mounted window's contiguous height: `scrollHeight`
        // shrinks to ~5 rows, no virtual space exists, and EVERY programmatic scroll
        // — `scrollToIndex` (j/k, jump-to-latest, outline jumps, find-step) and the
        // openScrollIntent landing — can only reach the few initially-mounted rows
        // (measured in-browser: a 278-node session had scrollHeight frozen at
        // ~5430px and scrolling mounted nothing further; jump-to-latest never reached
        // the tail). Virtuoso also passes `data-testid="virtuoso-item-list"` here.
        // Our `className` / `tabIndex` are applied AFTER the spread so they win (the
        // thread carries the styling + the focus target). `children` rides through
        // the spread too, so it isn't re-applied.
        const { children: _children, className: _vClassName, ...rest } = props as {
          children?: React.ReactNode; className?: string;
        } & Record<string, unknown>;
        return (
          <div
            {...rest}
            ref={(el) => {
              threadRef.current = el;
              if (typeof ref === 'function') ref(el);
              else if (ref) (ref as React.MutableRefObject<HTMLDivElement | null>).current = el;
            }}
            className="conv-reader-thread"
            tabIndex={-1}
          >
            {props.children as React.ReactNode}
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
              conversationRef,
              jump: readerJump(conversationRef, g.firstUuid, qualifiedInput),
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
          // #239 — the active window anchor (in-flight jump target / pinned turn)
          // so the windowed body centers on the target member in the SAME commit
          // the card force-opens — the deep-link target is mounted for the existing
          // walk-to-mount + direct-scroll land. Threaded down to nested cards too.
          windowAnchorUuid={windowAnchorUuid}
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
  }, [detail, sessionId, riseFor, getItemRef, getCardRef, handleSubagentOpenChange, forcedOpenKeys, isMobile, childCtx, suppressToolUseIds, spawnKindByToolUseId, jumpedUuid, windowAnchorUuid, cursorUuid, bulkSweep]);

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

  // #304 S2 (Codex F6) — every reader-state shell is programmatically focusable
  // so the compare focus-return last resort works on loading/error/empty paths too.
  if (loading && !detail) return (
    <div className="conv-reader conv-reader--loading" tabIndex={-1}>
      <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><SpinnerIcon /></span>
        <div className="conv-state-title">Loading conversation…</div></div>
    </div>
  );
  if (error) return (
    <div className="conv-reader conv-reader--error" tabIndex={-1}>
      <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><WarningIcon /></span>
        <div className="conv-state-title">{error}</div></div>
    </div>
  );
  if (!detail) return (
    <div className="conv-reader conv-reader--empty" tabIndex={-1}>
      <div className="conv-state"><span className="conv-state-glyph" aria-hidden="true"><ChatIcon /></span>
        <div className="conv-state-title">Select a conversation</div>
        <div className="conv-state-hint">Choose one from the list to start reading.</div></div>
    </div>
  );

  return (
    <div className="conv-reader" tabIndex={-1} ref={setReaderRoot}>
      {!isWide ? (
        // #304 S1 — the compact two-row header now applies across the whole
        // constrained band (!isWide, ≤1100px), not only phones. Row 1: ← Back ·
        // title · ⋯ overflow. Row 2 (slim): the compact Focus dropdown · 🔍 Find
        // · ☰ Outline. The secondary actions (Export, Compare, Latest, bulk
        // expand/collapse) + the completion/cost summaries fold into the ⋯ menu so
        // reading starts in the top ~40% of the screen. Back is gated on the
        // `mobileBack` prop (single-pane ≤880 only), so 881–1100 gets this compact
        // header WITHOUT Back beside the rail. The full desktop strip renders only
        // when wide (≥1101px). The `--mobile` class name is retained (component +
        // CSS + tests reference it); its structural CSS is self-gating on the
        // class across ≤1100.
        <div className="conv-reader-head conv-reader-head--mobile">
          <div className="conv-reader-row1">
            {mobileBack && (
              <button type="button" className="conv-back" onClick={() => dispatch({ type: 'SELECT_CONVERSATION', conversationRef: null })}>← Back</button>
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
              sessionId={conversationRef}
              exportTitle={detail.title}
              anonMode={anonMode}
              onToggleAnon={toggleAnonMode}
              onCompare={() => dispatch({ type: 'START_COMPARE_PICK', anchorRef: conversationRef })}
              onLatest={detail.last_anchor ? () => { void jumpToLatest(); } : null}
              latestBusy={jumpingLatest}
              onExpandAll={() => sweepDetails(true)}
              onCollapseAll={() => sweepDetails(false)}
              completionTotal={outline?.task_completion?.all_done ? outline.task_completion.total : null}
              costCumulative={cumCost.cost}
              costTotal={detail.cost_usd}
              costApprox={cumCost.approx}
              // #304 S3 (Codex F3) — the repaired ✓ Complete JUMP in the compact
              // band: promotes the read-only completion row to an actionable
              // menuitem when the session is all-done.
              onCompletionJump={outline?.task_completion?.all_done ? () => jumpToCompletion(outline.task_completion!.anchor_uuid) : null}
            />
          </div>
          <div className="conv-reader-row2">
            {/* The 4-button focus segment collapses to one compact dropdown that
                also absorbs the FocusMoreMenu sub-options (Edits/Bash/Subagents). */}
            <FocusCompactMenu
              focusMode={focusMode}
              subagents={subagentOptions}
              onSelect={(mode) => dispatch({ type: 'SET_CONV_FOCUS_MODE', mode })}
              errorCount={errorTurnCount}
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
      <div className={`conv-reader-head${density === 'compact' ? ' conv-reader-head--folded' : ''}`}>
        {mobileBack && (
          <button type="button" className="conv-back" onClick={() => dispatch({ type: 'SELECT_CONVERSATION', conversationRef: null })}>← Back</button>
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
        <div className="conv-reader-controls" onFocusCapture={onHeadFocusCapture}>
          {density === 'compact' ? (
            /* #304 S3 §1 — FOLDED desktop (reader element <720px — in practice the
               outline column open at 1101–~1400px): swap the four clusters for the
               S1 compact primitives, LITERALLY reused. The DESKTOP title/meta head
               layout above stays (no Back, full model ids); completion + cost
               surface as the ⋯ menu's rows (Q2). The completion row is the
               repaired JUMP (onCompletionJump — Codex F3). */
            <>
              <FocusCompactMenu
                focusMode={focusMode}
                subagents={subagentOptions}
                onSelect={(mode) => dispatch({ type: 'SET_CONV_FOCUS_MODE', mode })}
                errorCount={errorTurnCount}
              />
              <button
                type="button"
                className="conv-find-toggle"
                data-hdr-role="find"
                aria-pressed={convFindOpen}
                aria-label="Find in conversation"
                title="Find in conversation (/ or ⌘F / Ctrl+F)"
                onClick={toggleFind}
              ><SearchIcon /> Find</button>
              <button
                type="button"
                className="conv-outline-toggle"
                data-hdr-role="outline"
                aria-pressed={effectiveOutlineOpen}
                aria-label="Toggle session outline"
                title="Toggle session outline (o)"
                onClick={toggleOutline}
              >☰ Outline</button>
              <ReaderOverflowMenu
                sessionId={conversationRef}
                exportTitle={detail.title}
                anonMode={anonMode}
                onToggleAnon={toggleAnonMode}
                onCompare={() => dispatch({ type: 'START_COMPARE_PICK', anchorRef: conversationRef })}
                onLatest={detail.last_anchor ? () => { void jumpToLatest(); } : null}
                latestBusy={jumpingLatest}
                onExpandAll={() => sweepDetails(true)}
                onCollapseAll={() => sweepDetails(false)}
                completionTotal={outline?.task_completion?.all_done ? outline.task_completion.total : null}
                costCumulative={cumCost.cost}
                costTotal={detail.cost_usd}
                costApprox={cumCost.approx}
                onCompletionJump={outline?.task_completion?.all_done ? () => jumpToCompletion(outline.task_completion!.anchor_uuid) : null}
              />
            </>
          ) : (
          <>
          {/* #304 S3 §1 — the wide strip is regrouped into intent clusters
              (reading / nav / share / bulk) plus a quiet right-aligned status
              cluster. Each cluster is an atomic wrap unit (internally nowrap; the
              outer row keeps #238 R1's flex-wrap so whole GROUPS reflow). Grouping
              + order + the status demotion carry the hierarchy — no per-control
              chrome redesign. Keyboard bindings (] [ End / o) are unaffected (they
              live on the reader keymap, not the buttons). */}
          {/* READING — the focus radiogroup + the ▾ More menu. */}
          <div className="conv-hdr-group" data-hdr-group="reading">
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
                // cluster chip), NOT stats.error_count (the server's total
                // error-EVENT count, which double-counts a turn with multiple
                // error tools). The Stats card keeps the reconciliation phrasing
                // "N errors in M turns". Round 4 dropped the "== what clicking
                // the Errors filter navigates to" clause: the filter keeps
                // visible NODES, so a segmented Codex turn can show more rows
                // than it contributes to this count (see `errorTurnCount`).
                // #463 S4 remediation round 3 (F2) — and NOT the length of the
                // target list either, which landmark awareness made one entry per
                // failing CALL while this comment kept claiming turns.
                const errCount = errorTurnCount;
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
          </div>
          {/* NAV — Find · Outline · Latest (the on-screen navigation cluster). */}
          <div className="conv-hdr-group" data-hdr-group="nav">
            {/* #205 S2 (F3) — Find toggle. Mirrors the outline toggle's
                aria-pressed semantics + chrome; gives the `/` shortcut a visible,
                tappable counterpart (the only find affordance on touch). */}
            <button
              type="button"
              className="conv-find-toggle"
              data-hdr-role="find"
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
              data-hdr-role="outline"
              aria-pressed={effectiveOutlineOpen}
              aria-label="Toggle session outline"
              title="Toggle session outline (o)"
              onClick={toggleOutline}
            >☰ Outline</button>
            {/* jump-to-latest spec §5 — "Latest ↓" control. Hidden when
                last_anchor is null (a genuinely empty conversation). Disabled with
                a spinner glyph while jumpToLatest() resets to the tail. Bound to `End`. */}
            {detail.last_anchor && (
              <button
                type="button"
                className="conv-jump-latest"
                data-hdr-role="latest"
                aria-label="Jump to latest message"
                title="Jump to latest (End)"
                disabled={jumpingLatest}
                onClick={() => { void jumpToLatest(); }}
              >{jumpingLatest ? '… ' : ''}Latest ↓</button>
            )}
          </div>
          {/* SHARE — Anon · Export ▾ · Compare (the sharing / comparison cluster). */}
          <div className="conv-hdr-group" data-hdr-group="share">
            {/* #281 S4 — the "Anonymize" mode toggle chip, next to Export ▾.
                Default ON, persisted; single source for the menu + per-card copy. */}
            <button
              type="button"
              className="conv-anon-toggle"
              data-hdr-role="anon"
              aria-pressed={anonMode}
              aria-label="Anonymize shared transcripts"
              title={
                anonMode
                  ? 'Anonymize ON — exports & copies redact project paths, home, username & known secrets (best-effort; review before sharing)'
                  : 'Anonymize OFF — exports & copies are raw'
              }
              onClick={toggleAnonMode}
            >
              {anonMode ? '🎭 Anon' : 'Anon off'}
            </button>
            {/* #217 S5 §4 — whole-session export menu (F1/F5). Local state with
                its own Esc/outside-click close; fetches the new /export route. */}
            <ExportMenu conversationRef={conversationRef} title={detail.title} anonMode={anonMode} />
            {/* #217 S7 F10 — "Compare with…" — enters rail pick-mode with this
                session as the anchor (START_COMPARE_PICK). The rail then shows a
                banner and rows pick the second session. #304 S3 (Codex F9) — the
                #228 S5 E4 leading ::before divider + margin are suppressed inside
                the sharing cluster (the group border IS the divider now). */}
            <button
              type="button"
              id="conv-compare-with"
              className="conv-compare-with"
              data-hdr-role="compare"
              aria-label="Compare this session with another"
              title="Compare with another session"
              onClick={() => dispatch({ type: 'START_COMPARE_PICK', anchorRef: conversationRef })}
            ><span className="conv-compare-with-glyph" aria-hidden="true">⟷ </span>Compare with…</button>
          </div>
          {/* BULK — ⤢⤡ moves from first to LAST action position (least-used
              cluster; its old leading spot was unearned). #228 S2 surfaced the
              keyboard-only ] / [ sweeps as a discoverable control. */}
          <div className="conv-hdr-group" data-hdr-group="bulk">
            <div className="conv-bulk-toggle" role="group" aria-label="Expand or collapse all threads">
              <button
                type="button"
                className="conv-bulk-btn"
                data-hdr-role="bulk-expand"
                aria-label="Expand all threads"
                title="Expand all (])"
                onClick={() => sweepDetails(true)}
              >⤢</button>
              <button
                type="button"
                className="conv-bulk-btn"
                data-hdr-role="bulk-collapse"
                aria-label="Collapse all threads"
                title="Collapse all ([)"
                onClick={() => sweepDetails(false)}
              >⤡</button>
            </div>
          </div>
          {/* #304 S3 §1 (Q2) — quiet, right-aligned STATUS cluster (non-action):
              the #217 S6 F3 cumulative-cost chip + the #217 S5 F7 ✓ Complete jump.
              Complete keeps its jump affordance; its accent-button chrome is
              demoted in CSS. Both were the FIRST controls in the old flat strip;
              here they sit apart from the actions as glanceable status. */}
          <div className="conv-hdr-status">
            <CumulativeCostChip cumulative={cumCost.cost} total={detail.cost_usd} approx={cumCost.approx} pending={currentTurnUuid == null} />
            {outline?.task_completion?.all_done && (
              <button
                type="button"
                className="conv-complete-chip"
                data-hdr-role="complete"
                aria-label={`Session complete: ${outline.task_completion.total} tasks done — jump to the final checklist`}
                title="Jump to the final task checklist"
                onClick={() => jumpToCompletion(outline.task_completion!.anchor_uuid)}
              >
                ✓ Complete · {outline.task_completion.total}
              </button>
            )}
          </div>
          </>
          )}
        </div>
      </div>
      )}
      <ProviderThreadNav detail={detail} conversationRef={conversationRef} />
      {/* #177 S6 — the floating in-conversation find bar. Absolutely
          positioned top-right inside the reader column (zero layout shift). The
          stepRef wires its cursor to the reader's n/N bindings. */}
      {convFindOpen && (
        <FindBar
          sessionId={qualifiedInput ? conversationRef : sessionId}
          onClose={onFindClose}
          onTermsChange={onFindTermsChange}
          onExactFindChange={setExactFind}
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
      <ExactFindContext.Provider value={exactFind}>
      <TranscriptContext.Provider value={transcriptCtx}>
      {/* #463 S1 (#448) — an in-scroller indicator for a page request in flight
          with rows already mounted, so a reverse page or a jump drain is visibly
          in progress rather than silently pending. It is a SIBLING of the
          scroller, absolutely positioned over it, so mounting it never perturbs
          Virtuoso's own sizing.

          There is deliberately no companion `detail && nodes.length === 0`
          spinner. One shipped here and was removed after a requestAnimationFrame
          probe counted ZERO appearances of it across about fifteen cold opens,
          spanning the heaviest Codex conversation, a 3,890-turn Claude
          conversation and both viewports: the first detail response always
          arrives before React commits an empty-node frame. The same probe saw
          this indicator 21 times in a single drain, so the instrument was not
          blind. Do not reintroduce that state without evidence it can render. */}
      {detail && nodes.length > 0 && fetching && (
        <div className="conv-paging-indicator" data-testid="conv-paging-indicator" role="status">
          <span className="conv-paging-glyph" aria-hidden="true"><SpinnerIcon /></span>
          <span className="conv-paging-label">Loading more…</span>
        </div>
      )}
      {/* #463 S1 — the give-up state. Every path that abandons a jump must end
          somewhere VISIBLE. Before this, an unreachable deep link left the
          indicator flickering and then vanishing for good, so the reader showed a
          populated header, a transcript positioned nowhere near the requested
          message, and nothing at all to say the jump had failed.

          It is NOT gated on `!fetching`. Hiding it while a request is in flight
          made the message disappear and reappear around every later page the
          user's own scrolling triggered, for a jump that had already finished.
          The next page request CLEARS the state instead (the effect above).
          That retires the message rather than hiding it, but it does not make
          the two mutually exclusive: the clear runs in a passive effect, so
          they stack for the frame in which `fetching` first turns true, and a
          request already in flight when the give-up is set produces no rising
          edge and so leaves them stacked until it finishes. Both windows are
          narrow and neither is worth a layout effect; do not restate this as
          mutual exclusion.

          `role="status"` sits on the message span rather than on the container,
          so the live region announces the message alone and the dismiss button
          is not read as part of it. */}
      {detail && (jumpFailure === 'unresolved' || jumpFailure === 'landing_failed') && (
        <div className="conv-paging-indicator conv-paging-indicator--failed" data-testid="conv-jump-unresolved">
          <span className="conv-paging-label" role="status">
            {jumpFailure === 'landing_failed'
              ? 'The linked message could not be brought into view.'
              : 'The linked message was not found.'}
          </span>
          <button
            type="button"
            className="conv-paging-dismiss"
            onClick={() => setJumpFailure(null)}
            aria-label="Dismiss"
          >×</button>
        </div>
      )}
      {detail && jumpFailure === 'load_failed' && (
        <div className="conv-paging-indicator conv-paging-indicator--failed" data-testid="conv-jump-load-failed">
          <span className="conv-paging-label" role="status">Could not finish loading the linked message. Check your connection and try again.</span>
          <button
            type="button"
            className="conv-paging-dismiss"
            onClick={() => setJumpFailure(null)}
            aria-label="Dismiss"
          >×</button>
        </div>
      )}
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
          // #232 / #281 S5 A2 — ARMING GATE (defense-in-depth on top of
          // loadToTarget's re-entrancy guard). On a cold open Virtuoso fires
          // startReached/endReached as it settles the initial position (the
          // deep-link target's scrollToIndex, or the tail), BEFORE any user scroll.
          // Paging on those transient edge hits re-enters the very drain that's
          // positioning the window. `gates.shouldPage(edge)` folds both conditions:
          // the edge is armed (the open SETTLED — first atBottomStateChange OR the
          // jump landed OR the 750ms fallback) AND no programmatic run is in flight.
          // A genuine user scroll-to-edge happens only after settle, so real
          // reverse/forward paging is preserved.
          if (!gates.shouldPage('start')) return;
          doLoadPrevRef.current();
        }}
        endReached={() => {
          if (!gates.shouldPage('end')) return;
          void loadMore();
        }}
        // #281 S5 B1 (#285 FIX) — a truthy RAW `followOutput` prop (even this
        // callback) installs react-virtuoso's resize-autoscroll-to-LAST watcher
        // regardless of the callback's return, so `() => false` does NOT disable
        // it (that watcher is what pulled single-page opens to the bottom). While
        // the machine reports `followMode === 'suspended'` (a 'top' landing or an
        // anchor/restore open) we pass the LITERAL `false` prop to uninstall the
        // watcher; otherwise the live stick callback runs. …and while a same-session
        // jump is active (`activeJumpForSession`, #291) — the jump-dispatch render
        // must already be `false` so a jump-driven force-open's `SIZE_INCREASED`
        // finds no armed watcher. #486 applies the same literal suspension while
        // a heading measurement walk owns the viewport.
        followOutput={(followMode === 'suspended' || activeJumpForSession || headingWalkTarget != null) ? false : (atBottom) => (atBottom ? (reduced ? 'auto' : 'smooth') : false)}
        atBottomThreshold={80}
        atBottomStateChange={(atBottom) => { atBottomRef.current = atBottom; gates.arm(); follow.settle(); if (atBottom) setNewCount((n) => (n ? 0 : n)); }}
        itemsRendered={onItemsRendered}
        increaseViewportBy={600}
        onScroll={onBodyScroll}
      />
      </TranscriptContext.Provider>
      </ExactFindContext.Provider>
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
