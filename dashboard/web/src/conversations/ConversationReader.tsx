import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useConversation } from '../hooks/useConversation';
import { useKeymap } from '../hooks/useKeymap';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { groupSidechains } from './groupSidechains';
import { isSystemMarker } from './systemMarkers';
import { MessageItem } from './MessageItem';
import { SidechainGroup } from './SidechainGroup';
import { ResultIcon, SpinnerIcon, WarningIcon, ChatIcon } from './ConvIcons';
import { fmt } from '../lib/fmt';
import type { ConversationItem } from '../types/conversation';

// First non-blank line of the first MAIN-session, non-marker human message;
// fallback project_label → session_id. Mirrors the kernel _session_titles_map
// (#165 Q6). The opening human is always on page 1.
function deriveReaderTitle(detail: { items: ConversationItem[]; project_label: string; session_id: string }): string {
  for (const it of detail.items) {
    if (it.kind === 'human' && !it.is_sidechain && it.text.trim() && !isSystemMarker(it.text)) {
      const line = it.text.split('\n').map((s) => s.trim()).find(Boolean);
      if (line) return line.length > 120 ? line.slice(0, 120).trimEnd() + '…' : line;
    }
  }
  return detail.project_label || detail.session_id;
}

// Paginated transcript reader (spec §4). Lazy-loads the next page when a
// bottom sentinel scrolls into view (IntersectionObserver), and supports a
// jump-to-message: when a search hit sets conversationJump for THIS session,
// page until the target uuid is loaded, scroll it into view, flash a
// transient highlight (reduced-motion aware), then clear the jump. Every
// member uuid maps to its rendered element so a hit on any folded fragment
// resolves.
export function ConversationReader({ sessionId, mobileBack }: { sessionId: string; mobileBack?: boolean }) {
  const { detail, loading, error, hasMore, loadMore, loadUntil } = useConversation(sessionId);
  const jump = useSyncExternalStore(subscribeStore, () => getState().conversationJump);
  const reduced = useReducedMotion();
  const sentinelRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  // Per-anchor-uuid memoized ref callbacks: a stable callback identity per item
  // so the memo'd MessageItems don't detach/re-attach on every paged append.
  const refCallbacks = useRef<Map<string, (el: HTMLDivElement | null) => void>>(new Map());
  // Tracks the pending highlight-removal timeout so it can be cancelled on
  // unmount (no classList.remove on a detached node, no leaked timer) and
  // superseded on a rapid re-jump (no two overlapping 2s timers racing).
  const highlightTimerRef = useRef<number | null>(null);
  // The subagent_key of the thread being force-opened for the in-flight jump
  // (#160). null when no force is active. Setting it opens that SidechainGroup in
  // the same render (its `open` is derived), so the target member's ref attaches
  // and the jump effect re-fires (forcedOpenKey dep) to scroll to it.
  const [forcedOpenKey, setForcedOpenKey] = useState<string | null>(null);
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
  const threadRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  // Stable mirrors so the `useMemo(() => [...], [])` keymap array never churns.
  const hasMoreRef = useRef(hasMore);
  hasMoreRef.current = hasMore;
  const loadMoreRef = useRef(loadMore);
  loadMoreRef.current = loadMore;
  const reducedRef = useRef(reduced);
  reducedRef.current = reduced;

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

  // #176 — floating "↑ Top of turn" button. Replaces the #175 sticky turn
  // header (which floated an opaque mask over the prose). `jumpTopVisible` gates
  // the button; `jumpTopTargetRef` holds the top-level block currently under the
  // viewport top so a click can scroll it back to its start. Both are reset on a
  // session switch (the reader is reused across conversations).
  const [jumpTopVisible, setJumpTopVisible] = useState(false);
  const jumpTopTargetRef = useRef<HTMLElement | null>(null);

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
  }, []);

  // Stick-if-at-bottom on a live append; otherwise preserve position + count the
  // new turns. Keyed on items.length (+ hasMore so prevHasMoreRef tracks each
  // commit). useLayoutEffect so atBottomRef reflects the PRE-append position and
  // the stick happens before paint (no visible jump).
  useLayoutEffect(() => {
    const b = bodyRef.current;
    const len = detail?.items.length ?? 0;
    const prevLen = prevLenRef.current;
    const added = len - prevLen;
    // Live append (not the final pagination page): already fully paged before
    // this growth, and not the very first page load (prevLen > 0).
    const live = added > 0 && prevHasMoreRef.current === false && prevLen > 0;
    if (b && live) {
      if (atBottomRef.current) {
        b.scrollTo({ top: b.scrollHeight });           // instant stick to the newest turn
      } else {
        // Capture `added` in a local const — the ref is mutated below, so the
        // functional updater must not read prevLenRef.current lazily.
        setNewCount((n) => n + added);                 // preserve position, surface the pill
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
  }, []);

  const groups = useMemo(() => groupSidechains(detail?.items ?? []), [detail?.items]);
  const title = useMemo(
    () => (detail ? deriveReaderTitle(detail) : ''),
    [detail],
  );

  // Lazy-load when the bottom sentinel scrolls into view.
  useEffect(() => {
    if (!sentinelRef.current || !hasMore) return;
    const obs = new IntersectionObserver((es) => { if (es[0].isIntersecting) void loadMore(); });
    obs.observe(sentinelRef.current);
    return () => obs.disconnect();
  }, [hasMore, loadMore]);

  // Jump-to-message: page until the target is loaded, then scroll+highlight.
  // Wait for the first page (`detail`) before attempting — otherwise the effect
  // would fire while page 1 is still in flight (nextAfter unknown), page nowhere,
  // and clear the jump prematurely. It re-runs when detail?.items.length grows
  // (a paged-in target's ref attaches on the next commit) and when forcedOpenKey
  // changes (a force-opened thread's member ref attaches in that commit).
  useEffect(() => {
    if (!jump || jump.session_id !== sessionId) {
      // Jump cleared, or it now points at another session — release any force-pin
      // so a thread we expanded for it isn't left pinned (the user regains
      // collapse control). No loop: this re-fires on the forcedOpenKey dep,
      // re-hits this guard with forcedOpenKey === null, and returns.
      if (forcedOpenKey !== null) setForcedOpenKey(null);
      return;
    }
    if (!detail || detail.session_id !== sessionId) return; // cross-session transient: keep the pin
    let cancelled = false;
    void (async () => {
      await loadUntil(jump.uuid);
      if (cancelled) return;
      const el = itemRefs.current.get(jump.uuid);
      if (el) {
        el.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' });
        el.classList.add('conv-item--jumped');
        if (highlightTimerRef.current != null) window.clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = window.setTimeout(() => {
          el.classList.remove('conv-item--jumped');
          highlightTimerRef.current = null;
        }, 2000);
        dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
        setForcedOpenKey(null); // reset for the next jump (thread stays open via its latch)
        return;
      }
      // No ref. Either the target just paged in (its MessageItem ref attaches on
      // React's NEXT commit — the detail?.items.length re-fire handles that), OR
      // it lives inside a COLLAPSED subagent thread (members are ref-less while
      // closed). For the collapsed case, force the owning thread open: `open` is
      // derived from forceOpen so it opens in the same commit, the member's ref
      // attaches, and this effect re-fires on the forcedOpenKey dep to scroll via
      // the branch above.
      const targetItem = detail.items.find((it) => it.member_uuids.includes(jump.uuid));
      if (targetItem && targetItem.subagent_key != null) {
        if (forcedOpenKey !== targetItem.subagent_key) {
          setForcedOpenKey(targetItem.subagent_key);
          return; // wait for the group to open + attach the ref, then re-fire
        }
        // Already forced open: the ref attaches in the forcedOpenKey commit (before
        // this re-fire), so reaching here means it's genuinely absent — fall through
        // to the exhaustion clear rather than spinning.
      }
      if (!hasMore) {
        dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
      }
    })();
    return () => { cancelled = true; };
    // hasMore stays in deps so the give-up clear fires on the edge where the final
    // page appends 0 items (items.length unchanged) but flips the cursor.
    // forcedOpenKey re-fires the effect once a force-opened thread has attached the
    // target's ref. No infinite loop: loadUntil/fetchNext serialize via
    // loadingMoreRef, hasMore transitions a bounded number of times, and the
    // forcedOpenKey path either resolves (clears) or settles to a stable key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, sessionId, detail?.items.length, hasMore, forcedOpenKey]);

  // Cancel any pending highlight-removal timer on unmount only (NOT on every
  // jump-effect re-run — that would strip the flash the instant the successful
  // jump dispatches CLEAR_CONVERSATION_JUMP and re-fires the effect).
  useEffect(() => () => {
    if (highlightTimerRef.current != null) window.clearTimeout(highlightTimerRef.current);
  }, []);

  // The reader is reused across session switches (ConversationsView mounts it at
  // a fixed position), so drop stale ref callbacks when the session changes.
  useEffect(() => () => { refCallbacks.current.clear(); }, [sessionId]);

  // The reused reader must not carry a force-pin across sessions (subagent_key is
  // only an agent-file hash). Reset on every session change; no-op on first mount.
  useEffect(() => { setForcedOpenKey(null); }, [sessionId]);

  // #175 — the reused reader must not carry the live-tail pill/scroll state across
  // sessions. Clearing `newCount` drops a stale "↓ N new" pill the instant we switch
  // conversations, and resetting `atBottomRef` keeps the next session's first live
  // append on its default stick-to-bottom path (until the user scrolls it).
  // #176 — also drop a stale floating "↑ Top of turn" button + its target so the
  // next conversation starts with the button hidden.
  useEffect(() => {
    setNewCount(0);
    atBottomRef.current = true;
    setJumpTopVisible(false);
    jumpTopTargetRef.current = null;
  }, [sessionId]);

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
  }, [groups]);

  // Render-time rise classifier (G1 §4b). Returns `['conv-rise', {style}]` for a
  // top-level group's FIRST appearance, or `['', undefined]` to suppress —
  // when reduced-motion is on, when the group was already painted (seenRef),
  // or when it OWNS the active jump target. The jump-target suppression MUST
  // be render-time (Codex P2): refs attach at commit BEFORE the jump effect
  // runs loadUntil/scroll/flash, so the rise/no-rise choice is made while
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

  // Reset the focused-turn cursor to the top on a session switch (the reused
  // reader carries no cursor across conversations).
  useEffect(() => { setFocusedIndex(0); }, [sessionId]);

  // Imperatively move the `conv-item--focused` class onto the cursor's child,
  // off every other. Re-runs on a cursor step AND when the group list changes
  // (paged appends grow `children`), so the ring tracks the right element.
  // Imperative (not a render prop) so memoized MessageItems don't re-render.
  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;
    const kids = thread.children;
    for (let i = 0; i < kids.length; i++) {
      kids[i].classList.toggle('conv-item--focused', i === focusedIndex);
    }
  }, [focusedIndex, groups]);

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
    const next = Math.max(0, Math.min(last, cur + delta));
    // At the last loaded group with more to come, kick a load; the cursor
    // advances on the next press once the new child has mounted.
    if (delta > 0 && cur === last && hasMoreRef.current) { void loadMoreRef.current(); return; }
    if (next === cur) return;
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
  }, []);

  const keymapBindings = useMemo(
    () => {
      const guard = () => !getState().openModal && getState().inputMode === null;
      const mk = (key: string, action: () => void) =>
        ({ key, scope: 'global' as const, view: 'conversations' as const, when: guard, action });
      return [
        mk('j', () => stepFocus(1)),
        mk('k', () => stepFocus(-1)),
        mk('[', () => sweepDetails(false)),
        mk(']', () => sweepDetails(true)),
        mk('g', () => jumpToTop()),
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
        <div className="conv-reader-title">{title || detail.session_id}</div>
        <div className="conv-reader-meta">
          {detail.project_label || '—'} · {detail.git_branch ?? '—'} · {fmt.usd2(detail.cost_usd)} · {detail.models.join(', ')}
        </div>
      </div>
      <div className="conv-reader-body" ref={bodyRef} onScroll={onBodyScroll}>
        <div className="conv-reader-thread" ref={threadRef}>
          {groups.map((g, idx) => {
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
                  forceOpen={detail.session_id === sessionId && g.subagentKey === forcedOpenKey}
                  riseClassName={riseClass}
                  riseStyle={riseStyle}
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
                      <MessageItem key={item.anchor.uuid} item={item} ref={getItemRef(item)} />
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
              />
            );
          })}
        </div>
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
