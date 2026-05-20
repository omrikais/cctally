// ProjectsTrendChart — geometry coverage for the 1-week degenerate
// render case (issue #68). Spec §3.3 doesn't anticipate weekCount === 1,
// and the original `xFor` collapsed every point to `VW/2`, drawing each
// polygon as a zero-width vertical line.
//
// Path A from the issue: synthesize a horizontal span across
// [VW*0.1, VW*0.9] so each series renders as a rectangle (wide stacked
// "bar") instead of a line.
import { fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ProjectsTrendChart } from './ProjectsTrendChart';
import type { ProjectsTrendEnvelope } from '../types/envelope';
import { stubMobileMedia } from '../test-utils/mobileMedia';

const VW = 400;
const EXPECTED_LEFT = VW * 0.1; // 40
const EXPECTED_RIGHT = VW * 0.9; // 360

function buildOneWeekTrend(): ProjectsTrendEnvelope {
  return {
    window_weeks: 1,
    weeks: [
      {
        week_start_date: '2026-05-13',
        week_label: 'wk0',
        total_cost_usd: 30,
        total_pct: 5,
      },
    ],
    projects: [
      {
        key: 'p-a',
        bucket_path: '/repos/p-a',
        weekly_cost: [20],
        weekly_pct: [3],
        sessions_per_week: [2],
        first_seen_per_week: ['2026-05-13T01:00:00Z'],
        last_seen_per_week: ['2026-05-13T23:00:00Z'],
      },
      {
        key: 'p-b',
        bucket_path: '/repos/p-b',
        weekly_cost: [10],
        weekly_pct: [2],
        sessions_per_week: [1],
        first_seen_per_week: ['2026-05-13T02:00:00Z'],
        last_seen_per_week: ['2026-05-13T22:00:00Z'],
      },
    ],
  };
}

function parseXs(points: string): number[] {
  return points
    .trim()
    .split(/\s+/)
    .map((pair) => Number.parseFloat(pair.split(',')[0]!));
}

function buildSixProjectTrend(): ProjectsTrendEnvelope {
  return {
    window_weeks: 4,
    weeks: Array.from({ length: 4 }, (_, j) => ({
      week_start_date: `2026-04-${String(j + 1).padStart(2, '0')}`,
      week_label: `wk${j}`,
      total_cost_usd: 100,
      total_pct: 20,
    })),
    projects: Array.from({ length: 6 }, (_, i) => ({
      key: `legend-project-${i + 1}`,
      bucket_path: `/repos/legend-project-${i + 1}`,
      weekly_cost: [10 + i, 11 + i, 12 + i, 13 + i],
      weekly_pct: [1, 2, 3, 4],
      sessions_per_week: [1, 1, 1, 1],
      first_seen_per_week: [null, null, null, null],
      last_seen_per_week: [null, null, null, null],
    })),
  };
}

describe('<ProjectsTrendChart /> 1-week render (issue #68)', () => {
  it('spreads single-week polygons across a non-trivial horizontal extent', () => {
    const { container } = render(
      <ProjectsTrendChart
        trend={buildOneWeekTrend()}
        yMode="absolute"
        windowWeeks={1}
      />,
    );
    const polygons = container.querySelectorAll('svg polygon');
    expect(polygons.length).toBeGreaterThanOrEqual(2);
    polygons.forEach((poly) => {
      const xs = parseXs(poly.getAttribute('points') ?? '');
      const xMin = Math.min(...xs);
      const xMax = Math.max(...xs);
      // Old (broken) behavior collapsed every x to VW/2 (= 200) so xMax
      // - xMin was 0; assert a substantial span instead.
      expect(xMax - xMin).toBeGreaterThan(VW * 0.5);
      // Span must be anchored to the synthesized [VW*0.1, VW*0.9] edges
      // so the chart visually fills the SVG instead of floating mid-frame.
      expect(xMin).toBeCloseTo(EXPECTED_LEFT, 2);
      expect(xMax).toBeCloseTo(EXPECTED_RIGHT, 2);
    });
  });

  it('emits closed quads (>= 4 points) for each series under weekCount === 1', () => {
    // A line of 2 points renders as zero-area; a rectangle needs at
    // least 4 corners. Guards against a regression that drops back to
    // the 2-point-per-polygon shape.
    const { container } = render(
      <ProjectsTrendChart
        trend={buildOneWeekTrend()}
        yMode="absolute"
        windowWeeks={1}
      />,
    );
    container.querySelectorAll('svg polygon').forEach((poly) => {
      const xs = parseXs(poly.getAttribute('points') ?? '');
      expect(xs.length).toBeGreaterThanOrEqual(4);
    });
  });

  it('leaves multi-week geometry untouched (weekCount === 4 spans full width)', () => {
    const fourWeek: ProjectsTrendEnvelope = {
      window_weeks: 4,
      weeks: Array.from({ length: 4 }, (_, j) => ({
        week_start_date: `2026-04-${String(j + 1).padStart(2, '0')}`,
        week_label: `wk${j}`,
        total_cost_usd: 10 + j,
        total_pct: 1 + j,
      })),
      projects: [
        {
          key: 'p-a',
          bucket_path: '/repos/p-a',
          weekly_cost: [10, 11, 12, 13],
          weekly_pct: [1, 2, 3, 4],
          sessions_per_week: [1, 1, 1, 1],
          first_seen_per_week: [null, null, null, null],
          last_seen_per_week: [null, null, null, null],
        },
      ],
    };
    const { container } = render(
      <ProjectsTrendChart trend={fourWeek} yMode="absolute" windowWeeks={4} />,
    );
    const poly = container.querySelector('svg polygon');
    const xs = parseXs(poly?.getAttribute('points') ?? '');
    // Multi-week path should still anchor x=0 (j=0) and x=VW (j=3).
    expect(Math.min(...xs)).toBeCloseTo(0, 2);
    expect(Math.max(...xs)).toBeCloseTo(VW, 2);
  });
});

