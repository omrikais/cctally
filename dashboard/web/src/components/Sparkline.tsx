import type { TrendChartDatum } from '../store/selectors';

// Panel sparkline: plain <div class="bar"> children with inline `height`
// percent based on the max spark_height. The newest bar is tinted white-
// on-purple; index.css owns layout/color rules on .trend-spark > .bar.
// The Trend modal's SVG dual-line chart lives in TrendModal.tsx.

interface Props {
  data: TrendChartDatum[];
}

// #620 S1 — a withheld week leaves the chart rather than sitting on the axis.
//
// `bin/_lib_share_templates._optional_chart_points` already omits such a point
// from the shared artifact, "because a withheld value plotted at zero draws a
// cliff to the axis that reads as a measured collapse", and keeps the x
// position so the gap sits where the missing week is. This is the same rule on
// the live panel, so the artifact and the panel describe one absence one way.
//
// The predicate reads `dollar_per_pct`, not the height: `dollar_per_pct` is the
// quantity the chart plots and `spark_height` is only its normalisation, and
// `_lib_view_models` floors a withheld week's height at 1 rather than nulling
// it — so a height-only predicate could not tell a withheld week from a real
// cheap one.
const WITHHELD_TITLE = 'No $/1% for this week — the rate was not measured.';

export function Sparkline({ data }: Props) {
  const withheld = data.map((d) => d.dollar_per_pct == null);
  const heights = data.map((d) => d.spark_height ?? 0);
  // Only measured weeks set the scale. A fabricated zero in the denominator
  // would rescale every remaining bar against a value nothing measured.
  const measured = heights.filter((_, i) => !withheld[i]);
  const max = Math.max(1, ...measured);
  return (
    <>
      {heights.map((h, i) => {
        const isLast = i === heights.length - 1;
        if (withheld[i]) {
          // The slot is kept so the surviving weeks hold their positions and
          // the gap lands on the week that is actually missing.
          return (
            <div
              key={data[i].label + '|' + i}
              className="bar is-withheld"
              title={WITHHELD_TITLE}
            />
          );
        }
        const style: React.CSSProperties = {
          height: `${Math.max(6, (h / max) * 100)}%`,
        };
        if (isLast) {
          style.background =
            'color-mix(in srgb, var(--accent-purple) 70%, white 30%)';
        }
        return (
          <div
            key={data[i].label + '|' + i}
            className="bar"
            style={style}
          />
        );
      })}
    </>
  );
}
