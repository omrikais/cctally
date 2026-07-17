import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useIsMobile } from '../hooks/useIsMobile';
import {
  dispatch,
  getRenderedRows,
  getState,
  subscribeStore,
} from '../store/store';
import { SessionsControls } from '../components/SessionsControls';
import { SortableHeader } from '../components/SortableHeader';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { openMostRecentSessionModal } from '../store/actions';
import { sessionsColumns } from '../lib/sessionsColumns';
import { fmt } from '../lib/fmt';
import { modelChipClass } from '../lib/model';
import { singleModelLabel } from '../lib/sessionsModel';
import { costClass } from '../lib/cost';
import { transcriptsEnabled } from '../lib/transcripts';
import { openShareModal } from '../store/shareSlice';
import { HighlightText } from '../lib/highlightText';
import { rovingAction } from '../lib/sessionsRovingKeyboard';
import { useActiveSource } from './sourcePanel';
import { SourceSessionsGrid } from './SourceSessionsGrid';
import type { SessionRow } from '../types/envelope';

// #299 — the row's focusable per-row controls, in DOM (= visual L→R) order.
const CONTROL_SELECTOR =
  '.sess-open-conv, .chip.model-chip, .sess-open-title, .project-cell-link';

// #299 — stable per-row identity for roving-focus tracking; matches the React key.
const rowKey = (r: SessionRow) => r.session_id || `${r.started_utc}-${r.model}`;

// #294 S5 §6.3 — source-aware wrapper. Claude keeps the full sortable/filterable
// grid (byte-identical, via getRenderedRows + legacy SessionRow columns). Codex
// and All render through the source-native SourceSessionsGrid (provider display
// rows: label / last_activity / models / the five token cells / cost, sortable
// by recency·label·total·cost, filter+search over label+models, roving grid);
// in All the two providers' rows interleave by recency with a per-row source
// chip (no merging of labels or native keys). The Sessions surface is NOT the
// generic provider-labeled-sections shell — All is a single interleaved grid.
export function SessionsPanel() {
  const activeSource = useActiveSource();
  if (activeSource === 'claude') return <ClaudeSessionsPanel />;
  return <SourceSessionsGrid />;
}