describe('<ProjectsTrendChart /> — mobile legend (D2)', () => {
  beforeEach(() => stubMobileMedia(true));
  afterEach(() => vi.restoreAllMocks());

  it('renders 6 legend items: top-5 projects + (other)', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSixProjectTrend()} yMode="absolute" windowWeeks={4} />,
    );
    const items = container.querySelectorAll(
      '.projects-trend-legend > .projects-trend-legend-item',
    );
    expect(items.length).toBe(6);
  });

  it('pins (other) as the last legend item', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSixProjectTrend()} yMode="absolute" windowWeeks={4} />,
    );
    const items = Array.from(
      container.querySelectorAll('.projects-trend-legend > .projects-trend-legend-item'),
    );
    const lastKey = items.at(-1)?.getAttribute('data-series-key');
    expect(lastKey).toBe('(other)');
  });

  it('renders non-(other) items as <button> and (other) as <span>', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSixProjectTrend()} yMode="absolute" windowWeeks={4} />,
    );
    const items = Array.from(
      container.querySelectorAll('.projects-trend-legend > .projects-trend-legend-item'),
    );
    items.forEach((el) => {
      const key = el.getAttribute('data-series-key');
      const tag = el.tagName.toLowerCase();
      if (key === '(other)') {
        expect(tag).toBe('span');
      } else {
        expect(tag).toBe('button');
      }
    });
  });

  it('clicking a non-(other) legend item calls onProjectSelect with its key', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <ProjectsTrendChart
        trend={buildSixProjectTrend()}
        yMode="absolute"
        windowWeeks={4}
        onProjectSelect={onSelect}
      />,
    );
    const firstButton = container.querySelector(
      '.projects-trend-legend > button.projects-trend-legend-item',
    ) as HTMLButtonElement | null;
    expect(firstButton).not.toBeNull();
    const key = firstButton!.getAttribute('data-series-key');
    fireEvent.click(firstButton!);
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(key);
  });

  it('clicking (other) does NOT call onProjectSelect', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <ProjectsTrendChart
        trend={buildSixProjectTrend()}
        yMode="absolute"
        windowWeeks={4}
        onProjectSelect={onSelect}
      />,
    );
    const otherSpan = container.querySelector(
      '.projects-trend-legend > span.projects-trend-legend-item[data-series-key="(other)"]',
    ) as HTMLElement | null;
    expect(otherSpan).not.toBeNull();
    fireEvent.click(otherSpan!);
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe('<ProjectsTrendChart /> — desktop legend (non-interactive)', () => {
  beforeEach(() => stubMobileMedia(false));
  afterEach(() => vi.restoreAllMocks());

  it('renders all legend items as <span> on desktop (no <button>s)', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSixProjectTrend()} yMode="absolute" windowWeeks={4} />,
    );
    const buttons = container.querySelectorAll('.projects-trend-legend > button');
    expect(buttons.length).toBe(0);
    const spans = container.querySelectorAll(
      '.projects-trend-legend > span.projects-trend-legend-item',
    );
    expect(spans.length).toBe(6);
  });
});
