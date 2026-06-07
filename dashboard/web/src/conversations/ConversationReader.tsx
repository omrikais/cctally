import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useConversation } from '../hooks/useConversation';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { groupSidechains } from './groupSidechains';
import { MessageItem } from './MessageItem';
import { SidechainGroup } from './SidechainGroup';
import { fmt } from '../lib/fmt';

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

  const groups = useMemo(() => groupSidechains(detail?.items ?? []), [detail?.items]);

  // Lazy-load when the bottom sentinel scrolls into view.
  useEffect(() => {
    if (!sentinelRef.current || !hasMore) return;
    const obs = new IntersectionObserver((es) => { if (es[0].isIntersecting) void loadMore(); });
    obs.observe(sentinelRef.current);
    return () => obs.disconnect();
  }, [hasMore, loadMore]);

  // Jump-to-message: page until the target is loaded, then scroll+highlight.
  // Wait for the first page (`detail`) before attempting — otherwise the
  // effect would fire while page 1 is still in flight (nextAfter unknown),
  // page nowhere, and clear the jump prematurely. It re-runs when
  // detail?.items.length grows, so it engages once page 1 has landed.
  useEffect(() => {
    if (!jump || jump.session_id !== sessionId || !detail) return;
    let cancelled = false;
    void (async () => {
      await loadUntil(jump.uuid);
      if (cancelled) return;
      const el = itemRefs.current.get(jump.uuid);
      if (el) {
        el.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' });
        el.classList.add('conv-item--jumped');
        window.setTimeout(() => el.classList.remove('conv-item--jumped'), 2000);
        dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
        return;
      }
      // No ref yet. loadUntil may have just appended the target's page; its
      // MessageItem ref callback runs on React's NEXT commit, after this
      // microtask. Leave the jump set so the detail?.items.length re-fire
      // re-runs this effect once the element is attached, and we scroll
      // then. Only give up — clearing the jump — when paging is exhausted
      // (hasMore === false) and the element still never appeared (e.g. the
      // hit resolved into a collapsed sidechain member, which gets no ref).
      if (!hasMore) dispatch({ type: 'CLEAR_CONVERSATION_JUMP' });
    })();
    return () => { cancelled = true; };
    // `hasMore` is in deps so the give-up clear still fires on the edge
    // where the final page appends 0 items (items.length unchanged) but
    // flips the cursor → hasMore goes false. No infinite loop: loadUntil/
    // fetchNext serialize via loadingMoreRef, loadUntil returns once
    // exhausted-or-found, and hasMore only transitions a bounded number of
    // times.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, sessionId, detail?.items.length, hasMore]);

  const setItemRef = (item: { member_uuids: string[] }) => (el: HTMLDivElement | null) => {
    // Map EVERY member uuid → this element so a search hit on any
    // fragment resolves (anchor uuid is a prose fragment, but the
    // all-member map is belt-and-suspenders per spec §3).
    for (const u of item.member_uuids) {
      if (el) itemRefs.current.set(u, el); else itemRefs.current.delete(u);
    }
  };

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
        {groups.map((g) =>
          g.kind === 'subagent'
            ? <SidechainGroup key={`sc-${g.subagentKey}`} subagentKey={g.subagentKey} items={g.items} nested={g.nested} />
            : <MessageItem key={g.item.anchor.uuid} item={g.item} ref={setItemRef(g.item)} />,
        )}
        {hasMore && <div ref={sentinelRef} className="conv-load-sentinel">Loading more…</div>}
      </div>
    </div>
  );
}
