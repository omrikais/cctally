import { useLayoutEffect, useRef, useState } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { ModelLegend } from '../components/ModelLegend';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { PeriodRow } from '../types/envelope';

// #264 S2 / #265 — the Weekly summary TILE (restored from the S8 collapse).
// Renders ALL weeks; the bento card scrolls internally (the #264 S4 A1 inner
// scroll — mirrors the Blocks A2 uncap) so every week is reachable rather than
// stranding the older ones behind a scrollbar that had nothing to reveal. The
// footer summarizes the whole window. S1 card chrome: the header right-side
// leaves live in a `.panel-header-actions` cluster with a ⤢ ExpandButton.

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
  const rows = allRows;
  const total = env?.weekly?.total_cost_usd ?? 0;

  const seenLabels = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  useLayoutEffect(() => {
    const newLabels = rows.filter((r) => !seenLabels.current.has(r.label));
    if (newLabels.length === 0) return;
    newLabels.forEach((r) => seenLabels.current.add(r.label));
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
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'weekly' })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
        }
      }}
    >
      <div className="panel-header">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#bar-chart" />
        </svg>
        <h2>
          Weekly <span className="sub">(model split)</span>
        </h2>
        <div className="panel-header-actions">
          <ShareIcon
            panel="weekly"
            panelLabel="Weekly"
            triggerId="weekly-panel"
            onClick={() => dispatch(openShareModal('weekly', 'weekly-panel'))}
          />
          <ExpandButton
            label="Weekly"
            onOpen={() => dispatch({ type: 'OPEN_MODAL', kind: 'weekly' })}
          />
          <PanelGrip />
        </div>
      </div>
      <div className="panel-body">
        {rows.length === 0 ? (
          env?.hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">No usage history yet.</div>
          )
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
