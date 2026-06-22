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

describe('ComparisonMetrics', () => {
  it('marks a lower-is-better metric (cost) as improved when B < A', () => {
    const { container } = render(
      <ComparisonMetrics a={M({ cost: 0.42 })} b={M({ cost: 0.31 })} />,
    );
    const cell = container.querySelector('[data-metric="cost"]') as HTMLElement;
    expect(cell.className).toContain('conv-cmp-metric-down');
  });

  it('does NOT color a neutral metric (tokens) even when B differs', () => {
    const { container } = render(
      <ComparisonMetrics a={M({ tokens: 100 })} b={M({ tokens: 120 })} />,
    );
    const cell = container.querySelector('[data-metric="tokens"]') as HTMLElement;
    expect(cell.className).not.toContain('conv-cmp-metric-down');
  });

  it('does NOT mark cost down when B is higher (a regression, not an improvement)', () => {
    const { container } = render(
      <ComparisonMetrics a={M({ cost: 0.31 })} b={M({ cost: 0.42 })} />,
    );
    const cell = container.querySelector('[data-metric="cost"]') as HTMLElement;
    expect(cell.className).not.toContain('conv-cmp-metric-down');
  });

  it('renders an em dash for a null duration', () => {
    const { container } = render(
      <ComparisonMetrics a={M({ durationSeconds: null })} b={M({ durationSeconds: null })} />,
    );
    const cell = container.querySelector('[data-metric="duration"]') as HTMLElement;
    expect(cell.textContent).toContain('—');
  });

  it('renders all six metric cells', () => {
    const { container } = render(<ComparisonMetrics a={M()} b={M()} />);
    for (const k of ['cost', 'tokens', 'prompts', 'errors', 'duration', 'files']) {
      expect(container.querySelector(`[data-metric="${k}"]`)).not.toBeNull();
    }
  });
});
