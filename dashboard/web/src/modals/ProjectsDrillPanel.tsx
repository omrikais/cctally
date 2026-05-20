// ProjectsDrillPanel — per-project drill that appears below the
// projects table when a row is selected (spec §3.5, plan Task 5 Step 5).
//
// Lazy-fetches GET /api/project/<key>?weeks=N via `useProjectDetail`
// (stale-while-revalidate). Renders two columns on desktop:
//   - Models (this project): horizontal mini-bars sized to top model.
//   - Recent sessions:        clickable rows opening SessionModal (the
//                             cross-nav "replace pattern"; spec §4.2).
//
// `sessions_total > sessions.length` adds a "+N more" affordance below
// the visible list (spec §3.5).
import { useProjectDetail } from '../hooks/useProjectDetail';
import { dispatch } from '../store/store';
import { fmt } from '../lib/fmt';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { modelChipClass } from '../lib/model';
import { costClass } from '../lib/cost';
import type { CSSProperties } from 'react';

export interface ProjectsDrillPanelProps {
  projectKey: string;
  windowWeeks: number;
}

export function ProjectsDrillPanel({ projectKey, windowWeeks }: ProjectsDrillPanelProps) {
  const { data, loading, error } = useProjectDetail(projectKey, windowWeeks);
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };

  // Stale-on-switch guard: while `useProjectDetail` is fetching for the
  // newly selected project — OR for the same project under a different
  // window (e.g. 12w → 4w) — the SWR pattern keeps prior `data` mounted.
  // Without a window check the drill keeps rendering the prior window's
  // cost/models/sessions under the new `{windowWeeks}w` heading until
  // /api/project resolves; on large projects that fetch can take seconds
  // so the modal would show numbers that disagree with the visible
  // header. Render Loading… until the new fetch resolves so the drill
  // never lies about which (project, window) it represents.
  const isStaleForCurrentKey =
    data != null &&
    (data.key !== projectKey || data.window_weeks !== windowWeeks);

  if ((loading && !data) || isStaleForCurrentKey)
    return <div className="panel-empty">Loading…</div>;
  if (error && !data) return <div className="panel-empty">{error}</div>;
  if (!data) return null;

  const topModelCost = data.models[0]?.cost_usd ?? 0;
  const denom = topModelCost > 0 ? topModelCost : 1;
  const remaining = Math.max(0, data.sessions_total - data.sessions.length);

  return (
    <div className="projects-drill" data-testid="projects-drill" aria-live="polite">
      <div className="projects-drill-head">
        <span className="title">
          ▾ {data.key} · {data.sessions_total} session{data.sessions_total === 1 ? '' : 's'}
          {' · '}
          {fmt.usd2(data.window_cost_usd)} ({windowWeeks}w)
        </span>
      </div>
      <div className="projects-drill-grid">
        <div>
          <div className="section-label">Models (this project)</div>
          {data.models.length === 0 ? (
            <div className="muted">No model data for this window.</div>
          ) : (
            data.models.map((m) => {
              const widthPct = (m.cost_usd / denom) * 100;
              const style = { '--w': `${widthPct}%` } as CSSProperties;
              return (
                <div className="drill-bar-row" key={m.model}>
                  <span className={`chip ${modelChipClass(m.model)}`}>{m.model}</span>
                  <div className="drill-bar" style={style} />
                  <span className={`cost ${costClass(m.cost_usd)}`}>{fmt.usd2(m.cost_usd)}</span>
                </div>
              );
            })
          )}
        </div>
        <div>
          <div className="section-label">Recent sessions →</div>
          {data.sessions.length === 0 ? (
            <div className="muted">No sessions for this window.</div>
          ) : (
            data.sessions.map((s, i) => (
              <button
                key={s.session_id}
                data-testid={`drill-session-${i}`}
                className="drill-session-row"
                onClick={() =>
                  dispatch({
                    type: 'OPEN_MODAL',
                    kind: 'session',
                    sessionId: s.session_id,
                  })
                }
              >
                <span className="started">{fmt.datetimeShort(s.last_activity_at, ctx)}</span>
                <span className={`chip ${modelChipClass(s.primary_model)}`}>
                  {s.primary_model}
                </span>
                <span className={`cost ${costClass(s.cost_usd)}`}>{fmt.usd2(s.cost_usd)}</span>
              </button>
            ))
          )}
          <div className="drill-session-footer">
            {remaining > 0 && (
              <span className="muted">+{remaining} more</span>
            )}
            <button
              type="button"
              data-testid="drill-show-in-sessions"
              className="drill-show-in-sessions"
              onClick={() => {
                dispatch({ type: 'SET_FILTER', text: data.key });
                dispatch({ type: 'CLOSE_MODAL' });
              }}
            >
              Show in Sessions →
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
