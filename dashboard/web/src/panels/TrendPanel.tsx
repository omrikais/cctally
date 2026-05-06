import { useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { Sparkline } from '../components/Sparkline';
import { SortableHeader } from '../components/SortableHeader';
import { PanelGrip } from '../components/PanelGrip';
import { fmt } from '../lib/fmt';
import { applyTableSort } from '../lib/tableSort';
import { TREND_COLUMNS, type TrendTableRow } from '../lib/trendColumns';
import { buildTrendSparkData } from '../store/selectors';
import { dispatch, getState, subscribeStore } from '../store/store';

// Reads trend.weeks (8 rows) via buildTrendSparkData — CLAUDE.md
// gotcha: do NOT merge with trend.history (12 rows, modal-only).

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
        <svg className="icon" style={{ color: 'var(--accent-amber)' }}>
          <use href="/static/icons.svg#bar-chart" />
        </svg>
        <h3 style={{ color: 'var(--accent-amber)' }}>
          $/1% Trend <span className="sub">(8 weeks)</span>
        </h3>
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
        <div className="trend-spark" id="trend-spark">
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
