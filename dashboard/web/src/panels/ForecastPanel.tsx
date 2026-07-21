import { useSyncExternalStore } from 'react';
import { useSnapshot } from '../hooks/useSnapshot';
import { PanelGrip } from '../components/PanelGrip';
import { ShareIcon } from '../components/ShareIcon';
import { ExpandButton } from '../components/ExpandButton';
import { resolveVerdict } from '../lib/verdict';
import { cardRegionClick } from '../lib/cardRegion';
import { fmt } from '../lib/fmt';
import { dispatch, getState, subscribeStore } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import {
  presentationForecast,
  presentationForecastComposition,
  type ProviderPresentationSection,
  type ForecastPresentation,
} from '../lib/dashboardPresentation';
import { SourceChip } from './sourcePanel';

function ForecastProviderSummary({
  section,
}: {
  section: ProviderPresentationSection<ForecastPresentation>;
}) {
  const value = section.value;
  const verdict = resolveVerdict(value?.verdict ?? null);
  const verdictClass = verdict?.cls ?? 'good';
  return (
    <div
      className="source-provider-section provider-summary-card forecast-provider-summary"
      data-provider-section={section.source}
      aria-label={`${section.label} forecast`}
    >
      <div className="source-provider-head">
        <SourceChip source={section.source} />
        {section.status !== 'available' && (
          <span className="provider-section-status">{section.status}</span>
        )}
      </div>
      {value == null ? (
        <div className="provider-section-reason">{section.reason}</div>
      ) : (
        <>
          <div className="provider-summary-kpis">
            <div>
              <span className="provider-summary-label">{value.primaryLabel}</span>
              <strong className={`provider-summary-value is-${verdictClass}`}>
                {verdictClass === 'over' && value.projected != null
                  ? '≥100%'
                  : fmt.pct0(value.projected)}
              </strong>
            </div>
            <div>
              <span className="provider-summary-label">{value.recentLabel}</span>
              <strong className="provider-summary-value">{fmt.pct0(value.recent)}</strong>
            </div>
          </div>
          {verdict && (
            <span className={`fc-verdict-chip is-${verdictClass}`}>
              <span className="fc-verdict-glyph" aria-hidden="true">{verdict.glyph}</span>
              {' '}{verdict.label}
            </span>
          )}
          {section.reason && <div className="provider-section-reason">{section.reason}</div>}
        </>
      )}
    </div>
  );
}

