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
import { singleModelLabel } from '../lib/sessionsModel';
import { costClass } from '../lib/cost';
import { transcriptsEnabled } from '../lib/transcripts';
import { openShareModal } from '../store/shareSlice';

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
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#clock" />
          </svg>
          <h2>
            Recent Sessions <span className="sub">({total} total)</span>
          </h2>
          {oneModel && (
            <span className="sess-model-caption" title={`All sessions use ${oneModelRaw}`}>
              <span className={`ms-dot ${modelChipClass(oneModelRaw)}`} aria-hidden="true" />
              all · {oneModel}
            </span>
          )}
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
            <svg className="icon" aria-hidden="true">
              <use href={`/static/icons.svg#${collapsed ? 'chevron-down' : 'chevron-up'}`} />
            </svg>
          </button>
          <PanelGrip />
        </div>
      </div>
      {isMobile && <SessionsControls />}
      <div className="panel-body panel-body--scroll" id="panel-sessions-body">
        <table className={'sess-table' + (oneModel ? ' single-model' : '')}>
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
                  <td className="started">
                    {transcriptsOn && r.session_id && (
                      <button
                        type="button"
                        className="sess-open-conv"
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
                    {fmt.startedShort(r.started_utc, ctx, { noSuffix: true })}
                  </td>
                  <td>{r.duration_min}m</td>
                  {oneModel ? (
                    <td className="model-ditto-cell">
                      <span className="model-ditto" aria-hidden="true">·</span>
                    </td>
                  ) : (
                    <td onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        className={`chip model-chip ${chipCls}`}
                        aria-label={chipLabel}
                        onClick={() => dispatch({ type: 'SET_FILTER', text: r.model })}
                      >
                        {r.model}
                      </button>
                    </td>
                  )}
                  <td className="project">
                    {r.project_key && r.project_key !== '(unknown)' ? (
                      <button
                        type="button"
                        className="project-cell-link"
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
                        {r.project}
                      </button>
                    ) : (
                      // Null project_key (session_files row not yet
                      // ingested) OR literal '(unknown)' bucket — render
                      // plain text. Per spec §4.1: "When project_key is
                      // null or (unknown), the cell renders plain text
                      // (not clickable) with tooltip 'Project still
                      // resolving'."
                      <span title="Project still resolving">{r.project}</span>
                    )}
                  </td>
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
