interface ProgressBarProps {
  percent: number | null | undefined;
  cells?: number;
}

// Mirrors dashboard/static/render.js#renderProgress output:
//   <div id="cw-progress" class="progress"> (NOT "progress-bar" — matches
//   index.css `.progress { display:flex; ... }` rules). Exactly `cells`
//   children, each <div class="cell"> with `.on` when i < on.
export function ProgressBar({ percent, cells = 30 }: ProgressBarProps) {
  const v = percent ?? 0;
  const on = Math.max(0, Math.min(cells, Math.round((v / 100) * cells)));
  return (
    <div id="cw-progress" className="progress">
      {Array.from({ length: cells }, (_, i) => (
        <div key={i} className={'cell' + (i < on ? ' on' : '')} />
      ))}
    </div>
  );
}