// ForecastPanel (#248 §4) — a calm-when-healthy uniform TILE. The projected %
// at reset is the dominant number; the verdict chip's glyph comes straight from
// `resolveVerdict(...).glyph` (✓ / ⚠ / ⛔) — this is the panel side of C2,
// replacing the old `#fc-banner` that hardcoded `icons.svg#warn-triangle`.
// Escalation: `ok` stays calm (neutral tile, outlined green chip, no accent
// edge); `cap` (WARN) draws a 4px amber accent edge + a filled amber chip +
// amber number tint; `capped` (OVER) is red. The recent-24h projection + the
// two per-day budgets render muted at the foot; the full breakdown lives in the
// (out-of-scope) Forecast modal the tile opens.
// #294 S5 / #324 Task A — source-aware wrapper. Single-provider selections
// keep the canonical tile, while All composes independent Claude and Codex
// summaries inside one shell. The adapter keeps the legacy top-level Claude
// forecast from leaking into the Codex section.
export function ForecastPanel() {
  const env = useSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const composition = presentationForecastComposition(env, activeSource);
  if (activeSource === 'all') {
    return (
      <section
        className="panel accent-purple fc-tile"
        id="panel-forecast"
        role="region"
        aria-label="Forecast panel · Claude and Codex"
        data-panel-kind="forecast"
        data-source="all"
        onClick={cardRegionClick(() => dispatch({ type: 'OPEN_MODAL', kind: 'forecast' }))}
      >
        <div className="panel-header">
          <svg className="icon" aria-hidden="true">
            <use href="/static/icons.svg#crystal-ball" />
          </svg>
          <h2>Forecast <span className="sub">by provider</span></h2>
          <div className="panel-header-actions">
            <ShareIcon
              panel="forecast"
              panelLabel="Forecast"
              triggerId="forecast-panel"
              onClick={() => dispatch(openShareModal('forecast', 'forecast-panel'))}
            />
            <ExpandButton
              label="Forecast"
              onOpen={() => dispatch({ type: 'OPEN_MODAL', kind: 'forecast' })}
            />
            <PanelGrip />
          </div>
        </div>
        <div className="panel-body source-all-sections provider-composition provider-composition--panel">
          {composition.sections.map((section) => (
            <ForecastProviderSummary key={section.source} section={section} />
          ))}
        </div>
      </section>
    );
  }
  const fc = presentationForecast(env, activeSource);
  const v = resolveVerdict(fc.verdict);
  // `v.cls` is 'good' | 'warn' | 'over'. The accent edge escalates on any
  // non-OK verdict (cap/capped both set `warn: true`).
  const esc = v?.cls ?? 'good';
  const hasEdge = !!v?.warn;
  // A quota cannot report more than 100%, so capped forecasts deliberately
  // retain the backend's physical cap.  Mark it as a lower bound instead of
  // presenting 100% as a suspiciously exact repeated estimate.
  const projectedLabel = esc === 'over' && fc.projected != null
    ? '≥100%'
    : fmt.pct0(fc.projected);
  return (
    <section
      className={`panel accent-purple fc-tile fc-esc-${esc}${hasEdge ? ' fc-accent-edge' : ''}`}
      id="panel-forecast"
      role="region"
      aria-label="Forecast panel"
      data-panel-kind="forecast"
      data-source={activeSource}
      onClick={cardRegionClick(() => dispatch({ type: 'OPEN_MODAL', kind: 'forecast' }))}
    >
      <div className="panel-header">
        <svg className="icon" aria-hidden="true">
          <use href="/static/icons.svg#crystal-ball" />
        </svg>
        <h2>Forecast</h2>
        <div className="panel-header-actions">
          <ShareIcon
            panel="forecast"
            panelLabel="Forecast"
            triggerId="forecast-panel"
            onClick={() => dispatch(openShareModal('forecast', 'forecast-panel'))}
          />
          <ExpandButton
            label="Forecast"
            onOpen={() => dispatch({ type: 'OPEN_MODAL', kind: 'forecast' })}
          />
          <PanelGrip />
        </div>
      </div>
      <div className="panel-body fc-body">
        <div className="fc-hero">
          <div className="fc-eyebrow">{fc.primaryLabel}</div>
          <div className={`fc-num is-${esc}`}>{projectedLabel}</div>
          {v && (
            <span className={`fc-verdict-chip is-${esc}`}>
              <span className="fc-verdict-glyph" aria-hidden="true">{v.glyph}</span>
              {' '}
              {v.label}
            </span>
          )}
        </div>
        {/* #264 S1 (VOID-1) — pace bar: projection toward the 100% cap, sized
            to week_avg_projection_pct (clamped 0..100) and verdict-tinted, so
            the short-row tile fills its matched height instead of leaving a
            void. Decorative (role="presentation") — the number + verdict chip
            above already carry the value + status for AT. */}
        <div className={`fc-pace is-${esc}`} role="presentation">
          <div
            className="fc-pace-fill"
            style={{ width: `${Math.min(100, Math.max(0, fc.projected ?? 0))}%` }}
          />
        </div>
        <div className="fc-budget-foot">
          <div className="fc-foot-line">
            <span className="fc-foot-k">{fc.recentLabel}</span>
            <span className="fc-foot-v">{fmt.pct0(fc.recent)}</span>
          </div>
          {fc.foot.map((line) => (
            <div className="fc-foot-line" key={line.label}>
              <span className="fc-foot-k">{line.label}</span>
              <span className="fc-foot-v">{line.value}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
