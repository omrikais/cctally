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
import type { CacheReportEnvelope } from '../types/envelope';

function fmtSignedUsd(n: number): string {
  // Match the panel's signed-dollar rendering (Unicode minus sign);
  // see CacheReportPanel.tsx for the precedent.
  const sign = n >= 0 ? '+' : '−';
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

// Snap-up before floor — same idiom CLAUDE.md requires for any
// fraction-times-100 percent integer.
function floorPct(p: number): number {
  return Math.floor(p + 1e-9);
}

export interface CacheReportSpotlightProps {
  cr: CacheReportEnvelope;
}

export function CacheReportSpotlight({ cr }: CacheReportSpotlightProps) {
  const anomalous = cr.today.anomaly_triggered;
  const insufficient = cr.today.baseline_daily_row_count < 5;

  let pill: { text: string; cls: string };
  if (anomalous) {
    pill = { text: '⚠ Anomaly', cls: '' };
  } else if (insufficient) {
    pill = {
      text: `~ Building baseline · ${cr.today.baseline_daily_row_count}/5 days`,
      cls: 'thin',
    };
  } else {
    pill = { text: '✓ Healthy', cls: 'ok' };
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
    const abs = floorPct(Math.abs(delta));
    deltaText = `${delta < 0 ? '−' : '+'}${abs}pp`;
  }

  return (
    <div className="crm-section">
      <div className="crm-section-head crm-sh-spotlight">
        Today's spotlight
        <span className="meta">
          {cr.today.date} · {cr.days.length} days observed
        </span>
      </div>
      <div className={`crm-spotlight${anomalous ? ' anom' : ''}`}>
        <div
          style={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 14,
            alignItems: 'center',
          }}
        >
          <span className={`pill${pill.cls ? ' ' + pill.cls : ''}`}>{pill.text}</span>
          <span>
            <span className="k">Cache hit</span>{' '}
            <strong>{floorPct(cr.today.cache_hit_percent)}%</strong>
          </span>
          <span>
            <span className="k">14d median</span>{' '}
            <strong>{median !== null ? `${floorPct(median)}%` : '—'}</strong>
          </span>
          <span>
            <span className="k">Δ</span> <strong>{deltaText}</strong>
          </span>
          <span>
            <span className="k">Net</span>{' '}
            <strong>{fmtSignedUsd(cr.today.net_usd)}</strong>
          </span>
          <span>
            <span className="k">Saved / Wasted</span>{' '}
            <strong>
              ${cr.today.saved_usd.toFixed(2)} / ${cr.today.wasted_usd.toFixed(2)}
            </strong>
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
