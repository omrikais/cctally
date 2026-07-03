// Shared empty-state gauge for the Recent Alerts panel + modal (#265 item A).
// Extracted so both surfaces render an identical "you're clear" gauge and can't
// drift (they previously diverged: the panel had a one-liner + thin bar, the
// modal had this richer gauge). `compact` sizes it down for the 200px bento
// short-row tile (CSS `.ra-gauge--compact`). When `usedPct` is unknown we fall
// back to the `.panel-empty` one-liner (a null-fill gauge would be broken) —
// this matches both callers' prior behavior exactly.
interface Props {
  usedPct: number | null;
  thresholds: number[];
  compact?: boolean;
}

export function AlertsEmptyGauge({ usedPct, thresholds, compact = false }: Props): JSX.Element {
  const thresholdCopy = thresholds.map((t) => `${t}%`).join(' / ');
  if (usedPct == null) {
    return (
      <div className="panel-empty">
        No alerts yet. Alerts appear when usage crosses {thresholdCopy}.
      </div>
    );
  }
  const lowest = Math.min(...thresholds);
  const highest = Math.max(...thresholds);
  const fillPct = Math.max(0, Math.min(usedPct, 100));
  return (
    <div className={'ra-gauge' + (compact ? ' ra-gauge--compact' : '')}>
      {usedPct < lowest ? (
        <div className="ra-gauge-head">
          <span className="ra-gauge-check" aria-hidden="true">✓</span>
          You're at {Math.round(usedPct)}% — well under the line
        </div>
      ) : null}
      <div className="ra-gauge-hero">{Math.round(usedPct)}%</div>
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
        Alerts fire when weekly usage crosses {thresholdCopy}.
      </div>
    </div>
  );
}
