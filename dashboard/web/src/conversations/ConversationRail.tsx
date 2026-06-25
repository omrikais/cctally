import { Fragment, useEffect, useRef, useSyncExternalStore } from 'react';
import { dispatch, getState, subscribeStore } from '../store/store';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useConversations } from '../hooks/useConversations';
import { useConversationSearch } from '../hooks/useConversationSearch';
import { renderSnippet } from '../lib/snippet';
import { railDateBucket } from './railDateBucket';
import { ConversationFiltersPopover } from './ConversationFiltersPopover';
import { pickBannerLabel } from './pickBannerLabel';
import { fmt } from '../lib/fmt';
import type { ConversationFilters, ConversationSummary, RailSortKey, SearchHit, SearchKind } from '../types/conversation';

// #217 S4 / I-2.4 — rail sort options, 1:1 with the backend `_SORTS` keys.
const SORT_OPTIONS: { key: RailSortKey; label: string }[] = [
  { key: 'recent', label: 'Recent' },
  { key: 'oldest', label: 'Oldest' },
  { key: 'cost', label: 'Cost' },
  { key: 'messages', label: 'Messages' },
  { key: 'project', label: 'Project' },
];

// #177 S6 — kind chip facets. Order matches the Q7 mock (All · Prompts ·
// Assistant · Tools · Thinking). `Tools`/`Thinking` query the split index
// columns and disable while the one-time column split is still backfilling
// (searchDepth === 'prose-only').
//
// #217 S4 / I-3.2 — the two cross-session STRUCTURAL facets (Title / Files) join
// the row, visually grouped after a subtle separator so they read as distinct
// from the content kinds. Neither rides the split index (title →
// conversation_title_fts/LIKE, files → LIKE over conversation_file_touches), so
// both are `needsSplit:false` — NOT gated by `search_depth: prose-only`. `group`
// drives the in-row separator (§5f): the divider renders where it changes from
// 'content' to 'structural'.
const KIND_CHIPS: {
  kind: SearchKind;
  label: string;
  needsSplit: boolean;
  group: 'content' | 'structural';
}[] = [
  { kind: 'all', label: 'All', needsSplit: false, group: 'content' },
  { kind: 'prompts', label: 'Prompts', needsSplit: false, group: 'content' },
  { kind: 'assistant', label: 'Assistant', needsSplit: false, group: 'content' },
  { kind: 'tools', label: 'Tools', needsSplit: true, group: 'content' },
  { kind: 'thinking', label: 'Thinking', needsSplit: true, group: 'content' },
  { kind: 'title', label: 'Title', needsSplit: false, group: 'structural' },
  { kind: 'files', label: 'Files', needsSplit: false, group: 'structural' },
];

