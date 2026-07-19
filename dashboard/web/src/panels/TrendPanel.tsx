import { useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Sparkline } from '../components/Sparkline';
import { SortableHeader } from '../components/SortableHeader';
import { PanelGrip } from '../components/PanelGrip';
import { PanelSkeleton } from '../components/PanelSkeleton';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { cardRegionClick } from '../lib/cardRegion';
import { fmt } from '../lib/fmt';
import { applyTableSort } from '../lib/tableSort';
import { TREND_COLUMNS, type TrendTableRow } from '../lib/trendColumns';
import type { TrendChartDatum } from '../store/selectors';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import { presentationProviders, presentationTrend } from '../lib/dashboardPresentation';

// Reads trend.weeks (8 rows) via buildTrendSparkData — CLAUDE.md
// gotcha: do NOT merge with trend.history (12 rows, modal-only).

// S3 (#264, finding 3): the panel renders — AND SORTS BY — a subset of
// TREND_COLUMNS that omits the modal-only Cost column, derived ONCE at module
// scope. Passing this same subset to both applyTableSort and SortableHeader
// means a stale persisted `trendSortOverride.column==='cost_usd'` (set from the
// modal in an older build, or hand-edited localStorage) can't sort the panel by
// a hidden column: applyTableSort returns rows unsorted when the override id
// isn't in the passed set (`columns.find(...) === undefined → return rows`).
const PANEL_TREND_COLUMNS = TREND_COLUMNS.filter((c) => c.id !== 'cost_usd');

// A5 — accessible summary for the role="img" sparkline. Describes the
// series span, the latest $/1% value, and its direction vs the prior week
// (mirrors the table's delta read) without needing the visual bars.
function buildSparkLabel(data: TrendChartDatum[]): string {
  if (data.length === 0) return '$/1% trend: no data';
  const last = data[data.length - 1];
  const prev = data.length > 1 ? data[data.length - 2] : null;
  const latest =
    last.dollar_per_pct == null ? 'n/a' : fmt.usd2(last.dollar_per_pct);
  let dir = '';
  if (prev && last.dollar_per_pct != null && prev.dollar_per_pct != null) {
    const d = last.dollar_per_pct - prev.dollar_per_pct;
    dir =
      Math.abs(d) < 0.005
        ? ', flat vs prior week'
        : d > 0
          ? ', up vs prior week'
          : ', down vs prior week';
  }
  return `$/1% trend over ${data.length} weeks; latest ${latest}${dir}`;
}

// #294 S5 — source-aware wrapper. The $/1% trend is a Claude-only surface (Codex
// hero publishes no trend, §5.5): Claude renders the existing sparkline+table
// unchanged; Codex renders nothing (gate-hidden — the grid unmounts it, and the
// shell returns null defensively); All renders the Claude-labeled provider
// section (no Codex trend section). Wrapping stops the legacy top-level
// `env.trend` from leaking Claude $/1% data under a Codex label.
export function TrendPanel() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const presentation = presentationTrend(env, activeSource);
  const data: TrendChartDatum[] = presentation.rows.map((row) => ({
    ...row,
    spark_height: row.dollar_per_pct ?? 0,
  }));
  // #278 Theme A (ui-qa P3): header sub-label predicate — while hydrating with
  // no rows yet the sub-label reads "(loading)" instead of the misleading
  // "(0 weeks)" final-state copy (mirrors CacheReportPanel's header). Same
  // hydrating+empty condition the body's skeleton branch uses below.
  const hydratingEmpty = presentationProviders(env, activeSource).hydrating && data.length === 0;
  const trendOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.trendSortOverride,
  );
  const decorated: TrendTableRow[] = data.map((r, i) => ({ ...r, _chronoIdx: i }));
  const columns = activeSource === 'claude'
    ? PANEL_TREND_COLUMNS
    : PANEL_TREND_COLUMNS
        .filter((column) => column.id !== 'used_pct')
        .map((column) => column.id === 'dollar_per_pct' ? { ...column, label: 'Cost' } : column);
  const tableData = trendOverride
    ? applyTableSort(decorated, columns, trendOverride)
    : decorated;
  // A5 — text summary for the sparkline (a multi-point series, so role="img"
  // on the grid wrapper, not progressbar). Covers the weeks span + the
  // latest $/1% value and its direction vs the prior week.
  const sparkLabel = buildSparkLabel(data);
  return (
    <section
      className="panel accent-amber"
      id="panel-trend"
      role="region"
      aria-label="$/1% Trend panel"
      data-panel-kind="trend"
      data-source={activeSource}
      onClick={cardRegionClick(() => dispatch({ type: 'OPEN_MODAL', kind: 'trend' }))}
    >
      <div className="panel-header">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#bar-chart" />
        </svg>
        <h2>
          {presentation.title} <span className="sub">{hydratingEmpty ? '(loading)' : `(${data.length} week${data.length === 1 ? '' : 's'})`}</span>
        </h2>
        <div className="panel-header-actions">
          <ShareIcon
            panel="trend"
            panelLabel="Trend"
            triggerId="trend-panel"
            onClick={() => dispatch(openShareModal('trend', 'trend-panel'))}
          />
          <ExpandButton
            label="Trend"
            onOpen={() => dispatch({ type: 'OPEN_MODAL', kind: 'trend' })}
          />
          <PanelGrip />
        </div>
      </div>
      <div className="panel-body">
        {env?.hydrating && data.length === 0 ? (
          // #278 §1.4: the cheap first-paint seed hasn't built the trend rows
          // yet; show a loading skeleton instead of an empty sparkline/table.
          <PanelSkeleton />
        ) : (
        <>
        {/* #265 B — the chart leads and stays pinned; the table scrolls beneath
            it (`.trend-table-wrap`), so a thin history no longer pushes the
            sparkline + legend below the S4 in-card fold. */}
        <div className="trend-chart">
            <div className="trend-spark-title">{presentation.chartLabel}</div>
          <div
            className="trend-spark"
            id="trend-spark"
            role="img"
            aria-label={sparkLabel}
            style={{ gridTemplateColumns: `repeat(${Math.max(1, data.length)}, 1fr)` }}
          >
            <Sparkline data={data} />
          </div>
          <div className="trend-spark-legend">
            <span>older</span>
            <span className="line"></span>
            <span>▶ newer</span>
          </div>
        </div>
        <div className="trend-table-wrap">
          <table className="trend-table">
            <SortableHeader
              columns={columns}
              override={trendOverride}
              onChange={(next) =>
                dispatch({ type: 'SET_TABLE_SORT', table: 'trend', override: next })
              }
              accentVar="--accent-amber"
            />
            <tbody id="trend-rows">
              {tableData.map((w) => (
                <tr key={w.label} className={w.is_current ? 'current' : undefined}>
                  <td>{w.label}</td>
                  {activeSource === 'claude' && <td className="num">{fmt.pct0(w.used_pct)}</td>}
                  <td className={'num' + (w.is_current ? '' : ' dollar')}>
                    {fmt.usd2(w.dollar_per_pct)}
                  </td>
                  <td className={'num ' + fmt.deltaCls(w.delta, w.is_current)}>
                    {fmt.delta(w.delta)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        </>
        )}
      </div>
    </section>
  );
}
