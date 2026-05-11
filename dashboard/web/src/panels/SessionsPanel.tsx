import { useEffect, useMemo, useSyncExternalStore } from 'react';
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
import { ShareIcon } from '../components/ShareIcon';
import { SESSIONS_COLUMNS } from '../lib/sessionsColumns';
import { fmt } from '../lib/fmt';
import { modelChipClass } from '../lib/model';
import { openShareModal } from '../store/shareSlice';

function costClass(c: number | null | undefined): string {
  if (c == null) return 'cost-low';
  if (c < 0.25) return 'cost-xs';
  if (c < 1.0) return 'cost-low';
  if (c < 3.0) return 'cost-mid';
  return 'cost-high';
}

export function SessionsPanel() {
  const env = useSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  // F4: render the offset suffix once in the column header so each row
  // body stays compact ("YYYY-MM-DD HH:MM" without a per-row "UTC" / "PDT"
  // tail). Build a per-render columns array that overrides the default
  // "Started" label; everything else (compare, defaultDirection) inherits
  // from SESSIONS_COLUMNS so sort behavior is unchanged.
  const columns = useMemo(
    () =>
      SESSIONS_COLUMNS.map((col) =>
        col.id === 'started'
          ? { ...col, label: `Started (${display.offsetLabel})` }
          : col,
      ),
    [display.offsetLabel],
  );
  // Re-render on filter/sort/perPage change so the rendered row list
  // stays in sync with getRenderedRows — those slices feed both the
  // table below and the store's search-match recompute.
  useSyncExternalStore(subscribeStore, () => getState().filterText);
  useSyncExternalStore(subscribeStore, () => getState().sessionsSort);
  useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsPerPage);
  const searchMatches = useSyncExternalStore(subscribeStore, () => getState().searchMatches);
  const searchIndex = useSyncExternalStore(subscribeStore, () => getState().searchIndex);
  const collapsed = useSyncExternalStore(subscribeStore, () => getState().prefs.sessionsCollapsed);
  const sessionsOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.sessionsSortOverride,
  );

  const isMobile = useIsMobile();
  const total = env?.sessions?.total ?? 0;
  const filtered = getRenderedRows();
  // Match indices (as produced by the store's _recomputeSearch) are
  // positions into `filtered` — the exact same array we paint below —
  // so the rendered .search-match rows align with n/N navigation.
  const matchedSessionIds = new Set(
    searchMatches
      .map((i) => filtered[i]?.session_id)
      .filter((s): s is string => !!s),
  );

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
      tabIndex={0}
      role="region"
      aria-label="Recent Sessions panel"
      data-panel-kind="sessions"
      onKeyDown={(e) => {
        // Only fire when the section itself is focused — not when activation
        // bubbles from a control inside SessionsControls (filter/search input,
        // sort pill, search nav buttons). Matches main's focus-handler scope.
        if (e.target !== e.currentTarget) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'session' });
        }
      }}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" style={{ color: 'var(--accent-orange)' }}>
            <use href="/static/icons.svg#clock" />
          </svg>
          <h3 style={{ color: 'var(--accent-orange)' }}>
            Recent Sessions <span className="sub">({total} total)</span>
          </h3>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {!isMobile && <SessionsControls />}
          <ShareIcon
            panel="sessions"
            panelLabel="Sessions"
            triggerId="sessions-panel"
            onClick={() => dispatch(openShareModal('sessions', 'sessions-panel'))}
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
              dispatch({
                type: 'SAVE_PREFS',
                patch: { sessionsCollapsed: !collapsed },
              });
            }}
          >
            <svg className="icon">
              <use href={`/static/icons.svg#${collapsed ? 'chevron-down' : 'chevron-up'}`} />
            </svg>
          </button>
          <PanelGrip />
        </div>
      </div>
      {isMobile && <SessionsControls />}
      <div className="panel-body" id="panel-sessions-body">
        <table className="sess-table">
          <SortableHeader
            columns={columns}
            override={sessionsOverride}
            onChange={(next) =>
              dispatch({ type: 'SET_TABLE_SORT', table: 'sessions', override: next })
            }
          />
          <tbody id="sess-rows">
            {filtered.map((r) => {
              const isMatch = r.session_id ? matchedSessionIds.has(r.session_id) : false;
              const chipCls = modelChipClass(r.model);
              const cCls = costClass(r.cost_usd ?? null);
              const chipLabel = r.model ? `Filter by ${r.model}` : 'Filter by model';
              return (
                <tr
                  key={r.session_id || `${r.started_utc}-${r.model}`}
                  className={'session-row' + (isMatch ? ' search-match' : '')}
                  data-session-id={r.session_id}
                  onClick={() =>
                    r.session_id &&
                    dispatch({
                      type: 'OPEN_MODAL',
                      kind: 'session',
                      sessionId: r.session_id,
                    })
                  }
                >
                  <td className="started">{fmt.startedShort(r.started_utc, ctx, { noSuffix: true })}</td>
                  <td>{r.duration_min}m</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <span
                      className={`chip model-chip ${chipCls}`}
                      role="button"
                      tabIndex={-1}
                      aria-label={chipLabel}
                      onClick={() => dispatch({ type: 'SET_FILTER', text: r.model })}
                    >
                      {r.model}
                    </span>
                  </td>
                  <td className="project">{r.project}</td>
                  <td className={`num ${cCls}`}>{fmt.usd2(r.cost_usd)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
