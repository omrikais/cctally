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
import { computeAutoZoomDomain } from '../lib/chartDomain';
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
    anomaly_reasons: [],
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
    // The large variant now auto-zooms (CR-1, #250): the labels track the
    // computed domain of SAMPLE (median null → fit to the points), not a
    // fixed 100%/0%.
    const expected = computeAutoZoomDomain([65, 70, 98, 96], null, 5);
    expect(top?.textContent).toBe(`${Math.round(expected.hi)}%`);
    expect(bot?.textContent).toBe(`${Math.round(expected.lo)}%`);
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

  it('size=mini renders width="100%" with preserveAspectRatio="none" for edge-to-edge fill (issue #77 P2-4 Round 2)', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="mini"
      />,
    );
    const svg = container.querySelector('svg.cr-spark') as SVGSVGElement;
    expect(svg.getAttribute('width')).toBe('100%');
    expect(svg.getAttribute('height')).toBe('32');
    expect(svg.getAttribute('preserveAspectRatio')).toBe('none');
    // ViewBox keeps the polyline coordinate math (0..272 x, 0..32 y).
    expect(svg.getAttribute('viewBox')).toBe('0 0 272 32');
    // No wrapper, no axis labels for the panel variant.
    expect(container.querySelector('.cr-spark-wrap')).toBeNull();
    expect(container.querySelector('.cr-spark-axis-top')).toBeNull();
  });

  it('size=mini omits the large-only horizontal gridlines', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="mini"
      />,
    );
    expect(
      container.querySelectorAll('[data-testid^="cr-spark-gridline-"]').length,
    ).toBe(0);
  });

  it('size=large renders 3 horizontal gridlines (hi/mid/lo of the zoomed domain, CR-1 #250)', () => {
    const { container } = render(
      <CacheSparkline
        days={SAMPLE}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    // Fixed 0/25/50/75/100 gridlines are gone — the large variant now
    // draws exactly three lines at the zoomed domain's hi / mid / lo.
    expect(
      container.querySelectorAll('[data-testid^="cr-spark-gridline-"]').length,
    ).toBe(3);
    ['hi', 'mid', 'lo'].forEach((k) => {
      expect(
        container.querySelector(`[data-testid="cr-spark-gridline-${k}"]`),
      ).toBeTruthy();
    });
    // Bounds (hi/lo) are solid; the mid cue is dashed and lower-alpha.
    const boundStroke = container
      .querySelector('[data-testid="cr-spark-gridline-hi"]')
      ?.getAttribute('stroke');
    const midStroke = container
      .querySelector('[data-testid="cr-spark-gridline-mid"]')
      ?.getAttribute('stroke');
    expect(boundStroke).toMatch(/rgba\(255,255,255,0\.4/);
    expect(midStroke).toMatch(/rgba\(255,255,255,0\.1/);
    expect(
      container
        .querySelector('[data-testid="cr-spark-gridline-mid"]')
        ?.getAttribute('stroke-dasharray'),
    ).toBe('4,3');
    expect(
      container
        .querySelector('[data-testid="cr-spark-gridline-hi"]')
        ?.getAttribute('stroke-dasharray'),
    ).toBeNull();
  });
});

describe('<CacheSparkline /> size=large auto-zoom (CR-1, #250)', () => {
  it('labels the zoomed bottom to the data band, not the fixed 0%', () => {
    // Cache-realistic clustered-high fixture (~96-98%, median 97.4). The
    // median +/-5pp band pushes the top to exactly 100 (clipped at the
    // valid bound), so the load-bearing non-vacuous guard is the BOTTOM
    // axis: a fixed 0-100 domain would label it '0%'; the auto-zoom lifts
    // the floor to domain.lo (~88), matching spec CR-1's `domain.lo > 50`.
    const days = [
      row('2026-06-10', 97.2),
      row('2026-06-11', 96.8),
      row('2026-06-12', 98.1),
      row('2026-06-13', 97.5),
      row('2026-06-14', 96.2),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={97.4}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    // days[] is newest-first and reversed internally; min/max are order-free.
    const expected = computeAutoZoomDomain(
      [96.2, 97.5, 98.1, 96.8, 97.2],
      97.4,
      5,
    );
    const bot = container.querySelector('.cr-spark-axis-bot')!.textContent!;
    const top = container.querySelector('.cr-spark-axis-top')!.textContent!;
    expect(bot).not.toBe('0%'); // zoomed — the non-vacuous guard
    expect(bot).toBe(`${Math.round(expected.lo)}%`);
    expect(top).toBe(`${Math.round(expected.hi)}%`);
  });

  it('labels the zoomed top below 100% on a mid-range band', () => {
    // A mid-range cluster (~68-72, median 70) keeps the band inside
    // [0,100], so BOTH axis labels are driven by the domain — proving the
    // top label is dynamic (not a hardcoded 100%).
    const days = [
      row('2026-06-10', 70),
      row('2026-06-11', 72),
      row('2026-06-12', 68),
      row('2026-06-13', 71),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    const expected = computeAutoZoomDomain([71, 68, 72, 70], 70, 5);
    const top = container.querySelector('.cr-spark-axis-top')!.textContent!;
    const bot = container.querySelector('.cr-spark-axis-bot')!.textContent!;
    expect(top).not.toBe('100%');
    expect(bot).not.toBe('0%');
    expect(top).toBe(`${Math.round(expected.hi)}%`);
    expect(bot).toBe(`${Math.round(expected.lo)}%`);
  });

  it('mini variant is unchanged (no auto-zoom, no axis labels)', () => {
    const { container } = render(
      <CacheSparkline
        days={[row('2026-06-10', 97)]}
        baseline_median_percent={97}
        today_marker_color="var(--accent-green)"
        size="mini"
      />,
    );
    expect(container.querySelector('.cr-spark-axis-top')).toBeNull();
  });
});
