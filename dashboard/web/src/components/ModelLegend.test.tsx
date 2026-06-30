import { render } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { ModelLegend } from './ModelLegend';

describe('ModelLegend', () => {
  it('renders dot+name+% for top two and a +N overflow', () => {
    const { container } = render(<ModelLegend models={[
      { model: 'claude-opus-4-8', display: 'Opus 4.8', chip: 'opus', cost_pct: 70 },
      { model: 'claude-sonnet-5', display: 'Sonnet 5', chip: 'sonnet', cost_pct: 20 },
      { model: 'claude-haiku-4-5', display: 'Haiku', chip: 'haiku', cost_pct: 10 },
    ]} />);
    expect(container.querySelectorAll('.ms-leg').length).toBe(3); // 2 models + the +N
    expect(container.querySelector('.ms-dot.opus')).not.toBeNull();
    expect(container.querySelector('.ms-more')?.textContent).toBe('+1');
    expect(container.textContent).toContain('Opus 4.8 70%');
  });
  it('renders nothing for an empty model list', () => {
    const { container } = render(<ModelLegend models={[]} />);
    expect(container.querySelector('.model-legend')).toBeNull();
  });
});
