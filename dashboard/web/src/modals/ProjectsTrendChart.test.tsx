// ProjectsTrendChart — geometry coverage for the 1-week degenerate
// render case (issue #68). Spec §3.3 doesn't anticipate weekCount === 1,
// and the original `xFor` collapsed every point to `VW/2`, drawing each
// polygon as a zero-width vertical line.
//
// Path A from the issue: synthesize a horizontal span across
// [VW*0.1, VW*0.9] so each series renders as a rectangle (wide stacked
// "bar") instead of a line.
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ProjectsTrendChart } from './ProjectsTrendChart';
import type { ProjectsTrendEnvelope } from '../types/envelope';

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
