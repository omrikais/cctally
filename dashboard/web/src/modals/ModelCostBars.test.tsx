import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ModelCostBars } from './ModelCostBars';

describe('ModelCostBars', () => {
  it('renders one row per model with cost, relative to the top model', () => {
    render(<ModelCostBars rows={[
      { model: 'claude-opus-4-8', cost_usd: 10 },
      { model: 'claude-haiku-4-5', cost_usd: 5 },
    ]} />);
    expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument();
    expect(screen.getByText('claude-haiku-4-5')).toBeInTheDocument();
    const bars = document.querySelectorAll('.drill-bar');
    expect(bars).toHaveLength(2);
    // top model = 100%, second = 50%
    expect((bars[0] as HTMLElement).style.getPropertyValue('--w')).toBe('100%');
    expect((bars[1] as HTMLElement).style.getPropertyValue('--w')).toBe('50%');
  });

  it('renders nothing (no rows) for an empty model list', () => {
    const { container } = render(<ModelCostBars rows={[]} />);
    expect(container.querySelectorAll('.drill-bar-row')).toHaveLength(0);
  });

  it('guards a zero top-cost (no divide-by-zero, widths are 0%)', () => {
    render(<ModelCostBars rows={[{ model: 'x', cost_usd: 0 }]} />);
    const bar = document.querySelector('.drill-bar') as HTMLElement;
    expect(bar.style.getPropertyValue('--w')).toBe('0%');
  });
});
