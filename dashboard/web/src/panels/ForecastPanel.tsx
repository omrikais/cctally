import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { ShareIcon } from '../components/ShareIcon';
import { resolveVerdict } from '../lib/verdict';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';
import { openShareModal } from '../store/shareSlice';

// ForecastPanel (#248 §4) — a calm-when-healthy uniform TILE. The projected %
// at reset is the dominant number; the verdict chip's glyph comes straight from
// `resolveVerdict(...).glyph` (✓ / ⚠ / ⛔) — this is the panel side of C2,
// replacing the old `#fc-banner` that hardcoded `icons.svg#warn-triangle`.
// Escalation: `ok` stays calm (neutral tile, outlined green chip, no accent
// edge); `cap` (WARN) draws a 4px amber accent edge + a filled amber chip +
// amber number tint; `capped` (OVER) is red. The recent-24h projection + the
// two per-day budgets render muted at the foot; the full breakdown lives in the
// (out-of-scope) Forecast modal the tile opens.
export function ForecastPanel() {
  const env = useSnapshot();
  const fc = env?.forecast ?? null;
  const v = resolveVerdict(fc?.verdict ?? null);
  // `v.cls` is 'good' | 'warn' | 'over'. The accent edge escalates on any
  // non-OK verdict (cap/capped both set `warn: true`).
  const esc = v?.cls ?? 'good';
  const hasEdge = !!v?.warn;
  return (
    <section
      className={`panel accent-purple fc-tile fc-esc-${esc}${hasEdge ? ' fc-accent-edge' : ''}`}
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
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#crystal-ball" />
        </svg>
        <h2>Forecast</h2>
        <ShareIcon
          panel="forecast"
          panelLabel="Forecast"
          triggerId="forecast-panel"
          onClick={() => dispatch(openShareModal('forecast', 'forecast-panel'))}
        />
        <PanelGrip />
      </div>
      <div className="panel-body fc-body">
        <div className="fc-hero">
          <div className="fc-eyebrow">Projected @ reset</div>
          <div className={`fc-num is-${esc}`}>{fmt.pct0(fc?.week_avg_projection_pct)}</div>
          {v && (
            <span className={`fc-verdict-chip is-${esc}`}>
              <span className="fc-verdict-glyph" aria-hidden="true">{v.glyph}</span>
              {' '}
              {v.label}
            </span>
          )}
        </div>
        <div className="fc-budget-foot">
          <div className="fc-foot-line">
            <span className="fc-foot-k">Recent-24h</span>
            <span className="fc-foot-v">{fmt.pct0(fc?.recent_24h_projection_pct)} @ reset</span>
          </div>
          <div className="fc-foot-line">
            <span className="fc-foot-k">Budget ≤100%</span>
            <span className="fc-foot-v">{fmt.usd2(fc?.budget_100_per_day_usd)}/day</span>
          </div>
          <div className="fc-foot-line">
            <span className="fc-foot-k">Budget ≤90%</span>
            <span className="fc-foot-v">{fmt.usd2(fc?.budget_90_per_day_usd)}/day</span>
          </div>
        </div>
      </div>
    </section>
  );
}
