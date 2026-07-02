import { useLayoutEffect, useRef, useState } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { ShareIcon } from '../components/ShareIcon';
import { ModelLegend } from '../components/ModelLegend';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { PeriodRow } from '../types/envelope';

// #248 §2a — the panel is a uniform summary TILE in the two-tier grid, so the
// body caps to the 3 most-recent periods (the History modal keeps the full
// history). The footer still summarizes the WHOLE window (count + envelope
// total) so the tile reads "Nw total · $X · here are the 3 most recent".
//
// S8 (#254): this panel left the default grid (the History card supersedes it);
// the component + tests remain, and its open action now opens the History modal.
const VISIBLE_ROWS = 3;

function Row({ r, isFirstMount }: { r: PeriodRow; isFirstMount: boolean }) {
  const deltaCls =
    r.delta_cost_pct == null ? 'flat' :
    r.delta_cost_pct > 0 ? 'up' : r.delta_cost_pct < 0 ? 'down' : 'flat';
  return (
    <div className="period">
      <div className="meta">
        <span className="label">
          {r.label}
          {r.is_current && <span className="pill-current">Now</span>}
        </span>
        <span className="right">
          <span className="cost">{fmt.usd2(r.cost_usd)}</span>
          <span className={`delta ${deltaCls}`}>{fmt.deltaPct(r.delta_cost_pct)}</span>
        </span>
      </div>
      <div className="model-stack" role="presentation">
        {r.models.map((m) => (
          <span
            key={m.model}
            className={m.chip}
            // Spec §4.4: first paint of a row animates from 0 → target;
            // subsequent SSE updates render the target directly so the
            // 1 s tick doesn't feel jittery.
            style={{ width: isFirstMount ? '0%' : `${m.cost_pct}%` }}
            title={`${m.display} ${fmt.usd2(m.cost_usd)} (${m.cost_pct.toFixed(0)}%)`}
          />
        ))}
      </div>
      <ModelLegend models={r.models} />
    </div>
  );
}

export function WeeklyPanel() {
  const env = useSnapshot();
  const allRows = env?.weekly?.rows ?? [];
  const rows = allRows.slice(0, VISIBLE_ROWS);
  // View-model unification (spec §6.6a): consume the envelope's pre-
  // computed total instead of re-summing in JS. The footer count uses the
  // FULL window length (allRows) so it stays structurally consistent with that
  // total even though the body only paints the 3 most-recent rows (#248 §2a).
  const total = env?.weekly?.total_cost_usd ?? 0;

  // First-mount animation: track which row labels we've already painted.
  // On first encounter, render at width:0; after one layout pass, mark the
  // label seen and force a re-render so CSS transitions to target width.
  const seenLabels = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  useLayoutEffect(() => {
    const newLabels = rows.filter((r) => !seenLabels.current.has(r.label));
    if (newLabels.length === 0) return;
    newLabels.forEach((r) => seenLabels.current.add(r.label));
    // Schedule a re-render on the next frame so the browser paints the
    // 0% width first, then animates to the target via the CSS transition.
    const id = requestAnimationFrame(() => forceRender((n) => n + 1));
    return () => cancelAnimationFrame(id);
  }, [rows]);
  return (
    <section
      className="panel accent-cyan"
      id="panel-weekly"
      tabIndex={0}
      role="region"
      aria-label="Weekly usage panel"
      data-panel-kind="weekly"
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'history' })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'history' });
        }
      }}
    >
      <div className="panel-header">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#bar-chart" />
        </svg>
        <h2>
          Weekly <span className="sub">(model split · recent)</span>
        </h2>
        <ShareIcon
          panel="weekly"
          panelLabel="Weekly"
          triggerId="weekly-panel"
          onClick={() => dispatch(openShareModal('weekly', 'weekly-panel'))}
        />
        <PanelGrip />
      </div>
      <div className="panel-body">
        {rows.length === 0 ? (
          <div className="panel-empty">No usage history yet.</div>
        ) : (
          rows.map((r) => (
            <Row key={r.label} r={r} isFirstMount={!seenLabels.current.has(r.label)} />
          ))
        )}
      </div>
      {allRows.length > 0 && (
        <div className="panel-foot period-foot">
          <span>
            {allRows.length}w total
            <span className="sep" aria-hidden="true"> · </span>
            <span className="total">{fmt.usd2(total)}</span>
          </span>
        </div>
      )}
    </section>
  );
}
