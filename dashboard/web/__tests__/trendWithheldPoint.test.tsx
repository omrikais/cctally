// #620 S1 — a withheld $/1% leaves the chart rather than sitting on the axis.
//
// `bin/_lib_share_templates._optional_chart_points` already omits a point whose
// value was withheld, "because a withheld value plotted at zero draws a cliff
// to the axis that reads as a measured collapse", keeping the x position so the
// gap sits where the missing week actually is. The live panel did the opposite:
// `spark_height: row.dollar_per_pct ?? 0` fabricated a zero, and the shared
// artifact and the panel therefore described the same absence two different
// ways.
//
// The rule the client applies: `dollar_per_pct` is the quantity the sparkline
// plots, and `spark_height` is only its normalisation — so a datum with no
// `dollar_per_pct` is withheld whatever height accompanies it. That covers the
// Claude path (server-supplied `spark_heights`, which floor a withheld week at
// 1) and the composed path (client-derived) with one predicate.
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { Sparkline } from '../src/components/Sparkline';
import type { TrendChartDatum } from '../src/store/selectors';

function datum(over: Partial<TrendChartDatum>): TrendChartDatum {
  return {
    label: 'wk',
    used_pct: 10,
    dollar_per_pct: 1,
    delta: null,
    is_current: false,
    ...over,
  };
}

describe('#620 S1 — the sparkline omits a withheld week', () => {
  const rows: TrendChartDatum[] = [
    datum({ label: 'w1', dollar_per_pct: 2, spark_height: 2 }),
    // Withheld. The server floors its `spark_height` at 1 rather than nulling
    // it, so a height-only predicate could not tell this apart from a real
    // low week — which is why the predicate reads `dollar_per_pct`.
    datum({ label: 'w2', dollar_per_pct: null, spark_height: 1 }),
    datum({ label: 'w3', dollar_per_pct: 8, spark_height: 8 }),
  ];

  it('keeps the slot so the gap sits where the missing week is', () => {
    const { container } = render(<Sparkline data={rows} />);
    // One grid cell per week, still — the x position of every remaining week
    // is unchanged, exactly as `_optional_chart_points` keeps its index.
    expect(container.querySelectorAll('.bar').length).toBe(3);
  });

  it('draws no bar for the withheld week', () => {
    const { container } = render(<Sparkline data={rows} />);
    const bars = Array.from(container.querySelectorAll('.bar')) as HTMLElement[];
    expect(bars[1].classList.contains('is-withheld')).toBe(true);
    expect(bars[0].classList.contains('is-withheld')).toBe(false);
    expect(bars[2].classList.contains('is-withheld')).toBe(false);
    // Precondition asserted unconditionally: the other two DID draw, so the
    // assertion above is about this week and not about an empty chart.
    expect(bars[0].style.height).not.toBe('');
    expect(bars[2].style.height).not.toBe('');
  });

  it('an all-withheld window draws nothing, not a flat line near the axis', () => {
    // The defect this test pins is what the old code produced: three heights of
    // 0, a `Math.max(1, 0, 0, 0)` denominator, and `Math.max(6, 0)` floors —
    // three equal visible bars that read as three measured, nearly-zero weeks.
    const allWithheld = rows.map((r) => ({
      ...r, dollar_per_pct: null, spark_height: 0,
    }));
    const { container } = render(<Sparkline data={allWithheld} />);
    const bars = Array.from(container.querySelectorAll('.bar')) as HTMLElement[];
    expect(bars.length).toBe(3);
    for (const bar of bars) {
      expect(bar.classList.contains('is-withheld')).toBe(true);
      expect(bar.style.height).toBe('');
    }
  });

  it('names the empty slot on hover, while the gap itself is the disclosure', () => {
    // The gap is what a touch user sees, and it is the disclosure — the
    // `title` is an extra hint for a pointer, never the only statement.
    const { container } = render(<Sparkline data={rows} />);
    const bars = Array.from(container.querySelectorAll('.bar')) as HTMLElement[];
    expect(bars[1].getAttribute('title')).toBeTruthy();
  });
});
