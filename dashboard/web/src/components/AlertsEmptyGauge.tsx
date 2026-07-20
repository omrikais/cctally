import type { DashboardSelection } from '../types/envelope';

// Shared empty-state gauge for the Recent Alerts panel + modal (#265 item A).
// Extracted so both surfaces render an identical "you're clear" gauge and can't
// drift (they previously diverged: the panel had a one-liner + thin bar, the
// modal had this richer gauge). `compact` sizes it down for the 200px bento
// short-row tile (CSS `.ra-gauge--compact`). The canonical anatomy stays
// mounted even when `usedPct` is unknown: the hero renders an em dash and the
// fill stays at zero so every source keeps the same detail composition.
//
// Codex uses the retained native weekly quota observation and configured quota
// thresholds. Independent provider percentages are never combined.
interface Props {
  usedPct: number | null;
  thresholds: number[];
  compact?: boolean;
  // Defaults to 'claude' so existing callers (and the legacy pre-S4 path) keep
  // the exact weekly-gauge behavior.
  source?: DashboardSelection;
}

export function AlertsEmptyGauge({
  usedPct,
  thresholds,
  compact = false,
  source = 'claude',
}: Props): JSX.Element {
  const thresholdCopy = thresholds.map((t) => `${t}%`).join(' / ');
  const lowest = Math.min(...thresholds);
  const highest = Math.max(...thresholds);
  const fillPct = usedPct == null ? 0 : Math.max(0, Math.min(usedPct, 100));
  const headline = source === 'codex'
    ? 'No Codex alerts yet'
    : source === 'all'
      ? 'No alerts yet across Claude and Codex'
      : usedPct != null && usedPct < lowest
        ? `You're at ${Math.round(usedPct)}% — well under the line`
        : 'No alerts yet';
  const description = source === 'codex'
    ? `Codex quota alerts fire when native usage crosses ${thresholdCopy}.`
    : source === 'all'
      ? `Provider-native alerts appear at their configured ${thresholdCopy} thresholds.`
      : `Alerts fire when weekly usage crosses ${thresholdCopy}.`;
  return (
    <div className={'ra-gauge' + (compact ? ' ra-gauge--compact' : '')}>
      <div className="ra-gauge-head">
        <span className="ra-gauge-check" aria-hidden="true">✓</span>
        {headline}
      </div>
      <div className="ra-gauge-hero">{usedPct == null ? '—' : `${Math.round(usedPct)}%`}</div>
      <div className="ra-gauge-bar">
        <div className="ra-gauge-fill" style={{ width: `${fillPct}%` }} />
        {thresholds.map((th, i) => (
          <span
            key={`${th}-${i}`}
            className={
              'ra-gauge-tick ' +
              (th === lowest ? 'tick-amber' : th === highest ? 'tick-red' : 'tick-mid')
            }
            data-th={String(th)}
            style={{ left: `${th}%` }}
          />
        ))}
      </div>
      <div className="ra-gauge-copy">
        {description}
      </div>
    </div>
  );
}
