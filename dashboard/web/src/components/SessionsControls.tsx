import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import {
  dispatch,
  getState,
  subscribeStore,
  SESSION_SORT_KEYS,
  type SessionSortKey,
} from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import { useIsMobile } from '../hooks/useIsMobile';

function nextSort(cur: SessionSortKey): SessionSortKey {
  const idx = SESSION_SORT_KEYS.findIndex((o) => o.key === cur);
  const next = SESSION_SORT_KEYS[(idx === -1 ? 0 : idx + 1) % SESSION_SORT_KEYS.length];
  return next.key;
}

export function SessionsControls() {
  const filterText = useSyncExternalStore(subscribeStore, () => getState().filterText);
  const searchText = useSyncExternalStore(subscribeStore, () => getState().searchText);
  const searchMatches = useSyncExternalStore(subscribeStore, () => getState().searchMatches);
  const searchIndex = useSyncExternalStore(subscribeStore, () => getState().searchIndex);
  const sort = useSyncExternalStore(subscribeStore, () => getState().sessionsSort);
  const override = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.sessionsSortOverride,
  );

  const isMobile = useIsMobile();
  const [filterExpanded, setFilterExpanded] = useState(false);
  const [searchExpanded, setSearchExpanded] = useState(false);
  const filterInputRef = useRef<HTMLInputElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  // Mobile keeps the search input always rendered (Q1=B). The desktop
  // toggle still drives input-mode + auto-focus on `/`; on mobile the
  // input is visible without expansion and onFocus triggers the same
  // useEffect path so input-mode/select() are dispatched consistently.
  const showSearchContainer = searchExpanded || isMobile;
  // Chrome (match counter + prev/next) renders only when the user is
  // actively searching — focused or carrying a non-empty value. Without
  // this gate the always-present mobile container squeezed the input
  // down to ~14px at sub-360 widths because the counter+arrows demand
  // ~136px of fixed-width siblings; hiding them when the input is at
  // rest gives the row back to the input.
  const searchChromeShown = searchExpanded || searchText.length > 0;

  const openFilter = () => setFilterExpanded(true);
  const closeFilter = (opts?: { clear?: boolean }) => {
    if (opts?.clear) dispatch({ type: 'SET_FILTER', text: '' });
    setFilterExpanded(false);
    dispatch({ type: 'SET_INPUT_MODE', mode: null });
  };

  const openSearch = () => setSearchExpanded(true);
  const closeSearch = (opts?: { clear?: boolean }) => {
    if (opts?.clear) dispatch({ type: 'SET_SEARCH', text: '' });
    setSearchExpanded(false);
    dispatch({ type: 'SET_INPUT_MODE', mode: null });
  };

  useEffect(() => {
    if (filterExpanded) {
      filterInputRef.current?.focus();
      filterInputRef.current?.select();
      dispatch({ type: 'SET_INPUT_MODE', mode: 'filter' });
    }
  }, [filterExpanded]);

  useEffect(() => {
    if (searchExpanded) {
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
      dispatch({ type: 'SET_INPUT_MODE', mode: 'search' });
    }
  }, [searchExpanded]);

  useKeymap([
    // Modal-open guard mirrors main's sessions-controls.js handlers —
    // these shortcuts must not focus/mutate background Sessions state
    // while a modal sits on top. openSessionId alone is not enough:
    // the modal-open signal is `state.openModal !== null`.
    {
      key: 'f',
      scope: 'sessions',
      action: openFilter,
      when: () => !getState().openModal && !filterExpanded,
    },
    {
      key: '/',
      scope: 'sessions',
      action: openSearch,
      when: () => !getState().openModal && !searchExpanded,
    },
  ]);
  // NOTE: main does not bind `s` to sort cycling — `s` is the global
  // Settings key. Sort-pill button click is the only UI affordance for
  // cycling sort (unchanged from main's sessions-controls.js).

  const matchCount = searchMatches.length;
  const matchLabel = matchCount > 0
    ? `${searchIndex + 1} / ${matchCount}`
    : '0 / 0';

  const navSearch = (dir: 1 | -1) => {
    if (matchCount === 0) return;
    const next = ((searchIndex + dir) % matchCount + matchCount) % matchCount;
    dispatch({ type: 'SET_SEARCH_MATCHES', matches: searchMatches, index: next });
  };

  const onSortClick = () => {
    if (getState().prefs.sessionsSortOverride) {
      dispatch({ type: 'SET_TABLE_SORT', table: 'sessions', override: null });
    }
    dispatch({ type: 'SET_SORT', key: nextSort(sort) });
  };

  return (
    <div className="sessions-ctrls" id="sessions-ctrls">
      {/* Filter: collapsed shows a funnel button; expanded shows the input.
          With a saved filter term the collapsed button switches to a chip —
          parity with main's sessions-controls.js#collapseFilter: the chip
          carries the current text and a non-interactive × glyph; clicking
          the × clears the filter, clicking elsewhere on the button expands. */}
      {!filterExpanded ? (
        filterText ? (
          <button
            className="ctrl-btn as-chip"
            id="filter-btn"
            type="button"
            aria-label={`Filter: ${filterText}. Press Esc to clear.`}
            onClick={(e) => {
              const target = e.target as HTMLElement;
              if (target.classList.contains('chip-x')) {
                dispatch({ type: 'SET_FILTER', text: '' });
                return;
              }
              openFilter();
            }}
          >
            <span className="chip">filter: {filterText}</span>
            <span className="chip-x" title="Clear">×</span>
          </button>
        ) : (
          <button
            className="ctrl-btn"
            id="filter-btn"
            type="button"
            title="Filter (f)"
            onClick={openFilter}
          >
            <svg className="icon">
              <use href="/static/icons.svg#funnel" />
            </svg>
          </button>
        )
      ) : (
        <input
          type="search"
          className="ctrl-input"
          id="filter-input"
          placeholder="filter project|model…"
          ref={filterInputRef}
          value={filterText}
          onChange={(e) => dispatch({ type: 'SET_FILTER', text: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === 'Enter') closeFilter();
            else if (e.key === 'Escape') closeFilter({ clear: true });
          }}
          onBlur={() => closeFilter()}
        />
      )}

      {/* Search: collapsed shows a magnifier button; expanded (or mobile)
          shows the container with input + match-count + prev/next. */}
      {!showSearchContainer ? (
        <button
          className="ctrl-btn"
          id="search-btn"
          type="button"
          title="Search (/)"
          onClick={openSearch}
        >
          <svg className="icon">
            <use href="/static/icons.svg#magnifier" />
          </svg>
        </button>
      ) : (
        <div className="search-container" id="search-container">
          <input
            type="search"
            className="ctrl-input"
            id="search-input"
            placeholder="search…"
            ref={searchInputRef}
            value={searchText}
            onChange={(e) => dispatch({ type: 'SET_SEARCH', text: e.target.value })}
            onFocus={() => {
              if (!searchExpanded) setSearchExpanded(true);
            }}
            onKeyDown={(e) => {
              // Parity with main's sessions-controls.js#298-300: Enter
              // advances to the next match so keyboard users can walk
              // the match list without leaving the input. Escape cancels
              // and collapses; blur collapses without clearing.
              if (e.key === 'Enter') {
                e.preventDefault();
                navSearch(1);
              } else if (e.key === 'Escape') {
                closeSearch({ clear: true });
              }
            }}
            onBlur={() => closeSearch()}
          />
          {searchChromeShown && (
            <>
              <span className="match-count" id="search-count">
                {matchLabel}
              </span>
              <button
                className="ctrl-btn"
                id="search-prev"
                type="button"
                title="Prev (N)"
                // preventDefault on mousedown keeps focus in the search input,
                // so onBlur doesn't fire and unmount this button before click.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => navSearch(-1)}
              >
                ↑
              </button>
              <button
                className="ctrl-btn"
                id="search-next"
                type="button"
                title="Next (n)"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => navSearch(1)}
              >
                ↓
              </button>
            </>
          )}
        </div>
      )}

      {/* Sort pill */}
      <button
        className="ctrl-btn"
        id="sort-pill"
        type="button"
        title="Cycle sort"
        onClick={onSortClick}
      >
        <svg className="icon">
          <use href="/static/icons.svg#sort-updown" />
        </svg>
        <span className="label">
          sort: {override ? `${override.column} ${override.direction}` : sort}
        </span>
      </button>
    </div>
  );
}
