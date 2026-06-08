import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useConversation } from '../hooks/useConversation';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { groupSidechains } from './groupSidechains';
import { MessageItem } from './MessageItem';
import { SidechainGroup } from './SidechainGroup';
import { fmt } from '../lib/fmt';
import type { ConversationItem } from '../types/conversation';

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

  const groups = useMemo(() => groupSidechains(detail?.items ?? []), [detail?.items]);

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

  if (loading && !detail) return <div className="conv-reader conv-reader--loading">Loading…</div>;
  if (error) return <div className="conv-reader conv-reader--error">{error}</div>;
  if (!detail) return <div className="conv-reader conv-reader--empty">Select a conversation.</div>;

  return (
    <div className="conv-reader">
      <div className="conv-reader-head">
        {mobileBack && (
          <button type="button" className="conv-back" onClick={() => dispatch({ type: 'SELECT_CONVERSATION', sessionId: null })}>← Back</button>
        )}
        <div className="conv-reader-title">{detail.project_label || detail.session_id}</div>
        <div className="conv-reader-meta">
          {detail.git_branch ?? '—'} · {fmt.usd2(detail.cost_usd)} · {detail.models.join(', ')}
        </div>
      </div>
      <div className="conv-reader-body">
        <div className="conv-reader-thread">
          {groups.map((g) =>
            g.kind === 'subagent'
              ? <SidechainGroup
                  key={`sc-${g.subagentKey}`}
                  subagentKey={g.subagentKey}
                  items={g.items}
                  nested={g.nested}
                  getItemRef={getItemRef}
                  forceOpen={detail.session_id === sessionId && g.subagentKey === forcedOpenKey}
                />
              : <MessageItem key={g.item.anchor.uuid} item={g.item} ref={getItemRef(g.item)} />,
          )}
        </div>
        {hasMore && <div ref={sentinelRef} className="conv-load-sentinel">Loading more…</div>}
      </div>
    </div>
  );
}
