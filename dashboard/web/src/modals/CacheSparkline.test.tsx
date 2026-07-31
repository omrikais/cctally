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

// ---------------------------------------------------------------------------
// #443 S1 F1 — a synthetic today row is not a measurement.
// ---------------------------------------------------------------------------

describe('<CacheSparkline /> unobserved today (#443 S1)', () => {
  it('omits an unobserved trailing day from the polyline', () => {
    const days = [
      { ...row('2026-07-31', 0), observed: false },   // newest-first
      row('2026-07-30', 71),
      row('2026-07-29', 68),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={69}
        today_marker_color="green"
        size="large"
      />,
    );
    const points = container.querySelector('polyline')!.getAttribute('points')!;
    // Three x-slots exist, but only two are plotted.
    expect(points.trim().split(/\s+/)).toHaveLength(2);
  });

  it('renders a dashed guide instead of a data point for an unobserved today', () => {
    const days = [{ ...row('2026-07-31', 0), observed: false }, row('2026-07-30', 71)];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="mini"
      />,
    );
    expect(container.querySelector('[data-testid="cr-spark-today-marker"]')).toBeNull();
    expect(container.querySelector('[data-testid="cr-spark-today-unobserved"]')).not.toBeNull();
  });

  it('keeps the unobserved value out of the auto-zoom domain', () => {
    const days = [
      { ...row('2026-07-31', 0), observed: false },
      row('2026-07-30', 71),
      row('2026-07-29', 69),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="large"
      />,
    );
    // The bottom axis label would read 0% if the synthetic zero reached the domain.
    expect(container.querySelector('.cr-spark-axis-bot')!.textContent).not.toBe('0%');
  });

  it('keeps the unobserved row x-slot so Today stays rightmost', () => {
    const days = [
      { ...row('2026-07-31', 0), observed: false },
      row('2026-07-30', 71),
      row('2026-07-29', 69),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="large"
      />,
    );
    const guide = container.querySelector('[data-testid="cr-spark-today-unobserved"]')!;
    // 800-wide viewBox, three slots -> the last slot is the right edge, inset
    // by half the guide's stroke width so the WHOLE stroke stays inside the
    // viewBox. A stroke centred on x=800 is half-clipped by the SVG's overflow
    // and the surviving sliver reads as the chart frame's border, not a marker.
    expect(guide.getAttribute('x1')).toBe('799.5');
    expect(guide.getAttribute('x2')).toBe('799.5');
    // The polyline therefore stops short of the right edge.
    const points = container.querySelector('polyline')!.getAttribute('points')!;
    expect(points).not.toContain('800.0,');
  });

  it('insets the mini guide by the same half-stroke', () => {
    const days = [
      { ...row('2026-07-31', 0), observed: false },
      row('2026-07-30', 71),
      row('2026-07-29', 69),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="mini"
      />,
    );
    // 272-wide viewBox, same 1-unit stroke.
    expect(
      container.querySelector('[data-testid="cr-spark-today-unobserved"]')!.getAttribute('x1'),
    ).toBe('271.5');
  });

  it('leaves the observed today marker on the un-inset data x (no data point moved)', () => {
    const days = [row('2026-07-31', 73), row('2026-07-30', 71), row('2026-07-29', 69)];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="large"
      />,
    );
    // The inset is a guide-only concern: the today circle and the polyline's
    // last plotted coordinate both stay on the raw x-slot.
    expect(
      container.querySelector('[data-testid="cr-spark-today-marker"]')!.getAttribute('cx'),
    ).toBe('800');
    expect(container.querySelector('polyline')!.getAttribute('points')!).toContain('800.0,');
  });

  it('counts only observed days in the accessible label', () => {
    const days = [
      { ...row('2026-07-31', 0), observed: false },
      row('2026-07-30', 71),
      row('2026-07-29', 69),
    ];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="large"
      />,
    );
    expect(container.querySelector('svg.cr-spark')!.getAttribute('aria-label'))
      .toBe('Cache hit % timeline, 2 days');
  });
});
