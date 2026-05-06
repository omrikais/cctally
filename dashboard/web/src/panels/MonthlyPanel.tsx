import { useLayoutEffect, useRef, useState } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';
import type { PeriodRow } from '../types/envelope';

const VISIBLE_ROWS = 6;

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
    </div>
  );
}

export function MonthlyPanel() {
  const env = useSnapshot();
  const rows = (env?.monthly?.rows ?? []).slice(0, VISIBLE_ROWS);
  const total = rows.reduce((acc, r) => acc + r.cost_usd, 0);

  // First-mount animation: see WeeklyPanel for full notes. Spec §4.4.
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
      className="panel accent-pink"
      id="panel-monthly"
      tabIndex={0}
      role="region"
      aria-label="Monthly usage panel"
      data-panel-kind="monthly"
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'monthly' })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'monthly' });
        }
      }}
    >
      <div className="panel-header">
        <svg className="icon" style={{ color: 'var(--accent-pink)' }}>
          <use href="/static/icons.svg#calendar" />
        </svg>
        <h3 style={{ color: 'var(--accent-pink)' }}>
          Monthly <span className="sub">(model split · 6 months)</span>
        </h3>
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
      {rows.length > 0 && (
        <div className="panel-foot period-foot">
          <span>
            {rows.length}mo total
            <span className="sep" aria-hidden="true"> · </span>
            <span className="total">{fmt.usd2(total)}</span>
          </span>
        </div>
      )}
    </section>
  );
}
