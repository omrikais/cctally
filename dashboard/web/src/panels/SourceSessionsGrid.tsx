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
import { fmt } from '../lib/fmt';
import { modelChipClass } from '../lib/model';
import { HighlightText } from '../lib/highlightText';
import { rovingAction } from '../lib/sessionsRovingKeyboard';
import { resolveSourceView } from '../store/sourceView';
import { gatePanel } from '../lib/sourceGating';
import { sourceSessionsColumns } from '../lib/sourceSessionsColumns';
import { SourceChip, DegradedChip } from './sourcePanel';
import type { SessionDisplayRow } from '../lib/sourceRows';
import type { DashboardSelection } from '../types/envelope';

// #294 S5 §6.3 — the source-aware Sessions grid (Codex + All). The Claude
// Sessions panel keeps its own byte-identical grid (ClaudeSessionsPanel in
// SessionsPanel.tsx); this renders the provider-native display-row grid with the
// enumerated per-source bindings: columns title=label / recency=last_activity
// (default sort desc) / models chips / the five token cells / cost; sortable by
// recency, cost, total tokens, label; filter `f` + search `/` over the label +
// models haystack; collapse `c` respected; the #299 roving-tabindex grid-lite
// interaction carries over. In All mode every row shows a source chip and the two
// providers' rows interleave by the shared recency comparator (never merged).

// The row's focusable per-row control (the detail-open title button), matching
// the #299 CONTROL_SELECTOR contract for the roving grid.
const SOURCE_CONTROL_SELECTOR = '.source-detail-open';

function tokenCell(r: SessionDisplayRow, field: 'input' | 'cachedInput' | 'output' | 'reasoning' | 'total'): string {
  if (r.tokens.kind !== 'codex') return '—';
  return fmt.tokens(r.tokens[field]);
}

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
  useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsPerPage);
  const sourceSort = useSyncExternalStore(subscribeStore, () => getState().sourceSessionsSort);
  const searchMatches = useSyncExternalStore(subscribeStore, () => getState().searchMatches);
  const searchIndex = useSyncExternalStore(subscribeStore, () => getState().searchIndex);
  const searchText = useSyncExternalStore(subscribeStore, () => getState().searchText);
  const collapsed = useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsCollapsed);

  const isMobile = useIsMobile();
  const isAll = activeSource === 'all';
  const view = resolveSourceView(env, activeSource);
  const gate = gatePanel(view, 'sessions');
  const rows = getRenderedSourceRows();
  const columns = sourceSessionsColumns({ includeSource: isAll });
  const emptyLabel = isAll ? 'No sessions yet.' : 'No Codex sessions yet.';
  const tableTestId = isAll ? 'source-sessions-table' : 'codex-sessions-table';

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
    // real display:none exclusion). Mirrors ClaudeSessionsPanel.
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
        dispatch({ type: 'OPEN_SOURCE_DETAIL', source: src, resource: 'session', key });
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
  }, []);

  const showSkeleton = gate.mode === 'skeleton';

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
            Recent Sessions <span className="sub">{showSkeleton ? '(loading)' : `(${rows.length} shown)`}</span>
          </h2>
          {gate.mode === 'degraded' && <DegradedChip gate={gate} />}
        </div>
        <div className="panel-header-actions">
          {!isMobile && <SessionsControls />}
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
              override={sourceSort}
              grid
              onChange={(next) => dispatch({ type: 'SET_SOURCE_SESSIONS_SORT', override: next })}
            />
            <tbody id="sess-rows" role="rowgroup" onKeyDown={onRowsKeyDown}>
              {rows.map((r, i) => {
                const isMatch = matchedIdx.has(i);
                const isCurrent = i === currentIdx;
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
                    data-detail-source={r.source}
                    data-detail-key={r.key}
                    onClick={() =>
                      dispatch({ type: 'OPEN_SOURCE_DETAIL', source: r.source, resource: 'session', key: r.key })
                    }
                  >
                    {isAll && (
                      <td className="src" role="gridcell" onClick={(e) => e.stopPropagation()}>
                        <SourceChip source={r.source} />
                      </td>
                    )}
                    <td className="session" role="gridcell" title={r.title}>
                      <button
                        type="button"
                        className="source-detail-open sess-open-title"
                        tabIndex={-1}
                        aria-label={`Open ${r.source} session details: ${r.title}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          dispatch({ type: 'OPEN_SOURCE_DETAIL', source: r.source, resource: 'session', key: r.key });
                        }}
                      >
                        <HighlightText text={r.title} query={searchText} />
                      </button>
                    </td>
                    <td className="recency" role="gridcell">{recency}</td>
                    <td className="models" role="gridcell" onClick={(e) => e.stopPropagation()}>
                      {r.models.length === 0 ? (
                        <span className="src-model-empty" aria-hidden="true">—</span>
                      ) : (
                        r.models.map((m) => (
                          <span key={m} className={`chip model-chip ${modelChipClass(m)}`}>
                            <HighlightText text={m} query={searchText} />
                          </span>
                        ))
                      )}
                    </td>
                    <td className="num tok-input" role="gridcell">{tokenCell(r, 'input')}</td>
                    <td className="num tok-cached" role="gridcell">{tokenCell(r, 'cachedInput')}</td>
                    <td className="num tok-output" role="gridcell">{tokenCell(r, 'output')}</td>
                    <td className="num tok-reasoning" role="gridcell">{tokenCell(r, 'reasoning')}</td>
                    <td className="num tok-total" role="gridcell">{tokenCell(r, 'total')}</td>
                    <td className="num cost" role="gridcell">{fmt.usd2(r.costUsd)}</td>
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
