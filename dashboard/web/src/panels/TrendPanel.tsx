import { useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Sparkline } from '../components/Sparkline';
import { SortableHeader } from '../components/SortableHeader';
import { PanelGrip } from '../components/PanelGrip';
import { ShareIcon } from '../components/ShareIcon';
import { fmt } from '../lib/fmt';
import { applyTableSort } from '../lib/tableSort';
import { TREND_COLUMNS, type TrendTableRow } from '../lib/trendColumns';
import { buildTrendSparkData, type TrendChartDatum } from '../store/selectors';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';

// Reads trend.weeks (8 rows) via buildTrendSparkData — CLAUDE.md
// gotcha: do NOT merge with trend.history (12 rows, modal-only).

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

export function TrendPanel() {
  const env = useSnapshot();
  const data = buildTrendSparkData(env);
  const trendOverride = useSyncExternalStore(
    subscribeStore,
    () => getState().prefs.trendSortOverride,
  );
  const decorated: TrendTableRow[] = data.map((r, i) => ({ ...r, _chronoIdx: i }));
  const tableData = trendOverride
    ? applyTableSort(decorated, TREND_COLUMNS, trendOverride)
    : decorated;
  // A5 — text summary for the sparkline (a multi-point series, so role="img"
  // on the grid wrapper, not progressbar). Covers the weeks span + the
  // latest $/1% value and its direction vs the prior week.
  const sparkLabel = buildSparkLabel(data);
  return (
    <section
      className="panel accent-amber"
      id="panel-trend"
      tabIndex={0}
      role="region"
      aria-label="$/1% Trend panel"
      data-panel-kind="trend"
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'trend' })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'trend' });
        }
      }}
    >
      <div className="panel-header">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#bar-chart" />
        </svg>
        <h2>
          $/1% Trend <span className="sub">(8 weeks)</span>
        </h2>
        <ShareIcon
          panel="trend"
          panelLabel="Trend"
          triggerId="trend-panel"
          onClick={() => dispatch(openShareModal('trend', 'trend-panel'))}
        />
        <PanelGrip />
      </div>
      <div className="panel-body">
        <table className="trend-table">
          <SortableHeader
            columns={TREND_COLUMNS}
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
        <div className="trend-spark-title">$/1% trend:</div>
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
    </section>
  );
}
