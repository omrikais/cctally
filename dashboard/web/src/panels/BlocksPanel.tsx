import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { ShareIcon } from '../components/ShareIcon';
import { ModelLegend } from '../components/ModelLegend';
import { fmt } from '../lib/fmt';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import type { BlocksPanelRow } from '../types/envelope';

function Row({ r, maxCost, isFirstMount }: { r: BlocksPanelRow; maxCost: number; isFirstMount: boolean }) {
  const fillPct = maxCost > 0 ? (r.cost_usd / maxCost) * 100 : 0;
  const open = () => dispatch({
    type: 'OPEN_MODAL',
    kind: 'block',
    blockStartAt: r.start_at,
  });
  return (
    <div
      className="blocks-row"
      role="button"
      tabIndex={0}
      aria-label={`Open detail for block starting ${r.label}`}
      onClick={open}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      }}
    >
      <div className="meta">
        <span className="label">
          {r.anchor === 'heuristic' && (
            <span className="anchor-marker" aria-label="approximate start">~</span>
          )}
          {r.label}
          {r.is_active && <span className="pill-active">Active</span>}
        </span>
        <span className="cost">{fmt.usd2(r.cost_usd)}</span>
      </div>
      <div className="gauge-track">
        <div
          className="gauge-fill"
          // First paint of a row animates from 0 → target width;
          // subsequent SSE updates render straight to target.
          style={{ width: isFirstMount ? '0%' : `${fillPct}%` }}
        >
          {r.models.map((m) => (
            <span
              key={m.model}
              className={`seg-${m.chip}`}
              style={{ width: `${m.cost_pct}%` }}
              title={`${m.display} ${fmt.usd2(m.cost_usd)} (${m.cost_pct.toFixed(0)}%)`}
            />
          ))}
        </div>
      </div>
      <ModelLegend models={r.models} />
    </div>
  );
}

export function BlocksPanel() {
  const env = useSnapshot();
  const collapsed = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.blocksCollapsed,
  );
  // #248 §2a — uniform summary TILE: the body caps to the 3 most-recent blocks
  // (each row still opens its own Block modal). `maxCost` is computed over the
  // FULL week so the 3 shown bars keep their true scale vs the week's peak; the
  // footer count + total summarize the whole window.
  const allRows = env?.blocks?.rows ?? [];
  const rows = allRows.slice(0, 3);
  const maxCost = allRows.length > 0 ? Math.max(...allRows.map((r) => r.cost_usd), 0) : 0;
  // View-model unification follow-up (issue #56): footer total comes
  // from the typed envelope scalar rather than re-summing rows in JS.
  // The Python sync thread sums over the same visible rows via
  // `BlocksView.total_cost_usd` so the invariant
  // `total === sum(rows[*].cost_usd)` still holds structurally; the
  // `?? 0` fallback covers pre-#56 first-paint envelopes that haven't
  // populated the additive scalar yet.
  const total = env?.blocks?.total_cost_usd ?? 0;
  const hasHeuristic = rows.some((r) => r.anchor === 'heuristic');

  // First-mount animation: paint .gauge-fill width:0, then rAF flips to
  // target width so the CSS transition interpolates. Spec §2.5.
  const seenStarts = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  useLayoutEffect(() => {
    const newRows = rows.filter((r) => !seenStarts.current.has(r.start_at));
    if (newRows.length === 0) return;
    newRows.forEach((r) => seenStarts.current.add(r.start_at));
    const id = requestAnimationFrame(() => forceRender((n) => n + 1));
    return () => cancelAnimationFrame(id);
  }, [rows]);

  return (
    <section
      className={'panel accent-blue' + (collapsed ? ' blocks-collapsed' : '')}
      id="panel-blocks"
      tabIndex={0}
      role="region"
      aria-label="Blocks panel"
      data-panel-kind="blocks"
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#layers" />
          </svg>
          <h2>
            Blocks <span className="sub">(5h · current week)</span>
          </h2>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <ShareIcon
            panel="blocks"
            panelLabel="5-hour blocks"
            triggerId="blocks-panel"
            onClick={() => dispatch(openShareModal('blocks', 'blocks-panel'))}
          />
          <button
            type="button"
            className="panel-collapse-toggle"
            aria-expanded={!collapsed}
            aria-controls="panel-blocks-body"
            aria-label={collapsed ? 'Expand Blocks' : 'Collapse Blocks'}
            title={collapsed ? 'Expand' : 'Collapse'}
            onClick={(e) => {
              e.stopPropagation();
              dispatch({
                type: 'SAVE_PREFS',
                patch: { blocksCollapsed: !collapsed },
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
      <div className="panel-body" id="panel-blocks-body">
        {rows.length === 0 ? (
          <div className="panel-empty">No activity blocks this week yet.</div>
        ) : (
          rows.map((r) => (
            <Row
              key={r.start_at}
              r={r}
              maxCost={maxCost}
              isFirstMount={!seenStarts.current.has(r.start_at)}
            />
          ))
        )}
      </div>
      {allRows.length > 0 && (
        <div className="panel-foot">
          <span>
            {allRows.length} blocks
            <span className="sep" aria-hidden="true"> · </span>
            <span className="total">{fmt.usd2(total)}</span>
            {hasHeuristic && (
              <>
                <span className="sep" aria-hidden="true"> · </span>
                <span className="legend-anchor">~ = approximate start</span>
              </>
            )}
          </span>
        </div>
      )}
    </section>
  );
}
