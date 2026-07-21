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
import {
  presentationProviders,
  presentationTrend,
  type TrendProviderSection,
} from '../lib/dashboardPresentation';

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

function TrendSection({
  section,
  trendOverride,
  composed,
}: {
  section: TrendProviderSection;
  trendOverride: ReturnType<typeof getState>['prefs']['trendSortOverride'];
  composed: boolean;
}) {
  const data: TrendChartDatum[] = section.rows.map((row) => ({
    ...row,
    spark_height: row.dollar_per_pct ?? 0,
  }));
  const decorated: TrendTableRow[] = data.map((r, i) => ({ ...r, _chronoIdx: i }));
  const columns = PANEL_TREND_COLUMNS;
  const tableData = trendOverride
    ? applyTableSort(decorated, columns, trendOverride)
    : decorated;
  // A5 — text summary for the sparkline (a multi-point series, so role="img"
  // on the grid wrapper, not progressbar). Covers the weeks span + the
  // latest $/1% value and its direction vs the prior week.
  const sparkLabel = buildSparkLabel(data);
  const sparkId = composed ? `trend-spark-${section.source}` : 'trend-spark';
  return (
    <>
        {/* #265 B — the chart leads and stays pinned; the table scrolls beneath
            it (`.trend-table-wrap`), so a thin history no longer pushes the
            sparkline + legend below the S4 in-card fold. */}
        <div className="trend-chart">
            <div className="trend-spark-title">$/1% trend:</div>
          <div
            className="trend-spark"
            id={sparkId}
            role="img"
            aria-label={`${section.label} ${sparkLabel}`}
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
            <tbody id={composed ? `trend-rows-${section.source}` : 'trend-rows'}>
              {tableData.map((w) => (
                <tr key={`${section.source}:${w.label}`} className={w.is_current ? 'current' : undefined}>
                  <td>{w.label}</td>
                  <td className="num">{fmt.pct0(w.used_pct)}</td>
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
  );
}

// Source-aware wrapper. Claude and Codex retain their canonical series. All is
// a pair of source-owned charts/tables, never a chronology formed by sorting
// independent reset axes together.
export function TrendPanel() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const presentation = presentationTrend(env, activeSource);
  const trendOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.trendSortOverride,
  );
  const totalRows = presentation.sections.reduce((sum, section) => sum + section.rows.length, 0);
  const hydratingEmpty = presentationProviders(env, activeSource).hydrating && totalRows === 0;
  const single = presentation.sections[0];
  const sub = activeSource === 'all'
    ? `(Claude ${presentation.sections[0]?.rows.length ?? 0}w · Codex ${presentation.sections[1]?.rows.length ?? 0}c)`
    : `(${single?.rows.length ?? 0} week${single?.rows.length === 1 ? '' : 's'})`;

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
          {presentation.title} <span className="sub">{hydratingEmpty ? '(loading)' : sub}</span>
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
        {env?.hydrating && totalRows === 0 ? (
          <PanelSkeleton />
        ) : activeSource === 'all' ? (
          <div className="source-all-sections provider-composition trend-provider-composition">
            {presentation.sections.map((section) => (
              <section
                key={section.source}
                className="provider-summary-card source-provider-section trend-provider-section"
                data-provider-section={section.source}
                aria-label={`${section.label} $ per 1% trend`}
              >
                <div className="source-provider-head provider-composition-head">
                  <span className={`source-chip source-chip--${section.source}`}>{section.label}</span>
                  <span className="provider-summary-label">
                    {section.rows.length} {section.source === 'claude' ? 'weeks' : 'cycles'}
                  </span>
                </div>
                <TrendSection section={section} trendOverride={trendOverride} composed />
              </section>
            ))}
          </div>
        ) : single ? (
          <TrendSection section={single} trendOverride={trendOverride} composed={false} />
        ) : null}
      </div>
    </section>
  );
}
