import { useSnapshot } from '../hooks/useSnapshot';
import { ConfidenceDots } from '../components/ConfidenceDots';
import { PanelGrip } from '../components/PanelGrip';
import { resolveVerdict } from '../lib/verdict';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';

export function ForecastPanel() {
  const env = useSnapshot();
  const fc = env?.forecast ?? null;
  const v = resolveVerdict(fc?.verdict ?? null);
  return (
    <section
      className="panel accent-purple"
      id="panel-forecast"
      tabIndex={0}
      role="region"
      aria-label="Forecast panel"
      data-panel-kind="forecast"
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'forecast' })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'forecast' });
        }
      }}
    >
      <div className="panel-header">
        <svg className="icon" style={{ color: 'var(--accent-pink)' }}>
          <use href="/static/icons.svg#crystal-ball" />
        </svg>
        <h3 style={{ color: 'var(--accent-pink)' }}>Forecast</h3>
        <PanelGrip />
      </div>
      <div className="panel-body">
        <div
          id="fc-banner"
          className={'warn-banner' + (v ? ' ' + v.cls : '')}
          style={{ display: v ? '' : 'none' }}
        >
          <svg width="20" height="20">
            <use href="/static/icons.svg#warn-triangle" />
          </svg>
          <span id="fc-verdict-label">{v?.label ?? '—'}</span>
        </div>
        <div className="fc-row fc-wkavg">
          <div className="left">
            <div className="icon-box">
              <svg className="icon">
                <use href="/static/icons.svg#trending-up" />
              </svg>
            </div>
            <span>Week-avg projection</span>
          </div>
          <div className="right">
            <span className="num">{fmt.pct0(fc?.week_avg_projection_pct)}</span>
            <span className="at">@ reset</span>
          </div>
        </div>
        <div className="fc-row fc-24h">
          <div className="left">
            <div className="icon-box">
              <svg className="icon">
                <use href="/static/icons.svg#flame" />
              </svg>
            </div>
            <span>Recent-24h projection</span>
          </div>
          <div className="right">
            <span className="num">{fmt.pct0(fc?.recent_24h_projection_pct)}</span>
            <span className="at">@ reset</span>
          </div>
        </div>
        <div className="fc-divider"></div>
        <div className="fc-row fc-budget-100">
          <div className="left">
            <div className="icon-box">
              <svg className="icon">
                <use href="/static/icons.svg#dollar" />
              </svg>
            </div>
            <span>Budget to stay ≤100%</span>
          </div>
          <div className="right">
            <span className="num">{fmt.usd2(fc?.budget_100_per_day_usd)}</span>
            <span className="per">/day</span>
          </div>
        </div>
        <div className="fc-row fc-budget-90">
          <div className="left">
            <div className="icon-box">
              <svg className="icon">
                <use href="/static/icons.svg#dollar" />
              </svg>
            </div>
            <span>Budget to stay ≤90%</span>
          </div>
          <div className="right">
            <span className="num">{fmt.usd2(fc?.budget_90_per_day_usd)}</span>
            <span className="per">/day</span>
          </div>
        </div>
      </div>
      <div className="panel-foot fc-conf">
        <div className="lbl">
          <svg className="icon">
            <use href="/static/icons.svg#clock" />
          </svg>
          Confidence:<span className="val">{fc?.confidence ?? '—'}</span>
        </div>
        <ConfidenceDots n={fc?.confidence_score} />
      </div>
    </section>
  );
}
