import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { ComparisonLegend } from './ComparisonLegend';

describe('ComparisonLegend', () => {
  it('keys =, −, + and divergence, plus "absent" only in wide mode', () => {
    const { container, rerender } = render(<ComparisonLegend wide />);
    const txt = container.textContent ?? '';
    expect(txt).toContain('matched');
    expect(txt).toContain('only in A');
    expect(txt).toContain('only in B');
    expect(txt).toContain('divergence');
    expect(txt).toContain('absent');
    rerender(<ComparisonLegend wide={false} />);
    expect(container.textContent).not.toContain('absent');
  });
});
