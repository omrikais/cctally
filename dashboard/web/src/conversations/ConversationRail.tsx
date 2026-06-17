import { Fragment, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useConversations } from '../hooks/useConversations';
import { useConversationSearch } from '../hooks/useConversationSearch';
import { renderSnippet } from '../lib/snippet';
import { railDateBucket } from './railDateBucket';
import { ConversationFiltersPopover } from './ConversationFiltersPopover';
import { fmt } from '../lib/fmt';
import type { ConversationFilters, ConversationSummary, SearchHit, SearchKind } from '../types/conversation';

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

// Human label for a stored datePreset KEY (the popover stores the key; the chip
// shows the label). Falls back to the from→to range for a raw range (preset key
// null).
const DATE_PRESET_LABELS: Record<string, string> = {
  'this-month': 'This month',
  'last-month': 'Last month',
  'last-7d': 'Last 7d',
};

// One active-filter chip: a label + the patch that removes that axis (filters
// spec §4 — removable chips under the search box). `projects` produces one chip
// PER selected project so each is individually removable.
interface FilterChip { key: string; label: string; remove: () => void }

// True when any browse-filter axis is active (non-empty projects / non-null
// date / cost / rebuild). Drives the distinct filtered-to-zero empty state
// (spec §4 — "No conversations match these filters" + Clear filters), so a
// genuinely empty install keeps the generic "No conversations." copy.
function anyFilterActive(f: ConversationFilters): boolean {
  return (
    f.dateFrom != null ||
    f.dateTo != null ||
    f.projects.length > 0 ||
    f.costMin != null ||
    f.costMax != null ||
    f.rebuildMin != null
  );
}

function activeFilterChips(f: ConversationFilters): FilterChip[] {
  const chips: FilterChip[] = [];
  if (f.dateFrom || f.dateTo) {
    const label = f.datePreset
      ? DATE_PRESET_LABELS[f.datePreset] ?? f.datePreset
      : f.dateFrom && f.dateTo
        ? `${f.dateFrom} → ${f.dateTo}`
        : f.dateFrom
          ? `from ${f.dateFrom}`
          : `to ${f.dateTo}`;
    chips.push({
      key: 'date', label,
      remove: () => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { dateFrom: null, dateTo: null, datePreset: null } }),
    });
  }
  for (const proj of f.projects) {
    chips.push({
      key: `proj:${proj}`, label: proj,
      remove: () => dispatch({
        type: 'SET_CONVERSATION_FILTERS',
        patch: { projects: f.projects.filter((p) => p !== proj) },
      }),
    });
  }
  if (f.costMin != null) {
    chips.push({
      key: 'costMin', label: `≥$${f.costMin}`,
      remove: () => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { costMin: null } }),
    });
  }
  if (f.costMax != null) {
    chips.push({
      key: 'costMax', label: `≤$${f.costMax}`,
      remove: () => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { costMax: null } }),
    });
  }
  if (f.rebuildMin != null) {
    chips.push({
      key: 'rebuildMin', label: `≥${f.rebuildMin} ♻`,
      remove: () => dispatch({ type: 'SET_CONVERSATION_FILTERS', patch: { rebuildMin: null } }),
    });
  }
  return chips;
}

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
  const filtersOpen = useSyncExternalStore(subscribeStore, () => getState().convFiltersOpen);
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const inputRef = useRef<HTMLInputElement>(null);

  const isSearching = search.trim() !== '';
  const chips = activeFilterChips(filters);

  return (
    <aside className="conv-rail">
      <div className="conv-rail-search">
        <div className="conv-rail-search-bar">
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
          {/* Filters apply to browse only; disabled (with a hint) while a needle
              is active (filters spec §4 search-mode coexistence). */}
          <button
            type="button"
            className={`conv-rail-filters-btn${filtersOpen ? ' is-on' : ''}`}
            disabled={isSearching}
            aria-expanded={filtersOpen}
            title={isSearching ? 'Filters apply to browse' : 'Filter conversations'}
            onClick={() => dispatch({ type: 'TOGGLE_CONV_FILTERS' })}
          >
            Filters ▾
          </button>
        </div>
        {filtersOpen && !isSearching && <ConversationFiltersPopover />}
        {!isSearching && chips.length > 0 && (
          <div className="conv-rail-filters-active">
            {chips.map((c) => (
              <button
                key={c.key}
                type="button"
                className="conv-rail-filters-activechip"
                title={`Remove ${c.label}`}
                aria-label={`Remove ${c.label}`}
                onClick={c.remove}
              >
                {c.label}<span className="conv-rail-filters-x" aria-hidden="true">✕</span>
              </button>
            ))}
            <button
              type="button"
              className="conv-rail-filters-clearall"
              onClick={() => dispatch({ type: 'CLEAR_CONVERSATION_FILTERS' })}
            >
              Clear all
            </button>
          </div>
        )}
      </div>
      {isSearching
        ? <SearchList needle={search} kind={kind} ctx={ctx} />
        : <BrowseList selectedId={selected} ctx={ctx} />}
    </aside>
  );
}

