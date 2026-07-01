// ProjectsRankedBars — horizontal ranked-bar view for the ProjectsModal
// under a dominant distribution (PR-2, #250).
//
// When one project holds >= 60% of the window's cost (isDominant), the
// stacked-area chart degenerates to a near-solid block with unreadable
// slivers. This view renders one row per top-5 project + `(other)`,
// sorted desc, as `label · bar(width ∝ cost/topCost) · "$X · Y%"`.
//
// Colors + basenames come from the shared `projectsChart` palette
// (colorFor / basenameOf), the single source of truth also used by the
// stacked-area chart and the legend below — so a palette edit can never
// desync a ranked bar from its own legend swatch. Clicking a real project
// row drills via onProjectSelect(key); the `(other)` rollup is inert — the
// same drill contract as the stacked-area mode.
//
// Vanilla markup, no chart library (stdlib-only ethos).
import type { PreparedSeries } from './projectsChart';
import { OTHER_KEY, colorFor, basenameOf } from './projectsChart';

export function ProjectsRankedBars({
  series,
  onProjectSelect,
}: {
  series: PreparedSeries[];
  onProjectSelect?: (key: string) => void;
}) {
  const total = series.reduce((s, p) => s + p.cost, 0) || 1;
  const top = Math.max(...series.map((p) => p.cost), 0.01);
  return (
    <div className="projects-ranked" data-testid="projects-ranked-bars">
      {series.map((p, i) => {
        const isOther = p.key === OTHER_KEY;
        const color = colorFor(p.key, i);
        const pct = (p.cost / total) * 100;
        const w = Math.max((p.cost / top) * 100, 1.5);
        const label = basenameOf(p.bucket_path);
        return (
          <button
            key={p.key}
            type="button"
            className="projects-ranked-row"
            data-series-key={p.key}
            title={p.bucket_path}
            disabled={isOther}
            onClick={() => {
              if (!isOther) onProjectSelect?.(p.key);
            }}
          >
            <span className="rk-label">{label}</span>
            <span className="rk-track">
              <span className="rk-fill" style={{ width: `${w}%`, background: color }} />
            </span>
            <span className="rk-val">
              ${p.cost.toFixed(2)} · {pct.toFixed(pct < 10 ? 1 : 0)}%
            </span>
          </button>
        );
      })}
    </div>
  );
}
