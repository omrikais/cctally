import { useContext, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { ModelLegend } from '../components/ModelLegend';
import { fmt } from '../lib/fmt';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { keyOf } from '../modals/periodNav';
import { useReducedMotion } from '../hooks/useReducedMotion';
import { BoardModeContext } from '../lib/boardModeContext';
import { summarize } from '../lib/summaryWindow';
import { cardRegionClick } from '../lib/cardRegion';
import { presentationPeriodRows, presentationProviders } from '../lib/dashboardPresentation';
import type { PeriodRow } from '../types/envelope';
import { modelChipStyle } from '../lib/model';

// #264 S2 / #265 — the Weekly summary TILE (restored from the S8 collapse).
// #293 S3 — below 900px (stack mode) the card previews the newest
// SUMMARY_WINDOW_CAP weeks and defers the rest to the full-table weekly modal
// via an explicit "+N more" footer button; at >=900 it renders ALL weeks and
// the bento card scrolls internally (the #264 S4 A1 inner scroll) so every
// week stays reachable. The footer always summarizes the WHOLE window ($total
// is the envelope scalar, never the visible slice). S1 card chrome: the header
// right-side leaves live in a `.panel-header-actions` cluster with a ⤢
// ExpandButton.

function Row({ r, isFirstMount, reduced }: { r: PeriodRow; isFirstMount: boolean; reduced: boolean }) {
  const animate = isFirstMount && !reduced;
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
            style={{ ...modelChipStyle(m.model), width: animate ? '0%' : `${m.cost_pct}%` }}
            title={`${m.display} ${fmt.usd2(m.cost_usd)} (${m.cost_pct.toFixed(0)}%)`}
          />
        ))}
      </div>
      <ModelLegend models={r.models} />
    </div>
  );
}

// #294 S5 — source-aware wrapper. Claude = subscription-week summary tile;
// Codex = observed native reset-cycle summaries; All uses the same shared row
// anatomy for both providers without treating independent reset axes as one.
export function WeeklyPanel() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const allRows = presentationPeriodRows(env, activeSource, 'weekly');
  const mode = useContext(BoardModeContext);
  const providerGroups = activeSource === 'all'
    ? (['claude', 'codex'] as const).map((source) => {
        const rows = allRows.filter((row) => row.source === source);
        return { source, rows, ...summarize(rows, mode) };
      })
    : null;
  const singleSummary = summarize(allRows, mode);
  const visible = providerGroups?.flatMap((group) => group.visible) ?? singleSummary.visible;
  const hiddenCount = providerGroups?.reduce((sum, group) => sum + group.hiddenCount, 0)
    ?? singleSummary.hiddenCount;
  const total = allRows.reduce((sum, row) => sum + row.cost_usd, 0);
  const rowNoun = activeSource === 'claude' ? 'weeks' : activeSource === 'codex' ? 'cycles' : 'provider periods';
  const totalLabel = activeSource === 'claude' ? `${allRows.length}w` : `${allRows.length} ${rowNoun}`;
  const hydrating = presentationProviders(env, activeSource).hydrating;
  const reduced = useReducedMotion();

  const seen = useRef<Set<string>>(new Set());
  const [, forceRender] = useState(0);
  useLayoutEffect(() => {
    const fresh = visible.filter((r) => !seen.current.has(keyOf(r, 'week')));
    if (fresh.length === 0) return;
    fresh.forEach((r) => seen.current.add(keyOf(r, 'week')));
    const id = requestAnimationFrame(() => forceRender((n) => n + 1));
    return () => cancelAnimationFrame(id);
  }, [visible]);

  return (
    <section
      className="panel accent-cyan"
      id="panel-weekly"
      role="region"
      aria-label="Weekly usage panel"
      data-panel-kind="weekly"
      data-source={activeSource}
      onClick={cardRegionClick(() => dispatch({ type: 'OPEN_MODAL', kind: 'weekly' }))}
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
        {allRows.length === 0 ? (
          hydrating ? (
            <PanelSkeleton />
          ) : (
            <div className="panel-empty">No usage history yet.</div>
          )
        ) : providerGroups ? (
          <div className="source-all-sections provider-composition provider-composition--panel">
            {providerGroups.map((group) => (
              <section
                key={group.source}
                className="provider-summary-card source-provider-section weekly-provider-section"
                data-provider-section={group.source}
                aria-label={`${group.source === 'claude' ? 'Claude' : 'Codex'} weekly quota history`}
              >
                <div className="source-provider-head provider-composition-head">
                  <span className={`source-chip source-chip--${group.source}`}>
                    {group.source === 'claude' ? 'Claude' : 'Codex'}
                  </span>
                  <span className="provider-summary-label">
                    {group.rows.length} {group.source === 'claude' ? 'weeks' : 'cycles'}
                  </span>
                </div>
                {group.visible.length === 0 ? (
                  <div className="panel-source-empty">No quota history yet.</div>
                ) : group.visible.map((r) => (
                  <Row
                    key={keyOf(r, 'week')}
                    r={r}
                    isFirstMount={!seen.current.has(keyOf(r, 'week'))}
                    reduced={reduced}
                  />
                ))}
              </section>
            ))}
          </div>
        ) : (
          visible.map((r) => (
            <Row
              key={keyOf(r, 'week')}
              r={r}
              isFirstMount={!seen.current.has(keyOf(r, 'week'))}
              reduced={reduced}
            />
          ))
        )}
      </div>
      {allRows.length > 0 && (
        <div className="panel-foot period-foot">
          {hiddenCount > 0 ? (
            <span>
              <button
                type="button"
                className="period-foot-more"
                aria-label={`Show all ${allRows.length} ${rowNoun}`}
                onClick={(e) => {
                  e.stopPropagation();
                  dispatch({ type: 'OPEN_MODAL', kind: 'weekly' });
                }}
                onKeyDown={(e) => {
                  // Enter/Space-scoped guard: block ONLY the region's Enter/Space
                  // handler from also opening; the native button click does the
                  // single dispatch. A blanket stopPropagation would swallow
                  // PanelHost's Shift+Arrow reorder (#293 S3, Codex F6).
                  if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
                }}
              >
                +{hiddenCount} more
              </button>
              <span className="sep" aria-hidden="true"> · </span>
              <span className="total">{fmt.usd2(total)}</span>
            </span>
          ) : (
            <span>
              {totalLabel} total
              <span className="sep" aria-hidden="true"> · </span>
              <span className="total">{fmt.usd2(total)}{activeSource === 'all' ? ' combined cost' : ''}</span>
            </span>
          )}
        </div>
      )}
    </section>
  );
}
