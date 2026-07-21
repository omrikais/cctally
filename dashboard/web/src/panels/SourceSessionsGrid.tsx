import { useCallback, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { useIsMobile } from '../hooks/useIsMobile';
import {
  dispatch,
  getRenderedSourceRows,
  getState,
  subscribeStore,
} from '../store/store';
import { SessionsControls } from '../components/SessionsControls';
import { SortableHeader } from '../components/SortableHeader';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { fmt } from '../lib/fmt';
import { modelChipClass, modelChipStyle } from '../lib/model';
import { HighlightText } from '../lib/highlightText';
import { rovingAction } from '../lib/sessionsRovingKeyboard';
import { resolveSourceView } from '../store/sourceView';
import { gateSessions } from '../lib/sourceGating';
import { sourceSessionsColumns } from '../lib/sourceSessionsColumns';
import { collectSourceSessionRows, type SessionDisplayRow } from '../lib/sourceRows';
import { costClass } from '../lib/cost';
import { transcriptsEnabled } from '../lib/transcripts';
import { SourceChip, DegradedChip } from './sourcePanel';
import type { DashboardSelection } from '../types/envelope';
import { openShareModal } from '../store/shareSlice';
import { legacyClaudeConversationRef } from '../types/conversation';

// Provider-neutral Sessions grid for Claude, Codex, and All. It renders the
// canonical provider-adapted display rows behind the canonical columns: Started /
// Duration / Model / Project / Cache hit / Cost. Native token counters remain
// available in qualified detail. Filter `f` + search `/` cover the label +
// models haystack; collapse `c` respected; the #299 roving-tabindex grid-lite
// interaction carries over. In All mode every row shows a source chip and the two
// providers' rows interleave by the shared recency comparator (never merged).

// The row's focusable per-row control (the detail-open title button), matching
// the #299 CONTROL_SELECTOR contract for the roving grid.
const SOURCE_CONTROL_SELECTOR = '.sess-open-conv, .source-detail-open, .model-chip, .project-cell-link';

export function SourceSessionsGrid() {
  const activeSource = useSyncExternalStore(
    subscribeStore,
    () => getState().activeSource,
  ) as DashboardSelection;
  const env = useSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // Re-render on the slices that feed getRenderedSourceRows so the painted rows
  // stay in sync with the store's search-match recompute.
  useSyncExternalStore(subscribeStore, () => getState().filterText);
  useSyncExternalStore(subscribeStore, () => getState().sessionsSort);
  useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsPerPage);
  const sourceSort = useSyncExternalStore(subscribeStore, () => getState().sourceSessionsSort);
  const claudeSort = useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsSortOverride);
  const searchMatches = useSyncExternalStore(subscribeStore, () => getState().searchMatches);
  const searchIndex = useSyncExternalStore(subscribeStore, () => getState().searchIndex);
  const searchText = useSyncExternalStore(subscribeStore, () => getState().searchText);
  const collapsed = useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsCollapsed);

  const isMobile = useIsMobile();
  const isAll = activeSource === 'all';
  const view = resolveSourceView(env, activeSource);
  const gate = gateSessions(view);
  const rows = getRenderedSourceRows();
  const allRows = collectSourceSessionRows(view);
  const models = new Set(allRows.flatMap((row) => row.models));
  const oneModel = models.size <= 1;
  const singleModel = models.size === 1 ? [...models][0] : null;
  const columns = sourceSessionsColumns({ includeSource: isAll, oneModel });
  const sortOverride = activeSource === 'claude' ? claudeSort : sourceSort;
  const transcriptsOn = transcriptsEnabled(env);
  const total = activeSource === 'claude'
    ? env?.sessions?.total ?? rows.length
    : activeSource === 'codex'
      ? (view.entry?.data as { sessions?: { total_sessions?: number } } | null)?.sessions?.total_sessions ?? rows.length
      : allRows.length;
  const emptyLabel = activeSource === 'claude' ? 'No sessions yet.' : isAll ? 'No sessions yet.' : 'No Codex sessions yet.';
  const tableTestId = activeSource === 'claude' ? 'claude-sessions-table' : isAll ? 'source-sessions-table' : 'codex-sessions-table';

  // #299 roving-tabindex: the single body tab stop is the ROW keyed by identity
  // (the opaque source key), surviving re-sort/re-filter; falls back to the
  // current search match, else row 0.
  const [activeRowKey, setActiveRowKey] = useState<string | null>(null);
  const searchCurrentIdx = searchIndex >= 0 ? searchMatches[searchIndex] : -1;
  const activeIdx = activeRowKey ? rows.findIndex((r) => r.key === activeRowKey) : -1;
  const tabStopIdx =
    rows.length === 0 ? -1 : activeIdx >= 0 ? activeIdx : searchCurrentIdx >= 0 ? searchCurrentIdx : 0;
  // Search-match indices are positions into `rows` (= getRenderedSourceRows), the
  // exact array painted below, so `.search-match` + n/N align with the DOM.
  const matchedIdx = new Set(searchMatches);
  const currentIdx = searchCurrentIdx;

  const openSession = (row: SessionDisplayRow) => {
    if (!row.key) return;
    if (activeSource === 'claude' && row.source === 'claude') {
      dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: row.key });
    } else {
      dispatch({ type: 'OPEN_SOURCE_DETAIL', source: row.source, resource: 'session', key: row.key });
    }
  };

  const onRowsKeyDown = useCallback((e: React.KeyboardEvent<HTMLTableSectionElement>) => {
    if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    const targetEl = e.target as HTMLElement;
    const tr = targetEl.closest('tr.source-session-row') as HTMLElement | null;
    if (!tr) return;
    const tbody = e.currentTarget;
    const onRow = targetEl === tr;
    const allControls = Array.from(tr.querySelectorAll<HTMLElement>(SOURCE_CONTROL_SELECTOR));
    // Confine to on-screen controls (jsdom has no layout → offsetParent is
    // always null, so fall back to the full set — the ui-qa gate covers the
    // real display:none exclusion).
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
      const src = tr.dataset.detailSource;
      const key = tr.dataset.detailKey;
      if ((src === 'claude' || src === 'codex') && key) {
        if (activeSource === 'claude' && src === 'claude') {
          dispatch({ type: 'OPEN_MODAL', kind: 'session', sessionId: key });
        } else {
          dispatch({ type: 'OPEN_SOURCE_DETAIL', source: src, resource: 'session', key });
        }
      }
      return;
    }
    const len = tbody.children.length;
    if (len === 0) return;
    const curIdx = Number(tr.dataset.rowIndex);
    const target =
      action.to === 'next' ? Math.min(curIdx + 1, len - 1)
      : action.to === 'prev' ? Math.max(curIdx - 1, 0)
      : action.to === 'first' ? 0
      : len - 1;
    const targetTr = tbody.children[target] as HTMLElement | undefined;
    if (!targetTr) return;
    targetTr.focus();
    targetTr.scrollIntoView({ block: 'nearest' });
    setActiveRowKey(targetTr.dataset.rowKey ?? null);
  }, [activeSource]);

  const showSkeleton = gate.mode === 'skeleton'
    || (activeSource === 'claude' && !!env?.hydrating && rows.length === 0);

  return (
    <section
      className={'panel accent-orange' + (collapsed ? ' sessions-collapsed' : '')}
      id="panel-sessions"
      role="region"
      aria-label="Recent Sessions panel"
      data-panel-kind="sessions"
      data-source={activeSource}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div className="panel-title-wrap" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#clock" />
          </svg>
          <h2>
            Recent Sessions <span className="sub">{showSkeleton ? '(loading)' : activeSource === 'claude' ? `(${total} total)` : `(${rows.length} shown)`}</span>
          </h2>
          {singleModel && (
            <span className="sess-model-caption" title={`All sessions use ${singleModel}`}>
              <span
                className={`ms-dot ${modelChipClass(singleModel)}`}
                style={modelChipStyle(singleModel)}
                aria-hidden="true"
              />
              all · {singleModel}
            </span>
          )}
          {gate.mode === 'degraded' && <DegradedChip gate={gate} />}
        </div>
        <div className="panel-header-actions">
          {!isMobile && <SessionsControls />}
          <ShareIcon
            panel="sessions"
            panelLabel="Sessions"
            triggerId="sessions-panel"
            onClick={() => dispatch(openShareModal('sessions', 'sessions-panel'))}
          />
          <ExpandButton
            label="Sessions"
            disabled={rows.length === 0}
            onOpen={() => {
              const row = rows[0];
              if (row) openSession(row);
            }}
          />
          <button
            type="button"
            className="panel-collapse-toggle"
            aria-expanded={!collapsed}
            aria-controls="panel-sessions-body"
            aria-label={collapsed ? 'Expand Recent Sessions' : 'Collapse Recent Sessions'}
            title={collapsed ? 'Expand (c)' : 'Collapse (c)'}
            onClick={(e) => {
              e.stopPropagation();
              dispatch({ type: 'SAVE_PREFS', patch: { sessionsCollapsed: !collapsed } });
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
        {showSkeleton ? (
          <PanelSkeleton lines={4} />
        ) : rows.length === 0 ? (
          <div className="panel-empty" data-testid="source-sessions-empty">{emptyLabel}</div>
        ) : (
          <table className="sess-table source-sess-table" role="grid" data-testid={tableTestId}>
            <SortableHeader
              columns={columns}
              override={sortOverride}
              grid
              onChange={(next) => dispatch(activeSource === 'claude'
                ? { type: 'SET_TABLE_SORT', table: 'sessions', override: next }
                : { type: 'SET_SOURCE_SESSIONS_SORT', override: next })}
            />
            <tbody id="sess-rows" role="rowgroup" onKeyDown={onRowsKeyDown}>
              {rows.map((r, i) => {
                const isMatch = matchedIdx.has(i);
                const isCurrent = i === currentIdx;
                const sessionTitle = r.title || '—';
                const recency = r.recencyUtc
                  ? fmt.startedShort(r.recencyUtc, ctx, { noSuffix: true })
                  : '—';
                return (
                  <tr
                    key={r.key}
                    className={
                      'source-session-row session-row'
                      + (isMatch ? ' search-match' : '')
                      + (isCurrent ? ' search-match-current' : '')
                    }
                    role="row"
                    tabIndex={i === tabStopIdx ? 0 : -1}
                    aria-current={isCurrent ? 'true' : undefined}
                    data-row-index={i}
                    data-row-key={r.key}
                    data-session-id={activeSource === 'claude' ? r.key : undefined}
                    data-detail-source={r.source}
                    data-detail-key={r.key}
                    onClick={() => openSession(r)}
                  >
                    <td className="started recency" role="gridcell">
                      {transcriptsOn && r.source === 'claude' && r.key && (
                        <button
                          type="button"
                          className="sess-open-conv"
                          tabIndex={-1}
                          title="Open conversation"
                          aria-label="Open conversation"
                          onClick={(e) => {
                            e.stopPropagation();
                            dispatch({ type: 'OPEN_CONVERSATION', conversationRef: legacyClaudeConversationRef(r.key) });
                          }}
                        >
                          <svg className="icon" aria-hidden="true"><use href="/static/icons.svg#file-text" /></svg>
                        </button>
                      )}
                      <HighlightText text={recency} query={searchText} />
                      <span className="dur-fold"> · <HighlightText text={r.durationMin == null ? '—' : `${r.durationMin}m`} query={searchText} /></span>
                    </td>
                    <td className="dur" role="gridcell"><HighlightText text={r.durationMin == null ? '—' : `${r.durationMin}m`} query={searchText} /></td>
                    {!oneModel && <td className="model model-chip-cell models" role="gridcell" onClick={(e) => e.stopPropagation()}>
                      {r.models.length === 0 ? (
                        <span className="src-model-empty" aria-hidden="true">—</span>
                      ) : (
                        r.models.map((m) => (
                          <button key={m} type="button" tabIndex={-1} className={`chip model-chip ${modelChipClass(m)}`}
                            style={modelChipStyle(m)}
                            aria-label={`Filter by ${m}`} onClick={() => dispatch({ type: 'SET_FILTER', text: m })}>
                            <HighlightText text={m} query={searchText} />
                          </button>
                        ))
                      )}
                    </td>}
                    <td className="session" role="gridcell" title={r.title || undefined}>
                      {r.key ? <button
                        type="button"
                        className="source-detail-open sess-open-title"
                        tabIndex={-1}
                        aria-label={activeSource === 'claude'
                          ? r.title ? `Open session details: ${r.title}` : `Open session details, started ${recency}`
                          : `Open ${r.source} session details: ${sessionTitle}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          openSession(r);
                        }}
                      >
                        {isAll && <SourceChip source={r.source} />}
                        {r.title ? <HighlightText text={r.title} query={searchText} /> : <span className="sess-title-empty" aria-hidden="true">—</span>}
                      </button> : r.title ? <HighlightText text={r.title} query={searchText} /> : <span className="sess-title-empty" aria-hidden="true">—</span>}
                    </td>
                    <td className="project" role="gridcell">
                      {r.projectKey && r.projectKey !== '(unknown)' ? (
                        <button type="button" className="project-cell-link" tabIndex={-1} title={r.project}
                          aria-label={activeSource === 'claude' ? `Open Projects modal for ${r.projectKey}` : `Open ${r.source} project details: ${r.project}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (activeSource === 'claude') dispatch({ type: 'OPEN_MODAL', kind: 'projects', projectKey: r.projectKey ?? undefined });
                            else dispatch({ type: 'OPEN_SOURCE_DETAIL', source: r.source, resource: 'project', key: r.projectKey! });
                          }}>
                          <HighlightText text={r.project} query={searchText} />
                        </button>
                      ) : <span title="Project still resolving"><HighlightText text={r.project} query={searchText} /></span>}
                    </td>
                    <td className="num cache" role="gridcell">{r.cacheHitPct == null ? '—' : fmt.pct0(r.cacheHitPct)}</td>
                    <td className={`num cost ${costClass(r.costUsd)}`} role="gridcell"><HighlightText text={fmt.usd2(r.costUsd)} query={searchText} /></td>
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
