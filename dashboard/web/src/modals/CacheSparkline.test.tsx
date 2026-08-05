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
import { cacheReportChartSlot } from '../lib/cacheReportChartSlots';

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
    expect(svg.getAttribute('aria-label')).toBe('Cache hit % timeline, 4 days');
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

  it('keeps the unobserved row on the shared large Today slot', () => {
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
    const expected = cacheReportChartSlot('large', 2, 3).center;
    expect(Number(guide.getAttribute('x1'))).toBeCloseTo(expected, 6);
    expect(Number(guide.getAttribute('x2'))).toBeCloseTo(expected, 6);
    // The polyline therefore stops short of the right edge.
    const points = container.querySelector('polyline')!.getAttribute('points')!;
    expect(points).not.toContain(`${expected.toFixed(1)},`);
  });

  it('keeps the unobserved row on the shared mini Today slot', () => {
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
    expect(Number(
      container.querySelector('[data-testid="cr-spark-today-unobserved"]')!.getAttribute('x1'),
    )).toBeCloseTo(cacheReportChartSlot('mini', 2, 3).center, 6);
  });

  it('puts the observed marker and polyline on the same shared Today slot', () => {
    const days = [row('2026-07-31', 73), row('2026-07-30', 71), row('2026-07-29', 69)];
    const { container } = render(
      <CacheSparkline
        days={days}
        baseline_median_percent={70}
        today_marker_color="green"
        size="large"
      />,
    );
    const expected = cacheReportChartSlot('large', 2, 3).center;
    expect(Number(
      container.querySelector('[data-testid="cr-spark-today-marker"]')!.getAttribute('cx'),
    )).toBeCloseTo(expected, 6);
    expect(container.querySelector('polyline')!.getAttribute('points')!)
      .toContain(`${expected.toFixed(1)},`);
  });

  it('announces the full window and the measured subset in the accessible label (#469)', () => {
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
      .toBe('Cache hit % timeline, 3 days, 2 measured');
  });
});

// ---------------------------------------------------------------------------
// #443 S2 §4.3 — the percentage is read through `cachePercent`, never off
// `cache_hit_percent` directly. These assert GEOMETRY, not text: the sparkline
// uses raw percentages for its domain, its `points` string and its marker, so a
// row publishing neither key yields a NaN domain and a NaN-coordinate polyline
// while a text assertion elsewhere on the page still finds an em dash and
// passes over a visibly broken chart.
// ---------------------------------------------------------------------------

function rowWithoutPercent(date: string): CacheReportDailyRow {
  // Remove Claude's authoritative key to exercise the unresolved shape.
  const { cache_hit_percent: _drop, ...rest } = row(date, 0);
  void _drop;
  return rest as CacheReportDailyRow;
}

function codexRow(date: string, pct: number): CacheReportDailyRow {
  const { cache_hit_percent: _legacy, ...rest } = row(date, pct);
  void _legacy;
  return { ...rest, cached_input_percent: pct };
}

describe('<CacheSparkline /> #443 S2 nullable percent', () => {
  it('emits no NaN in the polyline or the domain when a row has no resolvable percent', () => {
    const { container } = render(
      <CacheSparkline
        days={[rowWithoutPercent('2026-05-11'), ...SAMPLE]}
        baseline_median_percent={70}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    const points = container.querySelector('polyline')!.getAttribute('points')!;
    expect(points).not.toContain('NaN');
    expect(points.split(' ')).toHaveLength(SAMPLE.length);
    const axes = Array.from(container.querySelectorAll('.cr-spark-axis'))
      .map((n) => n.textContent);
    expect(axes.join(' ')).not.toContain('NaN');
  });

  it('marks an unresolvable today without asserting a value', () => {
    const { container } = render(
      <CacheSparkline
        days={[rowWithoutPercent('2026-05-11'), ...SAMPLE]}
        baseline_median_percent={70}
        today_marker_color="var(--accent-green)"
        size="large"
      />,
    );
    // A filled dot would place a measurement where none exists; the guide
    // marks the slot instead, exactly as it does for an unobserved day.
    expect(container.querySelector('[data-testid="cr-spark-today-marker"]')).toBeNull();
    const guide = container.querySelector('[data-testid="cr-spark-today-unobserved"]')!;
    expect(guide).not.toBeNull();
    expect(guide.getAttribute('x1')).not.toContain('NaN');
  });

  it('plots the Codex authoritative percent, not the transitional alias', () => {
    // Non-vacuity for the accessor: a row whose two keys DISAGREE must follow
    // `cached_input_percent`. Equal values could not tell the two apart.
    const disagreeing = { ...codexRow('2026-05-11', 20), cache_hit_percent: 95 };
    const { container } = render(
      <CacheSparkline
        days={[disagreeing]}
        baseline_median_percent={null}
        today_marker_color="var(--accent-green)"
        size="large"
        source="codex"
      />,
    );
    const domain = computeAutoZoomDomain([20], null, 5);
    const axes = Array.from(container.querySelectorAll('.cr-spark-axis'))
      .map((n) => n.textContent);
    expect(axes[0]).toBe(`${Math.round(domain.hi)}%`);
    expect(axes[1]).toBe(`${Math.round(domain.lo)}%`);
  });
});
