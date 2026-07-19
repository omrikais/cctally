import { useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { ModelLegend } from '../components/ModelLegend';
import { fmt } from '../lib/fmt';
import { modelChipStyle } from '../lib/model';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { presentationBlocks, presentationProviders, type BlockPresentationRow } from '../lib/dashboardPresentation';

function openBlockDetail(r: BlockPresentationRow): void {
  if (r.source === 'claude') {
    dispatch({ type: 'OPEN_MODAL', kind: 'block', blockStartAt: r.start_at });
  } else {
    dispatch({ type: 'OPEN_SOURCE_DETAIL', source: r.source, resource: 'block', key: r.key });
  }
}

function Row({ r, maxCost, isFirstMount }: { r: BlockPresentationRow; maxCost: number; isFirstMount: boolean }) {
  const fillPct = maxCost > 0 ? (r.value / maxCost) * 100 : 0;
  const open = () => openBlockDetail(r);
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
        <span className="cost">{r.valueLabel}</span>
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
              style={{ ...modelChipStyle(m.model), width: `${m.cost_pct}%` }}
              title={`${m.display} ${fmt.usd2(m.cost_usd)} (${m.cost_pct.toFixed(0)}%)`}
            />
          ))}
        </div>
      </div>
      <ModelLegend models={r.models} />
    </div>
  );
}

// #294 S5 — source-aware wrapper. Both providers render real 5h activity
// blocks; Codex boundaries come from its durable native 300-minute windows.
export function BlocksPanel() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const collapsed = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.blocksCollapsed,
  );
  // #264 S4 (A2): render ALL blocks; the bento card scrolls internally (A1) so
  // every block is reachable (the old #248 slice(0,3) summary-cap hid blocks
  // 4..N with no view for them). `maxCost` still spans the full week so every
  // bar keeps its true scale vs the week's peak; the footer count + total
  // already summarize the whole set (each row still opens its own Block modal).
  const allRows = presentationBlocks(env, activeSource);
  const rows = allRows;
  const maxCost = allRows.length > 0 ? Math.max(...allRows.map((r) => r.value), 0) : 0;
  // Compatible provider costs can be combined once in All mode. These rows
  // are already source-qualified, so summing their displayed values preserves
  // the no-double-count invariant while keeping Codex-only totals truthful.
  const total = allRows.reduce((sum, row) => sum + row.value, 0);
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
      role="region"
      aria-label="Blocks panel"
      data-panel-kind="blocks"
      data-source={activeSource}
    >
      <div className="panel-header" style={{ justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#layers" />
          </svg>
          <h2>
            Blocks <span className="sub">({activeSource === 'codex' ? '5h · current cycle' : activeSource === 'all' ? '5h · current cycles' : '5h · current week'})</span>
          </h2>
        </div>
        <div className="panel-header-actions">
          <ShareIcon
            panel="blocks"
            panelLabel="5-hour blocks"
            triggerId="blocks-panel"
            onClick={() => dispatch(openShareModal('blocks', 'blocks-panel'))}
          />
          <ExpandButton
            label="Blocks"
            onOpen={() => {
              const row = allRows.find((item) => item.is_active) ?? allRows[0];
              if (!row) return;
              openBlockDetail(row);
            }}
            disabled={allRows.length === 0}
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
            presentationProviders(env, activeSource).hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">
              {activeSource === 'codex'
                ? 'No 5-hour activity blocks in the current Codex cycle.'
                : activeSource === 'all'
                  ? 'No 5-hour activity blocks in the current provider cycles.'
                  : 'No activity blocks this week yet.'}
            </div>
          )
        ) : (
          rows.map((r) => (
            <Row
              key={r.key}
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
