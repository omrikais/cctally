import { useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useConversations } from '../hooks/useConversations';
import { useConversationSearch } from '../hooks/useConversationSearch';
import { renderSnippet } from '../lib/snippet';
import { fmt } from '../lib/fmt';
import type { ConversationSummary, SearchHit } from '../types/conversation';

// Browse/search rail for the Conversations workspace (spec §4). When the
// needle is empty we browse the recent-conversations list (useConversations);
// otherwise we run the debounced cross-session search (useConversationSearch).
// The search input mirrors SessionsControls' input-mode discipline so global
// hotkeys stay suppressed while typing. The container carries the
// `conv-rail-search` class the view shell's '/' binding focuses.
export function ConversationRail() {
  const search = useSyncExternalStore(subscribeStore, () => getState().conversationSearch);
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
        ? <SearchList needle={search} ctx={ctx} />
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
  return (
    <div className="conv-rail-list">
      {rows.map((r) => (
        <BrowseRow key={r.session_id} row={r} ctx={ctx} active={r.session_id === selectedId} />
      ))}
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
      <div className="conv-rail-row-head">
        <span className="conv-rail-row-project">{row.project_label || '—'}</span>
        <span className="conv-rail-row-branch">{row.git_branch ?? '—'}</span>
      </div>
      <div className="conv-rail-row-meta">
        <span className="conv-rail-row-when">{fmt.startedShort(row.started_utc, ctx, { noSuffix: true })}</span>
        <span className="conv-rail-row-cost">{fmt.usd2(row.cost_usd)}</span>
        <span className="conv-rail-row-msgs">{row.msg_count} msgs</span>
      </div>
    </button>
  );
}

function SearchList({ needle, ctx }: { needle: string; ctx: RailCtx }) {
  const { hits, mode, loading, error } = useConversationSearch(needle);
  if (error) return <div className="conv-rail-list"><div className="conv-rail-empty">{error}</div></div>;
  if (loading && hits.length === 0) return <div className="conv-rail-list"><div className="conv-rail-empty">Searching…</div></div>;
  if (hits.length === 0) return <div className="conv-rail-list"><div className="conv-rail-empty">No matches.</div></div>;
  return (
    <div className="conv-rail-list">
      {mode === 'like' && <div className="conv-rail-hint">(basic search)</div>}
      {hits.map((h, i) => (
        <SearchRow key={`${h.session_id}-${h.uuid}-${i}`} hit={h} ctx={ctx} />
      ))}
    </div>
  );
}

function SearchRow({ hit, ctx }: { hit: SearchHit; ctx: RailCtx }) {
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
      <div className="conv-rail-row-head">
        <span className="conv-rail-row-project">{hit.project_label || '—'}</span>
        <span className="conv-rail-row-when">{fmt.startedShort(hit.ts, ctx, { noSuffix: true })}</span>
        <span className="conv-rail-row-cost">{fmt.usd2(hit.cost_usd)}</span>
      </div>
      <div className="conv-rail-row-snippet">{renderSnippet(hit.snippet)}</div>
    </button>
  );
}
