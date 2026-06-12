import { Fragment, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useConversations } from '../hooks/useConversations';
import { useConversationSearch } from '../hooks/useConversationSearch';
import { renderSnippet } from '../lib/snippet';
import { railDateBucket } from './railDateBucket';
import { fmt } from '../lib/fmt';
import type { ConversationSummary, SearchHit, SearchKind } from '../types/conversation';

// #177 S6 — kind chip facets. Order matches the Q7 mock (All · Prompts ·
// Assistant · Tools · Thinking). `Tools`/`Thinking` query the split index
// columns and disable while the one-time column split is still backfilling
// (searchDepth === 'prose-only').
const KIND_CHIPS: { kind: SearchKind; label: string; needsSplit: boolean }[] = [
  { kind: 'all', label: 'All', needsSplit: false },
  { kind: 'prompts', label: 'Prompts', needsSplit: false },
  { kind: 'assistant', label: 'Assistant', needsSplit: false },
  { kind: 'tools', label: 'Tools', needsSplit: true },
  { kind: 'thinking', label: 'Thinking', needsSplit: true },
];

// Browse/search rail for the Conversations workspace (spec §4). When the
// needle is empty we browse the recent-conversations list (useConversations);
// otherwise we run the debounced cross-session search (useConversationSearch).
// The search input mirrors SessionsControls' input-mode discipline so global
// hotkeys stay suppressed while typing. The container carries the
// `conv-rail-search` class the view shell's '/' binding focuses.
export function ConversationRail() {
  const search = useSyncExternalStore(subscribeStore, () => getState().conversationSearch);
  const kind = useSyncExternalStore(subscribeStore, () => getState().conversationSearchKind);
  const selected = useSyncExternalStore(subscribeStore, () => getState().selectedConversationId);
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const inputRef = useRef<HTMLInputElement>(null);

  const isSearching = search.trim() !== '';

  return (
    <aside className="conv-rail">
      <div className="conv-rail-search">
        <input
          ref={inputRef}
          type="search"
          className="conv-rail-search-input"
          placeholder="search all conversations…"
          value={search}
          onChange={(e) => dispatch({ type: 'SET_CONVERSATION_SEARCH', text: e.target.value })}
          onFocus={() => dispatch({ type: 'SET_INPUT_MODE', mode: 'search' })}
          onBlur={() => dispatch({ type: 'SET_INPUT_MODE', mode: null })}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              dispatch({ type: 'SET_CONVERSATION_SEARCH', text: '' });
              inputRef.current?.blur();
            }
          }}
        />
      </div>
      {isSearching
        ? <SearchList needle={search} kind={kind} ctx={ctx} />
        : <BrowseList selectedId={selected} ctx={ctx} />}
    </aside>
  );
}

interface RailCtx { tz: string; offsetLabel: string }

function BrowseList({ selectedId, ctx }: { selectedId: string | null; ctx: RailCtx }) {
  const { rows, loading, error, hasMore, loadMore } = useConversations();
  if (error) return <div className="conv-rail-list"><div className="conv-rail-empty">{error}</div></div>;
  if (loading && rows.length === 0) return <div className="conv-rail-list"><div className="conv-rail-empty">Loading…</div></div>;
  if (rows.length === 0) return <div className="conv-rail-list"><div className="conv-rail-empty">No conversations.</div></div>;
  // rows are date-desc; the bucket label changes monotonically as you scroll.
  let lastBucket: string | null = null;
  const now = Date.now();
  return (
    <div className="conv-rail-list">
      {rows.map((r) => {
        const bucket = railDateBucket(r.started_utc, ctx.tz, now);
        const isNewBucket = bucket !== lastBucket;
        if (isNewBucket) lastBucket = bucket;
        return (
          <Fragment key={r.session_id}>
            {isNewBucket && <div className="conv-rail-sec">{bucket}</div>}
            <BrowseRow row={r} ctx={ctx} active={r.session_id === selectedId} />
          </Fragment>
        );
      })}
      {hasMore && (
        <button type="button" className="conv-rail-more" onClick={() => void loadMore()}>
          Load more
        </button>
      )}
    </div>
  );
}

