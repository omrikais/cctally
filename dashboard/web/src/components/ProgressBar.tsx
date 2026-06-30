interface ProgressBarProps {
  percent: number | null | undefined;
  cells?: number;
  /** Accessible name for the gauge (A5), e.g. "7-day usage". */
  label?: string;
}

// Mirrors dashboard/static/render.js#renderProgress output:
//   <div id="cw-progress" class="progress"> (NOT "progress-bar" — matches
//   index.css `.progress { display:flex; ... }` rules). Exactly `cells`
//   children, each <div class="cell"> with `.on` when i < on.
//
// A5 — this is the one genuine single-value 0–100 gauge, so it exposes
// `role="progressbar"` with aria-valuenow/min/max + an aria-label. The
// trend sparkline / Projects leaderboard bar are NOT progressbars
// (they're role="img" summaries or decorative).
export function ProgressBar({ percent, cells = 30, label }: ProgressBarProps) {
  const v = percent ?? 0;
  const on = Math.max(0, Math.min(cells, Math.round((v / 100) * cells)));
  return (
    <div
      id="cw-progress"
      className="progress"
      role="progressbar"
      aria-valuenow={Math.round(v)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
    >
      {Array.from({ length: cells }, (_, i) => (
        <div key={i} className={'cell' + (i < on ? ' on' : '')} />
      ))}
    </div>
  );
}
