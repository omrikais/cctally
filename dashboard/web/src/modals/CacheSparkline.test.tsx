// CacheSparkline regression tests for issue #77 P2-1 and P2-2.
//
// - P2-1: size='large' must render width='100%' (responsive) so the
//   modal-body doesn't overflow at viewports < 800 px.
// - P2-2: axis labels '100%' / '0%' must render as HTML siblings of
//   the SVG (not <text> nodes inside the SVG) so the polyline at high
//   cache-hit % can't collide with the '100%' text.
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { CacheSparkline } from './CacheSparkline';
import type { CacheReportDailyRow } from '../types/envelope';

function row(date: string, pct: number): CacheReportDailyRow {
  return {
    date,
    cache_hit_percent: pct,
    input_tokens: 1_000_000,
    output_tokens: 100_000,
    cache_creation_tokens: 50_000,
    cache_read_tokens: 800_000,
    saved_usd: 1.0,
    wasted_usd: 0.1,
    net_usd: 0.9,
    anomaly_triggered: false,
    anomaly_reasons: [] as string[],
  };
}

const SAMPLE = [
  row('2026-05-07', 65),
  row('2026-05-08', 70),
  row('2026-05-09', 98),
  row('2026-05-10', 96),
];

describe('<CacheSparkline /> size=large layout (issue #77 P2-1, P2-2)', () => {
  it('size=large renders an SVG with width="100%"', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    const svg = container.querySelector('svg.cr-spark') as SVGSVGElement;
    expect(svg).toBeTruthy();
    expect(svg.getAttribute('width')).toBe('100%');
  });

  it('size=large empty-data fallback also renders width="100%"', () => {
    const { container } = render(
      <CacheSparkline
        days={[]}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    const svg = container.querySelector('svg.cr-spark') as SVGSVGElement;
    expect(svg).toBeTruthy();
    expect(svg.getAttribute('width')).toBe('100%');
  });

  it('size=large axis labels render as HTML siblings, not <text> in SVG', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    // Wrapper exists.
    const wrap = container.querySelector('.cr-spark-wrap');
    expect(wrap).toBeTruthy();
    // Labels are HTML spans, not SVG <text>.
    const top = container.querySelector('.cr-spark-axis-top');
    const bot = container.querySelector('.cr-spark-axis-bot');
    expect(top?.tagName).toBe('SPAN');
    expect(bot?.tagName).toBe('SPAN');
    expect(top?.textContent).toBe('100%');
    expect(bot?.textContent).toBe('0%');
    // No SVG <text> elements inside the chart any more.
    const svgTexts = container.querySelectorAll('svg.cr-spark text');
    expect(svgTexts.length).toBe(0);
  });

  it('size=large preserves the viewBox so aspect ratio holds', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    const svg = container.querySelector('svg.cr-spark') as SVGSVGElement;
    expect(svg.getAttribute('viewBox')).toBe('0 0 800 90');
  });

  it('size=mini still uses fixed pixel width and no wrapper', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="mini"
      />,
    );
    const svg = container.querySelector('svg.cr-spark') as SVGSVGElement;
    expect(svg.getAttribute('width')).toBe('272');
    expect(svg.getAttribute('height')).toBe('32');
    // No wrapper, no axis labels for the panel variant.
    expect(container.querySelector('.cr-spark-wrap')).toBeNull();
    expect(container.querySelector('.cr-spark-axis-top')).toBeNull();
  });
});
