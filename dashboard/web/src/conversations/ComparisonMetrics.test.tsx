import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { ComparisonMetrics } from './ComparisonMetrics';
import type { ComparisonMetrics as Metrics } from './comparisonMetricsCalc';

const M = (over: Partial<Metrics> = {}): Metrics => ({
  cost: 0.42,
  tokens: 100,
  prompts: 7,
  errors: 3,
  durationSeconds: 600,
  files: 9,
  ...over,
});

const deltaOf = (c: HTMLElement, k: string) =>
  c.querySelector(`[data-metric="${k}"] .conv-cmp-metric-delta`) as HTMLElement | null;

describe('ComparisonMetrics', () => {
  it('an improving lower-is-better metric (cost, B<A) → sem-improve + ▼ + SR "improved"', () => {
    const { container } = render(<ComparisonMetrics a={M({ cost: 0.42 })} b={M({ cost: 0.31 })} />);
    const d = deltaOf(container, 'cost')!;
    expect(d.className).toContain('sem-improve');
    expect(d.textContent).toContain('▼');
    expect(d.textContent).toContain('improved');
  });

  it('a worsening lower-is-better metric (cost, B>A) → sem-regress + ▲ + SR "regression"', () => {
    const { container } = render(<ComparisonMetrics a={M({ cost: 0.31 })} b={M({ cost: 0.42 })} />);
    const d = deltaOf(container, 'cost')!;
    expect(d.className).toContain('sem-regress');
    expect(d.textContent).toContain('▲');
    expect(d.textContent).toContain('regression');
  });

  it('a neutral metric (tokens) → sem-neutral with a directional arrow, never improve/regress', () => {
    const { container } = render(<ComparisonMetrics a={M({ tokens: 100 })} b={M({ tokens: 120 })} />);
    const d = deltaOf(container, 'tokens')!;
    expect(d.className).toContain('sem-neutral');
    expect(d.className).not.toContain('sem-improve');
    expect(d.className).not.toContain('sem-regress');
    expect(d.textContent).toMatch(/[▲▼]/);
  });

  it('renders no delta block for a flat metric (A === B)', () => {
    const { container } = render(<ComparisonMetrics a={M({ errors: 5 })} b={M({ errors: 5 })} />);
    expect(deltaOf(container, 'errors')).toBeNull();
  });

  it('renders an em dash for a null duration value, no delta', () => {
    const { container } = render(
      <ComparisonMetrics a={M({ durationSeconds: null })} b={M({ durationSeconds: null })} />,
    );
    const cell = container.querySelector('[data-metric="duration"]') as HTMLElement;
    expect(cell.textContent).toContain('—');
    expect(deltaOf(container, 'duration')).toBeNull();
  });

  it('a sub-hour duration delta renders compact ("−7m"), not "−0h 07m"', () => {
    const { container } = render(
      <ComparisonMetrics a={M({ durationSeconds: 14 * 60 })} b={M({ durationSeconds: 7 * 60 })} />,
    );
    expect(deltaOf(container, 'duration')!.textContent).toContain('−7m');
  });

  it('renders all six metric cells', () => {
    const { container } = render(<ComparisonMetrics a={M()} b={M()} />);
    for (const k of ['cost', 'tokens', 'prompts', 'errors', 'duration', 'files']) {
      expect(container.querySelector(`[data-metric="${k}"]`)).not.toBeNull();
    }
  });

  // #240 — JSDOM can't evaluate the @container reflow, but the strip MUST stay
  // nested inside its .conv-cmp-metrics-wrap query container; removing that
  // wrapper silently disables the container-driven 6→3→2 reflow. Guard the
  // structure so an accidental unwrap is caught here rather than only in browser.
  it('nests the strip inside its inline-size container wrapper', () => {
    const { container } = render(<ComparisonMetrics a={M()} b={M()} />);
    const strip = container.querySelector('.conv-cmp-metrics');
    expect(strip).not.toBeNull();
    expect(strip!.parentElement?.className).toContain('conv-cmp-metrics-wrap');
  });
});