interface RailCtx { tz: string; offsetLabel: string }

function BrowseList({ selectedId, ctx }: { selectedId: string | null; ctx: RailCtx }) {
  const { rows, loading, error, hasMore, loadMore, filterDegraded } = useConversations();
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  // filters spec §1 dual-branch parity — a one-line muted note when the
  // project/cost/rebuild axes couldn't apply (rollup non-authoritative). Rendered
  // above the list so it shows even on a degraded empty result.
  const degradedNote = filterDegraded
    ? <div className="conv-rail-filters-degraded">Project/cost/rebuild filters apply once indexing finishes.</div>
    : null;
  if (error) return <div className="conv-rail-list">{degradedNote}<div className="conv-rail-empty">{error}</div></div>;
  if (loading && rows.length === 0) return <div className="conv-rail-list">{degradedNote}<div className="conv-rail-empty">Loading…</div></div>;
  if (rows.length === 0) {
    // spec §4 Empty state — filtered-to-zero is DISTINCT from no-conversations-
    // at-all: the former offers a one-click escape (Clear filters) so the user
    // isn't stranded behind an over-narrow filter set.
    if (anyFilterActive(filters)) {
      return (
        <div className="conv-rail-list">
          {degradedNote}
          <div className="conv-rail-empty conv-rail-empty--filtered">
            <div>No conversations match these filters.</div>
            <button
              type="button"
              className="conv-rail-empty-clear"
              onClick={() => dispatch({ type: 'CLEAR_CONVERSATION_FILTERS' })}
            >
              Clear filters
            </button>
          </div>
        </div>
      );
    }
    return <div className="conv-rail-list">{degradedNote}<div className="conv-rail-empty">No conversations.</div></div>;
  }
  // rows are date-desc; the bucket label changes monotonically as you scroll.
  // Buckets group on last_activity_utc (filters spec §4 last-activity-everywhere
  // / Codex P2 #6) so the grouping agrees with the recent sort + the date filter.
  let lastBucket: string | null = null;
  const now = Date.now();
  return (
    <div className="conv-rail-list">
      {degradedNote}
      {rows.map((r) => {
        const bucket = railDateBucket(r.last_activity_utc, ctx.tz, now);
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
        <span className="conv-rail-row-when">{fmt.startedShort(row.last_activity_utc, ctx, { noSuffix: true })}</span>
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
  // #177 S6 M1 — when the active chip is a split-needing kind (Tools/Thinking)
  // but the index is still backfilling (prose-only), the chip greys out yet the
  // hook keeps fetching that kind and renders an empty/degraded list. The
  // disabled chip's hover-only title="indexing…" is keyboard-unreachable, so
  // fold a visible `· indexing…` note into the count line to explain the empty
  // state inline.
  const indexing =
    proseOnly && (kind === 'tools' || kind === 'thinking');
  // Count line: "No results" / "{total} results" / "{total} results · basic
  // search" — plus a trailing "· indexing…" when the active kind needs the split
  // index that's still building.
  const countText =
    (total === 0 ? 'No results' : `${total} results${mode === 'like' ? ' · basic search' : ''}`) +
    (indexing ? ' · indexing…' : '');
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
      {/* #177 S6 — title row is a flex line: the 2-line-clamped title TEXT as a
          min-width:0 child, the badge group trailing and flex-shrink:0 OUTSIDE
          the clamp box so a long title can never clip it off as a third line. */}
      <div className="conv-rail-row-title conv-rail-row-title--hit">
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
