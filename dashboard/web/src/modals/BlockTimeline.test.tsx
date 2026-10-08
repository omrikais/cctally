import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BlockTimeline } from './BlockTimeline';
import { _resetForTests, updateSnapshot } from '../store/store';
import type { BlockDetail, Envelope } from '../types/envelope';
import fixture from '../../e2e/fixtures/block-timeline-layout.json';

const DETAIL = fixture as BlockDetail;

beforeEach(() => {
  _resetForTests();
  updateSnapshot({ generated_at: '2026-04-20T14:00:00Z' } as Envelope);
});

function points(container: HTMLElement, selector = 'polyline:not([stroke-dasharray])') {
  const line = container.querySelector(selector);
  expect(line).not.toBeNull();
  return line!.getAttribute('points')!.split(' ').map((point) => point.split(',').map(Number));
}

describe('BlockTimeline vertical range', () => {
  it.each([
    { name: 'last sample above the retained total', values: [6, 9, 13, 17.81] },
    { name: 'earlier sample above both the final sample and retained total', values: [6, 22, 13, 17.81] },
  ])('fits every unchanged sample when $name', ({ values }) => {
    const detail: BlockDetail = {
      ...DETAIL,
      samples: DETAIL.samples.map((sample, i) => ({ ...sample, cum: values[i] })),
    };
    const original = structuredClone(detail);
    const { container } = render(<BlockTimeline detail={detail} />);
    const plotted = points(container);
    expect(plotted).toHaveLength(values.length + 1);
    // Membership/order and timestamp positions stay fixed, including the
    // initial zero. The range changes; the money is never clamped to the total.
    expect(plotted.map(([x]) => x)).toEqual([46, 111.4, 209.5, 307.6, 536.5]);
    for (const [, y] of plotted) {
      expect(y).toBeGreaterThanOrEqual(18);
      expect(y).toBeLessThanOrEqual(160);
    }
    const maximumIndex = values.indexOf(Math.max(...values)) + 1;
    expect(plotted[maximumIndex][1]).toBe(18);
    expect(plotted.at(-1)![1]).toBeCloseTo(160 - values.at(-1)! / Math.max(...values) * 142, 1);
    expect(screen.getByText(/trajectory is drawn/)).toHaveTextContent(`totals $${values.at(-1)!.toFixed(2)}`);
    expect(detail).toEqual(original);
  });

  it('keeps the retained total as the range ceiling when it exceeds every sample', () => {
    const { container } = render(<BlockTimeline detail={{ ...DETAIL, cost_usd: 41.5 }} />);
    expect(points(container).at(-1)![1]).toBeCloseTo(160 - 17.81 / 41.5 * 142, 1);
    expect(container.querySelector('svg text')!.textContent).toBe('$0');
    expect(screen.getByText('$42')).toBeTruthy();
  });

  it('keeps a larger active projection in range without changing the cumulative or ghost endpoints', () => {
    const { container } = render(<BlockTimeline detail={{
      ...DETAIL,
      is_active: true,
      projection: { total_cost_usd: 46.5, total_tokens: 19_300_000, remaining_minutes: 60 },
    }} />);
    const cumulative = points(container);
    const projection = points(container, 'polyline[stroke-dasharray]');
    expect(cumulative).toHaveLength(DETAIL.samples.length + 2);
    expect(cumulative.at(-1)).toEqual([569.2, 105.6]);
    expect(projection).toEqual([[569.2, 105.6], [700, 18]]);
    expect(screen.getByText('proj $46.50')).toBeTruthy();
  });

  it('renders all-zero samples finitely at the baseline with one zero tick', () => {
    const { container } = render(<BlockTimeline detail={{
      ...DETAIL, cost_usd: 0,
      samples: DETAIL.samples.map((sample) => ({ ...sample, cum: 0 })),
    }} />);
    expect(points(container).map(([, y]) => y)).toEqual([160, 160, 160, 160, 160]);
    expect(screen.getAllByText('$0')).toHaveLength(1);
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it.each(['retained', 'computed'] as const)('preserves the defensive empty %s state without drawing a trajectory', (factsSource) => {
    const { container } = render(<BlockTimeline detail={{
      ...DETAIL, facts_source: factsSource, cost_usd: 0, samples: [],
    }} />);
    expect(container.querySelector('polyline')).toBeNull();
    expect(screen.getByText(factsSource === 'retained'
      ? 'Cost trajectory unavailable for this block.'
      : 'No spend recorded yet in this block.')).toBeTruthy();
    expect(container.innerHTML).not.toMatch(/NaN|Infinity/);
  });
});
