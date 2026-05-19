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
import type { CSSProperties } from 'react';

export interface ProjectsDrillPanelProps {
  projectKey: string;
  windowWeeks: number;
}

export function ProjectsDrillPanel({ projectKey, windowWeeks }: ProjectsDrillPanelProps) {
  const { data, loading, error } = useProjectDetail(projectKey, windowWeeks);
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };

  if (loading && !data) return <div className="panel-empty">Loading…</div>;
  if (error && !data) return <div className="panel-empty">{error}</div>;
  if (!data) return null;

  const topModelCost = data.models[0]?.cost_usd ?? 0;
  const denom = topModelCost > 0 ? topModelCost : 1;
  const remaining = Math.max(0, data.sessions_total - data.sessions.length);

  return (
    <div className="projects-drill" aria-live="polite">
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
                  <span className="cost">{fmt.usd2(m.cost_usd)}</span>
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
                <span>{fmt.datetimeShort(s.last_activity_at, ctx)}</span>
                <span className={`chip ${modelChipClass(s.primary_model)}`}>
                  {s.primary_model}
                </span>
                <span className="cost">{fmt.usd2(s.cost_usd)}</span>
              </button>
            ))
          )}
          {remaining > 0 && (
            <div className="muted">+{remaining} more</div>
          )}
        </div>
      </div>
    </div>
  );
}
