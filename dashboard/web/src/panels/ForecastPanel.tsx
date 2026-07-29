import { useSyncExternalStore } from 'react';
import { useAccountScope, useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { sourceAccounts } from '../store/accountFocus';
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

// #416 QA P0 — the shared blank. A forecast is a claim about ONE quota
// allowance, and a decorated Codex provider has several; no summary statistic
// over them is the quantity this slot holds (D6). The pointer reuses the hero's
// `hero-per-account-value` italic-caption vocabulary so the blanked slots on
// the hero and the panel read as one deliberate treatment rather than two
// unrelated absences.
export function ForecastPerAccountValue(): JSX.Element {
  return (
    <span
      className="hero-per-account-value"
      data-testid="forecast-per-account"
      title="Each Codex account has its own quota cycle — independent forecasts are never blended."
    >
      per account
    </span>
  );
}

function ForecastProviderSummary({
  section,
  perAccount,
}: {
  section: ProviderPresentationSection<ForecastPresentation>;
  perAccount: boolean;
}) {
  const value = section.value;
  const verdict = perAccount ? null : resolveVerdict(value?.verdict ?? null);
  const verdictClass = verdict?.cls ?? 'good';
  return (
    <div
      className="source-provider-section provider-summary-card forecast-provider-summary"
      data-provider-section={section.source}
      aria-label={`${section.label} forecast`}
    >
      <div className="source-provider-head">
        <SourceChip source={section.source} />
        {section.status !== 'available' && !perAccount && (
          <span className="provider-section-status">{section.status}</span>
        )}
      </div>
      {value == null && !perAccount ? (
        <div className="provider-section-reason">{section.reason}</div>
      ) : (
        <>
          <div className="provider-summary-kpis">
            <div>
              <span className="provider-summary-label">
                {value?.primaryLabel ?? 'Projected @ reset'}
              </span>
              <strong className={`provider-summary-value is-${verdictClass}`}>
                {perAccount
                  ? <ForecastPerAccountValue />
                  : verdictClass === 'over' && value!.projected != null
                    ? '≥100%'
                    : fmt.pct0(value!.projected)}
              </strong>
            </div>
            <div>
              <span className="provider-summary-label">
                {perAccount ? 'Current quota' : value!.recentLabel}
              </span>
              <strong className="provider-summary-value">
                {perAccount ? <ForecastPerAccountValue /> : fmt.pct0(value!.recent)}
              </strong>
            </div>
          </div>
          {verdict && (
            <span className={`fc-verdict-chip is-${verdictClass}`}>
              <span className="fc-verdict-glyph" aria-hidden="true">{verdict.glyph}</span>
              {' '}{verdict.label}
            </span>
          )}
          {section.reason && !perAccount && (
            <div className="provider-section-reason">{section.reason}</div>
          )}
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
  const env = useScopedSnapshot();
  const activeSource = useSyncExternalStore(subscribeStore, () => getState().activeSource);
  const scope = useAccountScope();
  const composition = presentationForecastComposition(env, activeSource);
  // #416 QA P0. On the Codex tab the gate is the scope chokepoint's own answer
  // (decorated AND unfocused), the same predicate the hero uses. On the
  // combined tab there is no Codex chip to focus, so the gate is simply whether
  // the provider ships `accounts[]` — matching `SharedHero`'s `all` branch.
  const codexPerAccount = activeSource === 'all'
    ? sourceAccounts(env?.sources?.codex ?? null) != null
    : scope.scopesSupported && scope.accountKey == null;
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
            <ForecastProviderSummary
              key={section.source}
              section={section}
              perAccount={section.source === 'codex' && codexPerAccount}
            />
          ))}
        </div>
      </section>
    );
  }
  const fc = presentationForecast(env, activeSource);
  // #416 QA P0 — under "All accounts" the tile blanks: the verdict, the accent
  // edge and the escalation tint all describe ONE account's allowance, and this
  // tile sat ~40px under a hero that already reads `Forecast @ reset —
  // per account`, contradicting it in a single glance. The expansion carries
  // the per-account disclosure.
  const perAccount = activeSource === 'codex' && codexPerAccount;
  const v = perAccount ? null : resolveVerdict(fc.verdict);
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
          <div className={`fc-num is-${esc}${perAccount ? ' is-blank' : ''}`}>
            {perAccount ? <ForecastPerAccountValue /> : projectedLabel}
          </div>
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
            style={{ width: `${perAccount ? 0 : Math.min(100, Math.max(0, fc.projected ?? 0))}%` }}
          />
        </div>
        <div className="fc-budget-foot">
          <div className="fc-foot-line">
            <span className="fc-foot-k">{perAccount ? 'Current quota' : fc.recentLabel}</span>
            <span className="fc-foot-v">
              {perAccount ? <ForecastPerAccountValue /> : fmt.pct0(fc.recent)}
            </span>
          </div>
          {/* `Confidence` describes one account's sample history, so it blanks
              with the projection it qualifies. `Budget pace` is a SPEND axis —
              the one thing D6 does let "All accounts" merge — and the server
              already merges it, so it rides through unchanged. */}
          {(perAccount ? fc.foot.filter((l) => l.label !== 'Confidence') : fc.foot).map((line) => (
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