// #217 S4 / I-3.3 / #223 — match-kind badge labels. Exported + `satisfies`-typed
// against the SearchHit['match_kinds'] union so a NEW union member without a
// label entry fails `tsc` (a runtime test alone can't catch that). Any unmapped
// value still falls back to itself in KindBadges.
export const MATCH_KIND_LABELS = {
  tool: 'tool',
  thinking: 'thinking',
  title: 'title',
  file: 'file',
} satisfies Record<NonNullable<SearchHit['match_kinds']>[number], string>;

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
  const railSort = useSyncExternalStore(subscribeStore, () => getState().conversationRailSort);
  // #217 S7 F10 — comparison pick-mode. When `comparePick` is set (the reader's
  // "Compare with…" affordance fired START_COMPARE_PICK), the rail shows a banner
  // and rows PICK the second session (OPEN_COMPARE) instead of opening it. Esc
  // cancels.
  const comparePick = useSyncExternalStore(subscribeStore, () => getState().comparePick);
  // #228 S5 E5 — the shared rail title cache, so the pick banner names the
  // anchor session instead of echoing an opaque short hash.
  const titles = useSyncExternalStore(subscribeStore, () => getState().conversationTitles);
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const inputRef = useRef<HTMLInputElement>(null);

  // Esc cancels pick-mode from anywhere in the rail (the banner's Cancel button
  // is the visible affordance; this is the keyboard parity). Bound only while
  // pick-mode is active so it never competes with the view-level Esc otherwise.
  useEffect(() => {
    if (!comparePick) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        dispatch({ type: 'CANCEL_COMPARE_PICK' });
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [comparePick]);

  const isSearching = search.trim() !== '';
  const chips = activeFilterChips(filters);
  // #228 S5 E5 — the cached title (truncated) when known, else the short hash.
  const pickLabel = comparePick ? pickBannerLabel(comparePick.anchor, titles) : null;

  return (
    <aside className={`conv-rail${comparePick ? ' conv-rail--picking' : ''}`}>
      {comparePick && (
        <div className="conv-rail-pickbanner" role="status" aria-live="polite">
          <span className="conv-rail-pickbanner-text">
            Comparing with{' '}
            {pickLabel?.kind === 'title'
              ? <strong className="conv-rail-pickbanner-title">“{pickLabel.text}”</strong>
              : <code>{pickLabel?.text}</code>}
            {' '}— pick a session
          </span>
          <button
            type="button"
            className="conv-rail-pickcancel"
            aria-label="Cancel comparison pick"
            onClick={() => dispatch({ type: 'CANCEL_COMPARE_PICK' })}
          >
            Cancel
          </button>
        </div>
      )}
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
          {/* #217 S4 / I-2.5 — filters apply to BOTH browse and search (the
              shared-filter decision), so the button stays enabled in search mode
              and the popover/chips render in both modes. */}
          <button
            type="button"
            className={`conv-rail-filters-btn${filtersOpen ? ' is-on' : ''}`}
            aria-expanded={filtersOpen}
            title="Filter conversations"
            onClick={() => dispatch({ type: 'TOGGLE_CONV_FILTERS' })}
          >
            Filters ▾
          </button>
          {/* #217 S4 / I-2.4 — rail sort control. Always visible; the active key
              threads into the browse `sort` param via useConversations. */}
          <label className="conv-rail-sort">
            <span className="conv-rail-sort-label">Sort</span>
            <select
              className="conv-rail-sort-select"
              aria-label="Sort conversations"
              value={railSort}
              onChange={(e) => dispatch({
                type: 'SET_CONVERSATION_RAIL_SORT',
                sort: e.target.value as RailSortKey,
              })}
            >
              {SORT_OPTIONS.map((o) => (
                <option key={o.key} value={o.key}>{o.label}</option>
              ))}
            </select>
          </label>
        </div>
        {filtersOpen && <ConversationFiltersPopover />}
        {chips.length > 0 && (
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
        ? <SearchList needle={search} kind={kind} ctx={ctx} pickAnchor={comparePick?.anchor ?? null} />
        : <BrowseList selectedId={selected} ctx={ctx} pickAnchor={comparePick?.anchor ?? null} />}
    </aside>
  );
}

// #217 S7 F10 — the click action for a rail row, shared by browse + search rows.
// In pick-mode (`pickAnchor` set) a click on a NON-anchor row dispatches
// OPEN_COMPARE { a: anchor, b: row }; otherwise it falls back to the row's normal
// open/select action. The anchor row is rendered disabled, so its click never
// reaches here.
function pickOr(
  pickAnchor: string | null,
  rowSessionId: string,
  fallback: () => void,
): () => void {
  if (pickAnchor && pickAnchor !== rowSessionId) {
    return () => dispatch({ type: 'OPEN_COMPARE', a: pickAnchor, b: rowSessionId });
  }
  return fallback;
}

interface RailCtx { tz: string; offsetLabel: string }

function BrowseList({ selectedId, ctx, pickAnchor }: { selectedId: string | null; ctx: RailCtx; pickAnchor: string | null }) {
  const { rows, loading, error, hasMore, loadMore, loadingMore, filterDegraded, sortDegraded, retry } = useConversations();
  const filters = useSyncExternalStore(subscribeStore, () => getState().conversationFilters);
  // filters spec §1 dual-branch parity — a one-line muted note when the
  // project/cost/rebuild axes couldn't apply (rollup non-authoritative). Rendered
  // above the list so it shows even on a degraded empty result. #217 S4 / I-2.3 —
  // the parallel sort_degraded note for a cost/project sort that fell back to
  // recent order in the same non-authoritative window.
  const degradedNote = (
    <>
      {filterDegraded && (
        <div className="conv-rail-filters-degraded">Project/cost/rebuild filters apply once indexing finishes.</div>
      )}
      {sortDegraded && (
        <div className="conv-rail-sort-degraded">Cost/Project sort unavailable while indexing — showing recent order.</div>
      )}
    </>
  );
  if (error) return (
    <div className="conv-rail-list">{degradedNote}
      <div className="conv-rail-empty">
        {error}
        <button type="button" className="conv-rail-retry" onClick={() => retry()}>Retry</button>
      </div>
    </div>
  );
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
            <BrowseRow row={r} ctx={ctx} active={r.session_id === selectedId} pickAnchor={pickAnchor} />
          </Fragment>
        );
      })}
      {hasMore && (
        // #217 S3 E10#7 — match SearchList's disabled-while-loading affordance
        // (decision d; no sentinel-ization). The button greys + shows a loading
        // label while a page is in flight so a second click can't re-fire.
        <button
          type="button"
          className="conv-rail-more"
          disabled={loadingMore}
          onClick={() => void loadMore()}
        >
          {loadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
    </div>
  );
}

function BrowseRow({ row, ctx, active, pickAnchor }: { row: ConversationSummary; ctx: RailCtx; active: boolean; pickAnchor: string | null }) {
  const isAnchor = pickAnchor === row.session_id;
  return (
    <button
      type="button"
      className={`conv-rail-row${active ? ' is-active' : ''}${pickAnchor ? ' conv-rail-row--pick' : ''}${isAnchor ? ' conv-rail-row--anchor' : ''}`}
      disabled={isAnchor}
      aria-disabled={isAnchor || undefined}
      title={isAnchor ? 'This is the anchor session' : undefined}
      onClick={pickOr(pickAnchor, row.session_id, () => dispatch({ type: 'SELECT_CONVERSATION', sessionId: row.session_id }))}
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
// #217 S4 / I-3.2 — a subtle separator divides the content kinds (All …
// Thinking) from the two structural facets (Title / Files); the row still wraps
// to a second line on narrow widths (#205) and every chip keeps the ≥44px touch
// target via `.conv-rail-chip`.
function KindChips({ kind, proseOnly }: { kind: SearchKind; proseOnly: boolean }) {
  let prevGroup: 'content' | 'structural' | null = null;
  return (
    <div className="conv-rail-chips" role="radiogroup" aria-label="Search kind">
      {KIND_CHIPS.map((c) => {
        const disabled = c.needsSplit && proseOnly;
        const checked = kind === c.kind;
        const needsSep = prevGroup !== null && c.group !== prevGroup;
        prevGroup = c.group;
        return (
          <Fragment key={c.kind}>
            {needsSep && <span className="conv-rail-chips-sep" aria-hidden="true" />}
            <button
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
          </Fragment>
        );
      })}
    </div>
  );
}

function SearchList({ needle, kind, ctx, pickAnchor }: { needle: string; kind: SearchKind; ctx: RailCtx; pickAnchor: string | null }) {
  const { hits, mode, total, loading, loadingMore, searchDepth, filterDegraded, error, loadMore } =
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
      {/* #217 S4 / I-2.5 — shared-filter parity: a one-line note when a
          project/cost/rebuild filter couldn't apply to the search (rollup
          non-authoritative). The search response carries this TOP-LEVEL. */}
      {filterDegraded && (
        <div className="conv-rail-search-filters-degraded">Some filters unavailable while indexing.</div>
      )}
      {error
        ? <div className="conv-rail-empty" role="alert">{error}</div>
        : loading && hits.length === 0
          ? <div className="conv-rail-empty" role="status">Searching…</div>
          : (
            <>
              <div className="conv-rail-count" aria-live="polite">{countText}</div>
              {hits.map((h, i) => (
                <SearchRow key={`${h.session_id}-${h.uuid}-${i}`} hit={h} ctx={ctx} pickAnchor={pickAnchor} />
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

function SearchRow({ hit, ctx, pickAnchor }: { hit: SearchHit; ctx: RailCtx; pickAnchor: string | null }) {
  const badges = hit.match_kinds ?? [];
  const isAnchor = pickAnchor === hit.session_id;
  // #217 S4 / I-3.3 — a kind=files hit (`match_kinds` includes 'file') renders
  // the FILE PATH prominently (primary line, file styling) with the session
  // title secondary; its snippet IS the plain path. Every other hit keeps the
  // #177 S6 title-prominent layout. Click navigates to `hit.uuid` for both —
  // the first-turn anchor for a title hit, the most-recent-touch anchor for a
  // file hit — same as content hits.
  const isFileHit = badges.includes('file');
  return (
    <button
      type="button"
      className={`conv-rail-row conv-rail-row--hit${isFileHit ? ' conv-rail-row--filehit' : ''}${pickAnchor ? ' conv-rail-row--pick' : ''}${isAnchor ? ' conv-rail-row--anchor' : ''}`}
      disabled={isAnchor}
      aria-disabled={isAnchor || undefined}
      title={isAnchor ? 'This is the anchor session' : undefined}
      onClick={pickOr(pickAnchor, hit.session_id, () =>
        dispatch({
          type: 'OPEN_CONVERSATION',
          sessionId: hit.session_id,
          jump: { session_id: hit.session_id, uuid: hit.uuid },
        }),
      )}
    >
      {isFileHit ? (
        // File hit: the path leads (file styling), the session title trails as a
        // muted secondary line, the badge group rides top-right OUTSIDE the
        // clamp box (same flex discipline as the title row).
        <>
          <div className="conv-rail-row-title conv-rail-row-title--hit">
            <span className="conv-rail-row-filepath conv-rail-row-title-text">{hit.snippet}</span>
            {badges.length > 0 && <KindBadges badges={badges} />}
          </div>
          <div className="conv-rail-row-filetitle">{hit.title}</div>
        </>
      ) : (
        // #177 S6 — title row is a flex line: the 2-line-clamped title TEXT as a
        // min-width:0 child, the badge group trailing and flex-shrink:0 OUTSIDE
        // the clamp box so a long title can never clip it off as a third line.
        <div className="conv-rail-row-title conv-rail-row-title--hit">
          <span className="conv-rail-row-title-text">{hit.title}</span>
          {badges.length > 0 && <KindBadges badges={badges} />}
        </div>
      )}
      <div className="conv-rail-row-meta">
        <span className="conv-rail-row-project">{hit.project_label || '—'}</span>
        <span className="conv-rail-row-when">{fmt.startedShort(hit.ts, ctx, { noSuffix: true })}</span>
        <span className="conv-rail-row-cost">{fmt.usd2(hit.cost_usd)}</span>
      </div>
      {/* #217 S4 QA fix — suppress the bottom snippet row for a file hit: its
          `snippet` IS the path, already shown prominently on the filepath line
          above, so rendering it here too duplicates the path (and would wrap a
          plain path in renderSnippet's [bracket] highlighting). Every other kind
          (prose/title/tool/thinking) keeps its snippet row. */}
      {!isFileHit && <div className="conv-rail-row-snippet">{renderSnippet(hit.snippet)}</div>}
    </button>
  );
}

// #217 S4 / I-3.3 — the match-kind badge group, factored out so the title and
// file-hit layouts share one renderer. Labels route through MATCH_KIND_LABELS
// (title→"title", file→"file"; tool/thinking identity).
function KindBadges({ badges }: { badges: NonNullable<SearchHit['match_kinds']> }) {
  return (
    <span className="conv-rail-kindbs">
      {badges.map((b) => (
        <span key={b} className="conv-rail-kindb">{MATCH_KIND_LABELS[b] ?? b}</span>
      ))}
    </span>
  );
}
