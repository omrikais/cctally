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
import { ProjectsTrendChart, projectsAxisLabels } from './ProjectsTrendChart';
import type { ProjectsTrendEnvelope } from '../types/envelope';
import { stubMobileMedia } from '../test-utils/mobileMedia';

const VW = 400;
const EXPECTED_LEFT = VW * 0.1; // 40
const EXPECTED_RIGHT = VW * 0.9; // 360

// Kept deliberately BALANCED (no single project >= 60% of the window
// total) so it routes through the stacked-area path — the geometry these
// #68 tests assert. p-a=18, p-b=16 -> 18/34 ~= 0.53 < DOMINANCE_THRESHOLD.
function buildOneWeekTrend(): ProjectsTrendEnvelope {
  return {
    window_weeks: 1,
    weeks: [
      {
        week_start_date: '2026-05-13',
        week_label: 'wk0',
        total_cost_usd: 34,
        total_pct: 5,
      },
    ],
    projects: [
      {
        key: 'p-a',
        bucket_path: '/repos/p-a',
        weekly_cost: [18],
        weekly_pct: [3],
        sessions_per_week: [2],
        first_seen_per_week: ['2026-05-13T01:00:00Z'],
        last_seen_per_week: ['2026-05-13T23:00:00Z'],
      },
      {
        key: 'p-b',
        bucket_path: '/repos/p-b',
        weekly_cost: [16],
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
    // Two balanced projects (each ~50% of the total) so this stays on the
    // stacked-area path (a lone project is 100%-dominant and would render
    // ranked bars instead of the polygons this test asserts).
    const fourWeek: ProjectsTrendEnvelope = {
      window_weeks: 4,
      weeks: Array.from({ length: 4 }, (_, j) => ({
        week_start_date: `2026-04-${String(j + 1).padStart(2, '0')}`,
        week_label: `wk${j}`,
        total_cost_usd: 20 + 2 * j,
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
        {
          key: 'p-b',
          bucket_path: '/repos/p-b',
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

// PR-2 conditional swap: a dominant distribution renders ranked bars
// (not stacked-area polygons); a balanced distribution keeps the stacked
// area AND now shows y-axis labels.
function buildDominantTrend(): ProjectsTrendEnvelope {
  return {
    window_weeks: 1,
    weeks: [
      { week_start_date: '2026-06-01', week_label: 'W0', total_cost_usd: 100, total_pct: 20 },
    ],
    projects: [
      { key: 'cctally-dev', bucket_path: '/repos/cctally-dev', weekly_cost: [92],
        weekly_pct: [null], sessions_per_week: [1], first_seen_per_week: [null], last_seen_per_week: [null] },
      { key: 'superpowers', bucket_path: '/repos/superpowers', weekly_cost: [3],
        weekly_pct: [null], sessions_per_week: [1], first_seen_per_week: [null], last_seen_per_week: [null] },
      { key: 'misc', bucket_path: '/repos/misc', weekly_cost: [5],
        weekly_pct: [null], sessions_per_week: [1], first_seen_per_week: [null], last_seen_per_week: [null] },
    ],
  };
}

describe('<ProjectsTrendChart /> — PR-2 conditional swap (#250)', () => {
  beforeEach(() => stubMobileMedia(false));
  afterEach(() => vi.restoreAllMocks());

  it('renders ranked bars (not stacked-area polygons) under a dominant distribution', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildDominantTrend()} yMode="absolute" windowWeeks={1} />,
    );
    expect(container.querySelector('[data-testid="projects-ranked-bars"]')).not.toBeNull();
    // Non-vacuous: the stacked-area SVG is gone in dominant mode.
    expect(container.querySelector('polygon')).toBeNull();
  });

  it('renders the stacked area WITH y-axis labels under a balanced distribution', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSixProjectTrend()} yMode="absolute" windowWeeks={4} />,
    );
    expect(container.querySelector('polygon')).not.toBeNull();
    expect(container.querySelector('[data-testid="projects-yaxis"]')).not.toBeNull();
  });
});

// #750 S4 §3.5 / D5: a credited week's two cycles share `week_start_date`,
// the billing-cycle join key. Keying the x-axis on it gave React duplicate
// keys, which a production build compiles the warning out of — so the keys
// are read directly rather than inferred from console output.
function buildSameDayCreditedTrend(): ProjectsTrendEnvelope {
  const week = (startAt: string, label: string, cost: number, pct: number) => ({
    week_start_date: '2026-05-15',
    week_start_at: startAt,
    week_label: label,
    total_cost_usd: cost,
    total_pct: pct,
  });
  return {
    window_weeks: 3,
    weeks: [
      week('2026-05-15T02:00:00Z', 'May 15', 30, 30),
      week('2026-05-15T08:00:00Z', 'May 15', 20, 20),
      week('2026-05-15T14:00:00Z', 'May 15', 10, 10),
    ],
    projects: [
      { key: 'p-a', bucket_path: '/repos/p-a', weekly_cost: [16, 11, 5],
        weekly_pct: [null, null, null], sessions_per_week: [1, 1, 1],
        first_seen_per_week: [null, null, null], last_seen_per_week: [null, null, null] },
      { key: 'p-b', bucket_path: '/repos/p-b', weekly_cost: [14, 9, 5],
        weekly_pct: [null, null, null], sessions_per_week: [1, 1, 1],
        first_seen_per_week: [null, null, null], last_seen_per_week: [null, null, null] },
    ],
  };
}

function xAxisFiberKeys(container: HTMLElement): (string | null)[] {
  const axis = container.querySelector('.projects-trend-xaxis')
    ?? container.querySelector('[data-testid="projects-xaxis"]');
  const host = (axis ?? container) as HTMLElement & Record<string, unknown>;
  const spans = Array.from(host.querySelectorAll('span'));
  return spans.map((el) => {
    const fiberKey = Object.keys(el).find((k) => k.startsWith('__reactFiber$'));
    if (!fiberKey) return null;
    return ((el as unknown as Record<string, { key: string | null }>)[fiberKey]).key;
  });
}

describe('<ProjectsTrendChart /> — credited-week x-axis identity (#750 S4)', () => {
  beforeEach(() => stubMobileMedia(false));
  afterEach(() => vi.restoreAllMocks());

  it('renders distinct React fiber keys for three same-day cycles', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSameDayCreditedTrend()} yMode="absolute" windowWeeks={3} />,
    );
    const keys = xAxisFiberKeys(container).filter((k): k is string => k !== null);
    expect(keys.length).toBeGreaterThanOrEqual(3);
    expect(new Set(keys).size).toBe(keys.length);
  });

  // §3.2a. The keys were already distinct; the LABELS were not, so a reader
  // saw three identical `May 15` columns. `useDisplayTz` falls back to Etc/UTC
  // with no snapshot, so the rendered times are the seeded UTC instants.
  it('suffixes colliding x-axis labels with each cycle own time', () => {
    const { container } = render(
      <ProjectsTrendChart trend={buildSameDayCreditedTrend()} yMode="absolute" windowWeeks={3} />,
    );
    const text = Array.from(container.querySelectorAll('span')).map((el) => el.textContent);
    expect(text).toContain('May 15 02:00');
    expect(text).toContain('May 15 08:00');
    expect(text).toContain('May 15 14:00');
    expect(text).not.toContain('May 15');
  });

  it('leaves non-colliding labels byte-identical', () => {
    const trend = buildSameDayCreditedTrend();
    // Move the INSTANTS apart, not just the labels: the axis derives its date
    // from `week_start_at`, so relabelling alone would still collide.
    trend.weeks = trend.weeks.map((w, i) => ({
      ...w,
      week_label: `May 1${5 + i}`,
      week_start_at: `2026-05-1${5 + i}T02:00:00Z`,
    }));
    const { container } = render(
      <ProjectsTrendChart trend={trend} yMode="absolute" windowWeeks={3} />,
    );
    const text = Array.from(container.querySelectorAll('span')).map((el) => el.textContent);
    expect(text).toContain('May 15');
    expect(text).toContain('May 16');
    expect(text).toContain('May 17');
    expect(text.some((t) => (t ?? '').includes(':'))).toBe(false);
  });

  // The date and the time must come from ONE instant in ONE zone. Appending a
  // display-zone time to the wire's UTC `week_label` rendered `Apr 17 20:00`
  // for an instant whose Los Angeles date is Apr 16, so the axis ran backwards.
  it('derives the date in the display zone, not from the wire label', () => {
    const weeks = buildSameDayCreditedTrend().weeks;
    // 02:00Z is May 14 in Los Angeles; 08:00Z and 14:00Z are both May 15, so
    // only the genuine pair collides and the lone cycle keeps a bare date.
    expect(projectsAxisLabels(weeks, 'America/Los_Angeles'))
      .toEqual(['May 14', 'May 15 01:00', 'May 15 07:00']);
    // The same three instants all fall on one UTC date, so all three collide.
    expect(projectsAxisLabels(weeks, 'Etc/UTC'))
      .toEqual(['May 15 02:00', 'May 15 08:00', 'May 15 14:00']);
  });

  it('falls back to the wire label when an instant is absent', () => {
    expect(projectsAxisLabels([{ week_label: 'wk0' }], 'Etc/UTC')).toEqual(['wk0']);
  });

  it('still renders when an older envelope omits week_start_at', () => {
    const trend = buildSameDayCreditedTrend();
    trend.weeks = trend.weeks.map((w) => {
      const { week_start_at: _drop, ...rest } = w;
      return rest;
    });
    const { container } = render(
      <ProjectsTrendChart trend={trend} yMode="absolute" windowWeeks={3} />,
    );
    expect(container.querySelector('.projects-trend')).not.toBeNull();
  });
});