function ClaudeSessionsPanel() {
  const env = useSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // Re-render on filter/sort/perPage change so the rendered row list
  // stays in sync with getRenderedRows — those slices feed both the
  // table below and the store's search-match recompute.
  useSyncExternalStore(subscribeStore, () => getState().filterText);
  useSyncExternalStore(subscribeStore, () => getState().sessionsSort);
  useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsPerPage);
  const searchMatches = useSyncExternalStore(subscribeStore, () => getState().searchMatches);
  const searchIndex = useSyncExternalStore(subscribeStore, () => getState().searchIndex);
  // SESS-2: the live needle drives the in-cell <mark> highlighting. The row
  // haystack is space-joined across cells and includes project_key, so a query
  // that spans cells ("foo 5m") or matches only project_key can row-match
  // (wash + aria-current) without any in-cell mark — an accepted limitation.
  const searchText = useSyncExternalStore(subscribeStore, () => getState().searchText);
  const collapsed = useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsCollapsed);
  const sessionsOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.sessionsSortOverride,
  );

  const isMobile = useIsMobile();
  const total = env?.sessions?.total ?? 0;
  // C3 (#249): collapse the redundant per-row model column when the WHOLE
  // session set is one model. Computed over env.sessions.rows (NOT the
  // filtered/paged `filtered` below) so a project-filtered single-model view
  // keeps its meaningful model-filter chips.
  const allSessionRows = env?.sessions?.rows ?? [];
  const oneModel = singleModelLabel(allSessionRows);
  const oneModelRaw = oneModel ? (allSessionRows[0]?.model ?? '') : '';
  // Conversation viewer (spec §4 entry). The per-row "open conversation"
  // affordance is shown only when transcripts are enabled for THIS
  // request (loopback, or LAN with dashboard.expose_transcripts). Absent
  // / false envelope flag → hide the button entirely (fail closed via the
  // shared `transcriptsEnabled` selector) so the feature stays invisible
  // for users who can't reach the transcript routes.
  const transcriptsOn = transcriptsEnabled(env);
  // S3 (#264): build the render columns from the single builder source.
  // The Model column is dropped entirely when single-model (SESS-1 — the
  // caption is the signpost); Session + Cache are always present. F4:
  // override the Started label to carry the tz offset once in the header so
  // each row body stays compact ("HH:MM" without a per-row "UTC"/"PDT"
  // tail); everything else (compare, defaultDirection) inherits from the
  // builder so sort behavior is unchanged.
  const columns = useMemo(
    () =>
      sessionsColumns({ oneModel: !!oneModel, transcriptsOn }).map((col) =>
        col.id === 'started'
          ? { ...col, label: `Started (${display.offsetLabel})` }
          : col,
      ),
    [oneModel, transcriptsOn, display.offsetLabel],
  );
  const filtered = getRenderedRows();
  // #278 Theme A (ui-qa P3): header sub-label predicate — while hydrating with
  // no rows yet the sub-label reads "(loading)" instead of the misleading
  // "(0 total)" final-state copy (mirrors CacheReportPanel's header). Same
  // hydrating+empty condition the body's skeleton branch uses below.
  const hydratingEmpty = !!env?.hydrating && filtered.length === 0;
  // Match indices (as produced by the store's _recomputeSearch) are
  // positions into `filtered` — the exact same array we paint below —
  // so the rendered .search-match rows align with n/N navigation.
  const matchedSessionIds = new Set(
    searchMatches
      .map((i) => filtered[i]?.session_id)
      .filter((s): s is string => !!s),
  );
  // SESS-2: the current match (n/N cursor) — same index math as the
  // scroll-sync effect below — gets the stronger `search-match-current`
  // emphasis + aria-current. Null when there are no matches / out of range.
  const currentSessionId =
    searchIndex >= 0 ? filtered[searchMatches[searchIndex]]?.session_id ?? null : null;

  // #299 roving-tabindex state: the single body tab stop is the ROW keyed by
  // `activeRowKey` (same identity as the React key). The delegated keydown
  // handler (below) sets it on Up/Down/Home/End; between keystrokes it survives
  // re-sort / re-filter because it is keyed by identity, not index.
  const [activeRowKey, setActiveRowKey] = useState<string | null>(null);
  // #299 default-landing (P3-G): the current `/`-search match
  // (searchMatches[searchIndex], already a rendered index) if one exists, else
  // row 0; -1 (no body stop) only when the list is empty. Re-sort/re-filter or a
  // search never strands the single tab stop — worst case it falls back to row 0.
  const searchCurrentIdx = searchIndex >= 0 ? searchMatches[searchIndex] : -1;
  const activeIdx = activeRowKey
    ? filtered.findIndex((r) => rowKey(r) === activeRowKey)
    : -1;
  const tabStopIdx =
    filtered.length === 0
      ? -1
      : activeIdx >= 0
        ? activeIdx
        : searchCurrentIdx >= 0
          ? searchCurrentIdx
          : 0;

  // #299 — one delegated keydown on <tbody>. Every keydown from a row or a
  // nested button bubbles here; context is derived from the event (no separate
  // cell-focus state → no focus/state desync). Stable identity: reads live DOM
  // via e.currentTarget/e.target and only touches the module-stable `dispatch`
  // and `setActiveRowKey`, so [] deps are correct.
  const onRowsKeyDown = useCallback((e: React.KeyboardEvent<HTMLTableSectionElement>) => {
    // Modifier bail (folded P2-C) — FIRST line: let Shift+Arrow bubble to
    // PanelHost's panel reorder, and never swallow OS/browser chords.
    if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    const targetEl = e.target as HTMLElement;
    const tr = targetEl.closest('tr.session-row') as HTMLElement | null;
    if (!tr) return;
    const tbody = e.currentTarget;
    const onRow = targetEl === tr;
    const allControls = Array.from(tr.querySelectorAll<HTMLElement>(CONTROL_SELECTOR));
    // Confine the cell axis to on-screen controls: `offsetParent === null` drops
    // a display:none control (the mobile-hidden Project column). jsdom has no
    // layout (offsetParent is always null), so fall back to the full set when the
    // filter removes everything — in a real browser the always-present title
    // button keeps `visible` non-empty, so this fallback only engages under
    // jsdom. The real display:none exclusion is browser-verified at the ui-qa gate.
    const visible = allControls.filter((el) => el.offsetParent !== null);
    const controls = visible.length > 0 ? visible : allControls;
    const cellIdx = onRow ? -1 : controls.indexOf(targetEl);
    const action = rovingAction(e.key, { onRow, cellIdx, cellCount: controls.length });
    if (!action) return;
    e.preventDefault();
    e.stopPropagation();
    if (action.kind === 'cell') {
      controls[action.to]?.focus();
      return;
    }
    if (action.kind === 'rowFocus') {
      tr.focus();
      return;
    }
    if (action.kind === 'activateRow') {
      const sid = tr.dataset.sessionId;
      if (sid) dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: sid });
      return;
    }
    // action.kind === 'row' — resolve + clamp against the rendered rows (no wrap).
    const len = tbody.children.length;
    if (len === 0) return;
    const curIdx = Number(tr.dataset.rowIndex);
    const target =
      action.to === 'next' ? Math.min(curIdx + 1, len - 1)
      : action.to === 'prev' ? Math.max(curIdx - 1, 0)
      : action.to === 'first' ? 0
      : len - 1; // 'last'
    const targetTr = tbody.children[target] as HTMLElement | undefined;
    if (!targetTr) return;
    targetTr.focus();
    targetTr.scrollIntoView({ block: 'nearest' });
    setActiveRowKey(targetTr.dataset.rowKey ?? null);
  }, []);

  useEffect(() => {
    if (searchIndex < 0) return;
    const renderedIdx = searchMatches[searchIndex];
    const sid = filtered[renderedIdx]?.session_id;
    if (!sid) return;
    const el = document.querySelector(
      `[data-session-id="${CSS.escape(sid)}"]`,
    );
    (el as HTMLElement | null)?.scrollIntoView({ block: 'nearest' });
  }, [searchIndex, searchMatches, filtered]);

  return (
    <section
      className={'panel accent-orange' + (collapsed ? ' sessions-collapsed' : '')}
      id="panel-sessions"
      role="region"
      aria-label="Recent Sessions panel"
      data-panel-kind="sessions"
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div className="panel-title-wrap" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#clock" />
          </svg>
          <h2>
            Recent Sessions <span className="sub">{hydratingEmpty ? '(loading)' : `(${total} total)`}</span>
          </h2>
          {oneModel && (
            <span className="sess-model-caption" title={`All sessions use ${oneModelRaw}`}>
              <span className={`ms-dot ${modelChipClass(oneModelRaw)}`} aria-hidden="true" />
              all · {oneModel}
            </span>
          )}
        </div>
        <div className="panel-header-actions">
          {!isMobile && <SessionsControls />}
          <ShareIcon
            panel="sessions"
            panelLabel="Sessions"
            triggerId="sessions-panel"
            onClick={() => dispatch(openShareModal('sessions', 'sessions-panel'))}
          />
          <ExpandButton label="Sessions" onOpen={openMostRecentSessionModal} />
          <button
            type="button"
            className="panel-collapse-toggle"
            aria-expanded={!collapsed}
            aria-controls="panel-sessions-body"
            aria-label={collapsed ? 'Expand Recent Sessions' : 'Collapse Recent Sessions'}
            title={collapsed ? 'Expand (c)' : 'Collapse (c)'}
            onClick={(e) => {
              e.stopPropagation();
              dispatch({
                type: 'SAVE_PREFS',
                patch: { sessionsCollapsed: !collapsed },
              });
            }}
          >
            <svg className="icon" aria-hidden="true">
              <use href={`/static/icons.svg#${collapsed ? 'chevron-down' : 'chevron-up'}`} />
            </svg>
          </button>
          <PanelGrip />
        </div>
      </div>
      {isMobile && <SessionsControls />}
      <div className="panel-body panel-body--scroll" id="panel-sessions-body">
        {env?.hydrating && filtered.length === 0 ? (
          // #278 §1.4: the cheap first-paint seed hasn't built sessions yet;
          // show a loading skeleton rather than an empty table shell.
          <PanelSkeleton lines={4} />
        ) : (
        <table className="sess-table" role="grid">
          <SortableHeader
            columns={columns}
            override={sessionsOverride}
            grid
            onChange={(next) =>
              dispatch({ type: 'SET_TABLE_SORT', table: 'sessions', override: next })
            }
          />
          <tbody id="sess-rows" role="rowgroup" onKeyDown={onRowsKeyDown}>
            {filtered.map((r, i) => {
              const isMatch = r.session_id ? matchedSessionIds.has(r.session_id) : false;
              const isCurrent = !!r.session_id && r.session_id === currentSessionId;
              const chipCls = modelChipClass(r.model);
              const cCls = costClass(r.cost_usd ?? null);
              const chipLabel = r.model ? `Filter by ${r.model}` : 'Filter by model';
              // Computed once: reused by the Started cell, the Started-cell
              // folded duration (SESS-1), and the title button's empty-title
              // fallback aria-label (A11Y-2).
              const startedShort = fmt.startedShort(r.started_utc, ctx, { noSuffix: true });
              const titleContent = r.title ? (
                <HighlightText text={r.title} query={searchText} />
              ) : (
                <span className="sess-title-empty" aria-hidden="true">—</span>
              );
              return (
                <tr
                  key={r.session_id || `${r.started_utc}-${r.model}`}
                  className={
                    'session-row'
                    + (isMatch ? ' search-match' : '')
                    + (isCurrent ? ' search-match-current' : '')
                  }
                  role="row"
                  tabIndex={i === tabStopIdx ? 0 : -1}
                  aria-current={isCurrent ? 'true' : undefined}
                  data-session-id={r.session_id}
                  data-row-index={i}
                  data-row-key={rowKey(r)}
                  onClick={() =>
                    r.session_id &&
                    dispatch({
                      type: 'OPEN_MODAL',
                      kind: 'session',
                      sessionId: r.session_id,
                    })
                  }
                >
                  <td className="started" role="gridcell">
                    {transcriptsOn && r.session_id && (
                      <button
                        type="button"
                        className="sess-open-conv"
                        tabIndex={-1}
                        title="Open conversation"
                        aria-label="Open conversation"
                        onClick={(e) => {
                          // stopPropagation so the enclosing <tr>'s
                          // session-modal click handler doesn't ALSO fire.
                          e.stopPropagation();
                          dispatch({
                            type: 'OPEN_CONVERSATION',
                            sessionId: r.session_id,
                          });
                        }}
                      >
                        <svg className="icon" aria-hidden="true">
                          <use href="/static/icons.svg#file-text" />
                        </svg>
                      </button>
                    )}
                    <HighlightText text={startedShort} query={searchText} />
                    <span className="dur-fold"> · <HighlightText text={`${r.duration_min}m`} query={searchText} /></span>
                  </td>
                  <td className="dur" role="gridcell"><HighlightText text={`${r.duration_min}m`} query={searchText} /></td>
                  {!oneModel && (
                    <td className="model model-chip-cell" role="gridcell" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        className={`chip model-chip ${chipCls}`}
                        tabIndex={-1}
                        aria-label={chipLabel}
                        onClick={() => dispatch({ type: 'SET_FILTER', text: r.model })}
                      >
                        <HighlightText text={r.model} query={searchText} />
                      </button>
                    </td>
                  )}
                  <td className="session" role="gridcell" title={r.title ?? undefined}>
                    {r.session_id ? (
                      <button
                        type="button"
                        className="sess-open-title"
                        tabIndex={-1}
                        aria-label={
                          r.title
                            ? `Open session details: ${r.title}`
                            : `Open session details, started ${startedShort}`
                        }
                        onClick={(e) => {
                          // stopPropagation so the enclosing <tr> onClick
                          // doesn't ALSO dispatch (single OPEN_MODAL).
                          e.stopPropagation();
                          dispatch({
                            type: 'OPEN_MODAL',
                            kind: 'session',
                            sessionId: r.session_id!,
                          });
                        }}
                      >
                        {titleContent}
                      </button>
                    ) : (
                      titleContent
                    )}
                  </td>
                  <td className="project" role="gridcell">
                    {r.project_key && r.project_key !== '(unknown)' ? (
                      <button
                        type="button"
                        className="project-cell-link"
                        tabIndex={-1}
                        title={r.project}
                        aria-label={`Open Projects modal for ${r.project_key}`}
                        onClick={(e) => {
                          // stopPropagation so the enclosing <tr>'s
                          // session-modal click handler doesn't ALSO fire.
                          e.stopPropagation();
                          dispatch({
                            type: 'OPEN_MODAL',
                            kind: 'projects',
                            projectKey: r.project_key ?? undefined,
                          });
                        }}
                      >
                        <HighlightText text={r.project} query={searchText} />
                      </button>
                    ) : (
                      // Null project_key (session_files row not yet
                      // ingested) OR literal '(unknown)' bucket — render
                      // plain text. Per spec §4.1: "When project_key is
                      // null or (unknown), the cell renders plain text
                      // (not clickable) with tooltip 'Project still
                      // resolving'."
                      <span title="Project still resolving">
                        <HighlightText text={r.project} query={searchText} />
                      </span>
                    )}
                  </td>
                  <td className="num cache" role="gridcell">
                    {r.cache_hit_pct == null ? '—' : fmt.pct0(r.cache_hit_pct)}
                  </td>
                  <td className={`num ${cCls}`} role="gridcell">
                    <HighlightText text={fmt.usd2(r.cost_usd)} query={searchText} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        )}
      </div>
    </section>
  );
}
