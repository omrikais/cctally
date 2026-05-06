import type { TrendChartDatum } from '../store/selectors';

// Panel sparkline: plain <div class="bar"> children with inline `height`
// percent based on the max spark_height. The newest bar is tinted white-
// on-purple; index.css owns layout/color rules on .trend-spark > .bar.
// The Trend modal's SVG dual-line chart lives in TrendModal.tsx.

interface Props {
  data: TrendChartDatum[];
}

export function Sparkline({ data }: Props) {
  const heights = data.map((d) => d.spark_height ?? 0);
  const max = Math.max(1, ...heights);
  return (
    <>
      {heights.map((h, i) => {
        const isLast = i === heights.length - 1;
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
