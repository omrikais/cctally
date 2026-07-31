// CacheReportSpotlight — section 1 of the Cache Report modal.
//
// Renders today's status pill (Healthy / Anomaly / Building baseline),
// the inline stat row (Cache hit, 14d median, Δ, Net, Saved / Wasted),
// and — on anomaly — the comma-joined reasons line with the active
// thresholds. Section heading + sub-card border swap between
// ``var(--accent-teal)`` (healthy / insufficient) and
// ``var(--accent-amber)`` (anomalous) so the spotlight visually
// matches the panel border accent.
//
// Spec 2026-05-21 §3.3.
import type { ReactNode } from 'react';
import type { CacheReportEnvelope } from '../types/envelope';
import { fmt } from '../lib/fmt';
import { CACHE_REPORT_MIN_BASELINE_DAYS } from '../lib/cache-report-constants';
import { cacheReportVerdict } from '../lib/cacheReportVerdict';

export interface CacheReportSpotlightProps {
  cr: CacheReportEnvelope;
  nativeCacheOnly?: boolean;
}

export function CacheReportSpotlight({ cr, nativeCacheOnly = false }: CacheReportSpotlightProps) {
  const verdict = cacheReportVerdict(cr);
  const { insufficient } = verdict;
  const anomalous = verdict.chromeAmber;
  const todayObserved = verdict.todayObserved;
  const observedDays = cr.days.filter((d) => d.observed !== false).length;

  let pill: { text: string; cls: string };
  if (insufficient) {
    pill = {
      text: `~ Building baseline · ${cr.today.baseline_daily_row_count}/${CACHE_REPORT_MIN_BASELINE_DAYS} days`,
      cls: 'thin',
    };
  } else if (anomalous) {
    pill = { text: '⚠ Anomaly', cls: '' };
  } else if (!todayObserved) {
    // Both predicates are unevaluated for a synthetic today row, so a
    // Healthy pill here would be a verdict over nothing (#443 F1).
    pill = { text: '· No activity today', cls: 'thin' };
  } else {
    pill = { text: '✓ Healthy', cls: 'ok' };
  }

  // One statement of the rule: a field that was never measured renders an em
  // dash, so a real $0.00 break-even day stays distinguishable from an absent
  // one. The 14d median is deliberately NOT wrapped — it is known even when
  // today is not.
  const unmeasured = (node: ReactNode) =>
    todayObserved ? node : <span className="m-unavailable">—</span>;
  if (nativeCacheOnly) {
    pill = { text: 'Provider cache reuse', cls: 'ok' };
  }

  const median = cr.today.baseline_median_percent;
  const delta = cr.today.delta_pp;

  // Delta render: spec calls for "−Xpp" when below median (worse) and
  // "+Xpp" when above. Today.delta_pp is signed with our convention
  // matching the panel (negative = below median). We render with a
  // Unicode minus or plus respectively.
  let deltaText: string;
  if (delta === null) {
    deltaText = '—';
  } else {
    const abs = fmt.pctFloor(Math.abs(delta));
    deltaText = `${delta < 0 ? '−' : '+'}${abs}pp`;
  }

  return (
    <div className="crm-section">
      <div className="crm-section-head crm-sh-spotlight">
        Today's spotlight
        <span className="meta">
          {fmt.calDate(cr.today.date)} · {observedDays} days observed
        </span>
      </div>
      <div className={`crm-spotlight${anomalous ? ' anom' : ''}`}>
        <div className="crm-spotlight-row">
          <span className={`pill${pill.cls ? ' ' + pill.cls : ''}`}>{pill.text}</span>
          <span>
            <span className="k">Cache hit</span>{' '}
            <strong>
              {nativeCacheOnly && cr.days.length === 0
                ? <span className="m-unavailable">Unavailable</span>
                : unmeasured(`${fmt.pctFloor(cr.today.cache_hit_percent)}%`)}
            </strong>
          </span>
          <span>
            <span className="k">14d median</span>{' '}
            <strong>{median !== null ? `${fmt.pctFloor(median)}%` : <span className="m-unavailable">Unavailable</span>}</strong>
          </span>
          <span>
            <span className="k">Δ</span> <strong>{nativeCacheOnly ? <span className="m-unavailable">Unavailable</span> : unmeasured(deltaText)}</strong>
          </span>
          <span>
            <span className="k">Net</span>{' '}
            <strong>{nativeCacheOnly ? <span className="m-unavailable">Unavailable</span> : unmeasured(fmt.usdSigned(cr.today.net_usd))}</strong>
          </span>
          <span>
            <span className="k">Saved / Wasted</span>{' '}
            <strong>{nativeCacheOnly ? <span className="m-unavailable">Unavailable</span> : unmeasured(`$${cr.today.saved_usd.toFixed(2)} / $${cr.today.wasted_usd.toFixed(2)}`)}</strong>
          </span>
        </div>
        {anomalous && cr.today.anomaly_reasons.length > 0 && (
          <div className="reasons">
            Reasons:{' '}
            {cr.today.anomaly_reasons.map((r, i) => (
              <span key={r}>
                {i > 0 && ' · '}
                <code>{r}</code>
              </span>
            ))}
            {'  |  '}thresholds: {cr.anomaly_threshold_pp}pp drop, net &lt; 0
          </div>
        )}
      </div>
    </div>
  );
}