function BrowseRow({ row, ctx, active }: { row: ConversationSummary; ctx: RailCtx; active: boolean }) {
  return (
    <button
      type="button"
      className={`conv-rail-row${active ? ' is-active' : ''}`}
      onClick={() => dispatch({ type: 'SELECT_CONVERSATION', sessionId: row.session_id })}
    >
      <div className="conv-rail-row-title">{row.title}</div>
      <div className="conv-rail-row-meta">
        <span className="conv-rail-row-project">{row.project_label || '—'}</span>
        <span className="conv-rail-row-branch">{row.git_branch ?? '—'}</span>
        <span className="conv-rail-row-when">{fmt.startedShort(row.started_utc, ctx, { noSuffix: true })}</span>
        <span className="conv-rail-row-cost">{fmt.usd2(row.cost_usd)}</span>
        <span className="conv-rail-row-msgs">{row.msg_count} msgs</span>
      </div>
    </button>
  );
}

// #177 S6 — single-select kind chip row, shown only while a needle is active.
// `Tools`/`Thinking` disable while the split index is still backfilling.
function KindChips({ kind, proseOnly }: { kind: SearchKind; proseOnly: boolean }) {
  return (
    <div className="conv-rail-chips" role="radiogroup" aria-label="Search kind">
      {KIND_CHIPS.map((c) => {
        const disabled = c.needsSplit && proseOnly;
        const checked = kind === c.kind;
        return (
          <button
            key={c.kind}
            type="button"
            role="radio"
            aria-checked={checked}
            disabled={disabled}
            title={disabled ? 'indexing…' : undefined}
            className={`conv-rail-chip${checked ? ' is-on' : ''}`}
            onClick={() => dispatch({ type: 'SET_CONVERSATION_SEARCH_KIND', kind: c.kind })}
          >
            {c.label}
          </button>
        );
      })}
    </div>
  );
}

function SearchList({ needle, kind, ctx }: { needle: string; kind: SearchKind; ctx: RailCtx }) {
  const { hits, mode, total, loading, loadingMore, searchDepth, error, loadMore } =
    useConversationSearch(needle, kind);
  const proseOnly = searchDepth === 'prose-only';
  const remaining = total - hits.length;
  // Count line: "No results" / "{total} results" / "{total} results · basic search".
  const countText =
    total === 0 ? 'No results' : `${total} results${mode === 'like' ? ' · basic search' : ''}`;
  return (
    <div className="conv-rail-list">
      <KindChips kind={kind} proseOnly={proseOnly} />
      {error
        ? <div className="conv-rail-empty">{error}</div>
        : loading && hits.length === 0
          ? <div className="conv-rail-empty">Searching…</div>
          : (
            <>
              <div className="conv-rail-count" aria-live="polite">{countText}</div>
              {hits.map((h, i) => (
                <SearchRow key={`${h.session_id}-${h.uuid}-${i}`} hit={h} ctx={ctx} />
              ))}
              {hits.length < total && (
                <button
                  type="button"
                  className="conv-rail-more"
                  disabled={loadingMore}
                  onClick={() => loadMore()}
                >
                  Load {Math.min(50, remaining)} more ({remaining} remaining)
                </button>
              )}
            </>
          )}
    </div>
  );
}

function SearchRow({ hit, ctx }: { hit: SearchHit; ctx: RailCtx }) {
  const badges = hit.match_kinds ?? [];
  return (
    <button
      type="button"
      className="conv-rail-row conv-rail-row--hit"
      onClick={() =>
        dispatch({
          type: 'OPEN_CONVERSATION',
          sessionId: hit.session_id,
          jump: { session_id: hit.session_id, uuid: hit.uuid },
        })
      }
    >
      <div className="conv-rail-row-title">
        <span className="conv-rail-row-title-text">{hit.title}</span>
        {badges.length > 0 && (
          <span className="conv-rail-kindbs">
            {badges.map((b) => (
              <span key={b} className="conv-rail-kindb">{b}</span>
            ))}
          </span>
        )}
      </div>
      <div className="conv-rail-row-meta">
        <span className="conv-rail-row-project">{hit.project_label || '—'}</span>
        <span className="conv-rail-row-when">{fmt.startedShort(hit.ts, ctx, { noSuffix: true })}</span>
        <span className="conv-rail-row-cost">{fmt.usd2(hit.cost_usd)}</span>
      </div>
      <div className="conv-rail-row-snippet">{renderSnippet(hit.snippet)}</div>
    </button>
  );
}
